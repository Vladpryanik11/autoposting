"""Конвейер публикаций: план → подготовка → (одобрение) → Postmypost → проверка статуса.

Все методы идемпотентны и безопасны для повторного вызова: рабочий процесс вызывает их
по кругу, а после сбоя продолжает с того же места.
"""
import json
import logging
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app import planner
from app import postmypost as pmp_api
from app.captions import Captioner
from app.config import Settings
from app.db import (DOWNLOADED, DRAFTED, FAILED, PLANNED, PUBLISHED, READY, REJECTED, RELEASED, STATUS_LABELS,
                    REVIEW, SCHEDULED, get_kv, log_event, notify, now)
from app.planner import from_iso

log = logging.getLogger(__name__)

PUBLISH_MIN_LEAD = timedelta(minutes=2)   # позже этого отправлять в Postmypost уже поздно
SYNC_DELAY = timedelta(minutes=5)         # через сколько после слота проверять, вышел ли пост
STUCK_AFTER = timedelta(hours=6)          # не вышел за это время — считаем ошибкой
PLATFORM_NAMES = {"instagram": "Instagram", "tiktok": "TikTok", "youtube": "YouTube", "other": "другое"}
PLATFORM_ICON = {"instagram": "📸", "tiktok": "🎵", "youtube": "▶️", "other": "🌐"}


def default_render(cfg: Settings):
    def render(src: Path, dst: Path) -> dict:
        from app.uniq import cleanup_reports, uniqualize_checked
        dst.parent.mkdir(parents=True, exist_ok=True)
        res = uniqualize_checked(str(src), str(dst), cfg.uniq_max_similarity, cfg.uniq_intensity,
                                 fmt="reels", encoder=cfg.uniq_encoder or None)
        cleanup_reports(str(dst))
        return {"similarity": res["similarity"], "intensity": res["intensity"]}
    return render


class Pipeline:
    def __init__(self, conn: sqlite3.Connection, cfg: Settings, *, pmp=None,
                 captioner: Captioner | None = None, render=None, clock=None,
                 rng: random.Random | None = None):
        self.conn, self.cfg, self.pmp = conn, cfg, pmp
        self.captioner = captioner or Captioner(cfg.anthropic_api_key, cfg.caption_model)
        self.render = render or default_render(cfg)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.rng = rng or random.Random()
        self.tz = ZoneInfo(cfg.timezone)

    # --- вспомогательное --------------------------------------------------------

    def local(self, iso: str) -> str:
        return from_iso(iso).astimezone(self.tz).strftime("%d.%m %H:%M")

    def _row(self, pub_id: int) -> sqlite3.Row | None:
        return self.conn.execute("""
            SELECT x.*, d.name AS dest_name, d.platform, d.pmp_account_id,
                   p.caption AS original_caption, p.video_path, p.status AS post_status, p.url AS orig_url,
                   s.username
            FROM publications x
            JOIN destinations d ON d.id = x.destination_id
            JOIN source_posts p ON p.id = x.source_post_id
            JOIN sources s ON s.id = p.source_id
            WHERE x.id = ?""", (pub_id,)).fetchone()

    def _set(self, pub_id: int, **fields) -> None:
        fields["updated_at"] = now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE publications SET {sets} WHERE id = ?", (*fields.values(), pub_id))

    def _drop_variant(self, pub: sqlite3.Row) -> None:
        if pub["variant_path"]:
            (self.cfg.data_dir / pub["variant_path"]).unlink(missing_ok=True)

    def _fail(self, pub: sqlite3.Row, error: str, final: bool = False) -> None:
        attempts = pub["attempts"] + 1
        if final or attempts >= self.cfg.max_attempts:
            self._drop_variant(pub)
            self._set(pub["id"], status=FAILED, attempts=attempts, error=error, variant_path=None)
            notify(self.conn, "error",
                   f"❌ Публикация №{pub['id']} ({pub['dest_name']}, {self.local(pub['slot_at'])}) "
                   f"не вышла: {error}\nОстальные публикации идут по плану.", pub["id"])
        else:
            self._set(pub["id"], attempts=attempts, error=error)
        log_event(self.conn, "publication", pub["id"], "failed", {"error": error, "attempt": attempts})
        log.warning("Публикация %s: %s (попытка %d)", pub["id"], error, attempts)

    # --- этапы ------------------------------------------------------------------

    def plan(self) -> planner.PlanReport:
        return planner.plan(self.conn, self.cfg, self.clock(), self.rng)

    def prepare(self, limit: int = 3) -> int:
        """Подпись + уникальная копия для ближайших запланированных публикаций."""
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM publications WHERE status = ? ORDER BY slot_at LIMIT ?", (PLANNED, limit))]
        done = 0
        for pub_id in ids:
            pub = self._row(pub_id)
            if pub and pub["status"] == PLANNED:
                done += self._prepare_one(pub)
        return done

    def _prepare_one(self, pub: sqlite3.Row) -> int:
        src = self.cfg.data_dir / (pub["video_path"] or "")
        if pub["post_status"] != DOWNLOADED or not pub["video_path"] or not src.exists():
            self._fail(pub, "исходное видео не найдено на диске", final=True)
            return 0
        cap = self.captioner.make(pub["original_caption"], pub["platform"], get_kv(self.conn, "review_mode"))
        dst = self.cfg.variant_dir / f"{pub['id']}.mp4"
        try:
            res = self.render(src, dst)
        except Exception as e:  # noqa: BLE001 — любая ошибка кодирования = неудачная попытка
            dst.unlink(missing_ok=True)
            self._fail(pub, f"уникализация: {str(e)[:300]}")
            return 0
        status = REVIEW if cap.review_reason else READY
        self._set(pub["id"], status=status, caption=cap.text, title=cap.title,
                  review_reason=cap.review_reason,
                  variant_path=dst.relative_to(self.cfg.data_dir).as_posix(),
                  similarity=res.get("similarity"), intensity=res.get("intensity"), error=None)
        log_event(self.conn, "publication", pub["id"], "prepared",
                  {"status": status, "similarity": res.get("similarity"), "rewritten": cap.rewritten})
        if status == REVIEW:
            notify(self.conn, "review", self.review_text(pub["id"]), pub["id"])
        return 1

    def publish(self) -> int:
        """Готовые публикации → Postmypost (заранее, на время слота)."""
        if not self.pmp or not self.cfg.postmypost_project_id:
            return 0
        cutoff = planner.to_iso(self.clock() + PUBLISH_MIN_LEAD)
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM publications WHERE status = ? AND slot_at > ? ORDER BY slot_at", (READY, cutoff))]
        done = 0
        for pub_id in ids:
            pub = self._row(pub_id)
            if pub and pub["status"] == READY:
                done += self._publish_one(pub)
        return done

    def _publish_one(self, pub: sqlite3.Row) -> int:
        project = self.cfg.postmypost_project_id
        draft = get_kv(self.conn, "draft_mode") == "1"
        slot_at = from_iso(pub["slot_at"])
        try:
            file_id = pub["pmp_file_id"]
            if not file_id:
                path = self.cfg.data_dir / (pub["variant_path"] or "")
                if not pub["variant_path"] or not path.exists():
                    self._set(pub["id"], status=PLANNED, variant_path=None)  # копия пропала — пересобрать
                    return 0
                file_id = self.pmp.upload(project, path)
                self._set(pub["id"], pmp_file_id=file_id)  # сохраняем сразу: повторно не загружаем
            pub_pmp_id = None
            if pub["attempts"]:  # прошлая попытка могла создать публикацию и упасть до сохранения id
                pub_pmp_id = self.pmp.find_publication(project, slot_at, pub["pmp_account_id"])
            if not pub_pmp_id:
                res = self.pmp.publish(project, [pub["pmp_account_id"]], [file_id],
                                       text=pub["caption"] or "", title=pub["title"] or "",
                                       post_at=slot_at.astimezone(self.tz),
                                       status=pmp_api.DRAFT if draft else pmp_api.PENDING,
                                       platform=pub["platform"])
                pub_pmp_id = res.get("id") or (res.get("data") or {}).get("id")
        except Exception as e:  # noqa: BLE001 — сеть, API, S3: попробуем в следующий раз
            self._fail(pub, f"Postmypost: {str(e)[:300]}")
            return 0
        self._drop_variant(pub)
        self._set(pub["id"], status=DRAFTED if draft else SCHEDULED, pmp_publication_id=pub_pmp_id,
                  is_draft=int(draft), variant_path=None, error=None)
        log_event(self.conn, "publication", pub["id"], "scheduled",
                  {"pmp_publication_id": pub_pmp_id, "draft": draft})
        return 1

    def release_missed(self) -> int:
        """Слот наступил, а публикация не отправлена — освобождаем ролик для будущих слотов."""
        cutoff = planner.to_iso(self.clock() + PUBLISH_MIN_LEAD)
        rows = self.conn.execute(
            "SELECT id FROM publications WHERE status IN (?, ?, ?) AND slot_at <= ?",
            (PLANNED, REVIEW, READY, cutoff)).fetchall()
        for r in rows:
            pub = self._row(r["id"])
            self._drop_variant(pub)
            self._set(pub["id"], status=RELEASED, variant_path=None)
            log_event(self.conn, "publication", pub["id"], "missed", {"was": pub["status"]})
            if pub["status"] == REVIEW:
                notify(self.conn, "info", f"⏰ Публикация №{pub['id']} ({pub['dest_name']}) не проверена до "
                                          f"{self.local(pub['slot_at'])} — это время пропущено, ролик вернётся "
                                          f"в очередь.", pub["id"])
        return len(rows)

    def sync(self, limit: int = 20) -> int:
        """Проверяет в Postmypost, вышли ли запланированные посты."""
        if not self.pmp:
            return 0
        t = self.clock()
        rows = self.conn.execute("SELECT id FROM publications WHERE status = ? AND slot_at < ? "
                                 "ORDER BY slot_at LIMIT ?",
                                 (SCHEDULED, planner.to_iso(t - SYNC_DELAY), limit)).fetchall()
        changed = 0
        for r in rows:
            pub = self._row(r["id"])
            try:
                data = self.pmp.publication(pub["pmp_publication_id"])
            except Exception as e:  # noqa: BLE001
                log.warning("sync #%s: %s", pub["id"], e)
                continue
            st = data.get("publication_status")
            if st == pmp_api.ST_PUBLISHED:
                self._set(pub["id"], status=PUBLISHED, error=None, published_at=pub["slot_at"])
                log_event(self.conn, "publication", pub["id"], "published")
                changed += 1
            elif st in (pmp_api.ST_ERROR, pmp_api.ST_DELETED, pmp_api.ST_NOT_DELETED):
                why = {pmp_api.ST_ERROR: "соцсеть не приняла пост — причина видна в кабинете Postmypost",
                       pmp_api.ST_DELETED: "публикацию удалили в Postmypost",
                       pmp_api.ST_NOT_DELETED: "ошибка удаления в Postmypost"}[st]
                log.warning("sync #%s: %s", pub["id"], json.dumps(data, ensure_ascii=False)[:500])
                self._fail(pub, why, final=True)
                changed += 1
            elif t - from_iso(pub["slot_at"]) > STUCK_AFTER:
                self._fail(pub, f"не вышла за {STUCK_AFTER.seconds // 3600} ч — проверь её в кабинете Postmypost",
                           final=True)
                changed += 1
        self.fetch_links()
        return changed

    def fetch_links(self, limit: int = 5, max_checks: int = 5) -> int:
        """Ссылка на вышедший пост — из аналитики Postmypost (появляется не сразу, поэтому несколько попыток).
        Сопоставляем по времени выхода (±3 ч) и началу подписи."""
        if not (self.pmp and self.cfg.postmypost_project_id and hasattr(self.pmp, "published_posts")):
            return 0
        since = planner.to_iso(self.clock() - timedelta(days=3))
        rows = self.conn.execute(
            "SELECT id FROM publications WHERE status = ? AND post_url IS NULL AND link_checks < ? "
            "AND slot_at > ? ORDER BY slot_at LIMIT ?", (PUBLISHED, max_checks, since, limit)).fetchall()
        found = 0
        for r in rows:
            pub = self._row(r["id"])
            slot = from_iso(pub["slot_at"])
            local_day = slot.astimezone(self.tz).date()
            try:
                posts = self.pmp.published_posts(self.cfg.postmypost_project_id, pub["pmp_account_id"],
                                                 local_day - timedelta(days=1), local_day + timedelta(days=1))
            except Exception as e:  # noqa: BLE001
                log.warning("ссылка на пост #%s: %s", pub["id"], e)
                self._set(pub["id"], link_checks=pub["link_checks"] + 1)
                continue
            head = (pub["caption"] or "")[:40].strip().lower()
            best, best_score = None, None
            for item in posts:
                try:
                    delta = abs((datetime.fromisoformat(item["created_at"]) - slot).total_seconds())
                except (KeyError, TypeError, ValueError):
                    continue
                if delta > 3 * 3600:
                    continue
                score = delta - (3600 if head and head in (item.get("content") or "").lower() else 0)
                if best_score is None or score < best_score:
                    best, best_score = item, score
            fields = {"link_checks": pub["link_checks"] + 1}
            if best and best.get("external_url"):
                fields |= {"post_url": best["external_url"], "external_id": best.get("external_id"),
                           "published_at": best.get("created_at") or pub["published_at"]}
                found += 1
            self._set(pub["id"], **fields)
        return found

    def unschedule(self, destination_id: int) -> int:
        """Снимает будущие публикации аккаунта (после смены слотов/группы/выключения)."""
        cutoff = planner.to_iso(self.clock())
        rows = self.conn.execute(
            "SELECT id FROM publications WHERE destination_id = ? AND slot_at > ? AND status IN (?, ?, ?, ?, ?)",
            (destination_id, cutoff, PLANNED, REVIEW, READY, SCHEDULED, DRAFTED)).fetchall()
        released = 0
        for r in rows:
            pub = self._row(r["id"])
            if pub["pmp_publication_id"] and pub["status"] in (SCHEDULED, DRAFTED):
                try:
                    self.pmp.delete_publication(pub["pmp_publication_id"], [pub["pmp_account_id"]])
                except Exception as e:  # noqa: BLE001
                    notify(self.conn, "error", f"⚠️ Не получилось снять публикацию №{pub['id']} в Postmypost: {e}",
                           pub["id"])
                    continue
            self._drop_variant(pub)
            self._set(pub["id"], status=RELEASED, variant_path=None)
            log_event(self.conn, "publication", pub["id"], "unscheduled")
            released += 1
        return released

    def cancel(self, pub_id: int) -> str:
        """«Не публиковать это видео в этот аккаунт»: снять (и в Postmypost) и больше не планировать."""
        pub = self._row(pub_id)
        if not pub:
            return f"Публикация №{pub_id} не найдена"
        if pub["status"] not in (PLANNED, REVIEW, READY, SCHEDULED, DRAFTED):
            return f"Публикацию №{pub_id} уже нельзя снять — она {STATUS_LABELS.get(pub['status'], pub['status'])}"
        if pub["status"] in (SCHEDULED, DRAFTED) and pub["pmp_publication_id"]:
            if from_iso(pub["slot_at"]) <= self.clock():
                return f"Публикация №{pub_id} уже должна была выйти — снять нельзя"
            self.pmp.delete_publication(pub["pmp_publication_id"], [pub["pmp_account_id"]])
        self._drop_variant(pub)
        self._set(pub_id, status=REJECTED, variant_path=None)
        log_event(self.conn, "publication", pub_id, "cancelled")
        return f"🚫 Публикация №{pub_id} снята: это видео в {pub['dest_name']} не пойдёт, в это время выйдет другое."

    def release_future_drafts(self) -> int:
        """После выключения режима черновиков: будущие черновики → удалить и пересобрать настоящими."""
        rows = self.conn.execute("SELECT id FROM publications WHERE status = ? AND slot_at > ?",
                                 (DRAFTED, planner.to_iso(self.clock()))).fetchall()
        released = 0
        for r in rows:
            pub = self._row(r["id"])
            try:
                if pub["pmp_publication_id"] and self.pmp:
                    self.pmp.delete_publication(pub["pmp_publication_id"], [pub["pmp_account_id"]])
            except Exception as e:  # noqa: BLE001
                notify(self.conn, "error", f"⚠️ Не получилось удалить черновик №{pub['id']} в Postmypost: {e}")
                continue
            self._set(pub["id"], status=RELEASED)
            log_event(self.conn, "publication", pub["id"], "undrafted")
            released += 1
        return released

    def review(self, pub_id: int, approve: bool, caption: str | None = None) -> str:
        pub = self._row(pub_id)
        if not pub:
            return f"Публикация №{pub_id} не найдена"
        if pub["status"] != REVIEW:
            return f"Публикация №{pub_id} уже не ждёт проверки — она {STATUS_LABELS.get(pub['status'], pub['status'])}"
        if approve:
            fields = {"status": READY, "review_reason": None}
            if caption and caption.strip():
                from app.captions import make_title
                fields |= {"caption": caption.strip(), "title": make_title(caption.strip())}
            self._set(pub_id, **fields)
            log_event(self.conn, "publication", pub_id, "approved", {"edited": bool(caption)})
            return f"✅ Публикация №{pub_id} одобрена" + (" с новым текстом" if caption else "")
        self._drop_variant(pub)
        self._set(pub_id, status=REJECTED, variant_path=None)
        log_event(self.conn, "publication", pub_id, "rejected")
        return f"❌ Публикация №{pub_id} отклонена — этот ролик в {pub['dest_name']} больше не пойдёт"

    # --- тексты для бота и сводки ------------------------------------------------------

    def review_text(self, pub_id: int) -> str:
        """Текст уведомления о проверке без бота (в лог). В боте показывается карточка публикации."""
        p = self._row(pub_id)
        return (f"🔎 Нужна проверка: публикация №{p['id']}\n"
                f"{p['dest_name']} ({PLATFORM_NAMES.get(p['platform'], p['platform'])}), "
                f"{self.local(p['slot_at'])}, ролик из @{p['username']}\n"
                f"Почему: {p['review_reason']}\n"
                f"Оригинал: {p['orig_url']}\n"
                f"———\n{p['caption'] or '(без подписи)'}")

    def dest_line(self, d: sqlite3.Row, html: bool = False) -> str:
        """Одна строка про аккаунт — одинаковая в статусе бота и в ежедневной сводке."""
        from html import escape
        icon = PLATFORM_ICON.get(d["platform"], "🌐")
        name = f"<b>{escape(d['name'])}</b>" if html else d["name"]
        if not d["enabled"]:
            return f"{icon} {name} — ⏸ выключен"
        if d["start_date"] and d["start_date"] > self.clock().astimezone(self.tz).date().isoformat():
            return f"{icon} {name} — 🕓 начнёт с {d['start_date'][8:10]}.{d['start_date'][5:7]}"
        queued = self.conn.execute(
            "SELECT COUNT(*) FROM publications WHERE destination_id = ? AND slot_at > ? AND status IN (?, ?, ?, ?, ?)",
            (d["id"], planner.to_iso(self.clock()), PLANNED, REVIEW, READY, SCHEDULED, DRAFTED)).fetchone()[0]
        spare = len(planner.available_posts(self.conn, d))
        empty = " ⚠️ пусто — пришли ссылки" if not queued and not spare else ""
        return f"{icon} {name}: в очереди {queued + spare}{empty}"

    def queue_lines(self) -> list[str]:
        rows = self.conn.execute("SELECT * FROM destinations ORDER BY route_group, name").fetchall()
        lines = [self.dest_line(d) for d in rows]
        return lines or ["Аккаунтов нет — в боте: 📤 Аккаунты → 🔄 Подтянуть аккаунты"]

    def status_text(self) -> str:
        flags = ["⏸ на паузе" if get_kv(self.conn, "paused") == "1" else "▶️ работает",
                 "📝 черновики" if get_kv(self.conn, "draft_mode") == "1" else "🚀 боевой режим",
                 f"проверка: {get_kv(self.conn, 'review_mode')}"]
        posts = dict(self.conn.execute("SELECT status, COUNT(*) FROM source_posts GROUP BY status").fetchall())
        upcoming = self.conn.execute(
            "SELECT x.id, x.slot_at, x.status, d.name FROM publications x JOIN destinations d "
            "ON d.id = x.destination_id WHERE x.status IN (?, ?, ?, ?, ?) ORDER BY x.slot_at LIMIT 8",
            (PLANNED, REVIEW, READY, SCHEDULED, DRAFTED)).fetchall()
        up = [f"  {self.local(u['slot_at'])} {u['name']} — {STATUS_LABELS.get(u['status'], u['status'])} "
              f"(№{u['id']})" for u in upcoming]
        return "\n".join([
            " · ".join(flags),
            f"Ролики: скачано {posts.get(DOWNLOADED, 0)}, не скачались {posts.get(FAILED, 0)}, "
            f"ждут скачивания {posts.get('DISCOVERED', 0) + posts.get('NEEDS_REFRESH', 0)}",
            "", "Аккаунты:", *self.queue_lines(),
            "", "Ближайшие:", *(up or ["  пусто"]),
        ])

    def daily_summary(self) -> str:
        since = planner.to_iso(self.clock() - timedelta(days=1))
        count = lambda sql, *a: self.conn.execute(sql, a).fetchone()[0]  # noqa: E731
        published = count("SELECT COUNT(*) FROM publications WHERE status = ? AND updated_at > ?",
                          PUBLISHED, since)
        failed = count("SELECT COUNT(*) FROM publications WHERE status = ? AND updated_at > ?", FAILED, since)
        missed = count("SELECT COUNT(*) FROM events WHERE entity = 'publication' AND event = 'missed' "
                       "AND created_at > ?", since)
        new = count("SELECT COUNT(*) FROM source_posts WHERE created_at > ?", since)
        return "\n".join([
            "📊 Сводка за сутки",
            f"Вышло постов: {published} · ошибок: {failed} · пропущено: {missed} · новых роликов: {new}",
            "", *self.queue_lines(),
        ])

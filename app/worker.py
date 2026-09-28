"""Рабочий процесс: крутит конвейер по кругу (отдельный поток рядом с ботом).

Один тик:
  1. поручения от бота (забрать ролики сейчас, снять расписание аккаунта);
  2. раз в ingest_every_min — сбор новых роликов через Apify;
  3. упущенные слоты → ролики обратно в очередь;
  4. если не на паузе: план → подготовка (до 2 роликов за тик) → отправка в Postmypost;
  5. раз в sync_every_min — проверка, вышли ли посты;
  6. раз в сутки в summary_time — сводка в бот.
"""
import json
import logging
import threading
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app import db
from app.captions import Captioner
from app.config import Settings
from app.pipeline import Pipeline

log = logging.getLogger(__name__)
ERROR_REPEAT = timedelta(hours=1)  # одинаковую ошибку повторяем в бот не чаще раза в час
STEP_NAMES = {"ingest": "сбор роликов", "release": "возврат пропущенных публикаций",
              "plan": "составление расписания", "prepare": "подготовка роликов",
              "publish": "отправка в Postmypost", "sync": "проверка вышедших постов", "summary": "сводка"}
COMMAND_NAMES = {"fetch": "сбор роликов", "links": "рилсы по ссылкам", "cancel": "снятие публикации",
                 "undraft": "переход в боевой режим", "unschedule": "пересборка расписания"}


class Worker:
    def __init__(self, cfg: Settings, *, apify=None, pmp=None, captioner=None, render=None,
                 clock=None, echo_outbox: bool = False):
        self.cfg = cfg
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.echo_outbox = echo_outbox  # без Telegram-бота — печатать уведомления в лог
        self.apify = apify if apify is not None else self._make_apify()
        self.pmp = pmp if pmp is not None else self._make_pmp()
        self.captioner = captioner or Captioner(cfg.anthropic_api_key, cfg.caption_model)
        self.render = render

    def _make_apify(self):
        if not self.cfg.apify_token:
            log.warning("APIFY_TOKEN не задан — сбор новых роликов выключен")
            return None
        from app.apify import Apify
        return Apify(self.cfg.apify_token)

    def _make_pmp(self):
        if not (self.cfg.postmypost_token and self.cfg.postmypost_project_id):
            log.warning("POSTMYPOST_TOKEN/POSTMYPOST_PROJECT_ID не заданы — отправка в Postmypost выключена")
            return None
        from app.postmypost import Postmypost
        return Postmypost(self.cfg.postmypost_token)

    # --- цикл ----------------------------------------------------------------------

    def run_forever(self, stop: threading.Event, interval: float = 20.0) -> None:
        log.info("Рабочий процесс запущен")
        while not stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 — поток не должен умирать молча (например, база занята)
                log.exception("Тик рабочего процесса упал")
                try:
                    conn = db.connect(self.cfg.db_path)
                    db.notify(conn, "error", f"⚠️ Сбой рабочего процесса: {type(e).__name__}: {str(e)[:200]}. "
                                             f"Повторю через минуту.")
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            stop.wait(interval)
        log.info("Рабочий процесс остановлен")

    def tick(self) -> None:
        conn = db.connect(self.cfg.db_path)
        try:
            p = Pipeline(conn, self.cfg, pmp=self.pmp, captioner=self.captioner, render=self.render,
                         clock=self.clock)
            fetch_now = self._run_commands(conn, p)
            if fetch_now:  # ручной сбор засчитывается как плановый — не платим сразу второй раз
                db.set_kv(conn, "last_run:ingest", self.clock().isoformat())
            self._step(conn, "ingest", lambda: self._ingest(conn, manual=fetch_now),
                       fetch_now or self._due(conn, "ingest", self.cfg.ingest_every_min),
                       always_report=fetch_now)
            self._step(conn, "release", p.release_missed)
            if db.get_kv(conn, "paused") != "1":
                self._step(conn, "plan", p.plan)
                self._step(conn, "prepare", lambda: p.prepare(limit=2))
                self._step(conn, "publish", p.publish)
            self._step(conn, "sync", p.sync, self._due(conn, "sync", self.cfg.sync_every_min))
            self._step(conn, "summary", lambda: db.notify(conn, "summary", p.daily_summary()),
                       self._summary_due(conn))
            if self.echo_outbox:
                self._echo_outbox(conn)
        finally:
            conn.close()

    def _step(self, conn, name: str, fn, when: bool = True, always_report: bool = False) -> None:
        """Шаг изолирован: его ошибка не останавливает остальные и приходит в бот (без спама)."""
        if not when:
            return
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            log.exception("Шаг %s упал", name)
            key, text = f"last_error:{name}", f"{type(e).__name__}: {str(e)[:300]}"
            last = db.get_kv(conn, key)
            stamp = self.clock().isoformat()
            if always_report or not last or last.split("|", 1)[1:] != [text] or \
                    self.clock() - datetime.fromisoformat(last.split("|", 1)[0]) > ERROR_REPEAT:
                db.notify(conn, "error", f"⚠️ Сбой: {STEP_NAMES.get(name, name)}. Остальное работает, "
                                         f"попробую снова через минуту.\nЕсли сообщение повторяется — перешли "
                                         f"его тому, кто настраивал бота.\nПодробности: {text}")
                db.set_kv(conn, key, f"{stamp}|{text}")

    # --- расписание шагов -------------------------------------------------------------

    def _due(self, conn, job: str, every_min: int) -> bool:
        key = f"last_run:{job}"
        last = db.get_kv(conn, key)
        t = self.clock()
        if last and t - datetime.fromisoformat(last) < timedelta(minutes=every_min):
            return False
        db.set_kv(conn, key, t.isoformat())
        return True

    def _summary_due(self, conn) -> bool:
        local = self.clock().astimezone(ZoneInfo(self.cfg.timezone))
        hh, mm = map(int, self.cfg.summary_time.split(":"))
        if local.time() < time(hh, mm) or db.get_kv(conn, "last_summary_date") == local.date().isoformat():
            return False
        db.set_kv(conn, "last_summary_date", local.date().isoformat())
        return True

    # --- действия ---------------------------------------------------------------------

    def _ingest(self, conn, manual: bool = False) -> None:
        if not self.apify:
            if manual:
                db.notify(conn, "error", "⚠️ Не могу забрать ролики: не задан APIFY_TOKEN в .env.")
            return
        from app.ingest import Ingestor
        rep = Ingestor(conn, self.apify, self.cfg).run()
        log.info("Сбор: новых %d, скачано %d, ошибок %d", rep.new, rep.downloaded, rep.failed)
        if rep.errors:
            db.notify(conn, "error", "⚠️ Сбор роликов:\n" + "\n".join(rep.errors[:10]))
        if manual:  # по кнопке — всегда отчитываемся; по расписанию — только об ошибках
            if not conn.execute("SELECT 1 FROM sources WHERE enabled = 1").fetchone():
                text = "📥 Источников нет — добавь основной аккаунт: 📥 Источники → ➕ Добавить источник."
            else:
                text = (f"📥 Сбор роликов готов: новых {rep.new}, скачано {rep.downloaded}"
                        + (f", повторов {rep.duplicates}" if rep.duplicates else "")
                        + (f", не скачалось {rep.failed}" if rep.failed else "")
                        + ".")
            db.notify(conn, "info", text)

    def _add_links(self, conn, arg: dict) -> str:
        links = [tuple(x) for x in arg["links"]]
        if not self.apify:
            text = "⚠️ Не могу скачать рилсы по ссылкам: не задан APIFY_TOKEN в .env."
        else:
            from app.ingest import Ingestor
            rep = Ingestor(conn, self.apify, self.cfg).add_links(links, arg.get("group", "default"))
            parts = [f"новых {rep.new}", f"скачано {rep.downloaded}"]
            if rep.known:
                parts.append(f"уже были в базе {rep.known} — поставлены в начало очереди")
            if rep.not_found:
                parts.append(f"не найдено {rep.not_found} (удалён или закрытый аккаунт)")
            if rep.duplicates:
                parts.append(f"повторов {rep.duplicates}")
            if rep.failed:
                parts.append(f"не скачалось {rep.failed}")
            text = f"🔗 Рилсы по ссылкам ({len(links)}): " + ", ".join(parts) + ". Они пойдут в публикации первыми."
            if rep.errors:
                text += "\n⚠️ " + "\n".join(rep.errors[:5])
        db.notify(conn, "info", text)
        return text

    def _run_commands(self, conn, p: Pipeline) -> bool:
        fetch = False
        for c in conn.execute("SELECT * FROM commands WHERE done_at IS NULL ORDER BY id").fetchall():
            result = "ok"
            try:
                if c["kind"] == "fetch":
                    fetch = True
                elif c["kind"] == "links":
                    result = self._add_links(conn, json.loads(c["arg"]))
                elif c["kind"] == "cancel":
                    result = p.cancel(int(c["arg"]))
                    db.notify(conn, "info", result, int(c["arg"]))
                elif c["kind"] == "undraft":
                    n = p.release_future_drafts()
                    result = f"черновиков пересоздаётся: {n}"
                    if n:
                        db.notify(conn, "info", f"🚀 Будущих черновиков: {n} — удалены в Postmypost и будут "
                                                f"поставлены заново как настоящие публикации.")
                elif c["kind"] == "unschedule":
                    n = p.unschedule(int(c["arg"]))
                    name = conn.execute("SELECT name FROM destinations WHERE id = ?", (int(c["arg"]),)).fetchone()
                    result = f"снято публикаций: {n}"
                    if n:
                        db.notify(conn, "info", f"🔄 {name['name'] if name else c['arg']}: снято будущих "
                                                f"публикаций — {n}, расписание пересоберётся.")
                else:
                    result = "неизвестная команда"
            except Exception as e:  # noqa: BLE001
                log.exception("Команда %s упала", c["kind"])
                result = f"ошибка: {e}"
                db.notify(conn, "error", f"⚠️ Не получилось: {COMMAND_NAMES.get(c['kind'], c['kind'])}. "
                                         f"Попробуй ещё раз.\nПодробности: {str(e)[:300]}")
            conn.execute("UPDATE commands SET done_at = ?, result = ? WHERE id = ?", (db.now(), result, c["id"]))
        return fetch

    def _echo_outbox(self, conn) -> None:
        for m in conn.execute("SELECT * FROM outbox WHERE sent_at IS NULL ORDER BY id").fetchall():
            log.info("[%s] %s", m["kind"], m["text"])
            conn.execute("UPDATE outbox SET sent_at = ? WHERE id = ?", (db.now(), m["id"]))

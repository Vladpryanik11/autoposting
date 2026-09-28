"""Сбор роликов с основных аккаунтов: Apify → source_posts → исходники на диске.

Один вызов Ingestor.run():
  1. дожидается запусков Apify, прерванных прошлым вызовом (не платим дважды);
  2. новым аккаунтам — архивный сбор (по одному запуску на аккаунт);
  3. остальным — сбор роликов, вышедших с прошлой проверки (+30 мин запаса; дубли отсекает shortcode);
  4. скачивает найденное; если ссылка протухла — перезапрашивает рилс по его URL.
"""
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from app import media
from app.apify import TERMINAL, Apify, cost_cap
from app.config import Settings
from app.db import (DISCOVERED, DOWNLOADED, DUPLICATE, FAILED, NEEDS_REFRESH, log_event, now,
                    tx)

INCREMENTAL_OVERLAP = timedelta(minutes=30)  # запас на задержку появления рилса в выдаче Instagram


@dataclass
class Report:
    runs: int = 0
    items: int = 0
    new: int = 0
    downloaded: int = 0
    duplicates: int = 0
    refreshed: int = 0
    failed: int = 0
    skipped: int = 0
    known: int = 0        # ссылки на рилсы, которые уже были в базе
    not_found: int = 0    # ссылки, по которым Apify ничего не вернул (удалён, закрытый аккаунт)
    errors: list[str] = field(default_factory=list)


class Ingestor:
    def __init__(self, conn: sqlite3.Connection, apify: Apify, settings: Settings,
                 http: requests.Session | None = None):
        self.conn, self.apify, self.cfg = conn, apify, settings
        self.http = http or requests.Session()
        self.report = Report()
        self._refreshed_ids: set[int] = set()
        self._scraped_sources: set[int] = set()  # уже собраны в этом вызове — второй раз не платим

    def run(self) -> Report:
        for row in self.conn.execute(
                f"SELECT * FROM apify_runs WHERE status NOT IN ({','.join('?' * len(TERMINAL))})",
                tuple(TERMINAL)).fetchall():
            self._finish(row)
        self._scrape_archive()
        self._scrape_incremental()
        self._download_pending()
        self._refresh_expired()
        self._download_pending(only=self._refreshed_ids)
        return self.report

    def add_links(self, links: list[tuple[str, str]], group: str = "default") -> Report:
        """Рилсы, присланные ссылками: скачать и поставить в очередь первыми."""
        codes = [sc for sc, _ in links]
        marks = ",".join("?" * len(codes))
        rows = self.conn.execute(f"SELECT id, shortcode, status, sha256 FROM source_posts "
                                 f"WHERE shortcode IN ({marks})", codes).fetchall() if codes else []
        known = set()
        for r in rows:
            if r["status"] in (DOWNLOADED, DISCOVERED, NEEDS_REFRESH):  # есть — просто поднимаем первым
                known.add(r["shortcode"])
                self.conn.execute("UPDATE source_posts SET priority = 1 WHERE id = ?", (r["id"],))
            elif r["status"] == DUPLICATE:  # это повтор уже скачанного ролика — поднимаем оригинал
                known.add(r["shortcode"])
                self.conn.execute("UPDATE source_posts SET priority = 1 WHERE sha256 = ? AND status = ?",
                                  (r["sha256"], DOWNLOADED))
            elif r["status"] == FAILED:  # раньше не скачался — ещё одна попытка со свежей ссылкой
                self.conn.execute("UPDATE source_posts SET status = ?, attempts = 0, error = NULL, "
                                  "priority = 1, updated_at = ? WHERE id = ?", (NEEDS_REFRESH, now(), r["id"]))
        self.report.known = len(known)
        todo = [(sc, url) for sc, url in links if sc not in known]
        if todo:
            self._start("links", {**self._base_input(), "username": [u for _, u in todo], "resultsLimit": 1},
                        cost_cap(len(todo)), {"shortcodes": [sc for sc, _ in todo], "group": group})
        self._download_pending()
        return self.report

    # --- запуски Apify ---------------------------------------------------------

    def _base_input(self) -> dict:
        return {"skipTrialReels": True, "includeDownloadedVideo": self.cfg.apify_downloaded_video}

    def _scrape_archive(self) -> None:
        for src in self.conn.execute(
                "SELECT * FROM sources WHERE enabled = 1 AND archive_done = 0 AND kind = 'profile'").fetchall():
            limit = self.cfg.apify_archive_limit
            self._start("archive", {**self._base_input(), "username": [src["username"]],
                                    "resultsLimit": limit},
                        cost_cap(limit), {"source_ids": [src["id"]]})

    def _scrape_incremental(self) -> None:
        srcs = [s for s in self.conn.execute(
                    "SELECT * FROM sources WHERE enabled = 1 AND archive_done = 1 AND kind = 'profile' "
                    "AND last_scraped_at IS NOT NULL").fetchall()
                if s["id"] not in self._scraped_sources]
        if not srcs:
            return
        # Apify берёт деньги за каждый возвращённый ролик — окно ровно с прошлой проверки + запас
        since = min(datetime.fromisoformat(s["last_scraped_at"]) for s in srcs) - INCREMENTAL_OVERLAP
        limit = self.cfg.apify_incremental_limit
        self._start("incremental", {**self._base_input(),
                                    "username": [s["username"] for s in srcs],
                                    "resultsLimit": limit,
                                    "onlyPostsNewerThan": since.astimezone(timezone.utc)
                                    .strftime("%Y-%m-%dT%H:%M:%S")},
                    cost_cap(limit * len(srcs)), {"source_ids": [s["id"] for s in srcs]})

    def _refresh_expired(self) -> None:
        posts = self.conn.execute("SELECT shortcode, url FROM source_posts WHERE status = ?",
                                  (NEEDS_REFRESH,)).fetchall()
        if posts:
            self._start("refresh", {**self._base_input(), "username": [p["url"] for p in posts],
                                    "resultsLimit": 1},
                        cost_cap(len(posts)), {"shortcodes": [p["shortcode"] for p in posts]})

    def _start(self, mode: str, run_input: dict, cap: float, meta: dict) -> None:
        run = self.apify.start_run(self.cfg.apify_actor, run_input, cap)
        # run_id сохраняем ДО ожидания: если процесс упадёт, следующий вызов его дождётся
        cur = self.conn.execute(
            "INSERT INTO apify_runs (run_id, dataset_id, mode, meta, status, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run["id"], run["defaultDatasetId"], mode, json.dumps(meta), run["status"], now()))
        log_event(self.conn, "apify_run", cur.lastrowid, "started", {"mode": mode, "cap_usd": cap})
        self.report.runs += 1
        self._finish(self.conn.execute("SELECT * FROM apify_runs WHERE id = ?",
                                       (cur.lastrowid,)).fetchone())

    def _finish(self, row: sqlite3.Row) -> None:
        run = self.apify.wait_run(row["run_id"])
        items = self.apify.dataset_items(row["dataset_id"])
        meta = json.loads(row["meta"])
        ok = run["status"] == "SUCCEEDED"
        if not ok:
            self.report.errors.append(f"Запуск Apify {row['run_id']} ({row['mode']}): {run['status']}")

        with tx(self.conn):
            if row["mode"] == "refresh":
                new = self._store_refresh(items, meta["shortcodes"])
            elif row["mode"] == "links":
                new = self._store_links(items, meta["shortcodes"], meta.get("group", "default"), row["id"])
            else:
                new = self._store_items(items, meta["source_ids"], row["id"])
                self._scraped_sources.update(meta["source_ids"])
                if ok:
                    ids = meta["source_ids"]
                    marks = ",".join("?" * len(ids))
                    self.conn.execute(f"UPDATE sources SET last_scraped_at = ? WHERE id IN ({marks})",
                                      (row["started_at"], *ids))
                    if row["mode"] == "archive":
                        self.conn.execute(f"UPDATE sources SET archive_done = 1 WHERE id IN ({marks})",
                                          tuple(ids))
            self.conn.execute("UPDATE apify_runs SET status = ?, items_count = ?, new_count = ?, "
                              "finished_at = ? WHERE id = ?",
                              (run["status"], len(items), new, now(), row["id"]))
            log_event(self.conn, "apify_run", row["id"], "finished",
                      {"status": run["status"], "items": len(items), "new": new})
        self.report.items += len(items)

    # --- разбор результатов ----------------------------------------------------

    @staticmethod
    def _video_fields(item: dict) -> dict:
        return {"video_url": item.get("videoUrl"), "audio_url": item.get("audioUrl"),
                "downloaded_video_url": item.get("downloadedVideo")}

    def _store_items(self, items: list[dict], source_ids: list[int], run_row_id: int) -> int:
        marks = ",".join("?" * len(source_ids))
        by_name = {r["username"]: r["id"] for r in self.conn.execute(
            f"SELECT id, username FROM sources WHERE id IN ({marks})", tuple(source_ids))}
        new = 0
        for item in items:
            shortcode = item.get("shortCode")
            if item.get("error") or not shortcode or not (item.get("videoUrl") or item.get("downloadedVideo")):
                self.report.skipped += 1
                if item.get("error"):
                    log_event(self.conn, "apify_run", run_row_id, "item_error",
                              {k: item.get(k) for k in ("error", "errorDescription", "inputUrl", "url")})
                continue
            source_id = self._resolve_source(item, by_name)
            if source_id is None:
                self.report.skipped += 1
                log_event(self.conn, "apify_run", run_row_id, "unknown_owner",
                          {"shortcode": shortcode, "owner": item.get("ownerUsername")})
                continue
            existing = self.conn.execute("SELECT id, status FROM source_posts WHERE shortcode = ?",
                                         (shortcode,)).fetchone()
            if existing is None:
                self._insert_post(item, source_id)
                new += 1
            elif existing["status"] in (DISCOVERED, NEEDS_REFRESH):
                self._update_urls(existing["id"], item)  # свежие ссылки на ещё не скачанный ролик
        self.report.new += new
        return new

    def _store_links(self, items: list[dict], shortcodes: list[str], group: str, run_row_id: int) -> int:
        from app import sources as sources_mod
        wanted, found, new = set(shortcodes), set(), 0
        for item in items:
            sc = item.get("shortCode")
            if item.get("error") or not sc or not (item.get("videoUrl") or item.get("downloadedVideo")):
                self.report.skipped += 1
                if item.get("error"):
                    log_event(self.conn, "apify_run", run_row_id, "item_error",
                              {k: item.get(k) for k in ("error", "errorDescription", "inputUrl", "url")})
                continue
            found.add(sc)
            source_id = sources_mod.links_source(self.conn, item.get("ownerUsername") or "unknown", group)
            existing = self.conn.execute("SELECT id, status FROM source_posts WHERE shortcode = ?",
                                         (sc,)).fetchone()
            if existing is None:
                self._insert_post(item, source_id, priority=1)
                new += 1
            else:
                self.conn.execute("UPDATE source_posts SET priority = 1 WHERE id = ?", (existing["id"],))
                if existing["status"] in (DISCOVERED, NEEDS_REFRESH):
                    self._update_urls(existing["id"], item)
        self.report.not_found += len(wanted - found)
        self.report.new += new
        return new

    def _insert_post(self, item: dict, source_id: int, priority: int = 0) -> None:
        shortcode = item["shortCode"]
        self.conn.execute(
            "INSERT INTO source_posts (source_id, shortcode, external_id, url, caption, "
            "published_at, duration, width, height, video_url, audio_url, "
            "downloaded_video_url, status, priority, raw, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source_id, shortcode, str(item.get("id") or ""),
             item.get("url") or f"https://www.instagram.com/reel/{shortcode}/",
             item.get("caption"), item.get("timestamp"), item.get("videoDuration"),
             item.get("dimensionsWidth"), item.get("dimensionsHeight"),
             *self._video_fields(item).values(), DISCOVERED, priority,
             json.dumps(item, ensure_ascii=False), now(), now()))

    @staticmethod
    def _resolve_source(item: dict, by_name: dict[str, int]) -> int | None:
        owner = (item.get("ownerUsername") or "").lower()
        if owner in by_name:
            return by_name[owner]
        m = re.search(r"instagram\.com/([^/?#]+)", item.get("inputUrl") or "", re.I)
        if m and m.group(1).lower() in by_name:
            return by_name[m.group(1).lower()]
        # коллаб-рилс с чужим владельцем, найденный в профиле единственного аккаунта запуска
        return next(iter(by_name.values())) if len(by_name) == 1 else None

    def _update_urls(self, post_id: int, item: dict) -> None:
        f = self._video_fields(item)
        self.conn.execute("UPDATE source_posts SET video_url = ?, audio_url = ?, "
                          "downloaded_video_url = ?, status = ?, updated_at = ? WHERE id = ?",
                          (*f.values(), DISCOVERED, now(), post_id))

    def _store_refresh(self, items: list[dict], shortcodes: list[str]) -> int:
        fresh = {i["shortCode"]: i for i in items
                 if i.get("shortCode") and not i.get("error") and (i.get("videoUrl") or i.get("downloadedVideo"))}
        for sc in shortcodes:
            post = self.conn.execute("SELECT * FROM source_posts WHERE shortcode = ?", (sc,)).fetchone()
            if post is None or post["status"] != NEEDS_REFRESH:
                continue
            if sc in fresh:
                self._update_urls(post["id"], fresh[sc])
                self._refreshed_ids.add(post["id"])
                self.report.refreshed += 1
            else:
                self._fail_attempt(post, "рилс не найден при повторном запросе", expired=True)
        return 0

    # --- скачивание ------------------------------------------------------------

    def _download_pending(self, only: set[int] | None = None) -> None:
        posts = self.conn.execute(
            "SELECT p.*, s.username FROM source_posts p JOIN sources s ON s.id = p.source_id "
            "WHERE p.status = ? ORDER BY p.published_at", (DISCOVERED,)).fetchall()
        for post in posts:
            if only is None or post["id"] in only:
                self._download(post)

    def _download(self, post: sqlite3.Row) -> None:
        dst = self.cfg.source_dir / post["username"] / f"{post['shortcode']}.mp4"
        try:
            sha = self._fetch_video(post, dst)
            info = media.probe(dst)
            if not info["has_audio"] and post["audio_url"]:
                audio = dst.with_name(dst.stem + ".audio.m4a")
                try:
                    media.download(post["audio_url"], audio, self.http)
                    media.mux(dst, audio, dst)
                finally:
                    audio.unlink(missing_ok=True)
                info = media.probe(dst)
        except (media.DownloadError, media.MediaError) as e:
            dst.unlink(missing_ok=True)
            self._fail_attempt(post, str(e), expired=getattr(e, "expired", False))
            return

        dup = self.conn.execute("SELECT id FROM source_posts WHERE sha256 = ? AND id <> ? AND status = ?",
                                (sha, post["id"], DOWNLOADED)).fetchone()
        if dup:
            dst.unlink(missing_ok=True)
            self.conn.execute("UPDATE source_posts SET status = ?, sha256 = ?, error = ?, updated_at = ? "
                              "WHERE id = ?", (DUPLICATE, sha, f"тот же файл, что у поста {dup['id']}",
                                               now(), post["id"]))
            log_event(self.conn, "source_post", post["id"], "duplicate", {"of": dup["id"]})
            self.report.duplicates += 1
            return

        self.conn.execute(
            "UPDATE source_posts SET status = ?, video_path = ?, sha256 = ?, width = ?, height = ?, "
            "duration = ?, error = NULL, updated_at = ? WHERE id = ?",
            (DOWNLOADED, dst.relative_to(self.cfg.data_dir).as_posix(), sha, info["width"],
             info["height"], info["duration"], now(), post["id"]))
        log_event(self.conn, "source_post", post["id"], "downloaded")
        self.report.downloaded += 1

    def _fetch_video(self, post: sqlite3.Row, dst: Path) -> str:
        """Пробует ссылку Instagram, затем копию Apify (если включена)."""
        last: media.DownloadError | None = None
        expired = True
        for url in filter(None, (post["video_url"], post["downloaded_video_url"])):
            try:
                return media.download(url, dst, self.http)
            except media.DownloadError as e:
                last, expired = e, expired and e.expired
        raise media.DownloadError(str(last or "нет ссылки на видео"), expired=expired)

    def _fail_attempt(self, post: sqlite3.Row, error: str, expired: bool) -> None:
        attempts = post["attempts"] + 1
        if attempts >= self.cfg.download_max_attempts:
            status = FAILED
            self.report.failed += 1
            self.report.errors.append(f"{post['url']}: {error}")
        else:
            status = NEEDS_REFRESH if expired else DISCOVERED
        self.conn.execute("UPDATE source_posts SET status = ?, attempts = ?, error = ?, updated_at = ? "
                          "WHERE id = ?", (status, attempts, error, now(), post["id"]))
        log_event(self.conn, "source_post", post["id"], "download_failed",
                  {"error": error, "attempt": attempts, "status": status})

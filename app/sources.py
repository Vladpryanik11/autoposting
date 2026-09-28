"""Источники роликов.

kind = 'profile' — основной аккаунт студии: собираем архив и новые ролики каждый час.
kind = 'links'   — автор рилса, присланного ссылкой: сам аккаунт не собираем, только присланные рилсы.
"""
import re
import sqlite3

from app.db import log_event, now

USERNAME_RE = re.compile(r"^[a-z0-9._]{1,30}$")
NOT_PROFILES = {"p", "reel", "reels", "stories", "explore", "tv"}
REEL_LINK_RE = re.compile(r"(?:https?://)?(?:www\.|m\.)?instagram\.com/(?:[\w.]+/)?(?:reel|reels|p|tv)/"
                          r"([A-Za-z0-9_-]{5,40})", re.I)


def normalize_username(value: str) -> str:
    """'@Name', 'name', 'https://instagram.com/name/' → 'name'."""
    v = value.strip()
    m = re.search(r"instagram\.com/([^/?#]+)", v, re.I)
    if m:
        v = m.group(1)
    v = v.lstrip("@").lower()
    if v in NOT_PROFILES or not USERNAME_RE.match(v):
        raise ValueError(f"Не похоже на Instagram-аккаунт: {value!r}")
    return v


def extract_reel_links(text: str | None) -> list[tuple[str, str]]:
    """Все ссылки на рилсы в тексте → [(shortcode, канонический URL)] без повторов, в исходном порядке."""
    seen, out = set(), []
    for m in REEL_LINK_RE.finditer(text or ""):
        sc = m.group(1)
        if sc not in seen:
            seen.add(sc)
            out.append((sc, f"https://www.instagram.com/reel/{sc}/"))
    return out


def add(conn: sqlite3.Connection, value: str, group: str = "default") -> tuple[str, bool]:
    """Добавляет основной аккаунт. Возвращает (username, добавлен_ли_новый). Группу существующего не меняет.
    Если автор раньше встречался только в присланных ссылках — превращаем его в полноценный источник."""
    username = normalize_username(value)
    existing = conn.execute("SELECT id, kind FROM sources WHERE username = ?", (username,)).fetchone()
    if existing and existing["kind"] == "links":
        conn.execute("UPDATE sources SET kind = 'profile', enabled = 1, archive_done = 0 WHERE id = ?",
                     (existing["id"],))
        log_event(conn, "source", existing["id"], "promoted_from_links")
        return username, True
    cur = conn.execute(
        "INSERT INTO sources (username, route_group, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT (platform, username) DO NOTHING", (username, group, now()))
    if cur.rowcount:
        log_event(conn, "source", cur.lastrowid, "added", {"group": group})
    return username, bool(cur.rowcount)


def links_source(conn: sqlite3.Connection, username: str, group: str) -> int:
    """Источник для автора присланного рилса: существующий (любого вида) или новый выключенный."""
    username = username.lower()
    row = conn.execute("SELECT id FROM sources WHERE username = ?", (username,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO sources (username, route_group, enabled, archive_done, kind, created_at) "
                       "VALUES (?, ?, 0, 1, 'links', ?)", (username, group, now()))
    log_event(conn, "source", cur.lastrowid, "added_from_link", {"group": group})
    return cur.lastrowid


def group_of(conn: sqlite3.Connection, username: str) -> str | None:
    row = conn.execute("SELECT route_group FROM sources WHERE username = ?", (username,)).fetchone()
    return row["route_group"] if row else None


def set_enabled(conn: sqlite3.Connection, value: str, enabled: bool) -> bool:
    username = normalize_username(value)
    cur = conn.execute("UPDATE sources SET enabled = ? WHERE username = ? AND kind = 'profile'",
                       (int(enabled), username))
    return bool(cur.rowcount)


def list_with_stats(conn: sqlite3.Connection, kind: str | None = "profile") -> list[sqlite3.Row]:
    where = "WHERE s.kind = ?" if kind else ""
    return conn.execute(f"""
        SELECT s.*,
               COUNT(p.id) AS posts,
               SUM(p.status = 'DOWNLOADED') AS downloaded,
               SUM(p.status IN ('FAILED')) AS failed
        FROM sources s LEFT JOIN source_posts p ON p.source_id = s.id
        {where}
        GROUP BY s.id ORDER BY s.route_group, s.username
    """, (kind,) if kind else ()).fetchall()

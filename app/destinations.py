"""Аккаунты-сателлиты (куда публикуем) — импорт из Postmypost и настройка расписания."""
import json
import re
import sqlite3
from datetime import date, timedelta

from app.db import log_event, now

SLOT_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def platform_of(channel: dict | None) -> str:
    text = " ".join(str(channel.get(k, "")) for k in ("code", "name")).lower() if channel else ""
    for platform, marks in (("instagram", ("instagram",)), ("tiktok", ("tiktok",)),
                            ("youtube", ("youtube",))):
        if any(m in text for m in marks):
            return platform
    return "other"


def parse_slots(values: list[str]) -> list[str]:
    slots = []
    for v in values:
        m = SLOT_RE.match(v.strip())
        if not m:
            raise ValueError(f"Неверное время слота: {v!r} (нужно ЧЧ:ММ)")
        slots.append(f"{int(m.group(1)):02d}:{m.group(2)}")
    if not slots:
        raise ValueError("Нужен хотя бы один слот")
    return sorted(set(slots))


def parse_date(value: str, today: date | None = None) -> str:
    """'2026-10-01', '01.10', '01.10.2026', 'сегодня', 'завтра', 'послезавтра' → 'ГГГГ-ММ-ДД'."""
    today = today or date.today()
    v = value.strip().lower()
    words = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
    if v in words:
        return (today + timedelta(days=words[v])).isoformat()
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", v)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        year = (2000 + int(y) if len(y) == 2 else int(y)) if y else today.year
        try:
            result = date(year, mo, d)
        except ValueError:
            result = None
        if result:
            if not y and result < today:  # «01.03» в октябре — это следующий год
                result = date(year + 1, mo, d)
            return result.isoformat()
    raise ValueError(f"Не понял дату «{value}». Напиши, например, 01.10 или «завтра».")


def groups(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT route_group FROM sources UNION SELECT route_group FROM destinations ORDER BY 1")]


def sync_from_postmypost(conn: sqlite3.Connection, api, project_id: int,
                         default_slots: tuple[str, ...]) -> tuple[int, int, list[str]]:
    """Подтягивает аккаунты проекта. Новые — включены, но без даты старта (не планируются)."""
    channels = {c["id"]: c for c in api.channels()}
    added = updated = 0
    warnings = []
    for acc in api.accounts(project_id):
        platform = platform_of(channels.get(acc.get("chanel_id") or acc.get("channel_id")))
        name = acc.get("name") or acc.get("login") or str(acc["id"])
        if acc.get("connection_status") == 2:
            warnings.append(f"{name}: в Postmypost нужна повторная авторизация")
        if conn.execute("SELECT 1 FROM destinations WHERE pmp_account_id = ?", (acc["id"],)).fetchone():
            conn.execute("UPDATE destinations SET platform = ?, name = ? WHERE pmp_account_id = ?",
                         (platform, name, acc["id"]))
            updated += 1
        else:
            conn.execute("INSERT INTO destinations (pmp_account_id, platform, name, slots, created_at) "
                         "VALUES (?, ?, ?, ?, ?)",
                         (acc["id"], platform, name, json.dumps(list(default_slots)), now()))
            added += 1
    log_event(conn, "destinations", None, "synced", {"added": added, "updated": updated})
    return added, updated, warnings


def all_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM destinations ORDER BY route_group, platform, name").fetchall()


def resolve(conn: sqlite3.Connection, ref: str) -> list[int]:
    """'all' → все; '3' → наш id; иначе — по имени аккаунта."""
    ref = ref.strip()
    if ref.lower() == "all":
        return [r["id"] for r in conn.execute("SELECT id FROM destinations")]
    if ref.isdigit():
        row = conn.execute("SELECT id FROM destinations WHERE id = ? OR pmp_account_id = ?",
                           (int(ref), int(ref))).fetchone()
    else:
        row = conn.execute("SELECT id FROM destinations WHERE lower(name) = lower(?)",
                           (ref.lstrip("@"),)).fetchone()
    if not row:
        raise ValueError(f"Аккаунт не найден: {ref!r}. Список: /accounts")
    return [row["id"]]


def update(conn: sqlite3.Connection, ids: list[int], **fields) -> None:
    allowed = {"route_group", "enabled", "slots", "start_date"}
    assert set(fields) <= allowed, fields
    if "slots" in fields:
        fields["slots"] = json.dumps(fields["slots"])
    sets = ", ".join(f"{k} = ?" for k in fields)
    for i in ids:
        conn.execute(f"UPDATE destinations SET {sets} WHERE id = ?", (*fields.values(), i))
        log_event(conn, "destination", i, "updated", {k: v for k, v in fields.items()})

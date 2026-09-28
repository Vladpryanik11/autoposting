"""Планировщик: заполняет слоты аккаунтов роликами на plan_days вперёд.

Правила (DECISIONS.md, п. 7):
- у аккаунта свои слоты (местное время) + случайный сдвиг ±jitter_min;
- аккаунт без даты старта не планируется, до даты старта — тоже;
- один ролик в один аккаунт — один раз;
- один ролик в разных аккаунтах — не ближе min_gap_same_video_min;
- два ролика одного источника подряд в один аккаунт — только если нет альтернативы;
- сначала присланные ссылками, потом самые свежие ролики.
"""
import json
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import Settings
from app.db import ACTIVE, DOWNLOADED, PLANNED, RELEASED, log_event, now

# Длительность ролика (сек), которую принимает площадка
DURATION_LIMITS = {"youtube": (1.0, 180.0), "instagram": (3.0, 900.0), "tiktok": (3.0, 600.0)}
DEFAULT_DURATION = (1.0, 900.0)
MIN_LEAD = timedelta(minutes=30)  # слот ближе этого не планируем — не успеем подготовить

_ACTIVE_SQL = ",".join(f"'{s}'" for s in ACTIVE)


@dataclass
class PlanReport:
    created: int = 0
    empty_slots: dict[str, int] = field(default_factory=dict)  # аккаунт → слотов без ролика


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def slot_times(dest: sqlite3.Row, cfg: Settings, now_utc: datetime) -> list[datetime]:
    """Номинальные слоты аккаунта в окне [now + MIN_LEAD, now + plan_days] (UTC).
    Без даты старта аккаунт публикует сразу; с датой в будущем — начиная с неё."""
    tz = ZoneInfo(cfg.timezone)
    horizon = now_utc + timedelta(days=cfg.plan_days)
    day = now_utc.astimezone(tz).date()
    if dest["start_date"]:
        day = max(day, date.fromisoformat(dest["start_date"]))
    out = []
    while day <= horizon.astimezone(tz).date():
        for s in json.loads(dest["slots"]):
            hh, mm = map(int, s.split(":"))
            dt = datetime.combine(day, time(hh, mm), tz).astimezone(timezone.utc)
            if now_utc + MIN_LEAD <= dt <= horizon:
                out.append(dt)
        day += timedelta(days=1)
    return out


def available_posts(conn: sqlite3.Connection, dest: sqlite3.Row) -> list[sqlite3.Row]:
    """Скачанные ролики группы аккаунта, которые ещё не ставились в этот аккаунт, свежие первыми."""
    lo, hi = DURATION_LIMITS.get(dest["platform"], DEFAULT_DURATION)
    return conn.execute("""
        SELECT p.id, p.source_id, p.priority FROM source_posts p JOIN sources s ON s.id = p.source_id
        WHERE p.status = ? AND s.route_group = ?
          AND (p.duration IS NULL OR p.duration BETWEEN ? AND ?)
          AND NOT EXISTS (SELECT 1 FROM publications x WHERE x.source_post_id = p.id
                          AND x.destination_id = ? AND x.status <> ?)
        -- присланные ссылками — первыми и в порядке присылки; остальные — свежие первыми
        ORDER BY p.priority DESC, CASE WHEN p.priority > 0 THEN p.id END ASC,
                 p.published_at DESC, p.id DESC""",
                        (DOWNLOADED, dest["route_group"], lo, hi, dest["id"], RELEASED)).fetchall()


def _previous_source(conn: sqlite3.Connection, dest_id: int, slot_at: datetime) -> int | None:
    row = conn.execute(f"""
        SELECT p.source_id FROM publications x JOIN source_posts p ON p.id = x.source_post_id
        WHERE x.destination_id = ? AND x.status IN ({_ACTIVE_SQL}) AND x.slot_at < ?
        ORDER BY x.slot_at DESC LIMIT 1""", (dest_id, to_iso(slot_at))).fetchone()
    return row["source_id"] if row else None


def choose_post(conn: sqlite3.Connection, cfg: Settings, dest: sqlite3.Row,
                slot_at: datetime) -> sqlite3.Row | None:
    gap = timedelta(minutes=cfg.min_gap_same_video_min)
    prev_source = _previous_source(conn, dest["id"], slot_at)
    fallback = None
    for post in available_posts(conn, dest):
        if fallback is not None and post["priority"] < fallback["priority"]:
            break  # чередование источников не важнее приоритета: ролик по ссылке не уступает обычному
        others = conn.execute(f"SELECT slot_at FROM publications WHERE source_post_id = ? "
                              f"AND status IN ({_ACTIVE_SQL})", (post["id"],)).fetchall()
        if any(abs(from_iso(o["slot_at"]) - slot_at) < gap for o in others):
            continue
        if post["source_id"] != prev_source:
            return post
        fallback = fallback or post
    return fallback


def plan(conn: sqlite3.Connection, cfg: Settings, now_utc: datetime,
         rng: random.Random | None = None) -> PlanReport:
    rng = rng or random.Random()
    rep = PlanReport()
    dests = conn.execute("SELECT * FROM destinations WHERE enabled = 1 "
                         "ORDER BY id").fetchall()
    # все слоты всех аккаунтов по времени: свежие ролики уходят в ближайшие слоты везде
    slots = sorted((nominal, d["id"], d) for d in dests for nominal in slot_times(d, cfg, now_utc))
    for nominal, _, dest in slots:
        busy = conn.execute(f"SELECT 1 FROM publications WHERE destination_id = ? AND slot_nominal = ? "
                            f"AND status IN ({_ACTIVE_SQL})", (dest["id"], to_iso(nominal))).fetchone()
        if busy:
            continue
        shift = timedelta(seconds=rng.randint(-cfg.jitter_min * 60, cfg.jitter_min * 60))
        slot_at = max(nominal + shift, now_utc + MIN_LEAD / 2)
        post = choose_post(conn, cfg, dest, slot_at)
        if post is None:
            rep.empty_slots[dest["name"]] = rep.empty_slots.get(dest["name"], 0) + 1
            continue
        cur = conn.execute(
            "INSERT INTO publications (source_post_id, destination_id, slot_nominal, slot_at, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (post["id"], dest["id"], to_iso(nominal), to_iso(slot_at), PLANNED, now(), now()))
        log_event(conn, "publication", cur.lastrowid, "planned",
                  {"post": post["id"], "destination": dest["id"], "slot_at": to_iso(slot_at)})
        rep.created += 1
    return rep


def days_of_content(conn: sqlite3.Connection, dest: sqlite3.Row) -> float:
    """На сколько дней хватит ещё не запланированных роликов при текущих слотах."""
    per_day = len(json.loads(dest["slots"])) or 1
    return len(available_posts(conn, dest)) / per_day

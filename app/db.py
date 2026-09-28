"""SQLite: схема, миграции, журнал событий.

Схема версионируется через PRAGMA user_version: каждая новая версия —
новый элемент MIGRATIONS, старые не меняются.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS = [
    # v1 — сбор роликов с основных аккаунтов
    """
    CREATE TABLE sources (
        id              INTEGER PRIMARY KEY,
        platform        TEXT NOT NULL DEFAULT 'instagram',
        username        TEXT NOT NULL,
        route_group     TEXT NOT NULL DEFAULT 'default',
        enabled         INTEGER NOT NULL DEFAULT 1,
        archive_done    INTEGER NOT NULL DEFAULT 0,
        last_scraped_at TEXT,
        created_at      TEXT NOT NULL,
        UNIQUE (platform, username)
    );

    CREATE TABLE apify_runs (
        id          INTEGER PRIMARY KEY,
        run_id      TEXT NOT NULL UNIQUE,
        dataset_id  TEXT,
        mode        TEXT NOT NULL,            -- archive | incremental | refresh
        meta        TEXT NOT NULL DEFAULT '{}',
        status      TEXT NOT NULL,            -- статус Apify: RUNNING, SUCCEEDED, FAILED...
        items_count INTEGER,
        new_count   INTEGER,
        started_at  TEXT NOT NULL,
        finished_at TEXT
    );

    CREATE TABLE source_posts (
        id                   INTEGER PRIMARY KEY,
        source_id            INTEGER NOT NULL REFERENCES sources(id),
        shortcode            TEXT NOT NULL UNIQUE,
        external_id          TEXT,
        url                  TEXT NOT NULL,
        caption              TEXT,
        published_at         TEXT,
        duration             REAL,
        width                INTEGER,
        height               INTEGER,
        video_url            TEXT,
        audio_url            TEXT,
        downloaded_video_url TEXT,
        video_path           TEXT,
        sha256               TEXT,
        status               TEXT NOT NULL,
        attempts             INTEGER NOT NULL DEFAULT 0,
        error                TEXT,
        raw                  TEXT,
        created_at           TEXT NOT NULL,
        updated_at           TEXT NOT NULL
    );
    CREATE INDEX source_posts_status ON source_posts(status);
    CREATE INDEX source_posts_sha ON source_posts(sha256);

    CREATE TABLE events (
        id         INTEGER PRIMARY KEY,
        entity     TEXT NOT NULL,
        entity_id  INTEGER,
        event      TEXT NOT NULL,
        detail     TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX events_entity ON events(entity, entity_id);
    """,
    # v2 — аккаунты-сателлиты, публикации, настройки, связь с ботом
    """
    CREATE TABLE destinations (
        id             INTEGER PRIMARY KEY,
        pmp_account_id INTEGER NOT NULL UNIQUE,
        platform       TEXT NOT NULL,            -- instagram | tiktok | youtube | other
        name           TEXT NOT NULL,
        route_group    TEXT NOT NULL DEFAULT 'default',
        enabled        INTEGER NOT NULL DEFAULT 1,
        slots          TEXT NOT NULL,            -- JSON ["10:00","15:00","20:00"], местное время
        start_date     TEXT,                     -- YYYY-MM-DD; пока пусто — аккаунт не планируется
        created_at     TEXT NOT NULL
    );

    CREATE TABLE publications (
        id                 INTEGER PRIMARY KEY,
        source_post_id     INTEGER NOT NULL REFERENCES source_posts(id),
        destination_id     INTEGER NOT NULL REFERENCES destinations(id),
        slot_nominal       TEXT NOT NULL,        -- слот по расписанию (UTC)
        slot_at            TEXT NOT NULL,        -- фактическое время со случайным сдвигом (UTC)
        status             TEXT NOT NULL,
        caption            TEXT,
        title              TEXT,
        review_reason      TEXT,
        variant_path       TEXT,
        similarity         REAL,
        intensity          TEXT,
        pmp_file_id        INTEGER,
        pmp_publication_id INTEGER,
        is_draft           INTEGER NOT NULL DEFAULT 0,
        attempts           INTEGER NOT NULL DEFAULT 0,
        error              TEXT,
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL
    );
    -- один ролик в один аккаунт — один раз (RELEASED = слот упущен, ролик свободен)
    CREATE UNIQUE INDEX publications_post_dest ON publications(source_post_id, destination_id)
        WHERE status <> 'RELEASED';
    CREATE INDEX publications_status ON publications(status, slot_at);
    CREATE INDEX publications_dest_slot ON publications(destination_id, slot_nominal);

    CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);

    CREATE TABLE outbox (                        -- сообщения, которые бот должен отправить
        id             INTEGER PRIMARY KEY,
        kind           TEXT NOT NULL,            -- error | review | info | summary
        text           TEXT NOT NULL,
        publication_id INTEGER,
        created_at     TEXT NOT NULL,
        sent_at        TEXT
    );

    CREATE TABLE commands (                      -- поручения от бота рабочему процессу
        id         INTEGER PRIMARY KEY,
        kind       TEXT NOT NULL,                -- fetch | unschedule | undraft | cancel | links
        arg        TEXT,
        created_at TEXT NOT NULL,
        done_at    TEXT,
        result     TEXT
    );
    """,
    # v3 — рилсы по ссылкам: авторы таких рилсов не собираются целиком, сами рилсы идут первыми
    """
    ALTER TABLE sources ADD COLUMN kind TEXT NOT NULL DEFAULT 'profile';   -- profile | links
    ALTER TABLE source_posts ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;  -- 1 — прислан ссылкой
    """,
    # v4 — история: ссылка на вышедший пост (что отдаёт Postmypost) и когда вышел
    """
    ALTER TABLE publications ADD COLUMN post_url TEXT;
    ALTER TABLE publications ADD COLUMN external_id TEXT;
    ALTER TABLE publications ADD COLUMN published_at TEXT;
    ALTER TABLE publications ADD COLUMN link_checks INTEGER NOT NULL DEFAULT 0;
    """,
]

# Статусы source_posts
DISCOVERED = "DISCOVERED"          # найден, ждёт скачивания
NEEDS_REFRESH = "NEEDS_REFRESH"    # ссылка на видео протухла — перезапросить у Apify
DOWNLOADED = "DOWNLOADED"          # исходник на диске
DUPLICATE = "DUPLICATE"            # тот же файл уже есть под другим shortcode
FAILED = "FAILED"                  # скачать не удалось за download_max_attempts попыток

# Статусы publications
PLANNED = "PLANNED"      # слот назначен, ждёт подписи и уникализации
REVIEW = "REVIEW"        # ждёт одобрения в боте
READY = "READY"          # копия и подпись готовы, ждёт отправки в Postmypost
SCHEDULED = "SCHEDULED"  # запланирован в Postmypost
DRAFTED = "DRAFTED"      # создан в Postmypost как черновик (режим черновиков)
PUBLISHED = "PUBLISHED"  # вышел
REJECTED = "REJECTED"    # отклонён в боте — в этот аккаунт больше не планируется
RELEASED = "RELEASED"    # слот упущен или снят — ролик снова доступен для этого аккаунта
# FAILED (общий с source_posts) — не удалось подготовить или опубликовать
ACTIVE = (PLANNED, REVIEW, READY, SCHEDULED, DRAFTED, PUBLISHED)  # занимают слот

# Как статусы публикаций называются в боте и уведомлениях
STATUS_LABELS = {
    PLANNED: "🕓 готовится", REVIEW: "🔎 ждёт проверки", READY: "📦 готова к отправке",
    SCHEDULED: "✅ поставлена", DRAFTED: "📝 черновик", PUBLISHED: "🟢 вышла",
    FAILED: "❌ ошибка", REJECTED: "🚫 снята", RELEASED: "↩️ снята, ролик вернулся в очередь",
}
TZ_NAMES = {"Europe/Moscow": "по Москве", "Europe/Kaliningrad": "по Калининграду",
            "Asia/Yekaterinburg": "по Екатеринбургу", "Asia/Novosibirsk": "по Новосибирску",
            "Europe/Minsk": "по Минску", "Asia/Almaty": "по Алматы", "Europe/Kiev": "по Киеву"}


def group_label(group: str) -> str:
    return "основная" if group == "default" else group


def tz_label(tz: str) -> str:
    return TZ_NAMES.get(tz, tz)

# Настройки, которые меняются из бота (таблица kv)
KV_DEFAULTS = {
    "paused": "0",         # 1 — не планировать и не отправлять новое
    "draft_mode": "0",     # 1 — создавать в Postmypost только черновики (для проверки; только из CLI)
    "review_mode": "auto",  # auto — только сомнительные; cta — всё с кодовым словом; all — всё
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # транзакции — явно через tx()
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    migrate(conn)
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    """Транзакция: всё внутри применяется целиком или не применяется вовсе."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for n, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {n};\nCOMMIT;")


def get_kv(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else KV_DEFAULTS.get(key, "")


def set_kv(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))


def notify(conn: sqlite3.Connection, kind: str, text: str, publication_id: int | None = None) -> None:
    """Сообщение в очередь для Telegram-бота."""
    conn.execute("INSERT INTO outbox (kind, text, publication_id, created_at) VALUES (?, ?, ?, ?)",
                 (kind, text, publication_id, now()))


def add_command(conn: sqlite3.Connection, kind: str, arg: str | None = None) -> int:
    return conn.execute("INSERT INTO commands (kind, arg, created_at) VALUES (?, ?, ?)",
                        (kind, arg, now())).lastrowid


def log_event(conn: sqlite3.Connection, entity: str, entity_id: int | None,
              event: str, detail: dict | str | None = None) -> None:
    if isinstance(detail, dict):
        detail = json.dumps(detail, ensure_ascii=False)
    conn.execute("INSERT INTO events (entity, entity_id, event, detail, created_at) "
                 "VALUES (?, ?, ?, ?, ?)", (entity, entity_id, event, detail, now()))

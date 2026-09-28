"""Настройки из .env."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    # Apify — сбор роликов
    apify_token: str = ""
    apify_actor: str = "apify~instagram-reel-scraper"
    apify_archive_limit: int = 1000      # роликов с профиля при первом (архивном) сборе
    apify_incremental_limit: int = 30    # роликов с профиля при обычной проверке
    apify_downloaded_video: bool = False  # платная копия видео у Apify ($0.02 за МБ)
    download_max_attempts: int = 3
    # Postmypost — публикация
    postmypost_token: str = ""
    postmypost_project_id: int | None = None
    # Подписи — Claude
    anthropic_api_key: str = ""
    caption_model: str = "claude-haiku-4-5"
    # Telegram-бот
    telegram_bot_token: str = ""
    telegram_admin_ids: tuple[int, ...] = ()
    # Расписание
    timezone: str = "Europe/Moscow"
    default_slots: tuple[str, ...] = ("10:00", "15:00", "20:00")
    plan_days: int = 2                   # на сколько дней вперёд планировать и отправлять в Postmypost
    jitter_min: int = 15                 # случайный сдвиг времени публикации ±N минут
    min_gap_same_video_min: int = 120    # один ролик в разных аккаунтах — не ближе N минут
    # Уникализация
    uniq_intensity: str = "medium"
    uniq_max_similarity: float = 0.9
    uniq_encoder: str = ""               # например h264_nvenc; пусто — автовыбор
    # Рабочий процесс
    ingest_every_min: int = 60
    sync_every_min: int = 15
    summary_time: str = "21:00"          # ежедневная сводка в боте (местное время)
    queue_low_days: int = 3              # предупреждать, если роликов осталось меньше чем на N дней
    max_attempts: int = 3                # попыток подготовить/опубликовать публикацию

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def source_dir(self) -> Path:
        return self.data_dir / "media" / "source"

    @property
    def variant_dir(self) -> Path:
        return self.data_dir / "media" / "variants"


def _bool(v: str) -> bool:
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _list(v: str) -> list[str]:
    return [x.strip() for x in v.replace(";", ",").split(",") if x.strip()]


def load() -> Settings:
    load_dotenv(ROOT / ".env")
    e = lambda key, default="": (os.environ.get(key) or default).strip()  # noqa: E731
    project = e("POSTMYPOST_PROJECT_ID")
    return Settings(
        data_dir=Path(e("DATA_DIR") or ROOT / "data"),
        apify_token=e("APIFY_TOKEN"),
        apify_actor=e("APIFY_ACTOR", "apify~instagram-reel-scraper"),
        apify_archive_limit=int(e("APIFY_ARCHIVE_LIMIT", "1000")),
        apify_incremental_limit=int(e("APIFY_INCREMENTAL_LIMIT", "30")),
        apify_downloaded_video=_bool(e("APIFY_DOWNLOADED_VIDEO", "0")),
        download_max_attempts=int(e("DOWNLOAD_MAX_ATTEMPTS", "3")),
        postmypost_token=e("POSTMYPOST_TOKEN"),
        postmypost_project_id=int(project) if project else None,
        anthropic_api_key=e("ANTHROPIC_API_KEY"),
        caption_model=e("CAPTION_MODEL", "claude-haiku-4-5"),
        telegram_bot_token=e("TELEGRAM_BOT_TOKEN"),
        telegram_admin_ids=tuple(int(x) for x in _list(e("TELEGRAM_ADMIN_IDS"))),
        timezone=e("TIMEZONE", "Europe/Moscow"),
        default_slots=tuple(_list(e("DEFAULT_SLOTS", "10:00,15:00,20:00"))),
        plan_days=int(e("PLAN_DAYS", "2")),
        jitter_min=int(e("JITTER_MIN", "15")),
        min_gap_same_video_min=int(e("MIN_GAP_SAME_VIDEO_MIN", "120")),
        uniq_intensity=e("UNIQ_INTENSITY", "medium"),
        uniq_max_similarity=float(e("UNIQ_MAX_SIMILARITY", "0.9")),
        uniq_encoder=e("UNIQ_ENCODER"),
        ingest_every_min=int(e("INGEST_EVERY_MIN", "60")),
        sync_every_min=int(e("SYNC_EVERY_MIN", "15")),
        summary_time=e("SUMMARY_TIME", "21:00"),
        queue_low_days=int(e("QUEUE_LOW_DAYS", "3")),
    )

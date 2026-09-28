"""Сквозной тест: настоящий yt_uniquifier + ffmpeg, путь с кириллицей, рабочий процесс целиком.
Подделан только Postmypost. Медленный (~1 мин): пропустить — pytest -m "not slow"."""
import dataclasses
import importlib.util
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import db
from app.captions import Captioner
from app.media import probe
from app.worker import Worker

from helpers import FakePmp, add_dest, add_source, pubs

pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(importlib.util.find_spec("yt_uniquifier") is None, reason="нет yt_uniquifier")]


class KeepingPmp(FakePmp):
    """Сохраняет загруженные файлы, чтобы проверить итоговое видео."""

    def __init__(self, keep_dir: Path):
        super().__init__()
        self.keep_dir = keep_dir

    def upload(self, project_id, path):
        self.keep_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(path, self.keep_dir / Path(path).name)
        return super().upload(project_id, path)


def test_real_uniqualization_through_worker(conn, cfg, tmp_path):
    cfg = dataclasses.replace(cfg, postmypost_project_id=777, data_dir=tmp_path / "данные")
    conn = db.connect(cfg.db_path)
    src = cfg.source_dir / "studio" / "REAL1.mp4"
    src.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=720x1280:rate=30:duration=3", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(src)], check=True)
    s = add_source(conn)
    conn.execute("INSERT INTO source_posts (source_id, shortcode, url, caption, published_at, duration, "
                 "video_path, status, created_at, updated_at) VALUES (?, 'REAL1', 'https://instagram.com/reel/REAL1/', "
                 "'Напиши «ПРОМТ» в комментарии — пришлю гайд. Автор @studio', '2026-09-20T10:00:00+00:00', 3.0, ?, ?, ?, ?)",
                 (s, src.relative_to(cfg.data_dir).as_posix(), db.DOWNLOADED, db.now(), db.now()))
    now = datetime.now(timezone.utc)
    slot = (now + timedelta(hours=2)).astimezone(ZoneInfo(cfg.timezone)).strftime("%H:%M")
    add_dest(conn, 11, start=now.astimezone(ZoneInfo(cfg.timezone)).date().isoformat(), slots=(slot,))
    db.set_kv(conn, "draft_mode", "0")

    pmp = KeepingPmp(tmp_path / "uploaded")
    Worker(cfg, apify=False, pmp=pmp, captioner=Captioner(), clock=lambda: now).tick()

    pub = pubs(conn)[0]
    assert pub["status"] == db.SCHEDULED, pub["error"]
    assert pub["similarity"] is not None and pub["intensity"] in ("medium", "strong")
    assert "«ПРОМТ»" in pub["caption"] and "@studio" not in pub["caption"]
    out = tmp_path / "uploaded" / f"{pub['id']}.mp4"
    info = probe(out)
    assert (info["width"], info["height"]) == (1080, 1920) and info["has_audio"]
    assert out.read_bytes() != src.read_bytes()
    assert not list(cfg.variant_dir.glob("*"))  # после отправки локальная копия удалена
    conn.close()

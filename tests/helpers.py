"""Общие помощники для тестов планировщика, конвейера и рабочего процесса."""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from app import db
from app.postmypost import PostmypostError

T0 = datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc)  # 09:00 по Москве


class Clock:
    def __init__(self, t: datetime = T0):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def add_source(conn, username="studio", group="default") -> int:
    return conn.execute("INSERT INTO sources (username, route_group, archive_done, created_at) "
                        "VALUES (?, ?, 1, ?)", (username, group, db.now())).lastrowid


def add_post(conn, cfg, source_id: int, shortcode: str, published: str = "2026-09-20T10:00:00+00:00",
             duration: float = 20.0, caption: str = "Подпись ролика #reels") -> int:
    path = cfg.source_dir / "s" / f"{shortcode}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video " + shortcode.encode())
    return conn.execute(
        "INSERT INTO source_posts (source_id, shortcode, url, caption, published_at, duration, video_path, "
        "status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source_id, shortcode, f"https://www.instagram.com/reel/{shortcode}/", caption, published, duration,
         path.relative_to(cfg.data_dir).as_posix(), db.DOWNLOADED, db.now(), db.now())).lastrowid


def add_dest(conn, pmp_id: int, platform="instagram", group="default", start="2026-10-01",
             slots=("10:00", "15:00", "20:00"), name=None) -> int:
    return conn.execute(
        "INSERT INTO destinations (pmp_account_id, platform, name, route_group, slots, start_date, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pmp_id, platform, name or f"acc{pmp_id}", group, json.dumps(list(slots)), start, db.now())).lastrowid


def fake_render(src: Path, dst: Path) -> dict:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dst)
    return {"similarity": 0.85, "intensity": "medium"}


class FakePmp:
    def __init__(self):
        self.uploads, self.published, self.deleted = [], [], []
        self.statuses: dict[int, int] = {}
        self.fail_publish = 0
        self.found = None
        self.find_calls = 0

    def upload(self, project_id, path):
        self.uploads.append(Path(path).name)
        return 100 + len(self.uploads)

    def publish(self, project_id, account_ids, file_ids, **kw):
        if self.fail_publish:
            self.fail_publish -= 1
            raise PostmypostError("POST /publications: не отвечает (500)")
        self.published.append({"accounts": account_ids, "files": file_ids, **kw})
        return {"id": 500 + len(self.published)}

    def find_publication(self, project_id, post_at, account_id):
        self.find_calls += 1
        return self.found

    def publication(self, pid):
        return {"id": pid, "publication_status": self.statuses.get(pid, 5)}

    def delete_publication(self, pid, account_ids, delete_option=3):
        assert account_ids and all(isinstance(a, int) for a in account_ids)
        self.deleted.append(pid)

    # для /sync_accounts
    def channels(self):
        return [{"id": 1, "code": "instagram", "name": "Instagram"}, {"id": 2, "code": "tiktok", "name": "TikTok"},
                {"id": 3, "code": "youtube", "name": "YouTube"}]

    def accounts(self, project_id):
        return [{"id": 11, "chanel_id": 1, "name": "sat_insta", "connection_status": 1},
                {"id": 12, "chanel_id": 2, "name": "sat_tt", "connection_status": 2},
                {"id": 13, "chanel_id": 3, "name": "sat_yt", "connection_status": 1}]


def pubs(conn, dest_id=None):
    q = ("SELECT x.*, p.shortcode, p.source_id FROM publications x JOIN source_posts p ON p.id = x.source_post_id "
         + ("WHERE x.destination_id = ? " if dest_id else "") + "ORDER BY x.slot_at")
    return conn.execute(q, (dest_id,) if dest_id else ()).fetchall()

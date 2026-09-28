import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import db
from app.config import Settings


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


@pytest.fixture(scope="session")
def clips(tmp_path_factory) -> dict[str, bytes]:
    """Маленькие настоящие ролики: со звуком, другой со звуком, без звука, отдельный звук."""
    d = tmp_path_factory.mktemp("clips")
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=180x320:rate=15:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / "a.mp4"))
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=180x320:rate=15:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=660:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / "b.mp4"))
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=180x320:rate=15:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(d / "mute.mp4"))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=880:duration=1", "-c:a", "aac", str(d / "audio.m4a"))
    return {p.stem: p.read_bytes() for p in d.iterdir()}


class CDN:
    """Локальный «Instagram CDN»: путь → байты файла или HTTP-код ошибки."""

    def __init__(self):
        self.routes: dict[str, bytes | int] = {}
        self.hits: list[str] = []
        cdn = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cdn.hits.append(self.path)
                body = cdn.routes.get(self.path, 404)
                if isinstance(body, int):
                    self.send_response(body)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.server_port}{path}"


@pytest.fixture
def cdn():
    c = CDN()
    yield c
    c.server.shutdown()


class FakeApify:
    """Подмена Apify: каждый запуск получает ответ от функции respond(input) → items."""

    def __init__(self, respond):
        self.respond = respond
        self.starts: list[dict] = []
        self.status = "SUCCEEDED"
        self._items: dict[str, list[dict]] = {}

    def start_run(self, actor, run_input, max_total_charge_usd):
        n = len(self.starts) + 1
        self.starts.append({"input": run_input, "cap": max_total_charge_usd})
        self._items[f"ds{n}"] = self.respond(run_input)
        return {"id": f"run{n}", "defaultDatasetId": f"ds{n}", "status": "RUNNING"}

    def wait_run(self, run_id):
        return {"id": run_id, "status": self.status}

    def dataset_items(self, dataset_id):
        return self._items.get(dataset_id, [])


@pytest.fixture
def cfg(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data", apify_token="test", download_max_attempts=3)


@pytest.fixture
def conn(cfg):
    c = db.connect(cfg.db_path)
    yield c
    c.close()


def reel(shortcode: str, owner: str, video_url: str | None, **extra) -> dict:
    return {"id": f"id_{shortcode}", "shortCode": shortcode, "ownerUsername": owner,
            "url": f"https://www.instagram.com/reel/{shortcode}/", "type": "Video",
            "caption": f"подпись {shortcode}", "timestamp": "2026-09-20T10:00:00.000Z",
            "videoUrl": video_url, **extra}

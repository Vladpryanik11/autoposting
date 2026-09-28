"""Скачивание, склейка дорожек и проверка видеофайлов."""
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import requests

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")


class DownloadError(RuntimeError):
    def __init__(self, message: str, expired: bool = False):
        super().__init__(message)
        self.expired = expired  # ссылка протухла — нужна свежая от Apify


class MediaError(RuntimeError):
    pass


def download(url: str, dst: Path, session: requests.Session | None = None) -> str:
    """Скачивает файл атомарно (через .part), возвращает sha256 содержимого."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    h = hashlib.sha256()
    try:
        with (session or requests).get(url, stream=True, timeout=(15, 120),
                                       headers={"User-Agent": USER_AGENT}) as r:
            if r.status_code in (403, 404, 410):
                raise DownloadError(f"HTTP {r.status_code}", expired=True)
            if r.status_code >= 400:
                raise DownloadError(f"HTTP {r.status_code}")
            with open(part, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
                    h.update(chunk)
    except requests.RequestException as e:
        part.unlink(missing_ok=True)
        raise DownloadError(str(e)) from e
    except DownloadError:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, dst)
    return h.hexdigest()


def probe(path: Path) -> dict:
    r = subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json",
                        str(path)], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise MediaError(f"ffprobe: {r.stderr.strip()[:300]}")
    info = json.loads(r.stdout)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise MediaError("в файле нет видеодорожки")
    return {
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "duration": float(info.get("format", {}).get("duration") or 0),
        "has_audio": any(s.get("codec_type") == "audio" for s in info["streams"]),
    }


def mux(video: Path, audio: Path, dst: Path) -> None:
    """Склеивает видео без звука и отдельную аудиодорожку без перекодирования."""
    tmp = dst.with_name(dst.stem + ".mux" + dst.suffix)
    r = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                        "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
                        "-c", "copy", "-shortest", "-movflags", "+faststart", str(tmp)],
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise MediaError(f"ffmpeg mux: {r.stderr.strip()[:300]}")
    os.replace(tmp, dst)

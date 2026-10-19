"""Полный доступ к upstream video-uniquifier CLI без урезания его функций.

Запуск: python -m app.uniq_full --help
        python -m app.uniq_full probe input.mp4
        python -m app.uniq_full run input.mp4 --profile instagram_reels --out output.mp4
Все аргументы после модуля передаются исходному инструменту без изменений.
"""
import os
from pathlib import Path
import subprocess
import sys


def upstream_executable() -> str:
    configured = os.environ.get("VIDEO_UNIQ_BIN", "").strip()
    if configured:
        return configured
    local = Path("/opt/video-uniq/bin/video-uniq")
    if local.is_file():
        return str(local)
    return "video-uniq"


def main(args: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if args is None else args)
    if not argv:
        print("Usage: python -m app.uniq_full <upstream-command> [arguments...]\n"
              "Examples: version, probe, preflight, run, batch, qa, calibrate, corpus, "
              "queue, worker, profile, subtitles, telemetry.\n"
              "Try: python -m app.uniq_full run --help")
        return 0
    try:
        return subprocess.call([upstream_executable(), *argv])
    except FileNotFoundError:
        print("video-uniq not installed. Install video-uniquifier[web] or use updated Docker image.",
              file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())

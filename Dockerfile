FROM python:3.12-slim

# ffmpeg из Debian собран с librubberband (сдвиг тона звука) — он нужен уникализатору
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Single, pinned upstream source tree; MIT attribution retained in vendor/LICENSE.
# v2.1.2 resolves to commit 2d967b15d182af6ac4d5c303c3adce625eba98b0.
ARG VIDEO_UNIQ_COMMIT=2d967b15d182af6ac4d5c303c3adce625eba98b0
RUN git clone https://github.com/Hostlife22/video-uniquifier.git vendor/video-uniquifier \
 && git -C vendor/video-uniquifier checkout --detach "$VIDEO_UNIQ_COMMIT" \
 && pip install --no-cache-dir -e vendor/video-uniquifier

# Required executable exposed by pyproject.toml.
RUN video-uniq --version
ENV VIDEO_UNIQ_BIN=video-uniq
COPY app/ app/
COPY main.py pytest.ini ./
COPY tests/ tests/

ENV DATA_DIR=/app/data \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    TZ=Europe/Moscow

VOLUME ["/app/data"]
CMD ["python", "main.py", "serve"]

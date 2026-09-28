FROM python:3.12-slim

# ffmpeg из Debian собран с librubberband (сдвиг тона звука) — он нужен уникализатору
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# yt_uniquifier — проверенная версия + наш патч чёрных углов при повороте
ARG YT_UNIQUIFIER_COMMIT=2016d000579925c90a8e1b1270e8c05b5f3fa014
COPY patches/ patches/
RUN git clone https://github.com/Hostlife22/yt_uniquifier.git vendor/yt_uniquifier \
 && git -C vendor/yt_uniquifier checkout "$YT_UNIQUIFIER_COMMIT" \
 && git -C vendor/yt_uniquifier apply ../../patches/yt_uniquifier-rotate-corners.patch \
 && pip install --no-cache-dir -e vendor/yt_uniquifier

COPY app/ app/
COPY main.py pytest.ini ./
COPY tests/ tests/

ENV DATA_DIR=/app/data \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    TZ=Europe/Moscow

VOLUME ["/app/data"]
CMD ["python", "main.py", "serve"]

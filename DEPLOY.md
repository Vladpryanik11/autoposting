# Запуск и развёртывание

## 1. Ключи и доступы (заполнить `.env`)

| Что | Где взять | Переменная |
|---|---|---|
| Токен Apify | console.apify.com → Settings → API & Integrations | `APIFY_TOKEN` |
| Токен Postmypost | app.postmypost.io → Access Tokens → Create | `POSTMYPOST_TOKEN` |
| Проект Postmypost | `python main.py accounts` | `POSTMYPOST_PROJECT_ID` |
| Бот Telegram | @BotFather → `/newbot` | `TELEGRAM_BOT_TOKEN` |
| Твой Telegram ID | запусти бота и напиши ему `/start` — он ответит ID | `TELEGRAM_ADMIN_IDS` |
| Ключ Claude (рерайт подписей) | console.anthropic.com → API Keys | `ANTHROPIC_API_KEY` |

Без `ANTHROPIC_API_KEY` всё работает, просто подпись публикуется очищенным оригиналом.

**Postmypost: API есть только на тарифе «Продвинутый»** (на «Стандартном» и «Премиальном» API отвечает
«Ваш тариф не поддерживает API»). Считаются только сателлиты, куда публикуем; основные аккаунты студии
в Postmypost подключать не нужно. Цена на 27.09.2026, помесячно: 5 акк. — 1 490 ₽, 10 — 2 390 ₽,
15 — 2 790 ₽, 20 — 3 290 ₽, 30 — 4 790 ₽ (годовая оплата −10 %).
Для первой пробы в `.env` поставь `APIFY_ARCHIVE_LIMIT=5` (≈ $0.02), потом верни 1000.

## 2. Windows (отладка на своём компьютере)

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
git clone https://github.com/Hostlife22/yt_uniquifier.git vendor/yt_uniquifier
git -C vendor/yt_uniquifier checkout 2016d000579925c90a8e1b1270e8c05b5f3fa014
git -C vendor/yt_uniquifier apply ../../patches/yt_uniquifier-rotate-corners.patch
.venv\Scripts\pip install -e vendor/yt_uniquifier
copy .env.example .env
```
Нужен `ffmpeg` в PATH (`winget install Gyan.FFmpeg`). Запуск — двойной клик по `start.bat`.
Пока компьютер выключен, уже поставленные в Postmypost публикации (на 2 дня вперёд) выходят сами,
но новые не готовятся.

## 3. VPS (постоянная работа)

Сервер: Ubuntu 22.04/24.04, **от 4 ядер**, 8 ГБ RAM, диск от 60 ГБ (архив 1000 роликов ≈ 10–20 ГБ).

```bash
curl -fsSL https://get.docker.com | sh
git clone <репозиторий проекта> autopost && cd autopost      # или скопировать папку проекта (без .venv и data)
cp .env.example .env && nano .env
docker compose up -d --build
docker compose logs -f                 # логи; Ctrl+C — выйти из просмотра
```
Обновление: `git pull && docker compose up -d --build`. База и видео лежат в `./data` и переживают
пересборку. Перенос с Windows: скопируй папку `data/` на сервер до первого запуска — продолжит с того же места.

Проверка внутри контейнера: `docker compose exec autopost python -m pytest -m "not slow"`.

## 4. Порядок первого запуска

1. Бот: `/start` → «🔄 Подтянуть аккаунты» — подключённые в Postmypost аккаунты появятся в 📤 Аккаунты.
2. Пришли в чат ссылки на рилсы — бот скачает их и через пару минут пришлёт отчёт.
3. Аккаунты публикуют сразу. Время постов — 📤 Аккаунты → аккаунт → 🕒 Время постов.
4. Через минуту `/queue`: появятся публикации; режим черновиков включён — в Postmypost будут черновики.
5. Проверь черновики в кабинете Postmypost (видео, подпись, заголовок YouTube).
6. Всё хорошо → ⚙️ Настройки → 🚀 Боевой режим. Дальше просто присылай ссылки на рилсы в чат.

## 5. Если что-то пошло не так

- `/status` — режимы, запас роликов по аккаунтам (⚠️ — меньше чем на 3 дня), ближайшие публикации.
- Ошибки приходят в бот сами; полный лог — `data/app.log`.
- Снять все будущие публикации аккаунта: `/account_off id` (удалит их и в Postmypost).
- Остановить всё новое: `/pause`.

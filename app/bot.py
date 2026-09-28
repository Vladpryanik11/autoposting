"""Telegram-бот: меню кнопками, карточки аккаунтов и публикаций, ссылки на рилсы, уведомления.

Бот не делает тяжёлую работу сам: настройки пишет в базу, поручения — в таблицу commands,
а рабочий процесс подхватывает их в течение ~20 секунд. Уведомления берёт из таблицы outbox.
Экраны (текст + кнопки) — в app/bot_views.py.
"""
import asyncio
import json
import logging
import re
from contextlib import closing
from datetime import datetime, timedelta

from telegram import BotCommand, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from app import bot_views as v
from app import db, destinations, sources
from app.captions import Captioner
from app.config import Settings
from app.pipeline import Pipeline

log = logging.getLogger(__name__)
PUB_RE = re.compile(r"(?:#pub|№)(\d+)")
GROUP_RE = re.compile(r"^[\w\-]{1,30}$")
MAX_VIDEO_BYTES = 45 * 1024 * 1024  # лимит Telegram для ботов — 50 МБ
MAX_TXT_BYTES = 1024 * 1024
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
APIFY_USAGE_TTL = timedelta(minutes=5)

MENU_ROUTES = {v.MENU_STATUS: "m:status", v.MENU_HISTORY: "m:hist:0", v.MENU_SETTINGS: "m:set",
               # кнопки прежнего меню — пока у пользователя не обновилась клавиатура
               "🗓 Очередь": "m:queue:0", "📥 Источники": "m:src", "📤 Аккаунты": "m:set", "❓ Помощь": "m:help"}


def cfg_of(ctx) -> Settings:
    return ctx.application.bot_data["cfg"]


def open_db(ctx) -> "closing":
    return closing(db.connect(cfg_of(ctx).db_path))


def pipeline(conn, cfg: Settings) -> Pipeline:
    return Pipeline(conn, cfg, captioner=Captioner(), render=lambda *_: {})


def is_admin(update: Update, ctx) -> bool:
    return bool(update.effective_user) and update.effective_user.id in cfg_of(ctx).telegram_admin_ids


def fit(text: str, limit: int = 3900) -> str:
    """Telegram не примет больше 4096 символов — обрезаем по целым строкам (теги HTML не рвутся)."""
    if len(text) <= limit:
        return text
    out = ""
    for line in text.split("\n"):
        if len(out) + len(line) + 1 > limit:
            break
        out += line + "\n"
    return out + "… (показано не всё)"


def stash_links(ctx, links: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Ссылки копятся до подтверждения: длинный список Telegram режет на несколько сообщений."""
    pending = ctx.user_data.get("links") or []
    seen = {sc for sc, _ in pending}
    merged = pending + [(sc, url) for sc, url in links if sc not in seen]
    ctx.user_data["links"] = merged
    return merged


async def show(update: Update, view, edit: bool = False, **kw) -> None:
    text, markup = view
    text = fit(text)
    if edit and update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                                          link_preview_options=NO_PREVIEW)
            return
        except BadRequest as err:
            if "not modified" in str(err).lower():
                return
            log.warning("edit_message_text: %s — отправляю новым сообщением", err)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                              link_preview_options=NO_PREVIEW, **kw)


def _rebuild(conn, dest_ids: list[int]) -> None:
    """Настройки аккаунта изменились — снять его будущие публикации, рабочий процесс пересоберёт."""
    for i in dest_ids:
        db.add_command(conn, "unschedule", str(i))


# --- маршрутизация кнопок (синхронная часть — тестируется без Telegram) ---------------------

def route(p: Pipeline, user_data: dict, data: str, today=None):
    """Нажатие кнопки → (экран или None, всплывающая подсказка или None)."""
    conn = p.conn
    parts = data.split(":")
    head = parts[0]
    num = lambda i: int(parts[i])  # noqa: E731

    if head == "m":
        screen = parts[1]
        page = num(2) if len(parts) > 2 else 0
        return {"start": lambda: v.welcome(p), "status": lambda: v.status_view(p),
                "queue": lambda: v.queue_view(p, page), "review": lambda: v.review_list_view(p),
                "hist": lambda: v.history_view(p, page), "set": lambda: v.settings_view(p),
                "src": lambda: v.sources_view(p), "errors": lambda: v.errors_view(p),
                "help": lambda: v.help_view(p)}[screen](), None

    if data == "nav:cancel":
        user_data.pop("await", None)
        return ("Отменено.", None), None

    if head == "src":
        action = parts[1]
        if action == "add":
            user_data["await"] = ("add_source",)
            return v.prompt("add_source"), None
        if action == "links":
            user_data["await"] = ("links",)
            return v.prompt("links"), None
        if action == "fetch":
            db.add_command(conn, "fetch")
            return None, "📥 Проверяю — отчёт придёт сюда через пару минут"
        if action == "tog":
            conn.execute("UPDATE sources SET enabled = 1 - enabled WHERE id = ? AND kind = 'profile'", (num(2),))
            return v.sources_view(p), "Готово"

    if head == "acc":
        if parts[1].isdigit():
            return v.account_card(p, num(1)), None
        action, dest_id = parts[1], (num(2) if len(parts) > 2 else None)
        if action == "tog":
            row = conn.execute("SELECT enabled FROM destinations WHERE id = ?", (dest_id,)).fetchone()
            destinations.update(conn, [dest_id], enabled=0 if row["enabled"] else 1)
            if row["enabled"]:
                _rebuild(conn, [dest_id])
            return v.account_card(p, dest_id), ("⏸ Выключен" if row["enabled"] else "▶️ Включён")
        if action == "n":
            destinations.update(conn, [dest_id], slots=v.PER_DAY[num(3)])
            _rebuild(conn, [dest_id])
            return v.account_card(p, dest_id), f"🕒 {num(3)} в день — расписание обновится через минуту"
        if action == "sx":
            user_data["await"] = ("slots", dest_id)
            return v.prompt("slots"), None
        if action == "grp":
            return v.group_picker(p, dest_id), None
        if action == "g":
            group = v.group_from_button(destinations.groups(conn), parts[3:])
            if group is None:
                return v.group_picker(p, dest_id), "Список групп изменился — выбери ещё раз"
            destinations.update(conn, [dest_id], route_group=group)
            _rebuild(conn, [dest_id])
            return v.account_card(p, dest_id), "🏷 Группа изменена"
        if action == "gx":
            user_data["await"] = ("group", dest_id)
            return v.prompt("group"), None

    if head == "pub":
        if parts[1].isdigit():
            return v.pub_card(p, num(1)), None
        action, pub_id = parts[1], num(2)
        if action == "edit":
            user_data["await"] = ("edit_caption", pub_id)
            return v.prompt("edit_caption"), None
        if action == "un":
            return v.cancel_confirm(p, pub_id), None
        if action == "unok":
            db.add_command(conn, "cancel", str(pub_id))
            return v.queue_view(p), "🚫 Снимаю с публикации"

    if head == "rv":
        result = p.review(num(2), parts[1] == "1")
        return v.pub_card(p, num(2)), result

    if head == "set" and parts[1] == "pause":
        db.set_kv(conn, "paused", parts[2])
        return v.status_view(p), "⏸ Пауза" if parts[2] == "1" else "▶️ Работаем"

    if head == "lnk" and parts[1] == "undo":
        cur = conn.execute("DELETE FROM commands WHERE id = ? AND kind = 'links' AND done_at IS NULL", (num(2),))
        if cur.rowcount:
            return ("↩️ Отменено — эти ссылки не будут скачаны.", None), None
        return None, "Уже в работе — отдельную публикацию можно снять в 🗓 Очереди"

    if head == "lnk":
        links = user_data.pop("links", None)
        if parts[1] == "no" or not links:
            return ("Отменено." if links else "Ссылки устарели — пришли их ещё раз.", None), None
        group = v.group_from_button(destinations.groups(conn) or ["default"], parts[2:])
        if group is None:
            user_data["links"] = links
            return v.links_confirm(p, links), "Список групп изменился — выбери ещё раз"
        db.add_command(conn, "links", json.dumps({"links": links, "group": group}))
        return (f"🔗 Принято: {len(links)}. Скачаю и пришлю отчёт — обычно пара минут.", None), None

    return ("Эта кнопка устарела — открой раздел из меню внизу.", None), None


# --- обработчики ---------------------------------------------------------------------

async def cmd_start(update: Update, ctx) -> None:
    ctx.user_data.pop("await", None)
    with open_db(ctx) as conn:
        text, markup = v.welcome(pipeline(conn, cfg_of(ctx)))
    if markup is None:  # одно сообщение: приветствие + меню внизу
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=v.main_keyboard(),
                                                  link_preview_options=NO_PREVIEW)
    else:  # нужна кнопка в сообщении — меню отдельным
        await update.effective_message.reply_text("Меню — внизу 👇", reply_markup=v.main_keyboard())
        await show(update, (text, markup))


async def cmd_screen(update: Update, ctx, data: str) -> None:
    await show_screen(update, ctx, data, edit=False)


async def show_screen(update: Update, ctx, data: str, edit: bool) -> None:
    cfg = cfg_of(ctx)
    if data == "m:set":  # в настройках — расход Apify (сетевой запрос, кешируем)
        usage = await apify_usage(ctx)
        with open_db(ctx) as conn:
            return await show(update, v.settings_view(pipeline(conn, cfg), usage), edit)
    with open_db(ctx) as conn:
        view, _ = route(pipeline(conn, cfg), ctx.user_data, data)
    await show(update, view, edit)


async def apify_usage(ctx):
    cfg = cfg_of(ctx)
    if not cfg.apify_token:
        return None
    cache = ctx.application.bot_data.get("apify_usage")
    if cache and datetime.now() - cache[0] < APIFY_USAGE_TTL:
        return cache[1]

    def fetch():
        import requests
        r = requests.get("https://api.apify.com/v2/users/me/limits", timeout=5,
                         headers={"Authorization": f"Bearer {cfg.apify_token}"})
        r.raise_for_status()
        d = r.json()["data"]
        return (float((d.get("current") or {}).get("monthlyUsageUsd") or 0),
                float((d.get("limits") or {}).get("maxMonthlyUsageUsd") or 0))
    try:
        usage = await asyncio.to_thread(fetch)
    except Exception as e:  # noqa: BLE001 — нет данных о расходе не повод ломать экран
        log.warning("Apify usage: %s", e)
        usage = None
    ctx.application.bot_data["apify_usage"] = (datetime.now(), usage)
    return usage


async def sync_accounts(update: Update, ctx) -> str:
    cfg = cfg_of(ctx)
    if not (cfg.postmypost_token and cfg.postmypost_project_id):
        return "Нужны POSTMYPOST_TOKEN и POSTMYPOST_PROJECT_ID в .env"

    def work():
        from app.postmypost import Postmypost
        with closing(db.connect(cfg.db_path)) as conn:
            return destinations.sync_from_postmypost(conn, Postmypost(cfg.postmypost_token),
                                                     cfg.postmypost_project_id, cfg.default_slots)
    try:
        added, updated, warnings = await asyncio.to_thread(work)
    except Exception as e:  # noqa: BLE001
        return f"Postmypost: {str(e)[:150]}"
    return f"🔄 Новых аккаунтов: {added}, обновлено: {updated}" + (f". {warnings[0]}" if warnings else "")


async def answer(q, text: str | None = None, show_alert: bool = False) -> None:
    """Ответ на нажатие. Старое нажатие (Telegram ждёт ответ ~15 с) — не повод ронять обработчик."""
    try:
        await q.answer(text, show_alert=show_alert)
    except BadRequest as e:
        log.info("answer callback: %s", e)


async def on_callback(update: Update, ctx) -> None:
    q = update.callback_query
    log.info("Кнопка %s от %s", q.data, update.effective_user.id if update.effective_user else "?")
    if not is_admin(update, ctx):
        return await answer(q, "Нет доступа")
    ctx.user_data.pop("await", None)  # ушёл с экрана ввода — текст больше не ждём (кнопки ввода поставят снова)
    if q.data == "acc:sync":
        await answer(q, "🔄 Подтягиваю аккаунты из Postmypost…")
        toast = await sync_accounts(update, ctx)
        await update.effective_message.reply_text(toast)
        return await show_screen(update, ctx, "m:acc", edit=True)
    if q.data == "hist:csv":
        await answer(q, "📄 Готовлю таблицу…")
        return await send_history_csv(update, ctx)
    if q.data == "m:set":
        await answer(q)
        return await show_screen(update, ctx, "m:set", edit=True)
    with open_db(ctx) as conn:
        view, toast = route(pipeline(conn, cfg_of(ctx)), ctx.user_data, q.data)
    await answer(q, toast[:190] if toast else None)
    if view:
        await show(update, view, edit=True)


async def on_text(update: Update, ctx) -> None:
    text = update.message.text or ""
    if text in MENU_ROUTES:
        ctx.user_data.pop("await", None)
        return await show_screen(update, ctx, MENU_ROUTES[text], edit=False)
    cfg = cfg_of(ctx)
    waiting = ctx.user_data.get("await")
    links = sources.extract_reel_links(text)
    if waiting and links and waiting[0] != "links":
        ctx.user_data.pop("await", None)  # прислали ссылки на рилсы — значит, это они, а не подпись/время
        waiting = None
    with open_db(ctx) as conn:
        p = pipeline(conn, cfg)
        if waiting:
            return await handle_input(update, ctx, p, waiting, text, links)
        if links:
            return await accept_links(update, ctx, p, links)
        replied = update.message.reply_to_message
        m = PUB_RE.search((replied.text or replied.caption or "") if replied else "")
        if m:
            return await update.message.reply_text(p.review(int(m.group(1)), True, text))
    await update.message.reply_text("Это не команда 🙂 Выбери раздел в меню внизу или пришли ссылки на рилсы.",
                                    reply_markup=v.main_keyboard())


async def handle_input(update: Update, ctx, p: Pipeline, waiting: tuple, text: str, links) -> None:
    kind = waiting[0]
    conn = p.conn
    try:
        if kind == "add_source":
            results = []
            for line in filter(None, (x.strip() for x in text.splitlines())):
                name, *rest = line.split()
                group = rest[0] if rest and GROUP_RE.match(rest[0]) else "default"
                try:
                    username, created = sources.add(conn, name, group)
                    results.append(f"{'✅ добавлен' if created else '• уже есть'}: @{username} "
                                   f"(группа {db.group_label(sources.group_of(conn, username))})")
                except ValueError as err:
                    results.append(f"⚠️ {err}")
            ctx.user_data.pop("await", None)
            await update.message.reply_text("\n".join(results) + "\n\nНачну забирать ролики в течение часа "
                                            "или сразу по кнопке «📥 Проверить сейчас».")
            return await show(update, v.sources_view(p))
        if kind == "links":
            if not links:
                return await update.message.reply_text(
                    "Ссылок на рилсы Instagram здесь нет. Нужны ссылки вида instagram.com/reel/…")
            ctx.user_data.pop("await", None)
            return await accept_links(update, ctx, p, links)
        if kind == "slots":
            slots = destinations.parse_slots(text.replace(",", " ").split())
            destinations.update(conn, [waiting[1]], slots=slots)
            _rebuild(conn, [waiting[1]])
        elif kind == "group":
            name = text.strip()
            if not GROUP_RE.match(name):
                raise ValueError("Название группы — одно слово из букв, цифр, _ или -, до 30 символов.")
            destinations.update(conn, [waiting[1]], route_group=name)
            _rebuild(conn, [waiting[1]])
        elif kind == "edit_caption":
            ctx.user_data.pop("await", None)
            await update.message.reply_text(p.review(waiting[1], True, text))
            return await show(update, v.pub_card(p, waiting[1]))
    except ValueError as err:
        return await update.message.reply_text(f"⚠️ {err}\nПопробуй ещё раз или нажми «✖️ Отмена».")
    ctx.user_data.pop("await", None)
    await update.message.reply_text("✅ Сохранено. Расписание аккаунта обновится в течение минуты.")
    await show(update, v.account_card(p, waiting[1]))


async def accept_links(update: Update, ctx, p: Pipeline, links) -> None:
    """Одна группа аккаунтов — ссылки сразу в работу (с кнопкой «Отменить»); несколько — спросить группу."""
    merged = stash_links(ctx, links)
    groups = destinations.groups(p.conn) or ["default"]
    if len(groups) > 1:
        return await show(update, v.links_confirm(p, merged))
    ctx.user_data.pop("links", None)
    command_id = db.add_command(p.conn, "links", json.dumps({"links": merged, "group": groups[0]}))
    await show(update, v.links_queued(p, merged, command_id))


async def send_history_csv(update: Update, ctx) -> None:
    import io
    with open_db(ctx) as conn:
        data = v.history_csv(pipeline(conn, cfg_of(ctx)))
    name = f"publications_{datetime.now():%Y-%m-%d}.csv"
    await update.effective_message.reply_document(io.BytesIO(data), filename=name,
                                                  caption="📄 История публикаций — открывается в Excel "
                                                          "и Google Таблицах.")


async def on_document(update: Update, ctx) -> None:
    doc = update.message.document
    if not doc or doc.file_size and doc.file_size > MAX_TXT_BYTES:
        return await update.message.reply_text("Пришли .txt-файл до 1 МБ со ссылками на рилсы.")
    data = await (await doc.get_file()).download_as_bytearray()
    text = bytes(data).decode("utf-8", errors="ignore")
    links = sources.extract_reel_links(text)
    if not links:
        return await update.message.reply_text("В файле нет ссылок на рилсы Instagram (instagram.com/reel/…).")
    ctx.user_data.pop("await", None)
    with open_db(ctx) as conn:
        await accept_links(update, ctx, pipeline(conn, cfg_of(ctx)), links)


async def on_error(update: object, ctx) -> None:
    """Любая ошибка в обработчике — в журнал и коротко пользователю, чтобы не было «кнопка молчит»."""
    log.error("Ошибка в обработчике бота", exc_info=ctx.error)
    if isinstance(update, Update):
        try:
            if update.callback_query:
                await update.callback_query.answer("⚠️ Что-то пошло не так — попробуй ещё раз", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text(
                    f"⚠️ Не получилось: {str(ctx.error)[:200]}\nПопробуй ещё раз или открой раздел из меню.")
        except TelegramError:
            pass


async def on_unknown_command(update: Update, ctx) -> None:
    await update.effective_message.reply_text(v.UNKNOWN_COMMAND, reply_markup=v.main_keyboard())


async def on_stranger(update: Update, ctx) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            f"Нет доступа. Твой Telegram ID: {update.effective_user.id} — добавь его в TELEGRAM_ADMIN_IDS.")


# --- уведомления --------------------------------------------------------------------------

async def send_outbox(app: Application) -> int:
    cfg: Settings = app.bot_data["cfg"]
    with closing(db.connect(cfg.db_path)) as conn:
        rows = conn.execute("SELECT * FROM outbox WHERE sent_at IS NULL ORDER BY id LIMIT 20").fetchall()
        for m in rows:
            video, view = None, None
            if m["kind"] == "review" and m["publication_id"]:
                p = pipeline(conn, cfg)
                view = v.pub_card(p, m["publication_id"])
                pub = conn.execute("SELECT variant_path FROM publications WHERE id = ?",
                                   (m["publication_id"],)).fetchone()
                if pub and pub["variant_path"]:
                    path = cfg.data_dir / pub["variant_path"]
                    video = path if path.exists() and path.stat().st_size <= MAX_VIDEO_BYTES else None
            delivered = True
            for chat_id in cfg.telegram_admin_ids:
                try:  # главное — карточка с кнопками; она уходит первой и отдельно от видео
                    if view:
                        await app.bot.send_message(chat_id, fit("🔎 <b>Нужна проверка</b>\n" + view[0]),
                                                   parse_mode=ParseMode.HTML, reply_markup=view[1],
                                                   link_preview_options=NO_PREVIEW)
                    else:
                        await app.bot.send_message(chat_id, m["text"][:4000], link_preview_options=NO_PREVIEW)
                except NetworkError as e:  # сеть/лимит Telegram — не помечаем отправленным, повторим
                    log.warning("Telegram → %s: %s — повторю", chat_id, e)
                    delivered = False
                    continue
                except TelegramError as e:  # чат недоступен, сообщение некорректно — повтор не поможет
                    log.warning("Telegram → %s: %s", chat_id, e)
                    continue
                if video:
                    try:
                        with open(video, "rb") as fh:
                            await app.bot.send_video(chat_id, fh, supports_streaming=True,
                                                     read_timeout=180, write_timeout=180)
                    except TelegramError as e:
                        log.warning("Видео → %s: %s", chat_id, e)
            if delivered:
                conn.execute("UPDATE outbox SET sent_at = ? WHERE id = ?", (db.now(), m["id"]))
            else:
                break  # сохраняем порядок уведомлений: дальше — после восстановления связи
        return len(rows)


async def outbox_loop(app: Application) -> None:
    while True:
        try:
            await send_outbox(app)
        except Exception:  # noqa: BLE001
            log.exception("Отправка уведомлений упала")
        await asyncio.sleep(5)


async def _post_init(app: Application) -> None:
    try:
        await app.bot.set_my_commands([BotCommand("start", "шаги запуска и меню"), BotCommand("status", "статус"),
                                       BotCommand("queue", "очередь публикаций"),
                                       BotCommand("help", "как это работает")])
    except TelegramError as e:  # не критично — меню команд обновится при следующем запуске
        log.warning("set_my_commands: %s", e)
    with closing(db.connect(app.bot_data["cfg"].db_path)) as conn:
        db.notify(conn, "info", "✅ Бот обновлён. Нажми /start — обновится меню.")
    app.bot_data["outbox_task"] = asyncio.get_running_loop().create_task(outbox_loop(app))


async def _post_shutdown(app: Application) -> None:
    task = app.bot_data.get("outbox_task")
    if task:
        task.cancel()


def build_app(cfg: Settings) -> Application:
    app = (Application.builder().token(cfg.telegram_bot_token)
           .connect_timeout(20).read_timeout(30).write_timeout(60).pool_timeout(20).concurrent_updates(True)
           .get_updates_connect_timeout(20).get_updates_read_timeout(40)
           .post_init(_post_init).post_shutdown(_post_shutdown).build())
    app.bot_data["cfg"] = cfg
    admins = filters.User(user_id=list(cfg.telegram_admin_ids))
    app.add_handler(CommandHandler(["start", "menu"], cmd_start, filters=admins))
    for name, data in (("status", "m:status"), ("queue", "m:queue:0"), ("help", "m:help")):
        app.add_handler(CommandHandler(name, lambda u, c, d=data: cmd_screen(u, c, d), filters=admins))
    app.add_handler(CommandHandler("cancel", lambda u, c: show_screen(u, c, "nav:cancel", False), filters=admins))
    app.add_handler(CallbackQueryHandler(on_callback))
    new_msg = filters.UpdateType.MESSAGE  # правки старых сообщений не обрабатываем
    app.add_handler(MessageHandler(new_msg & admins & filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(new_msg & admins & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(new_msg & admins & filters.COMMAND, on_unknown_command))
    app.add_handler(MessageHandler(~admins, on_stranger))
    app.add_error_handler(on_error)
    return app

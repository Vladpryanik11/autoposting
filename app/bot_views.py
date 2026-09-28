"""Экраны Telegram-бота: текст (HTML) + кнопки. Чистые функции от базы — легко проверять тестами.

Интерфейс минимальный — под сценарий «присылаю ссылки на рилсы, они публикуются»:
  меню: 📊 Статус · 📜 История · ⚙️ Настройки;
  всё остальное — внутри этих экранов и только когда нужно.
Слова: «аккаунт» — куда публикуем; «очередь» — что ещё выйдет; «автосбор» — забирать рилсы с аккаунтов самому.
"""
import html
import json
import zlib
from datetime import timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from app import db, destinations, planner, sources
from app.db import STATUS_LABELS
from app.db import group_label, tz_label
from app.pipeline import PLATFORM_ICON, Pipeline

e = html.escape
View = tuple[str, InlineKeyboardMarkup | None]

MENU_STATUS, MENU_HISTORY, MENU_SETTINGS = "📊 Статус", "📜 История", "⚙️ Настройки"
MENU = [[MENU_STATUS, MENU_HISTORY], [MENU_SETTINGS]]

PLATFORM_NAME = {"instagram": "Instagram", "tiktok": "TikTok", "youtube": "YouTube Shorts", "other": "другая сеть"}
# Сколько постов в день → время постов
PER_DAY = {1: ["18:00"], 2: ["12:00", "19:00"], 3: ["10:00", "15:00", "20:00"],
           4: ["09:00", "13:00", "17:00", "21:00"], 5: ["09:00", "12:00", "15:00", "18:00", "21:00"]}
UPCOMING = (db.PLANNED, db.REVIEW, db.READY, db.SCHEDULED, db.DRAFTED)
REEL_PRICE = 0.0026
# Статусы в очереди — коротко и по-человечески
QUEUE_STATUS = {db.PLANNED: "готовлю", db.READY: "готово", db.REVIEW: "🔎 нужно твоё решение",
                db.SCHEDULED: "✅ стоит в расписании", db.DRAFTED: "черновик"}


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows if row])


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(MENU, resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="Пришли ссылки на рилсы")


def group_button(i: int, group: str) -> str:
    """Номер группы + короткая метка названия: старая кнопка не назначит другую группу молча."""
    return f"{i}:{zlib.crc32(group.encode()) % 100000}"


def group_from_button(groups: list[str], parts: list[str]) -> str | None:
    try:
        i, tag = int(parts[0]), parts[1]
    except (IndexError, ValueError):
        return None
    if i >= len(groups) or group_button(i, groups[i]) != f"{i}:{tag}":
        return None
    return groups[i]


def _count(conn, sql: str, *args) -> int:
    return conn.execute(sql, args).fetchone()[0] or 0


def when(p: Pipeline, iso: str) -> str:
    """«сегодня 12:07», «завтра 09:03», «30.09 15:00»."""
    t = planner.from_iso(iso).astimezone(p.tz)
    today = p.clock().astimezone(p.tz).date()
    day = {today: "сегодня", today + timedelta(days=1): "завтра"}.get(t.date(), t.strftime("%d.%m"))
    return f"{day} {t:%H:%M}"


def _slots_text(d) -> str:
    slots = json.loads(d["slots"])
    return f"{len(slots)} в день — " + ", ".join(slots)


def _several_groups(conn) -> bool:
    return len(destinations.groups(conn)) > 1


def _upcoming(p: Pipeline, limit: int, offset: int = 0, status: str | None = None):
    statuses = (status,) if status else UPCOMING
    return p.conn.execute(
        f"SELECT x.id, x.slot_at, x.status, d.name, d.platform FROM publications x "
        f"JOIN destinations d ON d.id = x.destination_id WHERE x.status IN ({','.join('?' * len(statuses))}) "
        f"ORDER BY x.slot_at LIMIT ? OFFSET ?", (*statuses, limit, offset)).fetchall()


def _line(p: Pipeline, r, with_status: bool = True) -> str:
    s = f"{when(p, r['slot_at'])} · {PLATFORM_ICON.get(r['platform'], '🌐')} {e(r['name'])}"
    return s + (f" · {QUEUE_STATUS.get(r['status'], '')}" if with_status and r["status"] != db.SCHEDULED else "")


# --- старт и статус ------------------------------------------------------------------

def welcome(p: Pipeline) -> View:
    dests = p.conn.execute("SELECT * FROM destinations WHERE enabled = 1 ORDER BY name").fetchall()
    if not dests:
        return ("<b>👋 Привет!</b>\nПрисылай мне ссылки на рилсы — я опубликую их в твоих аккаунтах.\n\n"
                "Сначала подключи аккаунты (Instagram, TikTok, YouTube) в Postmypost и нажми кнопку ниже.",
                kb([[("🔄 Подтянуть аккаунты", "acc:sync")]]))
    lines = ["<b>👋 Привет!</b>", "Присылай ссылки на рилсы — одну, списком или .txt-файлом. "
             "Я скачаю их, сделаю уникальную копию и опубликую в:"]
    lines += [f"{PLATFORM_ICON.get(d['platform'], '🌐')} {e(d['name'])} — {_slots_text(d)}" for d in dests]
    lines += ["", "Меню внизу: 📊 Статус — что в очереди, 📜 История — что вышло, ⚙️ Настройки — аккаунты и время."]
    return "\n".join(lines), None


def status_view(p: Pipeline) -> View:
    c = p.conn
    paused = db.get_kv(c, "paused") == "1"
    n_dests = _count(c, "SELECT COUNT(*) FROM destinations WHERE enabled = 1")
    queued = _count(c, f"SELECT COUNT(*) FROM publications WHERE status IN ({','.join('?' * len(UPCOMING))})",
                    *UPCOMING)
    downloading = _count(c, "SELECT COUNT(*) FROM source_posts WHERE status IN (?, ?)",
                         db.DISCOVERED, db.NEEDS_REFRESH) + \
        (1 if c.execute("SELECT 1 FROM commands WHERE kind IN ('links', 'fetch') AND done_at IS NULL").fetchone()
         else 0)
    waiting = _count(c, "SELECT COUNT(*) FROM source_posts p WHERE p.status = ? AND NOT EXISTS "
                        "(SELECT 1 FROM publications x WHERE x.source_post_id = p.id)", db.DOWNLOADED)
    n_review = _count(c, "SELECT COUNT(*) FROM publications WHERE status = ?", db.REVIEW)

    lines = ["⏸ <b>На паузе</b> — новое не публикуется" if paused else "▶️ <b>Работает</b>"]
    if not n_dests:
        lines.append("Нет аккаунтов для публикации — ⚙️ Настройки → 🔄 Подтянуть аккаунты.")
    lines.append(f"🗓 В очереди: {queued}" + (" · ещё скачивается" if downloading else "")
                 + (f" · ждут своего времени: {waiting}" if waiting else ""))
    if n_review:
        lines.append(f"🔎 Нужно твоё решение: {n_review}")
    upcoming = _upcoming(p, 5)
    if upcoming:
        lines += ["", "<b>Ближайшие</b>"] + [_line(p, r) for r in upcoming]
    elif n_dests and not downloading:
        lines += ["", "Очередь пустая — пришли ссылки на рилсы 👇"]
    rows = []
    if n_review:
        rows.append([(f"🔎 Решить ({n_review})", "m:review")])
    rows.append([("🗓 Вся очередь", "m:queue:0"),
                 ("▶️ Продолжить" if paused else "⏸ Пауза", f"set:pause:{0 if paused else 1}")])
    return "\n".join(lines), kb(rows)


def queue_view(p: Pipeline, page: int = 0, per: int = 8) -> View:
    rows = _upcoming(p, per + 1, page * per)
    has_next, rows = len(rows) > per, rows[:per]
    if not rows and page == 0:
        return "🗓 Очередь пустая — пришли ссылки на рилсы.", kb([[("◀️ Назад", "m:status")]])
    lines = ["<b>🗓 Очередь</b> — нажми номер, чтобы открыть"]
    lines += [f"<b>{page * per + i}.</b> {_line(p, r)}" for i, r in enumerate(rows, 1)]
    numbers = [(str(page * per + i), f"pub:{r['id']}") for i, r in enumerate(rows, 1)]
    nav = ([("◀️", f"m:queue:{page - 1}")] if page else []) + ([("▶️", f"m:queue:{page + 1}")] if has_next else [])
    return "\n".join(lines), kb([numbers[i:i + 4] for i in range(0, len(numbers), 4)]
                                + [nav, [("◀️ Назад", "m:status")]])


def review_list_view(p: Pipeline) -> View:
    rows = _upcoming(p, 20, status=db.REVIEW)
    if not rows:
        return "🔎 Сейчас ничего не нужно решать.", kb([[("◀️ Назад", "m:status")]])
    lines = ["<b>🔎 Нужно твоё решение</b>"] + [f"<b>{i}.</b> {_line(p, r, False)}" for i, r in enumerate(rows, 1)]
    numbers = [(str(i), f"pub:{r['id']}") for i, r in enumerate(rows, 1)]
    return "\n".join(lines), kb([numbers[i:i + 4] for i in range(0, len(numbers), 4)])


def pub_card(p: Pipeline, pub_id: int) -> View:
    r = p._row(pub_id)
    if not r:
        return "Эта публикация больше не существует.", kb([[("◀️ Назад", "m:queue:0")]])
    lines = [f"{PLATFORM_ICON.get(r['platform'], '🌐')} <b>{e(r['dest_name'])}</b> · {when(p, r['slot_at'])}",
             f"<a href=\"{e(r['orig_url'])}\">Оригинал</a> · {STATUS_LABELS.get(r['status'], r['status'])}"]
    if r["status"] == db.REVIEW:
        lines.append("\n🔎 <b>Проверь подпись:</b> в ней есть призыв к действию, но непонятно, какое "
                     "кодовое слово должны писать. Если текст в порядке — жми «Опубликовать», "
                     "если нужно поправить — «Изменить текст».")
    if r["status"] == db.FAILED and r["error"]:
        lines.append(f"❌ {e(r['error'][:300])}")
    caption = r["caption"] or r["original_caption"] or ""
    lines.append(f"<blockquote>{e(caption[:800]) or '(без подписи)'}</blockquote>")
    rows = []
    if r["status"] == db.REVIEW:
        rows.append([("✅ Опубликовать", f"rv:1:{pub_id}"), ("❌ Пропустить", f"rv:0:{pub_id}")])
        rows.append([("✏️ Изменить текст", f"pub:edit:{pub_id}")])
    elif r["status"] in (db.PLANNED, db.READY, db.SCHEDULED, db.DRAFTED) and \
            planner.from_iso(r["slot_at"]) > p.clock():
        rows.append([("🚫 Не публиковать", f"pub:un:{pub_id}")])
    rows.append([("◀️ Назад", "m:queue:0")])
    return "\n".join(lines), kb(rows)


def cancel_confirm(p: Pipeline, pub_id: int) -> View:
    r = p._row(pub_id)
    return (f"Не публиковать это видео в <b>{e(r['dest_name'])}</b>? Вместо него в это время выйдет другое.",
            kb([[("🚫 Да, не публиковать", f"pub:unok:{pub_id}"), ("Оставить", f"pub:{pub_id}")]]))


# --- ссылки ------------------------------------------------------------------------------

def _links_summary(p: Pipeline, links: list[tuple[str, str]]) -> list[str]:
    known = {r[0] for r in p.conn.execute(
        f"SELECT shortcode FROM source_posts WHERE shortcode IN ({','.join('?' * len(links))}) "
        f"AND status <> ?", [sc for sc, _ in links] + [db.FAILED])}
    return [f"Из них уже были у меня: {len(known)} — поставлю их первыми."] if known else []


def links_queued(p: Pipeline, links: list[tuple[str, str]], command_id: int) -> View:
    return ("\n".join([f"🔗 Принято: {len(links)} {plural(len(links), 'ссылка', 'ссылки', 'ссылок')} на рилсы.",
                       *_links_summary(p, links),
                       "Скачаю и поставлю в ближайшее время постов — отчёт пришлю сюда."]),
            kb([[("↩️ Отменить", f"lnk:undo:{command_id}")]]))


def links_confirm(p: Pipeline, links: list[tuple[str, str]]) -> View:
    """Только если групп аккаунтов несколько — спросить, куда."""
    groups = destinations.groups(p.conn) or ["default"]
    lines = [f"🔗 Ссылок на рилсы: <b>{len(links)}</b>", *_links_summary(p, links), "", "В какие аккаунты их отправить?"]
    rows = [[(f"✅ {group_label(g)}", f"lnk:ok:{group_button(i, g)}")] for i, g in enumerate(groups)]
    rows.append([("✖️ Отмена", "lnk:no")])
    return "\n".join(lines), kb(rows)


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(n % 10, many)


# --- история ---------------------------------------------------------------------------

def _history_rows(p: Pipeline, limit: int | None = None, offset: int = 0):
    """Что уже вышло (или должно было): вышедшие, ошибки, черновики с прошедшим временем. Новые сверху."""
    sql = ("SELECT x.*, d.name AS dest_name, d.platform, sp.url AS orig_url, sp.caption AS orig_caption, "
           "s.username FROM publications x JOIN destinations d ON d.id = x.destination_id "
           "JOIN source_posts sp ON sp.id = x.source_post_id JOIN sources s ON s.id = sp.source_id "
           "WHERE x.status IN (?, ?) OR (x.status = ? AND x.slot_at <= ?) ORDER BY x.slot_at DESC")
    args = [db.PUBLISHED, db.FAILED, db.DRAFTED, planner.to_iso(p.clock())]
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        args += [limit, offset]
    return p.conn.execute(sql, args).fetchall()


def _is_profile(url: str) -> bool:
    """Instagram через Postmypost отдаёт ссылку на профиль, а не на пост."""
    return "instagram.com/" in url and not any(x in url for x in ("/reel/", "/p/", "/tv/"))


def history_view(p: Pipeline, page: int = 0, per: int = 10) -> View:
    rows = _history_rows(p, per + 1, page * per)
    has_next, rows = len(rows) > per, rows[:per]
    if not rows and page == 0:
        return "📜 Пока ничего не вышло. Здесь будут опубликованные ролики со ссылками.", None
    lines = ["<b>📜 История</b> — новые сверху"]
    for r in rows:
        mark = {db.PUBLISHED: "🟢", db.FAILED: "❌", db.DRAFTED: "📝"}.get(r["status"], "")
        links = [f"<a href=\"{e(r['orig_url'])}\">оригинал</a>"]
        if r["post_url"]:
            links.insert(0, f"<a href=\"{e(r['post_url'])}\">"
                            f"{'аккаунт' if _is_profile(r['post_url']) else 'пост'}</a>")
        lines.append(f"{mark} {when(p, r['published_at'] or r['slot_at'])} · "
                     f"{PLATFORM_ICON.get(r['platform'], '🌐')} {e(r['dest_name'])} · " + " · ".join(links))
    nav = ([("◀️", f"m:hist:{page - 1}")] if page else []) + ([("▶️", f"m:hist:{page + 1}")] if has_next else [])
    return "\n".join(lines), kb([nav, [("📄 Скачать таблицей", "hist:csv")]])


def history_csv(p: Pipeline) -> bytes:
    """Вся история публикаций — таблицей для Excel/Google Таблиц (UTF-8 с BOM, разделитель «;»)."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["№", "Когда", "Аккаунт", "Площадка", "Статус", "Ссылка на пост/аккаунт", "Оригинал",
                "Автор оригинала", "Подпись", "Ошибка"])
    for r in _history_rows(p):
        w.writerow([r["id"], p.local(r["published_at"] or r["slot_at"]), r["dest_name"],
                    PLATFORM_NAME.get(r["platform"], r["platform"]),
                    STATUS_LABELS.get(r["status"], r["status"]).split(" ", 1)[-1],
                    r["post_url"] or "", r["orig_url"], "@" + r["username"],
                    (r["caption"] or r["orig_caption"] or "").replace("\r", " "), r["error"] or ""])
    return ("﻿" + buf.getvalue()).encode("utf-8")


# --- настройки ------------------------------------------------------------------------------

def settings_view(p: Pipeline, apify_usage: tuple[float, float] | None = None) -> View:
    rows = destinations.all_rows(p.conn)
    lines = ["<b>⚙️ Настройки</b>", "", "<b>Куда публикую</b> — нажми на аккаунт, чтобы поменять время:"]
    if not rows:
        lines.append("пока никуда — подключи аккаунты в Postmypost и нажми «🔄 Подтянуть аккаунты».")
    for d in rows:
        state = _slots_text(d) if d["enabled"] else "выключен"
        lines.append(f"{PLATFORM_ICON.get(d['platform'], '🌐')} {e(d['name'])} — {state}")
    if apify_usage:
        lines += ["", f"Скачивание роликов в этом месяце: ${apify_usage[0]:.2f} из ${apify_usage[1]:.0f}"]
    buttons = [[(f"{PLATFORM_ICON.get(d['platform'], '🌐')} {d['name']}", f"acc:{d['id']}")] for d in rows]
    buttons += [[("🔄 Подтянуть аккаунты", "acc:sync")],
                [("📥 Автосбор с аккаунтов", "m:src"), ("⚠️ Ошибки", "m:errors")]]
    return "\n".join(lines), kb(buttons)


def account_card(p: Pipeline, dest_id: int) -> View:
    d = p.conn.execute("SELECT * FROM destinations WHERE id = ?", (dest_id,)).fetchone()
    if not d:
        return "Аккаунт не найден.", kb([[("◀️ Назад", "m:set")]])
    per_day = len(json.loads(d["slots"]))
    lines = [f"{PLATFORM_ICON.get(d['platform'], '🌐')} <b>{e(d['name'])}</b> · "
             f"{PLATFORM_NAME.get(d['platform'], d['platform'])}",
             (f"Публикует {_slots_text(d)} ({e(tz_label(p.cfg.timezone))}, ±{p.cfg.jitter_min} мин)"
              if d["enabled"] else "⏸ Выключен — ничего не публикует"),
             "", "Сколько постов в день?"]
    if _several_groups(p.conn):
        lines.insert(2, f"Группа: {e(group_label(d['route_group']))}")
    counts = [((f"·{n}·" if n == per_day else str(n)), f"acc:n:{dest_id}:{n}") for n in PER_DAY]
    rows = [counts, [("✏️ Своё время", f"acc:sx:{dest_id}"),
                     ("⏸ Выключить" if d["enabled"] else "▶️ Включить", f"acc:tog:{dest_id}")]]
    if _several_groups(p.conn):
        rows.append([("🏷 Группа", f"acc:grp:{dest_id}")])
    rows.append([("◀️ Назад", "m:set")])
    return "\n".join(lines), kb(rows)


def group_picker(p: Pipeline, dest_id: int) -> View:
    d = p.conn.execute("SELECT name, route_group FROM destinations WHERE id = ?", (dest_id,)).fetchone()
    groups = destinations.groups(p.conn)
    rows = [[(("✓ " if g == d["route_group"] else "") + group_label(g), f"acc:g:{dest_id}:{group_button(i, g)}")]
            for i, g in enumerate(groups)]
    rows += [[("✏️ Новая группа", f"acc:gx:{dest_id}")], [("◀️ Назад", f"acc:{dest_id}")]]
    return (f"<b>Группа для {e(d['name'])}</b>\nАккаунт получает ролики только своей группы.", kb(rows))


def sources_view(p: Pipeline) -> View:
    rows = sources.list_with_stats(p.conn, "profile")
    lines = ["<b>📥 Автосбор</b>", "Аккаунты, с которых я сам забираю новые рилсы раз в час. "
             "Это не обязательно — можно просто присылать ссылки."]
    for r in rows:
        lines.append(f"{'▶️' if r['enabled'] else '⏸'} @{e(r['username'])} — скачано {r['downloaded'] or 0}"
                     + ("" if r["archive_done"] else ", собираю архив"))
    buttons = [[("➕ Добавить аккаунт", "src:add")]]
    buttons += [[(f"{'⏸ Выключить' if r['enabled'] else '▶️ Включить'} @{r['username']}", f"src:tog:{r['id']}")]
                for r in rows]
    if rows:
        buttons.append([("📥 Проверить сейчас", "src:fetch")])
    buttons.append([("◀️ Назад", "m:set")])
    return "\n".join(lines), kb(buttons)


def errors_view(p: Pipeline) -> View:
    rows = p.conn.execute("SELECT text, created_at FROM outbox WHERE kind = 'error' "
                          "ORDER BY id DESC LIMIT 10").fetchall()
    if not rows:
        return "✅ Ошибок не было.", kb([[("◀️ Назад", "m:set")]])
    lines = ["<b>⚠️ Последние ошибки</b>"] + [f"• {when(p, r['created_at'])} — {e(r['text'][:250])}" for r in rows]
    return "\n".join(lines), kb([[("◀️ Назад", "m:set")]])


def help_view(p: Pipeline) -> View:
    return ("\n".join([
        "<b>Как это работает</b>",
        "1. Присылаешь ссылки на рилсы — одну, списком или .txt-файлом.",
        "2. Я скачиваю их и делаю под каждый аккаунт свою копию: 9:16, чуть другие цвета, скорость и звук.",
        "3. Подпись беру из оригинала без @упоминаний и ссылок, кодовое слово сохраняю.",
        "4. Публикую в ближайшее время постов, присланное — раньше всего остального.",
        "5. Если в подписи что-то непонятно — спрошу тебя.",
        "",
        "📊 Статус — что в очереди · 📜 История — что вышло · ⚙️ Настройки — аккаунты и время постов.",
    ]), None)


# --- подсказки для ввода -----------------------------------------------------------------

PROMPTS = {
    "add_source": "Пришли имя аккаунта или ссылку на профиль. Несколько — каждый с новой строки.",
    "links": "Пришли ссылки на рилсы — одну, списком или .txt-файлом.",
    "slots": "Напиши время через пробел, например: <code>9:00 14:00 20:30</code>",
    "group": "Напиши название группы, например <code>brandA</code>.",
    "edit_caption": "Пришли новый текст подписи — опубликую с ним.",
}


def prompt(kind: str) -> View:
    return PROMPTS[kind], kb([[("✖️ Отмена", "nav:cancel")]])


UNKNOWN_COMMAND = "Такой команды нет 🙂 Пользуйся меню внизу или просто пришли ссылки на рилсы."

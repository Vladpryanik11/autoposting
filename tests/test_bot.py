import asyncio
import dataclasses
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app import bot, db
from app import bot_views as v
from app.captions import Captioner
from app.pipeline import Pipeline

from helpers import Clock, FakePmp, add_dest, add_post, add_source, fake_render, pubs


# --- подделки Telegram -----------------------------------------------------------------

class FakeMessage:
    def __init__(self, text="", reply_to=None, document=None):
        self.text, self.caption, self.reply_to_message, self.document = text, None, reply_to, document
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw.get("reply_markup")))


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append(("text", chat_id, text, reply_markup))

    async def send_video(self, chat_id, fh, supports_streaming=True, **kw):
        self.sent.append(("video", chat_id, len(fh.read()), None))


def make(cfg, text="", reply_to=None, user=1, document=None, user_data=None):
    app = SimpleNamespace(bot_data={"cfg": cfg}, bot=FakeBot())
    msg = FakeMessage(text, reply_to, document)
    update = SimpleNamespace(effective_message=msg, message=msg, effective_user=SimpleNamespace(id=user),
                             callback_query=None)
    ctx = SimpleNamespace(application=app, args=[], user_data={} if user_data is None else user_data)
    return update, ctx, msg


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def bcfg(cfg):
    return dataclasses.replace(cfg, telegram_admin_ids=(1, 2), telegram_bot_token="123:ABC")


@pytest.fixture
def p(conn, bcfg):
    return Pipeline(conn, bcfg, captioner=Captioner(), render=fake_render, clock=Clock())


def buttons(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row] if markup else []


def fill(conn, cfg, caption="Ролик #reels"):
    s = add_source(conn)
    for i in range(12):
        add_post(conn, cfg, s, f"P{i}", caption=caption)
    return add_dest(conn, 11, name="promtlaba", start=None)


REVIEW_CAPTION = "Напиши в комментарии и я пришлю тебе гайд"


# --- экраны ------------------------------------------------------------------------------

def test_menu_is_three_buttons():
    assert [b for row in v.MENU for b in row] == ["📊 Статус", "📜 История", "⚙️ Настройки"]


def test_all_screens_render_on_empty_and_full_db(conn, bcfg, p):
    empty = [v.welcome(p), v.status_view(p), v.queue_view(p), v.history_view(p), v.settings_view(p),
             v.sources_view(p), v.errors_view(p), v.help_view(p), v.review_list_view(p)]
    assert all(isinstance(t, str) and t for t, _ in empty)
    d = fill(conn, bcfg)
    p.plan()
    p.prepare(limit=3)
    db.notify(conn, "error", "что-то <сломалось> & упало")
    pub_id = pubs(conn)[0]["id"]
    for text, markup in [v.welcome(p), v.status_view(p), v.queue_view(p), v.queue_view(p, 1), v.pub_card(p, pub_id),
                         v.cancel_confirm(p, pub_id), v.settings_view(p, (0.12, 5.0)), v.account_card(p, d),
                         v.group_picker(p, d), v.sources_view(p), v.errors_view(p), v.history_view(p),
                         v.links_confirm(p, [("AAA11", "https://www.instagram.com/reel/AAA11/")])]:
        assert text
        for label, data in buttons(markup):
            assert len(data.encode()) <= 64, data  # лимит Telegram на callback_data


def test_no_jargon_in_main_screens(conn, bcfg, p):
    fill(conn, bcfg)
    p.plan()
    p.prepare(limit=2)
    text = "\n".join(t for t, _ in (v.welcome(p), v.status_view(p), v.settings_view(p), v.queue_view(p)))
    for word in ("боев", "черновик", "слот", "Apify", "похожесть", "запас", "группа", "#pub"):
        assert word.lower() not in text.lower(), word


def test_welcome(conn, bcfg, p):
    text, markup = v.welcome(p)
    assert buttons(markup) == [("🔄 Подтянуть аккаунты", "acc:sync")]
    fill(conn, bcfg)
    text, markup = v.welcome(p)
    assert "promtlaba — 3 в день" in text and markup is None


def test_status_home(conn, bcfg, p):
    fill(conn, bcfg)
    text, _ = v.status_view(p)
    assert "▶️ <b>Работает</b>" in text and "🗓 В очереди: 0" in text
    p.plan()
    text, markup = v.status_view(p)
    assert "Ближайшие" in text and "сегодня 1" in text
    assert [d for _, d in buttons(markup)] == ["m:queue:0", "set:pause:1"]


def test_html_is_escaped(conn, bcfg, p):
    fill(conn, bcfg, caption="Скидка <50%> & подарок")
    p.plan()
    p.prepare(limit=1)
    assert "&lt;50%&gt; &amp; подарок" in v.pub_card(p, pubs(conn)[0]["id"])[0]


def test_plural():
    assert [v.plural(n, "ссылка", "ссылки", "ссылок") for n in (1, 2, 5, 11, 21, 22)] == \
        ["ссылка", "ссылки", "ссылок", "ссылок", "ссылка", "ссылки"]


# --- кнопки ------------------------------------------------------------------------------

def test_posts_per_day_and_toggle(conn, bcfg, p):
    d = fill(conn, bcfg)
    view, toast = bot.route(p, {}, f"acc:n:{d}:2")
    assert json.loads(conn.execute("SELECT slots FROM destinations").fetchone()[0]) == ["12:00", "19:00"]
    assert "2 в день" in toast and "·2·" in str(buttons(view[1]))
    assert tuple(conn.execute("SELECT kind, arg FROM commands").fetchone()) == ("unschedule", str(d))
    _, toast = bot.route(p, {}, f"acc:tog:{d}")
    assert conn.execute("SELECT enabled FROM destinations").fetchone()[0] == 0 and "Выключен" in toast


def test_group_button_only_when_several_groups(conn, bcfg, p):
    d = fill(conn, bcfg)
    assert "acc:grp" not in str(buttons(v.account_card(p, d)[1]))
    add_source(conn, "other", group="brandB")
    assert f"acc:grp:{d}" in str(buttons(v.account_card(p, d)[1]))


def test_pause_from_status(conn, bcfg, p):
    view, toast = bot.route(p, {}, "set:pause:1")
    assert db.get_kv(conn, "paused") == "1" and "На паузе" in view[0] and "▶️ Продолжить" in str(buttons(view[1]))


def test_review_and_cancel_buttons(conn, bcfg, p):
    fill(conn, bcfg, caption=REVIEW_CAPTION)
    p.plan()
    p.prepare(limit=2)
    first, second = pubs(conn)[:2]
    assert first["status"] == db.REVIEW
    card = v.pub_card(p, first["id"])
    assert [d for _, d in buttons(card[1])][:3] == [f"rv:1:{first['id']}", f"rv:0:{first['id']}",
                                                    f"pub:edit:{first['id']}"]
    view, toast = bot.route(p, {}, f"rv:1:{first['id']}")
    assert "одобрена" in toast and pubs(conn)[0]["status"] == db.READY
    bot.route(p, {}, f"pub:unok:{second['id']}")
    assert tuple(conn.execute("SELECT kind, arg FROM commands").fetchone()) == ("cancel", str(second["id"]))


def test_stale_or_unknown_button(p):
    assert "устарели" in bot.route(p, {}, "lnk:ok:0:1")[0][0]
    assert "устарела" in bot.route(p, {}, "zzz:1")[0][0]


def test_stale_group_button_does_not_assign_other_group(conn, bcfg, p):
    d = fill(conn, bcfg)
    add_source(conn, "gamma_src", group="gamma")
    gamma_btn = next(data for label, data in buttons(v.group_picker(p, d)[1]) if "gamma" in label)
    conn.execute("UPDATE sources SET route_group = 'zzz' WHERE username = 'gamma_src'")
    _, toast = bot.route(p, {}, gamma_btn)
    assert "изменился" in toast and conn.execute("SELECT route_group FROM destinations").fetchone()[0] == "default"


# --- ввод текста и ссылки -----------------------------------------------------------------------

@pytest.mark.parametrize("label, expected", [("📊 Статус", "Работает"), ("⚙️ Настройки", "Настройки"),
                                             ("📜 История", "Пока ничего"), ("📥 Источники", "Автосбор")])
def test_menu_labels_open_screens(conn, bcfg, label, expected):
    update, ctx, msg = make(bcfg, text=label)
    run(bot.on_text(update, ctx))
    assert expected in msg.replies[0][0]


def test_links_go_to_work_immediately_and_can_be_undone(conn, bcfg, p):
    ud = {}
    update, ctx, msg = make(bcfg, text="глянь https://www.instagram.com/reel/AbC12345/ и "
                                       "https://instagram.com/p/XyZ98765/?igsh=1", user_data=ud)
    run(bot.on_text(update, ctx))
    assert "Принято: 2 ссылки" in msg.replies[0][0]
    arg = json.loads(conn.execute("SELECT arg FROM commands WHERE kind = 'links'").fetchone()[0])
    assert [sc for sc, _ in arg["links"]] == ["AbC12345", "XyZ98765"] and arg["group"] == "default"
    undo = buttons(msg.replies[0][1])[0][1]
    assert "Отменено" in bot.route(p, ud, undo)[0][0]
    assert conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 0
    assert "Уже в работе" in bot.route(p, ud, undo)[1]


def test_links_with_several_groups_ask_where_and_accumulate(conn, bcfg, p):
    add_source(conn, "a", group="brandA")
    add_source(conn, "b", group="brandB")
    ud = {}
    for text in ("https://instagram.com/reel/AAAAA1/ https://instagram.com/reel/AAAAA2/",
                 "https://instagram.com/reel/BBBBB1/ https://instagram.com/reel/AAAAA1/"):
        update, ctx, msg = make(bcfg, text=text, user_data=ud)
        run(bot.on_text(update, ctx))
    assert "В какие аккаунты" in msg.replies[0][0] and "<b>3</b>" in msg.replies[0][0]
    to_b = next(d for label, d in buttons(msg.replies[0][1]) if "brandB" in label)
    bot.route(p, ud, to_b)
    arg = json.loads(conn.execute("SELECT arg FROM commands").fetchone()[0])
    assert arg["group"] == "brandB" and [sc for sc, _ in arg["links"]] == ["AAAAA1", "AAAAA2", "BBBBB1"]


def test_txt_file_with_links(conn, bcfg):
    class Doc:
        file_size = 100

        async def get_file(self):
            class F:
                async def download_as_bytearray(self):
                    return bytearray("https://instagram.com/reel/FILE1abc/\nhttps://instagram.com/reel/FILE2abc/"
                                     .encode())
            return F()

    update, ctx, msg = make(bcfg, document=Doc())
    run(bot.on_document(update, ctx))
    assert "Принято: 2" in msg.replies[0][0]
    assert len(json.loads(conn.execute("SELECT arg FROM commands").fetchone()[0])["links"]) == 2


def test_add_source_flow(conn, bcfg, p):
    ud = {}
    bot.route(p, ud, "src:add")
    update, ctx, msg = make(bcfg, text="https://instagram.com/Studio.Main brandA\n@second\nплохое имя!",
                            user_data=ud)
    run(bot.on_text(update, ctx))
    report = msg.replies[0][0]
    assert "✅ добавлен: @studio.main (группа brandA)" in report and "@second" in report
    assert "⚠️" in report and "await" not in ud


def test_custom_time_input(conn, bcfg):
    d = fill(conn, bcfg)
    ud = {"await": ("slots", d)}
    update, ctx, msg = make(bcfg, text="9:00, 13:30 21:00", user_data=ud)
    run(bot.on_text(update, ctx))
    assert json.loads(conn.execute("SELECT slots FROM destinations").fetchone()[0]) == ["09:00", "13:30", "21:00"]
    ud["await"] = ("slots", d)
    update, ctx, msg = make(bcfg, text="25:00", user_data=ud)
    run(bot.on_text(update, ctx))
    assert "Неверное время" in msg.replies[0][0] and ud["await"] == ("slots", d)  # ждём повторного ввода


def test_reply_to_review_edits_caption(conn, bcfg, p):
    fill(conn, bcfg, caption=REVIEW_CAPTION)
    p.plan()
    p.prepare(limit=1)
    pub = pubs(conn)[0]
    update, ctx, msg = make(bcfg, text="Пиши «ГАЙД» — пришлю", reply_to=FakeMessage(f"Публикация №{pub['id']}"))
    run(bot.on_text(update, ctx))
    assert "одобрена с новым текстом" in msg.replies[0][0]


def test_leaving_input_screen_by_button_cancels_waiting(conn, bcfg, p):
    """Ревью №1: «Изменить текст» → ушёл на другой экран → ссылка не должна стать подписью."""
    fill(conn, bcfg, caption=REVIEW_CAPTION)
    p.plan()
    p.prepare(limit=1)
    pub = pubs(conn)[0]
    ud = {}
    bot.route(p, ud, f"pub:edit:{pub['id']}")

    class Q:
        data = "m:status"

        async def answer(self, *a, **k):
            pass

        async def edit_message_text(self, *a, **k):
            pass

    update, ctx, msg = make(bcfg, user_data=ud)
    update.callback_query = Q()
    run(bot.on_callback(update, ctx))
    assert "await" not in ud
    update, ctx, msg = make(bcfg, text="https://instagram.com/reel/NEWREEL1/", user_data=ud)
    run(bot.on_text(update, ctx))
    assert pubs(conn)[0]["status"] == db.REVIEW and "Принято" in msg.replies[0][0]


def test_links_while_waiting_for_time_are_links(conn, bcfg):
    d = fill(conn, bcfg)
    ud = {"await": ("slots", d)}
    update, ctx, msg = make(bcfg, text="https://instagram.com/reel/NEWREEL1/", user_data=ud)
    run(bot.on_text(update, ctx))
    assert "Принято" in msg.replies[0][0] and "await" not in ud


def test_unknown_text_and_stranger(conn, bcfg):
    update, ctx, msg = make(bcfg, text="привет")
    run(bot.on_text(update, ctx))
    assert "Это не команда" in msg.replies[0][0]
    update, ctx, msg = make(bcfg, user=4242)
    run(bot.on_stranger(update, ctx))
    assert "4242" in msg.replies[0][0]


# --- история ------------------------------------------------------------------------------

def test_history_screen_and_csv(conn, bcfg):
    fill(conn, bcfg)
    clock = Clock()
    p = Pipeline(conn, dataclasses.replace(bcfg, postmypost_project_id=1), pmp=FakePmp(),
                 captioner=Captioner(), render=fake_render, clock=clock)
    p.plan(), p.prepare(limit=2), p.publish()
    a, b = pubs(conn)[:2]
    conn.execute("UPDATE publications SET status = ?, post_url = ? WHERE id = ?",
                 (db.PUBLISHED, "https://www.instagram.com/promtlaba", a["id"]))
    conn.execute("UPDATE publications SET status = ?, error = ? WHERE id = ?", (db.FAILED, "соцсеть не приняла", b["id"]))
    clock.t += timedelta(days=1)
    text, markup = v.history_view(p)
    assert "🟢" in text and "❌" in text and ">аккаунт<" in text and ">оригинал<" in text
    assert ("📄 Скачать таблицей", "hist:csv") in buttons(markup)
    data = v.history_csv(p).decode("utf-8-sig")
    header, *lines = data.strip().splitlines()
    assert header.startswith("№;Когда;Аккаунт") and len(lines) == 2 and "соцсеть не приняла" in data


# --- приложение и уведомления -------------------------------------------------------------

def test_long_screens_are_trimmed_by_lines():
    text = "\n".join(f"<b>строка {i}</b> " + "x" * 80 for i in range(100))
    out = bot.fit(text)
    assert len(out) < 4096 and out.endswith("(показано не всё)") and out.count("<b>") == out.count("</b>")


def test_build_app_with_and_without_admins(bcfg):
    assert bot.build_app(bcfg).handlers[0]
    assert bot.build_app(dataclasses.replace(bcfg, telegram_admin_ids=())).handlers[0]


def test_send_outbox_review_card_with_video_and_buttons(conn, bcfg, p):
    fill(conn, bcfg, caption=REVIEW_CAPTION)
    p.plan()
    p.prepare(limit=1)
    db.notify(conn, "info", "просто текст")
    _, ctx, _ = make(bcfg)
    app = ctx.application
    assert run(bot.send_outbox(app)) == 2
    assert [s[0] for s in app.bot.sent] == ["text", "video", "text", "video", "text", "text"]  # 2 админа
    review = app.bot.sent[0]
    assert "Нужна проверка" in review[2] and ("✅ Опубликовать", f"rv:1:{pubs(conn)[0]['id']}") in buttons(review[3])
    assert run(bot.send_outbox(app)) == 0


def test_outbox_retries_after_network_error(conn, bcfg):
    from telegram.error import TimedOut
    db.notify(conn, "info", "важное")
    _, ctx, _ = make(bcfg)
    calls = []

    async def flaky(chat_id, text, **kw):
        calls.append(chat_id)
        if len(calls) == 1:
            raise TimedOut()

    ctx.application.bot.send_message = flaky
    run(bot.send_outbox(ctx.application))
    assert conn.execute("SELECT sent_at FROM outbox").fetchone()[0] is None
    run(bot.send_outbox(ctx.application))
    assert conn.execute("SELECT sent_at FROM outbox").fetchone()[0] is not None

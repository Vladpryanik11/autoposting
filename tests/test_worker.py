import dataclasses
from datetime import timedelta

import pytest

from app import db
from app.captions import Captioner
from app.worker import Worker

from conftest import FakeApify
from helpers import Clock, FakePmp, add_dest, add_post, add_source, fake_render, pubs


@pytest.fixture
def setup(conn, cfg):
    cfg = dataclasses.replace(cfg, postmypost_project_id=777)
    s = add_source(conn)
    for i in range(6):
        add_post(conn, cfg, s, f"P{i}")
    d = add_dest(conn, 11)
    clock, pmp, apify = Clock(), FakePmp(), FakeApify(lambda inp: [])
    w = Worker(cfg, apify=apify, pmp=pmp, captioner=Captioner(), render=fake_render, clock=clock)
    return dataclasses.make_dataclass("S", ["w", "cfg", "clock", "pmp", "apify", "dest"])(w, cfg, clock, pmp, apify, d)


def test_tick_runs_whole_chain(conn, setup):
    setup.w.tick()
    rows = pubs(conn)
    assert len(rows) == 6
    assert [r["status"] for r in rows[:2]] == [db.SCHEDULED, db.SCHEDULED]  # 2 за тик
    setup.w.tick()
    assert [r["status"] for r in pubs(conn)[:4]] == [db.SCHEDULED] * 4


def test_pause_stops_new_work(conn, setup):
    db.set_kv(conn, "paused", "1")
    setup.w.tick()
    assert pubs(conn) == []


def test_ingest_runs_hourly_and_on_fetch_command(conn, setup):
    conn.execute("UPDATE sources SET last_scraped_at = ?", (db.now(),))
    setup.w.tick()
    assert len(setup.apify.starts) == 1
    setup.w.tick()
    assert len(setup.apify.starts) == 1  # час ещё не прошёл
    db.add_command(conn, "fetch")
    setup.w.tick()
    assert len(setup.apify.starts) == 2
    assert conn.execute("SELECT done_at FROM commands").fetchone()[0]
    setup.clock.t += timedelta(minutes=61)
    setup.w.tick()
    assert len(setup.apify.starts) == 3


def test_unschedule_command(conn, setup):
    setup.w.tick()
    db.set_kv(conn, "paused", "1")
    db.add_command(conn, "unschedule", str(setup.dest))
    setup.w.tick()
    assert {r["status"] for r in pubs(conn)} == {db.RELEASED}
    assert setup.pmp.deleted == [501, 502]
    assert "снято будущих публикаций — 6" in conn.execute(
        "SELECT text FROM outbox WHERE kind = 'info'").fetchone()[0]


def test_step_errors_are_reported_once_per_hour(conn, setup):
    def boom(*a, **k):
        raise RuntimeError("Postmypost лежит")

    setup.pmp.upload = boom
    for _ in range(3):
        setup.w.tick()
    errors = [e[0] for e in conn.execute(
        "SELECT text FROM outbox WHERE kind = 'error' AND text LIKE '%Postmypost лежит%'")]
    # каждая публикация пробует 3 раза, потом FAILED и ровно одно сообщение о ней; шаг целиком не падает
    first_two = pubs(conn)[:2]
    assert [r["status"] for r in first_two] == [db.FAILED, db.FAILED]
    assert [r["attempts"] for r in first_two] == [3, 3]
    assert len(errors) == 2 and all("не вышла" in e for e in errors)


def test_crashing_step_does_not_stop_others_and_is_deduplicated(conn, setup, monkeypatch):
    from app import pipeline

    monkeypatch.setattr(pipeline.Pipeline, "sync", lambda self, limit=20: 1 / 0)
    setup.w.tick()
    setup.clock.t += timedelta(minutes=16)
    setup.w.tick()
    msgs = conn.execute("SELECT text FROM outbox WHERE text LIKE '%проверка вышедших постов%'").fetchall()
    assert len(msgs) == 1 and "ZeroDivisionError" in msgs[0][0]
    assert pubs(conn)  # план и подготовка при этом работают
    setup.clock.t += timedelta(minutes=61)
    setup.w.tick()
    assert len(conn.execute("SELECT text FROM outbox WHERE text LIKE '%проверка вышедших постов%'").fetchall()) == 2


def test_daily_summary_once_per_day(conn, setup):
    setup.clock.t = setup.clock.t.replace(hour=18, minute=5)  # 21:05 по Москве
    setup.w.tick()
    setup.w.tick()
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE kind = 'summary'").fetchone()[0] == 1
    setup.clock.t += timedelta(days=1)
    setup.w.tick()
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE kind = 'summary'").fetchone()[0] == 2


def test_echo_outbox_without_bot(conn, setup):
    setup.w.echo_outbox = True
    db.notify(conn, "info", "привет")
    setup.w.tick()
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL").fetchone()[0] == 0


def test_drafts_off_recreates_future_drafts_as_real(conn, setup):
    db.set_kv(conn, "draft_mode", "1")
    setup.w.tick()
    assert [r["status"] for r in pubs(conn)[:2]] == [db.DRAFTED, db.DRAFTED]
    db.set_kv(conn, "draft_mode", "0")
    db.add_command(conn, "undraft")
    for _ in range(4):
        setup.w.tick()
    active = [r for r in pubs(conn) if r["status"] != db.RELEASED]
    assert setup.pmp.deleted == [501, 502]  # черновики удалены в Postmypost
    assert [r["status"] for r in active] == [db.SCHEDULED] * 6
    assert {p["status"] for p in setup.pmp.published[2:]} == {5}


def test_links_command_reports_result(conn, setup):
    import json as _json
    db.add_command(conn, "links", _json.dumps({"links": [["NOPE1", "https://www.instagram.com/reel/NOPE1/"]],
                                               "group": "default"}))
    setup.w.tick()
    text = conn.execute("SELECT text FROM outbox WHERE text LIKE '🔗%'").fetchone()[0]
    assert "не найдено 1" in text
    assert setup.apify.starts[-1]["input"]["username"] == ["https://www.instagram.com/reel/NOPE1/"]

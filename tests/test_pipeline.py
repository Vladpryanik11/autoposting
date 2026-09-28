import dataclasses
from datetime import timedelta

import pytest

from app import db
from app.captions import Captioner
from app.pipeline import Pipeline
from app.planner import from_iso

from helpers import Clock, FakePmp, add_dest, add_post, add_source, fake_render, pubs


@pytest.fixture
def env(conn, cfg):
    cfg = dataclasses.replace(cfg, postmypost_project_id=777)
    s = add_source(conn)
    for i in range(6):
        add_post(conn, cfg, s, f"P{i}", published=f"2026-09-{10 + i:02d}T10:00:00+00:00",
                 caption=f"Ролик {i} от @studio\n#reels")
    d = add_dest(conn, 11, slots=("10:00",))
    clock, fake = Clock(), FakePmp()
    p = Pipeline(conn, cfg, pmp=fake, captioner=Captioner(), render=fake_render, clock=clock)
    return dataclasses.make_dataclass("Env", ["p", "cfg", "clock", "pmp", "dest"])(p, cfg, clock, fake, d)


def outbox(conn, kind=None):
    q = "SELECT * FROM outbox" + (" WHERE kind = ?" if kind else "")
    return conn.execute(q, (kind,) if kind else ()).fetchall()


def test_full_flow_plan_prepare_publish_sync(conn, env):
    assert env.p.plan().created == 2  # 10:00 сегодня и завтра
    assert env.p.prepare() == 2
    first = pubs(conn)[0]
    assert first["status"] == db.READY and first["caption"] == "Ролик 5 от\n#reels"  # без @упоминания
    assert first["similarity"] == 0.85 and (env.cfg.data_dir / first["variant_path"]).exists()

    db.set_kv(conn, "draft_mode", "0")
    assert env.p.publish() == 2
    first = pubs(conn)[0]
    assert first["status"] == db.SCHEDULED and first["pmp_publication_id"] == 501
    assert first["variant_path"] is None and not list(env.cfg.variant_dir.glob("*.mp4"))  # копии удалены
    sent = env.pmp.published[0]
    assert sent["accounts"] == [11] and sent["status"] == 5 and sent["platform"] == "instagram"
    assert sent["post_at"] == from_iso(first["slot_at"])
    assert sent["title"] == "Ролик 5 от"

    env.pmp.statuses[501] = 1
    env.clock.t = from_iso(first["slot_at"]) + timedelta(minutes=10)
    assert env.p.sync() == 1
    assert pubs(conn)[0]["status"] == db.PUBLISHED


def test_publishes_for_real_by_default_drafts_only_on_request(conn, env):
    assert db.get_kv(conn, "draft_mode") == "0"
    db.set_kv(conn, "draft_mode", "1")  # режим проверки (только из CLI)
    env.p.plan(), env.p.prepare(), env.p.publish()
    assert {r["status"] for r in pubs(conn)} == {db.DRAFTED}
    assert {p["status"] for p in env.pmp.published} == {4}
    env.clock.t += timedelta(days=1)
    assert env.p.sync() == 0  # черновики не проверяются


def test_uncertain_cta_goes_to_review_and_can_be_edited(conn, env):
    conn.execute("UPDATE source_posts SET caption = 'Напиши в комментарии и я пришлю тебе гайд'")
    env.p.plan()
    env.p.prepare()
    pub = pubs(conn)[0]
    assert pub["status"] == db.REVIEW
    msg = outbox(conn, "review")[0]
    assert f"публикация №{pub['id']}" in msg["text"] and "кодовое слово не распознано" in msg["text"]
    assert env.p.publish() == 0  # без одобрения не отправляется

    assert "одобрена" in env.p.review(pub["id"], True, "Пиши «ГАЙД» в комментарии")
    pub = pubs(conn)[0]
    assert pub["status"] == db.READY and pub["caption"] == "Пиши «ГАЙД» в комментарии"
    assert "уже не ждёт" in env.p.review(pub["id"], False)


def test_rejected_publication_frees_slot_but_not_video(conn, env):
    db.set_kv(conn, "review_mode", "all")
    env.p.plan()
    env.p.prepare()
    first = pubs(conn)[0]
    env.p.review(first["id"], False)
    assert pubs(conn)[0]["status"] == db.REJECTED
    env.p.plan()
    replanned = [r for r in pubs(conn) if r["slot_nominal"] == first["slot_nominal"] and r["status"] == db.PLANNED]
    assert replanned and replanned[0]["shortcode"] != first["shortcode"]


def test_missing_source_file_fails_immediately(conn, env):
    env.p.plan()
    pub = pubs(conn)[0]
    path = env.cfg.data_dir / conn.execute("SELECT video_path FROM source_posts WHERE id = ?",
                                           (pub["source_post_id"],)).fetchone()[0]
    path.unlink()
    env.p.prepare()
    assert pubs(conn)[0]["status"] == db.FAILED
    assert "не найдено" in outbox(conn, "error")[0]["text"]


def test_render_error_retries_then_fails(conn, env):
    calls = []

    def broken(src, dst):
        calls.append(1)
        raise RuntimeError("ffmpeg упал")

    env.p.render = broken
    env.p.plan()
    for _ in range(3):
        env.p.prepare(limit=1)
    pub = pubs(conn)[0]
    assert pub["status"] == db.FAILED and pub["attempts"] == 3 and len(calls) == 3
    assert "ffmpeg упал" in outbox(conn, "error")[0]["text"]


def test_publish_retry_does_not_reupload_and_checks_for_duplicate(conn, env):
    db.set_kv(conn, "draft_mode", "0")
    env.p.plan()
    env.p.prepare(limit=1)
    env.pmp.fail_publish = 1
    assert env.p.publish() == 0
    pub = pubs(conn)[0]
    assert pub["status"] == db.READY and pub["pmp_file_id"] == 101 and pub["attempts"] == 1

    env.pmp.found = 999  # прошлая попытка всё-таки создала публикацию
    assert env.p.publish() == 1
    pub = pubs(conn)[0]
    assert env.pmp.uploads == ["1.mp4"] and env.pmp.find_calls == 1
    assert pub["pmp_publication_id"] == 999 and len(env.pmp.published) == 0


def test_missed_slots_are_released_and_video_returns_to_queue(conn, env):
    db.set_kv(conn, "review_mode", "all")
    env.p.plan()
    env.p.prepare()
    first = pubs(conn)[0]
    env.clock.t = from_iso(first["slot_at"])
    assert env.p.release_missed() == 1
    assert pubs(conn)[0]["status"] == db.RELEASED and not (env.cfg.data_dir / first["variant_path"]).exists()
    assert "не проверена" in outbox(conn, "info")[0]["text"]
    env.clock.t += timedelta(hours=1)
    env.p.plan()
    again = [r for r in pubs(conn) if r["shortcode"] == first["shortcode"] and r["status"] == db.PLANNED]
    assert again, "ролик из упущенного слота должен вернуться в очередь"


def test_sync_marks_errors_and_stuck(conn, env):
    db.set_kv(conn, "draft_mode", "0")
    env.p.plan(), env.p.prepare(), env.p.publish()
    a, b = pubs(conn)
    env.pmp.statuses[a["pmp_publication_id"]] = 3
    env.clock.t = from_iso(b["slot_at"]) + timedelta(hours=7)
    assert env.p.sync() == 2
    a, b = pubs(conn)
    assert a["status"] == db.FAILED and "не приняла" in a["error"]
    assert b["status"] == db.FAILED and "не вышла" in b["error"]
    assert len(outbox(conn, "error")) == 2


def test_unschedule_removes_future_posts_everywhere(conn, env):
    db.set_kv(conn, "draft_mode", "0")
    env.p.plan()
    env.p.prepare(limit=1)
    env.p.publish()
    assert [r["status"] for r in pubs(conn)] == [db.SCHEDULED, db.PLANNED]
    assert env.p.unschedule(env.dest) == 2
    assert {r["status"] for r in pubs(conn)} == {db.RELEASED}
    assert env.pmp.deleted == [501]


def test_publish_skipped_without_postmypost(conn, env):
    env.p.pmp = None
    env.p.plan(), env.p.prepare()
    assert env.p.publish() == 0 and {r["status"] for r in pubs(conn)} == {db.READY}


def test_status_and_summary_texts(conn, env):
    env.p.plan()
    text = env.p.status_text()
    assert "acc11" in text and "Ближайшие" in text
    assert "Сводка за сутки" in env.p.daily_summary()
    assert "acc11: в очереди 6" in text and "⚠️" not in text  # 2 запланировано + 4 свободных ролика
    conn.execute("UPDATE source_posts SET status = 'FAILED'")
    conn.execute("DELETE FROM publications")
    assert "⚠️ пусто" in "\n".join(env.p.queue_lines())


def test_history_link_from_postmypost_analytics(conn, env):
    db.set_kv(conn, "draft_mode", "0")
    env.p.plan(), env.p.prepare(), env.p.publish()
    first = pubs(conn)[0]
    slot = from_iso(first["slot_at"])
    env.pmp.statuses[first["pmp_publication_id"]] = 1
    env.pmp.published_posts = lambda project, account, d1, d2: [
        {"external_id": "999", "external_url": "https://www.instagram.com/acc11",
         "created_at": (slot + timedelta(hours=5)).isoformat(), "content": "чужой пост"},
        {"external_id": "123", "external_url": "https://www.instagram.com/acc11",
         "created_at": (slot + timedelta(minutes=2)).isoformat(), "content": first["caption"]},
    ]
    env.clock.t = slot + timedelta(minutes=10)
    env.p.sync()
    row = pubs(conn)[0]
    assert row["status"] == db.PUBLISHED and row["external_id"] == "123"
    assert row["post_url"] == "https://www.instagram.com/acc11" and row["published_at"]


def test_history_link_gives_up_after_several_tries(conn, env):
    db.set_kv(conn, "draft_mode", "0")
    env.p.plan(), env.p.prepare(), env.p.publish()
    first = pubs(conn)[0]
    env.pmp.statuses[first["pmp_publication_id"]] = 1
    calls = []
    env.pmp.published_posts = lambda *a: calls.append(1) or []
    env.clock.t = from_iso(first["slot_at"]) + timedelta(minutes=10)
    for _ in range(8):
        env.p.sync()
    assert len(calls) == 5 and pubs(conn)[0]["post_url"] is None

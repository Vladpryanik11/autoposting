import random
from datetime import timedelta

from app import db, planner
from app.planner import from_iso

from helpers import T0, add_dest, add_post, add_source, pubs


def run(conn, cfg, t=T0, seed=1):
    return planner.plan(conn, cfg, t, random.Random(seed))


def test_fills_slots_newest_first_with_jitter(conn, cfg):
    s = add_source(conn)
    for i in range(10):
        add_post(conn, cfg, s, f"P{i}", published=f"2026-09-{10 + i:02d}T10:00:00+00:00")
    d = add_dest(conn, 11)
    rep = run(conn, cfg)
    rows = pubs(conn, d)
    # окно 2 суток от 09:00 МСК: 10/15/20 сегодня и завтра = 6 слотов
    assert rep.created == 6 and len(rows) == 6
    assert [r["shortcode"] for r in rows] == ["P9", "P8", "P7", "P6", "P5", "P4"]
    for r in rows:
        shift = abs(from_iso(r["slot_at"]) - from_iso(r["slot_nominal"]))
        assert shift <= timedelta(minutes=cfg.jitter_min)
    assert from_iso(rows[0]["slot_nominal"]).astimezone(planner.ZoneInfo("Europe/Moscow")).hour == 10


def test_plan_is_idempotent(conn, cfg):
    s = add_source(conn)
    for i in range(10):
        add_post(conn, cfg, s, f"P{i}")
    add_dest(conn, 11)
    assert run(conn, cfg).created == 6
    assert run(conn, cfg, seed=2).created == 0


def test_no_start_date_or_future_start(conn, cfg):
    s = add_source(conn)
    for i in range(10):
        add_post(conn, cfg, s, f"P{i}")
    now_ = add_dest(conn, 11, start=None)  # без даты — публикует сразу
    later = add_dest(conn, 12, start="2026-10-02")
    run(conn, cfg)
    assert len(pubs(conn, now_)) == 6  # сегодня и завтра по 3 слота
    assert len(pubs(conn, later)) == 3 and all(from_iso(r["slot_nominal"]) >= from_iso("2026-10-02T00:00:00+03:00") for r in pubs(conn, later))


def test_same_video_in_other_accounts_is_spaced(conn, cfg):
    s = add_source(conn)
    for i in range(12):
        add_post(conn, cfg, s, f"P{i}", published=f"2026-09-{10 + i:02d}T10:00:00+00:00")
    add_dest(conn, 11)
    add_dest(conn, 12)
    add_dest(conn, 13)
    run(conn, cfg)
    by_post: dict[str, list] = {}
    for r in pubs(conn):
        by_post.setdefault(r["shortcode"], []).append(from_iso(r["slot_at"]))
    for times in by_post.values():
        times.sort()
        assert all(b - a >= timedelta(minutes=cfg.min_gap_same_video_min) for a, b in zip(times, times[1:]))


def test_alternates_sources_when_possible(conn, cfg):
    a, b = add_source(conn, "a"), add_source(conn, "b")
    for i in range(3):
        add_post(conn, cfg, a, f"A{i}", published=f"2026-09-2{i}T10:00:00+00:00")
    add_post(conn, cfg, b, "B0", published="2026-09-01T10:00:00+00:00")
    d = add_dest(conn, 11, slots=("10:00", "15:00", "20:00"))
    run(conn, cfg)
    names = {a: "a", b: "b"}
    first_day = [names[r["source_id"]] for r in pubs(conn, d)][:3]
    assert first_day == ["a", "b", "a"]


def test_groups_route_only_to_their_accounts(conn, cfg):
    a = add_source(conn, "brand_a", group="A")
    add_post(conn, cfg, a, "A1")
    add_dest(conn, 11, group="B")
    rep = run(conn, cfg)
    assert rep.created == 0 and rep.empty_slots == {"acc11": 6}


def test_youtube_skips_too_long_videos(conn, cfg):
    s = add_source(conn)
    add_post(conn, cfg, s, "LONG", duration=240, published="2026-09-30T10:00:00+00:00")
    add_post(conn, cfg, s, "SHORT", duration=50)
    d = add_dest(conn, 11, platform="youtube")
    run(conn, cfg)
    assert [r["shortcode"] for r in pubs(conn, d)] == ["SHORT"]


def test_released_post_can_be_planned_again_rejected_cannot(conn, cfg):
    s = add_source(conn)
    add_post(conn, cfg, s, "R")
    add_post(conn, cfg, s, "X")
    d = add_dest(conn, 11, slots=("10:00",))
    run(conn, cfg)
    first, second = pubs(conn, d)
    conn.execute("UPDATE publications SET status = ? WHERE id = ?", (db.RELEASED, first["id"]))
    conn.execute("UPDATE publications SET status = ? WHERE id = ?", (db.REJECTED, second["id"]))
    run(conn, cfg)
    active = [r["shortcode"] for r in pubs(conn, d) if r["status"] == db.PLANNED]
    assert active == [first["shortcode"]]


def test_reports_empty_slots_when_content_runs_out(conn, cfg):
    s = add_source(conn)
    add_post(conn, cfg, s, "ONLY")
    add_dest(conn, 11, name="sat")
    rep = run(conn, cfg)
    assert rep.created == 1 and rep.empty_slots == {"sat": 5}


def test_days_of_content(conn, cfg):
    s = add_source(conn)
    for i in range(9):
        add_post(conn, cfg, s, f"P{i}")
    d = add_dest(conn, 11)
    row = conn.execute("SELECT * FROM destinations WHERE id = ?", (d,)).fetchone()
    assert planner.days_of_content(conn, row) == 3.0


def test_linked_reels_go_first(conn, cfg):
    s = add_source(conn)
    add_post(conn, cfg, s, "NEW", published="2026-09-30T10:00:00+00:00")
    old = add_post(conn, cfg, s, "OLD_LINKED", published="2025-01-01T10:00:00+00:00")
    conn.execute("UPDATE source_posts SET priority = 1 WHERE id = ?", (old,))
    d = add_dest(conn, 11)
    run(conn, cfg)
    assert [r["shortcode"] for r in pubs(conn, d)][:2] == ["OLD_LINKED", "NEW"]


def test_linked_reels_all_go_before_regular_in_sent_order(conn, cfg):
    """Ревью №4: чередование источников не пропускает обычный ролик вперёд присланного."""
    stranger = add_source(conn, "stranger")
    studio = add_source(conn, "studio")
    for i in range(6):
        add_post(conn, cfg, studio, f"R{i}", published=f"2026-09-2{i}T10:00:00+00:00")
    linked = [add_post(conn, cfg, stranger, f"L{i}", published=f"2025-0{i + 1}-01T10:00:00+00:00")
              for i in range(3)]
    for post_id in linked:
        conn.execute("UPDATE source_posts SET priority = 1 WHERE id = ?", (post_id,))
    d = add_dest(conn, 11)
    run(conn, cfg)
    assert [r["shortcode"] for r in pubs(conn, d)][:4] == ["L0", "L1", "L2", "R5"]

import json
from datetime import datetime, timedelta, timezone

import pytest

from app import db, media, sources
from app.ingest import Ingestor

from conftest import FakeApify, reel


def status_of(conn, shortcode):
    return conn.execute("SELECT * FROM source_posts WHERE shortcode = ?", (shortcode,)).fetchone()


# --- аккаунты ---------------------------------------------------------------------

@pytest.mark.parametrize("value", ["studio.main", "@Studio.Main", "https://www.instagram.com/studio.main/",
                                   "instagram.com/studio.main?igsh=abc"])
def test_normalize_username(value):
    assert sources.normalize_username(value) == "studio.main"


@pytest.mark.parametrize("value", ["https://www.instagram.com/reel/ABC123/", "bad name!", ""])
def test_normalize_username_rejects_non_profiles(value):
    with pytest.raises(ValueError):
        sources.normalize_username(value)


def test_add_source_is_idempotent(conn):
    assert sources.add(conn, "@Studio") == ("studio", True)
    assert sources.add(conn, "studio") == ("studio", False)


# --- сбор -------------------------------------------------------------------------

def test_first_run_collects_archive_then_only_new_with_overlap(conn, cfg, cdn, clips):
    sources.add(conn, "studio")
    cdn.routes |= {"/r1.mp4": clips["a"], "/r2.mp4": clips["b"]}
    responses = [[reel("R1", "studio", cdn.url("/r1.mp4"))],
                 [reel("R1", "studio", cdn.url("/r1.mp4")), reel("R2", "studio", cdn.url("/r2.mp4"))]]
    apify = FakeApify(lambda inp: responses.pop(0))

    rep1 = Ingestor(conn, apify, cfg).run()
    assert (rep1.runs, rep1.new, rep1.downloaded) == (1, 1, 1)
    archive = apify.starts[0]["input"]
    assert archive["username"] == ["studio"] and archive["resultsLimit"] == cfg.apify_archive_limit
    assert "onlyPostsNewerThan" not in archive

    rep2 = Ingestor(conn, apify, cfg).run()
    incremental = apify.starts[1]["input"]
    assert incremental["resultsLimit"] == cfg.apify_incremental_limit
    since = datetime.fromisoformat(incremental["onlyPostsNewerThan"]).replace(tzinfo=timezone.utc)
    last = datetime.fromisoformat(conn.execute("SELECT started_at FROM apify_runs ORDER BY id LIMIT 1").fetchone()[0])
    assert last - since == timedelta(minutes=30)  # не сутки: каждый ролик оплачивается 1–2 раза, а не 24
    assert (rep2.runs, rep2.new, rep2.downloaded) == (1, 1, 1)  # R1 не скачан повторно
    assert cdn.hits.count("/r1.mp4") == 1

    post = status_of(conn, "R1")
    assert post["status"] == db.DOWNLOADED
    path = cfg.data_dir / post["video_path"]
    assert path == cfg.source_dir / "studio" / "R1.mp4" and path.exists()
    assert post["sha256"] and post["width"] == 180 and post["height"] == 320


def test_new_source_is_not_scraped_twice_in_one_run(conn, cfg):
    sources.add(conn, "studio")
    apify = FakeApify(lambda inp: [])
    Ingestor(conn, apify, cfg).run()
    assert len(apify.starts) == 1  # только архив, без лишнего платного запуска


def test_failed_archive_run_is_retried_next_time(conn, cfg):
    sources.add(conn, "studio")
    apify = FakeApify(lambda inp: [])
    apify.status = "FAILED"
    rep = Ingestor(conn, apify, cfg).run()
    assert rep.errors
    apify.status = "SUCCEEDED"
    Ingestor(conn, apify, cfg).run()
    assert [s["input"].get("onlyPostsNewerThan") for s in apify.starts] == [None, None]
    assert conn.execute("SELECT archive_done FROM sources").fetchone()[0] == 1


def test_unfinished_run_is_resumed_not_restarted(conn, cfg, cdn, clips):
    sources.add(conn, "studio")
    cdn.routes["/r1.mp4"] = clips["a"]
    apify = FakeApify(lambda inp: [reel("R1", "studio", cdn.url("/r1.mp4"))])
    # процесс упал после старта запуска: run записан, но не обработан
    run = apify.start_run("actor", {}, 1.0)
    conn.execute("INSERT INTO apify_runs (run_id, dataset_id, mode, meta, status, started_at) "
                 "VALUES (?, ?, 'archive', ?, 'RUNNING', ?)",
                 (run["id"], run["defaultDatasetId"], json.dumps({"source_ids": [1]}), db.now()))

    rep = Ingestor(conn, apify, cfg).run()
    assert len(apify.starts) == 1  # новый платный запуск не понадобился
    assert rep.downloaded == 1


def test_same_file_under_other_shortcode_is_duplicate(conn, cfg, cdn, clips):
    sources.add(conn, "studio")
    cdn.routes |= {"/x.mp4": clips["a"], "/y.mp4": clips["a"]}
    apify = FakeApify(lambda inp: [reel("X", "studio", cdn.url("/x.mp4")),
                                   reel("Y", "studio", cdn.url("/y.mp4"))])
    rep = Ingestor(conn, apify, cfg).run()
    assert (rep.downloaded, rep.duplicates) == (1, 1)
    dup = status_of(conn, "Y")
    assert dup["status"] == db.DUPLICATE and not (cfg.source_dir / "studio" / "Y.mp4").exists()


def test_separate_audio_track_is_muxed(conn, cfg, cdn, clips):
    sources.add(conn, "studio")
    cdn.routes |= {"/v.mp4": clips["mute"], "/a.m4a": clips["audio"]}
    apify = FakeApify(lambda inp: [reel("M", "studio", cdn.url("/v.mp4"), audioUrl=cdn.url("/a.m4a"))])
    Ingestor(conn, apify, cfg).run()
    post = status_of(conn, "M")
    assert post["status"] == db.DOWNLOADED
    assert media.probe(cfg.data_dir / post["video_path"])["has_audio"]
    assert not list((cfg.source_dir / "studio").glob("*.m4a"))


def test_expired_link_is_refreshed_by_reel_url(conn, cfg, cdn, clips):
    sources.add(conn, "studio")
    cdn.routes |= {"/old.mp4": 403, "/new.mp4": clips["a"]}

    def respond(inp):
        if inp["username"] == ["studio"]:
            return [reel("E", "studio", cdn.url("/old.mp4"))]
        assert inp["username"] == ["https://www.instagram.com/reel/E/"] and inp["resultsLimit"] == 1
        return [reel("E", "studio", cdn.url("/new.mp4"))]

    apify = FakeApify(respond)
    rep = Ingestor(conn, apify, cfg).run()
    assert (rep.refreshed, rep.downloaded) == (1, 1)
    assert status_of(conn, "E")["status"] == db.DOWNLOADED


def test_gives_up_after_max_attempts(conn, cfg, cdn):
    sources.add(conn, "studio")
    cdn.routes["/dead.mp4"] = 403
    apify = FakeApify(lambda inp: [reel("D", "studio", cdn.url("/dead.mp4"))])
    for _ in range(3):
        Ingestor(conn, apify, cfg).run()
    post = status_of(conn, "D")
    assert post["status"] == db.FAILED and post["attempts"] == 3
    starts = len(apify.starts)
    Ingestor(conn, apify, cfg).run()
    assert not any(s["input"]["resultsLimit"] == 1 for s in apify.starts[starts:])  # больше не платим


def test_network_error_retries_on_next_run_without_refresh(conn, cfg, cdn, clips):
    sources.add(conn, "studio")
    cdn.routes["/flaky.mp4"] = 500
    apify = FakeApify(lambda inp: [reel("F", "studio", cdn.url("/flaky.mp4"))] if inp.get("resultsLimit") != 1 else [])
    Ingestor(conn, apify, cfg).run()
    assert status_of(conn, "F")["status"] == db.DISCOVERED
    cdn.routes["/flaky.mp4"] = clips["a"]
    Ingestor(conn, apify, cfg).run()
    assert status_of(conn, "F")["status"] == db.DOWNLOADED


def test_items_without_video_or_with_error_are_skipped(conn, cfg):
    sources.add(conn, "studio")
    apify = FakeApify(lambda inp: [reel("P", "studio", None, type="Image"),
                                   {"error": "not_found", "errorDescription": "Post does not exist"}])
    rep = Ingestor(conn, apify, cfg).run()
    assert rep.skipped == 2 and rep.new == 0
    assert conn.execute("SELECT COUNT(*) FROM events WHERE event = 'item_error'").fetchone()[0] == 1


def test_reels_are_attributed_to_their_source_in_multi_source_run(conn, cfg, cdn, clips):
    for name in ("one", "two"):
        sources.add(conn, name)
    conn.execute("UPDATE sources SET archive_done = 1, last_scraped_at = ?", (db.now(),))
    cdn.routes |= {"/1.mp4": clips["a"], "/2.mp4": clips["b"]}
    apify = FakeApify(lambda inp: [reel("A1", "one", cdn.url("/1.mp4")),
                                   reel("B1", "stranger", cdn.url("/2.mp4"),
                                        inputUrl="https://www.instagram.com/two/")])
    Ingestor(conn, apify, cfg).run()
    owners = dict(conn.execute("SELECT p.shortcode, s.username FROM source_posts p "
                               "JOIN sources s ON s.id = p.source_id").fetchall())
    assert owners == {"A1": "one", "B1": "two"}


def test_cost_cap_is_passed_to_apify(conn, cfg):
    sources.add(conn, "studio")
    apify = FakeApify(lambda inp: [])
    Ingestor(conn, apify, cfg).run()
    assert 0 < apify.starts[0]["cap"] < 3.2  # 1000 роликов × $0.0026 + запас


# --- рилсы по ссылкам ---------------------------------------------------------------

def test_extract_reel_links():
    text = ("смотри https://www.instagram.com/reel/AbC123xyz/?igsh=MWx4 и\n"
            "instagram.com/p/ZZZ999aaa/ ещё m.instagram.com/reels/QqQ777wwww\n"
            "https://www.instagram.com/some.user/reel/UsR555ttt/ повтор https://instagram.com/reel/AbC123xyz\n"
            "профиль https://instagram.com/studio/ и тикток https://tiktok.com/@x/video/1")
    assert [sc for sc, _ in sources.extract_reel_links(text)] == ["AbC123xyz", "ZZZ999aaa", "QqQ777wwww", "UsR555ttt"]
    assert sources.extract_reel_links(text)[0][1] == "https://www.instagram.com/reel/AbC123xyz/"
    assert sources.extract_reel_links("просто текст") == []


def test_links_are_downloaded_prioritized_and_author_not_scraped(conn, cfg, cdn, clips):
    sources.add(conn, "studio")
    cdn.routes |= {"/l1.mp4": clips["a"], "/l2.mp4": clips["b"]}
    apify = FakeApify(lambda inp: [reel("L1", "stranger", cdn.url("/l1.mp4")),
                                   reel("L2", "studio", cdn.url("/l2.mp4"))]
                      if inp["resultsLimit"] == 1 else [])
    links = sources.extract_reel_links("https://instagram.com/reel/L1/ https://instagram.com/reel/L2/ "
                                       "https://instagram.com/reel/GONE1/")
    rep = Ingestor(conn, apify, cfg).add_links(links, group="brandA")
    assert (rep.new, rep.downloaded, rep.not_found) == (2, 2, 1)
    assert apify.starts[0]["input"]["username"] == [u for _, u in links]
    stranger = conn.execute("SELECT * FROM sources WHERE username = 'stranger'").fetchone()
    assert (stranger["kind"], stranger["enabled"], stranger["route_group"]) == ("links", 0, "brandA")
    l2 = conn.execute("SELECT p.priority, s.username FROM source_posts p JOIN sources s ON s.id = p.source_id "
                      "WHERE shortcode = 'L2'").fetchone()
    assert tuple(l2) == (1, "studio")  # автор уже был источником — ролик приписан ему

    starts = len(apify.starts)
    Ingestor(conn, apify, cfg).run()
    usernames = [u for s in apify.starts[starts:] for u in s["input"]["username"]]
    assert "stranger" not in usernames  # автора присланного рилса целиком не собираем


def test_known_links_are_bumped_without_paying(conn, cfg, cdn, clips):
    sources.add(conn, "studio")
    cdn.routes["/k.mp4"] = clips["a"]
    apify = FakeApify(lambda inp: [reel("K1", "studio", cdn.url("/k.mp4"))] if inp["resultsLimit"] != 1 else [])
    Ingestor(conn, apify, cfg).run()
    starts = len(apify.starts)
    rep = Ingestor(conn, apify, cfg).add_links([("K1", "https://www.instagram.com/reel/K1/")])
    assert rep.known == 1 and len(apify.starts) == starts
    assert status_of(conn, "K1")["priority"] == 1


def test_adding_link_author_as_source_turns_on_full_collection(conn, cfg):
    sources.links_source(conn, "stranger", "default")
    assert sources.add(conn, "stranger") == ("stranger", True)
    row = conn.execute("SELECT kind, enabled, archive_done FROM sources WHERE username = 'stranger'").fetchone()
    assert tuple(row) == ("profile", 1, 0)


def test_failed_reel_sent_as_link_gets_another_chance(conn, cfg, cdn, clips):
    """Ревью №3: рилс, который раньше не скачался, по ссылке скачивается заново."""
    sources.add(conn, "studio")
    cdn.routes |= {"/dead.mp4": 403, "/alive.mp4": clips["a"]}
    apify = FakeApify(lambda inp: [reel("F1", "studio", cdn.url("/dead.mp4"))] if inp["resultsLimit"] != 1
                      else [reel("F1", "studio", cdn.url("/alive.mp4"))])
    conn.execute("INSERT INTO source_posts (source_id, shortcode, url, status, attempts, created_at, updated_at) "
                 "VALUES (1, 'F1', 'https://www.instagram.com/reel/F1/', 'FAILED', 3, ?, ?)", (db.now(), db.now()))
    rep = Ingestor(conn, apify, cfg).add_links([("F1", "https://www.instagram.com/reel/F1/")])
    assert rep.known == 0 and rep.downloaded == 1
    row = status_of(conn, "F1")
    assert (row["status"], row["priority"]) == (db.DOWNLOADED, 1)


def test_duplicate_link_bumps_original(conn, cfg, cdn, clips):
    sources.add(conn, "studio")
    cdn.routes |= {"/x.mp4": clips["a"], "/y.mp4": clips["a"]}
    apify = FakeApify(lambda inp: [reel("X", "studio", cdn.url("/x.mp4")), reel("Y", "studio", cdn.url("/y.mp4"))])
    Ingestor(conn, apify, cfg).run()
    assert status_of(conn, "Y")["status"] == db.DUPLICATE
    Ingestor(conn, apify, cfg).add_links([("Y", "https://www.instagram.com/reel/Y/")])
    assert status_of(conn, "X")["priority"] == 1

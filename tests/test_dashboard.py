from coffee_kb import safety
from coffee_kb.clock import iso
from coffee_kb.dashboard import build_dashboard, render_html
from coffee_kb.manifest import Manifest


def _list(env, ids):
    with env.manifest() as m:
        for rank, vid in enumerate(ids):
            m.upsert_listed_item(
                "youtube", vid, iso(env.clock.now()), item_type="video", listing_rank=rank
            )


def test_dashboard_shows_cooldown_as_critical_and_escapes(cfg, data_paths, clock):
    safety.write_cooldown(data_paths, "<script>alert(1)</script> HTTP 429", clock.now())
    with Manifest(data_paths.manifest) as m:
        data = build_dashboard(
            cfg, data_paths, m, clock.now(), {"collector": {}, "log": ["line <b>"]}
        )
    assert data["health"]["level"] == "critical"
    page = render_html(data)
    assert "Cooldown latch active" in page
    assert "<script>alert" not in page and "&lt;b&gt;" in page


def test_dashboard_reports_run_in_progress(cfg, data_paths, clock):
    import json

    with Manifest(data_paths.manifest) as m:
        rid = m.start_run("fetch", {}, iso(clock.now()))
        m.record_attempt_start("youtube", "a", rid, iso(clock.now()))
        data_paths.lock.write_text(json.dumps({"command": "fetch", "started_at": iso(clock.now())}))
        data = build_dashboard(cfg, data_paths, m, clock.now(), {})
    assert data["running"] == {"command": "fetch", "since": iso(clock.now()), "videos_done": 1}
    assert "Run in progress" in render_html(data)


def test_videos_collected_section(env, capsys):
    from fakes import FakeHTTPError

    from coffee_kb.config import Config
    from coffee_kb.dashboard import collected_videos

    ids = ["abcDEF12345", "broken00001", "notyet00001"]
    with env.manifest() as m:
        for rank, vid in enumerate(ids):
            m.upsert_listed_item(
                "youtube", vid, iso(env.clock.now()), item_type="video", listing_rank=rank
            )
    env.yt.add_video(ids[0])
    env.yt.add_video(ids[1], manual=FakeHTTPError(404, "Not Found"))
    env.run("fetch", "--limit", "2")
    with Manifest(env.paths.manifest) as m:
        cfg = Config(data_dir=env.paths.root)
        vids = collected_videos(cfg, env.paths, m)
        data = build_dashboard(cfg, env.paths, m, env.clock.now(), {})
    assert {v["item_id"] for v in vids} == {"abcDEF12345", "broken00001"}  # not the unfetched one
    by_id = {v["item_id"]: v for v in vids}
    assert by_id["abcDEF12345"]["complete"] and by_id["abcDEF12345"]["manual"]
    assert by_id["abcDEF12345"]["sponsor_segments"] == 2
    assert not by_id["broken00001"]["complete"]
    assert by_id["broken00001"]["problems"] == ["captions_manual"]
    page = render_html(data)
    assert "Videos collected" in page and "2 fetched: 1 complete, 1 partial" in page
    assert "https://www.youtube.com/watch?v=abcDEF12345" in page
    assert "Grind Size Explained (fixture)" in page and "2:30" in page
    assert "partial: captions_manual" in page and "notyet00001" not in page

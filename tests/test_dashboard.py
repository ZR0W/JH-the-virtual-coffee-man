from fakes import FakeHTTPError

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


def test_dashboard_command_writes_html(env, capsys):
    ids = ["abcDEF12345", "broken00001", "later000001"]
    _list(env, ids)
    env.yt.add_video(ids[0])
    env.yt.add_video(ids[1], manual=FakeHTTPError(404, "Not Found"))
    env.run("fetch", "--limit", "2")
    capsys.readouterr()
    calls = env.yt.network_calls()
    assert env.run("dashboard") == 0
    out = env.paths.root / "dashboard.html"
    assert capsys.readouterr().out.strip() == str(out)
    page = out.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>")
    assert "Videos collected" in page and "1 <span" in page  # 1 complete (the other is partial)
    assert "Fetch attempts per day" in page and "<svg" in page and "daily cap 30" in page
    assert "broken00001" in page  # errors listed
    assert "Healthy, with item errors" in page
    assert "http://" not in page and "https://" not in page.split("<title>")[0]  # no external loads
    assert env.yt.network_calls() == calls  # dashboard made none


def test_dashboard_shows_cooldown_as_critical_and_escapes(cfg, data_paths, clock):
    safety.write_cooldown(data_paths, "<script>alert(1)</script> HTTP 429", clock.now())
    with Manifest(data_paths.manifest) as m:
        data = build_dashboard(
            cfg, data_paths, m, clock.now(), {"timer": {}, "journal": "line <b>"}
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

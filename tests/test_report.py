import json

from fakes import FakeHTTPError, fixture_info

from coffee_kb import safety
from coffee_kb.clock import iso

VID = "abcDEF12345"


def _list(env, ids):
    with env.manifest() as m:
        for rank, vid in enumerate(ids):
            m.upsert_listed_item(
                "youtube", vid, iso(env.clock.now()), item_type="video", listing_rank=rank
            )


def test_report_json_after_fetch_and_normalize(env, capsys):
    ids = [VID, "autoOnly001", "noCaps00001", "broken00001", "members0001"]
    _list(env, ids + [f"later{i:06d}" for i in range(40)])
    env.yt.add_video(VID)
    env.yt.add_video(ids[1], fixture_info(ids[1], subtitles={}))
    env.yt.add_video(ids[2], fixture_info(ids[2], subtitles={}, automatic_captions={}))
    env.yt.add_video(ids[3], manual=FakeHTTPError(404, "Not Found"))
    env.yt.add_video(ids[4], fixture_info(ids[4], availability="subscriber_only"))
    env.run("fetch", "--limit", "5")
    env.run("normalize", "--video", VID)  # leave the others stale on purpose
    capsys.readouterr()
    assert env.run("report", "--format", "json") == 0
    rep = json.loads(capsys.readouterr().out)

    assert rep["items"]["total"] == 45
    assert rep["items"]["fetched"] == 4
    assert rep["items"]["by_availability"]["skipped_restricted"] == 1
    assert rep["captions"] == {"manual+auto": 1, "auto_only": 2, "manual_only": 0, "none": 1}
    assert rep["sponsorblock"]["with_segments"] == 4
    assert rep["totals"]["hours_fetched"] == round(4 * 150 / 3600, 2)
    assert rep["totals"]["transcript_words"] > 0
    assert rep["errors"]["captions_manual"][0]["item_id"] == "broken00001"
    assert "404" in rep["errors"]["captions_manual"][0]["detail"]
    assert set(rep["stale_normalization"]) == {"autoOnly001", "noCaps00001", "broken00001"}
    st = rep["safety"]
    assert st["cooldown"] is None
    assert st["fetches_last_24h"] == 5 and st["remaining_allowance"] == 25
    # 40 never fetched + 1 with a caption error; the restricted one is not counted.
    assert st["remaining_to_fetch"] == 41 and st["estimated_days_remaining"] == 2
    assert rep["last_runs"]["fetch"]["exit_code"] == 0


def test_report_text_shows_cooldown(env, capsys):
    safety.write_cooldown(env.paths, "HTTP 429 on captions", env.clock.now(), item_id=VID)
    assert env.run("report") == 0
    out = capsys.readouterr().out
    assert "COOLDOWN LATCH ACTIVE" in out and "HTTP 429 on captions" in out
    assert "2026-10-16T09:30:00Z" in out
    assert env.yt.network_calls() == 0


def test_report_on_empty_data_dir(env, capsys):
    assert env.run("report") == 0
    out = capsys.readouterr().out
    assert "Items: 0 in manifest" in out and "Errors: none" in out

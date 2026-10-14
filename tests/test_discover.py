import json

from fakes import FakeHTTPError, fixture_bytes

from coffee_kb import safety
from coffee_kb.paths import list_snapshots

CHANNEL = "https://www.youtube.com/@jameshoffmann"


def _listing(*ids, **extra):
    return {"_type": "playlist", "entries": [{"id": i, "title": f"T {i}", **extra} for i in ids]}


def _setup_tabs(env, videos=None, shorts=None, streams=None):
    env.yt.infos[f"{CHANNEL}/videos"] = (
        json.loads(fixture_bytes("channel_videos.json")) if videos is None else videos
    )
    env.yt.infos[f"{CHANNEL}/shorts"] = _listing("shortAAAAAA") if shorts is None else shorts
    env.yt.infos[f"{CHANNEL}/streams"] = _listing("streamBBBBB") if streams is None else streams


def test_discover_populates_manifest_and_raw_snapshot(env):
    _setup_tabs(env)
    assert env.run("discover") == 0
    with env.manifest() as m:
        rows = {r["item_id"]: r for r in m.items("youtube")}
    assert set(rows) == {"abcDEF12345", "ghiJKL67890", "mnoPQR13579", "shortAAAAAA", "streamBBBBB"}
    assert rows["abcDEF12345"]["item_type"] == "video"
    assert rows["abcDEF12345"]["listing_rank"] == 0 and rows["ghiJKL67890"]["listing_rank"] == 1
    assert rows["abcDEF12345"]["duration_s"] == 150
    assert rows["mnoPQR13579"]["availability"] == "skipped_restricted"
    assert rows["shortAAAAAA"]["item_type"] == "short"
    assert rows["streamBBBBB"]["item_type"] == "stream"
    # One flat session for all three tabs, paced with a request interval from the 5–10 s range.
    assert len(env.yt.sessions) == 1 and env.yt.sessions[0].flat
    assert 5 <= env.yt.sessions[0].interval <= 10
    snaps = list_snapshots(env.paths.channel_snapshots_dir("youtube"))
    assert len(snaps) == 1
    assert {p.name for p in snaps[0].iterdir()} == {
        "videos.json",
        "shorts.json",
        "streams.json",
        "fetch.json",
    }
    rec = json.loads((snaps[0] / "fetch.json").read_text(encoding="utf-8"))
    assert rec["kind"] == "channel_listing" and rec["yt_dlp_version"] == env.yt.version
    assert not any(p.name.startswith(".tmp-") for p in snaps[0].parent.iterdir())


def test_discover_is_throttled_to_weekly(env):
    _setup_tabs(env)
    assert env.run("discover") == 0
    calls = env.yt.network_calls()
    env.clock.advance(days=6)
    assert env.run("discover") == 0
    assert env.yt.network_calls() == calls  # throttled: no requests
    assert env.run("discover", "--force") == 0
    assert env.yt.network_calls() == calls + 3
    env.clock.advance(days=7, seconds=1)
    assert env.run("discover") == 0
    assert env.yt.network_calls() == calls + 6


def test_vanished_items_marked_missing_not_deleted(env):
    _setup_tabs(env)
    env.run("discover")
    env.clock.advance(days=8)
    _setup_tabs(env, videos=_listing("abcDEF12345"))
    assert env.run("discover") == 0
    with env.manifest() as m:
        assert m.get_item("youtube", "ghiJKL67890")["listing_status"] == "missing_from_listing"
        assert m.get_item("youtube", "abcDEF12345")["listing_status"] == "listed"


def test_missing_tab_is_empty_not_error(env):
    _setup_tabs(env)
    env.yt.infos[f"{CHANNEL}/streams"] = RuntimeError(
        "ERROR: [youtube:tab] @jameshoffmann: This channel does not have a streams tab"
    )
    assert env.run("discover") == 0


def test_failed_tab_does_not_mark_missing(env):
    _setup_tabs(env)
    env.run("discover")
    env.clock.advance(days=8)
    _setup_tabs(env, videos=RuntimeError("weird extractor failure"))
    assert env.run("discover") == 1
    with env.manifest() as m:
        assert m.get_item("youtube", "ghiJKL67890")["listing_status"] == "listed"
        # A failed discover doesn't reset the weekly throttle.
        assert m.last_run("discover")["exit_code"] == 1


def test_discover_block_writes_latch_and_stops(env):
    _setup_tabs(env)
    env.yt.infos[f"{CHANNEL}/videos"] = FakeHTTPError(429, "Too Many Requests")
    assert env.run("discover") == safety.EXIT_TEMPFAIL
    latch = safety.read_cooldown(env.paths)
    assert latch is not None and "429" in latch.reason and latch.command == "discover"
    # Stopped at the first tab.
    assert env.yt.network_calls() == 1
    # No partial snapshot left behind.
    assert list_snapshots(env.paths.channel_snapshots_dir("youtube")) == []


def test_discover_block_from_warning(env):
    _setup_tabs(env)
    env.yt.warnings_for[f"{CHANNEL}/videos"] = ["[youtube] Sign in to confirm you're not a bot"]
    assert env.run("discover") == safety.EXIT_TEMPFAIL
    assert env.yt.network_calls() == 1


def test_discover_refuses_during_cooldown_even_with_force(env, capsys):
    _setup_tabs(env)
    safety.write_cooldown(env.paths, "HTTP 429", env.clock.now())
    env.clock.advance(hours=100)  # even long after: only clear-cooldown removes it
    assert env.run("discover") == safety.EXIT_TEMPFAIL
    assert env.run("discover", "--force") == safety.EXIT_TEMPFAIL
    assert env.yt.network_calls() == 0
    assert "cooldown latch present" in capsys.readouterr().err


def test_transient_tab_error_is_retried_after_60s(env):
    _setup_tabs(env)
    state = {"n": 0}
    listing = _listing("abcDEF12345")

    def flaky():
        state["n"] += 1
        if state["n"] == 1:
            raise ConnectionResetError("Connection reset by peer")
        return listing

    env.yt.infos[f"{CHANNEL}/videos"] = flaky
    assert env.run("discover") == 0
    assert 60 in env.clock.sleeps
    assert state["n"] == 2

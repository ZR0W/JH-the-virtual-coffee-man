import json
from datetime import timedelta

import httpx
import pytest
from fakes import FakeHTTPError, fixture_bytes, fixture_info

from coffee_kb import safety
from coffee_kb.clock import iso
from coffee_kb.paths import latest_snapshot, list_snapshots
from coffee_kb.sources.youtube.client import build_ydl_params
from coffee_kb.sources.youtube.fetch import choose_track

VID = "abcDEF12345"
WATCH = "https://www.youtube.com/watch?v={}"


def _ids(n, prefix="vid"):
    return [f"{prefix}{i:08d}" for i in range(n)]


def _list(env, ids):
    """Put items in the manifest as discover would (newest first by rank)."""
    with env.manifest() as m:
        for rank, vid in enumerate(ids):
            m.upsert_listed_item(
                "youtube", vid, iso(env.clock.now()), item_type="video", listing_rank=rank
            )


def _video_calls(env):
    return [c for c in env.yt.calls if c[0] == "extract_info"]


# --- happy path ---------------------------------------------------------------------------


def test_fetch_one_video_writes_complete_snapshot(env):
    env.yt.add_video(VID)
    assert env.run("fetch", "--video", VID) == 0
    snap = latest_snapshot(env.paths.snapshots_dir("youtube", VID))
    assert snap is not None and not snap.name.startswith(".tmp-")
    files = sorted(
        str(p.relative_to(snap)).replace("\\", "/") for p in snap.rglob("*") if p.is_file()
    )
    assert files == [
        "captions/en-orig.auto.json3",
        "captions/en.manual.json3",
        "fetch.json",
        "info.json",
        "sponsorblock.json",
    ]
    assert json.loads((snap / "info.json").read_text(encoding="utf-8"))["id"] == VID
    assert (snap / "sponsorblock.json").read_bytes() == fixture_bytes("sponsorblock.json")
    rec = json.loads((snap / "fetch.json").read_text(encoding="utf-8"))
    assert rec["item_id"] == VID and rec["yt_dlp_version"] == env.yt.version
    assert rec["coffee_kb_version"] and rec["fetched_at"] == "2026-10-14T09:30:00Z"
    assert rec["components"]["metadata"]["status"] == "ok"
    assert rec["components"]["captions_manual"] == {
        "status": "ok",
        "file": "captions/en.manual.json3",
        "lang": "en",
        "format": "json3",
    }
    assert rec["components"]["sponsorblock"]["http_status"] == 200
    assert rec["components"]["sponsorblock"]["segments"] == 2
    assert rec["block_signal"] is None
    assert 5 <= rec["pacing"]["request_interval_s"] <= 10

    with env.manifest() as m:
        item = m.get_item("youtube", VID)
        assert item["latest_snapshot"] == env.paths.rel(snap)
        assert item["title"] == "Grind Size Explained (fixture)"
        assert item["upload_date"] == "2026-10-01" and item["duration_s"] == 150
        assert item["availability"] == "public"
        assert item["listing_status"] == "not_yet_listed"
        comps = m.components("youtube", VID)
        assert {k: v["status"] for k, v in comps.items()} == {
            "metadata": "ok",
            "captions_manual": "ok",
            "captions_auto": "ok",
            "sponsorblock": "ok",
        }
        assert m.attempts("youtube")[0]["outcome"] == "ok"
    # SponsorBlock queried once with the configured categories.
    assert len(env.sb_requests) == 1
    q = env.sb_requests[0].url.params
    assert q["videoID"] == VID and "music_offtopic" in json.loads(q["categories"])


def test_pacing_between_requests_subtitles_and_videos(env):
    ids = _ids(3)
    _list(env, ids)
    for v in ids:
        env.yt.add_video(v)
    assert env.run("fetch") == 0
    sleeps = env.clock.sleeps
    # Per video: 10 s before each of 2 caption downloads; 60–120 s between videos.
    assert sleeps.count(10) == 6
    between = [s for s in sleeps if s != 10]
    assert len(between) == 2 and all(60 <= s <= 120 for s in between)
    # Request interval for yt-dlp drawn per video from [5, 10].
    assert all(5 <= s.interval <= 10 for s in env.yt.sessions)
    assert len(env.yt.sessions) == 3


def test_pacing_also_applies_across_runs(env):
    env.yt.add_video("aaaaaaaaaaa")
    env.yt.add_video("bbbbbbbbbbb")
    env.run("fetch", "--video", "aaaaaaaaaaa")
    env.clock.sleeps.clear()
    env.run("fetch", "--video", "bbbbbbbbbbb")
    first = env.clock.sleeps[0]
    assert 30 < first <= 120  # waited out the between-video delay since the last attempt


def test_rerun_does_no_network_for_complete_videos(env):
    ids = _ids(3)
    _list(env, ids)
    for v in ids:
        env.yt.add_video(v)
    assert env.run("fetch") == 0
    calls = env.yt.network_calls()
    sb = len(env.sb_requests)
    assert env.run("fetch") == 0
    assert env.run("fetch", "--video", ids[0]) == 0
    assert env.yt.network_calls() == calls and len(env.sb_requests) == sb
    with env.manifest() as m:
        assert len(m.attempts("youtube")) == 3  # skips don't count against the cap


def test_refresh_creates_new_snapshot_and_keeps_old(env):
    env.yt.add_video(VID)
    env.run("fetch", "--video", VID)
    env.clock.advance(hours=1)
    env.run("fetch", "--video", VID, "--refresh")
    snaps = list_snapshots(env.paths.snapshots_dir("youtube", VID))
    assert [s.name for s in snaps] == ["20261014T093000Z", snaps[1].name]
    assert snaps[1].name > snaps[0].name
    with env.manifest() as m:
        assert m.get_item("youtube", VID)["latest_snapshot"].endswith(snaps[1].name)


def test_order_is_newest_first_and_limit(env):
    ids = _ids(5)
    _list(env, ids)
    for v in ids:
        env.yt.add_video(v)
    assert env.run("fetch", "--limit", "2") == 0
    assert [c[1] for c in _video_calls(env)] == [WATCH.format(ids[0]), WATCH.format(ids[1])]


def test_dry_run_makes_no_requests(env, capsys):
    ids = _ids(3)
    _list(env, ids)
    assert env.run("fetch", "--dry-run") == 0
    assert env.yt.network_calls() == 0
    out = capsys.readouterr().out
    assert out.count("would fetch") == 3
    with env.manifest() as m:
        assert m.attempts("youtube") == []


def test_fetch_without_discover_says_so(env, capsys):
    assert env.run("fetch") == 0
    assert "run `coffee-kb discover` first" in capsys.readouterr().err
    assert env.yt.network_calls() == 0


def test_video_url_is_accepted(env):
    env.yt.add_video(VID)
    assert env.run("fetch", "--video", f"https://www.youtube.com/watch?v={VID}&t=30s") == 0
    with pytest.raises(SystemExit) as exc:
        env.run("fetch", "--video", "../../etc")
    assert exc.value.code == 2


# --- daily cap ----------------------------------------------------------------------------


def test_daily_cap_stops_at_30(env, capsys):
    ids = _ids(40)
    _list(env, ids)
    for v in ids:
        env.yt.add_video(v)
    assert env.run("fetch") == 0
    assert len(_video_calls(env)) == 30
    with env.manifest() as m:
        assert len(m.attempts("youtube")) == 30
    # A second run inside the window does nothing at all.
    env.yt.calls.clear()
    assert env.run("fetch") == 0
    assert env.yt.network_calls() == 0
    assert "daily cap reached" in capsys.readouterr().err
    # --limit can't exceed the remaining allowance either.
    assert env.run("fetch", "--limit", "50") == 0
    assert env.yt.network_calls() == 0
    # 24 h after the first attempts, the allowance returns.
    env.clock.advance(hours=24)
    assert env.run("fetch") == 0
    assert len(_video_calls(env)) == 10


def test_limit_is_clamped_to_remaining_allowance(env):
    with env.manifest() as m:
        for i in range(28):
            m.record_attempt_start(
                "youtube", f"old{i:08d}", None, iso(env.clock.now() - timedelta(hours=1))
            )
    ids = _ids(5)
    _list(env, ids)
    for v in ids:
        env.yt.add_video(v)
    assert env.run("fetch", "--limit", "5") == 0
    assert len(_video_calls(env)) == 2


def test_failed_attempts_count_toward_cap(env):
    ids = _ids(31)
    _list(env, ids)
    for v in ids:
        env.yt.add_video(v, FakeHTTPError(404, "Not Found") if v.endswith("0") else None)
    env.run("fetch")
    with env.manifest() as m:
        assert len(m.attempts("youtube")) == 30


# --- stop on block + cooldown latch ---------------------------------------------------------


def test_bot_check_stops_run_and_writes_cooldown(env, capsys):
    ids = _ids(4)
    _list(env, ids)
    env.yt.add_video(ids[0])
    env.yt.add_video(
        ids[1],
        Exception("ERROR: [youtube] x: Sign in to confirm you’re not a bot. Use --cookies"),
    )
    env.yt.add_video(ids[2])
    env.yt.add_video(ids[3])
    assert env.run("fetch") == safety.EXIT_TEMPFAIL
    # Stopped immediately: video 3 and 4 never requested, and no retry of video 2.
    assert [c[1] for c in _video_calls(env)] == [WATCH.format(ids[0]), WATCH.format(ids[1])]
    latch = json.loads(env.paths.cooldown.read_text(encoding="utf-8"))
    assert "not a bot" in latch["reason"]
    assert latch["item_id"] == ids[1] and latch["yt_dlp_version"] == env.yt.version
    assert latch["detected_at"]
    assert "BLOCK SIGNAL" in capsys.readouterr().err
    with env.manifest() as m:
        assert m.components("youtube", ids[1])["metadata"]["status"] == "error"
        assert m.attempts("youtube")[1]["outcome"] == "blocked"
        assert m.last_run("fetch")["exit_code"] == safety.EXIT_TEMPFAIL
    # No snapshot for the blocked video.
    assert latest_snapshot(env.paths.snapshots_dir("youtube", ids[1])) is None


def test_fetch_and_discover_refuse_while_cooldown_exists(env, capsys):
    ids = _ids(2)
    _list(env, ids)
    for v in ids:
        env.yt.add_video(v)
    safety.write_cooldown(env.paths, "HTTP 429", env.clock.now())
    env.clock.advance(days=10)
    assert env.run("fetch") == safety.EXIT_TEMPFAIL
    assert env.run("fetch", "--video", ids[0]) == safety.EXIT_TEMPFAIL
    assert env.run("discover", "--force") == safety.EXIT_TEMPFAIL
    assert env.yt.network_calls() == 0 and env.sb_requests == []
    assert "refusing to run" in capsys.readouterr().err
    with env.manifest() as m:
        assert m.attempts("youtube") == []


@pytest.mark.parametrize(
    "caption_response",
    [
        FakeHTTPError(429, "Too Many Requests"),
        FakeHTTPError(403, "Forbidden"),
        b"<!DOCTYPE html><html><body>Our systems have detected unusual traffic</body></html>",
    ],
)
def test_block_on_caption_download_keeps_honest_snapshot(env, caption_response):
    ids = _ids(2)
    _list(env, ids)
    env.yt.add_video(ids[0], manual=caption_response)
    env.yt.add_video(ids[1])
    assert env.run("fetch") == safety.EXIT_TEMPFAIL
    assert env.paths.cooldown.exists()
    # Auto captions, SponsorBlock and the next video were not attempted.
    assert len(_video_calls(env)) == 1
    assert sum(1 for c in env.yt.calls if c[0] == "fetch_bytes") == 1
    assert env.sb_requests == []
    snap = latest_snapshot(env.paths.snapshots_dir("youtube", ids[0]))
    rec = json.loads((snap / "fetch.json").read_text(encoding="utf-8"))
    assert rec["components"]["metadata"]["status"] == "ok"
    assert rec["components"]["captions_manual"]["status"] == "error"
    assert rec["components"]["captions_auto"]["status"] == "pending"
    assert rec["block_signal"]
    with env.manifest() as m:
        assert not m.is_complete(
            "youtube", ids[0], ["metadata", "captions_manual", "captions_auto"]
        )


def test_block_signal_in_ytdlp_warning_stops_run(env):
    ids = _ids(2)
    _list(env, ids)
    for v in ids:
        env.yt.add_video(v)
    env.yt.warnings_for[WATCH.format(ids[0])] = [
        "[youtube] x: Unable to download API page: HTTP Error 429: Too Many Requests"
    ]
    assert env.run("fetch") == safety.EXIT_TEMPFAIL
    assert len(_video_calls(env)) == 1
    assert env.paths.cooldown.exists()


def test_three_consecutive_unknown_errors_trip_the_latch(env):
    ids = _ids(5)
    _list(env, ids)
    for v in ids:
        env.yt.add_video(v, RuntimeError("Some unexpected extractor failure"))
    assert env.run("fetch") == safety.EXIT_TEMPFAIL
    assert len(_video_calls(env)) == 3
    assert "3 consecutive" in safety.read_cooldown(env.paths).reason


def test_unknown_errors_reset_by_success(env):
    ids = _ids(5)
    _list(env, ids)
    for i, v in enumerate(ids):
        env.yt.add_video(v, None if i == 2 else RuntimeError("Some unexpected extractor failure"))
    assert env.run("fetch") == 1  # errors, but no block
    assert len(_video_calls(env)) == 5
    assert not env.paths.cooldown.exists()


def test_transient_error_retried_at_most_twice_with_60s_wait(env):
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        raise ConnectionResetError("Connection reset by peer")

    env.yt.add_video(VID, flaky)
    assert env.run("fetch", "--video", VID) == 1
    assert state["n"] == 3  # 1 + 2 retries
    assert env.clock.sleeps.count(60) == 2
    assert not env.paths.cooldown.exists()


def test_transient_then_success(env):
    state = {"n": 0}
    info = fixture_info(VID)

    def flaky():
        state["n"] += 1
        if state["n"] == 1:
            raise TimeoutError("The read operation timed out")
        return info

    env.yt.add_video(VID, info)
    env.yt.infos[WATCH.format(VID)] = flaky
    assert env.run("fetch", "--video", VID) == 0


# --- unavailable / restricted / absent -------------------------------------------------------


def test_unavailable_and_restricted_are_recorded_and_skipped(env):
    ids = ["unavail0001", "members0001", "agegate0001", "okvideo0001", "premier0001"]
    _list(env, ids)
    env.yt.add_video(
        ids[0],
        RuntimeError(
            "ERROR: [youtube] x: Video unavailable. This video has been removed by the uploader"
        ),
    )
    env.yt.add_video(ids[1], fixture_info(ids[1], availability="subscriber_only"))
    env.yt.add_video(
        ids[2],
        RuntimeError(
            "ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate for some users."
        ),
    )
    env.yt.add_video(ids[3])
    env.yt.add_video(ids[4], fixture_info(ids[4], live_status="is_upcoming"))
    assert env.run("fetch") == 0
    assert not env.paths.cooldown.exists()
    with env.manifest() as m:
        avail = {i: m.get_item("youtube", i)["availability"] for i in ids}
        assert avail == {
            ids[0]: "unavailable",
            ids[1]: "skipped_restricted",
            ids[2]: "skipped_restricted",
            ids[3]: "public",
            ids[4]: "upcoming",
        }
        assert m.components("youtube", ids[1])["captions_auto"]["status"] == "skipped"
        assert m.components("youtube", ids[4])["metadata"]["status"] == "pending"
    for i in (ids[0], ids[1], ids[2], ids[4]):
        assert latest_snapshot(env.paths.snapshots_dir("youtube", i)) is None
    # Next run: restricted/unavailable are not retried, the upcoming one is.
    env.yt.calls.clear()
    env.clock.advance(hours=25)
    env.run("fetch")
    assert [c[1] for c in _video_calls(env)] == [WATCH.format(ids[4])]


def test_missing_captions_recorded_as_absent(env):
    info = fixture_info(VID, subtitles={}, automatic_captions={})
    env.yt.add_video(VID, info)
    assert env.run("fetch", "--video", VID) == 0
    with env.manifest() as m:
        comps = m.components("youtube", VID)
        assert comps["captions_manual"]["status"] == "absent"
        assert comps["captions_auto"]["status"] == "absent"
        assert m.is_complete("youtube", VID, ["metadata", "captions_manual", "captions_auto"])


def test_po_token_withheld_captions_are_errors_not_absent(env):
    info = fixture_info(VID, subtitles={}, automatic_captions={})
    env.yt.add_video(VID, info)
    env.yt.warnings_for[WATCH.format(VID)] = [
        f"[youtube] {VID}: There are missing subtitles languages because a PO token was not provided."
    ]
    env.run("fetch", "--video", VID)
    with env.manifest() as m:
        comps = m.components("youtube", VID)
        assert comps["captions_manual"]["status"] == "error"
        assert "PO token" in comps["captions_manual"]["detail"]
    assert not env.paths.cooldown.exists()


def test_vtt_fallback_when_json3_invalid(env):
    info = fixture_info(VID)
    env.yt.add_video(VID, info, manual=b"")
    vtt_url = info["subtitles"]["en"][1]["url"]
    env.yt.urls[vtt_url] = fixture_bytes("en.manual.vtt")
    assert env.run("fetch", "--video", VID) == 0
    snap = latest_snapshot(env.paths.snapshots_dir("youtube", VID))
    assert (snap / "captions" / "en.manual.vtt").exists()
    assert not (snap / "captions" / "en.manual.json3").exists()


# --- SponsorBlock ----------------------------------------------------------------------------


def test_sponsorblock_404_stored_as_empty(env):
    env.sb_handler = lambda req: httpx.Response(404, text="Not Found")
    env.yt.add_video(VID)
    assert env.run("fetch", "--video", VID) == 0
    snap = latest_snapshot(env.paths.snapshots_dir("youtube", VID))
    assert json.loads((snap / "sponsorblock.json").read_text()) == {"segments": []}


def test_sponsorblock_error_never_trips_latch_or_refetches(env):
    env.sb_handler = lambda req: httpx.Response(429, text="slow down")
    env.yt.add_video(VID)
    assert env.run("fetch", "--video", VID) == 0
    assert not env.paths.cooldown.exists()
    with env.manifest() as m:
        sb = m.components("youtube", VID)["sponsorblock"]
        assert sb["status"] == "error" and "429" in sb["detail"]
    calls = env.yt.network_calls()
    env.run("fetch", "--video", VID)
    assert env.yt.network_calls() == calls


# --- crash safety --------------------------------------------------------------------------


def test_interrupted_fetch_leaves_no_snapshot_and_is_cleaned_up(env):
    def boom():
        raise KeyboardInterrupt

    info = env.yt.add_video(VID)
    env.yt.urls[info["automatic_captions"]["en-orig"][0]["url"]] = boom
    with pytest.raises(KeyboardInterrupt):
        env.run("fetch", "--video", VID)
    snaps_dir = env.paths.snapshots_dir("youtube", VID)
    assert list_snapshots(snaps_dir) == []
    assert not env.paths.lock.exists()
    with env.manifest() as m:
        assert m.attempts("youtube")[0]["outcome"] == "interrupted"
        assert m.last_run("fetch")["exit_code"] == 130
    env.yt.add_video(VID)
    env.clock.advance(minutes=5)
    assert env.run("fetch", "--video", VID) == 0
    assert [p.name for p in snaps_dir.iterdir()] == [list_snapshots(snaps_dir)[0].name]


def test_lock_prevents_overlapping_runs(env, capsys):
    env.yt.add_video(VID)
    env.paths.root.mkdir(parents=True)
    env.paths.lock.write_text(
        json.dumps({"pid": 1, "command": "fetch", "started_at": iso(env.clock.now())})
    )
    assert env.run("fetch", "--video", VID) == 1
    assert env.yt.network_calls() == 0
    assert "one at a time" in capsys.readouterr().err


# --- audio ---------------------------------------------------------------------------------


def test_audio_off_by_default_and_opt_in(env):
    env.yt.add_video(VID)
    env.run("fetch", "--video", VID)
    assert not any(c[0] == "download_audio" for c in env.yt.calls)
    env.clock.advance(hours=1)
    # Asking for audio makes the existing snapshot incomplete → new snapshot with audio.
    env.run("fetch", "--video", VID, "--audio")
    assert any(c[0] == "download_audio" for c in env.yt.calls)
    snap = latest_snapshot(env.paths.snapshots_dir("youtube", VID))
    assert (snap / "audio.webm").exists()


# --- no evasion / params -------------------------------------------------------------------


def test_ydl_params_are_plain_and_paced(cfg):
    params = build_ydl_params(cfg.fetch, 7.5, logger=None)
    assert not set(params) & safety.FORBIDDEN_YTDLP_PARAMS
    assert params["sleep_interval_requests"] == 7.5
    assert params["sleep_interval_subtitles"] == 10
    assert params["extractor_retries"] == 0
    with pytest.raises(safety.UnsafeConfigError):
        build_ydl_params(cfg.fetch, 1.0, logger=None)


def test_block_warning_aborts_extraction_immediately():
    from coffee_kb.sources.youtube.client import _CollectingLogger

    logger = _CollectingLogger()
    logger.warning("[youtube] Some formats may be missing")
    with pytest.raises(safety.BlockDetected):
        logger.warning("[youtube] x: Sign in to confirm you're not a bot")


# --- track choice --------------------------------------------------------------------------


def test_choose_track():
    langs = ("en", "en-GB", "en-US")
    fmt = [{"ext": "json3", "url": "u"}]
    assert choose_track({"en": fmt, "de": fmt}, langs, auto=False)[0] == "en"
    assert choose_track({"en-GB": fmt}, langs, auto=False)[0] == "en-GB"
    assert choose_track({"en-uYU-mmqFLq8": fmt}, langs, auto=False)[0] == "en-uYU-mmqFLq8"
    assert choose_track({"de": fmt}, langs, auto=False) is None
    assert choose_track({"en": fmt, "en-orig": fmt, "fr": fmt}, langs, auto=True)[0] == "en-orig"
    assert choose_track({"en": fmt}, langs, auto=True)[0] == "en"
    # Original language is German: 'en' would be a machine translation → not used.
    assert choose_track({"de-orig": fmt, "de": fmt, "en": fmt}, langs, auto=True) is None
    assert choose_track({}, langs, auto=True) is None


def test_main_videos_before_shorts_and_streams_when_dates_unknown(env):
    with env.manifest() as m:
        now = iso(env.clock.now())
        m.upsert_listed_item("youtube", "stream00000", now, item_type="stream", listing_rank=0)
        m.upsert_listed_item("youtube", "short000000", now, item_type="short", listing_rank=0)
        m.upsert_listed_item("youtube", "video000001", now, item_type="video", listing_rank=1)
        m.upsert_listed_item("youtube", "video000000", now, item_type="video", listing_rank=0)
        order = [r["item_id"] for r in m.items_newest_first("youtube")]
    assert order == ["video000000", "video000001", "short000000", "stream00000"]


def test_incomplete_video_retried_at_most_three_times(env, capsys):
    info = fixture_info(VID, subtitles={})  # auto track only
    auto_url = info["automatic_captions"]["en-orig"][0]["url"]
    _list(env, [VID])
    env.yt.add_video(VID, info, auto=b"")  # json3 response empty: captions not ready
    del env.yt.urls[auto_url]
    env.yt.urls[auto_url] = b""
    for _ in range(3):
        assert env.run("fetch") == 0
        env.clock.advance(hours=25)
    with env.manifest() as m:
        assert m.failed_attempt_counts("youtube") == {VID: 3}
        detail = m.components("youtube", VID)["captions_auto"]["detail"]
        assert "empty response" in detail and "formats offered: json3, vtt" in detail
    calls = env.yt.network_calls()
    capsys.readouterr()
    assert env.run("fetch") == 0
    assert env.yt.network_calls() == calls  # given up: no more automatic retries
    assert "after 3 failed attempts" in capsys.readouterr().err
    assert env.run("report", "--format", "json") == 0
    assert json.loads(capsys.readouterr().out)["retry_exhausted"] == [VID]
    # An explicit --video still retries it.
    assert env.run("fetch", "--video", VID) == 0
    assert env.yt.network_calls() > calls


def test_invalid_caption_content_does_not_feed_unknown_streak(env):
    ids = _ids(4)
    _list(env, ids)
    for v in ids:
        info = env.yt.add_video(v, manual=b"", auto=b"not captions")
        env.yt.urls[info["subtitles"]["en"][1]["url"]] = b""  # vtt fallback: empty too
        env.yt.urls[info["automatic_captions"]["en-orig"][1]["url"]] = b"{}"
    assert env.run("fetch") == 0
    assert not env.paths.cooldown.exists()
    assert len(_video_calls(env)) == 4

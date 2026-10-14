"""Offline tests for every safety mechanism in docs/DESIGN.md §6.6."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from coffee_kb import cli, safety
from coffee_kb.clock import iso
from coffee_kb.config import ConfigError, FetchConfig, config_from_dict, load_config
from coffee_kb.safety import ErrorKind

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.toml"


# --- floors ---------------------------------------------------------------------------------


def test_repo_config_loads_with_safe_defaults():
    cfg = load_config(REPO_CONFIG)
    fc = cfg.fetch
    assert fc.sleep_between_videos_s == 60 and fc.sleep_between_videos_jitter_s == 60
    assert fc.sleep_between_requests_s == 5 and fc.sleep_between_requests_max_s == 10
    assert fc.sleep_before_subtitles_s == 10
    assert fc.max_videos_per_24h == 30
    assert fc.audio is False


@pytest.mark.parametrize(
    "key,value",
    [
        ("sleep_between_videos_s", 29),
        ("sleep_between_videos_s", 0),
        ("sleep_between_requests_s", 2.9),
        ("sleep_between_requests_max_s", 2),
        ("sleep_before_subtitles_s", 4),
        ("max_videos_per_24h", 51),
        ("max_videos_per_24h", 0),
        ("discover_min_interval_days", 1),
        ("max_transient_retries", 3),
        ("transient_retry_wait_s", 5),
        ("cooldown_min_hours", 1),
        ("sleep_between_videos_jitter_s", -1),
    ],
)
def test_floors_reject_low_values(tmp_path, key, value):
    with pytest.raises(ConfigError, match="unsafe"):
        config_from_dict({"fetch": {key: value}}, base_dir=tmp_path)


def test_floors_accept_exact_floor_values(tmp_path):
    cfg = config_from_dict(
        {
            "fetch": {
                "sleep_between_videos_s": 30,
                "sleep_between_requests_s": 3,
                "sleep_between_requests_max_s": 3,
                "sleep_before_subtitles_s": 5,
                "max_videos_per_24h": 50,
            }
        },
        base_dir=tmp_path,
    )
    assert cfg.fetch.sleep_between_videos_s == 30.0


def test_request_range_must_be_ordered(tmp_path):
    with pytest.raises(ConfigError, match="sleep_between_requests_max_s"):
        config_from_dict(
            {"fetch": {"sleep_between_requests_s": 8, "sleep_between_requests_max_s": 5}},
            base_dir=tmp_path,
        )


def test_unknown_keys_and_bad_types_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown key"):
        config_from_dict({"fetch": {"sleep_between_video_s": 60}}, base_dir=tmp_path)
    with pytest.raises(ConfigError, match="unknown section"):
        config_from_dict({"speed": {}}, base_dir=tmp_path)
    with pytest.raises(ConfigError, match="number"):
        config_from_dict({"fetch": {"sleep_between_videos_s": "fast"}}, base_dir=tmp_path)
    with pytest.raises(ConfigError, match="true or false"):
        config_from_dict({"fetch": {"audio": "yes"}}, base_dir=tmp_path)


def test_pacer_refuses_unchecked_config(clock):
    with pytest.raises(safety.UnsafeConfigError):
        safety.Pacer(FetchConfig(sleep_between_videos_s=1), clock)


def test_no_pacing_override_on_command_line():
    parser = cli.build_parser()
    for argv in (
        ["fetch", "--sleep", "1"],
        ["fetch", "--sleep-interval", "1"],
        ["--fast", "fetch"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_config_error_exits_2(tmp_path, capsys):
    bad = tmp_path / "config.toml"
    bad.write_text("[fetch]\nsleep_between_videos_s = 5\n", encoding="utf-8")
    assert cli.main(["--config", str(bad), "status"]) == cli.EXIT_USAGE
    assert "below the floor" in capsys.readouterr().err


# --- pacing ---------------------------------------------------------------------------------


def test_pacer_ranges_and_uses_clock(clock, rng):
    pacer = safety.Pacer(FetchConfig(), clock, rng)
    delays = [pacer.wait_between_videos() for _ in range(200)]
    assert all(60 <= d <= 120 for d in delays)
    assert max(delays) - min(delays) > 30  # actually randomized
    intervals = [pacer.request_interval() for _ in range(200)]
    assert all(5 <= d <= 10 for d in intervals)
    assert pacer.wait_before_subtitle() == 10
    assert pacer.wait_before_retry() == 60
    assert clock.sleeps[:200] == delays  # slept via the clock seam, never for real


# --- daily cap ------------------------------------------------------------------------------


def _attempts(manifest, clock, n, minutes_apart=1):
    for i in range(n):
        t = clock.now() - timedelta(minutes=minutes_apart * (n - i))
        manifest.record_attempt_start("youtube", f"v{i}", None, iso(t))


def test_daily_cap_counts_attempts_in_rolling_window(manifest, clock):
    fc = FetchConfig()
    assert safety.remaining_allowance(manifest, fc, clock.now()) == 30
    # An attempt 25 h ago does not count.
    manifest.record_attempt_start("youtube", "old", None, iso(clock.now() - timedelta(hours=25)))
    _attempts(manifest, clock, 29)
    assert safety.remaining_allowance(manifest, fc, clock.now()) == 1
    assert safety.next_eligible_time(manifest, fc, clock.now()) == clock.now()
    manifest.record_attempt_start("youtube", "last", None, iso(clock.now()))
    assert safety.remaining_allowance(manifest, fc, clock.now()) == 0
    # The oldest attempt in the window was 29 minutes ago → eligible 24 h after it.
    nxt = safety.next_eligible_time(manifest, fc, clock.now())
    assert nxt == clock.now() - timedelta(minutes=29) + timedelta(hours=24)
    clock.current = nxt + timedelta(seconds=1)
    assert safety.remaining_allowance(manifest, fc, clock.now()) == 1


def test_cap_is_per_source_type(manifest, clock):
    manifest.record_attempt_start("other", "x", None, iso(clock.now()))
    assert safety.remaining_allowance(manifest, FetchConfig(), clock.now()) == 30


# --- block classification -------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser",
        "Sign in to confirm you're not a bot. This helps protect our community.",
        "Unable to download webpage: HTTP Error 429: Too Many Requests",
        "Unable to download video subtitles for 'en': HTTP Error 429: Too Many Requests",
        "HTTP Error 403: Forbidden",
        "Our systems have detected unusual traffic from your computer network",
        "Got a CAPTCHA page",
        "redirected to https://consent.youtube.com/m?continue=...",
        "Video unavailable. This content isn't available, try again later.",
        "The current session has been rate-limited by YouTube for up to an hour.",
    ],
)
def test_block_signals(message):
    assert safety.classify_message(message) is ErrorKind.BLOCK


@pytest.mark.parametrize("status", [403, 429])
def test_block_by_http_status(status):
    assert safety.classify_message("whatever", http_status=status) is ErrorKind.BLOCK


@pytest.mark.parametrize(
    "message,kind",
    [
        ("Sign in to confirm your age. This video may be inappropriate", ErrorKind.RESTRICTED),
        ("Join this channel to get access to members-only content", ErrorKind.RESTRICTED),
        ("This video is available to this channel's members on level: X", ErrorKind.RESTRICTED),
        ("Video unavailable. This video has been removed by the uploader", ErrorKind.UNAVAILABLE),
        ("Private video. Sign in if you've been granted access", ErrorKind.UNAVAILABLE),
        ("Premieres in 3 hours", ErrorKind.UPCOMING),
        ("This live event will begin in a few moments.", ErrorKind.UPCOMING),
        ("<urlopen error [Errno -3] Temporary failure in name resolution>", ErrorKind.TRANSIENT),
        ("Connection reset by peer", ErrorKind.TRANSIENT),
        ("The read operation timed out", ErrorKind.TRANSIENT),
        ("Some brand new extractor failure", ErrorKind.UNKNOWN),
    ],
)
def test_other_classes(message, kind):
    assert safety.classify_message(message) is kind


def test_classify_exception_reads_status_and_chain():
    class FakeHTTPError(Exception):
        def __init__(self) -> None:
            super().__init__("HTTP Error 429")
            self.status = 429

    class FakeDownloadError(Exception):
        def __init__(self, msg, exc_info):
            super().__init__(msg)
            self.exc_info = exc_info

    inner = FakeHTTPError()
    outer = FakeDownloadError("ERROR: unable to download", (type(inner), inner, None))
    kind, msg = safety.classify_exception(outer)
    assert kind is ErrorKind.BLOCK
    assert "429" in msg
    kind, _ = safety.classify_exception(ConnectionResetError("reset"))
    assert kind is ErrorKind.TRANSIENT


def test_find_block_signal_in_warnings():
    warnings = [
        "[youtube] abc: Some formats may be missing",
        "[youtube] abc: Sign in to confirm you're not a bot; retrying with a different client",
    ]
    assert "not a bot" in safety.find_block_signal(warnings)
    assert safety.find_block_signal(["[youtube] Sign in to confirm your age"]) is None
    assert safety.find_block_signal([]) is None


def test_looks_like_html():
    assert safety.looks_like_html(b"  <!DOCTYPE html><html><body>captcha</body></html>")
    assert safety.looks_like_html(b"<html lang=en>")
    assert not safety.looks_like_html(b'{"events": []}')
    assert not safety.looks_like_html(b"WEBVTT\n\n")


def test_unknown_error_streak():
    streak = safety.UnknownErrorStreak()
    streak.record_unknown("a", "x")
    streak.record_unknown("b", "y")
    streak.record_success()
    streak.record_unknown("c", "z")
    streak.record_unknown("d", "z")
    with pytest.raises(safety.BlockDetected, match="3 consecutive"):
        streak.record_unknown("e", "z")


# --- cooldown latch -------------------------------------------------------------------------


def test_cooldown_write_read_ensure(data_paths, clock):
    safety.ensure_no_cooldown(data_paths)
    latch = safety.write_cooldown(
        data_paths, "HTTP 429", clock.now(), item_id="abc", yt_dlp_version="1"
    )
    assert data_paths.cooldown.exists()
    data = json.loads(data_paths.cooldown.read_text(encoding="utf-8"))
    assert data["reason"] == "HTTP 429" and data["item_id"] == "abc"
    assert data["detected_at"] == "2026-10-14T09:30:00Z" and data["yt_dlp_version"] == "1"
    with pytest.raises(safety.CooldownActive):
        safety.ensure_no_cooldown(data_paths)
    # A second trip keeps the first detection.
    clock.advance(hours=1)
    again = safety.write_cooldown(data_paths, "other", clock.now())
    assert again.reason == latch.reason and again.detected_at == latch.detected_at


def test_unreadable_latch_still_blocks(data_paths):
    data_paths.root.mkdir(parents=True)
    data_paths.cooldown.write_text("garbage", encoding="utf-8")
    with pytest.raises(safety.CooldownActive):
        safety.ensure_no_cooldown(data_paths)


def test_clear_cooldown_requires_48h(data_paths, clock):
    fc = FetchConfig()
    safety.write_cooldown(data_paths, "HTTP 429", clock.now())
    clock.advance(hours=47, minutes=59)
    with pytest.raises(safety.CooldownTooRecent):
        safety.clear_cooldown(data_paths, fc, clock.now())
    assert data_paths.cooldown.exists()
    clock.advance(minutes=1)
    cleared = safety.clear_cooldown(data_paths, fc, clock.now())
    assert cleared.reason == "HTTP 429"
    assert not data_paths.cooldown.exists()
    archived = list(data_paths.cooldown_history_dir.iterdir())
    assert len(archived) == 1


def test_cooldown_hours_cannot_be_lowered_below_48(data_paths, clock):
    fc = replace(FetchConfig(), cooldown_min_hours=1)  # bypassing validation on purpose
    safety.write_cooldown(data_paths, "x", clock.now())
    clock.advance(hours=2)
    with pytest.raises(safety.CooldownTooRecent):
        safety.clear_cooldown(data_paths, fc, clock.now())


def _write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text('[paths]\ndata_dir = "data"\n', encoding="utf-8")
    return path


def test_cli_clear_cooldown_refuses_early(tmp_path, clock, capsys, monkeypatch):
    conf = _write_config(tmp_path)
    from coffee_kb.paths import DataPaths

    paths = DataPaths(tmp_path / "data")
    safety.write_cooldown(paths, "Sign in to confirm you're not a bot", clock.now())
    monkeypatch.setattr("builtins.input", lambda _prompt: "clear")
    clock.advance(hours=10)
    assert cli.main(["--config", str(conf), "clear-cooldown"], clock=clock) == cli.EXIT_ERROR
    assert "Refusing" in capsys.readouterr().out
    assert paths.cooldown.exists()


def test_cli_clear_cooldown_needs_confirmation(tmp_path, clock, capsys, monkeypatch):
    conf = _write_config(tmp_path)
    from coffee_kb.paths import DataPaths

    paths = DataPaths(tmp_path / "data")
    safety.write_cooldown(paths, "HTTP 429", clock.now())
    clock.advance(hours=49)
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    assert cli.main(["--config", str(conf), "clear-cooldown"], clock=clock) == cli.EXIT_ERROR
    assert paths.cooldown.exists()
    monkeypatch.setattr("builtins.input", lambda _prompt: "clear")
    assert cli.main(["--config", str(conf), "clear-cooldown"], clock=clock) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "upgrade yt-dlp" in out and "Cooldown cleared" in out
    assert not paths.cooldown.exists()


def test_cli_status_reports_cooldown_and_cap(tmp_path, clock, capsys):
    conf = _write_config(tmp_path)
    from coffee_kb.manifest import Manifest
    from coffee_kb.paths import DataPaths

    paths = DataPaths(tmp_path / "data")
    with Manifest(paths.manifest) as m:
        for i in range(30):
            m.record_attempt_start("youtube", f"v{i}", None, iso(clock.now() - timedelta(hours=1)))
    safety.write_cooldown(paths, "HTTP 429", clock.now(), item_id="v29")
    assert cli.main(["--config", str(conf), "status", "--format", "json"], clock=clock) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["cooldown"]["reason"] == "HTTP 429"
    assert state["cooldown"]["can_clear_from"] == "2026-10-16T09:30:00Z"
    assert state["fetches_last_24h"] == 30 and state["remaining_allowance"] == 0
    assert state["next_eligible_fetch"] == "2026-10-15T08:30:00Z"
    assert cli.main(["--config", str(conf), "status"], clock=clock) == 0
    out = capsys.readouterr().out
    assert "ACTIVE" in out and "uv lock --upgrade-package yt-dlp" in out


# --- run lock -------------------------------------------------------------------------------


def test_run_lock_is_exclusive(data_paths, clock):
    with safety.run_lock(data_paths, clock, "fetch"):
        assert data_paths.lock.exists()
        with pytest.raises(safety.LockHeld), safety.run_lock(data_paths, clock, "discover"):
            pass
    assert not data_paths.lock.exists()


def test_stale_lock_is_taken_over(data_paths, clock):
    data_paths.root.mkdir(parents=True)
    old = iso(clock.now() - timedelta(hours=9))
    data_paths.lock.write_text(json.dumps({"pid": 1, "command": "fetch", "started_at": old}))
    with safety.run_lock(data_paths, clock, "fetch"):
        info = json.loads(data_paths.lock.read_text())
        assert info["started_at"] == iso(clock.now())


# --- no evasion -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key", ["cookiefile", "cookiesfrombrowser", "proxy", "impersonate", "http_headers", "username"]
)
def test_evasion_params_rejected(key):
    with pytest.raises(safety.UnsafeConfigError):
        safety.assert_no_evasion({"quiet": True, key: "x"})
    safety.assert_no_evasion({"quiet": True, key: None})


def test_find_config_order(tmp_path, monkeypatch):
    from coffee_kb.config import find_config

    repo_cfg = Path(__file__).resolve().parents[1] / "config.toml"
    elsewhere = tmp_path / "somewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("COFFEE_KB_CONFIG", raising=False)
    # Run from any directory: falls back to the checkout's config.toml.
    assert find_config(None).resolve() == repo_cfg
    (elsewhere / "config.toml").write_text("", encoding="utf-8")
    assert find_config(None) == Path("config.toml")
    monkeypatch.setenv("COFFEE_KB_CONFIG", str(tmp_path / "env.toml"))
    assert find_config(None) == tmp_path / "env.toml"
    assert find_config(tmp_path / "x.toml") == tmp_path / "x.toml"

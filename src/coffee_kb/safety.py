"""YouTube request safety (docs/DESIGN.md §6.6). Core feature, not polish.

Everything that limits how we talk to YouTube lives here:

* pacing floors, checked when the config loads (no CLI/env override exists);
* the pacer that sleeps between videos and before requests;
* the rolling 24 h cap, counted from fetch attempts recorded in the manifest;
* the block-signal classifier;
* the cooldown latch (``data/COOLDOWN``) and its 48 h clearing rule;
* a run lock so two runs never overlap;
* a guard that rejects any bot-detection-evasion option in the yt-dlp params.
"""

from __future__ import annotations

import contextlib
import enum
import json
import logging
import os
import random
import re
import socket
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from coffee_kb.clock import Clock, iso, parse_iso, stamp

if TYPE_CHECKING:
    from coffee_kb.config import FetchConfig
    from coffee_kb.paths import DataPaths

log = logging.getLogger(__name__)

# --- floors and ceilings ------------------------------------------------------------------
# Do not lower these, and do not add a way around them. See CLAUDE.md "YouTube request safety".
MIN_SLEEP_BETWEEN_VIDEOS_S = 30.0
MIN_SLEEP_BETWEEN_REQUESTS_S = 3.0
MIN_SLEEP_BEFORE_SUBTITLES_S = 5.0
MAX_VIDEOS_PER_24H_CEILING = 50
MIN_DISCOVER_INTERVAL_DAYS = 7.0
MAX_TRANSIENT_RETRIES_CEILING = 2
MIN_TRANSIENT_RETRY_WAIT_S = 60.0
MIN_COOLDOWN_HOURS = 48.0
UNKNOWN_ERROR_STREAK_LIMIT = 3
CAP_WINDOW = timedelta(hours=24)
STALE_LOCK_AGE = timedelta(hours=6)

EXIT_TEMPFAIL = 75  # EX_TEMPFAIL: blocked or cooling down; the timer log shows it clearly


class UnsafeConfigError(ValueError):
    pass


def validate_fetch_config(fc: FetchConfig) -> None:
    """Raise if any pacing value is below its floor (or a ceiling is exceeded)."""
    problems: list[str] = []

    def floor(name: str, value: float, minimum: float) -> None:
        if value < minimum:
            problems.append(f"{name} = {value} is below the floor of {minimum}")

    floor("sleep_between_videos_s", fc.sleep_between_videos_s, MIN_SLEEP_BETWEEN_VIDEOS_S)
    floor("sleep_between_videos_jitter_s", fc.sleep_between_videos_jitter_s, 0)
    floor("sleep_between_requests_s", fc.sleep_between_requests_s, MIN_SLEEP_BETWEEN_REQUESTS_S)
    floor(
        "sleep_between_requests_max_s",
        fc.sleep_between_requests_max_s,
        MIN_SLEEP_BETWEEN_REQUESTS_S,
    )
    if fc.sleep_between_requests_max_s < fc.sleep_between_requests_s:
        problems.append("sleep_between_requests_max_s must be >= sleep_between_requests_s")
    floor("sleep_before_subtitles_s", fc.sleep_before_subtitles_s, MIN_SLEEP_BEFORE_SUBTITLES_S)
    floor("discover_min_interval_days", fc.discover_min_interval_days, MIN_DISCOVER_INTERVAL_DAYS)
    floor("transient_retry_wait_s", fc.transient_retry_wait_s, MIN_TRANSIENT_RETRY_WAIT_S)
    floor("cooldown_min_hours", fc.cooldown_min_hours, MIN_COOLDOWN_HOURS)
    floor("socket_timeout_s", fc.socket_timeout_s, 1)
    if not 1 <= fc.max_videos_per_24h <= MAX_VIDEOS_PER_24H_CEILING:
        problems.append(
            f"max_videos_per_24h = {fc.max_videos_per_24h} must be between 1 and "
            f"{MAX_VIDEOS_PER_24H_CEILING}"
        )
    if not 0 <= fc.max_transient_retries <= MAX_TRANSIENT_RETRIES_CEILING:
        problems.append(
            f"max_transient_retries = {fc.max_transient_retries} must be between 0 and "
            f"{MAX_TRANSIENT_RETRIES_CEILING}"
        )
    if problems:
        raise UnsafeConfigError(
            "unsafe [fetch] config (floors protect the home IP from YouTube blocks): "
            + "; ".join(problems)
        )


# --- pacing ---------------------------------------------------------------------------------


class Pacer:
    """Sleeps between videos and before our own requests. Strictly sequential use."""

    def __init__(self, fc: FetchConfig, clock: Clock, rng: random.Random | None = None) -> None:
        validate_fetch_config(fc)  # defence in depth: never pace from an unchecked config
        self.fc = fc
        self.clock = clock
        self.rng = rng or random.Random()

    def between_videos_delay(self) -> float:
        return self.fc.sleep_between_videos_s + self.rng.uniform(
            0, self.fc.sleep_between_videos_jitter_s
        )

    def request_interval(self) -> float:
        """Seconds between requests inside one video (passed to yt-dlp's
        ``sleep_interval_requests``, which is fixed per YoutubeDL instance, so each video
        draws its own value from the configured range)."""
        return self.rng.uniform(
            self.fc.sleep_between_requests_s, self.fc.sleep_between_requests_max_s
        )

    def wait_between_videos(self) -> float:
        delay = self.between_videos_delay()
        log.info("pacing: sleeping %.0f s before the next video", delay)
        self.clock.sleep(delay)
        return delay

    def wait_before_subtitle(self) -> float:
        delay = self.fc.sleep_before_subtitles_s
        log.debug("pacing: sleeping %.0f s before caption download", delay)
        self.clock.sleep(delay)
        return delay

    def wait_before_request(self) -> float:
        delay = self.request_interval()
        log.debug("pacing: sleeping %.1f s before request", delay)
        self.clock.sleep(delay)
        return delay

    def wait_before_retry(self) -> float:
        delay = self.fc.transient_retry_wait_s
        log.info("transient error: waiting %.0f s before retrying", delay)
        self.clock.sleep(delay)
        return delay


# --- daily cap ------------------------------------------------------------------------------


class AttemptLog(Protocol):
    def attempt_times_since(self, source_type: str, since: str) -> list[str]: ...


def attempts_in_window(
    manifest: AttemptLog, now: datetime, source_type: str = "youtube"
) -> list[datetime]:
    since = iso(now - CAP_WINDOW)
    return sorted(parse_iso(t) for t in manifest.attempt_times_since(source_type, since))


def remaining_allowance(
    manifest: AttemptLog, fc: FetchConfig, now: datetime, source_type: str = "youtube"
) -> int:
    used = len(attempts_in_window(manifest, now, source_type))
    return max(0, fc.max_videos_per_24h - used)


def next_eligible_time(
    manifest: AttemptLog, fc: FetchConfig, now: datetime, source_type: str = "youtube"
) -> datetime:
    """When the next video fetch may start under the rolling 24 h cap."""
    times = attempts_in_window(manifest, now, source_type)
    over = len(times) - fc.max_videos_per_24h
    if over < 0:
        return now
    return times[over] + CAP_WINDOW


# --- block detection ------------------------------------------------------------------------


class ErrorKind(enum.Enum):
    BLOCK = "block"  # stop the run, write the cooldown latch, no retries
    RESTRICTED = "restricted"  # members-only, age-gated, paid: needs auth → skipped_restricted
    UPCOMING = "upcoming"  # premiere / scheduled or live stream not finished yet
    UNAVAILABLE = "unavailable"  # removed, private, terminated
    TRANSIENT = "transient"  # DNS, connection reset, timeout: at most 2 retries, 60 s apart
    UNKNOWN = "unknown"  # anything else; 3 in a row are treated as a block


# Checked before block patterns, because "Sign in to confirm your age" is not a bot check.
_RESTRICTED_PATTERNS = [
    r"confirm your age",
    r"age[- ]restricted",
    r"inappropriate for some users",
    r"members[- ]only",
    r"join this channel",
    r"available to this channel'?s members",
    r"requires payment",
    r"purchase",
    r"premium",
]

BLOCK_PATTERNS = [
    r"sign in to confirm",
    r"not a bot",
    r"\b429\b",
    r"too many requests",
    r"\b403\b",
    r"forbidden",
    r"captcha",
    r"unusual traffic",
    r"consent\.youtube\.com",
    r"consent\.google\.",
    r"before you continue to youtube",
    r"rate[- ]limit",
    r"try again later",
    r"content isn.t available",
    r"html where json was expected",
]

_UPCOMING_PATTERNS = [
    r"premieres? in",
    r"this live event will begin",
    r"live event will begin",
    r"is_upcoming",
]

_UNAVAILABLE_PATTERNS = [
    r"video unavailable",
    r"private video",
    r"video is private",
    r"has been removed",
    r"no longer available",
    r"account .* terminated",
    r"been terminated",
    r"is not available",
    r"does not exist",
]

_TRANSIENT_PATTERNS = [
    r"timed? ?out",
    r"connection (reset|refused|aborted)",
    r"temporary failure in name resolution",
    r"name or service not known",
    r"getaddrinfo failed",
    r"nodename nor servname",
    r"network is unreachable",
    r"remote ?disconnected",
    r"remote end closed connection",
    r"incomplete ?read",
    r"connection broken",
    r"\b50[0234]\b",
]


def _match_any(patterns: Iterable[str], text: str) -> str | None:
    for pat in patterns:
        if re.search(pat, text, flags=re.IGNORECASE):
            return pat
    return None


def classify_message(message: str, http_status: int | None = None) -> ErrorKind:
    """Classify a YouTube-side error message (yt-dlp error text or our own request error).

    Only call this for YouTube/googlevideo traffic: any 403 or 429 here is a block signal.
    SponsorBlock errors are handled separately and never trip the latch.
    """
    if http_status in (403, 429):
        return ErrorKind.BLOCK
    text = message or ""
    if _match_any(_RESTRICTED_PATTERNS, text):
        return ErrorKind.RESTRICTED
    if _match_any(BLOCK_PATTERNS, text):
        return ErrorKind.BLOCK
    if _match_any(_UPCOMING_PATTERNS, text):
        return ErrorKind.UPCOMING
    if _match_any(_UNAVAILABLE_PATTERNS, text):
        return ErrorKind.UNAVAILABLE
    if _match_any(_TRANSIENT_PATTERNS, text):
        return ErrorKind.TRANSIENT
    if http_status is not None and http_status >= 500:
        return ErrorKind.TRANSIENT
    return ErrorKind.UNKNOWN


def _http_status_of(exc: BaseException) -> int | None:
    for attr in ("status", "code", "status_code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int) and 100 <= val <= 599:
            return val
    return None


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        # yt-dlp's DownloadError/ExtractorError keep the underlying error in exc_info / orig_msg.
        info = getattr(cur, "exc_info", None)
        if isinstance(info, tuple) and len(info) >= 2 and isinstance(info[1], BaseException):
            stack.append(info[1])
        for nxt in (cur.__cause__, cur.__context__, getattr(cur, "cause", None)):
            if isinstance(nxt, BaseException):
                stack.append(nxt)


def classify_exception(exc: BaseException) -> tuple[ErrorKind, str]:
    status: int | None = None
    parts: list[str] = []
    for e in _exception_chain(exc):
        status = status or _http_status_of(e)
        parts.append(f"{type(e).__name__}: {e}")
        if isinstance(e, socket.timeout | TimeoutError | ConnectionError) and status is None:
            parts.append("connection reset/timed out")
    message = " | ".join(parts)
    return classify_message(message, status), message


def find_block_signal(lines: Iterable[str]) -> str | None:
    """Return the first line (e.g. a yt-dlp warning) that carries a block signal."""
    for line in lines:
        if _match_any(_RESTRICTED_PATTERNS, line):
            continue
        if _match_any(BLOCK_PATTERNS, line):
            return line
    return None


def looks_like_html(body: bytes) -> bool:
    head = body[:512].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html")) or b"<html" in head


class BlockDetected(Exception):
    """A block signal was seen. The run must stop now; the caller writes the latch."""

    def __init__(self, reason: str, item_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.item_id = item_id


class UnknownErrorStreak:
    """Unknown errors on N consecutive videos are treated as a block signal."""

    def __init__(self, limit: int = UNKNOWN_ERROR_STREAK_LIMIT) -> None:
        self.limit = limit
        self.messages: list[str] = []

    def record_success(self) -> None:
        self.messages.clear()

    def record_unknown(self, item_id: str, message: str) -> None:
        self.messages.append(f"{item_id}: {message}")
        if len(self.messages) >= self.limit:
            raise BlockDetected(
                f"unknown errors on {self.limit} consecutive videos: " + " || ".join(self.messages),
                item_id=item_id,
            )


# --- cooldown latch -------------------------------------------------------------------------


class CooldownActive(Exception):
    def __init__(self, latch: Cooldown) -> None:
        super().__init__(latch.reason)
        self.latch = latch


class CooldownTooRecent(Exception):
    pass


@dataclass
class Cooldown:
    reason: str
    detected_at: str
    item_id: str | None = None
    yt_dlp_version: str | None = None
    command: str | None = None

    def describe(self) -> str:
        item = f" (item {self.item_id})" if self.item_id else ""
        return f"since {self.detected_at}{item}: {self.reason}"


def read_cooldown(paths: DataPaths) -> Cooldown | None:
    path = paths.cooldown
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Cooldown(
            reason=str(data.get("reason") or "unknown"),
            detected_at=str(data["detected_at"]),
            item_id=data.get("item_id"),
            yt_dlp_version=data.get("yt_dlp_version"),
            command=data.get("command"),
        )
    except (OSError, ValueError, KeyError, TypeError):
        # An unreadable latch is still a latch. Use the file time as the detection time.
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).replace(microsecond=0)
        return Cooldown(reason="COOLDOWN file present but unreadable", detected_at=iso(mtime))


def ensure_no_cooldown(paths: DataPaths) -> None:
    latch = read_cooldown(paths)
    if latch is not None:
        raise CooldownActive(latch)


def write_cooldown(
    paths: DataPaths,
    reason: str,
    now: datetime,
    item_id: str | None = None,
    yt_dlp_version: str | None = None,
    command: str | None = None,
) -> Cooldown:
    """Write the latch. If one already exists, the first detection is kept."""
    existing = read_cooldown(paths)
    if existing is not None:
        return existing
    latch = Cooldown(
        reason=reason,
        detected_at=iso(now),
        item_id=item_id,
        yt_dlp_version=yt_dlp_version,
        command=command,
    )
    paths.root.mkdir(parents=True, exist_ok=True)
    tmp = paths.cooldown.with_name(paths.cooldown.name + ".tmp")
    tmp.write_text(json.dumps(asdict(latch), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, paths.cooldown)
    return latch


def cooldown_clear_time(latch: Cooldown, fc: FetchConfig) -> datetime:
    hours = max(fc.cooldown_min_hours, MIN_COOLDOWN_HOURS)
    return parse_iso(latch.detected_at) + timedelta(hours=hours)


def clear_cooldown(paths: DataPaths, fc: FetchConfig, now: datetime) -> Cooldown | None:
    """Remove the latch if at least ``cooldown_min_hours`` have passed. User-only command.

    The latch is archived to ``cooldown_history/`` rather than deleted.
    """
    latch = read_cooldown(paths)
    if latch is None:
        return None
    allowed_at = cooldown_clear_time(latch, fc)
    if now < allowed_at:
        raise CooldownTooRecent(
            f"cooldown set {latch.detected_at}; it can be cleared from {iso(allowed_at)} "
            f"({fc.cooldown_min_hours:.0f} h minimum). Waiting is the only fix."
        )
    paths.cooldown_history_dir.mkdir(parents=True, exist_ok=True)
    os.replace(paths.cooldown, paths.cooldown_history_dir / f"COOLDOWN.{stamp(now)}.json")
    return latch


# --- run lock -------------------------------------------------------------------------------


class LockHeld(Exception):
    pass


@contextlib.contextmanager
def run_lock(paths: DataPaths, clock: Clock, command: str) -> Iterator[None]:
    """Cross-platform exclusive lock (O_EXCL file) so two runs never overlap.

    A lock older than ``STALE_LOCK_AGE`` is assumed to be left by a crashed run.
    """
    paths.root.mkdir(parents=True, exist_ok=True)
    path = paths.lock
    payload = json.dumps({"pid": os.getpid(), "command": command, "started_at": iso(clock.now())})
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                info = json.loads(path.read_text(encoding="utf-8"))
                started = parse_iso(info["started_at"])
            except (OSError, ValueError, KeyError):
                info, started = {}, None
            if started is not None and clock.now() - started < STALE_LOCK_AGE:
                raise LockHeld(
                    f"another coffee-kb run holds {path} ({info.get('command')}, pid "
                    f"{info.get('pid')}, since {info.get('started_at')}). Runs are strictly "
                    "one at a time."
                ) from None
            log.warning("removing stale lock %s (%s)", path, info or "unreadable")
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        break
    else:  # pragma: no cover - only if another process races us twice
        raise LockHeld(f"could not acquire {path}")
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


# --- no evasion -----------------------------------------------------------------------------

# yt-dlp params that would amount to authentication or bot-detection evasion. Never set them.
FORBIDDEN_YTDLP_PARAMS = frozenset(
    {
        "cookiefile",
        "cookiesfrombrowser",
        "username",
        "password",
        "usenetrc",
        "netrc_location",
        "netrc_cmd",
        "twofactor",
        "videopassword",
        "ap_mso",
        "ap_username",
        "ap_password",
        "proxy",
        "geo_verification_proxy",
        "geo_bypass",
        "geo_bypass_country",
        "geo_bypass_ip_block",
        "source_address",
        "impersonate",
        "http_headers",
        "extractor_args",
        "client_certificate",
    }
)


def assert_no_evasion(params: dict[str, Any]) -> None:
    bad = sorted(
        k for k in params if k in FORBIDDEN_YTDLP_PARAMS and params[k] not in (None, False)
    )
    if bad:
        raise UnsafeConfigError(f"forbidden yt-dlp params (auth/evasion): {', '.join(bad)}")


# --- state summary (used by `status` and `report`) -------------------------------------------


def safety_state(paths: DataPaths, fc: FetchConfig, manifest: Any, now: datetime) -> dict[str, Any]:
    latch = read_cooldown(paths)
    used = len(attempts_in_window(manifest, now))
    remaining = max(0, fc.max_videos_per_24h - used)
    nxt = next_eligible_time(manifest, fc, now)
    last_discover = manifest.last_successful_run("discover")
    discover_next = None
    if last_discover is not None:
        discover_next = iso(
            parse_iso(last_discover["started_at"]) + timedelta(days=fc.discover_min_interval_days)
        )
    return {
        "cooldown": None
        if latch is None
        else {
            **asdict(latch),
            "can_clear_from": iso(cooldown_clear_time(latch, fc)),
        },
        "fetches_last_24h": used,
        "max_videos_per_24h": fc.max_videos_per_24h,
        "remaining_allowance": remaining,
        "next_eligible_fetch": iso(nxt),
        "last_discover": None if last_discover is None else last_discover["started_at"],
        "next_discover_allowed": discover_next,
    }

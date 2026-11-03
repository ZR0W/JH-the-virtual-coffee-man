"""``discover``: list the channel's tabs into the manifest (docs/DESIGN.md §4.1, §6.1).

Uses yt-dlp flat extraction (no per-video requests). The listings are also stored as a raw
snapshot under ``raw/youtube/_channel/`` so the manifest's listing data stays rebuildable.

Self-throttled: refuses to run more often than ``discover_min_interval_days`` unless
``--force``; the cooldown latch is checked by the caller before this runs, ``--force`` or not.
Playlist membership (``/playlists`` tab) is not collected yet. TODO if wanted later.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from coffee_kb import safety
from coffee_kb.clock import iso, parse_iso
from coffee_kb.runtime import Runtime, tool_versions
from coffee_kb.safety import BlockDetected, ErrorKind
from coffee_kb.snapshots import SnapshotWriter

log = logging.getLogger(__name__)

SOURCE_TYPE = "youtube"
TAB_ITEM_TYPES = {"videos": "video", "shorts": "short", "streams": "stream"}
RESTRICTED_AVAILABILITY = {"subscriber_only", "premium_only", "needs_auth"}
_NO_TAB_RE = re.compile(r"does not have an? \w+ tab", re.IGNORECASE)


@dataclass
class DiscoverResult:
    exit_code: int
    throttled: bool = False
    counts_by_type: dict[str, int] = field(default_factory=dict)
    new_items: int = 0
    missing: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    block_reason: str | None = None
    snapshot: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "throttled": self.throttled,
            "counts_by_type": self.counts_by_type,
            "new_items": self.new_items,
            "missing_from_listing": self.missing,
            "errors": self.errors,
            "block_reason": self.block_reason,
            "snapshot": self.snapshot,
        }


def discover_allowed_at(rt: Runtime) -> datetime | None:
    last = rt.manifest.last_successful_run("discover")
    if last is None:
        return None
    return parse_iso(last["started_at"]) + timedelta(days=rt.cfg.fetch.discover_min_interval_days)


def _upload_date(entry: dict[str, Any]) -> str | None:
    raw = entry.get("upload_date")
    if isinstance(raw, str) and re.fullmatch(r"\d{8}", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    for key in ("timestamp", "release_timestamp"):
        ts = entry.get(key)
        if isinstance(ts, int | float):
            return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")
    return None


def _availability(entry: dict[str, Any]) -> str | None:
    avail = entry.get("availability")
    if avail in RESTRICTED_AVAILABILITY:
        return "skipped_restricted"
    if avail == "private":
        return "unavailable"
    if entry.get("live_status") in ("is_upcoming", "is_live"):
        return "upcoming"
    return None


def _int_or_none(v: Any) -> int | None:
    return int(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def run_discover(rt: Runtime, *, force: bool) -> DiscoverResult:
    """Caller must have checked the cooldown latch and taken the run lock."""
    now = rt.clock.now()
    allowed_at = discover_allowed_at(rt)
    if allowed_at is not None and now < allowed_at and not force:
        log.info(
            "discover ran recently; next run allowed from %s (use --force to override)",
            iso(allowed_at),
        )
        return DiscoverResult(exit_code=0, throttled=True)

    yc = rt.youtube
    handle = rt.cfg.youtube.channel_handle
    result = DiscoverResult(exit_code=0)
    writer = SnapshotWriter(rt.paths.channel_snapshots_dir(SOURCE_TYPE), now)
    listings: dict[str, list[dict[str, Any]]] = {}
    streak = safety.UnknownErrorStreak()
    tab_results: dict[str, Any] = {}
    warnings: list[str] = []
    fc = rt.cfg.fetch

    try:
        # One yt-dlp session for all tabs, so the request sleep applies between every request.
        with yc.session(rt.pacer.request_interval(), flat=True) as session:
            for tab in rt.cfg.youtube.tabs:
                url = f"https://www.youtube.com/{handle}/{tab}"
                log.info("discover: listing %s", url)
                attempts = 0
                while True:
                    try:
                        info = session.extract_info(url)
                    except BlockDetected:
                        raise
                    except Exception as exc:
                        kind, msg = safety.classify_exception(exc)
                        if _NO_TAB_RE.search(msg):
                            log.info("discover: channel has no %s tab", tab)
                            listings[tab] = []
                            tab_results[tab] = {"status": "absent", "detail": msg[:500]}
                            break
                        if kind is ErrorKind.BLOCK:
                            raise BlockDetected(f"discover {tab}: {msg[:500]}") from exc
                        if kind is ErrorKind.TRANSIENT and attempts < fc.max_transient_retries:
                            attempts += 1
                            rt.pacer.wait_before_retry()
                            continue
                        result.errors[tab] = msg[:500]
                        tab_results[tab] = {"status": "error", "detail": msg[:500]}
                        if kind is ErrorKind.UNKNOWN:
                            streak.record_unknown(tab, msg[:200])
                        break
                    else:
                        streak.record_success()
                        entries = [e for e in (info.get("entries") or []) if isinstance(e, dict)]
                        listings[tab] = entries
                        writer.write_json(f"{tab}.json", info)
                        tab_results[tab] = {"status": "ok", "entries": len(entries)}
                        break
                signal = safety.find_block_signal(session.warnings)
                if signal:
                    raise BlockDetected(f"discover {tab}: {signal[:500]}")
            warnings = list(session.warnings)
    except BlockDetected as exc:
        writer.abort()
        result.exit_code = safety.EXIT_TEMPFAIL
        result.block_reason = exc.reason
        safety.write_cooldown(
            rt.paths,
            exc.reason,
            rt.clock.now(),
            yt_dlp_version=getattr(yc, "version", None),
            command="discover",
        )
        log.error("BLOCK SIGNAL during discover: %s", exc.reason)
        log.error(
            "Wrote %s. discover/fetch will refuse to run until it is cleared.", rt.paths.cooldown
        )
        return result
    except BaseException:
        writer.abort()
        raise

    now_iso = iso(rt.clock.now())
    seen: set[str] = set()
    for tab, entries in listings.items():
        item_type = TAB_ITEM_TYPES[tab]
        count = 0
        for rank, entry in enumerate(entries):
            item_id = entry.get("id")
            if not isinstance(item_id, str) or not item_id:
                continue
            seen.add(item_id)
            count += 1
            if rt.manifest.upsert_listed_item(
                SOURCE_TYPE,
                item_id,
                now_iso,
                item_type=item_type,
                title=entry.get("title"),
                duration_s=_int_or_none(entry.get("duration")),
                upload_date=_upload_date(entry),
                availability=_availability(entry),
                listing_rank=rank,
            ):
                result.new_items += 1
        result.counts_by_type[item_type] = count

    if result.errors:
        log.warning(
            "discover: %d tab(s) failed (%s); not marking anything missing this time",
            len(result.errors),
            ", ".join(sorted(result.errors)),
        )
        result.exit_code = 1
    else:
        result.missing = rt.manifest.mark_missing(SOURCE_TYPE, seen, now_iso)

    record = {
        "kind": "channel_listing",
        "source_type": SOURCE_TYPE,
        "channel_handle": handle,
        "fetched_at": iso(now),
        "finished_at": now_iso,
        **tool_versions(getattr(yc, "version", None)),
        "run_id": rt.run_id,
        "tabs": tab_results,
        "warnings": warnings,
    }
    if listings:
        final = writer.finalize(record)
        result.snapshot = rt.paths.rel(final)
    else:
        writer.abort()
    log.info(
        "discover: %s; %d new; %d missing from listing",
        ", ".join(f"{n} {t}s" for t, n in sorted(result.counts_by_type.items()))
        or "nothing listed",
        result.new_items,
        len(result.missing),
    )
    return result

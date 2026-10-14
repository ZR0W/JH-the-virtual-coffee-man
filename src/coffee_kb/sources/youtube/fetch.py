"""``fetch``: per-video metadata, captions, SponsorBlock, optional audio (docs/DESIGN.md §4.2, §6.2).

Each fetched video becomes one write-once snapshot directory. The caller (cli.py) checks the
cooldown latch and takes the run lock before calling ``run_fetch``; this module enforces the
daily cap, pacing, retries, and stop-on-block.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, TypeVar

from coffee_kb import manifest as mf
from coffee_kb import safety
from coffee_kb.clock import iso, parse_iso
from coffee_kb.paths import check_item_id
from coffee_kb.runtime import Runtime, tool_versions
from coffee_kb.safety import BlockDetected, ErrorKind
from coffee_kb.snapshots import SnapshotWriter
from coffee_kb.sources.youtube import sponsorblock

log = logging.getLogger(__name__)

SOURCE_TYPE = "youtube"
WATCH_URL = "https://www.youtube.com/watch?v={}"
SKIP_AVAILABILITY = {"unavailable", "skipped_restricted"}
# An incomplete video is retried automatically on later runs at most this many times in total
# (e.g. auto captions that never appear); after that only `fetch --video ID` retries it.
MAX_FAILED_ATTEMPTS = 3
RESTRICTED_AVAILABILITY = {"subscriber_only", "premium_only", "needs_auth"}
CAPTION_FORMATS = ("json3", "vtt")
CAPTION_KINDS = (
    # (track name, component, info.json key)
    ("manual", "captions_manual", "subtitles"),
    ("auto", "captions_auto", "automatic_captions"),
)
YOUTUBE_COMPONENTS = ("metadata", "captions_manual", "captions_auto")

T = TypeVar("T")


def required_components(audio: bool) -> list[str]:
    """Components that must be ok/absent for a video to count as complete (and be skipped).

    SponsorBlock is deliberately not required: an outage of that community service must never
    cause YouTube re-fetches. SponsorBlock errors are surfaced by ``report`` and can be fixed
    with ``fetch --refresh --video ID``.
    """
    comps = list(YOUTUBE_COMPONENTS)
    if audio:
        comps.append(mf.COMPONENT_AUDIO)
    return comps


@dataclass
class FetchOptions:
    limit: int | None = None
    videos: list[str] = field(default_factory=list)
    since: str | None = None
    item_type: str | None = None
    audio: bool = False
    refresh: bool = False
    dry_run: bool = False
    wait_for_cap: bool = False  # wait for rolling-cap slots instead of exiting (daily timer)


@dataclass
class VideoOutcome:
    item_id: str
    status: str  # 'ok' | 'partial' | 'skipped' | 'error'
    detail: str | None = None
    snapshot: str | None = None
    unknown_error: bool = False
    block_reason: str | None = None
    title: str | None = None


@dataclass
class FetchSummary:
    exit_code: int = 0
    planned: list[str] = field(default_factory=list)
    already_complete: int = 0
    allowance: int = 0
    outcomes: list[VideoOutcome] = field(default_factory=list)
    block_reason: str | None = None
    message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for o in self.outcomes:
            counts[o.status] = counts.get(o.status, 0) + 1
        return {
            "planned": len(self.planned),
            "already_complete": self.already_complete,
            "allowance_at_start": self.allowance,
            "counts": counts,
            "errors": {
                o.item_id: o.detail for o in self.outcomes if o.status in ("error", "partial")
            },
            "block_reason": self.block_reason,
            "message": self.message,
        }


class _ComponentFailure(Exception):
    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def _short(msg: str, n: int = 500) -> str:
    msg = " ".join(str(msg).split())
    return msg if len(msg) <= n else msg[: n - 3] + "..."


# --- candidate selection (offline) ----------------------------------------------------------


def retry_exhausted(manifest: mf.Manifest, required: list[str]) -> list[str]:
    """Incomplete items that hit MAX_FAILED_ATTEMPTS and are no longer retried automatically."""
    counts = manifest.failed_attempt_counts(SOURCE_TYPE)
    return sorted(
        i
        for i, n in counts.items()
        if n >= MAX_FAILED_ATTEMPTS and not manifest.is_complete(SOURCE_TYPE, i, required)
    )


def select_candidates(rt: Runtime, opts: FetchOptions) -> tuple[list[str], int]:
    """Return (item ids to fetch in order, number skipped as already complete)."""
    m = rt.manifest
    required = required_components(opts.audio)
    if opts.videos:
        ids: list[str] = []
        for vid in opts.videos:
            check_item_id(vid)
            if vid not in ids:
                ids.append(vid)
    else:
        ids = []
        for row in m.items_newest_first(SOURCE_TYPE):
            if row["listing_status"] == mf.MISSING_FROM_LISTING:
                continue
            if opts.item_type and row["item_type"] != opts.item_type:
                continue
            if opts.since and (row["upload_date"] is None or row["upload_date"] < opts.since):
                continue
            if not opts.refresh and row["availability"] in SKIP_AVAILABILITY:
                continue
            ids.append(row["item_id"])
    if opts.refresh:
        return ids, 0
    todo = [i for i in ids if not m.is_complete(SOURCE_TYPE, i, required)]
    skipped_complete = len(ids) - len(todo)
    if not opts.videos:
        exhausted = set(retry_exhausted(m, required))
        if exhausted & set(todo):
            log.info(
                "%d incomplete video(s) skipped after %d failed attempts (see `report`; "
                "retry one with `fetch --video ID`)",
                len(exhausted & set(todo)),
                MAX_FAILED_ATTEMPTS,
            )
            todo = [i for i in todo if i not in exhausted]
    return todo, skipped_complete


# --- caption track choice -------------------------------------------------------------------


def choose_track(
    tracks: dict[str, Any] | None, languages: tuple[str, ...], auto: bool
) -> tuple[str, list[dict[str, Any]]] | None:
    """Pick one English track. For auto captions prefer the original-language ASR track
    (yt-dlp labels it ``<lang>-orig``) and never pick a machine translation of another
    language."""
    if not tracks:
        return None
    usable = {k: v for k, v in tracks.items() if isinstance(v, list) and v}
    bases = {lang.split("-")[0] for lang in languages}
    if auto:
        has_orig = any(k.endswith("-orig") for k in usable)
        for lang in languages:
            for key in (f"{lang}-orig",) if has_orig else (f"{lang}-orig", lang):
                if key in usable:
                    return key, usable[key]
        for key in sorted(usable):
            if key.split("-")[0] in bases and (key.endswith("-orig") or not has_orig):
                return key, usable[key]
        return None
    for lang in languages:
        if lang in usable:
            return lang, usable[lang]
    for key in sorted(usable):
        if key.split("-")[0] in bases:
            return key, usable[key]
    return None


def _valid_caption(fmt: str, body: bytes) -> str | None:
    """Return None if the body is a usable caption file, else a reason."""
    if not body.strip():
        return "empty response"
    if fmt == "json3":
        try:
            data = json.loads(body)
        except ValueError:
            return "response is not JSON"
        if not isinstance(data, dict) or "events" not in data:
            return "JSON without 'events'"
        return None
    text = body.decode("utf-8", errors="replace").lstrip("﻿").lstrip()
    if not text.startswith("WEBVTT"):
        return "response is not WebVTT"
    return None


# --- per-video fetch ------------------------------------------------------------------------


class VideoFetcher:
    def __init__(self, rt: Runtime, audio: bool) -> None:
        self.rt = rt
        self.audio = audio
        self.fc = rt.cfg.fetch

    def _call(self, fn: Callable[[], T], what: str, item_id: str) -> T:
        """Run one YouTube request. Block → BlockDetected (no retry). Transient → at most
        ``max_transient_retries`` retries, ``transient_retry_wait_s`` apart."""
        retries = 0
        while True:
            try:
                return fn()
            except BlockDetected:
                raise
            except Exception as exc:
                kind, msg = safety.classify_exception(exc)
                if kind is ErrorKind.BLOCK:
                    raise BlockDetected(f"{what}: {_short(msg)}", item_id=item_id) from exc
                if kind is ErrorKind.TRANSIENT and retries < self.fc.max_transient_retries:
                    retries += 1
                    log.warning(
                        "%s %s: transient error (%s); retry %d",
                        item_id,
                        what,
                        _short(msg, 120),
                        retries,
                    )
                    self.rt.pacer.wait_before_retry()
                    continue
                raise _ComponentFailure(kind, _short(msg)) from exc

    def fetch(self, item_id: str) -> VideoOutcome:
        rt = self.rt
        m = rt.manifest
        started = rt.clock.now()
        t0 = rt.clock.monotonic()
        m.ensure_item(SOURCE_TYPE, item_id, iso(started))
        attempt_id = m.record_attempt_start(SOURCE_TYPE, item_id, rt.run_id, iso(started))
        writer = SnapshotWriter(rt.paths.snapshots_dir(SOURCE_TYPE, item_id), started)
        try:
            outcome = self._fetch_into(item_id, writer, started, t0)
        except BaseException as exc:
            writer.abort()
            m.finish_attempt(attempt_id, rt.now_iso(), "interrupted", _short(repr(exc)))
            raise
        m.finish_attempt(
            attempt_id,
            rt.now_iso(),
            "blocked" if outcome.block_reason else outcome.status,
            outcome.block_reason or outcome.detail,
        )
        return outcome

    def _fetch_into(
        self, item_id: str, writer: SnapshotWriter, started: datetime, t0: float
    ) -> VideoOutcome:
        rt = self.rt
        url = WATCH_URL.format(item_id)
        comps: dict[str, dict[str, Any]] = {}
        requested = [*YOUTUBE_COMPONENTS, "sponsorblock"] + (
            [mf.COMPONENT_AUDIO] if self.audio else []
        )
        request_interval = rt.pacer.request_interval()
        block: str | None = None
        unknown = False
        info: dict[str, Any] | None = None
        warnings: list[str] = []

        with rt.youtube.session(request_interval) as session:
            # 1. metadata ----------------------------------------------------------------
            try:
                info = self._call(lambda: session.extract_info(url), "metadata", item_id)
            except BlockDetected as exc:
                block = exc.reason
                comps["metadata"] = {"status": mf.STATUS_ERROR, "detail": f"block signal: {block}"}
            except _ComponentFailure as f:
                writer.abort()
                return self._record_unfetched(item_id, f)
            warnings = session.warnings

            if info is not None:
                skip = self._skip_reason(info)
                if skip is not None:
                    writer.abort()
                    outcome = self._record_skip(item_id, *skip)
                    outcome.block_reason = safety.find_block_signal(warnings)
                    return outcome
                writer.write_json("info.json", info)
                comps["metadata"] = {"status": mf.STATUS_OK, "file": "info.json"}
                block = safety.find_block_signal(warnings)

            # 2. captions ----------------------------------------------------------------
            for kind, comp, key in CAPTION_KINDS:
                if block or info is None:
                    break
                try:
                    comps[comp] = self._fetch_caption(session, writer, item_id, info, kind, key)
                except BlockDetected as exc:
                    block = exc.reason
                    comps[comp] = {"status": mf.STATUS_ERROR, "detail": f"block signal: {block}"}
                    break
                if comps[comp].get("kind") is ErrorKind.UNKNOWN:
                    unknown = True
                block = safety.find_block_signal(warnings)

            # 3. audio (opt-in) ------------------------------------------------------------
            if self.audio and not block and info is not None:
                try:
                    path = self._call(
                        lambda: session.download_audio(url, writer.tmp_dir), "audio", item_id
                    )
                    writer.files.append(path.name)
                    comps[mf.COMPONENT_AUDIO] = {"status": mf.STATUS_OK, "file": path.name}
                except BlockDetected as exc:
                    block = exc.reason
                    comps[mf.COMPONENT_AUDIO] = {
                        "status": mf.STATUS_ERROR,
                        "detail": f"block signal: {block}",
                    }
                except _ComponentFailure as f:
                    unknown = unknown or f.kind is ErrorKind.UNKNOWN
                    comps[mf.COMPONENT_AUDIO] = {"status": mf.STATUS_ERROR, "detail": f.message}
                block = block or safety.find_block_signal(warnings)
            warnings = list(session.warnings)

        # 4. SponsorBlock (not YouTube; skipped once a block is seen, the run stops now) ------
        if not block and info is not None:
            sb = sponsorblock.fetch_segments(
                rt.http, rt.cfg.sponsorblock.base_url, item_id, rt.cfg.sponsorblock.categories
            )
            entry: dict[str, Any] = {"status": sb.status, "http_status": sb.http_status}
            if sb.body is not None:
                writer.write_bytes("sponsorblock.json", sb.body)
                entry["file"] = "sponsorblock.json"
                entry["segments"] = sb.segment_count
            if sb.detail:
                entry["detail"] = sb.detail
            comps["sponsorblock"] = entry

        for name in requested:
            comps.setdefault(
                name,
                {
                    "status": mf.STATUS_PENDING,
                    "detail": "not attempted (run stopped on block signal)",
                }
                if block
                else {"status": mf.STATUS_PENDING},
            )

        snapshot_rel: str | None = None
        if info is not None:
            record = {
                "kind": "item_fetch",
                "source_type": SOURCE_TYPE,
                "item_id": item_id,
                "url": url,
                "fetched_at": iso(started),
                "finished_at": rt.now_iso(),
                "duration_ms": int((rt.clock.monotonic() - t0) * 1000),
                **tool_versions(getattr(rt.youtube, "version", None)),
                "run_id": rt.run_id,
                "args": rt.args,
                "requested_components": requested,
                "pacing": {
                    "request_interval_s": round(request_interval, 2),
                    "sleep_before_subtitles_s": self.fc.sleep_before_subtitles_s,
                },
                "components": {k: _jsonable(v) for k, v in comps.items()},
                "warnings": warnings,
                "block_signal": block,
            }
            final = writer.finalize(record)
            snapshot_rel = rt.paths.rel(final)
        else:
            writer.abort()

        self._update_manifest(item_id, info, comps, snapshot_rel)

        yt_errors = [c for c in requested if comps[c]["status"] == mf.STATUS_ERROR]
        if info is None:
            status = "error"
        elif yt_errors:
            status = "partial"
        else:
            status = "ok"
        detail = "; ".join(f"{c}: {comps[c].get('detail')}" for c in yt_errors) or None
        return VideoOutcome(
            item_id=item_id,
            status=status,
            detail=detail,
            snapshot=snapshot_rel,
            unknown_error=unknown,
            block_reason=block,
            title=(info or {}).get("title"),
        )

    def _skip_reason(self, info: dict[str, Any]) -> tuple[str, str, str] | None:
        """(availability, component status, detail) if the video must not be collected."""
        avail = info.get("availability")
        if avail in RESTRICTED_AVAILABILITY:
            return "skipped_restricted", mf.STATUS_SKIPPED, f"availability={avail}"
        if avail == "private":
            return "unavailable", mf.STATUS_SKIPPED, "availability=private"
        live = info.get("live_status")
        if live in ("is_upcoming", "is_live"):
            return "upcoming", mf.STATUS_PENDING, f"live_status={live}; retried on a later run"
        return None

    def _fetch_caption(
        self,
        session: Any,
        writer: SnapshotWriter,
        item_id: str,
        info: dict[str, Any],
        kind: str,
        key: str,
    ) -> dict[str, Any]:
        langs = self.rt.cfg.youtube.caption_languages
        chosen = choose_track(info.get(key), langs, auto=(kind == "auto"))
        if chosen is None:
            pot = [w for w in session.warnings if "po token" in w.lower()]
            if pot:
                # Not "absent": YouTube withheld it. Never fail silently.
                return {"status": mf.STATUS_ERROR, "detail": _short(pot[0])}
            return {"status": mf.STATUS_ABSENT}
        lang, formats = chosen
        by_ext = {f.get("ext"): f for f in formats if isinstance(f, dict) and f.get("url")}
        problems: list[str] = []
        last_kind: ErrorKind | None = None
        for ext in CAPTION_FORMATS:
            fmt = by_ext.get(ext)
            if fmt is None:
                continue
            self.rt.pacer.wait_before_subtitle()
            what = f"captions {lang}.{kind}.{ext}"
            try:
                body = self._call(lambda fmt=fmt: session.fetch_bytes(fmt["url"]), what, item_id)
            except _ComponentFailure as f:
                problems.append(f"{ext}: {f.message}")
                last_kind = f.kind
                continue
            if safety.looks_like_html(body):
                raise BlockDetected(
                    f"{what}: HTML where JSON was expected (CAPTCHA/consent page?)", item_id=item_id
                )
            reason = _valid_caption(ext, body)
            if reason:
                # A 200 response with unusable content (e.g. captions not generated yet) is not
                # a transport error, so it doesn't count toward the unknown-error streak.
                head = body[:80].decode("utf-8", errors="replace")
                problems.append(f"{ext}: {reason} ({len(body)} bytes, starts {head!r})")
                continue
            rel = f"captions/{lang}.{kind}.{ext}"
            writer.write_bytes(rel, body)
            return {"status": mf.STATUS_OK, "file": rel, "lang": lang, "format": ext}
        if not problems:
            return {
                "status": mf.STATUS_ERROR,
                "detail": f"track {lang} offered no {'/'.join(CAPTION_FORMATS)} format "
                f"(have: {', '.join(sorted(str(k) for k in by_ext)) or 'none'})",
                "lang": lang,
            }
        offered = ", ".join(sorted(str(k) for k in by_ext)) or "none"
        return {
            "status": mf.STATUS_ERROR,
            "detail": f"{'; '.join(problems)} [formats offered: {offered}]",
            "lang": lang,
            "kind": last_kind,
        }

    # --- manifest updates -----------------------------------------------------------------

    def _record_unfetched(self, item_id: str, f: _ComponentFailure) -> VideoOutcome:
        if f.kind is ErrorKind.RESTRICTED:
            return self._record_skip(item_id, "skipped_restricted", mf.STATUS_SKIPPED, f.message)
        if f.kind is ErrorKind.UNAVAILABLE:
            return self._record_skip(item_id, "unavailable", mf.STATUS_SKIPPED, f.message)
        if f.kind is ErrorKind.UPCOMING:
            return self._record_skip(item_id, "upcoming", mf.STATUS_PENDING, f.message)
        now = self.rt.now_iso()
        m = self.rt.manifest
        m.set_component(SOURCE_TYPE, item_id, "metadata", mf.STATUS_ERROR, now, detail=f.message)
        return VideoOutcome(
            item_id=item_id,
            status="error",
            detail=f"metadata: {f.message}",
            unknown_error=f.kind is ErrorKind.UNKNOWN,
        )

    def _record_skip(
        self, item_id: str, availability: str, status: str, detail: str
    ) -> VideoOutcome:
        now = self.rt.now_iso()
        m = self.rt.manifest
        m.update_item(SOURCE_TYPE, item_id, availability=availability)
        comps = [*YOUTUBE_COMPONENTS, "sponsorblock"] + ([mf.COMPONENT_AUDIO] if self.audio else [])
        for comp in comps:
            m.set_component(SOURCE_TYPE, item_id, comp, status, now, detail=_short(detail))
        log.info("%s: %s (%s)", item_id, availability, _short(detail, 160))
        return VideoOutcome(
            item_id=item_id, status="skipped", detail=f"{availability}: {_short(detail)}"
        )

    def _update_manifest(
        self,
        item_id: str,
        info: dict[str, Any] | None,
        comps: dict[str, dict[str, Any]],
        snapshot_rel: str | None,
    ) -> None:
        m = self.rt.manifest
        now = self.rt.now_iso()
        if info is not None:
            fields: dict[str, Any] = {
                "title": info.get("title"),
                "availability": info.get("availability") or "public",
                "latest_snapshot": snapshot_rel,
            }
            ud = info.get("upload_date")
            if isinstance(ud, str) and len(ud) == 8 and ud.isdigit():
                fields["upload_date"] = f"{ud[:4]}-{ud[4:6]}-{ud[6:]}"
            if isinstance(info.get("duration"), int | float):
                fields["duration_s"] = int(info["duration"])
            row = m.get_item(SOURCE_TYPE, item_id)
            if (row is None or row["item_type"] is None) and info.get("live_status") in (
                "was_live",
                "post_live",
            ):
                fields["item_type"] = "stream"
            m.update_item(SOURCE_TYPE, item_id, **fields)
        for name, entry in comps.items():
            m.set_component(
                SOURCE_TYPE,
                item_id,
                name,
                entry["status"],
                now,
                detail=entry.get("detail"),
                snapshot=snapshot_rel,
            )


def _jsonable(entry: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.value if isinstance(v, ErrorKind) else v) for k, v in entry.items()}


# --- the run --------------------------------------------------------------------------------


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def run_fetch(rt: Runtime, opts: FetchOptions) -> FetchSummary:
    """Caller must have checked the cooldown latch and taken the run lock (unless dry-run)."""
    fc = rt.cfg.fetch
    summary = FetchSummary()
    now = rt.clock.now()
    summary.allowance = safety.remaining_allowance(rt.manifest, fc, now)

    if not opts.videos and not rt.manifest.items(SOURCE_TYPE):
        summary.message = "manifest has no items: run `coffee-kb discover` first, or use --video ID"
        log.warning(summary.message)
        return summary

    todo, complete = select_candidates(rt, opts)
    summary.already_complete = complete
    if complete:
        log.info("%d video(s) already complete; skipped without network access", complete)
    if not todo:
        summary.message = "nothing to fetch"
        log.info(summary.message)
        return summary

    wait_budget_s = fc.max_wait_for_cap_minutes * 60 if opts.wait_for_cap else 0.0
    if summary.allowance <= 0:
        nxt = safety.next_eligible_time(rt.manifest, fc, now)
        if not (wait_budget_s and (nxt - now).total_seconds() <= wait_budget_s):
            summary.message = (
                f"daily cap reached ({fc.max_videos_per_24h} video fetch attempts per 24 h); "
                f"next eligible at {iso(nxt)}"
            )
            log.info(summary.message)
            return summary

    # With --wait-for-cap the run plans a full batch and waits for each rolling-window slot to
    # free up; the cap is still checked before every single video.
    cap = fc.max_videos_per_24h if opts.wait_for_cap else summary.allowance
    if opts.limit is not None and opts.limit > cap:
        log.info("--limit %d clamped to the remaining daily allowance of %d", opts.limit, cap)
    n = min(cap, opts.limit) if opts.limit is not None else cap
    if len(todo) > n:
        log.info("%d video(s) need fetching; this run does %d (daily cap / --limit)", len(todo), n)
    summary.planned = todo[:n]

    est_per_video = (
        fc.sleep_between_videos_s
        + fc.sleep_between_videos_jitter_s / 2
        + 2 * fc.sleep_before_subtitles_s
        + 3 * (fc.sleep_between_requests_s + fc.sleep_between_requests_max_s) / 2
    )
    if opts.dry_run:
        for vid in summary.planned:
            row = rt.manifest.get_item(SOURCE_TYPE, vid)
            title = row["title"] if row is not None else None
            print(f"would fetch {vid}  {title or ''}".rstrip())
        print(
            f"{len(summary.planned)} video(s); estimated duration "
            f"{_fmt_duration(est_per_video * len(summary.planned))} at the configured pacing"
        )
        summary.message = "dry run: no network access"
        return summary

    fetcher = VideoFetcher(rt, opts.audio)
    streak = safety.UnknownErrorStreak()
    total = len(summary.planned)
    log.info(
        "fetching %d video(s), one at a time; expect roughly %s",
        total,
        _fmt_duration(est_per_video * total),
    )
    run_t0 = rt.clock.monotonic()
    waited = 0.0
    try:
        for i, vid in enumerate(summary.planned):
            _pace_before_video(rt, first=(i == 0))
            if safety.remaining_allowance(rt.manifest, fc, rt.clock.now()) <= 0:
                waited_now = _wait_for_cap_slot(rt, wait_budget_s - waited)
                waited += waited_now
                if safety.remaining_allowance(rt.manifest, fc, rt.clock.now()) <= 0:
                    summary.message = "daily cap reached during the run"
                    log.info(summary.message)
                    break
            safety.ensure_no_cooldown(rt.paths)
            outcome = fetcher.fetch(vid)
            summary.outcomes.append(outcome)
            done = i + 1
            elapsed = rt.clock.monotonic() - run_t0
            eta = elapsed / done * (total - done)
            log.info(
                "[%d/%d] %s %s — %s%s | ETA %s",
                done,
                total,
                vid,
                (outcome.title or "")[:60],
                outcome.status,
                f" ({_short(outcome.detail, 160)})" if outcome.detail else "",
                _fmt_duration(eta),
            )
            if outcome.block_reason:
                raise BlockDetected(outcome.block_reason, item_id=vid)
            if outcome.unknown_error:
                streak.record_unknown(vid, outcome.detail or "unknown error")
            elif outcome.status in ("ok", "partial"):
                streak.record_success()
    except BlockDetected as exc:
        latch = safety.write_cooldown(
            rt.paths,
            exc.reason,
            rt.clock.now(),
            item_id=exc.item_id,
            yt_dlp_version=getattr(rt.youtube, "version", None),
            command="fetch",
        )
        summary.block_reason = exc.reason
        summary.exit_code = safety.EXIT_TEMPFAIL
        log.error("BLOCK SIGNAL: %s", exc.reason)
        log.error(
            "Stopped immediately and wrote %s. discover/fetch refuse to run until the user "
            "clears it (at least %d h from %s). Do not retry; waiting is the fix.",
            rt.paths.cooldown,
            math.ceil(fc.cooldown_min_hours),
            latch.detected_at,
        )
        return summary

    if any(o.status == "error" for o in summary.outcomes):
        summary.exit_code = 1
    return summary


def _wait_for_cap_slot(rt: Runtime, budget_s: float) -> float:
    """Sleep until the rolling 24 h window frees a slot, if that fits in ``budget_s``.
    Returns the seconds slept (0 if the wait would exceed the budget)."""
    if budget_s <= 0:
        return 0.0
    now = rt.clock.now()
    nxt = safety.next_eligible_time(rt.manifest, rt.cfg.fetch, now)
    wait = (nxt - now).total_seconds() + 1
    if wait > budget_s:
        log.info("next cap slot frees at %s, beyond the wait budget; stopping", iso(nxt))
        return 0.0
    log.info(
        "daily cap full: waiting %s for the next slot (frees at %s)", _fmt_duration(wait), iso(nxt)
    )
    rt.clock.sleep(wait)
    return wait


def _pace_before_video(rt: Runtime, first: bool) -> None:
    """60–120 s between videos, also across runs (measured from the last recorded attempt)."""
    if not first:
        rt.pacer.wait_between_videos()
        return
    last = rt.manifest.last_attempt_time(SOURCE_TYPE)
    if last is None:
        return
    delay = rt.pacer.between_videos_delay()
    remaining = (parse_iso(last) + timedelta(seconds=delay) - rt.clock.now()).total_seconds()
    if remaining > 0:
        log.info("pacing: last video fetch was moments ago; sleeping %.0f s", remaining)
        rt.clock.sleep(remaining)

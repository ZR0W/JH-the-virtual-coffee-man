"""``report``: coverage, errors and safety state (docs/DESIGN.md §6.4). Offline."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from coffee_kb import __version__, safety
from coffee_kb.clock import iso
from coffee_kb.config import Config
from coffee_kb.manifest import (
    DONE_STATUSES,
    MISSING_FROM_LISTING,
    STATUS_ERROR,
    STATUS_OK,
    Manifest,
)
from coffee_kb.normalize.pipeline import read_jsonl
from coffee_kb.paths import DataPaths
from coffee_kb.sources.youtube.fetch import SKIP_AVAILABILITY, required_components

SOURCE_TYPE = "youtube"


def _transcript_words(paths: DataPaths, item_id: str) -> tuple[int, str | None]:
    for track in ("manual", "auto"):
        path = paths.transcript_path(SOURCE_TYPE, item_id, track)
        if path.exists():
            return sum(len(r.get("text", "").split()) for r in read_jsonl(path)), track
    return 0, None


def _sponsor_count(paths: DataPaths, snapshot_rel: str) -> int | None:
    path = paths.resolve(snapshot_rel) / "sponsorblock.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    segs = data.get("segments", []) if isinstance(data, dict) else data
    return len(segs or [])


def build_report(
    cfg: Config, paths: DataPaths, manifest: Manifest, now: datetime
) -> dict[str, Any]:
    items = manifest.items(SOURCE_TYPE)
    comps_by_item: dict[str, dict[str, Any]] = defaultdict(dict)
    for row in manifest.all_components(SOURCE_TYPE):
        comps_by_item[row["item_id"]][row["component"]] = row

    by_type = Counter(r["item_type"] or "unknown" for r in items)
    by_availability = Counter(r["availability"] or "not_fetched_yet" for r in items)
    by_listing = Counter(r["listing_status"] for r in items)
    fetched = [r for r in items if r["latest_snapshot"]]
    required = required_components(cfg.fetch.audio)

    caption_cov = Counter()
    sponsor_with = sponsor_without = sponsor_unknown = 0
    total_seconds = 0
    words_total = 0
    words_by_track = Counter()
    for r in fetched:
        c = comps_by_item[r["item_id"]]
        manual = (
            c.get("captions_manual") is not None and c["captions_manual"]["status"] == STATUS_OK
        )
        auto = c.get("captions_auto") is not None and c["captions_auto"]["status"] == STATUS_OK
        key = (
            "manual+auto"
            if manual and auto
            else "manual_only"
            if manual
            else "auto_only"
            if auto
            else "none"
        )
        caption_cov[key] += 1
        n = _sponsor_count(paths, r["latest_snapshot"])
        if n is None:
            sponsor_unknown += 1
        elif n > 0:
            sponsor_with += 1
        else:
            sponsor_without += 1
        total_seconds += r["duration_s"] or 0
        w, track = _transcript_words(paths, r["item_id"])
        words_total += w
        if track:
            words_by_track[track] += 1

    errors: dict[str, list[dict[str, str]]] = defaultdict(list)
    for item_id, comps in sorted(comps_by_item.items()):
        for name, row in sorted(comps.items()):
            if row["status"] == STATUS_ERROR:
                errors[name].append({"item_id": item_id, "detail": row["detail"] or ""})

    normalized = {
        r["item_id"]: r.get("snapshot") for r in read_jsonl(paths.items_jsonl(SOURCE_TYPE))
    }
    stale = sorted(
        r["item_id"] for r in fetched if normalized.get(r["item_id"]) != r["latest_snapshot"]
    )

    def complete(r: Any) -> bool:
        c = comps_by_item[r["item_id"]]
        return bool(r["latest_snapshot"]) and all(
            c.get(n) is not None and c[n]["status"] in DONE_STATUSES for n in required
        )

    remaining = [
        r
        for r in items
        if r["listing_status"] != MISSING_FROM_LISTING
        and (r["availability"] not in SKIP_AVAILABILITY)
        and not complete(r)
    ]
    state = safety.safety_state(paths, cfg.fetch, manifest, now)
    state["remaining_to_fetch"] = len(remaining)
    state["estimated_days_remaining"] = math.ceil(len(remaining) / cfg.fetch.max_videos_per_24h)

    last_runs = {}
    for cmd in ("discover", "fetch", "normalize"):
        row = manifest.last_run(cmd)
        if row is not None:
            last_runs[cmd] = {
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "exit_code": row["exit_code"],
            }

    return {
        "generated_at": iso(now),
        "coffee_kb_version": __version__,
        "items": {
            "total": len(items),
            "by_type": dict(sorted(by_type.items())),
            "by_availability": dict(sorted(by_availability.items())),
            "by_listing_status": dict(sorted(by_listing.items())),
            "fetched": len(fetched),
            "complete": sum(1 for r in items if complete(r)),
        },
        "captions": {
            "manual+auto": caption_cov["manual+auto"],
            "auto_only": caption_cov["auto_only"],
            "manual_only": caption_cov["manual_only"],
            "none": caption_cov["none"],
        },
        "sponsorblock": {
            "with_segments": sponsor_with,
            "without_segments": sponsor_without,
            "not_fetched": sponsor_unknown,
        },
        "totals": {
            "hours_fetched": round(total_seconds / 3600, 2),
            "transcript_words": words_total,
            "transcript_source": dict(words_by_track),
        },
        "errors": {k: v for k, v in sorted(errors.items())},
        "stale_normalization": stale,
        "safety": state,
        "last_runs": last_runs,
    }


def render_text(rep: dict[str, Any]) -> str:
    out: list[str] = []
    add = out.append
    it = rep["items"]
    add(f"coffee-kb report ({rep['generated_at']}, coffee-kb {rep['coffee_kb_version']})")
    add("")
    add(f"Items: {it['total']} in manifest, {it['fetched']} fetched, {it['complete']} complete")
    add("  by type:         " + _kv(it["by_type"]))
    add("  by availability: " + _kv(it["by_availability"]))
    add("  by listing:      " + _kv(it["by_listing_status"]))
    add("")
    c = rep["captions"]
    add(
        f"Captions (fetched items): manual+auto {c['manual+auto']}, auto-only {c['auto_only']}, "
        f"manual-only {c['manual_only']}, none {c['none']}"
    )
    s = rep["sponsorblock"]
    add(
        f"SponsorBlock: {s['with_segments']} with segments, {s['without_segments']} without, "
        f"{s['not_fetched']} not fetched"
    )
    t = rep["totals"]
    add(
        f"Totals: {t['hours_fetched']} h of video fetched; {t['transcript_words']} transcript words "
        f"(manual preferred, auto fallback; {_kv(t['transcript_source']) or 'no transcripts'})"
    )
    add("")
    if rep["errors"]:
        add("Errors by component:")
        for comp, rows in rep["errors"].items():
            add(f"  {comp}: {len(rows)}")
            for r in rows:
                add(f"    {r['item_id']}: {r['detail'][:200]}")
    else:
        add("Errors: none")
    if rep["stale_normalization"]:
        add(
            f"Fetched but not normalized ({len(rep['stale_normalization'])}): "
            + ", ".join(rep["stale_normalization"][:20])
            + (" ..." if len(rep["stale_normalization"]) > 20 else "")
            + "  → run `coffee-kb normalize`"
        )
    else:
        add("Normalization: up to date")
    add("")
    st = rep["safety"]
    add("Safety state:")
    if st["cooldown"]:
        cd = st["cooldown"]
        add(f"  COOLDOWN LATCH ACTIVE since {cd['detected_at']}: {cd['reason'][:300]}")
        add(f"  can be cleared from {cd['can_clear_from']} (user only: coffee-kb clear-cooldown)")
    else:
        add("  cooldown latch: none")
    add(
        f"  fetch attempts in the last 24 h: {st['fetches_last_24h']} / {st['max_videos_per_24h']} "
        f"({st['remaining_allowance']} remaining)"
    )
    add(
        f"  remaining to fetch: {st['remaining_to_fetch']} → about "
        f"{st['estimated_days_remaining']} day(s) at the current cap"
    )
    add(f"  last discover: {st['last_discover'] or 'never'}")
    if rep["last_runs"]:
        add("")
        add("Last runs:")
        for cmd, r in rep["last_runs"].items():
            add(f"  {cmd}: started {r['started_at']}, exit {r['exit_code']}")
    return "\n".join(out)


def _kv(d: dict[str, Any]) -> str:
    return ", ".join(f"{k} {v}" for k, v in d.items())

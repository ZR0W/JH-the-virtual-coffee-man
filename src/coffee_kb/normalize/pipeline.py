"""``normalize``: rebuild ``normalized/`` from the latest raw snapshots (docs/DESIGN.md §5.3–5.5).

A pure function of ``raw/``: it never reads the manifest and never touches the network, so
``normalized/`` can always be deleted and regenerated. Outputs are overwritten atomically.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from coffee_kb import __version__
from coffee_kb.clock import iso, parse_stamp
from coffee_kb.normalize.captions import Cue, parse_caption_file
from coffee_kb.paths import DataPaths, latest_snapshot, list_snapshots

log = logging.getLogger(__name__)

NORMALIZER_VERSION = __version__
SOURCE_TYPE = "youtube"
TRACKS = ("manual", "auto")
SPONSOR_JOIN_CATEGORIES = {"sponsor", "selfpromo"}
TAB_ITEM_TYPES = {"videos": "video", "shorts": "short", "streams": "stream"}


@dataclass
class NormalizeSummary:
    items: int = 0
    transcripts: dict[str, int] = field(default_factory=dict)
    sponsor_segments: int = 0
    skipped: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "transcripts": self.transcripts,
            "sponsor_segments": self.sponsor_segments,
            "skipped": self.skipped,
            "errors": self.errors,
        }


# --- small helpers ----------------------------------------------------------------------------


def _write_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _date(ud: Any) -> str | None:
    if isinstance(ud, str) and len(ud) == 8 and ud.isdigit():
        return f"{ud[:4]}-{ud[4:6]}-{ud[6:]}"
    return None


def _ms(seconds: Any) -> int | None:
    if isinstance(seconds, int | float) and not isinstance(seconds, bool):
        return int(round(seconds * 1000))
    return None


def load_listing_types(paths: DataPaths, source_type: str = SOURCE_TYPE) -> dict[str, str]:
    """item_id → item_type from the newest channel listing snapshot that has each tab."""
    types: dict[str, str] = {}
    found_tabs: set[str] = set()
    for snap in reversed(list_snapshots(paths.channel_snapshots_dir(source_type))):
        for tab, item_type in TAB_ITEM_TYPES.items():
            f = snap / f"{tab}.json"
            if tab in found_tabs or not f.exists():
                continue
            found_tabs.add(tab)
            try:
                listing = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            for e in listing.get("entries") or []:
                if isinstance(e, dict) and isinstance(e.get("id"), str):
                    types.setdefault(e["id"], item_type)
    return types


def _item_type(info: dict[str, Any], listing_types: dict[str, str], item_id: str) -> str | None:
    if info.get("live_status") in ("was_live", "post_live", "is_live"):
        return "stream"
    if item_id in listing_types:
        return listing_types[item_id]
    for key in ("original_url", "webpage_url"):
        if "/shorts/" in str(info.get(key) or ""):
            return "short"
    return None


def chapters_ms(info: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    duration_ms = _ms(info.get("duration"))
    raw = info.get("chapters") or []
    for i, ch in enumerate(raw):
        start = _ms(ch.get("start_time")) or 0
        end = _ms(ch.get("end_time"))
        if end is None:
            end = _ms(raw[i + 1].get("start_time")) if i + 1 < len(raw) else duration_ms
        out.append({"title": ch.get("title"), "start_ms": start, "end_ms": end})
    return out


def sponsor_segments(raw: Any) -> list[dict[str, Any]]:
    """SponsorBlock response (a list, or {"segments": []} for a 404) → plain dicts."""
    segs = raw.get("segments", []) if isinstance(raw, dict) else raw
    out = []
    for s in segs or []:
        seg = s.get("segment") or [None, None]
        out.append(
            {
                "category": s.get("category"),
                "action_type": s.get("actionType"),
                "start_ms": _ms(seg[0]),
                "end_ms": _ms(seg[1]),
                "votes": s.get("votes"),
                "locked": s.get("locked"),
                "uuid": s.get("UUID"),
                "video_duration_ms": _ms(s.get("videoDuration")),
            }
        )
    return out


def _chapter_at(chapters: list[dict[str, Any]], t_ms: int) -> str | None:
    current = None
    for ch in chapters:
        if ch["start_ms"] <= t_ms and (ch["end_ms"] is None or t_ms < ch["end_ms"]):
            current = ch["title"]
    return current


def _in_sponsor(segs: list[dict[str, Any]], cue: Cue) -> bool:
    mid = (cue.start_ms + cue.end_ms) // 2
    for s in segs:
        if s["category"] not in SPONSOR_JOIN_CATEGORIES:
            continue
        if s["start_ms"] is None or s["end_ms"] is None:
            continue
        if s["start_ms"] <= mid < s["end_ms"] or (
            cue.start_ms == cue.end_ms and s["start_ms"] <= cue.start_ms < s["end_ms"]
        ):
            return True
    return False


def find_caption_file(snapshot: Path, track: str) -> Path | None:
    """Prefer json3 over vtt for a track."""
    cap_dir = snapshot / "captions"
    if not cap_dir.is_dir():
        return None
    for ext in ("json3", "vtt"):
        found = sorted(cap_dir.glob(f"*.{track}.{ext}"))
        if found:
            return found[0]
    return None


# --- per item ---------------------------------------------------------------------------------


def normalize_item(
    paths: DataPaths, item_id: str, listing_types: dict[str, str], summary: NormalizeSummary
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    snap = latest_snapshot(paths.snapshots_dir(SOURCE_TYPE, item_id))
    # Clear old transcript outputs for this item; they are rebuilt below.
    tdir = paths.transcripts_dir(SOURCE_TYPE)
    if tdir.is_dir():
        for old in tdir.glob(f"{item_id}.*.jsonl"):
            old.unlink()
    if snap is None:
        summary.skipped[item_id] = "no complete snapshot"
        return None, []
    info_path = snap / "info.json"
    if not info_path.exists():
        summary.skipped[item_id] = "snapshot has no info.json"
        return None, []
    info = json.loads(info_path.read_text(encoding="utf-8"))
    fetch_rec: dict[str, Any] = {}
    if (snap / "fetch.json").exists():
        fetch_rec = json.loads((snap / "fetch.json").read_text(encoding="utf-8"))
    snapshot_rel = paths.rel(snap)
    counts_as_of = fetch_rec.get("fetched_at") or iso(parse_stamp(snap.name))
    prov = {"snapshot": snapshot_rel, "normalizer_version": NORMALIZER_VERSION}

    chapters = chapters_ms(info)
    segs: list[dict[str, Any]] = []
    sb_path = snap / "sponsorblock.json"
    if sb_path.exists():
        segs = sponsor_segments(json.loads(sb_path.read_text(encoding="utf-8")))
    sponsor_rows = [
        {"source_type": SOURCE_TYPE, "item_id": item_id, **s, **prov}
        for s in sorted(segs, key=lambda s: (s["start_ms"] or 0, s["uuid"] or ""))
    ]

    tracks_meta: dict[str, Any] = {}
    for track in TRACKS:
        cap = find_caption_file(snap, track)
        if cap is None:
            tracks_meta[track] = None
            continue
        lang = cap.name.split(f".{track}.")[0]
        fmt = cap.suffix.lstrip(".")
        try:
            cues = parse_caption_file(cap.name, cap.read_bytes(), track)
        except (ValueError, KeyError, TypeError) as exc:
            summary.errors[f"{item_id}.{track}"] = f"{type(exc).__name__}: {exc}"
            tracks_meta[track] = {"lang": lang, "format": fmt, "error": str(exc)}
            continue
        rows = []
        for cue in cues:
            row: dict[str, Any] = {
                "source_type": SOURCE_TYPE,
                "item_id": item_id,
                "track": track,
                "start_ms": cue.start_ms,
                "end_ms": cue.end_ms,
                "text": cue.text,
            }
            if cue.words is not None:
                row["words"] = [{"w": w.w, "start_ms": w.start_ms} for w in cue.words]
            if cue.annotations:
                row["annotations"] = cue.annotations
            row["chapter"] = _chapter_at(chapters, cue.start_ms)
            row["in_sponsor_segment"] = _in_sponsor(segs, cue)
            row["lang"] = lang
            row["caption_format"] = fmt
            row.update(prov)
            rows.append(row)
        _write_jsonl_atomic(paths.transcript_path(SOURCE_TYPE, item_id, track), rows)
        summary.transcripts[track] = summary.transcripts.get(track, 0) + 1
        tracks_meta[track] = {"lang": lang, "format": fmt, "cues": len(rows)}

    item_row = {
        "source_type": SOURCE_TYPE,
        "item_id": item_id,
        "item_type": _item_type(info, listing_types, item_id),
        "title": info.get("title"),
        "description": info.get("description"),
        "upload_date": _date(info.get("upload_date")),
        "duration_s": int(info["duration"])
        if isinstance(info.get("duration"), int | float)
        else None,
        "tags": info.get("tags") or [],
        "categories": info.get("categories") or [],
        "chapters": chapters,
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "counts_as_of": counts_as_of,
        "url": info.get("webpage_url") or f"https://www.youtube.com/watch?v={item_id}",
        "availability": info.get("availability"),
        "live_status": info.get("live_status"),
        "channel": info.get("channel"),
        "channel_id": info.get("channel_id"),
        "language": info.get("language"),
        "caption_tracks": tracks_meta,
        "sponsor_segment_count": len(segs),
        **prov,
    }
    return item_row, sponsor_rows


def run_normalize(paths: DataPaths, video_ids: list[str] | None = None) -> NormalizeSummary:
    summary = NormalizeSummary()
    listing_types = load_listing_types(paths)
    ids = list(video_ids) if video_ids else list(paths.iter_raw_item_ids(SOURCE_TYPE))
    items: dict[str, dict[str, Any]] = {}
    sponsors: dict[str, list[dict[str, Any]]] = {}
    for item_id in ids:
        try:
            row, srows = normalize_item(paths, item_id, listing_types, summary)
        except (OSError, ValueError) as exc:
            summary.errors[item_id] = f"{type(exc).__name__}: {exc}"
            log.error("normalize %s: %s", item_id, exc)
            continue
        if row is not None:
            items[item_id] = row
            sponsors[item_id] = srows

    items_path = paths.items_jsonl(SOURCE_TYPE)
    sponsor_path = paths.sponsor_segments_jsonl(SOURCE_TYPE)
    if video_ids:
        # Partial run: replace only these items' rows.
        keep = set(video_ids)
        old_items = {r["item_id"]: r for r in read_jsonl(items_path) if r["item_id"] not in keep}
        old_items.update(items)
        items = old_items
        old_sponsors: dict[str, list[dict[str, Any]]] = {}
        for r in read_jsonl(sponsor_path):
            if r["item_id"] not in keep:
                old_sponsors.setdefault(r["item_id"], []).append(r)
        old_sponsors.update(sponsors)
        sponsors = old_sponsors
    _write_jsonl_atomic(items_path, [items[k] for k in sorted(items)])
    flat = [r for k in sorted(sponsors) for r in sponsors[k]]
    _write_jsonl_atomic(sponsor_path, flat)
    summary.items = len(items)
    summary.sponsor_segments = len(flat)
    log.info(
        "normalize: %d item(s); transcripts: %s; %d sponsor segment(s); %d skipped; %d error(s)",
        summary.items,
        ", ".join(f"{n} {t}" for t, n in sorted(summary.transcripts.items())) or "none",
        summary.sponsor_segments,
        len(summary.skipped),
        len(summary.errors),
    )
    return summary

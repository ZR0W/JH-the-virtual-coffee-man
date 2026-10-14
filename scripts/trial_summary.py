"""Offline summary of normalized/ for the 20-video trial checklist (checkpoint C).

Reads only data/normalized/ (no network, no manifest writes):

    uv run python scripts/trial_summary.py [--data DIR] [--samples N]

Per video: caption cue/word counts, auto/manual word ratio, chapters, cues inside sponsor
segments, and "boundary repeats": places where an auto cue starts with the last 3+ words of the
previous cue. Leftover rolling-caption duplicates would show up there, but so does natural
speech ("a little bit nervous. I'm a little bit nervous"), so a few per long video are normal;
the auto/manual word ratio (≈1.0 when de-duplication works) is the stronger signal. Then a few
timestamped transcript samples for eyeballing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from coffee_kb.normalize.pipeline import read_jsonl
from coffee_kb.paths import DataPaths

RATIO_RANGE = (0.8, 1.25)


def boundary_repeats(rows: list[dict]) -> int:
    count = 0
    for a, b in zip(rows, rows[1:], strict=False):
        aw, bw = a["text"].lower().split(), b["text"].lower().split()
        for k in range(min(len(aw), len(bw)), 2, -1):
            if aw[-k:] == bw[:k]:
                count += 1
                break
    return count


def ts(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60:02d}:{s % 60:02d}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--data", type=Path, default=Path("data"))
    p.add_argument("--samples", type=int, default=12, help="transcript lines per sample")
    args = p.parse_args(argv)
    paths = DataPaths(args.data)
    items = read_jsonl(paths.items_jsonl("youtube"))
    if not items:
        print("no normalized items; run `uv run coffee-kb normalize` first")
        return 1
    items.sort(key=lambda r: r.get("upload_date") or "", reverse=True)

    print(
        f"{'video':<12} {'type':<6} {'date':<10} {'min':>4} {'man cues/words':>15} "
        f"{'auto cues/words':>16} {'ratio':>5} {'chap':>4} {'spons':>5} {'rep':>3}"
    )
    flags: list[str] = []
    tracks: dict[str, dict[str, list[dict]]] = {}
    for it in items:
        vid = it["item_id"]
        t = {tr: read_jsonl(paths.transcript_path("youtube", vid, tr)) for tr in ("manual", "auto")}
        tracks[vid] = t
        words = {tr: sum(len(r["text"].split()) for r in rows) for tr, rows in t.items()}
        ratio = words["auto"] / words["manual"] if words["manual"] and words["auto"] else None
        best = t["manual"] or t["auto"]
        sponsor = sum(1 for r in best if r["in_sponsor_segment"])
        reps = boundary_repeats(t["auto"])
        chapters = len(it.get("chapters") or [])
        print(
            f"{vid:<12} {str(it.get('item_type'))[:6]:<6} {str(it.get('upload_date')):<10} "
            f"{(it.get('duration_s') or 0) // 60:>4} "
            f"{len(t['manual']):>6}/{words['manual']:<8} {len(t['auto']):>6}/{words['auto']:<9} "
            f"{(f'{ratio:.2f}' if ratio else '-'):>5} {chapters:>4} {sponsor:>5} {reps:>3}"
        )
        if not t["manual"] and not t["auto"]:
            flags.append(f"{vid}: no transcript at all")
        if ratio is not None and not RATIO_RANGE[0] <= ratio <= RATIO_RANGE[1]:
            flags.append(
                f"{vid}: auto/manual word ratio {ratio:.2f} (possible duplication or gaps)"
            )
        if reps:
            flags.append(
                f"{vid}: {reps} auto-caption boundary repeat(s) (natural speech counts too)"
            )
        if best and chapters and all(r["chapter"] is None for r in best):
            flags.append(f"{vid}: has chapters but no cue got one")
    print()
    print("Flags:" if flags else "Flags: none")
    for f in flags:
        print(f"  - {f}")

    samples = [it for it in items if tracks[it["item_id"]]["auto"]][:2]
    for it in samples:
        vid = it["item_id"]
        print(f"\n=== {vid}  {it.get('title')}")
        for tr in ("auto", "manual"):
            rows = tracks[vid][tr]
            if not rows:
                continue
            print(f"--- {tr} (first {args.samples} cues)")
            for r in rows[: args.samples]:
                mark = " [SPONSOR]" if r["in_sponsor_segment"] else ""
                ann = f" {r['annotations']}" if r.get("annotations") else ""
                print(f"  {ts(r['start_ms'])} [{r['chapter']}]{mark} {r['text']}{ann}")
        sponsor_rows = [
            r for r in (tracks[vid]["manual"] or tracks[vid]["auto"]) if r["in_sponsor_segment"]
        ]
        if sponsor_rows:
            print(
                f"--- cues in sponsor segments: {len(sponsor_rows)}, first: "
                f"{ts(sponsor_rows[0]['start_ms'])} {sponsor_rows[0]['text']}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Caption files (json3 / WebVTT) → transcript cues (docs/DESIGN.md §5.4).

Pure functions, no I/O. Rules:

* Every spoken word is emitted once. YouTube auto captions in WebVTT repeat text across rolling
  cues (each cue shows the previous line again plus a new one, and there are 10 ms "snapshot"
  cues that only repeat). json3 doesn't repeat, but the same guarantees are checked for both.
* Markup tags are stripped; ``[Music]``-style annotations are moved into ``annotations``
  instead of being deleted silently.
* ``words`` (per-word start times) only when the source has word timing (auto captions).
* Cues are keyed by ``start_ms``; two cues with the same start are merged so that
  ``(item_id, track, start_ms)`` is a stable, unique ID.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from typing import Any

_TAG_RE = re.compile(r"<[^>]*>")
_ANNOTATION_RE = re.compile(r"\[[^\[\]]*\]|♪+")
_VTT_TIMING_RE = re.compile(
    r"^\s*(?P<start>(?:\d+:)?\d{1,2}:\d{2}[.,]\d{3})\s*-->\s*(?P<end>(?:\d+:)?\d{1,2}:\d{2}[.,]\d{3})"
)
_VTT_INLINE_TS_RE = re.compile(r"<((?:\d+:)?\d{1,2}:\d{2}[.,]\d{3})>")


@dataclass
class Word:
    w: str
    start_ms: int


@dataclass
class Cue:
    start_ms: int
    end_ms: int
    text: str
    words: list[Word] | None = None
    annotations: list[str] = field(default_factory=list)


# --- text cleanup ---------------------------------------------------------------------------


def clean_text(raw: str) -> tuple[str, list[str]]:
    """Strip markup, unescape entities, split out annotations, collapse whitespace."""
    text = html.unescape(_TAG_RE.sub("", raw))
    annotations = [a.strip() for a in _ANNOTATION_RE.findall(text) if a.strip()]
    text = _ANNOTATION_RE.sub(" ", text)
    text = " ".join(text.split())
    return text, annotations


def _is_annotation_token(token: str) -> bool:
    return bool(_ANNOTATION_RE.fullmatch(token.strip()))


def _vtt_time_ms(value: str) -> int:
    value = value.replace(",", ".")
    parts = value.split(":")
    secs = float(parts[-1])
    mins = int(parts[-2]) if len(parts) >= 2 else 0
    hours = int(parts[-3]) if len(parts) >= 3 else 0
    return int(round((hours * 3600 + mins * 60 + secs) * 1000))


# --- json3 ----------------------------------------------------------------------------------


def parse_json3(data: bytes | str | dict[str, Any], *, word_timing: bool) -> list[Cue]:
    doc = json.loads(data) if isinstance(data, bytes | str) else data
    cues: list[Cue] = []
    seen: set[tuple[int, str]] = set()
    for ev in doc.get("events") or []:
        segs = ev.get("segs")
        if not segs:
            continue  # window/style definitions
        start = int(ev.get("tStartMs", 0))
        dur = int(ev.get("dDurationMs", 0))
        raw = "".join(str(s.get("utf8", "")) for s in segs)
        if not raw.strip():
            continue  # aAppend "\n" line breaks
        text, annotations = clean_text(raw.replace("\n", " "))
        key = (start, raw)
        if key in seen:
            continue  # exact duplicate event
        seen.add(key)
        words: list[Word] | None = None
        if word_timing:
            words = []
            for s in segs:
                token = clean_text(str(s.get("utf8", "")))[0]
                if not token or _is_annotation_token(str(s.get("utf8", ""))):
                    continue
                # A seg is normally one word; if not, its parts share the seg's start time.
                offset = start + int(s.get("tOffsetMs", 0))
                words.extend(Word(part, offset) for part in token.split())
        cues.append(Cue(start, start + dur, text, words, annotations))
    return cues


# --- WebVTT ---------------------------------------------------------------------------------


@dataclass
class _VttCue:
    start_ms: int
    end_ms: int
    lines: list[str]


def _read_vtt(text: str) -> list[_VttCue]:
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", text)
    cues: list[_VttCue] = []
    for block in blocks:
        lines = block.split("\n")
        for i, line in enumerate(lines):
            m = _VTT_TIMING_RE.match(line)
            if m:
                payload = lines[i + 1 :]
                cues.append(_VttCue(_vtt_time_ms(m["start"]), _vtt_time_ms(m["end"]), payload))
                break
        # Blocks without a timing line (header, NOTE, STYLE) are ignored.
    return cues


def _line_words(line: str, cue_start: int) -> list[Word]:
    """Words of one VTT line with inline ``<00:00:01.234>`` timestamps; the first word of the
    line starts at the cue start."""
    words: list[Word] = []
    pos = 0
    current = cue_start
    for m in _VTT_INLINE_TS_RE.finditer(line):
        chunk = clean_text(line[pos : m.start()])[0]
        if chunk and not _is_annotation_token(chunk):
            words.extend(Word(w, current) for w in chunk.split())
        current = _vtt_time_ms(m.group(1))
        pos = m.end()
    chunk = clean_text(line[pos:])[0]
    if chunk and not _is_annotation_token(chunk):
        words.extend(Word(w, current) for w in chunk.split())
    return words


def parse_vtt(data: bytes | str, *, rolling: bool) -> list[Cue]:
    """Parse WebVTT. With ``rolling=True`` (YouTube auto captions) remove the rolling
    duplicates: a line repeated from the previous cue is not emitted again, and cues that only
    repeat earlier text (the 10 ms snapshot cues) disappear. A line carrying inline word
    timestamps is always new text."""
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    out: list[Cue] = []
    prev_lines: set[str] = set()
    for vc in _read_vtt(text):
        plain_lines = [clean_text(_VTT_INLINE_TS_RE.sub("", ln)) for ln in vc.lines]
        if not rolling:
            joined = " ".join(_VTT_INLINE_TS_RE.sub("", ln) for ln in vc.lines)
            t, ann = clean_text(joined)
            if t or ann:
                out.append(Cue(vc.start_ms, vc.end_ms, t, None, ann))
            continue
        new_text: list[str] = []
        new_ann: list[str] = []
        words: list[Word] = []
        current_lines: set[str] = set()
        for raw_line, (line_text, line_ann) in zip(vc.lines, plain_lines, strict=True):
            key = " ".join([*line_ann, line_text]).strip()
            if not key:
                continue
            current_lines.add(key)
            has_timing = bool(_VTT_INLINE_TS_RE.search(raw_line))
            if not has_timing and key in prev_lines:
                continue  # rolling repeat of the previous cue's line
            if line_text:
                new_text.append(line_text)
                words.extend(_line_words(raw_line, vc.start_ms))
            new_ann.extend(line_ann)
        prev_lines = current_lines
        if new_text or new_ann:
            out.append(Cue(vc.start_ms, vc.end_ms, " ".join(new_text), words, new_ann))
    return out


# --- post-processing ------------------------------------------------------------------------


def finalize_cues(cues: list[Cue], *, clip_overlaps: bool) -> list[Cue]:
    """Sort, merge cues sharing a start time (stable IDs), and optionally clip each cue's end
    to the next cue's start (auto captions overlap on screen but the speech does not)."""
    merged: list[Cue] = []
    for cue in sorted(cues, key=lambda c: c.start_ms):
        if merged and merged[-1].start_ms == cue.start_ms:
            last = merged[-1]
            last.text = " ".join(t for t in (last.text, cue.text) if t)
            last.end_ms = max(last.end_ms, cue.end_ms)
            last.annotations.extend(cue.annotations)
            if cue.words is not None:
                last.words = (last.words or []) + cue.words
            continue
        merged.append(cue)
    if clip_overlaps:
        for cur, nxt in zip(merged, merged[1:], strict=False):
            if cur.end_ms > nxt.start_ms:
                cur.end_ms = nxt.start_ms
    for cue in merged:
        if cue.end_ms < cue.start_ms:
            cue.end_ms = cue.start_ms
    return merged


def parse_caption_file(name: str, data: bytes, track: str) -> list[Cue]:
    """Dispatch on file extension. ``track`` is 'manual' or 'auto'."""
    auto = track == "auto"
    if name.endswith(".json3"):
        cues = parse_json3(data, word_timing=auto)
    elif name.endswith(".vtt"):
        cues = parse_vtt(data, rolling=auto)
    else:
        raise ValueError(f"unsupported caption format: {name}")
    return finalize_cues(cues, clip_overlaps=auto)

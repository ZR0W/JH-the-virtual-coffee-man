"""All ``data/`` path construction lives here, nowhere else.

Layout (see docs/DESIGN.md §3)::

    data/
      manifest.sqlite
      COOLDOWN
      raw/<source_type>/<item_id>/snapshots/<YYYYMMDDTHHMMSSZ>/...
      raw/<source_type>/_channel/snapshots/<YYYYMMDDTHHMMSSZ>/...   # discover listings
      normalized/<source_type>/items.jsonl
      normalized/<source_type>/transcripts/<item_id>.<track>.jsonl
      normalized/<source_type>/sponsor_segments.jsonl
      derived/

Paths stored in the manifest or in normalized records are *relative to the data root* and
always use forward slashes, so a ``data/`` directory copied from Windows to Linux (or back)
keeps working.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

# Directory under raw/<source_type>/ that holds channel listings rather than an item.
CHANNEL_DIR = "_channel"
TMP_PREFIX = ".tmp-"

_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SOURCE_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class InvalidIdError(ValueError):
    pass


def check_item_id(item_id: str) -> str:
    if not _ITEM_ID_RE.match(item_id) or item_id == CHANNEL_DIR:
        raise InvalidIdError(f"invalid item id: {item_id!r}")
    return item_id


def check_source_type(source_type: str) -> str:
    if not _SOURCE_TYPE_RE.match(source_type):
        raise InvalidIdError(f"invalid source type: {source_type!r}")
    return source_type


class DataPaths:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # --- top-level files ---------------------------------------------------------------
    @property
    def manifest(self) -> Path:
        return self.root / "manifest.sqlite"

    @property
    def cooldown(self) -> Path:
        return self.root / "COOLDOWN"

    @property
    def cooldown_history_dir(self) -> Path:
        return self.root / "cooldown_history"

    @property
    def lock(self) -> Path:
        return self.root / ".lock"

    # --- raw -----------------------------------------------------------------------------
    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    def source_raw_dir(self, source_type: str) -> Path:
        return self.raw_dir / check_source_type(source_type)

    def item_raw_dir(self, source_type: str, item_id: str) -> Path:
        return self.source_raw_dir(source_type) / check_item_id(item_id)

    def snapshots_dir(self, source_type: str, item_id: str) -> Path:
        return self.item_raw_dir(source_type, item_id) / "snapshots"

    def channel_snapshots_dir(self, source_type: str) -> Path:
        return self.source_raw_dir(source_type) / CHANNEL_DIR / "snapshots"

    def iter_raw_item_ids(self, source_type: str) -> Iterator[str]:
        base = self.source_raw_dir(source_type)
        if not base.is_dir():
            return
        for child in sorted(base.iterdir()):
            if child.is_dir() and child.name != CHANNEL_DIR and _ITEM_ID_RE.match(child.name):
                yield child.name

    # --- normalized ------------------------------------------------------------------------
    @property
    def normalized_dir(self) -> Path:
        return self.root / "normalized"

    def source_normalized_dir(self, source_type: str) -> Path:
        return self.normalized_dir / check_source_type(source_type)

    def items_jsonl(self, source_type: str) -> Path:
        return self.source_normalized_dir(source_type) / "items.jsonl"

    def transcripts_dir(self, source_type: str) -> Path:
        return self.source_normalized_dir(source_type) / "transcripts"

    def transcript_path(self, source_type: str, item_id: str, track: str) -> Path:
        return self.transcripts_dir(source_type) / f"{check_item_id(item_id)}.{track}.jsonl"

    def sponsor_segments_jsonl(self, source_type: str) -> Path:
        return self.source_normalized_dir(source_type) / "sponsor_segments.jsonl"

    # --- derived (later phases) ----------------------------------------------------------
    @property
    def derived_dir(self) -> Path:
        return self.root / "derived"

    # --- relative paths ------------------------------------------------------------------
    def rel(self, path: Path) -> str:
        """Data-root-relative path with forward slashes (portable across OSes)."""
        return PurePosixPath(*Path(path).relative_to(self.root).parts).as_posix()

    def resolve(self, rel: str) -> Path:
        return self.root.joinpath(*PurePosixPath(rel).parts)


# --- snapshot directories ----------------------------------------------------------------


def list_snapshots(snapshots_dir: Path) -> list[Path]:
    """Complete snapshots, oldest first. Temp (in-progress or crashed) dirs are excluded."""
    if not snapshots_dir.is_dir():
        return []
    return sorted(
        (p for p in snapshots_dir.iterdir() if p.is_dir() and not p.name.startswith(TMP_PREFIX)),
        key=lambda p: p.name,
    )


def latest_snapshot(snapshots_dir: Path) -> Path | None:
    snaps = list_snapshots(snapshots_dir)
    return snaps[-1] if snaps else None


def stale_tmp_dirs(snapshots_dir: Path) -> list[Path]:
    if not snapshots_dir.is_dir():
        return []
    return sorted(
        p for p in snapshots_dir.iterdir() if p.is_dir() and p.name.startswith(TMP_PREFIX)
    )


def unique_snapshot_name(snapshots_dir: Path, stamp: str) -> str:
    """``stamp``, or ``stamp-2``, ``stamp-3``... if a snapshot with that name already exists."""
    name = stamp
    n = 1
    while (snapshots_dir / name).exists() or (snapshots_dir / (TMP_PREFIX + name)).exists():
        n += 1
        name = f"{stamp}-{n}"
    return name

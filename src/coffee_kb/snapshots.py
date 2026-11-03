"""Write-once snapshot directories under ``raw/`` (docs/DESIGN.md §5.1).

Files are written into ``snapshots/.tmp-<stamp>/`` and the directory is renamed to
``snapshots/<stamp>/`` with ``os.replace`` only once ``fetch.json`` is written, so a crashed
fetch can never leave a half-snapshot that looks complete. Existing snapshots are never
modified.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from coffee_kb.clock import stamp
from coffee_kb.paths import TMP_PREFIX, stale_tmp_dirs, unique_snapshot_name

log = logging.getLogger(__name__)


def replace_with_retry(src: Path, dst: Path, attempts: int = 10, wait_s: float = 0.5) -> None:
    """``os.replace``, retried briefly on PermissionError.

    On Windows a just-written file can be held open for a moment by antivirus or the search
    indexer, which makes a rename fail with "Access is denied". This is local file-system
    waiting only; it has nothing to do with network pacing.
    """
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(wait_s)


def dump_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=False)


class SnapshotWriter:
    def __init__(self, snapshots_dir: Path, now: datetime) -> None:
        self.snapshots_dir = Path(snapshots_dir)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        for stale in stale_tmp_dirs(self.snapshots_dir):
            log.warning("removing incomplete snapshot left by an interrupted run: %s", stale)
            shutil.rmtree(stale, ignore_errors=True)
        self.name = unique_snapshot_name(self.snapshots_dir, stamp(now))
        self.tmp_dir = self.snapshots_dir / (TMP_PREFIX + self.name)
        self.tmp_dir.mkdir()
        self.final_dir = self.snapshots_dir / self.name
        self.files: list[str] = []
        self._done = False

    def write_bytes(self, rel: str, data: bytes) -> Path:
        path = self.tmp_dir.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.files.append(rel)
        return path

    def write_json(self, rel: str, data: Any) -> Path:
        return self.write_bytes(rel, (dump_json(data) + "\n").encode("utf-8"))

    def finalize(self, fetch_record: dict[str, Any]) -> Path:
        """Write fetch.json last, then atomically publish the snapshot."""
        record = dict(fetch_record)
        record["files"] = sorted(self.files)
        path = self.tmp_dir / "fetch.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        replace_with_retry(self.tmp_dir, self.final_dir)
        self._done = True
        return self.final_dir

    def abort(self) -> None:
        if not self._done:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
            self._done = True

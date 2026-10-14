"""The shared log file, ``data/logs/coffee-kb.log``.

Every command that does collection work (discover, fetch, normalize, collector run) appends to
it, in addition to stderr. It rotates at 5 MB, keeping 5 old files. The web app and
``coffee-kb collector logs`` read it.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 5


def add_file_handler(log_file: Path, logger_name: str = "coffee_kb") -> logging.Handler:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8", delay=True
    )
    handler.setFormatter(logging.Formatter(FORMAT, DATEFMT))
    handler.setLevel(logging.INFO)
    logging.getLogger(logger_name).addHandler(handler)
    return handler


def tail_lines(path: Path, n: int, chunk: int = 256 * 1024) -> list[str]:
    """Last ``n`` lines of a text file (reads only the end of it)."""
    if n <= 0 or not path.exists():
        return []
    with path.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        data = b""
        pos = size
        while pos > 0 and data.count(b"\n") <= n:
            step = min(chunk, pos)
            pos -= step
            fh.seek(pos)
            data = fh.read(step) + data
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-n:]

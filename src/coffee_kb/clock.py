"""Time and sleeping, behind one seam so tests never sleep for real.

All timestamps are UTC. Stored form is ISO-8601 with a ``Z`` suffix
(``2026-10-14T09:30:00Z``); snapshot directory names use the compact form
``20261014T093000Z``.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


class Clock:
    """Real wall clock. Tests substitute a fake that advances time on ``sleep``."""

    def now(self) -> datetime:
        return datetime.now(UTC).replace(microsecond=0)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def monotonic(self) -> float:
        return time.monotonic()


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(ISO_FORMAT)


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, ISO_FORMAT).replace(tzinfo=UTC)


def stamp(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(STAMP_FORMAT)


def parse_stamp(value: str) -> datetime:
    # Snapshot names may carry a "-2" style suffix if two snapshots share a second.
    return datetime.strptime(value.split("-", 1)[0], STAMP_FORMAT).replace(tzinfo=UTC)

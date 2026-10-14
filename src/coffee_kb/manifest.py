"""SQLite manifest: an index of what exists and its status (docs/DESIGN.md §5.2).

The manifest is not the source of truth (``raw/`` is), but it is where per-component status,
run history, and the fetch attempts behind the daily cap are recorded. Every write commits
immediately (autocommit), so an interrupted command leaves a consistent manifest behind.

All timestamps are UTC ISO-8601 strings (``2026-10-14T09:30:00Z``), which sort correctly as
text. Snapshot paths are data-root-relative with forward slashes.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

COMPONENTS_BASE = ("metadata", "captions_manual", "captions_auto", "sponsorblock")
COMPONENT_AUDIO = "audio"
COMPONENT_PINNED_COMMENT = "pinned_comment"  # stretch goal, not collected yet (TODO)

STATUS_OK = "ok"
STATUS_ABSENT = "absent"
STATUS_ERROR = "error"
STATUS_PENDING = "pending"
STATUS_SKIPPED = "skipped"
DONE_STATUSES = (STATUS_OK, STATUS_ABSENT)

LISTED = "listed"
MISSING_FROM_LISTING = "missing_from_listing"
NOT_YET_LISTED = "not_yet_listed"  # added with fetch --video before discover saw it

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  source_type      TEXT NOT NULL,
  item_id          TEXT NOT NULL,
  item_type        TEXT,                   -- 'video' | 'short' | 'stream'
  title            TEXT,
  upload_date      TEXT,                   -- YYYY-MM-DD
  duration_s       INTEGER,
  listing_status   TEXT NOT NULL,          -- 'listed' | 'missing_from_listing' | 'not_yet_listed'
  listing_rank     INTEGER,                -- position in its channel tab, 0 = newest
  availability     TEXT,                   -- 'public' | 'unavailable' | 'skipped_restricted'
                                           -- | 'upcoming' | ...
  first_seen_at    TEXT NOT NULL,
  last_seen_at     TEXT NOT NULL,
  latest_snapshot  TEXT,                   -- data-root-relative path, NULL until fetched
  PRIMARY KEY (source_type, item_id)
);

CREATE TABLE IF NOT EXISTS components (
  source_type   TEXT NOT NULL,
  item_id       TEXT NOT NULL,
  component     TEXT NOT NULL,             -- 'metadata' | 'captions_manual' | 'captions_auto'
                                           -- | 'sponsorblock' | 'audio' | 'pinned_comment'
  status        TEXT NOT NULL,             -- 'ok' | 'absent' | 'error' | 'pending' | 'skipped'
  detail        TEXT,
  snapshot      TEXT,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (source_type, item_id, component)
);

CREATE TABLE IF NOT EXISTS runs (
  run_id       INTEGER PRIMARY KEY,
  command      TEXT NOT NULL,
  args_json    TEXT,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  exit_code    INTEGER,
  summary_json TEXT
);

-- One row per video fetch attempt, written *before* the first request for that video.
-- The rolling 24 h cap counts these rows (successful and failed alike).
CREATE TABLE IF NOT EXISTS fetch_attempts (
  attempt_id   INTEGER PRIMARY KEY,
  source_type  TEXT NOT NULL,
  item_id      TEXT NOT NULL,
  run_id       INTEGER,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  outcome      TEXT,                       -- 'started' until finished
  detail       TEXT
);
CREATE INDEX IF NOT EXISTS fetch_attempts_started ON fetch_attempts (source_type, started_at);
"""


class Manifest:
    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        """``readonly=True`` (used by the web app) never writes or creates anything: it opens
        the file in SQLite's read-only mode, or an empty in-memory manifest if it doesn't
        exist yet."""
        self.path = Path(path)
        if readonly:
            if self.path.exists():
                uri = self.path.resolve().as_uri() + "?mode=ro"
                self.conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=10)
                self.conn.row_factory = sqlite3.Row
                return
            self.conn = sqlite3.connect(":memory:", isolation_level=None)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"{self.path} has schema version {version}, newer than this code "
                f"({SCHEMA_VERSION}). Update coffee-kb."
            )
        self.conn.executescript(SCHEMA)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Manifest:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- items ----------------------------------------------------------------------------

    def upsert_listed_item(
        self,
        source_type: str,
        item_id: str,
        now: str,
        *,
        item_type: str | None,
        title: str | None = None,
        duration_s: int | None = None,
        upload_date: str | None = None,
        availability: str | None = None,
        listing_rank: int | None = None,
    ) -> bool:
        """Insert or refresh an item seen in a channel listing. Returns True if new."""
        existing = self.get_item(source_type, item_id)
        if existing is None:
            self.conn.execute(
                """INSERT INTO items (source_type, item_id, item_type, title, upload_date,
                     duration_s, listing_status, listing_rank, availability, first_seen_at,
                     last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source_type,
                    item_id,
                    item_type,
                    title,
                    upload_date,
                    duration_s,
                    LISTED,
                    listing_rank,
                    availability,
                    now,
                    now,
                ),
            )
            return True
        # Listing data never overwrites richer values already learned from a full fetch.
        self.conn.execute(
            """UPDATE items SET
                 item_type = COALESCE(?, item_type),
                 title = COALESCE(title, ?),
                 upload_date = COALESCE(upload_date, ?),
                 duration_s = COALESCE(duration_s, ?),
                 availability = COALESCE(availability, ?),
                 listing_status = ?,
                 listing_rank = ?,
                 last_seen_at = ?
               WHERE source_type = ? AND item_id = ?""",
            (
                item_type,
                title,
                upload_date,
                duration_s,
                availability,
                LISTED,
                listing_rank,
                now,
                source_type,
                item_id,
            ),
        )
        return False

    def ensure_item(self, source_type: str, item_id: str, now: str) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO items (source_type, item_id, listing_status, first_seen_at,
                 last_seen_at) VALUES (?, ?, ?, ?, ?)""",
            (source_type, item_id, NOT_YET_LISTED, now, now),
        )

    def mark_missing(self, source_type: str, seen_ids: Iterable[str], now: str) -> list[str]:
        """Mark listed items not in ``seen_ids`` as missing_from_listing (never delete)."""
        seen = set(seen_ids)
        rows = self.conn.execute(
            "SELECT item_id FROM items WHERE source_type = ? AND listing_status = ?",
            (source_type, LISTED),
        ).fetchall()
        missing = sorted(r["item_id"] for r in rows if r["item_id"] not in seen)
        for item_id in missing:
            self.conn.execute(
                "UPDATE items SET listing_status = ? WHERE source_type = ? AND item_id = ?",
                (MISSING_FROM_LISTING, source_type, item_id),
            )
        return missing

    def get_item(self, source_type: str, item_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM items WHERE source_type = ? AND item_id = ?", (source_type, item_id)
        ).fetchone()

    def items(self, source_type: str | None = None) -> list[sqlite3.Row]:
        if source_type is None:
            return self.conn.execute("SELECT * FROM items ORDER BY source_type, item_id").fetchall()
        return self.conn.execute(
            "SELECT * FROM items WHERE source_type = ? ORDER BY item_id", (source_type,)
        ).fetchall()

    def items_newest_first(self, source_type: str) -> list[sqlite3.Row]:
        """Newest first: by upload date where known; otherwise (flat channel listings carry no
        dates) by channel listing position, main videos before shorts before streams."""
        return self.conn.execute(
            """SELECT * FROM items WHERE source_type = ?
               ORDER BY upload_date IS NULL, upload_date DESC,
                        CASE item_type WHEN 'video' THEN 0 WHEN 'short' THEN 1
                                       WHEN 'stream' THEN 2 ELSE 3 END,
                        listing_rank IS NULL, listing_rank ASC, item_id""",
            (source_type,),
        ).fetchall()

    _UPDATABLE = frozenset(
        {"item_type", "title", "upload_date", "duration_s", "availability", "latest_snapshot"}
    )

    def update_item(self, source_type: str, item_id: str, **fields: Any) -> None:
        bad = set(fields) - self._UPDATABLE
        if bad:
            raise ValueError(f"cannot update item fields: {sorted(bad)}")
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE items SET {cols} WHERE source_type = ? AND item_id = ?",
            (*fields.values(), source_type, item_id),
        )

    # --- components -----------------------------------------------------------------------

    def set_component(
        self,
        source_type: str,
        item_id: str,
        component: str,
        status: str,
        now: str,
        detail: str | None = None,
        snapshot: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO components (source_type, item_id, component, status, detail, snapshot,
                 updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (source_type, item_id, component) DO UPDATE SET
                 status = excluded.status, detail = excluded.detail,
                 snapshot = excluded.snapshot, updated_at = excluded.updated_at""",
            (source_type, item_id, component, status, detail, snapshot, now),
        )

    def components(self, source_type: str, item_id: str) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM components WHERE source_type = ? AND item_id = ?",
            (source_type, item_id),
        ).fetchall()
        return {r["component"]: r for r in rows}

    def all_components(self, source_type: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM components WHERE source_type = ? ORDER BY item_id, component",
            (source_type,),
        ).fetchall()

    def is_complete(self, source_type: str, item_id: str, required: Iterable[str]) -> bool:
        """True if the latest snapshot has every required component as ok/absent."""
        item = self.get_item(source_type, item_id)
        if item is None or not item["latest_snapshot"]:
            return False
        comps = self.components(source_type, item_id)
        for name in required:
            row = comps.get(name)
            if row is None or row["status"] not in DONE_STATUSES:
                return False
            if row["snapshot"] != item["latest_snapshot"]:
                return False
        return True

    # --- runs -----------------------------------------------------------------------------

    def start_run(self, command: str, args: dict[str, Any], now: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (command, args_json, started_at) VALUES (?, ?, ?)",
            (command, json.dumps(args, sort_keys=True, default=str), now),
        )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, now: str, exit_code: int, summary: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, exit_code = ?, summary_json = ? WHERE run_id = ?",
            (now, exit_code, json.dumps(summary, sort_keys=True, default=str), run_id),
        )

    def last_successful_run(self, command: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT * FROM runs WHERE command = ? AND exit_code = 0 AND finished_at IS NOT NULL
               ORDER BY started_at DESC, run_id DESC LIMIT 1""",
            (command,),
        ).fetchone()

    def recent_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC, run_id DESC LIMIT ?", (limit,)
        ).fetchall()

    def last_run(self, command: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM runs WHERE command = ? ORDER BY started_at DESC, run_id DESC LIMIT 1",
            (command,),
        ).fetchone()

    # --- fetch attempts (daily cap) --------------------------------------------------------

    def record_attempt_start(
        self, source_type: str, item_id: str, run_id: int | None, now: str
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO fetch_attempts (source_type, item_id, run_id, started_at, outcome)
               VALUES (?, ?, ?, ?, 'started')""",
            (source_type, item_id, run_id, now),
        )
        return int(cur.lastrowid)

    def finish_attempt(
        self, attempt_id: int, now: str, outcome: str, detail: str | None = None
    ) -> None:
        self.conn.execute(
            "UPDATE fetch_attempts SET finished_at = ?, outcome = ?, detail = ? WHERE attempt_id = ?",
            (now, outcome, detail, attempt_id),
        )

    def attempt_times_since(self, source_type: str, since: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT started_at FROM fetch_attempts WHERE source_type = ? AND started_at > ?",
            (source_type, since),
        ).fetchall()
        return [r["started_at"] for r in rows]

    def last_attempt_time(self, source_type: str) -> str | None:
        row = self.conn.execute(
            """SELECT MAX(COALESCE(finished_at, started_at)) AS t FROM fetch_attempts
               WHERE source_type = ?""",
            (source_type,),
        ).fetchone()
        return row["t"] if row is not None else None

    def failed_attempt_counts(self, source_type: str) -> dict[str, int]:
        """item_id → number of attempts that ended 'partial' or 'error' (not blocked,
        interrupted, or skipped as upcoming)."""
        rows = self.conn.execute(
            """SELECT item_id, COUNT(*) AS n FROM fetch_attempts
               WHERE source_type = ? AND outcome IN ('partial', 'error') GROUP BY item_id""",
            (source_type,),
        ).fetchall()
        return {r["item_id"]: r["n"] for r in rows}

    def attempts(self, source_type: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM fetch_attempts WHERE source_type = ? ORDER BY attempt_id", (source_type,)
        ).fetchall()

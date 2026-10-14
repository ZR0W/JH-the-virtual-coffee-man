from coffee_kb.manifest import (
    LISTED,
    MISSING_FROM_LISTING,
    NOT_YET_LISTED,
    SCHEMA_VERSION,
    Manifest,
)

NOW = "2026-10-14T09:30:00Z"
LATER = "2026-10-21T09:30:00Z"


def test_schema_created_and_versioned(manifest):
    tables = {
        r["name"]
        for r in manifest.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"items", "components", "runs", "fetch_attempts"} <= tables
    assert manifest.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_reopen_keeps_data(data_paths):
    with Manifest(data_paths.manifest) as m:
        m.upsert_listed_item("youtube", "a", NOW, item_type="video", title="A")
    with Manifest(data_paths.manifest) as m:
        assert m.get_item("youtube", "a")["title"] == "A"


def test_upsert_and_missing(manifest):
    assert manifest.upsert_listed_item("youtube", "a", NOW, item_type="video", listing_rank=0)
    assert manifest.upsert_listed_item("youtube", "b", NOW, item_type="short", listing_rank=0)
    assert not manifest.upsert_listed_item("youtube", "a", LATER, item_type="video", listing_rank=1)
    a = manifest.get_item("youtube", "a")
    assert a["first_seen_at"] == NOW and a["last_seen_at"] == LATER and a["listing_rank"] == 1

    missing = manifest.mark_missing("youtube", {"a"}, LATER)
    assert missing == ["b"]
    assert manifest.get_item("youtube", "b")["listing_status"] == MISSING_FROM_LISTING
    # Never deleted; reappearing flips it back.
    manifest.upsert_listed_item("youtube", "b", LATER, item_type="short")
    assert manifest.get_item("youtube", "b")["listing_status"] == LISTED


def test_listing_does_not_clobber_fetched_values(manifest):
    manifest.upsert_listed_item("youtube", "a", NOW, item_type="video", title="Listing title")
    manifest.update_item("youtube", "a", title="Full title", upload_date="2026-01-02")
    manifest.upsert_listed_item(
        "youtube", "a", LATER, item_type="video", title="Other", upload_date=None
    )
    row = manifest.get_item("youtube", "a")
    assert row["title"] == "Full title" and row["upload_date"] == "2026-01-02"


def test_ensure_item_for_manual_video(manifest):
    manifest.ensure_item("youtube", "x", NOW)
    manifest.ensure_item("youtube", "x", LATER)
    row = manifest.get_item("youtube", "x")
    assert row["listing_status"] == NOT_YET_LISTED and row["first_seen_at"] == NOW


def test_newest_first_order(manifest):
    manifest.upsert_listed_item("youtube", "old", NOW, item_type="video", upload_date="2020-01-01")
    manifest.upsert_listed_item("youtube", "new", NOW, item_type="video", upload_date="2026-01-01")
    manifest.upsert_listed_item("youtube", "r1", NOW, item_type="video", listing_rank=1)
    manifest.upsert_listed_item("youtube", "r0", NOW, item_type="video", listing_rank=0)
    assert [r["item_id"] for r in manifest.items_newest_first("youtube")] == [
        "new",
        "old",
        "r0",
        "r1",
    ]


def test_components_and_completeness(manifest):
    manifest.upsert_listed_item("youtube", "a", NOW, item_type="video")
    snap = "raw/youtube/a/snapshots/20261014T093000Z"
    assert not manifest.is_complete("youtube", "a", ["metadata"])
    manifest.update_item("youtube", "a", latest_snapshot=snap)
    manifest.set_component("youtube", "a", "metadata", "ok", NOW, snapshot=snap)
    manifest.set_component("youtube", "a", "captions_manual", "absent", NOW, snapshot=snap)
    assert manifest.is_complete("youtube", "a", ["metadata", "captions_manual"])
    assert not manifest.is_complete("youtube", "a", ["metadata", "captions_auto"])
    manifest.set_component(
        "youtube", "a", "captions_auto", "error", NOW, detail="boom", snapshot=snap
    )
    assert not manifest.is_complete("youtube", "a", ["metadata", "captions_auto"])
    # Upsert replaces the row.
    manifest.set_component("youtube", "a", "captions_auto", "ok", LATER, snapshot=snap)
    assert manifest.components("youtube", "a")["captions_auto"]["status"] == "ok"


def test_runs(manifest):
    rid = manifest.start_run("discover", {"force": False}, NOW)
    assert manifest.last_successful_run("discover") is None
    manifest.finish_run(rid, NOW, 0, {"new": 3})
    assert manifest.last_successful_run("discover")["run_id"] == rid
    rid2 = manifest.start_run("discover", {}, LATER)
    manifest.finish_run(rid2, LATER, 75, {})
    assert manifest.last_successful_run("discover")["run_id"] == rid
    assert manifest.last_run("discover")["run_id"] == rid2


def test_attempts_window(manifest):
    a = manifest.record_attempt_start("youtube", "a", None, "2026-10-13T09:00:00Z")
    manifest.finish_attempt(a, "2026-10-13T09:01:00Z", "ok")
    manifest.record_attempt_start("youtube", "b", None, "2026-10-14T09:00:00Z")
    assert manifest.attempt_times_since("youtube", "2026-10-13T09:30:00Z") == [
        "2026-10-14T09:00:00Z"
    ]
    assert len(manifest.attempts("youtube")) == 2

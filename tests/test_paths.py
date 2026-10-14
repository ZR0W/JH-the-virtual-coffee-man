from pathlib import Path

import pytest

from coffee_kb.paths import (
    InvalidIdError,
    latest_snapshot,
    list_snapshots,
    stale_tmp_dirs,
    unique_snapshot_name,
)


def test_layout(data_paths):
    root = data_paths.root
    assert data_paths.manifest == root / "manifest.sqlite"
    assert data_paths.cooldown == root / "COOLDOWN"
    assert data_paths.snapshots_dir("youtube", "abc_DEF-123") == (
        root / "raw" / "youtube" / "abc_DEF-123" / "snapshots"
    )
    assert data_paths.transcript_path("youtube", "abc", "auto") == (
        root / "normalized" / "youtube" / "transcripts" / "abc.auto.jsonl"
    )
    assert (
        data_paths.channel_snapshots_dir("youtube")
        == root / "raw" / "youtube" / "_channel" / "snapshots"
    )


@pytest.mark.parametrize("bad", ["", "../x", "a/b", "a\\b", "_channel", "x" * 65, "a b"])
def test_rejects_bad_item_ids(data_paths, bad):
    with pytest.raises(InvalidIdError):
        data_paths.item_raw_dir("youtube", bad)


def test_rejects_bad_source_type(data_paths):
    with pytest.raises(InvalidIdError):
        data_paths.source_raw_dir("../youtube")


def test_rel_is_posix_and_round_trips(data_paths):
    p = data_paths.snapshots_dir("youtube", "abc") / "20261014T093000Z"
    rel = data_paths.rel(p)
    assert rel == "raw/youtube/abc/snapshots/20261014T093000Z"
    assert "\\" not in rel
    assert data_paths.resolve(rel) == p


def test_snapshot_listing_ignores_tmp(tmp_path: Path):
    snaps = tmp_path / "snapshots"
    for name in ["20261014T093000Z", "20261015T093000Z", ".tmp-20261016T093000Z"]:
        (snaps / name).mkdir(parents=True)
    assert [p.name for p in list_snapshots(snaps)] == ["20261014T093000Z", "20261015T093000Z"]
    assert latest_snapshot(snaps).name == "20261015T093000Z"
    assert [p.name for p in stale_tmp_dirs(snaps)] == [".tmp-20261016T093000Z"]
    assert latest_snapshot(tmp_path / "nope") is None


def test_unique_snapshot_name(tmp_path: Path):
    snaps = tmp_path / "snapshots"
    snaps.mkdir()
    assert unique_snapshot_name(snaps, "20261014T093000Z") == "20261014T093000Z"
    (snaps / "20261014T093000Z").mkdir()
    assert unique_snapshot_name(snaps, "20261014T093000Z") == "20261014T093000Z-2"
    (snaps / "20261014T093000Z-2").mkdir()
    name = unique_snapshot_name(snaps, "20261014T093000Z")
    assert name == "20261014T093000Z-3"
    # Suffixed names still sort after the base and before the next second.
    assert "20261014T093000Z" < name < "20261014T093001Z"


def test_iter_raw_item_ids_skips_channel_dir(data_paths):
    for item in ["vid1", "vid2"]:
        data_paths.snapshots_dir("youtube", item).mkdir(parents=True)
    data_paths.channel_snapshots_dir("youtube").mkdir(parents=True)
    assert list(data_paths.iter_raw_item_ids("youtube")) == ["vid1", "vid2"]

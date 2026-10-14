import json
import shutil

import pytest
from fakes import fixture_bytes

from coffee_kb.normalize.captions import clean_text, parse_caption_file, parse_json3, parse_vtt
from coffee_kb.normalize.pipeline import NORMALIZER_VERSION, read_jsonl, run_normalize

VID = "abcDEF12345"
SNAP = "20261014T093000Z"


def _words(cues):
    return [w.w for c in cues for w in (c.words or [])]


# --- caption parsing -----------------------------------------------------------------------


def test_clean_text_strips_markup_and_keeps_annotations():
    assert clean_text("Hi, I&#39;m making <i>coffee</i>.") == ("Hi, I'm making coffee.", [])
    assert clean_text("[Laughs] Mostly, anyway.") == ("Mostly, anyway.", ["[Laughs]"])
    assert clean_text("♪ [Music] ♪") == ("", ["♪", "[Music]", "♪"])


def test_rolling_vtt_fixture_really_duplicates():
    import re

    raw = re.sub(r"<[^>]*>", "", fixture_bytes("en.auto.rolling.vtt").decode())
    assert raw.count("finer grind means") == 3  # the problem the de-dup solves
    assert raw.count("I'm making coffee") == 3


def test_auto_vtt_dedup_emits_each_word_once():
    cues = parse_caption_file("en.auto.vtt", fixture_bytes("en.auto.rolling.vtt"), "auto")
    text = " ".join(c.text for c in cues if c.text)
    assert text == "hi I'm making coffee finer grind means more extraction"
    assert _words(cues) == [
        "hi",
        "I'm",
        "making",
        "coffee",
        "finer",
        "grind",
        "means",
        "more",
        "extraction",
    ]
    # Word timing from the inline timestamps.
    first = cues[0]
    assert [(w.w, w.start_ms) for w in first.words] == [
        ("hi", 160),
        ("I'm", 560),
        ("making", 880),
        ("coffee", 1280),
    ]
    # [Music] appears once, as an annotation, not as text.
    anns = [a for c in cues for a in c.annotations]
    assert anns == ["[Music]"]
    # 10 ms snapshot cues vanished; cues don't overlap after clipping.
    assert all(c.end_ms - c.start_ms > 10 for c in cues)
    assert all(a.end_ms <= b.start_ms for a, b in zip(cues, cues[1:], strict=False))


def test_auto_json3_words_once_with_offsets():
    cues = parse_caption_file("en-orig.auto.json3", fixture_bytes("en-orig.auto.json3"), "auto")
    assert _words(cues) == [
        "hi", "I'm", "making", "coffee", "this", "video", "is", "sponsored",
        "finer", "grind", "means", "more", "extraction",
    ]  # fmt: skip
    finer = next(c for c in cues if c.text.startswith("finer"))
    assert [(w.w, w.start_ms) for w in finer.words] == [
        ("finer", 31000),
        ("grind", 31320),
        ("means", 31640),
    ]
    music = next(c for c in cues if c.annotations)
    assert music.text == "" and music.annotations == ["[Music]"]
    assert all(a.end_ms <= b.start_ms for a, b in zip(cues, cues[1:], strict=False))


def test_auto_json3_and_vtt_agree_on_text():
    j = parse_caption_file("x.auto.json3", fixture_bytes("en-orig.auto.json3"), "auto")
    v = parse_caption_file("x.auto.vtt", fixture_bytes("en.auto.rolling.vtt"), "auto")
    jw = [w for w in _words(j) if w not in ("this", "video", "is", "sponsored")]
    assert jw == _words(v)


def test_manual_json3():
    cues = parse_caption_file("en.manual.json3", fixture_bytes("en.manual.json3"), "manual")
    assert [c.text for c in cues] == [
        "Hi, I'm making coffee.",
        "",
        "This video is sponsored by a grinder company.",
        "Finer grind means more extraction.",
        "Mostly, anyway.",
    ]
    assert all(c.words is None for c in cues)
    assert cues[1].annotations == ["[Music]"] and cues[4].annotations == ["[Laughs]"]
    assert (cues[3].start_ms, cues[3].end_ms) == (31000, 34500)


def test_manual_vtt_is_not_deduplicated():
    cues = parse_vtt(
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nNo.\n\n00:00:02.000 --> 00:00:03.000\nNo.\n",
        rolling=False,
    )
    assert [c.text for c in cues] == ["No.", "No."]
    cues = parse_caption_file("en.manual.vtt", fixture_bytes("en.manual.vtt"), "manual")
    assert cues[0].text == "Hi, I'm making coffee."
    assert cues[1].text == "This video is sponsored by a grinder company."


def test_same_start_cues_merge_for_stable_ids():
    doc = {
        "events": [
            {"tStartMs": 1000, "dDurationMs": 500, "segs": [{"utf8": "one"}]},
            {"tStartMs": 1000, "dDurationMs": 900, "segs": [{"utf8": "two"}]},
            {"tStartMs": 1000, "dDurationMs": 500, "segs": [{"utf8": "one"}]},
        ]
    }
    from coffee_kb.normalize.captions import finalize_cues

    cues = finalize_cues(parse_json3(doc, word_timing=False), clip_overlaps=False)
    assert len(cues) == 1 and cues[0].text == "one two" and cues[0].end_ms == 1900


def test_vtt_hours_and_comma_times():
    cues = parse_vtt("WEBVTT\n\n01:02:03,500 --> 01:02:04.000\nlate\n", rolling=False)
    assert cues[0].start_ms == 3723500


def test_unsupported_format():
    with pytest.raises(ValueError):
        parse_caption_file("x.auto.srv3", b"", "auto")


# --- pipeline -------------------------------------------------------------------------------


def _make_snapshot(
    paths,
    item_id=VID,
    name=SNAP,
    *,
    manual="en.manual.json3",
    auto="en-orig.auto.json3",
    sponsor=True,
    info=None,
):
    snap = paths.snapshots_dir("youtube", item_id) / name
    (snap / "captions").mkdir(parents=True)
    info_doc = json.loads(fixture_bytes("info.json"))
    if info:
        info_doc.update(info)
    (snap / "info.json").write_text(json.dumps(info_doc), encoding="utf-8")
    if manual:
        ext = manual.rsplit(".", 1)[1]
        (snap / "captions" / f"en.manual.{ext}").write_bytes(fixture_bytes(manual))
    if auto:
        ext = auto.rsplit(".", 1)[1]
        (snap / "captions" / f"en-orig.auto.{ext}").write_bytes(fixture_bytes(auto))
    if sponsor:
        (snap / "sponsorblock.json").write_bytes(fixture_bytes("sponsorblock.json"))
    (snap / "fetch.json").write_text(
        json.dumps({"fetched_at": "2026-10-14T09:30:00Z"}), encoding="utf-8"
    )
    return snap


def test_normalize_end_to_end(data_paths):
    _make_snapshot(data_paths)
    summary = run_normalize(data_paths)
    assert summary.items == 1 and summary.transcripts == {"manual": 1, "auto": 1}
    assert not summary.errors

    items = read_jsonl(data_paths.items_jsonl("youtube"))
    assert len(items) == 1
    item = items[0]
    assert item["item_id"] == VID and item["source_type"] == "youtube"
    assert item["upload_date"] == "2026-10-01" and item["duration_s"] == 150
    assert item["chapters"] == [
        {"title": "Intro", "start_ms": 0, "end_ms": 30000},
        {"title": "Grind size", "start_ms": 30000, "end_ms": 150000},
    ]
    assert item["counts_as_of"] == "2026-10-14T09:30:00Z" and item["view_count"] == 123456
    assert item["snapshot"] == f"raw/youtube/{VID}/snapshots/{SNAP}"
    assert item["normalizer_version"] == NORMALIZER_VERSION
    assert item["caption_tracks"]["manual"]["format"] == "json3"

    manual = read_jsonl(data_paths.transcript_path("youtube", VID, "manual"))
    assert {r["track"] for r in manual} == {"manual"}
    by_start = {r["start_ms"]: r for r in manual}
    assert by_start[0]["chapter"] == "Intro"
    assert by_start[31000]["chapter"] == "Grind size"
    # Sponsor segment 10.0–20.5 s (category sponsor) covers the 12 s cue only.
    assert by_start[12000]["in_sponsor_segment"] is True
    assert by_start[0]["in_sponsor_segment"] is False
    assert by_start[2500]["text"] == "" and by_start[2500]["annotations"] == ["[Music]"]
    assert "words" not in by_start[0]
    for r in manual:
        assert r["snapshot"] == item["snapshot"] and r["normalizer_version"] == NORMALIZER_VERSION
        assert r["item_id"] == VID and r["source_type"] == "youtube"

    auto = read_jsonl(data_paths.transcript_path("youtube", VID, "auto"))
    assert auto[0]["words"][0] == {"w": "hi", "start_ms": 160}
    # Stable IDs: (item_id, track, start_ms) unique.
    assert len({r["start_ms"] for r in auto}) == len(auto)

    segs = read_jsonl(data_paths.sponsor_segments_jsonl("youtube"))
    assert [(s["category"], s["start_ms"], s["end_ms"]) for s in segs] == [
        ("sponsor", 10000, 20500),
        ("outro", 140000, 150000),
    ]
    assert segs[0]["votes"] == 12 and segs[0]["uuid"].startswith("0b1f")


def test_normalize_uses_latest_snapshot_and_vtt_fallback(data_paths):
    _make_snapshot(data_paths)
    _make_snapshot(
        data_paths,
        name="20261020T093000Z",
        manual="en.manual.vtt",
        auto="en.auto.rolling.vtt",
        sponsor=False,
    )
    run_normalize(data_paths)
    item = read_jsonl(data_paths.items_jsonl("youtube"))[0]
    assert item["snapshot"].endswith("20261020T093000Z")
    assert item["caption_tracks"]["auto"]["format"] == "vtt"
    assert read_jsonl(data_paths.sponsor_segments_jsonl("youtube")) == []


def test_normalize_is_rebuildable_and_deterministic(data_paths):
    _make_snapshot(data_paths)
    _make_snapshot(data_paths, item_id="ghiJKL67890", manual=None)
    run_normalize(data_paths)
    first = {p.name: p.read_bytes() for p in data_paths.normalized_dir.rglob("*.jsonl")}
    shutil.rmtree(data_paths.normalized_dir)
    run_normalize(data_paths)
    second = {p.name: p.read_bytes() for p in data_paths.normalized_dir.rglob("*.jsonl")}
    assert first == second
    assert not data_paths.transcript_path("youtube", "ghiJKL67890", "manual").exists()


def test_normalize_single_video_replaces_only_its_rows(data_paths):
    _make_snapshot(data_paths)
    _make_snapshot(data_paths, item_id="ghiJKL67890")
    run_normalize(data_paths)
    _make_snapshot(
        data_paths,
        item_id="ghiJKL67890",
        name="20261020T093000Z",
        sponsor=False,
        info={"title": "Renamed"},
    )
    run_normalize(data_paths, ["ghiJKL67890"])
    items = {r["item_id"]: r for r in read_jsonl(data_paths.items_jsonl("youtube"))}
    assert set(items) == {VID, "ghiJKL67890"} and items["ghiJKL67890"]["title"] == "Renamed"
    segs = read_jsonl(data_paths.sponsor_segments_jsonl("youtube"))
    assert {s["item_id"] for s in segs} == {VID}


def test_normalize_ignores_tmp_snapshots_and_uses_listing_type(data_paths):
    (data_paths.snapshots_dir("youtube", VID) / ".tmp-20261014T093000Z").mkdir(parents=True)
    summary = run_normalize(data_paths)
    assert summary.items == 0 and VID in summary.skipped
    _make_snapshot(data_paths)
    chan = data_paths.channel_snapshots_dir("youtube") / SNAP
    chan.mkdir(parents=True)
    (chan / "shorts.json").write_text(json.dumps({"entries": [{"id": VID}]}), encoding="utf-8")
    run_normalize(data_paths)
    assert read_jsonl(data_paths.items_jsonl("youtube"))[0]["item_type"] == "short"


def test_normalize_cli_after_fetch(env):
    env.yt.add_video(VID)
    assert env.run("fetch", "--video", VID) == 0
    calls = env.yt.network_calls()
    assert env.run("normalize") == 0
    assert env.yt.network_calls() == calls  # offline
    assert len(read_jsonl(env.paths.transcript_path("youtube", VID, "auto"))) > 0

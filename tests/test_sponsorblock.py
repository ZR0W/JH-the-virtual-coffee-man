import json

import httpx
from fakes import fixture_bytes

from coffee_kb.normalize.pipeline import sponsor_segments
from coffee_kb.sources.youtube.sponsorblock import USER_AGENT, fetch_segments

CATS = ("sponsor", "selfpromo")


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": USER_AGENT})


def test_200_stored_as_is():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, content=fixture_bytes("sponsorblock.json"))

    r = fetch_segments(_client(handler), "https://sponsor.ajay.app/", "abc", CATS)
    assert r.status == "ok" and r.http_status == 200 and r.segment_count == 2
    assert r.body == fixture_bytes("sponsorblock.json")
    url = seen[0].url
    assert url.path == "/api/skipSegments"
    assert url.params["videoID"] == "abc"
    assert json.loads(url.params["categories"]) == list(CATS)
    assert seen[0].headers["User-Agent"].startswith("coffee-kb/")


def test_404_means_no_segments():
    r = fetch_segments(_client(lambda req: httpx.Response(404)), "https://x", "abc", CATS)
    assert r.status == "ok" and r.http_status == 404
    assert json.loads(r.body) == {"segments": []}
    assert sponsor_segments(json.loads(r.body)) == []


def test_server_error_and_bad_json_are_errors():
    r = fetch_segments(
        _client(lambda req: httpx.Response(502, text="bad gateway")), "https://x", "a", CATS
    )
    assert r.status == "error" and r.http_status == 502 and r.body is None
    r = fetch_segments(
        _client(lambda req: httpx.Response(200, text="<html>")), "https://x", "a", CATS
    )
    assert r.status == "error" and "JSON" in r.detail
    r = fetch_segments(
        _client(lambda req: httpx.Response(200, json={"x": 1})), "https://x", "a", CATS
    )
    assert r.status == "error"


def test_network_error_is_an_error_not_a_crash():
    def handler(req):
        raise httpx.ConnectError("dns failure")

    r = fetch_segments(_client(handler), "https://x", "a", CATS)
    assert r.status == "error" and r.http_status is None and "ConnectError" in r.detail


def test_segments_are_renamed_consistently():
    segs = sponsor_segments(json.loads(fixture_bytes("sponsorblock.json")))
    assert segs[0] == {
        "category": "sponsor",
        "action_type": "skip",
        "start_ms": 10000,
        "end_ms": 20500,
        "votes": 12,
        "locked": 0,
        "uuid": segs[0]["uuid"],
        "video_duration_ms": 150000,
    }

"""SponsorBlock segments (docs/DESIGN.md §4.2).

SponsorBlock (sponsor.ajay.app) is a community service, not YouTube: one request per video,
paced by the video loop, and its errors never trip the YouTube cooldown latch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from coffee_kb import __version__

USER_AGENT = f"coffee-kb/{__version__} (personal research index)"


@dataclass
class SponsorBlockResult:
    status: str  # 'ok' | 'error'
    http_status: int | None
    body: bytes | None  # what to store as sponsorblock.json
    segment_count: int = 0
    detail: str | None = None


def make_client(timeout_s: float) -> httpx.Client:
    return httpx.Client(timeout=timeout_s, headers={"User-Agent": USER_AGENT})


def fetch_segments(
    client: httpx.Client, base_url: str, video_id: str, categories: tuple[str, ...]
) -> SponsorBlockResult:
    url = f"{base_url.rstrip('/')}/api/skipSegments"
    params = {"videoID": video_id, "categories": json.dumps(list(categories))}
    try:
        resp = client.get(url, params=params)
    except httpx.HTTPError as exc:
        return SponsorBlockResult("error", None, None, detail=f"{type(exc).__name__}: {exc}")
    if resp.status_code == 404:
        # 404 means "no segments for this video".
        return SponsorBlockResult("ok", 404, b'{"segments": []}\n', 0)
    if resp.status_code != 200:
        return SponsorBlockResult(
            "error", resp.status_code, None, detail=f"HTTP {resp.status_code}: {resp.text[:200]}"
        )
    try:
        data = json.loads(resp.content)
    except ValueError as exc:
        return SponsorBlockResult("error", 200, None, detail=f"invalid JSON: {exc}")
    if not isinstance(data, list):
        return SponsorBlockResult("error", 200, None, detail="unexpected response shape")
    # Stored exactly as returned.
    return SponsorBlockResult("ok", 200, resp.content, len(data))

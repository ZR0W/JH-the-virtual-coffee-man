"""Offline stand-ins for yt-dlp and SponsorBlock. Tests never touch the network."""

from __future__ import annotations

import contextlib
import copy
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).parent / "fixtures" / "youtube"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_info(video_id: str = "abcDEF12345", **overrides: Any) -> dict[str, Any]:
    info = json.loads(fixture_bytes("info.json"))
    if video_id != info["id"]:
        raw = json.dumps(info).replace("abcDEF12345", video_id)
        info = json.loads(raw)
    info.update(overrides)
    return info


class FakeHTTPError(Exception):
    def __init__(self, status: int, msg: str = "") -> None:
        super().__init__(f"HTTP Error {status}: {msg}")
        self.status = status


Response = Any  # dict | bytes | Exception | Callable[[], Any]


class FakeSession:
    def __init__(self, client: FakeYouTube, interval: float, flat: bool) -> None:
        self.client = client
        self.interval = interval
        self.flat = flat
        self.warnings: list[str] = []

    def _resolve(self, value: Response) -> Any:
        if callable(value) and not isinstance(value, type):
            value = value()
        if isinstance(value, BaseException):
            raise value
        return copy.deepcopy(value)

    def extract_info(self, url: str) -> dict[str, Any]:
        self.client.calls.append(("extract_info", url))
        self.warnings.extend(self.client.warnings_for.get(url, []))
        if url not in self.client.infos:
            raise RuntimeError(f"unexpected URL in test: {url}")
        return self._resolve(self.client.infos[url])

    def fetch_bytes(self, url: str) -> bytes:
        self.client.calls.append(("fetch_bytes", url))
        if url not in self.client.urls:
            raise RuntimeError(f"unexpected URL in test: {url}")
        return self._resolve(self.client.urls[url])

    def download_audio(self, url: str, dest_dir: Path) -> Path:
        self.client.calls.append(("download_audio", url))
        path = Path(dest_dir) / "audio.webm"
        path.write_bytes(b"fake opus")
        return path


class FakeYouTube:
    version = "2099.01.01-fake"

    def __init__(self) -> None:
        self.infos: dict[str, Response] = {}
        self.urls: dict[str, Response] = {}
        self.warnings_for: dict[str, list[str]] = {}
        self.calls: list[tuple[str, str]] = []
        self.sessions: list[FakeSession] = []

    @contextlib.contextmanager
    def session(self, request_interval_s: float, *, flat: bool = False) -> Iterator[FakeSession]:
        s = FakeSession(self, request_interval_s, flat)
        self.sessions.append(s)
        yield s

    def add_video(
        self,
        video_id: str,
        info: Response | None = None,
        *,
        manual: Response | None = None,
        auto: Response | None = None,
    ) -> dict[str, Any]:
        """Register a fixture-shaped video: info.json plus json3 caption URLs."""
        info = fixture_info(video_id) if info is None else info
        self.infos[f"https://www.youtube.com/watch?v={video_id}"] = info
        if isinstance(info, dict):
            subs = info.get("subtitles", {}).get("en", [])
            autos = info.get("automatic_captions", {}).get("en-orig", [])
            for f in subs:
                if f["ext"] == "json3":
                    self.urls[f["url"]] = (
                        fixture_bytes("en.manual.json3") if manual is None else manual
                    )
            for f in autos:
                if f["ext"] == "json3":
                    self.urls[f["url"]] = (
                        fixture_bytes("en-orig.auto.json3") if auto is None else auto
                    )
        return info if isinstance(info, dict) else {}

    def network_calls(self) -> int:
        return len(self.calls)


def sponsorblock_transport(
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> tuple[httpx.Client, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def default(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=fixture_bytes("sponsorblock.json"))

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return (handler or default)(request)

    return httpx.Client(transport=httpx.MockTransport(wrapped)), seen

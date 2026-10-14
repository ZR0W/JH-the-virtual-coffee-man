"""The one place that constructs yt-dlp. Everything YouTube-facing goes through here.

yt-dlp is used as a library with its default client settings: no cookies, no proxies, no
custom headers or impersonation, no PO-token providers (``safety.assert_no_evasion`` checks
the params). Pacing comes from the validated config:

* ``sleep_interval_requests`` — sleep between requests during extraction. yt-dlp takes a fixed
  value per ``YoutubeDL`` instance, so each video (one session) draws its own value from the
  configured 5–10 s range.
* ``sleep_interval_subtitles`` — set for completeness; caption files are downloaded by
  ``fetch.py`` itself (through the same yt-dlp HTTP stack) with an explicit sleep before each.

Retries inside yt-dlp are turned off so that ``fetch.py`` alone decides what is retried
(transient network errors only, never block signals).
"""

from __future__ import annotations

import contextlib
import copy
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol

from coffee_kb import safety
from coffee_kb.config import FetchConfig

log = logging.getLogger(__name__)


class YouTubeSession(Protocol):
    """One yt-dlp session: used for one video (or one discover run)."""

    warnings: list[str]

    def extract_info(self, url: str) -> dict[str, Any]:
        """Return a JSON-serializable info dict (no media download)."""
        ...

    def fetch_bytes(self, url: str) -> bytes:
        """GET a URL (e.g. a caption track) through yt-dlp's HTTP stack."""
        ...

    def download_audio(self, url: str, dest_dir: Path) -> Path:
        """Download best audio for the last extracted video into ``dest_dir``."""
        ...


class YouTubeClient(Protocol):
    version: str

    def session(
        self, request_interval_s: float, *, flat: bool = False
    ) -> contextlib.AbstractContextManager[YouTubeSession]: ...


def ytdlp_version() -> str:
    from yt_dlp.version import __version__

    return __version__


class _CollectingLogger:
    """yt-dlp logger: keeps warnings/errors for block scanning and provenance."""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        self.warnings.append(str(msg))
        log.warning("yt-dlp: %s", msg)
        # Stop on the first warning sign: raising here aborts the extraction before yt-dlp
        # moves on to its next request (it would otherwise fall back to another endpoint).
        signal = safety.find_block_signal([str(msg)])
        if signal:
            raise safety.BlockDetected(f"yt-dlp warning: {signal[:500]}")

    def error(self, msg: str) -> None:
        self.warnings.append(str(msg))
        log.debug("yt-dlp error: %s", msg)


def build_ydl_params(
    fc: FetchConfig, request_interval_s: float, logger: Any, *, flat: bool = False
) -> dict[str, Any]:
    safety.validate_fetch_config(fc)
    if request_interval_s < safety.MIN_SLEEP_BETWEEN_REQUESTS_S:
        raise safety.UnsafeConfigError(
            f"request interval {request_interval_s} is below the floor of "
            f"{safety.MIN_SLEEP_BETWEEN_REQUESTS_S}"
        )
    params: dict[str, Any] = {
        "logger": logger,
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "noplaylist": True,
        "skip_download": True,
        # Metadata and captions only: don't fail if formats can't be listed.
        "ignore_no_formats_error": True,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "getcomments": False,
        # Pacing (validated against the floors above).
        "sleep_interval_requests": request_interval_s,
        "sleep_interval_subtitles": fc.sleep_before_subtitles_s,
        "socket_timeout": fc.socket_timeout_s,
        # No retries inside yt-dlp: fetch.py decides, and never retries a block signal.
        "extractor_retries": 0,
        "retries": 0,
        "fragment_retries": 0,
    }
    if flat:
        params["extract_flat"] = "in_playlist"
        params["noplaylist"] = False
    safety.assert_no_evasion(params)
    return params


class _YtDlpSession:
    def __init__(self, ydl: Any, logger: _CollectingLogger, fc: FetchConfig, interval: float):
        self._ydl = ydl
        self._logger = logger
        self._fc = fc
        self._interval = interval
        self._last_info: dict[str, Any] | None = None

    @property
    def warnings(self) -> list[str]:
        return self._logger.warnings

    def extract_info(self, url: str) -> dict[str, Any]:
        info = self._ydl.extract_info(url, download=False)
        self._last_info = info
        return self._ydl.sanitize_info(info)

    def fetch_bytes(self, url: str) -> bytes:
        with contextlib.closing(self._ydl.urlopen(url)) as resp:
            return resp.read()

    def download_audio(self, url: str, dest_dir: Path) -> Path:
        from yt_dlp import YoutubeDL

        if self._last_info is None:
            raise RuntimeError("download_audio called before extract_info")
        params = dict(self._ydl.params)
        params.update(
            {
                "skip_download": False,
                "format": "bestaudio[acodec=opus]/bestaudio",
                "outtmpl": {"default": str(Path(dest_dir) / "audio.%(ext)s")},
                "sleep_interval": self._interval,
                "max_sleep_interval": self._fc.sleep_between_requests_max_s,
            }
        )
        safety.assert_no_evasion(params)
        with YoutubeDL(params) as ydl:
            ydl.process_ie_result(copy.deepcopy(self._last_info), download=True)
        found = sorted(Path(dest_dir).glob("audio.*"))
        if not found:
            raise RuntimeError("audio download produced no file")
        return found[0]


class YtDlpClient:
    def __init__(self, fc: FetchConfig) -> None:
        self.fc = fc
        self.version = ytdlp_version()

    @contextlib.contextmanager
    def session(self, request_interval_s: float, *, flat: bool = False) -> Iterator[YouTubeSession]:
        from yt_dlp import YoutubeDL

        logger = _CollectingLogger()
        params = build_ydl_params(self.fc, request_interval_s, logger, flat=flat)
        with YoutubeDL(params) as ydl:
            yield _YtDlpSession(ydl, logger, self.fc, request_interval_s)

"""Everything a command needs, bundled so tests can swap the network and the clock."""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field
from typing import Any

from coffee_kb import __version__
from coffee_kb.clock import Clock
from coffee_kb.config import Config
from coffee_kb.manifest import Manifest
from coffee_kb.paths import DataPaths
from coffee_kb.safety import Pacer


@dataclass
class Runtime:
    cfg: Config
    paths: DataPaths
    manifest: Manifest
    clock: Clock
    pacer: Pacer
    youtube: Any = None  # sources.youtube.client.YouTubeClient (None for offline commands)
    http: Any = None  # httpx.Client for SponsorBlock
    run_id: int | None = None
    args: dict[str, Any] = field(default_factory=dict)

    def now_iso(self) -> str:
        from coffee_kb.clock import iso

        return iso(self.clock.now())


def tool_versions(yt_dlp_version: str | None) -> dict[str, Any]:
    return {
        "coffee_kb_version": __version__,
        "yt_dlp_version": yt_dlp_version,
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
    }

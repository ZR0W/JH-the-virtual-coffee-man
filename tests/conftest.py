from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coffee_kb.clock import Clock
from coffee_kb.config import Config, FetchConfig
from coffee_kb.manifest import Manifest
from coffee_kb.paths import DataPaths

FIXTURES = Path(__file__).parent / "fixtures"


class FakeClock(Clock):
    """Never sleeps for real: ``sleep`` just advances the fake time and records the call."""

    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 10, 14, 9, 30, 0, tzinfo=UTC)
        self.sleeps: list[float] = []
        self._mono = 0.0

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)
        self._mono += seconds

    def monotonic(self) -> float:
        return self._mono

    def advance(self, **kwargs: float) -> None:
        self.current += timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def rng() -> random.Random:
    return random.Random(1234)


@pytest.fixture
def data_paths(tmp_path: Path) -> DataPaths:
    return DataPaths(tmp_path / "data")


@pytest.fixture
def cfg(data_paths: DataPaths) -> Config:
    return Config(data_dir=data_paths.root, fetch=FetchConfig())


@pytest.fixture
def manifest(data_paths: DataPaths):
    m = Manifest(data_paths.manifest)
    yield m
    m.close()


@pytest.fixture
def env(tmp_path: Path, clock: FakeClock, monkeypatch):
    """A config file + fake network wired into the CLI. ``env.run(...)`` runs a command."""
    from fakes import FakeYouTube, sponsorblock_transport

    from coffee_kb import cli

    conf = tmp_path / "config.toml"
    conf.write_text('[paths]\ndata_dir = "data"\n', encoding="utf-8")

    class Env:
        def __init__(self) -> None:
            self.config = conf
            self.paths = DataPaths(tmp_path / "data")
            self.clock = clock
            self.yt = FakeYouTube()
            self.sb_handler = None
            self.sb_requests: list = []

        def run(self, *argv: str) -> int:
            def http(_cfg):
                client, seen = sponsorblock_transport(self.sb_handler)
                self._seen = seen
                return client

            monkeypatch.setattr(cli, "make_youtube_client", lambda _cfg: self.yt)
            monkeypatch.setattr(cli, "make_http_client", http)
            code = cli.main(["--config", str(conf), *argv], clock=self.clock)
            self.sb_requests.extend(getattr(self, "_seen", []))
            self._seen = []
            return code

        def manifest(self) -> Manifest:
            return Manifest(self.paths.manifest)

    return Env()

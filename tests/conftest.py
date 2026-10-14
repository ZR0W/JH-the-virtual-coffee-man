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

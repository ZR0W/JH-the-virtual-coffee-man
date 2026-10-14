"""Load and validate ``config.toml``.

Unknown keys are an error (a typo must never silently fall back to a default), and the
pacing floors from ``safety.py`` are checked here, at load time. There is deliberately no
CLI flag or environment variable that overrides pacing.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from coffee_kb import safety

DEFAULT_CONFIG_NAME = "config.toml"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class PathsConfig:
    data_dir: str = "data"


@dataclass(frozen=True)
class YoutubeConfig:
    channel_handle: str = "@jameshoffmann"
    tabs: tuple[str, ...] = ("videos", "shorts", "streams")
    caption_languages: tuple[str, ...] = ("en", "en-GB", "en-US")


@dataclass(frozen=True)
class FetchConfig:
    sleep_between_videos_s: float = 60
    sleep_between_videos_jitter_s: float = 60
    sleep_between_requests_s: float = 5
    sleep_between_requests_max_s: float = 10
    sleep_before_subtitles_s: float = 10
    max_videos_per_24h: int = 30
    discover_min_interval_days: float = 7
    max_transient_retries: int = 2
    transient_retry_wait_s: float = 60
    cooldown_min_hours: float = 48
    max_wait_for_cap_minutes: float = 180
    socket_timeout_s: float = 30
    audio: bool = False


@dataclass(frozen=True)
class WebConfig:
    # 127.0.0.1 = only this machine (use an SSH tunnel); 0.0.0.0 = the whole LAN (pair it with
    # a firewall rule limited to your home subnet). The web app is read-only either way.
    host: str = "127.0.0.1"
    port: int = 8787


@dataclass(frozen=True)
class SponsorBlockConfig:
    base_url: str = "https://sponsor.ajay.app"
    categories: tuple[str, ...] = (
        "sponsor",
        "selfpromo",
        "interaction",
        "intro",
        "outro",
        "preview",
        "music_offtopic",
        "filler",
    )


@dataclass(frozen=True)
class Config:
    data_dir: Path
    youtube: YoutubeConfig = field(default_factory=YoutubeConfig)
    fetch: FetchConfig = field(default_factory=FetchConfig)
    sponsorblock: SponsorBlockConfig = field(default_factory=SponsorBlockConfig)
    web: WebConfig = field(default_factory=WebConfig)
    config_path: Path | None = None


_SECTIONS: dict[str, type] = {
    "paths": PathsConfig,
    "youtube": YoutubeConfig,
    "fetch": FetchConfig,
    "sponsorblock": SponsorBlockConfig,
    "web": WebConfig,
}


def _coerce(section: str, key: str, type_name: str, value: Any) -> Any:
    where = f"[{section}] {key}"
    if type_name == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{where} must be true or false, got {value!r}")
        return value
    if type_name == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where} must be an integer, got {value!r}")
        return value
    if type_name == "float":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"{where} must be a number, got {value!r}")
        return float(value)
    if type_name == "str":
        if not isinstance(value, str):
            raise ConfigError(f"{where} must be a string, got {value!r}")
        return value
    if type_name == "tuple[str, ...]":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{where} must be a list of strings, got {value!r}")
        return tuple(value)
    raise ConfigError(f"{where}: unsupported type {type_name}")  # pragma: no cover


def _build_section(name: str, cls: type, raw: dict[str, Any]) -> Any:
    types = {f.name: str(f.type) for f in fields(cls)}
    unknown = sorted(set(raw) - set(types))
    if unknown:
        raise ConfigError(f"unknown key(s) in [{name}]: {', '.join(unknown)}")
    values = {k: _coerce(name, k, types[k], v) for k, v in raw.items()}
    return cls(**values)


def config_from_dict(
    raw: dict[str, Any], base_dir: Path, config_path: Path | None = None
) -> Config:
    unknown = sorted(set(raw) - set(_SECTIONS))
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(unknown)}")
    built: dict[str, Any] = {}
    for name, cls in _SECTIONS.items():
        section = raw.get(name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"[{name}] must be a table")
        built[name] = _build_section(name, cls, section)

    data_dir = Path(built["paths"].data_dir)
    if not data_dir.is_absolute():
        data_dir = base_dir / data_dir
    cfg = Config(
        data_dir=data_dir,
        youtube=built["youtube"],
        fetch=built["fetch"],
        sponsorblock=built["sponsorblock"],
        web=built["web"],
        config_path=config_path,
    )
    try:
        safety.validate_fetch_config(cfg.fetch)
    except safety.UnsafeConfigError as exc:
        raise ConfigError(str(exc)) from exc
    if not 1 <= cfg.web.port <= 65535:
        raise ConfigError("[web] port must be between 1 and 65535")
    if not cfg.youtube.channel_handle.startswith("@"):
        raise ConfigError("[youtube] channel_handle must be a handle starting with '@'")
    bad_tabs = sorted(set(cfg.youtube.tabs) - {"videos", "shorts", "streams"})
    if bad_tabs:
        raise ConfigError(f"[youtube] tabs: unsupported tab(s) {', '.join(bad_tabs)}")
    return cfg


def load_config(path: Path) -> Config:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(
            f"config file not found: {path}. Run from the repository root or pass --config."
        )
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return config_from_dict(raw, base_dir=path.resolve().parent, config_path=path.resolve())

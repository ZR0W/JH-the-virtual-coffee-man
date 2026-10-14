"""``coffee-kb`` command line (docs/DESIGN.md §6).

Only ``discover`` and ``fetch`` talk to YouTube. ``normalize``, ``report``, ``status`` and
``clear-cooldown`` are offline. There is deliberately no option to change pacing here: it
comes only from config.toml and is checked against the floors in safety.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from coffee_kb import __version__, safety
from coffee_kb.clock import Clock, iso
from coffee_kb.config import DEFAULT_CONFIG_NAME, Config, ConfigError, load_config
from coffee_kb.manifest import Manifest
from coffee_kb.paths import DataPaths

log = logging.getLogger("coffee_kb")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

UPGRADE_HINT = (
    "Before each run, upgrade yt-dlp (many apparent blocks are an outdated extractor):\n"
    "  uv lock --upgrade-package yt-dlp\n"
    "  uv sync"
)


def _setup_logging(verbose: bool) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    root = logging.getLogger("coffee_kb")
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.propagate = False


def _ytdlp_version() -> str:
    try:
        from yt_dlp.version import __version__ as v
    except Exception:  # pragma: no cover - yt-dlp is a hard dependency
        return "unknown"
    return v


# --- status / clear-cooldown ----------------------------------------------------------------


def cmd_status(cfg: Config, paths: DataPaths, clock: Clock, args: argparse.Namespace) -> int:
    with Manifest(paths.manifest) as manifest:
        state = safety.safety_state(paths, cfg.fetch, manifest, clock.now())
    state["yt_dlp_version"] = _ytdlp_version()
    state["coffee_kb_version"] = __version__
    state["data_dir"] = str(paths.root)
    if getattr(args, "format", "text") == "json":
        print(json.dumps(state, indent=2))
        return EXIT_OK
    print(f"coffee-kb {__version__}  |  yt-dlp {state['yt_dlp_version']}  |  data: {paths.root}")
    cd = state["cooldown"]
    if cd is None:
        print("Cooldown latch: none")
    else:
        print(f"Cooldown latch: ACTIVE since {cd['detected_at']}")
        print(f"  reason: {cd['reason']}")
        if cd.get("item_id"):
            print(f"  item:   {cd['item_id']}")
        print(f"  discover/fetch refuse to run. Can be cleared from {cd['can_clear_from']}.")
    print(
        f"Daily cap: {state['fetches_last_24h']} of {state['max_videos_per_24h']} video fetch "
        f"attempts used in the last 24 h; {state['remaining_allowance']} remaining"
    )
    if state["remaining_allowance"] > 0:
        print("Next eligible fetch: now")
    else:
        print(f"Next eligible fetch: {state['next_eligible_fetch']}")
    if state["last_discover"]:
        print(
            f"Last discover: {state['last_discover']} (next allowed {state['next_discover_allowed']})"
        )
    else:
        print("Last discover: never")
    print()
    print(UPGRADE_HINT)
    return EXIT_OK


def cmd_clear_cooldown(
    cfg: Config, paths: DataPaths, clock: Clock, args: argparse.Namespace
) -> int:
    latch = safety.read_cooldown(paths)
    if latch is None:
        print("No cooldown latch present.")
        return EXIT_OK
    print(f"Cooldown latch {latch.describe()}")
    allowed = safety.cooldown_clear_time(latch, cfg.fetch)
    if clock.now() < allowed:
        print(
            f"Refusing: the latch can only be cleared from {iso(allowed)} "
            f"({cfg.fetch.cooldown_min_hours:.0f} h after the block). Waiting is the only fix."
        )
        return EXIT_ERROR
    print()
    print(UPGRADE_HINT)
    print()
    print("Only clear this after upgrading yt-dlp and checking that YouTube works normally")
    print("in a browser on this network.")
    try:
        answer = input("Type 'clear' to remove the latch: ")
    except EOFError:
        answer = ""
    if answer.strip() != "clear":
        print("Not cleared.")
        return EXIT_ERROR
    try:
        safety.clear_cooldown(paths, cfg.fetch, clock.now())
    except safety.CooldownTooRecent as exc:  # pragma: no cover - checked above
        print(f"Refusing: {exc}")
        return EXIT_ERROR
    print(f"Cooldown cleared; the latch was archived under {paths.cooldown_history_dir}.")
    return EXIT_OK


# --- parser -----------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="coffee-kb",
        description="Collect James Hoffmann's public YouTube videos into a local knowledge base.",
    )
    p.add_argument("--version", action="version", version=f"coffee-kb {__version__}")
    p.add_argument(
        "--config",
        type=Path,
        default=Path(DEFAULT_CONFIG_NAME),
        help="path to config.toml (default: ./config.toml)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    s = sub.add_parser("status", help="cooldown state, daily cap, next eligible time [offline]")
    s.add_argument("--format", choices=["text", "json"], default="text")
    s.set_defaults(handler=cmd_status)

    s = sub.add_parser(
        "clear-cooldown",
        help="remove the cooldown latch; user-only, refuses within 48 h of the block [offline]",
    )
    s.set_defaults(handler=cmd_clear_cooldown)
    return p


def main(argv: Sequence[str] | None = None, clock: Clock | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # Titles are Unicode; a redirected Windows console would otherwise use cp1252.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"coffee-kb: config error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    paths = DataPaths(cfg.data_dir)
    return int(args.handler(cfg, paths, clock or Clock(), args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

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
from coffee_kb.runtime import Runtime

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


# --- network-facing commands ------------------------------------------------------------


def make_youtube_client(cfg: Config):  # seam for tests
    from coffee_kb.sources.youtube.client import YtDlpClient

    return YtDlpClient(cfg.fetch)


def make_http_client(cfg: Config):  # seam for tests
    from coffee_kb.sources.youtube.sponsorblock import make_client

    return make_client(cfg.fetch.socket_timeout_s)


def _args_dict(args: argparse.Namespace) -> dict:
    out = {}
    for k, v in vars(args).items():
        if k == "handler":
            continue
        out[k] = str(v) if isinstance(v, Path) else v
    return out


def _refuse_if_cooling_down(paths: DataPaths, command: str) -> int | None:
    try:
        safety.ensure_no_cooldown(paths)
    except safety.CooldownActive as exc:
        print(
            f"coffee-kb {command}: refusing to run: cooldown latch present "
            f"({exc.latch.describe()}).\n"
            "No YouTube requests were made. Wait it out; see `coffee-kb status`.",
            file=sys.stderr,
        )
        return safety.EXIT_TEMPFAIL
    return None


def _run_networked(cfg: Config, paths: DataPaths, clock: Clock, args: argparse.Namespace, body):
    """Cooldown check → run lock → manifest run record → body(runtime) → (exit code, summary)."""
    command = args.command
    refused = _refuse_if_cooling_down(paths, command)
    if refused is not None:
        return refused
    try:
        with safety.run_lock(paths, clock, command), Manifest(paths.manifest) as manifest:
            http = make_http_client(cfg)
            try:
                rt = Runtime(
                    cfg=cfg,
                    paths=paths,
                    manifest=manifest,
                    clock=clock,
                    pacer=safety.Pacer(cfg.fetch, clock),
                    youtube=make_youtube_client(cfg),
                    http=http,
                    args=_args_dict(args),
                )
                rt.run_id = manifest.start_run(command, rt.args, iso(clock.now()))
                try:
                    code, summary = body(rt)
                except KeyboardInterrupt:
                    manifest.finish_run(rt.run_id, iso(clock.now()), 130, {"interrupted": True})
                    raise
                except Exception as exc:
                    manifest.finish_run(
                        rt.run_id, iso(clock.now()), EXIT_ERROR, {"crash": repr(exc)}
                    )
                    raise
                manifest.finish_run(rt.run_id, iso(clock.now()), code, summary)
                return code
            finally:
                http.close()
    except safety.LockHeld as exc:
        print(f"coffee-kb {command}: {exc}", file=sys.stderr)
        return EXIT_ERROR


def cmd_discover(cfg: Config, paths: DataPaths, clock: Clock, args: argparse.Namespace) -> int:
    from coffee_kb.sources.youtube.discover import run_discover

    def body(rt: Runtime):
        result = run_discover(rt, force=args.force)
        return result.exit_code, result.summary()

    return _run_networked(cfg, paths, clock, args, body)


def cmd_fetch(cfg: Config, paths: DataPaths, clock: Clock, args: argparse.Namespace) -> int:
    from coffee_kb.sources.youtube.fetch import FetchOptions, run_fetch

    if args.limit is not None and args.limit < 1:
        print("coffee-kb fetch: --limit must be at least 1", file=sys.stderr)
        return EXIT_USAGE
    opts = FetchOptions(
        limit=args.limit,
        videos=list(args.video or []),
        since=args.since,
        item_type=args.type,
        audio=bool(args.audio or cfg.fetch.audio),
        refresh=args.refresh,
        dry_run=args.dry_run,
        wait_for_cap=args.wait_for_cap,
    )
    if opts.audio:
        log.warning("audio download is ON (heaviest, most scrutinized traffic; see README)")

    def body(rt: Runtime):
        summary = run_fetch(rt, opts)
        return summary.exit_code, summary.as_dict()

    if opts.dry_run:
        latch = safety.read_cooldown(paths)
        if latch is not None:
            print(f"note: cooldown latch present ({latch.describe()}); a real fetch would refuse.")
        with Manifest(paths.manifest) as manifest:
            rt = Runtime(cfg, paths, manifest, clock, safety.Pacer(cfg.fetch, clock))
            return run_fetch(rt, opts).exit_code
    return _run_networked(cfg, paths, clock, args, body)


# --- offline commands ------------------------------------------------------------------------


def cmd_normalize(cfg: Config, paths: DataPaths, clock: Clock, args: argparse.Namespace) -> int:
    from coffee_kb.normalize.pipeline import run_normalize

    with Manifest(paths.manifest) as manifest:
        run_id = manifest.start_run("normalize", _args_dict(args), iso(clock.now()))
        summary = run_normalize(paths, list(args.video or []) or None)
        code = EXIT_ERROR if summary.errors else EXIT_OK
        manifest.finish_run(run_id, iso(clock.now()), code, summary.as_dict())
    for key, err in sorted(summary.errors.items()):
        print(f"normalize error: {key}: {err}", file=sys.stderr)
    return code


def cmd_report(cfg: Config, paths: DataPaths, clock: Clock, args: argparse.Namespace) -> int:
    from coffee_kb.report import build_report, render_text

    with Manifest(paths.manifest) as manifest:
        rep = build_report(cfg, paths, manifest, clock.now())
    if args.format == "json":
        print(json.dumps(rep, indent=2, ensure_ascii=False))
    else:
        print(render_text(rep))
    return EXIT_OK


def cmd_dashboard(cfg: Config, paths: DataPaths, clock: Clock, args: argparse.Namespace) -> int:
    from coffee_kb.dashboard import build_dashboard, render_html, systemd_info

    with Manifest(paths.manifest) as manifest:
        data = build_dashboard(cfg, paths, manifest, clock.now(), systemd_info())
    out = args.output or (paths.root / "dashboard.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(data), encoding="utf-8")
    print(out)
    return EXIT_OK


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


def _video_arg(value: str) -> str:
    """A video ID, or a YouTube URL containing one."""
    import re

    from coffee_kb.paths import InvalidIdError, check_item_id

    value = value.strip()
    m = re.search(r"(?:[?&]v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})", value)
    if m:
        value = m.group(1)
    try:
        return check_item_id(value)
    except InvalidIdError:
        raise argparse.ArgumentTypeError(f"not a YouTube video ID or URL: {value!r}") from None


def _date_arg(value: str) -> str:
    import datetime as _dt

    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value!r}") from None
    return value


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

    s = sub.add_parser(
        "discover", help="refresh the channel listing into the manifest [talks to YouTube]"
    )
    s.add_argument(
        "--force",
        action="store_true",
        help="run even if discover ran within discover_min_interval_days (cooldown still applies)",
    )
    s.set_defaults(handler=cmd_discover)

    s = sub.add_parser(
        "fetch",
        help="fetch metadata, captions, SponsorBlock for not-yet-complete videos [talks to YouTube]",
    )
    s.add_argument("--limit", type=int, help="at most N videos (clamped to the daily allowance)")
    s.add_argument(
        "--video",
        action="append",
        type=_video_arg,
        metavar="ID",
        help="fetch this video ID (repeatable)",
    )
    s.add_argument(
        "--since", type=_date_arg, metavar="YYYY-MM-DD", help="only videos uploaded on/after"
    )
    s.add_argument("--type", choices=["video", "short", "stream"], help="only this item type")
    s.add_argument("--audio", action="store_true", help="also download audio (off by default)")
    s.add_argument("--refresh", action="store_true", help="new snapshot even if one is complete")
    s.add_argument("--dry-run", action="store_true", help="show what would be fetched; no network")
    s.add_argument(
        "--wait-for-cap",
        action="store_true",
        help="if the rolling 24 h cap is full, wait (up to max_wait_for_cap_minutes) for slots "
        "to free up instead of exiting; used by the daily timer",
    )
    s.set_defaults(handler=cmd_fetch)

    s = sub.add_parser(
        "normalize", help="rebuild normalized/ from the latest raw snapshots [offline]"
    )
    s.add_argument(
        "--video",
        action="append",
        type=_video_arg,
        metavar="ID",
        help="only this video (repeatable)",
    )
    s.set_defaults(handler=cmd_normalize)

    s = sub.add_parser("report", help="coverage, errors and safety state [offline]")
    s.add_argument("--format", choices=["text", "json"], default="text")
    s.set_defaults(handler=cmd_report)

    s = sub.add_parser(
        "dashboard", help="write an HTML status page (default data/dashboard.html) [offline]"
    )
    s.add_argument("--output", type=Path, help="where to write the HTML file")
    s.set_defaults(handler=cmd_dashboard)

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

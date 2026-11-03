# CLAUDE.md — agent guide for coffee-kb

Read this file first. Then, depending on the task:
- operating, debugging, updating or removing the running system → [docs/OPERATIONS.md](docs/OPERATIONS.md)
- design, data formats, roadmap → [docs/DESIGN.md](docs/DESIGN.md) (§6.8 lists where the build refines the spec)
- user-facing setup and commands → [README.md](README.md)

## What this project is

**coffee-kb** is a personal knowledge base distilled from James Hoffmann's public YouTube videos
(https://www.youtube.com/@jameshoffmann). The long-term goal is to collect his coffee knowledge once,
store it with full provenance (video + timestamp), and then explore many ways of looking at it:
a knowledge graph, visualizations, a cited Q&A assistant, a non-LLM chatbot, flashcards, and so on.

The project has two halves:

1. **Data collection** (Phase 1) — fetch, store raw, normalize, and report coverage. **Built, tested,
   and deployed** on the user's home server (see "Current state" below).
2. **Everything downstream** (Phases 2–5) — extraction, synthesis, retrieval, and interfaces. These are
   documented in `docs/DESIGN.md` so the Phase 1 design doesn't block them. **Not started; don't build
   them unless the user asks.**

## Current state (2026-10-14)

- Phase 1 is complete: CLI, safety mechanisms, normalizer, report, and two server components.
  177 offline tests; `uv run ruff check . && uv run ruff format --check . && uv run pytest` is clean.
- Checkpoints passed: B (1-video smoke test on the user's Windows PC), C (discover + 20-video trial
  on the PC), D (moved to the home server).
- On the home server (Ubuntu, `~/coffee-kb`, systemd **user** units with linger):
  - **collector**: `coffee-kb-collector.timer` runs `coffee-kb collector run` daily at 03:00 + 0–2 h
    (discover weekly → `fetch --wait-for-cap` → normalize), logging to `data/logs/coffee-kb.log`.
  - **web**: `coffee-kb-web.service`, a read-only dashboard on port 8787 (LAN, ufw-limited).
- The PC's copy of `data/` is latched with a `COOLDOWN` ("fetching moved to the home server"): only
  the server fetches. 411 items listed; the backlog finishes about two weeks after deployment.
- Work lives on branch `claude/exciting-ramanujan-wa0f9u` until the user merges it.
- Full inventory of what is installed where, troubleshooting, updates and a clean uninstall:
  [docs/OPERATIONS.md](docs/OPERATIONS.md).

## ⚠️ YouTube request safety — read before writing or running any fetch code

YouTube blocks clients it thinks are bots. A block applies to the **user's home IP**, which the whole
household shares, so a careless run causes real inconvenience (failed fetches, CAPTCHAs on YouTube and
Google Search for hours to days). There is no deadline on this project. **Slow and complete beats fast.**
Full details in `docs/DESIGN.md` §6.6.

**Only `discover` and `fetch` talk to YouTube.** `normalize` and `report` are offline. SponsorBlock is a
separate service, not YouTube.

Rules, all enforced in code, not just documented:

1. **Very slow pacing by default.** 60–120 s (randomized) between videos, 5–10 s between individual
   requests within a video (yt-dlp `sleep_interval_requests`), 10 s before each caption download
   (yt-dlp `sleep_interval_subtitles`). One video at a time, never concurrent.
2. **Hard floors.** Config values below 30 s between videos or 3 s between requests are rejected with an
   error. Do not lower the floors, add a flag to bypass them, or "temporarily" speed things up for testing.
3. **Daily cap.** At most 30 videos fetched per rolling 24 h, counted from the manifest across runs.
   The whole channel takes about two weeks. That's intended.
4. **Stop on the first warning sign.** "Sign in to confirm you're not a bot", HTTP 429, HTTP 403 on
   YouTube endpoints, or a CAPTCHA/consent page → stop the run immediately, no retries.
5. **Cooldown latch.** That stop also writes `data/COOLDOWN` (reason + timestamp). While it exists,
   `discover` and `fetch` refuse to run, even from a scheduled timer. Only the user clears it
   (`coffee-kb clear-cooldown`), after waiting at least 48 h. Never clear it yourself.
6. **No evasion, ever.** No browser/account cookies, no proxies or VPNs, no rotating user agents, no
   PO-token generators or other bot-detection workarounds. If blocked, the answer is to wait.
7. **Audio stays off** unless the user explicitly turns it on. It's the heaviest and most scrutinized
   traffic.
8. **Agents don't run real collection.** Real fetching is done by the scheduled collector on the
   home server (or by the user). From a cloud session, make **at most one real single-video fetch
   attempt** and only to check fixture shapes (cloud IPs are usually blocked; the agent proxy refused
   YouTube during the build). On the server, see "Working on the server" below.

## Working on the server (if you've been given access)

The user may give you SSH access to the home server to diagnose, fix, update, or uninstall. Rules:

- **Diagnose read-only first.** Start with the diagnostic bundle in `docs/OPERATIONS.md` §4 (no
  YouTube requests, no changes). Report what you found before changing anything.
- **Allowed without asking:** the read-only commands in OPERATIONS §3–§4; `coffee-kb normalize`,
  `report`, `status`, `collector status|logs`, `web status`; restarting the web app
  (`systemctl --user restart coffee-kb-web`, a `Type=notify` unit: non-zero exit = it failed to start); re-running `collector install` / `web install`
  (idempotent).
- **Ask the user first:** anything with `sudo` (ufw, linger, apt); `git pull` / code updates
  (OPERATIONS §6); editing `config.toml`; `collector run`, `collector run-now`, or any manual
  `fetch`/`discover` (they talk to YouTube and use the daily cap); uninstalling a component;
  deleting anything under `data/` or the checkout. Do a clean uninstall only when the user asks,
  following OPERATIONS §7, and confirm with them whether to keep the data first.
- **Never:** clear or delete `data/COOLDOWN` (only the user runs `clear-cooldown`, after 48 h);
  lower pacing/cap or bypass floors; edit `data/raw/`; add cookies, proxies, user agents, PO-token
  providers or other evasion; enable audio; run fetch/discover "to test"; fetch from a second
  machine (the PC is retired); put hostnames, usernames or IPs into the repo.
- If a cooldown latch has tripped, stop and discuss; never suggest clearing it early.
- The server runs **Ubuntu** (bash); the user's PC runs **Windows** (PowerShell). Give commands in
  the right shell for the machine they run on.

## Changing the code

- Code and tests must work on Windows and Linux (`pathlib`, `os.replace`; systemd management in
  `service.py` is Linux-only and guarded). Tests are offline: fake YouTube/SponsorBlock clients
  (`tests/fakes.py`), fake clock (no real sleeps), small hand-trimmed fixtures.
- Safety mechanisms (DESIGN §6.6) each have offline tests; keep them passing and add tests for new
  behaviour. Don't weaken a safety test to make a change pass.
- After changing code that ships to the server, the user updates with OPERATIONS §6.
- The checkpoint history (how Phase 1 was built and rolled out) is in DESIGN §6.7–§6.8 and §10.

### Out of scope unless the user asks

- Any LLM call, extraction, chunking, embeddings, vector DB, knowledge graph, Obsidian export, or
  visualization beyond the status dashboard. Hooks (directories, stable IDs, provenance) exist.
- Downloading full video files.
- The official YouTube Data API (yt-dlp only; DESIGN §6.6, §10).
- Fetching all comments.

## Non-negotiable design rules

These come from the design discussion; don't relax them without asking the user.

1. **`data/raw/` is immutable.** Anything fetched is written once into a timestamped snapshot
   directory and never edited. A refresh writes a *new* snapshot; old ones are kept.
2. **Everything outside `raw/` is rebuildable.** `normalized/` and (later) `derived/` must be
   regenerable from `raw/` with a single command. Never put information only in `normalized/`.
3. **Provenance on everything.** Every record outside `raw/` carries `source_type`, `item_id`
   (video ID), timestamps in ms where applicable, the raw snapshot it came from, and the
   pipeline/tool version that produced it.
4. **Stable IDs.** Segments are identified by `(item_id, track, start_ms)`, never by array index.
5. **Source-generic data model.** Everything is `source_type → item → segment`. Only `youtube`
   exists now, but don't hard-code that assumption into paths or schema.
6. **Idempotent, incremental, resumable.** Every command can be interrupted and re-run safely.
   Per-component status lives in the manifest.
7. **Never fail silently.** Missing captions, unavailable videos, and fetch errors are recorded in
   the manifest and surfaced by `report`.

## Other fetching constraints

(The safety rules near the top of this file come first.)

- **Public content only.** No members-only videos, no paid content, no books. Skip anything that
  requires authentication and record it as `skipped_restricted`.
- Keep `yt-dlp` easy to upgrade (it breaks and gets fixed often); pin a minimum, not an exact version.
  Many apparent "blocks" are an outdated yt-dlp; the run docs tell the user to upgrade first.
- The systemd units are generated by `coffee-kb collector install` / `coffee-kb web install`
  (`src/coffee_kb/service.py`; `deploy/README.md` points there). Don't put the user's hostname,
  username, or IP in the repo.

## Data and git

- **`data/` is git-ignored** (except a `data/README.md` describing the layout). It holds third-party
  content (transcripts, possibly audio) and can be many GB. Never commit fetched content.
- Test fixtures under `tests/fixtures/` should be **small, hand-trimmed excerpts** (a few cues), not
  full transcripts.

## Scope, naming, and legal notes

- Framing: this is "a knowledge base of Hoffmann's videos," **not** an impersonation of him. Future
  interfaces should cite videos, not pretend to be him.
- Distribution is **personal use**. Don't add publishing, hosting, or public sharing steps
  (e.g. publishing an Obsidian vault with Quartz) without flagging it to the user — publishing changes
  the copyright/right-of-publicity picture. See `docs/DESIGN.md` §9.
- Downloading media (audio) is against YouTube's ToS even for personal use; that's why audio is
  opt-in. Captions + metadata are the lower-risk tier.

## Tech stack (Phase 1)

- Python ≥ 3.11, managed with `uv` (`pyproject.toml`, `uv.lock`). Tested on 3.11, 3.13; the server
  uses Ubuntu's 3.14.
- `yt-dlp` (as a Python library), `httpx` for SponsorBlock, stdlib `sqlite3`, `argparse` for the CLI,
  stdlib `http.server` for the web app.
- `pytest` for tests; `ruff` for lint/format.
- No other dependencies without a reason.

## Conventions

- Package lives in `src/coffee_kb/`; CLI entry point `coffee-kb` → `coffee_kb.cli:main`.
- All times stored as integer milliseconds; all timestamps as UTC ISO-8601 (`2026-10-14T09:30:00Z`).
- JSON outputs are UTF-8, one record per line for JSONL.
- Log to stderr; `--verbose` for debug. Collection commands and the web app also log to
  `data/logs/coffee-kb.log` (rotating).

## Repository map

```
src/coffee_kb/
  cli.py          argparse entry point: discover, fetch, normalize, report, status, clear-cooldown,
                  collector {install,uninstall,status,logs,run,run-now}, web {install,uninstall,status,serve}
  config.py       config.toml loading + validation (unknown keys and below-floor values are errors)
  safety.py       floors, pacer, daily cap, block classifier, cooldown latch, run lock, no-evasion guard
  manifest.py     SQLite manifest (items, components, runs, fetch_attempts); read-only mode for web
  paths.py        all data/ paths;  snapshots.py  write-once atomic snapshot dirs
  sources/youtube/{client,discover,fetch,sponsorblock}.py   the only YouTube/SponsorBlock traffic
  normalize/{captions,pipeline}.py   raw/ -> normalized/ (pure, offline)
  report.py       coverage/errors/safety report;  dashboard.py  dashboard page;  web.py  web app
  service.py      systemd user units for the collector and web components
  logs.py         data/logs/coffee-kb.log;  sdnotify.py  systemd READY=1 for the Type=notify web unit
tests/            offline tests (fakes.py, fixtures/youtube/)
scripts/trial_summary.py   offline transcript quality summary (checkpoint C)
docs/DESIGN.md  docs/OPERATIONS.md  README.md  data/README.md  deploy/README.md
config.toml       the only configuration (pacing floors enforced in code)
```
- Run before every commit: `uv run ruff check . && uv run ruff format --check . && uv run pytest`.

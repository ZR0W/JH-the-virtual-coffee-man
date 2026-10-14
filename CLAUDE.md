# CLAUDE.md — agent guide for coffee-kb

Read this file first, then [DESIGN.md](DESIGN.md) for the full design and roadmap.

## What this project is

**coffee-kb** is a personal knowledge base distilled from James Hoffmann's public YouTube videos
(https://www.youtube.com/@jameshoffmann). The long-term goal is to collect his coffee knowledge once,
store it with full provenance (video + timestamp), and then explore many ways of looking at it:
a knowledge graph, visualizations, a cited Q&A assistant, a non-LLM chatbot, flashcards, and so on.

The project has two halves:

1. **Data collection** (Phase 1) — fetch, store raw, normalize, and report coverage. **This is the only
   phase you are building now.**
2. **Everything downstream** (Phases 2–5) — extraction, synthesis, retrieval, and interfaces. These are
   documented in `DESIGN.md` so the Phase 1 design doesn't block them. **Do not build them yet.**

## ⚠️ YouTube request safety — read before writing or running any fetch code

YouTube blocks clients it thinks are bots. A block applies to the **user's home IP**, which the whole
household shares, so a careless run causes real inconvenience (failed fetches, CAPTCHAs on YouTube and
Google Search for hours to days). There is no deadline on this project. **Slow and complete beats fast.**
Full details in `DESIGN.md` §6.6.

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
8. **Agents don't bulk-fetch.** In a development session (cloud or local), you may make **at most one
   real single-video fetch** to check wiring and fixture shapes. Never run `fetch` without `--video` or
   with `--limit` above 1. The real collection is run by the user on their home server (§ Deployment in
   DESIGN.md). Cloud/datacenter IPs are usually blocked on sight; if that happens, say so and stop.

## Your task in this session: Phase 1 only

Build the data-collection pipeline described in `DESIGN.md` §4–§6. Concretely:

1. Scaffold the repo as laid out in `DESIGN.md` §3 (Python package, `data/` tree, tests, config).
2. Implement the SQLite **manifest** (schema in §5.2).
3. Implement the CLI commands `discover`, `fetch`, `normalize`, `report` (spec in §6).
4. Implement fetchers: yt-dlp metadata, caption tracks (human + auto), SponsorBlock segments.
   Audio download is implemented behind a flag that is **off by default**.
5. Implement caption normalization to the transcript JSONL format (§5.4), including the
   auto-caption rolling-duplicate cleanup.
6. Implement the coverage report (§6.4).
7. Implement every safety mechanism in §6.6 (pacing, floors, daily cap, stop-on-warning, cooldown
   latch, `clear-cooldown`), each with an offline test. **These are core features, not polish.**
8. Add `deploy/` systemd service + timer files and a server setup section in the README (§6.7).
9. Write offline tests using checked-in fixture files (no network in tests).
10. Update `README.md` with setup and run instructions, including a prominent "before you run fetch"
    safety section.

**Definition of done:** `pytest` passes offline, including tests that prove: pacing floors reject
low values, the daily cap stops at 30, a simulated bot-detection error stops the run and writes
`data/COOLDOWN`, and `discover`/`fetch` refuse to run while it exists. The user can then run
`coffee-kb discover && coffee-kb fetch --limit 20 && coffee-kb normalize && coffee-kb report`
on their home server and get the `data/` tree, populated manifest, normalized transcripts, and a
coverage report for 20 videos (that trial takes roughly 30–45 minutes, by design). Re-running does
no new network work for already-complete videos.

### Explicitly out of scope for now

- Any LLM call, extraction, chunking, embeddings, vector DB, knowledge graph, Obsidian export, UI,
  or visualization. Leave hooks (directories, stable IDs, provenance fields) but no implementation.
- Downloading full video files.
- The official YouTube Data API. yt-dlp only for now; the user will decide after the 20-video trial
  (DESIGN.md §6.6, §10).
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
- Phase 1 also ships `deploy/` files (systemd service + timer) for running the daily batch on the
  user's home server. See DESIGN.md §6.7. Don't put the user's hostname, username, or IP in the repo.

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
  the copyright/right-of-publicity picture. See `DESIGN.md` §9.
- Downloading media (audio) is against YouTube's ToS even for personal use; that's why audio is
  opt-in. Captions + metadata are the lower-risk tier.

## Tech stack (Phase 1)

- Python ≥ 3.11, managed with `uv` (`pyproject.toml`, `uv.lock`).
- `yt-dlp` (as a Python library), `httpx` for SponsorBlock, stdlib `sqlite3`, `argparse` for the CLI.
- `pytest` for tests; `ruff` for lint/format.
- No other dependencies without a reason.

## Conventions

- Package lives in `src/coffee_kb/`; CLI entry point `coffee-kb` → `coffee_kb.cli:main`.
- All times stored as integer milliseconds; all timestamps as UTC ISO-8601 (`2026-10-14T09:30:00Z`).
- JSON outputs are UTF-8, one record per line for JSONL.
- Log to stderr; `--verbose` for debug.
- Run before every commit: `uv run ruff check . && uv run ruff format --check . && uv run pytest`.

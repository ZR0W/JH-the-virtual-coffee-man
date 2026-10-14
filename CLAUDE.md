# CLAUDE.md — agent guide for coffee-kb

Read this file first, then [docs/DESIGN.md](docs/DESIGN.md) for the full design and roadmap.

## What this project is

**coffee-kb** is a personal knowledge base distilled from James Hoffmann's public YouTube videos
(https://www.youtube.com/@jameshoffmann). The long-term goal is to collect his coffee knowledge once,
store it with full provenance (video + timestamp), and then explore many ways of looking at it:
a knowledge graph, visualizations, a cited Q&A assistant, a non-LLM chatbot, flashcards, and so on.

The project has two halves:

1. **Data collection** (Phase 1) — fetch, store raw, normalize, and report coverage. **This is the only
   phase you are building now.**
2. **Everything downstream** (Phases 2–5) — extraction, synthesis, retrieval, and interfaces. These are
   documented in `docs/DESIGN.md` so the Phase 1 design doesn't block them. **Do not build them yet.**

## Your task in this session: Phase 1 only

Build the data-collection pipeline described in `docs/DESIGN.md` §4–§6. Concretely:

1. Scaffold the repo as laid out in `docs/DESIGN.md` §3 (Python package, `data/` tree, tests, config).
2. Implement the SQLite **manifest** (schema in §5.2).
3. Implement the CLI commands `discover`, `fetch`, `normalize`, `report` (spec in §6).
4. Implement fetchers: yt-dlp metadata, caption tracks (human + auto), SponsorBlock segments.
   Audio download is implemented behind a flag that is **off by default**.
5. Implement caption normalization to the transcript JSONL format (§5.4), including the
   auto-caption rolling-duplicate cleanup.
6. Implement the coverage report (§6.4).
7. Write offline tests using checked-in fixture files (no network in tests).
8. Update `README.md` with setup and run instructions.

**Definition of done:** `pytest` passes offline; running
`coffee-kb discover && coffee-kb fetch --limit 20 && coffee-kb normalize && coffee-kb report`
on a machine with YouTube access produces the `data/` tree, populated manifest, normalized
transcripts, and a coverage report for 20 videos. Re-running the same commands does no new network
work for already-complete videos.

### Explicitly out of scope for now

- Any LLM call, extraction, chunking, embeddings, vector DB, knowledge graph, Obsidian export, UI,
  or visualization. Leave hooks (directories, stable IDs, provenance fields) but no implementation.
- Downloading full video files.
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

## Fetching etiquette and environment constraints

- **Be slow on purpose.** Default sleep of several seconds between videos (configurable), modest
  concurrency (1 by default). This is a one-time archive, not a race.
- **Do not use the user's Google account cookies**, proxies, or any bot-detection evasion. If YouTube
  responds with "Sign in to confirm you're not a bot" or similar, stop, record the error, and tell
  the user. The intended place to run real fetches is the user's own home machine.
- **Cloud sessions are likely to be blocked** by YouTube (datacenter IPs). If you're running in one,
  build and test against fixtures, try at most one small real fetch to confirm wiring, and leave the
  bulk run to the user. Don't work around a block.
- **Public content only.** No members-only videos, no paid content, no books. Skip anything that
  requires authentication and record it as `skipped_restricted`.
- Keep `yt-dlp` easy to upgrade (it breaks and gets fixed often); pin a minimum, not an exact version.

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

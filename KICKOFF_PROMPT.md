# Kickoff prompt for the new Claude Code session

1. `CLAUDE.md` and `docs/DESIGN.md` are already in this repo.
2. Start a **cloud** Claude Code session on this repo and paste the following:

---

Read `CLAUDE.md` and `docs/DESIGN.md` in full before doing anything.

Build **Phase 1 (data collection) only**, as specified in CLAUDE.md "Your task in this session" and
docs/DESIGN.md §3–§6. Do not build anything from Phases 2–5.

## How we'll work

You build everything here in the cloud. I run every real fetch myself. Follow the checkpoints in
CLAUDE.md "How this session works" and docs/DESIGN.md §6.7:

1. **Build** (you): everything below, offline tests passing, pushed.
2. **Local smoke test** (me, my Windows PC): you give me PowerShell commands to set up and fetch ONE
   video, plus exactly what to paste back. Then stop and wait for my results.
3. **Local 20-video trial** (me, my PC): same pattern, with a checklist of what to inspect. Stop and wait.
4. **Move to my home server** (Ubuntu): ask me about the server setup before finalizing anything
   server-specific. We'll copy the PC's `data/` over so it continues where the trial stopped.

Don't move past a checkpoint without my results.

## ⚠️ YouTube safety comes first

This code will run against YouTube from my home network, which my household shares. A bot-detection
block would affect everyone at home for hours to days. Treat CLAUDE.md "YouTube request safety" and
docs/DESIGN.md §6.6 as hard requirements:

- Very slow by default: 60–120 s between videos, 5–10 s between requests, 10 s before subtitles,
  strictly one at a time. Enforce the floors in code, with no CLI or env override.
- Daily cap of 30 videos per 24 h, counted from the manifest.
- Stop on the first block signal, write the `data/COOLDOWN` latch, and refuse to run until I clear it.
- No cookies, proxies, VPNs, user-agent rotation, or any bot-detection workaround. Ever.
- **You make at most ONE real single-video fetch attempt from the cloud**, only to check fixture
  shapes. Never run `discover` or a multi-video `fetch`. If YouTube blocks you (likely), don't retry;
  tell me.
- Don't lower pacing to make tests or manual checks faster. Tests must mock time/sleep, not shorten it.

## Build order (commit after each step)

1. Scaffold: `pyproject.toml` (uv), `src/coffee_kb/` package, `config.toml`, `.gitignore` (ignore
   `data/` except `data/README.md`), `tests/`, ruff config.
2. `paths.py` + `manifest.py` (schema from DESIGN §5.2) with tests.
3. `safety.py` (DESIGN §6.6): config floors, daily cap, block-signal classifier, cooldown latch,
   `status` and `clear-cooldown` commands. Full offline tests. **Build this before any code that talks
   to YouTube**, and have `discover`/`fetch` call it before their first request.
4. `discover` command (self-throttled to weekly).
5. `fetch` command: metadata, manual + auto captions (json3, vtt fallback), SponsorBlock, optional
   `--audio`; atomic snapshot directories; `fetch.json` provenance; pacing via yt-dlp's
   request/subtitle sleep options plus our own between-video sleep.
6. `normalize` command, including auto-caption de-duplication, chapter join, and sponsor join, with
   fixture-based tests.
7. `report` command (including the safety-state section).
8. `deploy/` systemd service + timer **templates** (DESIGN §6.7), with placeholders and no personal
   hostnames, usernames, or IPs. We'll finalize them together at the server step.
9. README: setup for Windows (PowerShell) and Linux (bash), a prominent "Before you run fetch" safety
   section, the smoke test and 20-video trial, moving `data/` to the server, timer install, and
   monitoring commands.

The code must work on both Windows and Linux (`pathlib`, `os.replace` for the atomic snapshot rename,
nothing POSIX-only outside `deploy/`).

Tests must run offline using small fixtures. If you can reach YouTube, your one allowed real fetch is
a single video's info.json and json3 captions, trimmed to a few cues for fixtures. If you're blocked
(likely in a cloud environment), build from the documented formats and tell me.

When the build is done, give me checkpoint 2: the exact PowerShell commands to set up on my PC and
fetch one video, and what to paste back. Then wait.

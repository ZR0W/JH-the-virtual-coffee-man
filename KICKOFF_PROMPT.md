# Kickoff prompt for the new Claude Code session

1. `CLAUDE.md` and `DESIGN.md` are already in this repo.
2. Start a Claude Code session in this repo (cloud or your PC, not the home server) and paste the
   following:

---

Read `CLAUDE.md` and `DESIGN.md` in full before doing anything.

Build **Phase 1 (data collection) only**, as specified in CLAUDE.md "Your task in this session" and
DESIGN.md §3–§6. Do not build anything from Phases 2–5.

## ⚠️ YouTube safety comes first

This code will run against YouTube from my home network, which my household shares. A bot-detection
block would affect everyone at home for hours to days. Treat CLAUDE.md "YouTube request safety" and
DESIGN.md §6.6 as hard requirements:

- Very slow by default: 60–120 s between videos, 5–10 s between requests, 10 s before subtitles,
  strictly one at a time. Enforce the floors in code, with no CLI or env override.
- Daily cap of 30 videos per 24 h, counted from the manifest.
- Stop on the first block signal, write the `data/COOLDOWN` latch, and refuse to run until I clear it.
- No cookies, proxies, VPNs, user-agent rotation, or any bot-detection workaround. Ever.
- **You make at most ONE real single-video fetch in this session**, only to check fixture shapes.
  Never run `discover` or a multi-video `fetch` for real. If YouTube blocks you, don't retry; tell me.
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
8. `deploy/` systemd service + timer (DESIGN §6.7), with no personal hostnames, usernames, or IPs.
9. README: setup, a prominent "Before you run fetch" safety section, the 20-video trial, home-server
   timer install (user unit with linger), and monitoring commands.

Tests must run offline using small fixtures. If you can reach YouTube, your one allowed real fetch is
a single video's info.json and json3 captions, trimmed to a few cues for fixtures. If you're blocked
(likely in a cloud environment), build from the documented formats and tell me.

When done, give me:
- the exact commands to run the 20-video trial on my home server (expect ~30–45 min),
- what to check in the output to judge whether the data is good enough for Phase 2,
- the commands to install and verify the daily timer once the trial looks good.

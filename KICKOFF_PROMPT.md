# Kickoff prompt for the new Claude Code session

1. Create an empty repo (e.g. `coffee-kb`) and copy in `CLAUDE.md` and `docs/DESIGN.md`.
2. Start a Claude Code session in that repo and paste the following:

---

Read `CLAUDE.md` and `docs/DESIGN.md` in full before doing anything.

Build **Phase 1 (data collection) only**, as specified in CLAUDE.md "Your task in this session" and
DESIGN.md §3–§6. Do not build anything from Phases 2–5.

Work in this order and commit after each step:
1. Scaffold: `pyproject.toml` (uv), `src/coffee_kb/` package, `config.toml`, `.gitignore` (ignore
   `data/` except `data/README.md`), `tests/`, ruff config.
2. `paths.py` + `manifest.py` (schema from DESIGN §5.2) with tests.
3. `discover` command.
4. `fetch` command: metadata, manual + auto captions (json3, vtt fallback), SponsorBlock, optional
   `--audio`; atomic snapshot directories; `fetch.json` provenance.
5. `normalize` command, including the auto-caption de-duplication, chapter join, and sponsor join,
   with fixture-based tests.
6. `report` command.
7. README with setup and usage.

Tests must run offline using small fixtures. Before writing fixtures, if you can reach YouTube, fetch
ONE video's info.json and json3 captions to base fixtures on real shapes, trimmed to a few cues.
If YouTube blocks you (likely in a cloud environment), do not work around it — build from the
documented formats and tell me, and I'll run the real fetch locally.

When done, tell me the exact commands to run on my machine for a 20-video trial, and what to look at
in the output to judge whether the data is good enough for Phase 2.

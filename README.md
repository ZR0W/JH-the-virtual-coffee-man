# coffee-kb

A personal knowledge base built from James Hoffmann's public YouTube videos
(<https://www.youtube.com/@jameshoffmann>). It collects the videos' metadata, captions and
SponsorBlock segments once, with full provenance (video + timestamp), so later projects
(knowledge graph, cited Q&A, flashcards, visualizations) can be built without re-collecting.

This is a knowledge base *of his videos*. It cites them; it does not impersonate him.
Personal use only. See [docs/DESIGN.md](docs/DESIGN.md) for the full design.

**Status:** Phase 1 (data collection). Nothing from later phases is built yet.

---

## ⚠️ Before you run `fetch` (or `discover`)

YouTube blocks clients it thinks are bots, and a block hits your **home IP**, so everyone in
the household is affected (failed loads, CAPTCHAs on YouTube and Google Search for hours to
days). This project is deliberately slow and stops by itself at the first sign of trouble.

1. **Upgrade yt-dlp first, every time.** Many apparent "blocks" are just an outdated extractor:
   ```
   uv lock --upgrade-package yt-dlp
   uv sync
   ```
2. **Run it from home only.** Never from a work, office or university network: a flag there
   affects other people.
3. **Only one machine fetches at a time.** Never run the PC and the server in parallel.
4. **Expect it to be slow. That's on purpose:** 60–120 s between videos, 5–10 s between
   requests, 10 s before each caption download, one video at a time, **at most 30 videos per
   rolling 24 h**. A 20-video run takes roughly 30–45 minutes; the whole channel takes about two
   weeks. These limits are enforced in code. Lower values in `config.toml` are rejected and
   there is no command-line override.
5. **If it stops with a block signal, wait.** It writes `data/COOLDOWN` and exits with code 75.
   Until you clear the latch, `discover` and `fetch` refuse to run without making any request.
   See [If a cooldown latch trips](#if-a-cooldown-latch-trips).
6. **No workarounds, ever.** No browser cookies, no accounts, no proxies or VPNs, no
   user-agent tricks, no PO-token plugins. If YouTube is unhappy, the answer is to wait.
7. **Audio is off.** `--audio` downloads audio, the heaviest and most scrutinized traffic, and
   is against YouTube's ToS even for personal use. Don't turn it on unless you've decided to.

Only `discover` and `fetch` talk to YouTube. `normalize`, `report`, `status` and
`clear-cooldown` are offline. SponsorBlock (`sponsor.ajay.app`) is a separate community
service and is queried once per fetched video.

---

## Setup

You need `git` and [`uv`](https://docs.astral.sh/uv/). `uv` installs a suitable Python (≥ 3.11)
by itself if you don't have one.

### Windows (PowerShell)

```powershell
# Install uv and git (skip what you already have), then open a NEW PowerShell window.
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
winget install --id Git.Git -e

# Get the code
git clone https://github.com/ZR0W/JH-the-virtual-coffee-man.git coffee-kb
cd coffee-kb
uv sync

# Upgrade yt-dlp (do this before every run)
uv lock --upgrade-package yt-dlp
uv sync

# Check: offline, no network
uv run coffee-kb status
```

### Linux (bash)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh     # then open a new shell
git clone https://github.com/ZR0W/JH-the-virtual-coffee-man.git coffee-kb
cd coffee-kb
uv sync
uv lock --upgrade-package yt-dlp && uv sync
uv run coffee-kb status
```

Run every command from the repository root (that's where `config.toml` lives; `data/` is
created next to it). Use `--config PATH` to point elsewhere.

---

## Commands

```
coffee-kb discover [--force]          # list the channel into the manifest     [talks to YouTube]
coffee-kb fetch [--limit N] [--video ID_OR_URL ...] [--since YYYY-MM-DD]
                [--type video|short|stream] [--audio] [--refresh] [--dry-run]  [talks to YouTube]
coffee-kb normalize [--video ID ...]  # rebuild normalized/ from raw/           [offline]
coffee-kb report [--format text|json] # coverage, errors, safety state          [offline]
coffee-kb status [--format text|json] # cooldown, daily cap, next eligible time [offline]
coffee-kb clear-cooldown              # you only, ≥ 48 h after a block          [offline]
```

Prefix each with `uv run` (e.g. `uv run coffee-kb status`). Add `-v` for debug logging.

- **discover** lists the `/videos`, `/shorts` and `/streams` tabs (a few dozen paced requests).
  It refuses to run more than once every 7 days unless `--force` (the cooldown still applies).
  Videos that disappear from the channel are marked `missing_from_listing`, never deleted.
- **fetch** fetches, per video: full `info.json` metadata, one English human caption track and
  one English auto-caption track (json3, falling back to WebVTT), and SponsorBlock segments.
  Newest first. Videos whose latest snapshot is complete are skipped with no network access,
  so re-running is always safe. `--limit` is clamped to what's left of the daily cap.
  `--dry-run` shows what would be fetched and how long it would take, without any request.
  Members-only, private and removed videos are recorded and skipped.
- **normalize** is a pure function of `data/raw/`: delete `data/normalized/` any time and
  re-run it.
- **report** prints what's been collected and what went wrong. Nothing fails silently: missing
  captions, unavailable videos and errors all show up here.

Exit codes: `0` ok, `1` some videos/tabs failed (see `report`), `2` config/usage error,
`75` blocked or cooling down.

### Configuration

`config.toml` holds the channel handle, caption languages, pacing and SponsorBlock categories.
Pacing values have hard floors (30 s between videos, 3 s between requests, 5 s before
captions, at most 50 videos per 24 h, 48 h minimum cooldown). Unknown keys are an error.

---

## Data layout

Everything lives in `data/` (git-ignored except [data/README.md](data/README.md)):

```
data/manifest.sqlite                                   # index: items, component status, runs, attempts
data/COOLDOWN                                          # only after a block signal
data/raw/youtube/<video_id>/snapshots/<UTC stamp>/     # immutable, one per fetch
    info.json  captions/<lang>.manual.json3  captions/<lang>.auto.json3
    sponsorblock.json  fetch.json  [audio.<ext>]
data/raw/youtube/_channel/snapshots/<UTC stamp>/       # discover listings
data/normalized/youtube/items.jsonl
data/normalized/youtube/transcripts/<video_id>.<manual|auto>.jsonl
data/normalized/youtube/sponsor_segments.jsonl
```

`raw/` is write-once: a refresh (`fetch --refresh`) creates a new snapshot and keeps the old
one. Each snapshot's `fetch.json` records tool versions, arguments, per-component results and
any yt-dlp warnings. Every normalized record carries `source_type`, `item_id`, times in ms, the
`snapshot` it came from and the `normalizer_version`. Transcript cues are identified by
`(item_id, track, start_ms)`.

The `data/` directory is path-independent: you can copy it to another machine and continue.

---

## Checkpoint B: smoke test (1 video, your PC)

1. Setup as above, including the yt-dlp upgrade.
2. Pick **one recent video**: open <https://www.youtube.com/@jameshoffmann/videos> in your
   browser and copy the URL of the newest video.
3. Run (PowerShell, from the repo root; keep the quotes around the URL):
   ```powershell
   uv run coffee-kb fetch --video "PASTE-THE-URL-HERE"
   uv run coffee-kb normalize
   uv run coffee-kb report
   uv run coffee-kb status
   ```
   `fetch` takes about a minute (it waits 5–10 s between requests and 10 s before each caption
   download). Don't run `discover` for the smoke test.

The exact list of things to paste back is given in the chat for each checkpoint.

## Checkpoint C: 20-video trial (your PC)

```powershell
uv lock --upgrade-package yt-dlp
uv sync
uv run coffee-kb discover
uv run coffee-kb fetch --dry-run --limit 20
uv run coffee-kb fetch --limit 20      # ~30–45 minutes, by design
uv run coffee-kb normalize
uv run coffee-kb report
```

To inspect the results offline: `uv run python scripts/trial_summary.py` (caption coverage,
auto-caption duplicate check, chapters, sponsor joins, transcript samples).

Re-fetching a complete video does **no** network work: `uv run coffee-kb fetch --video ID` just
logs that it's already complete. Follow-up checks use `--video` on specific videos only; never a second full trial on
the same day.

---

## If a cooldown latch trips

`fetch` or `discover` saw a block signal ("Sign in to confirm you're not a bot", HTTP 429 or
403 from YouTube, a CAPTCHA/consent page, or unknown errors on 3 videos in a row). It stopped
immediately and wrote `data/COOLDOWN`.

1. **Don't retry, and don't work around it.** Check `uv run coffee-kb status`.
2. Wait **at least 48 hours**. Check that YouTube works normally in a browser at home.
3. Upgrade yt-dlp (`uv lock --upgrade-package yt-dlp`, then `uv sync`).
4. Then clear it yourself: `uv run coffee-kb clear-cooldown`. It refuses within 48 h of the
   block and asks you to type `clear`. The old latch is kept in `data/cooldown_history/`.
5. Restart gently: one `fetch --video ...`, then the normal daily batch.

---

## Home server (Ubuntu, daily timer)

The collection runs on the home server as a **systemd user unit** once a day, and you watch it
from your PC with a dashboard. Only one machine fetches at a time: once the server has the data,
the PC stops fetching.

### One-time setup

**1. Server: install and clone** (SSH in from the PC):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh      # uv in ~/.local/bin; open a new shell
git clone https://github.com/ZR0W/JH-the-virtual-coffee-man.git ~/coffee-kb
cd ~/coffee-kb
uv sync
uv run pytest -q                                     # offline sanity check
```

**2. PC: hand the data over and retire the PC as a fetcher** (PowerShell, in the PC's checkout).
Copying `data/` carries the manifest and the fetch history, so the server's daily cap counts
the PC's recent fetches and it continues exactly where the PC stopped.

```powershell
tar -czf $env:TEMP\coffee-kb-data.tgz data
scp $env:TEMP\coffee-kb-data.tgz <server>:
# Latch the PC so a stray `fetch` here can never run again from this copy:
$now = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
Set-Content -Encoding ascii data\COOLDOWN "{`"reason`": `"fetching moved to the home server; do not fetch from this PC`", `"detected_at`": `"$now`"}"
```

**3. Server: unpack and check**:

```bash
cd ~/coffee-kb
tar -xzf ~/coffee-kb-data.tgz && rm ~/coffee-kb-data.tgz
rm -f data/.lock data/*.txt data/trial-fetch.pid      # PC-only leftovers
uv run coffee-kb status                               # cooldown: none; cap counts the PC's runs
uv run coffee-kb report
```

**4. Server: install the timer** (user unit + linger):

```bash
cd ~/coffee-kb
mkdir -p ~/.config/systemd/user
sed -e "s|@REPO_DIR@|$HOME/coffee-kb|g" -e "s|@UV@|$HOME/.local/bin/uv|g" \
    deploy/coffee-kb-fetch.service > ~/.config/systemd/user/coffee-kb-fetch.service
cp deploy/coffee-kb-fetch.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now coffee-kb-fetch.timer
sudo loginctl enable-linger "$USER"                   # the only sudo: run without a login session
systemctl --user list-timers coffee-kb-fetch.timer    # shows the next run (03:00–05:00)
```

The first scheduled run is the first server fetch; there's no need to start one by hand.

**5. PC: the dashboard.** From the PC's checkout:

```powershell
.\tools\coffee-kb-dashboard.ps1 -Server <server>
```

It regenerates the page on the server over SSH, copies it to `%TEMP%` and opens it in your
browser. Nothing is served or opened on the network. Tip: a function in your PowerShell
`$PROFILE` makes it one word, e.g. `function ckb { & C:\path\to\checkout\tools\coffee-kb-dashboard.ps1 -Server <server> }`.

### What a daily run does

Between 03:00 and 05:00 (randomized): upgrade yt-dlp → `discover` (only if a week has passed) →
`fetch --wait-for-cap` (up to 30 videos, 60–120 s apart; if yesterday's run is still inside
the rolling 24 h window it waits for slots, up to 3 h) → `normalize` → `dashboard`. Roughly an
hour of mostly sleeping. The backlog finishes in about two weeks; after that a run picks up the
day's new uploads in a few minutes.

### Day to day

Nothing is required. Glance at the dashboard when you like (every few days is plenty):

| Dashboard banner | Meaning | What to do |
|---|---|---|
| ✔ Healthy | Collecting on schedule | Nothing |
| ⚠ Healthy, with item errors | Some videos have a missing component | Nothing; they're retried up to 3 times automatically. Persistent ones are listed; retry with `uv run coffee-kb fetch --video ID` |
| ⚠ No recent fetch | No successful fetch for 36 h | `systemctl --user list-timers`; `journalctl --user -u coffee-kb-fetch -n 100` |
| ⚠ Last fetch had errors | Exit code 1 | Read the errors and the log section; tell your agent if it's unclear |
| ✖ Cooldown latch active | YouTube showed a block signal; the job stopped itself | Do nothing for 48 h. Then follow [If a cooldown latch trips](#if-a-cooldown-latch-trips). Every daily run exits immediately without network until you clear it |

Useful commands on the server (`cd ~/coffee-kb` first):

```bash
uv run coffee-kb status                              # cooldown, cap, next eligible time
uv run coffee-kb report                              # coverage and errors
systemctl --user list-timers coffee-kb-fetch.timer   # next run
systemctl --user status coffee-kb-fetch              # last run result
journalctl --user -u coffee-kb-fetch -f              # follow a run live
```

**Updating the code:** the timer upgrades yt-dlp, which changes `uv.lock` in the checkout, so
discard that before pulling: `git checkout uv.lock && git pull && uv sync`.

**Pausing / removing:** `systemctl --user disable --now coffee-kb-fetch.timer` pauses it. To
remove everything: also delete the two files in `~/.config/systemd/user/`, run
`sudo loginctl disable-linger $USER`, and `rm -rf ~/coffee-kb`. Nothing else is installed
outside your home directory (uv lives in `~/.local/bin`, its caches in `~/.cache/uv` and
`~/.local/share/uv`).

**Disk and network:** about 1.3 MB per video, roughly 0.5 GB for the whole channel. Outbound
HTTPS only; no ports to open.

---

## Development

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

Tests are offline: fixtures under `tests/fixtures/` are small hand-trimmed samples, YouTube
and SponsorBlock are faked, and time is mocked (no test sleeps for real or lowers pacing).

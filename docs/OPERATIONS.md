# coffee-kb — Operations runbook

For anyone (human or agent) who has to look after, debug, update, move or remove the running
system. Read [CLAUDE.md](../CLAUDE.md) first for the safety rules; they apply here in full.

Placeholders: `<server>` = the home server's SSH host, `<user>` = the account on it, `<repo>` =
the checkout on the server (deployed at `~/coffee-kb`). Personal hostnames, usernames and IPs
are deliberately **not** in this repo; ask the user if you need them.

---

## 1. What is deployed (state as of 2026-10-14)

- **Where:** the user's home server (Ubuntu 26.04 LTS laptop-as-server, Wi-Fi only, `ufw`
  firewall), user account `<user>`, checkout at `~/coffee-kb`.
- **Branch:** `claude/exciting-ramanujan-wa0f9u` until it's merged; then `main`.
- **History:** built in the cloud, smoke-tested (1 video) and trialled (20 videos) on the user's
  Windows PC, then `data/` was copied to the server. The PC is **retired as a fetcher**: its
  `data\COOLDOWN` says "fetching moved to the home server". Never clear that latch on the PC.
  Only one machine may fetch at a time (they share the household's public IP).
- **Two components**, both systemd **user** units with linger enabled:

| | collector | web |
|---|---|---|
| Units | `coffee-kb-collector.timer` → `coffee-kb-collector.service` (oneshot) | `coffee-kb-web.service` (simple, always on) |
| Schedule | daily `03:00` + random 0–2 h, `Persistent=true` (server local time, America/New_York) | always; `Restart=on-failure` |
| Runs | `uv lock --upgrade-package yt-dlp` and `uv sync` (failure tolerated), then `uv run coffee-kb collector run` = discover (weekly) → `fetch --wait-for-cap` → normalize | `uv run --no-sync coffee-kb web serve` |
| Talks to YouTube | yes: paced, capped at 30 videos / rolling 24 h, stops on block signals | never; GET-only, cannot start a fetch |
| Writes | `data/raw`, `data/normalized`, `data/manifest.sqlite`, `data/logs/coffee-kb.log`, `uv.lock` (nightly yt-dlp upgrade) | nothing (manifest opened read-only) |
| Network | outbound HTTPS only | listens on `0.0.0.0:8787` (IPv4), firewall-limited to the home subnet |

- **Progress at deployment:** 411 items listed, 21 fetched (20 complete). About 14 nights to
  finish the backlog at 30/night; after that each run only picks up new uploads.
- **Decisions** (don't change without asking the user): user unit + linger; run window 03:00 +
  0–2 h; yt-dlp upgraded before each run; no JS runtime (deno) installed; monitoring through the
  web dashboard; LAN access on port 8787 with a subnet-limited ufw rule. See DESIGN §10.

## 2. Complete inventory on the server

Everything this project put on the server. Anything not listed here was not created by it.

**In the user's home directory**

| Path | What | Created by |
|---|---|---|
| `~/coffee-kb/` | git checkout | `git clone` |
| `~/coffee-kb/.venv/` | Python env (yt-dlp, httpx, coffee-kb; system Python 3.14) | `uv sync` |
| `~/coffee-kb/data/` | **all collected data + state**; see `data/README.md` | the app (+ copied from the PC) |
| `~/coffee-kb/data/logs/coffee-kb.log` (+ `.1`…`.5`) | application log, rotates at 5 MB | collector / web |
| `~/coffee-kb/uv.lock` | modified nightly by the yt-dlp upgrade (expected git diff) | collector unit |
| `~/.config/systemd/user/coffee-kb-collector.service` | generated unit | `coffee-kb collector install` |
| `~/.config/systemd/user/coffee-kb-collector.timer` | generated unit | `coffee-kb collector install` |
| `~/.config/systemd/user/coffee-kb-web.service` | generated unit | `coffee-kb web install` |
| `~/.config/systemd/user/timers.target.wants/coffee-kb-collector.timer` | enable symlink | `systemctl --user enable` |
| `~/.config/systemd/user/default.target.wants/coffee-kb-web.service` | enable symlink | `systemctl --user enable` |
| `~/.local/share/systemd/timers/stamp-coffee-kb-collector.timer` | last-trigger stamp (`Persistent=true`) | systemd |
| `~/.local/bin/uv`, `~/.local/bin/uvx`, `~/.local/bin/env`, `~/.local/bin/env.fish` | uv | uv installer |
| `~/.local/bin/coffee-kb` → `~/coffee-kb/.venv/bin/coffee-kb` (symlink, optional) | run `coffee-kb …` from any directory | `ln -sf` (README step 3b) |
| one line `. "$HOME/.local/bin/env"` in `~/.profile` and/or `~/.bashrc` | PATH for uv | uv installer |
| `~/.cache/uv/` | uv package cache | uv |
| `~/.cache/yt-dlp/` | yt-dlp's cache | yt-dlp |

**Outside the home directory (needed sudo)**

| What | Where | Created by | Check |
|---|---|---|---|
| linger flag (empty file) | `/var/lib/systemd/linger/<user>` | `sudo loginctl enable-linger <user>` | `loginctl show-user <user> -p Linger` |
| firewall rule `8787/tcp ALLOW IN <home-subnet>` (comment `coffee-kb web (LAN only)`) | `/etc/ufw/user.rules` | `sudo ufw allow from <home-subnet> to any port 8787 proto tcp comment 'coffee-kb web (LAN only)'` | `sudo ufw status numbered` |
| log lines | system journal (`journalctl --user -u coffee-kb-*`) | systemd | — |

No apt packages were installed (git was already present), nothing in `/usr` or `/opt`, no root
services, no router changes. **Linger and the ufw rule may also be relied on by other things the
user runs**; before removing either, check (§7).

## 3. Everyday commands (on the server, in `~/coffee-kb`)

`uv run coffee-kb …` must be run inside `~/coffee-kb` (elsewhere: "Failed to spawn: coffee-kb").
With the optional symlink (README step 3b), plain `coffee-kb …` works from any directory; the CLI
finds its config via `--config`, `$COFFEE_KB_CONFIG`, `./config.toml`, then the checkout it runs
from.

```bash
uv run coffee-kb collector status        # timer, next/last run, cooldown, 24 h cap
uv run coffee-kb collector logs -n 200   # app log; -f to follow
uv run coffee-kb web status              # web service state + URL
uv run coffee-kb report                  # coverage, errors, stale normalization
uv run coffee-kb status                  # safety state only
systemctl --user list-timers --no-pager
systemctl --user status coffee-kb-collector coffee-kb-web --no-pager
journalctl --user -u coffee-kb-collector -n 100 --no-pager   # incl. the yt-dlp upgrade step
journalctl --user -u coffee-kb-web -n 50 --no-pager
```

Dashboard: `http://<server-LAN-IP>:8787/` (also `/api/status` JSON, `/logs?n=500`, `/healthz`).

## 4. Health check / diagnostic bundle (read-only, safe to run any time)

These make **no** YouTube requests and change nothing. Run them first and read the output before
proposing any fix.

```bash
cd ~/coffee-kb
git status --short && git log --oneline -1
uv run coffee-kb collector status
uv run coffee-kb web status
uv run coffee-kb report
uv run coffee-kb collector logs -n 150
systemctl --user list-timers --no-pager
systemctl --user status coffee-kb-collector.service coffee-kb-web.service --no-pager
journalctl --user -u coffee-kb-collector -n 80 --no-pager
loginctl show-user "$USER" -p Linger
sudo ufw status numbered                  # needs the user's password; skip if not available
ss -ltnp | grep 8787
df -h ~ && du -sh ~/coffee-kb/data
ls -la ~/coffee-kb/data                   # COOLDOWN? .lock?
cat ~/coffee-kb/data/COOLDOWN 2>/dev/null
```

Healthy looks like: timer `active` with a next run; last run `success (exit 0)` (or "never"
before the first run); no `COOLDOWN`; `fetch attempts in the last 24 h` ≤ 30; web `active
(running)`; `ss` shows `0.0.0.0:8787`; Linger=yes.

## 5. Troubleshooting by symptom

Always: diagnose with §4 first, keep fixes minimal, never touch the safety rules (CLAUDE.md).

| Symptom | Likely cause | Fix |
|---|---|---|
| **`data/COOLDOWN` exists / exit 75 / dashboard "Cooldown latch active"** | YouTube block signal (or an extractor bug that looked like one) | **Do not clear it, do not run fetch/discover, do not "test".** Report the latch reason and the log lines around it to the user. After ≥ 48 h the *user* runs `uv run coffee-kb clear-cooldown` (README "If a cooldown latch trips"). If the log shows an extractor error rather than a bot check, an upgraded yt-dlp (`uv lock --upgrade-package yt-dlp && uv sync`) is the likely fix, but it still waits for the user to clear the latch. |
| Run finished but only N < 30 videos, log says "daily cap reached during the run" / "beyond the wait budget" | Rolling 24 h window still full from an earlier run (e.g. the PC's trial) | Expected. Nothing to do. |
| "No recent fetch" on the dashboard; timer has no next run | timer disabled, or units missing | `systemctl --user list-timers --all`; re-run `uv run coffee-kb collector install` (idempotent). |
| Timer never fires after reboot / logout | linger off | `loginctl show-user "$USER" -p Linger` → if `no`, the user runs `sudo loginctl enable-linger "$USER"`. |
| collector service failed with `uv: not found` / `No such file` | uv moved or was removed | reinstall uv (`curl -LsSf https://astral.sh/uv/install.sh \| sh`), then `uv run coffee-kb collector install` and `web install` (they record uv's absolute path). |
| Exit 1, `report` shows component errors on a few videos | per-video problem (captions not ready, SponsorBlock outage) | Usually nothing: incomplete videos are retried automatically up to 3 failed attempts, then listed under "No longer retried automatically". To retry one deliberately: `uv run coffee-kb fetch --video ID` (counts toward the cap). |
| Captions recorded as `error` mentioning "PO token" | YouTube withholding captions from yt-dlp's default clients | Don't add PO-token providers or cookies (forbidden). Upgrade yt-dlp; if it persists across many videos, stop and discuss with the user. |
| Many videos fail with unknown errors (3 in a row trips the latch) | outdated yt-dlp / YouTube change | Upgrade yt-dlp; see the cooldown row. |
| yt-dlp warning "No supported JavaScript runtime" in every fetch.json | known and accepted (decision: no deno) | Nothing, unless captions start failing; then discuss installing deno with the user. |
| `uv run coffee-kb …` → "Failed to spawn: coffee-kb" | not inside `~/coffee-kb` (uv finds the project by directory) | `cd ~/coffee-kb` first, or use the `~/.local/bin/coffee-kb` symlink (README step 3b). |
| `uv: command not found` in a new shell | `~/.local/bin` not on PATH | Log in again (Ubuntu's `~/.profile` adds it) or `source ~/.local/bin/env`. |
| `fetch`: "another coffee-kb run holds data/.lock" | a run is in progress, or a crash left the lock | If `systemctl --user status coffee-kb-collector` shows it running, wait. A lock older than 8 h is taken over automatically. Never delete it while a run is active. |
| `git pull` refuses: local changes to `uv.lock` | nightly yt-dlp upgrade | `git checkout uv.lock && git pull && uv sync` (then §6 step 3). |
| Web page unreachable from the PC | service down, port blocked, or the server's IP changed (DHCP) | `uv run coffee-kb web status`; `ss -ltnp \| grep 8787`; `sudo ufw status`; `ip -4 addr` for the current IP. Restart: `systemctl --user restart coffee-kb-web`. |
| Web shows "internal error" | exception while rendering | `uv run coffee-kb collector logs -n 100` (web errors are logged there) and `journalctl --user -u coffee-kb-web`. |
| `database is locked` | the web app reading while the collector writes, briefly | Transient (10 s busy timeout); if persistent, check for a stuck process with `ps aux \| grep coffee-kb`. |
| Disk full | data or logs grew | `du -sh ~/coffee-kb/data/*`; expected total ≈ 0.5 GB for the whole channel (no audio). Logs are capped at ~30 MB. |
| Normalization stale in `report` | normalize failed or was skipped | `uv run coffee-kb normalize` (offline, safe, idempotent). |
| Config error / exit 2 | `config.toml` edited with a value below a floor or an unknown key | Revert the edit (`git diff config.toml`). Never lower floors. |

**Never, as a "fix":** lower pacing or the cap, add flags around the floors, clear the cooldown,
delete `data/COOLDOWN`, edit `data/raw/`, add cookies/proxies/user agents/PO-token plugins,
enable audio, run a manual multi-video fetch "to test", or run discover with `--force` "to see".

## 6. Updating the code

```bash
cd ~/coffee-kb
git checkout uv.lock                 # discard the nightly yt-dlp bump
git pull
uv sync
uv run pytest -q                     # offline; must pass
uv run coffee-kb collector install   # regenerates units if service.py changed (idempotent)
uv run coffee-kb web install         # regenerates + restarts the web app
```

Don't update while a collector run is active (`systemctl --user status coffee-kb-collector`).

## 7. Clean uninstall

Do this only when the user asks. Decide first, **with the user**: keep the collected data or
delete it? (It took ~2 weeks of throttled fetching to build; a backup is one command.)

**Step 0: optional backup of the data** (path-independent; restores by unpacking into a checkout):

```bash
cd ~/coffee-kb && tar -czf ~/coffee-kb-data-$(date +%Y%m%d).tgz data
```

**Step 1: stop and remove both components** (removes the unit files and enable symlinks):

```bash
cd ~/coffee-kb
uv run coffee-kb collector uninstall
uv run coffee-kb web uninstall
rm -f ~/.local/share/systemd/timers/stamp-coffee-kb-collector.timer
systemctl --user daemon-reload
systemctl --user reset-failed 2>/dev/null || true
```

If the checkout or uv is already gone, do the same by hand:

```bash
systemctl --user disable --now coffee-kb-collector.timer coffee-kb-collector.service coffee-kb-web.service
rm -f ~/.config/systemd/user/coffee-kb-collector.service \
      ~/.config/systemd/user/coffee-kb-collector.timer \
      ~/.config/systemd/user/coffee-kb-web.service \
      ~/.local/share/systemd/timers/stamp-coffee-kb-collector.timer
systemctl --user daemon-reload
```

**Step 2: remove the firewall rule** (needs sudo):

```bash
sudo ufw status numbered             # find the line with 8787/tcp ... coffee-kb web (LAN only)
sudo ufw delete <number>             # confirm with y; re-list, numbers shift after each delete
```

**Step 3: linger.** It was enabled for coffee-kb, but other user services may rely on it now.
Check before disabling:

```bash
systemctl --user list-unit-files --state=enabled --no-pager   # anything besides default units?
```

If nothing else of the user's needs it: `sudo loginctl disable-linger "$USER"`.

**Step 4: remove the checkout, caches and the optional command symlink** (this deletes the
collected data unless backed up):

```bash
rm -rf ~/coffee-kb ~/.cache/yt-dlp
rm -f ~/.local/bin/coffee-kb
```

**Step 5 (optional): remove uv**, if nothing else uses it:

```bash
uv cache clean
rm -f ~/.local/bin/uv ~/.local/bin/uvx ~/.local/bin/env ~/.local/bin/env.fish
rm -rf ~/.local/share/uv ~/.cache/uv
grep -n 'local/bin/env' ~/.profile ~/.bashrc ~/.bash_profile ~/.zshrc 2>/dev/null   # delete those lines
```

**Step 6: verify nothing is left:**

```bash
systemctl --user list-unit-files --no-pager | grep -i coffee-kb    # expect nothing
ls ~/.config/systemd/user/ 2>/dev/null | grep -i coffee             # expect nothing
ss -ltnp | grep 8787                                                # expect nothing
sudo ufw status | grep 8787                                         # expect nothing
loginctl show-user "$USER" -p Linger                                # as decided in step 3
ls -d ~/coffee-kb ~/.cache/yt-dlp ~/.local/bin/coffee-kb 2>/dev/null   # expect nothing
```

**On the PC** (Windows), if the whole project is being removed: delete the checkout folder
(`C:\...\JH-the-virtual-coffee-man`, which includes the PC's old `data\` and its retirement
latch). uv on Windows lives in `%USERPROFILE%\.local\bin` (remove with `uv cache clean` then
deleting `uv.exe`/`uvx.exe` and `%LOCALAPPDATA%\uv`) only if the user wants it gone.

## 8. Backup, restore, move

- `data/` is the only state that matters, and it's path-independent. Back up with
  `tar -czf coffee-kb-data.tgz data` from the checkout.
- Restore / move: clone the repo, `uv sync`, unpack `data/` into the checkout, check with
  `uv run coffee-kb status` and `report`, then `collector install` / `web install` there.
- **Only one machine may fetch.** Before activating a new machine, uninstall the collector on the
  old one (or latch it like the PC: write a `data/COOLDOWN` there with reason "fetching moved").
  Copying `data/` carries the fetch history, so the 24 h cap stays correct across the move.

"""The status dashboard page served by ``coffee-kb web`` (read-only, offline).

Built from the manifest, the report, the collector's systemd timer state (Linux) and the tail
of ``data/logs/coffee-kb.log``. Self-contained HTML: no external requests.
"""

from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

from coffee_kb.clock import iso, parse_iso
from coffee_kb.config import Config
from coffee_kb.manifest import Manifest
from coffee_kb.paths import DataPaths
from coffee_kb.report import build_report

SOURCE_TYPE = "youtube"
DAYS = 21


def system_info(paths: DataPaths, log_lines: int = 80) -> dict[str, Any]:
    from coffee_kb.logs import tail_lines
    from coffee_kb.service import collector_unit_state

    return {"collector": collector_unit_state(), "log": tail_lines(paths.log_file, log_lines)}


def build_dashboard(
    cfg: Config,
    paths: DataPaths,
    manifest: Manifest,
    now: datetime,
    system: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rep = build_report(cfg, paths, manifest, now)
    attempts = manifest.attempts(SOURCE_TYPE)

    # Per-day attempts (UTC days) for the last DAYS days.
    by_day: dict[str, Counter] = defaultdict(Counter)
    for a in attempts:
        by_day[a["started_at"][:10]][a["outcome"] or "started"] += 1
    days = []
    for i in range(DAYS - 1, -1, -1):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        c = by_day.get(d, Counter())
        days.append({"date": d, "total": sum(c.values()), "outcomes": dict(c)})

    # A run in progress: the lock is held and the latest fetch run hasn't finished.
    running = None
    if paths.lock.exists():
        try:
            lock = json.loads(paths.lock.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            lock = {}
        last_fetch = manifest.last_run("fetch")
        done = 0
        if last_fetch is not None and last_fetch["finished_at"] is None:
            done = sum(1 for a in attempts if a["run_id"] == last_fetch["run_id"])
        running = {
            "command": lock.get("command"),
            "since": lock.get("started_at"),
            "videos_done": done,
        }

    runs = []
    for r in manifest.recent_runs(12):
        summary = json.loads(r["summary_json"]) if r["summary_json"] else {}
        dur = None
        if r["finished_at"]:
            dur = int((parse_iso(r["finished_at"]) - parse_iso(r["started_at"])).total_seconds())
        runs.append(
            {
                "command": r["command"],
                "started_at": r["started_at"],
                "duration_s": dur,
                "exit_code": r["exit_code"],
                "counts": summary.get("counts"),
                "message": summary.get("message") or summary.get("block_reason"),
            }
        )

    items = rep["items"]
    listed_total = items["total"] - items["by_listing_status"].get("missing_from_listing", 0)
    health = _health(rep, runs, running, now)
    return {
        "generated_at": iso(now),
        "health": health,
        "report": rep,
        "days": days,
        "running": running,
        "runs": runs,
        "progress": {"complete": items["complete"], "total": listed_total},
        "system": system or {},
    }


def _health(
    rep: dict[str, Any], runs: list[dict], running: dict | None, now: datetime
) -> dict[str, str]:
    st = rep["safety"]
    if st["cooldown"]:
        return {
            "level": "critical",
            "label": "Cooldown latch active",
            "detail": f"Blocked since {st['cooldown']['detected_at']}. Do nothing until "
            f"{st['cooldown']['can_clear_from']}; then see README “If a cooldown latch trips”.",
        }
    fetch_runs = [r for r in runs if r["command"] == "fetch" and r["exit_code"] is not None]
    if fetch_runs and fetch_runs[0]["exit_code"] not in (0,):
        return {
            "level": "serious",
            "label": "Last fetch had errors",
            "detail": f"Exit code {fetch_runs[0]['exit_code']} at {fetch_runs[0]['started_at']}. "
            "See errors and the log below.",
        }
    if running:
        return {
            "level": "good",
            "label": "Running now",
            "detail": f"{running['command']} since {running['since']}",
        }
    last_ok = next((r for r in fetch_runs if r["exit_code"] == 0), None)
    if st["remaining_to_fetch"] and (
        last_ok is None or now - parse_iso(last_ok["started_at"]) > timedelta(hours=36)
    ):
        return {
            "level": "warning",
            "label": "No recent fetch",
            "detail": "No successful fetch in the last 36 h while videos remain. Is the timer enabled?",
        }
    if rep["errors"] or rep["retry_exhausted"]:
        return {
            "level": "warning",
            "label": "Healthy, with item errors",
            "detail": "Some videos have component errors; see below.",
        }
    return {"level": "good", "label": "Healthy", "detail": "Collecting on schedule."}


# --- rendering ------------------------------------------------------------------------------

_ICONS = {"good": "✔", "warning": "⚠", "serious": "⚠", "critical": "✖"}

CSS = """
.viz-root{color-scheme:light;--surface:#fcfcfb;--page:#f9f9f7;--ink:#0b0b0b;--ink2:#52514e;
--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);--series-1:#2a78d6;
--good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b}
@media (prefers-color-scheme:dark){.viz-root{color-scheme:dark;--surface:#1a1a19;--page:#0d0d0d;
--ink:#f0efec;--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--series-1:#3987e5}}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);
font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 10px}
.sub{color:var(--ink2);font-size:13px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px}
.banner{display:flex;gap:12px;align-items:flex-start;margin:18px 0}
.banner .icon{font-size:20px;line-height:1}.banner strong{display:block}
.lvl-good .icon{color:var(--good)}.lvl-warning .icon{color:var(--warning)}
.lvl-serious .icon{color:var(--serious)}.lvl-critical .icon{color:var(--critical)}
.lvl-critical{border-left:4px solid var(--critical)}.lvl-serious{border-left:4px solid var(--serious)}
.lvl-warning{border-left:4px solid var(--warning)}.lvl-good{border-left:4px solid var(--good)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.tile .label{color:var(--ink2);font-size:13px}.tile .value{font-size:28px;font-weight:600;
font-variant-numeric:tabular-nums}.tile .note{color:var(--muted);font-size:12px}
.hero .value{font-size:48px}
.bar{height:8px;background:var(--grid);border-radius:4px;overflow:hidden;margin-top:8px}
.bar>span{display:block;height:100%;background:var(--series-1);border-radius:4px}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--grid);vertical-align:top}
th{color:var(--ink2);font-weight:600}
pre{white-space:pre-wrap;word-break:break-word;font-size:12px;color:var(--ink2);margin:0}
svg text{fill:var(--muted);font-size:11px}.gridline{stroke:var(--grid)}.axis{stroke:var(--axis)}
.capline{stroke:var(--ink2);stroke-dasharray:4 4}.b{fill:var(--series-1)}
.b:hover{opacity:.8}details{margin-top:8px}summary{cursor:pointer;color:var(--ink2)}
.scroll{overflow-x:auto}a{color:var(--series-1)}
"""


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _dur(s: int | None) -> str:
    if s is None:
        return "—"
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {sec:02d}s"


def _chart(days: list[dict], cap: int) -> str:
    w, h, left, bottom, top = 720, 200, 32, 22, 12
    plot_w, plot_h = w - left - 8, h - bottom - top
    ymax = max(cap, max((d["total"] for d in days), default=0))
    step = plot_w / len(days)
    bw = max(4.0, step - 2)  # 2px surface gap between bars
    parts = [
        f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Video fetch attempts per day">'
    ]
    for v in (0, ymax // 2, ymax):
        y = top + plot_h - plot_h * v / ymax
        parts.append(f'<line class="gridline" x1="{left}" x2="{w - 8}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{v}</text>')
    ycap = top + plot_h - plot_h * cap / ymax
    parts.append(
        f'<line class="capline" x1="{left}" x2="{w - 8}" y1="{ycap:.1f}" y2="{ycap:.1f}"/>'
    )
    parts.append(f'<text x="{w - 10}" y="{ycap - 4:.1f}" text-anchor="end">daily cap {cap}</text>')
    base = top + plot_h
    for i, d in enumerate(days):
        x = left + i * step + (step - bw) / 2
        if d["total"]:
            bh = max(2.0, plot_h * d["total"] / ymax)
            r = min(4.0, bw / 2, bh)
            y = base - bh
            path = (
                f"M{x:.1f},{base:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
                f"H{x + bw - r:.1f} Q{x + bw:.1f},{y:.1f} {x + bw:.1f},{y + r:.1f} V{base:.1f} Z"
            )
            tip = f"{d['date']}: {d['total']} fetch attempt(s)" + "".join(
                f"\n  {k}: {v}" for k, v in sorted(d["outcomes"].items())
            )
            # Hit target is the full column, larger than the bar.
            parts.append(
                f'<g><title>{_e(tip)}</title><rect x="{left + i * step:.1f}" y="{top}" '
                f'width="{step:.1f}" height="{plot_h}" fill="transparent"/>'
                f'<path class="b" d="{path}"/></g>'
            )
        if i % 3 == 0 or i == len(days) - 1:
            parts.append(
                f'<text x="{x + bw / 2:.1f}" y="{h - 6}" text-anchor="middle">{d["date"][5:]}</text>'
            )
    parts.append(f'<line class="axis" x1="{left}" x2="{w - 8}" y1="{base}" y2="{base}"/></svg>')
    return "".join(parts)


def render_html(data: dict[str, Any]) -> str:
    rep = data["report"]
    st = rep["safety"]
    hl = data["health"]
    prog = data["progress"]
    pct = 100 * prog["complete"] / prog["total"] if prog["total"] else 0
    sysi = data["system"]
    col = sysi.get("collector") or {}
    out: list[str] = []
    a = out.append
    a('<!doctype html><html lang="en"><head><meta charset="utf-8">')
    a('<meta name="viewport" content="width=device-width,initial-scale=1">')
    if data.get("refresh_s"):
        a(f'<meta http-equiv="refresh" content="{int(data["refresh_s"])}">')
    a(
        f'<title>coffee-kb status</title><style>{CSS}</style></head><body class="viz-root"><div class="wrap">'
    )
    a(
        f'<h1>coffee-kb collection</h1><div class="sub">Snapshot generated {_e(data["generated_at"])} '
        f"(UTC){'; refreshes every minute' if data.get('refresh_s') else ''}.</div>"
    )
    a(
        f'<div class="card banner lvl-{_e(hl["level"])}"><span class="icon" aria-hidden="true">'
        f"{_ICONS[hl['level']]}</span><div><strong>{_e(hl['label'])}</strong>"
        f'<span class="sub">{_e(hl["detail"])}</span></div></div>'
    )
    if data["running"]:
        r = data["running"]
        a(
            f'<div class="card sub">Run in progress: <b>{_e(r["command"])}</b> since {_e(r["since"])}; '
            f"{r['videos_done']} video(s) attempted so far.</div>"
        )

    a('<div class="tiles" style="margin-top:12px">')
    a(
        f'<div class="card tile hero"><div class="label">Videos collected</div><div class="value">'
        f'{prog["complete"]} <span class="sub">/ {prog["total"]}</span></div>'
        f'<div class="bar"><span style="width:{pct:.1f}%"></span></div>'
        f'<div class="note">{pct:.0f}% complete</div></div>'
    )
    a(
        f'<div class="card tile"><div class="label">Days remaining (est.)</div><div class="value">'
        f'{st["estimated_days_remaining"]}</div><div class="note">{st["remaining_to_fetch"]} left at '
        f"{st['max_videos_per_24h']}/day</div></div>"
    )
    a(
        f'<div class="card tile"><div class="label">Fetches in last 24 h</div><div class="value">'
        f'{st["fetches_last_24h"]} <span class="sub">/ {st["max_videos_per_24h"]}</span></div>'
        f'<div class="note">next eligible '
        f"{'now' if st['remaining_allowance'] > 0 else _e(st['next_eligible_fetch'])}</div></div>"
    )
    nxt = col.get("next_run") or "— (collector timer not installed)"
    last = col.get("last_result") or "—"
    if col.get("last_exit_status") not in (None, "", "0"):
        last += f" (exit {col['last_exit_status']})"
    a(
        f'<div class="card tile"><div class="label">Next collector run</div><div class="value" '
        f'style="font-size:16px;padding-top:10px">{_e(nxt)}</div><div class="note">timer '
        f"{_e(col.get('timer') or 'unknown')}; last run {_e(col.get('last_trigger') or '—')}: "
        f"{_e(last)}</div></div>"
    )
    a("</div>")

    a("<h2>Fetch attempts per day (last 21 days, UTC)</h2>")
    a(
        f'<div class="card">{_chart(data["days"], st["max_videos_per_24h"])}<details><summary>Table view'
        "</summary><table><tr><th>Date</th><th>Attempts</th><th>Outcomes</th></tr>"
    )
    for d in reversed(data["days"]):
        if d["total"]:
            oc = ", ".join(f"{k} {v}" for k, v in sorted(d["outcomes"].items()))
            a(f"<tr><td>{d['date']}</td><td>{d['total']}</td><td>{_e(oc)}</td></tr>")
    a("</table></details></div>")

    c = rep["captions"]
    a('<h2>Coverage</h2><div class="card"><table>')
    a(
        f"<tr><th>Items in manifest</th><td>{rep['items']['total']} "
        f"({_e(', '.join(f'{k} {v}' for k, v in rep['items']['by_type'].items()))})</td></tr>"
    )
    a(
        f"<tr><th>Captions</th><td>manual+auto {c['manual+auto']} · auto-only {c['auto_only']} "
        f"· manual-only {c['manual_only']} · none {c['none']}</td></tr>"
    )
    s = rep["sponsorblock"]
    a(
        f"<tr><th>SponsorBlock</th><td>{s['with_segments']} with segments · "
        f"{s['without_segments']} without</td></tr>"
    )
    t = rep["totals"]
    a(
        f"<tr><th>Collected</th><td>{t['hours_fetched']} h of video · "
        f"{t['transcript_words']:,} transcript words</td></tr>"
    )
    a(
        f"<tr><th>Normalization</th><td>{'up to date' if not rep['stale_normalization'] else _e(str(len(rep['stale_normalization'])) + ' item(s) stale')}"
        f"</td></tr><tr><th>Last discover</th><td>{_e(st['last_discover'] or 'never')}</td></tr></table></div>"
    )

    a(
        '<h2>Recent runs</h2><div class="card scroll"><table><tr><th>Command</th><th>Started (UTC)</th>'
        "<th>Duration</th><th>Exit</th><th>Result</th></tr>"
    )
    for r in data["runs"]:
        counts = ", ".join(f"{k} {v}" for k, v in (r["counts"] or {}).items())
        res = "; ".join(x for x in (counts, r["message"] or "") if x)
        exit_ = "running" if r["exit_code"] is None else r["exit_code"]
        a(
            f"<tr><td>{_e(r['command'])}</td><td>{_e(r['started_at'])}</td><td>{_dur(r['duration_s'])}</td>"
            f"<td>{_e(exit_)}</td><td>{_e(res[:200])}</td></tr>"
        )
    a("</table></div>")

    a('<h2>Errors</h2><div class="card">')
    if not rep["errors"] and not rep["retry_exhausted"]:
        a('<span class="sub">None.</span>')
    for comp, rows in rep["errors"].items():
        a(f"<b>{_e(comp)}</b> ({len(rows)})<ul>")
        for r in rows[:30]:
            a(f"<li><code>{_e(r['item_id'])}</code>: {_e(r['detail'][:240])}</li>")
        a("</ul>")
    if rep["retry_exhausted"]:
        a(
            f"<p>No longer retried automatically: {_e(', '.join(rep['retry_exhausted']))} "
            "(retry with <code>coffee-kb fetch --video ID</code>)</p>"
        )
    a("</div>")

    if sysi.get("log"):
        a(
            '<h2>Recent log <span class="sub">(data/logs/coffee-kb.log, '
            '<a href="logs?n=1000">more</a>)</span></h2>'
            f'<div class="card scroll"><pre>{_e(chr(10).join(sysi["log"]))}</pre></div>'
        )
    a("</div></body></html>")
    return "".join(out)

# coffee-kb — Design

Status: Phase 1 (data collection) in progress. Phases 2–5 are design notes only.

## Contents

1. Goals and non-goals
2. Architecture overview
3. Repository layout
4. Phase 1 — what gets collected
5. Phase 1 — storage formats
6. Phase 1 — CLI and pipeline behavior (incl. §6.6 YouTube request safety, §6.7 home-server deployment)
7. Phases 2–5 — roadmap (not to be built yet)
8. Visualization and interface catalog
9. Legal, scope, and ethics notes
10. Open decisions

---

## 1. Goals and non-goals

**Goal.** Build a personal, well-sourced knowledge base of James Hoffmann's coffee knowledge from his
public YouTube channel, structured so that many different follow-up projects (graphs, charts, chatbots,
flashcards, retrieval experiments) can be built on the same data without re-collecting it.

**Design priorities**, in order:

1. **Don't lose information at collection time.** Collection is the only step that's expensive or
   impossible to repeat (videos get deleted, captions get edited, IPs get blocked). Everything after it
   is cheap to redo.
2. **Traceability.** Every fact, chunk, edge, or answer must trace to a video and timestamp.
3. **Diversity of follow-up.** No downstream choice (chunk size, embedding model, ontology) is baked
   into collection.
4. **Learning value.** The user wants hands-on experience with vector databases and retrieval;
   Phase 4 is designed as a set of experiments, not just a feature.

**Non-goals.** Impersonating James Hoffmann; publishing his content; collecting paywalled or
members-only material; real-time or high-volume scraping.

---

## 2. Architecture overview

```
           ┌──────────── Phase 1 (build now) ────────────┐
YouTube ──►│ discover ─► fetch ─► raw/ ─► normalize ─► normalized/ │──► report
SponsorBlock│                (immutable)            (rebuildable)  │
           └──────────────────────────────────────────────┘
                                   │
           ┌──────────── Phases 2–5 (later) ───────────────────────────┐
           │ derived/extractions  (L1: claims, recipes, products, ...)  │
           │ derived/synthesis    (L2: topic pages, L3: reasoning style)│
           │ derived/graph        (typed nodes + signed edges)          │
           │ derived/chunks + embeddings (retrieval experiments)        │
           │ interfaces: Obsidian vault, charts, bots, Anki, MCP, ...   │
           └────────────────────────────────────────────────────────────┘
```

Three data tiers:

| Tier | Path | Rule |
|---|---|---|
| Raw | `data/raw/` | Exactly what the source returned. Write-once, snapshot-versioned. |
| Normalized | `data/normalized/` | Deterministic, no LLM. Consistent format across sources. Rebuildable from raw. |
| Derived | `data/derived/` | Anything interpretive (LLM extraction, chunking, embeddings, graph). Versioned by strategy/model. Rebuildable. |

The **manifest** (`data/manifest.sqlite`) tracks what exists and its status. It's an index, not
the source of truth — it can be rebuilt by scanning `raw/` (implement `coffee-kb rebuild-manifest`
only if cheap; otherwise note it as a TODO).

---

## 3. Repository layout

```
coffee-kb/
  CLAUDE.md                 # agent guide
  README.md                 # human setup + usage
  pyproject.toml
  config.toml               # channel handle, sleep intervals, languages, flags
  DESIGN.md                 # this file
  KICKOFF_PROMPT.md         # prompt used to start the Phase 1 build session
  deploy/
    coffee-kb-fetch.service # systemd oneshot: discover (weekly) + fetch daily batch + normalize
    coffee-kb-fetch.timer   # daily, randomized start time
  src/coffee_kb/
    __init__.py             # __version__ (recorded in provenance)
    cli.py                  # argparse entry: discover / fetch / normalize / report
    config.py
    manifest.py             # SQLite schema + accessors
    safety.py               # pacing floors, daily cap, block detection, cooldown latch (§6.6)
    paths.py                # all data/ path construction lives here, nowhere else
    sources/
      __init__.py
      youtube/
        discover.py         # list channel items (videos, shorts, streams)
        fetch.py            # metadata, captions, optional audio
        sponsorblock.py
    normalize/
      captions.py           # json3/vtt -> transcript JSONL; auto-caption de-dup
    report.py
    # Phase 2+ packages (do NOT create in Phase 1, listed for orientation):
    #   extract/  synthesize/  graph/  retrieval/  export/  viz/  bots/
  tests/
    fixtures/               # tiny trimmed samples: info.json, json3, vtt, sponsorblock response
    test_manifest.py
    test_normalize.py
    test_sponsorblock.py
    test_paths.py
    test_safety.py          # floors, cap, stop-on-block, cooldown refusal
  data/                     # git-ignored except data/README.md
    README.md
    manifest.sqlite
    COOLDOWN                # exists only after a block; JSON {reason, detected_at, item_id}
    raw/
      youtube/
        <video_id>/
          snapshots/
            <YYYYMMDDTHHMMSSZ>/
              info.json
              captions/
                en.manual.json3        # human captions, if present
                en.auto.json3          # auto captions (word-level timing)
              sponsorblock.json        # API response (or {"segments": []} for 404)
              audio.opus               # only with --audio
              fetch.json               # provenance: tool versions, args, timings, errors
    normalized/
      youtube/
        items.jsonl                    # one row per video: id, title, dates, type, chapters, ...
        transcripts/<video_id>.<track>.jsonl
        sponsor_segments.jsonl
    derived/                           # empty in Phase 1; created by later phases
```

---

## 4. Phase 1 — what gets collected

### 4.1 Discovery

Enumerate all public items on the channel using yt-dlp flat extraction of the channel tabs:

- `/videos` → `item_type = "video"`
- `/shorts` → `item_type = "short"`
- `/streams` → `item_type = "stream"`

Resolve the channel from the handle in `config.toml` (`@jameshoffmann`); do not hard-code a channel ID.
Also record playlist membership if cheaply available from the `/playlists` tab (useful as
human-curated topic labels later); if it's expensive, leave it as a TODO.

Discovery only inserts/updates manifest rows; it does not download per-video data. Items that disappear
from the channel listing are marked `listing_status = "missing_from_listing"`, never deleted.

### 4.2 Per-item fetch

| Component | How | Notes |
|---|---|---|
| Metadata | yt-dlp full `info.json` (no download) | Keep the **entire** dict. Includes title, description, upload_date, duration, tags, **chapters**, view/like/comment counts (snapshot values), thumbnails, categories, availability. |
| Human captions | yt-dlp `writesubtitles`, language `en` (and `en-*` variants), format **json3**, fall back to vtt | Best text quality; product names usually correct. Record if absent. |
| Auto captions | yt-dlp `writeautomaticsub`, `en`, **json3** | Fetch even when human captions exist: word-level timing + cross-check. Note json3 auto-captions include per-word offsets. |
| SponsorBlock | `GET https://sponsor.ajay.app/api/skipSegments?videoID=<id>&categories=[...]` | Categories: `sponsor, selfpromo, interaction, intro, outro, preview, music_offtopic, filler`. 404 = no segments → store `{"segments": []}`. Store the response as-is. |
| Audio | yt-dlp `bestaudio`, opus | **Off by default** (`--audio`). Enables future re-transcription (better Whisper, custom vocabulary, speaker diarization). |
| Pinned comment | — | Stretch goal only. yt-dlp comment fetch is heavy; try with `max_comments` limited to a handful sorted by top and check `is_pinned`. If unreliable, skip and leave a TODO. Never fetch all comments. |
| Video | — | Not collected. Keyframes may be added in a later phase (§7.6). |

### 4.3 Things recorded per fetch (`fetch.json`)

`fetched_at`, `coffee_kb_version`, `yt_dlp_version`, requested components, per-component result
(`ok` / `absent` / `error` + message), HTTP status for SponsorBlock, durations. This file is what makes
a snapshot self-describing.

---

## 5. Phase 1 — storage formats

### 5.1 Snapshot semantics

- A fetch writes a new `snapshots/<UTC timestamp>/` directory (write to a temp dir, then rename
  atomically when complete — a crashed fetch must never leave a half-snapshot that looks complete).
- `fetch` skips items whose latest snapshot is complete for the requested components.
- `fetch --refresh` creates a new snapshot even if one exists; old snapshots are kept.
- The manifest records the latest complete snapshot path per item.

### 5.2 Manifest schema (SQLite)

Starting point; adjust as needed but keep the intent.

```sql
CREATE TABLE items (
  source_type      TEXT NOT NULL,          -- 'youtube'
  item_id          TEXT NOT NULL,          -- YouTube video ID
  item_type        TEXT,                   -- 'video' | 'short' | 'stream'
  title            TEXT,
  upload_date      TEXT,                   -- YYYY-MM-DD
  duration_s       INTEGER,
  listing_status   TEXT NOT NULL,          -- 'listed' | 'missing_from_listing'
  availability     TEXT,                   -- 'public' | 'unavailable' | 'skipped_restricted' | ...
  first_seen_at    TEXT NOT NULL,
  last_seen_at     TEXT NOT NULL,
  latest_snapshot  TEXT,                   -- relative path, NULL until fetched
  PRIMARY KEY (source_type, item_id)
);

CREATE TABLE components (                  -- per item, per component status
  source_type   TEXT NOT NULL,
  item_id       TEXT NOT NULL,
  component     TEXT NOT NULL,             -- 'metadata' | 'captions_manual' | 'captions_auto'
                                           -- | 'sponsorblock' | 'audio' | 'pinned_comment'
  status        TEXT NOT NULL,             -- 'ok' | 'absent' | 'error' | 'pending' | 'skipped'
  detail        TEXT,                      -- error message or note
  snapshot      TEXT,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (source_type, item_id, component)
);

CREATE TABLE runs (
  run_id       INTEGER PRIMARY KEY,
  command      TEXT NOT NULL,
  args_json    TEXT,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  summary_json TEXT                        -- counts: fetched / skipped / errors
);
```

### 5.3 `normalized/youtube/items.jsonl`

One line per item, derived from the latest `info.json`:
`source_type, item_id, item_type, title, description, upload_date, duration_s, tags, categories,
chapters [{title, start_ms, end_ms}], view_count, like_count, comment_count, counts_as_of,
url, snapshot, normalizer_version`.

### 5.4 `normalized/youtube/transcripts/<video_id>.<track>.jsonl`

`track` ∈ `manual`, `auto`. One line per cue/segment:

```json
{"source_type": "youtube", "item_id": "abc123", "track": "manual",
 "start_ms": 312400, "end_ms": 315900, "text": "Finer grind means more extraction...",
 "words": [{"w": "Finer", "start_ms": 312400}, ...],
 "chapter": "Grind size", "in_sponsor_segment": false,
 "snapshot": "raw/youtube/abc123/snapshots/20261014T093000Z",
 "normalizer_version": "0.1.0"}
```

- `words` only when the source has word timing (auto json3); otherwise omit.
- `chapter` is joined from the item's chapters by time.
- `in_sponsor_segment` is joined from SponsorBlock (`sponsor`, `selfpromo` categories); keep all
  categories available in `sponsor_segments.jsonl` for later.
- **Auto-caption cleanup:** YouTube auto captions (especially VTT) repeat text across rolling cues.
  json3 avoids most of this; whichever format is used, the normalizer must emit each spoken word once.
  Write a test with a fixture that demonstrates the duplication.
- Strip markup tags and `[Music]`-style annotations into a separate `annotations` field rather than
  deleting them silently.

### 5.5 `normalized/youtube/sponsor_segments.jsonl`

`item_id, category, start_ms, end_ms, votes, uuid` (whatever the API returns, renamed consistently).

---

## 6. Phase 1 — CLI and pipeline behavior

```
coffee-kb discover                       # refresh channel listing into manifest   [talks to YouTube]
coffee-kb fetch [--limit N] [--video ID ...] [--since YYYY-MM-DD] [--type video|short|stream]
                [--audio] [--refresh] [--dry-run]                                  [talks to YouTube]
coffee-kb normalize [--video ID ...]     # rebuild normalized/ from latest snapshots [offline]
coffee-kb report [--format text|json]                                               [offline]
coffee-kb status                         # cooldown state, today's count vs. cap, next eligible time [offline]
coffee-kb clear-cooldown                 # user-only; refuses if < 48 h since the block [offline]
```

There is deliberately **no** `--sleep` override on the command line. Pacing comes only from
`config.toml` and is checked against the floors in §6.6. `--limit` can't exceed the remaining daily
allowance.

### 6.1 `discover`
Lists channel tabs, upserts `items`, updates `last_seen_at`, marks vanished items. Prints counts by
type. Uses the same request pacing as `fetch` (it paginates, so it makes tens of requests). Run it
**at most once a week**; the scheduled job enforces this via the `runs` table.

### 6.2 `fetch`
- Checks the cooldown latch and daily cap **before** making any request (§6.6).
- Default order: newest first (so `--limit 20` gives recent, well-captioned videos for inspection).
- Skips items with a complete latest snapshot unless `--refresh`.
- Paces requests per §6.6: 60–120 s between videos, 5–10 s between requests, 10 s before subtitles.
- On a block signal: stop immediately, write the cooldown latch, record status, exit non-zero with a
  clear message. Transient network errors (DNS, connection reset, timeouts) get at most 2 retries with
  a 60 s wait; block signals get none.
- On unavailable/private/members-only: record `availability`, mark components `skipped`, continue.
- Prints progress with an ETA, since runs are long by design.

### 6.3 `normalize`
Pure function of `raw/` → `normalized/`. Overwrites normalized outputs. No network.

### 6.4 `report`
Prints:
- Items by type and availability; listed vs. fetched.
- Caption coverage: manual+auto / auto-only / manual-only / none.
- SponsorBlock coverage: items with ≥1 segment.
- Total hours of video fetched and total transcript words (manual preferred, auto fallback).
- Errors grouped by component with item IDs.
- Items fetched but not normalized (stale normalization).
- Safety state: cooldown latch present? (reason + since when), videos fetched in the last 24 h vs. cap,
  estimated days remaining to finish the channel at the current cap.

### 6.5 Config (`config.toml`)

```toml
[youtube]
channel_handle = "@jameshoffmann"
tabs = ["videos", "shorts", "streams"]
caption_languages = ["en", "en-GB", "en-US"]

[fetch]
# Pacing. Floors are enforced in code (§6.6); lower values are an error, not a warning.
sleep_between_videos_s = 60        # floor 30
sleep_between_videos_jitter_s = 60 # random extra 0..N, so 60–120 s per video
sleep_between_requests_s = 5       # yt-dlp sleep_interval_requests; floor 3
sleep_between_requests_max_s = 10  # yt-dlp max_sleep_interval_requests (randomized range)
sleep_before_subtitles_s = 10      # yt-dlp sleep_interval_subtitles; floor 5
max_videos_per_24h = 30            # ceiling 50
discover_min_interval_days = 7
max_transient_retries = 2
cooldown_min_hours = 48
audio = false

[sponsorblock]
categories = ["sponsor", "selfpromo", "interaction", "intro", "outro", "preview", "music_offtopic", "filler"]
```

### 6.6 YouTube request safety

**Why it matters.** YouTube's bot detection is based mostly on *what kind of client* is asking
(non-browser clients, no sign-in, scripted timing) rather than raw volume. When it triggers, it flags
the **public IP**, which the whole household shares. The typical progression is: yt-dlp requests fail
("Sign in to confirm you're not a bot", HTTP 429 on caption downloads) → if ignored, CAPTCHAs/"unusual
traffic" pages in normal browsers on YouTube and Google Search. It usually clears in hours to a few
days, and residential IPs are rarely blocked permanently. The user's Google account is never involved,
because no cookies are used; keeping it that way is the main reason for the no-cookies rule.

**Which parts make YouTube requests:** `discover` (channel tab pages, paginated) and `fetch`
(watch page / player API for metadata, one request per caption track, audio streams if enabled).
Nothing else. SponsorBlock (`sponsor.ajay.app`) is a separate community service and is paced
independently (1 request per video, no special handling beyond normal politeness).

**Expected volume** without audio: a few YouTube requests per video, ≈2–3k requests for the whole
channel. That's small; at 30 videos/day the channel takes about two weeks, with each daily run
lasting 45–60 minutes.

**Mechanisms (all in `safety.py`, all tested offline):**

| Mechanism | Behavior |
|---|---|
| Pacing | 60–120 s randomized between videos; 5–10 s randomized between requests inside a video; 10 s before each subtitle download. Strictly sequential. |
| Floors | Config load fails if between-video sleep < 30 s, between-request sleep < 3 s, subtitle sleep < 5 s, or daily cap > 50. No CLI or env override. |
| Daily cap | Count successful + failed video fetch attempts in the last 24 h from the manifest; refuse to start more than `max_videos_per_24h`. `--limit` is clamped to the remaining allowance. |
| Discover throttle | `discover` refuses to run more often than every `discover_min_interval_days` (manual `--force` allowed, but it still respects the cooldown). |
| Block detection | Classify yt-dlp/HTTP errors. **Block signals:** "Sign in to confirm", "not a bot", HTTP 429, HTTP 403 from YouTube/googlevideo hosts, CAPTCHA or consent-wall HTML where JSON was expected. Unknown errors on 3 consecutive videos are also treated as a block signal. |
| Stop on block | Abort the run immediately; no retry; finish writing the manifest; exit code 75 (EX_TEMPFAIL) so the timer log shows it clearly. |
| Cooldown latch | Write `data/COOLDOWN` (`{reason, detected_at, item_id, yt_dlp_version}`). While present, `discover` and `fetch` exit immediately without network access. |
| Clearing | Only `coffee-kb clear-cooldown`, only after `cooldown_min_hours` (48 h), and it prints a reminder to upgrade yt-dlp first. Agents never run it. |
| No evasion | No cookies, proxies, VPNs, user-agent rotation, PO-token providers, or impersonation plugins. yt-dlp is used with default client settings. |
| Audio off | `--audio` must be passed explicitly or set in config; it doubles as the riskiest traffic. |

**Before each run** (documented in README, printed by `status`): upgrade yt-dlp
(`uv lock --upgrade-package yt-dlp && uv sync`), since many failures are an outdated extractor rather
than a block.

**Don't run from shared networks** (work, office, university): a flag there affects other people.

**Optional lower-risk variant (open decision, §10):** use the official YouTube Data API v3 (free API
key) for `discover` and basic metadata, leaving yt-dlp only for the two caption files per video.
This roughly halves unofficial traffic. Trade-offs: an API key to manage, and chapters aren't returned
as structured data (parse them from description timestamps, or still fetch `info.json`).

### 6.7 Deployment on the home server

The real collection runs on the user's always-on home server (Ubuntu Server, Wi-Fi, on the home LAN),
not on a dev machine and never in the cloud. It shares the household's public IP, so §6.6 applies in
full; the benefit is that it can run a small batch every day unattended.

Setup (README must document; no personal hostnames/usernames/IPs in the repo):

1. Install `uv`, clone the repo, `uv sync`.
2. Run the **20-video trial manually** in a `tmux` session first and inspect the output:
   `uv run coffee-kb discover && uv run coffee-kb fetch --limit 20 && uv run coffee-kb normalize && uv run coffee-kb report`.
3. Only after the trial looks good, install the timer:
   - `deploy/coffee-kb-fetch.service`: `Type=oneshot`, runs as the regular (non-root) user,
     `WorkingDirectory` = repo; `ExecStart` runs `discover` (it self-throttles to weekly), then
     `fetch` (self-capped at the daily allowance), then `normalize`. Uses `Nice=10`.
     Exit code 75 is recorded but not restarted (`Restart=no`).
   - `deploy/coffee-kb-fetch.timer`: `OnCalendar=*-*-* 03:00`, `RandomizedDelaySec=2h`,
     `Persistent=true`.
   - Install as a user unit (`~/.config/systemd/user/`, plus `loginctl enable-linger <user>`) or a system
     unit with `User=`; document both, recommend the user unit.
4. Monitoring: `journalctl --user -u coffee-kb-fetch`, `uv run coffee-kb status`,
   `uv run coffee-kb report`.
5. No inbound ports are needed; the firewall stays as it is. The job only makes outbound HTTPS.
6. Disk: captions + metadata for the whole channel are tens of MB; audio (if ever enabled) a few GB.
7. Once the archive is complete, the timer can stay on to pick up new uploads (it'll typically fetch
   0–2 videos per day).

---

## 7. Phases 2–5 — roadmap (NOT to be built yet)

Recorded here so Phase 1 decisions keep these possible.

### 7.1 Phase 2 — Extraction (L1, per video)

LLM pass over each normalized transcript (+ description + chapters), producing typed records:

| Record | Fields (sketch) |
|---|---|
| `claim` | text, subject, polarity/sign, confidence/hedging, is_opinion vs. is_fact |
| `recipe` | method, dose_g, water_g or ratio, grind (descriptive), temp_c, time, steps, gear |
| `product_mention` | product, category, verdict (+/−/mixed), price if stated, is_sponsored |
| `experiment` | question, variables, method, result, conclusion |
| `recommendation` | audience/context, advice |
| `correction` | what was corrected (from description/pinned comment) |
| `open_question` | what he says is unknown/unresolved |

Rules:
- **Two-pass approach**: broad generic extraction first (with free-text labels), focused passes later.
  Don't design the full ontology before seeing real output.
- **Save raw LLM output** next to parsed records.
- Every record carries provenance: `item_id, start_ms, end_ms, track, extractor, schema_version,
  model, prompt_hash, extracted_at`.
- Exclude or flag content inside sponsor segments.
- Output: `derived/extractions/<schema_version>/<video_id>.jsonl`.
- Re-extraction for the whole channel is estimated at tens of dollars, so iterate freely.
- Start with ~20 videos on one topic (e.g. pour-over) and review by hand before scaling.

### 7.2 Phase 3 — Synthesis (L2 topic pages, L3 reasoning style)

- **Deduplicate claims**: embed claims, cluster near-duplicates → one canonical claim with N sources.
- **Entity resolution**: "the Niche", "Niche Zero", "my Niche" → one entity (embeddings + rules).
- **Topic pages** (espresso, pour-over, water, grinders, milk, roasting, origins/processing...):
  every statement links to L1 records.
- **Temporal awareness**: his opinions change (e.g. multiple V60 technique versions). Topic pages
  must say "as of <date> … earlier …", never average contradictory statements. Model with
  `superseded_by` / `contradicts` edges.
- **L3 reasoning profile**: how he reasons — tests before concluding, states uncertainty, considers
  price/accessibility, "it depends on your setup." Used for answer style, not facts.

### 7.3 Knowledge graph ontology (draft)

| Node type | Examples |
|---|---|
| Concept | extraction, TDS, channeling, bypass, fines |
| Variable | grind size, water temp, ratio, agitation, pressure, water hardness |
| Method | V60, AeroPress, espresso, French press, Moka pot |
| Equipment | specific grinders, kettles, machines, scales |
| Bean | origin, process (washed/natural/anaerobic), roast level |
| Taste | sour, bitter, astringent, sweet, body, clarity |
| Video | each source item |

Edge types: `increases`, `decreases` (signed, directional → cause-and-effect map), `part_of`,
`reviewed_in`, `recommends`, `superseded_by`, `contradicts`, `mentioned_in`.
Every edge carries a list of source references (item_id + start_ms).
Storage options: Postgres tables (nodes/edges) with recursive CTEs, or files exported to Obsidian.

### 7.4 Phase 4 — Retrieval and vector search (learning track)

Assessment: vector search **fits** this project (semantic transcript search, FAQ-bot matching, claim
dedup, entity resolution, channel atlas). A dedicated vector DB **server** is overkill at this scale
(~5k transcript chunks + ~20–50k claims; ~200 MB of vectors; brute-force numpy is milliseconds).
Use something embedded or Postgres-based.

- Recommended: **Postgres + pgvector** (most transferable; vectors next to SQL filters like date,
  method, item_type). Alternative: LanceDB or Chroma for zero-setup.
- Not for: relationships (→ graph), numeric/structured queries (→ SQL), timelines (→ SQL by date).

Experiment plan, in order:
1. **numpy brute-force baseline** first.
2. **Chunking strategies**: by chapter, fixed token window with overlap, by extracted claim.
   Store as `derived/chunks/<strategy>/`; chunk IDs reference `(item_id, track, start_ms, end_ms)`.
3. **Embedding models**: local (sentence-transformers) vs. API; check handling of coffee jargon.
   Store as `derived/embeddings/<model>/<chunk_strategy>/`.
4. **Hybrid search**: BM25 + vector (product names like "EK43" are where pure vector search struggles).
5. **Metadata filtering**: date, item_type, chapter, sponsor exclusion.
6. **Reranking**: top-50 → cross-encoder → top-5.
7. **Evaluation (most important)**: 30–50 hand-written questions with known answer locations
   (`item_id` + approximate timestamp). Metric: recall@5 / MRR. Run on every change.

Optional: the full corpus is near the size of a long-context window (~1.3M tokens of transcript;
L1 notes much smaller), so "load all L1 notes + prompt caching" is a viable no-retrieval baseline for
deep questions.

### 7.5 Phase 5 — Interfaces

See §8. Suggested first set: Obsidian vault, cause-and-effect map, brewing control chart,
decision-tree troubleshooter.

### 7.6 Later collection extensions (only if wanted)

- Re-transcribe from audio with Whisper + coffee vocabulary prompt (if `--audio` was used).
- Speaker diarization (guests vs. him).
- Keyframes per chapter + vision model for on-screen text, charts, products.
- Other sources via the same `source_type → item → segment` model: his podcast/guest appearances,
  other creators for comparison. Each is a new fetcher under `sources/`.
- Weekly scheduled `discover && fetch` to stay current.

---

## 8. Visualization and interface catalog

All of these read from `normalized/` + `derived/` and link back to `youtube.com/watch?v=<id>&t=<s>s`.

### 8.1 Visualizations

| # | Idea | Data needed | Notes |
|---|---|---|---|
| 1 | **Cause-and-effect map** — variables you control → what happens in the cup → taste | Graph with signed `increases`/`decreases` edges | Likely the most useful single picture. Left-to-right layered layout. |
| 2 | **Knowledge graph, Obsidian style** | Graph exported as Markdown notes with `[[wikilinks]]` | Obsidian renders the graph for free. Needs typed nodes/filters or it becomes a hairball. Custom alternative: Cytoscape.js / sigma.js with filter-by-type, color-by-year, path highlighting ("sour" → what to change). |
| 3 | **Brewing control chart** — extraction yield (x) vs. TDS (y) with ideal box, his recipes plotted | `recipe` records (+ stated measurements) | Classic coffee chart; color by method. |
| 4 | **Opinion timeline** — swimlane per topic, dots per video, markers where advice changed | Claims with dates + `superseded_by` | Makes view changes visible. |
| 5 | **Channel atlas** — UMAP of chunk embeddings, clustered, click to jump to video | Embeddings | Map of the channel as topic islands. |
| 6 | **Gear landscape** — price vs. verdict, faceted by category | `product_mention` | |
| 7 | **Topic × year heatmap** | Topic labels per video + dates | Shows eras (e.g. home-espresso period). |
| 8 | **Annotated flavor wheel** — SCA wheel with his notes and fixes per defect | Taste nodes + fixes | |

### 8.2 Non-agent outputs

- **Obsidian vault** as a personal wiki (browse, backlinks, edit).
- **Anki flashcards** from claims (spaced repetition).
- **Recipe database** in SQLite, browsable with Datasette.
- **Printable cheat sheets** per brew method: current recipe, variables, common fixes.
- **Brew calculator** from his ratios/recipes.

### 8.3 Chatbots without an LLM at runtime

(An LLM is still used at build time for extraction.)

| Type | How | Strength | Weakness |
|---|---|---|---|
| Decision tree | Guided buttons: problem → method → fix + video link | Fully predictable; matches his "change one variable" teaching | Can't go outside the tree |
| FAQ retrieval | Build-time Q&A pairs; runtime BM25 or local embeddings match | Never invents; offline; free | Brittle to phrasing; can't combine answers |
| Graph query bot | Pattern-matched questions → graph query → templated sentence | Good for "what causes X" | Rigid, templated feel |

Recommended combo: decision tree + FAQ retrieval.

### 8.4 LLM-based interfaces

- **Cited Q&A** (most reliable): answer + timestamp links.
- **Brew assistant**: user's gear + symptoms → diagnosis using his framework.
- **Comparison / history queries**: "how has his view on X changed".
- **Style-grounded chat**: answers in a conversational style informed by L3, but must cite sources and
  explicitly say "not covered; extrapolating from …" when beyond the evidence. Not presented as him.
- **MCP server**: tools like `search_knowledge`, `get_topic`, `get_recipe` so any agent can query it.

---

## 9. Legal, scope, and ethics notes

- **Personal use** of a research index over public videos is low-risk and common.
- **Risk increases with**: publishing the knowledge base or a hosted app; redistributing full
  transcripts (copyright); presenting a bot as "James Hoffmann" (right of publicity / impersonation).
- Paraphrased facts + links back to videos (the graph, charts, cheat sheets) carry much less risk than
  transcripts.
- **YouTube ToS** prohibits downloading content; captions/metadata are the lower-risk tier, audio is
  opt-in for that reason. Collection is very slow, capped, and non-evasive (no cookies, no proxies, no
  bot-detection workarounds), and stops itself at the first block signal (§6.6).
- **Excluded**: members-only content, his books, other paid material, bulk public comments (noise +
  other people's personal data).
- Any step toward publishing should be raised with the user first.

---

## 10. Open decisions

Record the answer here when decided.

| Question | Default until decided |
|---|---|
| Collect audio? | No (flag exists) |
| Include shorts and livestreams in later phases? | Collected and labeled; downstream can filter |
| Pinned comments | Stretch goal |
| Postgres+pgvector vs. LanceDB/Chroma for Phase 4 | pgvector |
| Which interface first after Phase 2 | Obsidian vault + cause-and-effect map |
| Project name | `coffee-kb` (working name) |
| Use official YouTube Data API for discover/metadata (§6.6)? | No for now; revisit after the 20-video trial |
| Daily cap / pacing | 30 videos per 24 h, 60–120 s between videos |

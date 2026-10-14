# data/

Everything in this directory except this README is git-ignored. It holds third-party content
(transcripts, metadata, possibly audio) and local state. Never commit it.

```
data/
  manifest.sqlite      # index: items, per-component status, runs, fetch attempts (daily cap)
  COOLDOWN             # exists only after a YouTube block signal; fetch/discover refuse to run
  cooldown_history/    # cleared latches, kept for the record
  raw/                 # IMMUTABLE. Write-once snapshots of exactly what the sources returned.
    youtube/
      _channel/snapshots/<YYYYMMDDTHHMMSSZ>/   # discover: one <tab>.json per channel tab
      <video_id>/snapshots/<YYYYMMDDTHHMMSSZ>/
        info.json                       # full yt-dlp metadata dict
        captions/<lang>.manual.json3    # human captions (or .vtt fallback), if present
        captions/<lang>.auto.json3      # auto captions, if present
        sponsorblock.json               # SponsorBlock API response ({"segments": []} for 404)
        audio.<ext>                     # only with --audio
        fetch.json                      # provenance: versions, args, per-component results
  normalized/          # REBUILDABLE from raw/ with `coffee-kb normalize`
    youtube/
      items.jsonl
      transcripts/<video_id>.<manual|auto>.jsonl
      sponsor_segments.jsonl
  derived/             # later phases (empty in Phase 1)
```

Directories named `.tmp-*` inside `snapshots/` are in-progress or crashed fetches. They are never
treated as snapshots and are cleaned up the next time that video is fetched.

The whole directory is path-independent: copy it to another machine (e.g. from the PC to the
home server) and the pipeline continues where it stopped.

# Web application architecture and phase status

Status: Phase 1 and Phase 2 implemented on 2026-08-04.

The authoritative product behavior remains in
[`WEB_UI_SPEC.md`](WEB_UI_SPEC.md); the proven media settings remain in
[`CURRENT_PIPELINE.md`](CURRENT_PIPELINE.md). This note maps implemented concepts
to code and records the current boundary without claiming later phases.

## Implemented

### Phase 1 — inventory

- `webapp/scan/Dockerfile` reproducibly installs `lsdvd`, `libdvdread`,
  libdvdnav, and MPlayer.
- `webapp/scan/catalog.py` scans only the two configured ISO filenames through
  a read-only source mount. It stores raw discovery output for audit and
  normalizes discs, titles, streams, and chapter start/end times into SQLite.
- The same scanner extracts navigation-correct title streams into the runtime
  data tree, then generates and validates a representative JPEG plus H.264/AAC
  proxy for every non-skipped, non-menu chapter using the existing FFmpeg
  container.
- `webapp/server/` implements the authenticated library and title/chapter
  browser. Chapter display names, notes, and priorities are catalog metadata;
  the ISOs are never altered.

Verified inventory on 2026-08-04: DVD 1 contains one 11,582.480-second title
with 15 chapters; DVD 2 contains one 4,253.000-second title with seven chapters.
All 22 chapter proxies and thumbnails validated, and chapter durations sum to
their title durations. Extraction durations were 11,582.464 and 4,252.960
seconds respectively, within DVD timestamp tolerance.

### Phase 2 — durable jobs

- `webapp/db/migrations/001_initial.sql` contains the WAL-mode data model for
  discs, titles, chapters, slices, jobs, future chunks, append-only events, and
  registered artifacts.
- `webapp/server/api.py` owns validated slice/queue APIs, deduplication,
  state-checked commands, ordering, polling, and SSE. `webapp/server/auth.py`
  provides password sessions and CSRF checks.
- `webapp/worker/runner.py` is the only restoration queue owner. It claims jobs
  transactionally, holds one host GPU lock, enforces the free-space reserve,
  captures structured events/logs/metrics, runs the fixed pipeline command,
  validates output, and recovers active jobs after restart.
- `pipeline_v3.sh` retains its original CLI and defaults. Optional worker-only
  environment settings relocate its work tree under `webapp/data/`, overmount
  archival sources read-only, publish stage-boundary events, and honor a fixed
  cancellation control file between major stages.
- `webapp/server/templates/queue.html` and the shared frontend show queue state,
  stage, frame progress, ETA, ordering, and controls. The UI explicitly says
  that cancellation waits for a stage boundary and pause is unavailable.

The server binds to `127.0.0.1:8093`, requires a non-hardcoded password, protects
writes with CSRF, and serves only database-registered media paths.

End-to-end verification on 2026-08-04 created a title-relative five-second
slice through the authenticated API and ran it through the queue worker and
`pipeline_v3.sh`. The registered result validated at exactly 5.000 seconds and
250 frames: 1920×1440 HEVC Main 10 at 50 fps with limited-range BT.709 tags and
48 kHz stereo FLAC. The worker recorded 419 structured state, stage, progress,
metric, and log events during the 339-second run. Authenticated HTTP Range
delivery returned `206 Partial Content` for the completed output.

## Not implemented

### Phase 3

There are no independently durable 750-frame restoration units yet. Therefore
there is no real pause, paused state workflow, chunk checksum map, chunk-level
resume, context-aware final assembly, or chunk-derived ETA. Existing completed
major stages remain reusable after failure/restart, exactly as
`pipeline_v3.sh` already supports. A running cancellation waits for the active
major stage to finish.

### Phase 4

There is no synchronized original/restored comparison player, automatic
comparison generation, cleanup/retention UI, packaged systemd units, packaged
web Docker deployment, or full browser test suite. Minimum private session
authentication, CSRF protection, safe media serving, and action events were
implemented earlier than the phase outline because they are required for a UI
that can start work.

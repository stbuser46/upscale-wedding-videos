# Web application architecture and phase status

Status: Phase 1 and Phase 2 implemented on 2026-08-04; durable ~750-frame
restoration units in production (the worker default); the complete Yacoob &
Aysha wedding restored on the local GPU; a cloud fan-out foundation added
2026-09-12.

The authoritative product behavior remains in
[`WEB_UI_SPEC.md`](WEB_UI_SPEC.md); the proven media settings remain in
[`CURRENT_PIPELINE.md`](CURRENT_PIPELINE.md). This note maps implemented concepts
to code and records the current boundary without claiming later phases.

## Restoration milestone

The pipeline has restored the entire **Yacoob & Aysha** wedding end to end on
the local RTX PRO 6000: all 22 chapters (DVD 1: 15, DVD 2: 7), each processed as
a chain of durable ~750-frame SeedVR2 units, validated, registered in the
catalog, and delivered to the NAS. Details and throughput are in
[`CURRENT_PIPELINE.md`](CURRENT_PIPELINE.md).

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

## Cloud fan-out (foundation)

Added 2026-09-12 (commit `9e7ccf5`). This is the substrate for running durable
restoration units on rented RunPod GPUs instead of, or alongside, the single
local card. It is deliberately a **standalone "Stage A"**: it can be proved end
to end for a couple of dollars before any worker or database integration is
written, so nothing here can disturb a running local restoration.

- `lib/seedvr2_unit_args.sh` is the single source of truth for the SeedVR2
  durable-unit argv. Both paths render their command from it — `pipeline_v3.sh`
  locally (inside `docker run`) and the cloud path natively on a pod — so the
  two can never drift. One builder, no fork.
- `webapp/cloud/runpod_api.py` is a stdlib-only RunPod control-plane client:
  pod CRUD over REST (`rest.runpod.io/v1`) and the GPU catalogue/stock over
  GraphQL (`api.runpod.io/graphql`), plus the ssh/rsync plumbing to drive a pod.
  It raises `RunpodError` instead of calling `sys.exit`, so it is safe to call
  from inside the worker's venv later without a library killing the process.
- `webapp/cloud/cli.py` is the standalone operator CLI: `offers`, `up`, `unit`,
  `status`, `down`, `reap`. It rents a pod, provisions it, runs one unit, and
  tears it down, touching neither the catalog database nor the worker. Every pod
  is recorded in `webapp/data/cloud_pods.json` **before** the create call
  returns, and teardown never depends on a clean exit.
- `webapp/cloud/reaper.py` is the money-safety kill switch: it asks RunPod which
  pods carry this project's `wedding-` name prefix and deletes them, independent
  of the database and worker.
- `docker/seedvr2-pod/` builds the pod image and provisioning scripts. Pods have
  no Docker daemon, so the pod's own image *is* the SeedVR2 runtime that
  `pipeline_v3.sh` would otherwise launch as a container.
- `webapp/db/migrations/006_cloud_pods.sql` adds the `cloud_pods` ledger: one
  row per pod ever created, written `state='creating'` before the create call
  returns so a crash mid-call still leaves a trace, with the reaper reconciling
  live pods against it and spend derived as rate × lifetime.
- `scripts/test_slice_equivalence.sh` (with `scripts/compare_units.sh`) is the
  slicing proof: stage-1 output is FFV1 with `-g 1`, so every frame is a
  keyframe and a stream-copy slice cuts on an exact frame boundary. The test
  compares per-frame decoded MD5s (CPU-only, no GPU) to prove that shipping a
  pod only its unit's frames feeds SeedVR2 byte-identically to a whole-file read.

**Not yet built:** the webapp worker does not schedule, launch, or reconcile
pods, so there is no automatic local/cloud placement, no multi-pod fan-out of a
single chapter, and no UI surface. The `cloud_pods` table and ledger exist, but
the worker path that fills them is future work.

## Not implemented

### Phase 3 (durable units landed; interactive pause still pending)

Durable ~750-frame units are now the worker's production default: `pipeline_v3.sh`
runs each unit in its own SeedVR2 container, `assemble_units.sh` performs the
context-aware final assembly, and completed units survive failure/restart. On
restart the worker re-probes every finished unit before restoring anything new,
so resume is unit-level rather than whole-stage, and the job's ETA is recomputed
from measured per-unit throughput (with a conservative planning rate seeded for
queued jobs). See `CURRENT_PIPELINE.md` and `webapp/README.md` for the measured
behavior.

What remains unimplemented from the original Phase 3 outline is **interactive
pause and a paused-state workflow**: a running cancellation still waits for the
active unit/stage boundary to finish rather than pausing, and there is no
resumable "paused" state exposed in the UI.

### Phase 4

There is no synchronized original/restored comparison player, automatic
comparison generation, cleanup/retention UI, packaged systemd units, packaged
web Docker deployment, or full browser test suite. Minimum private session
authentication, CSRF protection, safe media serving, and action events were
implemented earlier than the phase outline because they are required for a UI
that can start work.

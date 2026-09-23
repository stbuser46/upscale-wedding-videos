# Web application architecture and phase status

Status: Phase 1 and Phase 2 implemented on 2026-08-04; durable ~750-frame
restoration units in production (the worker default); the complete Yacoob &
Aysha wedding restored on the local GPU; and an opt-in cloud fan-out path that
dispatches units across a fleet of rented RunPod GPUs (default executor remains
local).

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

## Cloud fan-out

Runs durable restoration units on rented RunPod GPUs instead of, or alongside,
the single local card. It landed in two layers: a **foundation** (2026-09-12,
commit `9e7ccf5`) that could be proved end to end for a couple of dollars as a
standalone CLI, then the **worker/fleet integration** that dispatches a
chapter's units across a concurrent pod fleet.

The cloud path is **opt-in and dormant by default**. The worker chooses its
executor per process from `WEDDING_EXECUTOR` (default `local`); only
`WEDDING_EXECUTOR=cloud` routes durable units to pods (`run_job` →
`_run_units_cloud`). With the default, local-GPU restoration through the GUI is
byte-for-byte unchanged, and none of the code below runs. The choice is
worker-wide — there is no per-job cloud toggle in the API or GUI.

### Foundation (standalone)

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

### Worker/fleet integration

- `webapp/worker/slicer.py` cuts a chapter's stage-1 file into per-unit slices
  on exact frame boundaries (the equivalence proven above), so each pod is
  shipped only the frames its unit needs. The final unit tolerates a bounded
  short slice (`allow_short`), mirroring the local path's tail tolerance.
- `webapp/cloud/fleet.py` is a concurrent pod slot pool: it brings up
  `WEDDING_CLOUD_MAX_SLOTS` pods (default 16) in parallel, hands each out as a
  slot the moment it is provisioned, retires an idle pod immediately so it stops
  billing, and enforces `WEDDING_CLOUD_SPEND_CAP_USD` (default 250). It is a
  context manager whose `terminate_all()` also sweeps RunPod for any stray pod
  carrying this job's name prefix, so teardown never depends on a clean exit.
- `webapp/cloud/executor.py` restores one unit on a pod with abort polling and a
  hard per-unit timeout. Its three phases — `upload_unit_slice`, `restore_unit`,
  `download_unit` (all running the shared `seedvr2_unit_argv`) — are exposed
  separately so the dispatcher can pipeline them per pod; `run_unit_remote`
  composes them sequentially for single-pod/CLI use.
- `webapp/worker/runner.py` gains `_run_units_cloud`, pod reconciliation on
  startup (`_reconcile_cloud_pods`, terminating pods orphaned by a prior graceful
  restart), and the executor switch. The local durable-unit and whole-pipeline
  paths are untouched.

**Pipelined dispatch (2026-09-23).** `_run_units_cloud` keeps rented GPUs busy
instead of billing them through transfers:

- **Provision-first:** the fleet starts provisioning before the local stage-1
  deinterlace runs (pods take minutes to come up; the ~2-min prepare is hidden
  inside that window). A prepare failure still tears the fleet down.
- **Background pre-slicer:** one thread cuts slices in unit order a bounded
  distance ahead of the pods (~2×slots × ~220 MB on disk), so no pod ever waits
  on a local ffmpeg slice.
- **Warm-cache peer seeding:** the ~186 MB Inductor-cache tarball crosses the
  home upstream at most once per fleet (serialized); every later pod pulls it
  pod-to-pod at datacenter speed using an ephemeral job-scoped ed25519 key
  (`_ensure_cache_on_pod`, tested by `webapp/cloud/test_fleet_cache.py`). Any
  peer failure falls back to the old per-pod home upload. Observed motivation:
  during the 2026-09-23 live test the cache upload to pod 0 shared the home
  link with two unit-slice uploads and crawled; at 16 slots it would be ~3 GB.
- **Per-pod pipeline:** each pod is owned by one runner thread that uploads unit
  N+1 while N restores, and downloads/validates N in a helper thread while N+1
  restores — the paid GPU never idles on the home link. TTL refresh moves from
  `hand_back()` to per-completed-unit (pipelined pods never re-enter the slot
  queue); a heartbeat thread beats every 20 s during multi-minute restores.
- **Intermediate peer store (the "middle path"):** the multi-GB stage-1 FFV1
  crosses the home upstream at most ONCE per job — it is staged in the
  background onto the live pod with the best measured home ingress, unit slices
  are cut there with the exact local stream-copy command, delivered pod-to-pod
  with the job key, and each remote slice must match the locally-cut slice's
  framemd5 sha256 (`slice_frame_hash`) before a pod may restore it — a
  decoded-identity proof, cross-build-verified. Any failure or mismatch falls
  back to the per-unit home upload, so the peer path can only ever ship
  proven-identical bytes and can never be slower than the old design.
- **No unowned billing:** when the unit queue drains, runners call
  `fleet.mark_no_more_work()` — unclaimed pods in the slot queue are retired
  immediately, a pod that goes READY later is retired on arrival, and pending
  bring-ups are abandoned. Found live 2026-09-23: a slow-provisioning pod went
  READY after the queue emptied and billed idle until the spend cap tripped.
- **In-run retry:** a unit that fails on a pod is requeued for another pod
  (bounded at 2 total attempts) and the suspect pod is retired, instead of
  failing the whole run and re-provisioning a fresh fleet via auto-resume.
  Pause/cancel/cap semantics are unchanged and covered, with the rest of the
  dispatch logic, by the no-network mock suite
  `webapp/worker/test_cloud_dispatch.py`.
- `webapp/server/cloud_views.py` adds a read-only `/api/cloud` fleet-status
  endpoint (live pods, uptime, derived spend), rendered as a "Cloud fleet" panel
  on the queue page. It degrades to `{"enabled": false}` on a database without
  the `cloud_pods` table, so local-only deployments are unaffected.

**Money-safety (hardened 2026-09-19, after an independent xhigh review before the
first paid multi-pod run).** The blockers that review surfaced are fixed and
unit-verified without a GPU/RunPod (see `verify_*` proofs):

- **Hard spend cap.** `CloudFleet` runs a `_watchdog` thread that checks accrued
  `spend_so_far()` against `WEDDING_CLOUD_SPEND_CAP_USD` *while pods run* and
  tears the whole fleet down at the ceiling (`capped`) — not just at bring-up, so
  N already-launched pods can no longer bill past the cap unchecked.
- **Enforced TTL.** `pod_ttl_s` is now honoured: a pod that goes that long
  without finishing a unit is retired. `hand_back()` refreshes the clock on each
  completed unit, so a healthy pod chewing through units is never killed.
- **Honest teardown.** `_terminate` marks a ledger row `terminated` (which stops
  spend counting it) ONLY when the delete is confirmed; an unconfirmed delete
  stays `terminating` with `terminated_at` NULL, so spend keeps counting and the
  reaper finishes the job. `terminate_pod` treats only a 404/not-found as
  success (a 400 no longer masquerades as "gone"). `_reconcile_cloud_pods`
  reconciles the ledger against RunPod's live list rather than optimistically.
- **Idempotent create.** Pod creation is a non-idempotent POST that is no longer
  transport-retried; `_create_pod` adopts an existing pod by name before/after a
  create, so a lost response can't rent a billing twin. Total creates per slot
  ≤ `bring_up_attempts` (3), not the old 3×4×4.
- **Bounded auto-resume.** Interrupted jobs auto-requeue at most
  `MAX_AUTO_RESUMES` (3) via a persistent `jobs.auto_resume_count`, then park
  `failed` — a crash loop can't rent fleets forever. Reset on human
  start/resume/retry or completion.
- **Reaper backstop.** `webapp/cloud/reaper.py` decouples the balance query from
  the delete path (a GraphQL blip no longer blocks kills), gains `--loop` and an
  age filter (`--max-age-hours`), and ships as an age-filtered systemd timer
  (`wedding-reaper.timer`, >4h) that catches true orphans without touching a
  healthy in-progress fleet. Still set a RunPod account-level spend limit as the
  final backstop.

**Cross-GPU correctness fix:** the cloud slicer no longer hardcodes 50 fps — it
takes the job's real `output_fps`, so NTSC (59.94) units slice on the correct
frame boundary. The old default silently shipped shifted footage that frame-count
validation could not catch (only unit 0 was correct — which is why the single-
unit smoke test passed). `test_slice_equivalence.sh` now derives fps from the
input and proves interior NTSC units are frame-exact.

**Concurrency:** local and cloud workers hold separate locks and take a renewable
per-job lease (`jobs.lease_expires_at`, renewed by a background thread). Recovery
requeues only jobs whose lease is stale, so a second worker can no longer requeue
and double-run the job a live worker still owns. **Operational note:** because a
pre-lease (old-code) worker writes no lease, restart the local worker onto this
code *before* starting a cloud worker beside it, or the cloud worker's recovery
will treat the local job as unleased.

**Live-verified (2026-09-21, ~$0.91):** an interior unit (skip=746) of a real
Gulfraz NTSC segment was restored on a rented RTX PRO 6000 from a corrected-fps
slice → valid 1920×1440 10-bit HEVC BT.709 59.94 fps, and **51.97 dB avg / 49.88
dB min PSNR against the local-restored same unit** (0/750 frames < 25 dB): same
frames, not shifted (the old fps=50 bug would have shifted ~148 frames → ~10–15
dB). The guaranteed-teardown wrapper left 0 pods; the reaper confirmed clean. The
cloud path is now proven end-to-end for NTSC.

**Not yet built:** no per-job or in-GUI choice of executor (it is a worker-wide
env var), and no automatic local↔cloud placement or cost-based scheduling. A full
multi-pod paid fan-out has been proven per-unit but not yet run end-to-end for a
whole chapter.

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

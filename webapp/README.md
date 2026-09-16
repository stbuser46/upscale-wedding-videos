# Chapter restoration web app

This is the private chapter-based interface described in
[`docs/WEB_UI_SPEC.md`](../docs/WEB_UI_SPEC.md). It inventories the two fixed
DVD ISOs, serves lightweight chapter review media, stores selections in SQLite,
and runs the existing restoration pipeline through one host worker.

Phase 1 and Phase 2 are implemented, and restoration now runs as durable
~750-frame units (see "Progress, ETA, and worker health" below). Interactive
pause/resume is still deliberately unavailable: cancellation takes effect only
after the current `pipeline_v3.sh` unit/stage finishes. It has restored the
complete Yacoob & Aysha wedding (all 22 chapters, both DVDs) on the local GPU.

**Cloud fan-out** (`webapp/cloud/`) lets the worker restore a chapter's units in
parallel on rented RunPod GPUs. It is opt-in and dormant by default: local-GPU
operation through the GUI is unchanged unless the worker is started with
`WEDDING_EXECUTOR=cloud` (see "Cloud fan-out" below and `docs/ARCHITECTURE.md`).

## Folder layout

| Path | Responsibility |
| --- | --- |
| `config/` | Typed environment-backed paths, image names, bind address, and safeguards. |
| `db/` | SQLite connection policy, ordered migrations, and schema initialization. |
| `scan/` | Reproducible `lsdvd`/`libdvdread` image, ISO catalog normalization, title extraction, thumbnails, and H.264/AAC proxies. |
| `server/` | Flask application, password sessions, CSRF checks, API, templates, and static browser assets. |
| `worker/` | Single-GPU queue claimant, host lock, pipeline integration, structured events, output validation, and restart recovery. |
| `data/` | Gitignored runtime state only: database, raw scans, title caches, review media, logs, restoration work, outputs, controls, and the worker lock. |

The old `../webui/` comparison/voting application is separate and unchanged.
New runtime files must not be placed in `source/`, `out/`, or the legacy
`work/` directory.

## Initial setup

Run commands from the repository root. Python 3.14 is supported.

```bash
uv venv --seed webapp/.venv
webapp/.venv/bin/pip install -r webapp/requirements.txt
./webapp/scan/build_dvdtools.sh
webapp/.venv/bin/python -m webapp.db.init_db
```

On a host where `python3 -m venv` includes `ensurepip`, it can replace the first
command. This host needed `uv --seed` because its system Python omits
`ensurepip`.

Inventory both fixed ISO names read-only and generate all chapter review media:

```bash
webapp/.venv/bin/python -m webapp.scan.catalog --scan
webapp/.venv/bin/python -m webapp.scan.catalog --proxies
```

`--disc dvd1` or `--disc dvd2` limits proxy generation. `--limit N` is useful
for a smoke test, and `--force` regenerates review assets. Those switches never
accept a source or output path. `lsdvd` sees `source/` through a read-only Docker
mount. DVD titles are copied via libdvdnav into `webapp/data/title_sources/`
before seeking, so chapter times follow DVD navigation metadata rather than raw
ISO byte offsets.

## Start the server and worker

The password is mandatory and is never stored in source code:

```bash
export WEBAPP_PASSWORD='choose-a-long-private-password'
webapp/.venv/bin/python -m webapp.server.app
```

The development server binds only to `127.0.0.1:8093`. For a long-running
instance, use Gunicorn with one web process (SQLite remains the shared durable
state):

```bash
export WEBAPP_PASSWORD='choose-a-long-private-password'
webapp/.venv/bin/gunicorn --workers 1 --bind 127.0.0.1:8093 \
  'webapp.server.app:create_app()'
```

Start exactly one worker in another terminal:

```bash
webapp/.venv/bin/python -m webapp.worker.runner
```

The worker holds `webapp/data/worker/gpu.lock` for its lifetime. A second worker
exits instead of competing for the GPU. Jobs are queued but do not run until
the user presses **Start**, unless `WEBAPP_AUTO_START_JOBS=1` is configured.
`--once` processes at most one started job and is useful for verification.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `WEBAPP_PASSWORD` | none | Required single-user login password. |
| `WEBAPP_SECRET_KEY` | generated | Optional session key. Without it, a mode-0600 key is persisted in `webapp/data/`. |
| `WEBAPP_DATA_DIR` | `webapp/data` | Runtime root. It must remain inside this project for pipeline Docker mounts. |
| `WEBAPP_FFMPEG_IMAGE` | `linuxserver/ffmpeg:latest` | Container used for proxies and validation. |
| `WEBAPP_DVDTOOLS_IMAGE` | `wedding-dvdtools:latest` | Local `lsdvd`/libdvdread/MPlayer image. |
| `WEBAPP_FREE_SPACE_RESERVE_GIB` | `100` | Worker refuses a stage when free space is below this reserve. |
| `WEBAPP_AUTO_START_JOBS` | `0` | Start newly queued jobs automatically when set to a true value. |
| `SKIP_BASELINE` | `0` | Set in the worker's environment to skip the CPU-only baseline encode (see below). Passed straight through to `pipeline_v3.sh`. |

The bind address and port are intentionally fixed at `127.0.0.1:8093`. Put a
private authenticated reverse proxy or Tailscale in front; do not expose the
development server publicly.

### Skipping the baseline encode for production runs

Each job's stage 2 produces a non-AI comparison encode that costs ~30 CPU
minutes and leaves the GPU idle, and it never forms part of the restored
output. To reclaim that idle time on long unattended runs, start the worker with
`SKIP_BASELINE=1` in its environment — the worker forwards its environment to
`pipeline_v3.sh`, which then jumps straight from deinterlacing to restoration.
The restored result is byte-identical; only the throwaway comparison file is
omitted. A worker restart is required to change this, and is cheapest to do
while the current job is still in its restore warm-up (zero frames done).

### Progress, ETA, and worker health during durable-unit runs

In durable-unit mode the frame counter advances in whole-unit steps (750
frames) each time a unit is validated on disk — it is a count of frames that
are safely restored, not a live tail of the GPU. The job's fps and ETA are
recomputed from measured unit throughput after every unit; a new run seeds a
conservative ETA (0.71 fps planning rate) as soon as its first unit starts, so
the queue never shows "ETA pending" for a running durable job. Jobs that have
not started yet show "Est. restore ~H:MM" instead — the remaining frames at
the measured planning rate (`PLANNING_FPS` in `webapp/server/services.py`) —
so the GPU cost of each queued chapter is visible before it runs.

On resume, the worker re-probes every previously completed unit (one
containerized ffprobe each) before restoring anything new. It heartbeats
through that loop with a "validating unit N/M" detail, so a long resume shows
as working in the header pill instead of falsely reporting the worker down.

DVD chapters do not always cut on exact frame boundaries, so a chapter's
deinterlaced source can hold a few frames fewer than the catalog's timestamp
arithmetic predicts. If the final unit comes up short by 50 frames or less,
the worker accepts the frames that actually exist and shrinks the job's frame
total to match, rather than failing the whole chapter at 99%.

Stage 3 also persists its torch.compile cache across unit containers (see
`docs/CURRENT_PIPELINE.md`, 2026-09-08 update), which removes ~2–3 minutes of
recompilation per unit.

### Library and queue status

The library and per-DVD pages mark which chapters are already restored (a green
badge and a restored/total count per disc), so finished work is not accidentally
re-queued; queueing an already-restored chapter asks for confirmation first.
Chapters can be sorted shortest- or longest-first (each card shows an estimated
GPU-hours cost), and the queue page can hide finished and cancelled jobs.

## Operational behavior

- The browser never supplies a filesystem path, tag, image name, or shell
  fragment. Public job IDs and all paths are generated by the server.
- Only artifacts registered as valid in SQLite can be served. Flask conditional
  responses provide HTTP Range support for playback and downloads.
- SQLite uses WAL mode. Job state transitions, progress, metrics, and log lines
  are structured append-only events.
- Pipeline stages use `.partial` files followed by same-filesystem atomic
  renames. The worker validates duration, exact frame count, 10-bit HEVC, and
  48 kHz FLAC before completing a job.
- On worker startup, an active job is recorded as interrupted and requeued so
  completed pipeline stages can be reused. A pending cancellation becomes
  cancelled during recovery.
- A running cancellation writes a fixed worker control file. The current stage
  is allowed to finish, then `pipeline_v3.sh` exits at the boundary and releases
  GPU memory. There is no pause command in Phase 2.

## Cloud fan-out

`webapp/cloud/` can restore durable units on rented RunPod GPUs. Requirements: a
RunPod API key in `webapp/data/runpod.env` and an SSH key under `cloud/keys/`
(both gitignored).

> **Money-safety, read before enabling.** Pods bill until deleted. Teardown is
> automatic only on a graceful worker exit/restart; a hard kill (SIGKILL, OOM,
> power loss) can leave up to `WEDDING_CLOUD_MAX_SLOTS` pods billing until you
> run the reaper by hand. Before setting `WEDDING_EXECUTOR=cloud`, set a RunPod
> **account spend limit** and/or schedule `python -m webapp.cloud.reaper --yes`
> on a timer. The cloud path is also not yet live-verified end to end.

### Worker fleet mode (opt-in)

Start the worker with `WEDDING_EXECUTOR=cloud` and it dispatches a chapter's
durable units across a concurrent pod fleet instead of the local card. This is a
**worker-wide, per-process** choice — there is no per-job toggle in the GUI, and
with the default (`local`) the cloud code never runs. Tuning:

| Variable | Default | Meaning |
| --- | --- | --- |
| `WEDDING_EXECUTOR` | `local` | Set to `cloud` to run durable units on RunPod pods. |
| `WEDDING_CLOUD_MAX_SLOTS` | `16` | Pods brought up in parallel. |
| `WEDDING_CLOUD_SPEND_CAP_USD` | `250` | Fleet stops launching pods past this estimated spend (checked at bring-up only). |
| `WEDDING_CLOUD_TIER` | `SECURE` | RunPod cloud tier. |
| `WEDDING_CLOUD_GPU_PREFERENCE` | — | Preferred `gpuTypeId`s, in order. |

The queue page shows a read-only **Cloud fleet** panel (live pods, uptime, and
running spend) while cloud jobs run; it stays hidden for local-only operation.

### Standalone pod CLI (testing)

The CLI drives a single pod without touching the catalog database or worker, so
the cloud path can be exercised for a couple of dollars in isolation.

```bash
# Show GPU offers and current stock, then rent, run one unit, and tear down.
webapp/.venv/bin/python -m webapp.cloud.cli offers
webapp/.venv/bin/python -m webapp.cloud.cli up --gpu "RTX PRO 6000"
webapp/.venv/bin/python -m webapp.cloud.cli unit <pod> \
  --input slice.mkv --output unit.mkv --skip 0 --cap 754 --prepend 0 --drop 4
webapp/.venv/bin/python -m webapp.cloud.cli status
webapp/.venv/bin/python -m webapp.cloud.cli down --all
```

Every pod is recorded in `webapp/data/cloud_pods.json` before the create call
returns, and teardown never depends on a clean exit. Because a running pod bills
real money, there is a database-independent kill switch that terminates anything
carrying this project's `wedding-` pod-name prefix:

```bash
webapp/.venv/bin/python -m webapp.cloud.reaper --dry-run   # list what we own
webapp/.venv/bin/python -m webapp.cloud.reaper --yes       # terminate them all
```

The unit argv is shared with the local pipeline via `lib/seedvr2_unit_args.sh`,
and `scripts/test_slice_equivalence.sh` proves (CPU-only) that a stream-copy
slice feeds SeedVR2 byte-identically to a whole-file read. Every pod is written
to the `cloud_pods` ledger before creation and terminated on teardown; the
reaper above is the money-safety backstop for both modes.

## systemd and Docker later

No production unit or web-container manifest is shipped in this phase. For
systemd, create two services with the repository as `WorkingDirectory`: one
running the Gunicorn command above and one running `webapp.worker.runner`.
Load secrets from a root-readable `EnvironmentFile`, run both as the project
owner, set `Restart=on-failure`, and order the worker after Docker. The worker's
file lock still enforces one instance if systemd starts it twice.

If the Flask server is containerized later, mount only `webapp/data/` and expose
8093 to loopback/private networking. Do **not** mount the Docker socket into the
web container. Keep the GPU worker as a narrowly scoped host service, because
it alone invokes the fixed Docker commands and `pipeline_v3.sh`. A future image
must run migrations before serving and use the same persistent data mount;
this repository does not claim that deployment is implemented yet.

# Idle-only unattended restoration and durable pause/resume plan

Status: proposed design, documented on 2026-08-29. No part of this plan is
implemented yet.

## Objective

Allow the wedding restoration queue to run unattended whenever the NAS is
genuinely idle, automatically cover every non-skipped chapter that has not
already been restored, and yield the CPU, GPU and storage to interactive server
work without losing completed restoration work.

The approved SeedVR2 restoration settings and output quality are the baseline.
Idle scheduling must not silently select a smaller model, reduce resolution,
discard temporal context, or change the archival output format.

## Current implementation boundary

The database-backed application under `webapp/` is the correct foundation. It
already provides:

- a SQLite WAL catalog and restoration queue;
- transactionally claimed jobs and a single host GPU lock;
- job priorities, stage progress, events, metrics and output validation;
- canonical `pause_requested`, `paused` and `resuming` job states;
- a `job_chunks` table reserved for future durable restoration units;
- placeholder pause and resume API routes;
- major-stage reuse after a failure or worker restart.

It does not yet provide durable restoration units. SeedVR2 currently processes
many internal 750-frame chunks inside one long container and one open video
writer. The database only observes their progress; it has no independently
valid output to resume from. Freezing that process with `SIGSTOP` or Docker
pause would retain about 60 GiB of VRAM and is not an acceptable pause
mechanism.

The current unattended operation also needs repair before more automation is
enabled:

- the web server and worker are launched by an `@reboot` cron script rather
  than supervised system services;
- the GPU worker is currently stopped after SQLite-open errors following a
  reboot, while the web server remained available;
- the worker has no durable health signal or automatic recovery from a
  transient database failure;
- `WEBAPP_AUTO_START_JOBS=1` starts eligible jobs without checking whether the
  server is idle.

## NAS inventory snapshot

Read-only inspection on 2026-08-29 found:

| Item | Value |
| --- | ---: |
| Catalogued chapters | 22 |
| Restored chapters | 10 |
| Remaining non-skipped chapters | 12 |
| Remaining source duration | 11,241.760 seconds (about 187 minutes) |
| Remaining output frames | 562,088 |
| Estimated SeedVR2 time at 0.85 fps | 183.7 GPU-hours |
| Continuous running equivalent | about 7.7 days |

At eight idle GPU-hours per day, the backlog would take approximately 23 days,
before allowing for model startup, preparation, assembly and pauses.

DVD 1 Chapter 7 is queued and reports 5,950 restored frames, but no durable
chunk, intermediate restoration file or final output exists. That counter is
stale observational progress and those frames must be recomputed when the new
engine is introduced.

## Design overview

```text
catalog reconciler -> queued backlog -> idle gate -> preparation
                                           |
                                           v
                              durable SeedVR2 units
                               |       |       |
                            unit 1  unit 2  unit N
                               \       |       /
                                validated assembly -> audio mux -> final validation

busy signal -> pause request or urgent yield -> release VRAM -> wait for idle -> resume
```

There remains exactly one queue owner and one GPU restoration at a time. The
web process records commands; only the host worker starts fixed pipeline and
container commands.

## 1. Supervision and worker reliability

Replace the reboot cron entry with two explicit systemd services:

- a web service running the existing Gunicorn entry point;
- a restoration worker service ordered after the writable project filesystem
  and Docker, with restart-on-failure and a bounded restart delay.

The worker must tolerate a temporary SQLite error with retry and exponential
backoff. A database write failure while a restoration container is active must
not orphan the container. The worker must retain the container identity, retry
the state write, and either regain control or terminate the active partial unit
in a `finally` path.

Service hardening and priority should include:

- a dedicated low-priority systemd slice;
- low CPU weight and idle or low I/O scheduling priority;
- explicit working directory, user and environment file;
- no unrestricted Docker socket exposure to the web server;
- a visible worker heartbeat and last-error field in the UI;
- journal logging in addition to per-job pipeline logs.

The existing file lock remains a second defence against two workers owning the
GPU.

## 2. Automatic backlog reconciliation

Automatic mode periodically finds each chapter that:

- is not marked `skip`;
- has no completed chapter restoration;
- has no active or queued chapter job.

It then creates one normal queue job through the same validated server-side
path used by the UI. Database uniqueness remains the final deduplication
guard. Completed chapters are never overwritten, custom slices are not
automatically created, and a completed chapter is not made incomplete by a
later failed or cancelled duplicate job.

The current catalog would yield 12 unfinished chapters, including the existing
queued Chapter 7 job rather than duplicating it.

Transient failures may use a configurable retry limit and exponential backoff.
Validation failures, bad source media and repeated failures remain stopped for
manual review so unattended mode cannot enter an infinite retry loop.

Recommended initial ordering is the existing priority first, then queue
position. A later option may choose shortest-first within equal priority, but
that is not required for pause/resume.

## 3. Idle detection and GPU ownership

An idle decision must use hysteresis and several signals. GPU utilization alone
is unsafe: on the inspected NAS, an Ollama `llama-server` retained 56,014 MiB
of the 97,887 MiB GPU while utilization sometimes read zero. The proven
SeedVR2 configuration peaks at about 60 GiB, so the two workloads cannot safely
coexist.

Proposed initial start conditions, all continuously true for ten minutes:

- no foreign GPU compute process;
- at least 70 GiB of free VRAM;
- GPU utilization below 5%, excluding no process because the wedding worker
  has not started yet;
- low external CPU utilization and normalized load;
- no sustained Linux CPU, memory or I/O pressure;
- sufficient disk space above the existing configured reserve.

All thresholds and dwell times must be configurable. The UI should report the
actual blocking reason, for example `waiting: foreign GPU process` or
`waiting: only 41 GiB VRAM free`.

Once restoration starts, the monitor must distinguish the worker's container
PID/cgroup from foreign work. Otherwise SeedVR2's own high utilization would
immediately pause itself. CPU accounting should similarly subtract or identify
the low-priority restoration cgroup.

Recommended hysteresis:

- idle for ten minutes before starting or automatically resuming;
- sample active work every one to two seconds;
- after a busy period, require another five to ten idle minutes before resume;
- never repeatedly start and stop around a single threshold.

CPU and I/O work can be made genuinely low priority with cgroup weights. GPU
compute has no equivalent safe preemption mechanism here, so it must be
released by ending the SeedVR2 process.

### Coordinated GPU hand-off

Passive process monitoring can minimize interference but cannot guarantee that
a large interactive model will not begin allocating VRAM before the monitor
reacts. The robust design adds a local GPU hand-off operation:

1. A known GPU application or wrapper requests `yield GPU`.
2. The worker terminates only the current partial restoration unit and releases
   the restoration container.
3. The requester receives confirmation that the wedding workload has released
   VRAM, then starts its GPU work.
4. Restoration resumes only after the requester is gone and the idle dwell time
   passes again.

Ollama/Open WebUI is the first integration candidate on this NAS. The safest
default is never to kill or unload an unrelated workload without an explicit
integration policy. Ollama must also be configured to release an idle model;
otherwise its retained 56 GiB means the idle gate will correctly wait forever.

Unknown GPU programs remain covered by fast passive detection and an urgent
yield, but this fallback cannot offer the same allocation-before-start
guarantee as a coordinated hand-off.

## 4. Durable SeedVR2 units

The first implementation should retain the currently proven 750-new-frame
unit, 129-frame temporal batch, four-frame overlap and first-unit warm-up. This
keeps the accepted restoration profile unchanged.

Unit boundaries are frame-based against the prepared exact 50 fps FFV1 source:

- unit 1 processes 750 new frames with four reversed warm-up frames and writes
  only the 750 requested outputs;
- every later unit processes the previous four raw context frames plus up to
  750 new frames, uses no first-frame warm-up, and discards the four context
  outputs;
- the tail unit records its exact requested and actual frame counts.

The pinned upstream CLI already implements frame skipping, load caps, temporal
overlap and a generator that yields context-trimmed results after every
internal chunk. The maintained local image patch should extend that seam to:

1. open a new video writer for each yielded unit;
2. write to a job-scoped `.partial` path;
3. close and validate the independent video;
4. atomically rename it;
5. emit a structured completion event;
6. check the pause/yield control before beginning another unit.

Keeping one SeedVR2 process alive while the system stays idle preserves the
compiled VAE and model cache across units. Launching a completely new container
for every 750 frames is a simpler fallback, but it would repeatedly pay model
load and compilation costs and should only be selected if benchmarking shows
that overhead is acceptable.

Each `job_chunks` row should durably record at least:

- sequence number and source frame range;
- context and warm-up frame counts;
- settings fingerprint;
- `pending`, `running`, `valid` or `invalid` state;
- output-relative path;
- exact frame count and duration;
- file size and SHA-256 checksum;
- attempt count and timestamps;
- validation details or last error.

Only a closed, probed and checksummed file may become `valid`. Existing valid
units are immutable during resume. The current unit's partial output may be
discarded and recomputed without affecting earlier units.

The 750-frame setting gives roughly a 15-minute cooperative pause latency at
the measured throughput. If that is too long, 250 or 375 new frames are useful
future candidates because they remain aligned to the 125-new-frame temporal
step. A smaller size must pass quality, seam, throughput and memory tests before
becoming a default.

## 5. Pause, urgent yield, resume and cancellation

Manual cooperative pause:

1. API changes a running job to `pause_requested`.
2. Worker finishes, validates and records the current unit.
3. SeedVR2 exits and releases VRAM.
4. Job becomes `paused` with a manual pause reason.
5. It remains paused until an explicit resume.

Automatic idle pause follows the same safe path, but records an idle-policy
reason and becomes eligible for automatic resume after the idle dwell time.

Urgent yield is deliberately different. It terminates the active container,
removes or invalidates only the current `.partial` unit, and releases VRAM
within seconds. That unit is recomputed later. Urgent yield is appropriate for
a coordinated GPU request or explicit `Yield now` action; it must not corrupt
or delete previously validated units.

Cancellation stops after the current unit by default and preserves validated
units until an explicit cleanup decision. Force cancellation is a separate,
confirmed action using the same safe termination path as urgent yield.

Resume always:

1. validates the settings fingerprint;
2. checks registered unit paths, checksums and media metadata;
3. marks a missing or corrupt unit invalid;
4. begins at the first non-valid sequence;
5. never recomputes an earlier valid unit.

## 6. Restart recovery

On worker startup:

- a manually paused job remains paused;
- an automatically paused job remains paused until the idle gate passes;
- `running`, `pause_requested` or `resuming` jobs become interrupted while
  their unit inventory is audited;
- valid units remain valid;
- a `running` row without a valid final file returns to `pending`;
- orphan `.partial` files are quarantined or removed only after their job and
  path have been resolved safely;
- the job becomes queued for idle resume or failed with a clear validation
  error.

Recovery must not trust `frames_done` on its own. Progress is derived from the
sum of validated durable unit frame counts.

## 7. Final assembly

After all units validate:

1. construct a generated concat manifest containing only registered job unit
   paths;
2. concatenate in strict sequence without another lossy video encode;
3. perform the existing sample-accurate FLAC audio decode/trim/mux once;
4. write the final MKV through a same-filesystem partial path and atomic rename;
5. validate duration, exact frame count, HEVC Main 10 format, limited-range
   BT.709 tags and 48 kHz FLAC;
6. register the final artifact and complete the job.

Codec parameters and timestamps must be proven compatible with stream-copy
concatenation. If independent MP4 segments cannot be safely concatenated, the
unit container or writer format must change; the restored frames must not be
lossily re-encoded merely to make assembly convenient.

Preparation files and resumable units remain available through final
verification. Existing archival sources and completed outputs are never
deleted automatically.

## 8. API and UI changes

Enable the existing fixed routes:

- `POST /api/jobs/{id}/pause`;
- `POST /api/jobs/{id}/resume`;
- `POST /api/jobs/{id}/cancel`.

Add fixed-schema operations for:

- global automatic idle mode on/off;
- backlog reconciliation status;
- urgent local GPU yield;
- worker health and idle-gate status.

The queue should show:

- waiting/idle/busy reason;
- manual versus automatic pause reason;
- `pausing after current unit` rather than implying an instant pause;
- completed units and total units;
- current-unit progress, durable frames and ETA;
- last worker heartbeat;
- next automatic retry time;
- free disk and required VRAM.

The browser continues to send identifiers and fixed commands only. It never
supplies filesystem paths, container images or shell fragments.

## 9. Verification and rollout

### Automated tests

- backlog reconciliation produces no duplicate jobs;
- state transitions reject invalid pause, resume and cancel commands;
- manual pause never automatically resumes;
- automatic pause resumes only after the idle gate dwell time;
- progress is rebuilt from valid unit rows, not stale counters;
- missing, truncated or checksum-mismatched units are regenerated selectively;
- database failures retry and cannot orphan a container;
- path validation prevents a unit or concat manifest escaping the data tree;
- free-space reserve is checked before preparation, every unit and assembly.

### GPU acceptance test

Use an already approved representative sample and compare the existing
monolithic pipeline with the durable-unit pipeline:

- exact requested duration and frame count;
- codec, pixel format and colour tags;
- decoded PSNR/SSIM and targeted frame comparisons;
- inspection around every 125-frame batch and durable-unit join;
- no repeated or missing context frames;
- no first-frame cold-start artifact;
- no material quality regression from independent unit encoding;
- acceptable steady-state throughput and flat VRAM use.

Then test:

1. pause after one unit and confirm VRAM is released;
2. resume and confirm completed checksums and modification times do not change;
3. restart during a unit and regenerate only that unit;
4. corrupt one completed unit and regenerate only that unit;
5. trigger a coordinated GPU hand-off and confirm release within the agreed
   deadline;
6. run one short canary chapter end to end;
7. enable automatic backlog filling only after the canary output is approved.

Before applying database migrations or replacing cron, create a SQLite online
backup and record the current service/cron configuration. Deployment should be
reversible without touching source ISOs or completed outputs.

## Recommended implementation order

1. Worker supervision, database retry and health reporting.
2. Durable unit writer and validation behind a disabled feature flag.
3. Pause/resume/restart recovery and final assembly.
4. Quality and failure-injection acceptance tests.
5. Backlog reconciliation.
6. Idle gate, low-priority cgroups and UI status.
7. Coordinated Ollama/Open WebUI GPU hand-off.
8. One canary chapter, followed by opt-in unattended processing of the backlog.

## Decisions to confirm before implementation

1. Whether Ollama/Open WebUI is the main interactive GPU consumer and may be
   configured to release idle VRAM and use the local hand-off mechanism.
2. Whether a cooperative pause latency of about 15 minutes is acceptable, with
   urgent yield available in seconds, or whether smaller durable units must be
   benchmarked before rollout.
3. Whether automatic backlog filling should include all non-skipped chapters
   or only chapters at selected priorities.
4. The desired idle and resume dwell times; ten minutes idle and five to ten
   minutes after activity are the proposed starting values.

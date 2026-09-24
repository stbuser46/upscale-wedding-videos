# Needs fixing

> **Status update (2026-09-24, late evening — main session):**
> - **Item 1 (cloud ETA): FIXED in `runner.py`** — planning fallback now scales
>   by ready-pod count, ETA prefers windowed throughput over recent validated
>   units (40-min window) instead of the stall-polluted run average, and the
>   monolithic metrics tick freezes the countdown when frames aren't advancing.
>   Applies from the next worker process (the live one keeps its in-memory code).
> - **Item 2 (naive timestamps): DONE** — all `cloud_pods` time columns swept
>   and normalized to aware ISO (0 naive rows remain); `spend_so_far()` also
>   hardened to tolerate naive stamps rather than crash the money guard.
> - **Item 3 (cloud panel gaps): left for the UI workstream** (server view
>   files are being actively edited there; not touched from this session).
> - **Item 4 (shared worker_status row): FIXED in `runner.py`** — local worker
>   owns row 1, cloud workers own row 2 (created on demand). UI currently reads
>   row 1 (local); surfacing row 2 is a small server-side follow-up for the UI
>   workstream.
> All 21 dispatch/fleet mock tests green after these changes.

Notes from the 2026-09-24 evening session (webapp UI overhaul while the Gulfraz
cloud + local runs were live). **Items 1 and 4 touch worker code — only fix
them between runs, never while a job is active.**

## 1. Job ETA is wrong for cloud (durable) jobs — main item

Observed on job `restore-c31214965320` (Segment 14): UI showed **ETA 6:44:05**
while the real remaining work at healthy fleet throughput was ~1.5–2 h.

Three separate causes, all in `webapp/worker/runner.py`:

- **Planning fallback ignores parallelism** (`_unit_progress`, ~line 861-866).
  Until the first unit of the *current run* validates, ETA = remaining ÷ 0.71 fps
  — the single-local-GPU planning rate. With 7–8 pods the measured aggregate
  was ~3.2 fps (8 units / 31 min, 21:10–21:41 that evening), so the fallback
  overestimates by ~4×. Cold-start idea: scale the planning rate by the number
  of ready pods.
- **Measured fps is a run-average**, `(frames_done − frames_at_start) ÷
  elapsed_since_run_start`. Any stall (e.g. the RunPod create-500 churn
  21:42–22:00) is baked into the average for the rest of the run, and every
  worker restart resets to the fallback. Better: throughput over a recent
  window of validated units (last ~30–45 min).
- **The idle countdown lies during outages** (~line 541-545): a heartbeat
  subtracts 15 s from eta_seconds per tick regardless of progress, so ETA keeps
  shrinking even when zero pods are restoring. Freeze the countdown when no
  unit is actually in flight.

## 2. Naive vs aware timestamps in `cloud_pods` (root cause of the /live 500)

Some rows carry `terminated_at = 'YYYY-MM-DD HH:MM:SS'` (naive, SQLite
`datetime('now')`) while `created_at` is aware ISO from `utc_now()`.
Subtracting them raised TypeError and 500'd `/api/jobs/<id>/live` — **worked
around 2026-09-24** with `_parse_ts` in `webapp/server/api.py` (naive → UTC),
matching `cloud_views._parse_iso` and fleet.py's own tolerance.

Remaining work: no in-repo writer uses `datetime('now')` today (the naive rows
likely came from ad-hoc shell SQL during incident handling), so (a) keep any
future ad-hoc SQL on `utc_now()`-style stamps, (b) optionally add a one-off
migration normalising existing naive rows.

## 3. Cloud panel small gaps (server view layer, safe to fix anytime)

- `/api/cloud/pod-metrics` (`webapp/server/cloud_views.py`) shows a pod as
  state **"unknown"** when telemetry has it but the ledger row lacks a
  `pod_id` (query filters `pod_id IS NOT NULL`). Map to last-known ledger
  state or drop the tile once terminated.
- `/api/cloud/fleet` derives `active_job` only from *currently billing* pods,
  so the cloud panel's unit strip + per-job spend line vanish between pod
  waves (the queue card still shows units via `/live`). Fall back to the most
  recent active-state job that owns cloud pods.

## 4. `worker_status` is a single row shared by concurrent workers

The local worker and a cloud `--once` worker both beat `worker_status id=1`,
so the header "worker" pill and the heartbeat-derived unit totals are
ambiguous while both run. Needs per-worker rows (or an executor column) —
worker-side change, schedule between runs.

## Already fixed in this session (don't redo)

- Terminated-pod filter (default "Hide terminated") on the fleet panel.
- "Compute now" strip: local GPU + every live pod with state/GPU/CPU/RAM.
- LOCAL/CLOUD badges on panels and active queue cards.
- Pod lifecycle line (created → ready +duration → terminated) and live
  provisioning step on tiles; panel no longer hides between pod waves.
- `/live` 500 fix via `_parse_ts` (see item 2).

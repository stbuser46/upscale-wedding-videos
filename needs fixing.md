# Needs fixing

## Codex round-4 pre-commit review (2026-09-25) — triage

**Fixed before commit:**
- **#6 cloud heartbeat CHECK violation** — `worker_status` had `CHECK(id=1)`, so
  the cloud worker's id=2 heartbeats silently failed (no cloud health signal).
  Migration `009_worker_status_multi.sql` now permits id IN (1,2). Server views
  still read id=1 (local) — surfacing the cloud row (id=2) is a small UI-session
  follow-up.
- **#4 hot-add overshoot** — `request_replacement` now bounds by
  `live + in-flight < max_slots` (was bounding replacement *count* only, which
  let 8 initial + N hot-adds reach ~2×max_slots).

**Accepted / deferred (backstopped, not regressions from today's diff):**
- **#1 unconfirmed-create rows can close without provider proof; #2 a pricing
  API failure lets a pod accrue $0.** Both are money-edge cases already present
  in committed `0f953f4`; they did NOT leak in a full paid day (verified
  `our_pods()` == billing repeatedly). Backstops: the age-filtered reaper timer
  and a **RunPod account-level spend limit (set this in the RunPod console —
  the final guard).** Proper fix: unique per-attempt pod names + never close a
  pid-less row without a successful provider listing/delete; fail closed on
  unknown price. Do before large unattended fan-outs.
- **#3 settle/requeue not atomic** — a unit is removed from `open_units` before
  its requeue/DB write commits; a race window could let `drained()` fire and
  strand a tail retry. Fails RESUMABLY (no money/quality loss — resume re-runs
  it). Fix: one locked open→queued/terminal transition (or a `settling` state
  that still blocks `drained()`).
- **#7 ETA cold-start** doesn't pass `parallelism` on the very first estimate;
  windowed-fps denominator starts at first completion (overstates early). Cosmetic.
- **#8 restored-proxy staleness** — an existing `restored_proxy` row skips a
  newer restoration; verify file existence/freshness (`catalog.py`).

**Verified safe by Codex:** the `apad` mux fix (cannot truncate video, doesn't
bloat audio, video stream-copy unchanged); the warm engine is fully inert with
`WEDDING_POD_ENGINE` unset.



> **Warm-worker canary verdict (2026-09-25, ~$5.4 of pod time):** the paid
> identity program ran on a real RTX PRO 6000. Results:
> - Mechanics PROVEN: one resident process served all requests; upstream logs
>   confirm DiT+VAE reuse; request #2 ran **37% faster** (433 s vs 688 s on
>   the tail shape). First canary attempt also caught a real launch bug
>   (`setsid` without `--wait` orphaned the engine at stdin EOF — fixed).
> - Quality gate **FAILED — WEDDING_POD_ENGINE stays OFF**: decoded hashes
>   differ one-shot-vs-engine on BOTH shapes, including the engine's FIRST
>   request (fresh process), so the divergence is cache-mode code paths
>   (offload defaults etc. — Codex round-3 finding #4 confirmed), not
>   residual state.
> - **The cloud one-shot path is bit-DETERMINISTIC** (identical re-run hash
>   `5119c9…`), so production output is reproducible and bit-identity remains
>   the right acceptance bar.
> **Next step for whoever resumes this:** align the engine's effective
> execution parameters with one-shot mode (start with
> `_parse_offload_device`'s cache_enabled flip: pin
> `--vae_offload_device`/`--dit_offload_device` explicitly in engine argv;
> diff the full effective args/branches), then re-canary (~$1.5). The
> reuse machinery itself needs no further work.

## QUEUED NEXT (user-approved 2026-09-24): warm-worker engine, after disc 1

Keep cloud GPUs busy ~95%+ by eliminating the two remaining engine-internal
idle windows (currently ~15-20% of paid pod time):

1. **Persistent pod-side engine process** (the big one, ~10-15% throughput):
   **IMPLEMENTED behind `WEDDING_POD_ENGINE=1` (2026-09-24), Codex round-3
   adversarial findings all fixed (2026-09-25), awaiting the paid identity
   canary — keep the gate OFF until it passes.**
   `docker/seedvr2-pod/pod_engine.py` (resident service, line protocol over
   one persistent ssh) + `PodEngine`/`restore_unit_via_engine` in
   `webapp/cloud/executor.py` + dispatcher wiring in `runner.py::pod_runner`
   (engine created per pod slot, closed in the runner's `finally`; ANY
   engine-layer failure falls back to the classic one-shot `restore_unit` for
   that unit WITHOUT striking the pod — the classic verdict decides). The
   engine file rides `fleet._provision_pod` ONLY when the worker runs with
   `WEDDING_POD_ENGINE=1` (plumbed via `FleetConfig.pod_engine`) and the pod
   image (`COPY` in `docker/seedvr2-pod/Dockerfile`). With the gate off,
   dispatch AND provisioning are byte-identical to the one-ssh-per-unit path
   (mock-verified).
   **Codex round-3 fixes (2026-09-25):** (1) remote kill barrier — the engine
   runs under `setsid`, reports its pid/PGID at READY, and every abnormal end
   ssh-kills the whole remote group and VERIFIES death before any fallback;
   unverifiable death = status 3 → requeue elsewhere (no attempt burned) +
   retire the pod; (2) the dispatcher's engine path is fully
   exception-proofed (try/except → barrier → classic fallback); (3+4) the
   identity canary now primes with a full-size slice A and compares one-shot
   vs engine for BOTH A and a different-content tail-shaped B, with unique
   output names and a same-remote-pid persistence proof — and must be
   repeated per GPU class; (5) deadline parity — engine attempt + classic
   fallback share ONE 3600 s unit budget (600 s fallback floor); (6) engine
   log persistence moved to a bounded drop-oldest writer thread so a blocked
   log filesystem can never stop abort/stall/timeout enforcement; (7)
   gate-off provisioning no longer ships pod_engine.py; (8) the engine forces
   `--cache_dit` (engine-only) so the DiT is actually resident, with timing +
   "reusing cached" log evidence surfaced by the canary.
   Covered by `webapp/cloud/test_pod_engine.py` (19 real-subprocess protocol/
   barrier/log-writer tests against `webapp/cloud/fake_pod_engine.py`), 5
   engine-mode dispatch tests, and a provisioning-gate test — full suite 46
   green.
   **Acceptance gate before enabling:** run
   `scripts/test_pod_engine_identity.py` against a paid canary pod PER GPU
   CLASS the fleet may rent — it must print PASS (one-shot vs engine
   decoded-framemd5-sha256 identical for both the full-size A and tail-shaped
   B requests, one persistent engine pid). Never run automatically.
   Local-path benefit (same per-unit reload there) remains future work.
2. **Async HEVC writer** (secondary, ~30-60 s/unit): upstream engine patch,
   classified bit-identical by docs/PERF_STUDY_CODEX.md — write chunk N while
   the GPU starts N+1.

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

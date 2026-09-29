# GPU-saturation review — 2026-09-25 (Codex CLI + fresh-context reviewer, independent & convergent)

Commissioned mid-run (disc-2 Seg02) because pod GPUs oscillate 0%↔100% per unit.
Duty cycle today: **~72–86%** (per-unit cold python start + 3B model load 2–4 min,
VAE/HEVC save tail ~40 s, occasional serial slice delivery).
Both reviewers worked independently; their findings agree on root cause and ranking.
Full Codex transcript: session scratchpad `codex_throughput_review.md` (548 KB).

## Agreed priority order

### P1 — Fix warm-engine identity divergence (expected +13–23 pts → ~93–96% duty)
Both reviews independently root-caused the canary failure to the SAME config flip:
- One-shot path: `--chunk_size 0` → upstream sets `runner_cache=None`, forces
  `cache_dit=cache_vae=False` → **no offload** (`inference_cli.py:1677, 878-882`).
- Engine path: `pod_engine.py:113` passes persistent `RUNNER_CACHE`, `:94` forces
  `args.cache_dit=True`, pinned `--cache_vae` becomes live → cache_enabled makes
  `_parse_offload_device("none",…)` return **"cpu"** (`inference_cli.py:257`) and
  both models take the `cache_model=True` load path.
- So the engine's FIRST request already runs a different memory lifecycle
  (CPU offload + cached load) than one-shot — matches the canary diverging on
  request 1; RNG/tiling/residual-state theories ruled out (upstream re-seeds
  per batch, `generation_phases.py:313-318`).

Codex's diagnostic ladder (~$1.5–2, one pod, engine-side flags ONLY — never touch
the shared argv builder in `lib/seedvr2_unit_args.sh`):
1. Engine request with `runner_cache=None`, no forced cache_dit → must equal one-shot
   (proves `handle()` mirrors `main()`).
2. Cache object present but both cache flags suppressed → isolates reusable-context path.
3. Enable VAE/DiT caching independently; do NOT pass offload "none" (upstream converts
   it to CPU when caching is on) — test same-GPU target `"0"` (`inference_cli.py:267`,
   weights stay resident on the 95 GB card).
4. Hash tensors after VAE-encode / DiT / VAE-decode / post-process to find the first
   numerical divergence instead of paying repeated full-HEVC comparisons.
5. Only after request-A parity: re-run the existing two-request persistence canary.
Money-safety: unchanged (round-3 barrier/deadline machinery already mock-tested).
Gate stays OFF until hashes match.

### P2 — Bounded pod-local async HEVC writer (after P1; +3–5 pts → ~98–100%)
Decouple the ~40 s (and ~63 GiB-transient) buffered save from the engine:
bounded frame-block queue to a writer process on the SAME pod, exact rgb24 byte
stream + patched ffmpeg/libx265 args preserved, engine starts request N+1 once
N's frames are queued, unit "complete" only when N's ffmpeg exits, pod cannot
retire until writers/downloads drain. Reject the staging-VM variant (6.2 GB raw
RGB per unit vs ~70 MB compressed download).

### P3 — Pre-baked pod image (no steady-state gain; ~15–25 min + ~$5–6 saved per segment)
Each pod pays apt+pip+7.3 GB weight pull (~20–30 min billed, `fleet.py:842-862`)
every segment. Already flagged in PERF_REVIEW_2026-09-21 ("needs a registry push"),
still undone. Zero identity risk (same bytes, baked). Do before any topology work.

### P4 — Two concurrent one-shot lanes per pod (FALLBACK ONLY if P1 can't reach parity)
Ceiling ≈ the idle fraction (~15–25%), no compute speedup (Amdahl). Blockers:
- Dispatcher: slot model is one-pod-one-runner-for-life (`runner.py:1612`,
  `fleet.py:41-49`, single `current`, blocking `restore_unit`, retire-once in
  `finally`) → needs a pod-supervisor owning two lanes + refcounted retire.
- **Host RAM likely disqualifying**: pods guarantee only 80 GB RAM/GPU; the save
  transient is ~63 GiB → two lanes can exceed 126 GiB. Only on verified ≥160 GiB
  hosts, after a serialized-vs-concurrent identity canary.

## Agreed rejects
- Pre-spawn next one-shot during save: ~4–5% for two-process lifecycle complexity;
  strict subset of P4; redundant once engine works.
- Multi-GPU pods: no per-GPU utilization gain; coarsens retire granularity /
  money-safety; only a provisioning-amortization play that P3 captures cheaper.
- Persistent staging VM: duplicates the existing seed-pod design
  (`start_intermediate_staging`/`slice_from_seed` + framemd5 proofs) for standing
  cost + new failure domain.

## Additional losses found (reviewer)
- **First-wave ramp**: staging upload is bw-capped 3 MB/s (`fleet.py:713`) and fights
  the first wave's home-upload slices for the uplink (~15 min GPU-idle). Fix (M):
  stage the intermediate FIRST at full rate during the ~25-min provisioning window,
  cut all slices DC-side, never home-upload a slice.
- **Straggler tail**: segment wall ends on slowest pod while the single standby
  idles beside it — duplicate the last 1–2 units onto the standby (first valid
  result wins via idempotent `_settle`, `runner.py:1419-1423`); cap the extra spend.
- **Slicer ceiling**: one slicer thread hashes 750 FFV1 frames/unit
  (`runner.py:1484-1487`); fine at 8 pods, silently degrades to home uploads
  near 16 — watch when hot-adding.

## Stacked outcome estimate
48-unit segment: ~175 min today → **~115–125 min** (P1+P2+P3 + staging-first),
duty ~72–86% → ~98% steady-state. Literal 100% is impossible across cold start
and the final partial wave.

*Analysis only — no code modified; live run untouched. Canary ladder + image bake
belong to the session that owns commits (see `needs fixing.md` warm-engine item).*

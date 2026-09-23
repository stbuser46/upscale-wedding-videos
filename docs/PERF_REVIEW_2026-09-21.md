# Performance review — 2026-09-21 (local + cloud)

A read-only follow-up to `PERF_STUDY_CODEX.md` (2026-08-04), grounded in
production logs from the completed Yacoob & Aysha restoration. Question asked:
how can conversions go faster **without changing the restored pixels**.

## Status versus the 2026-08-04 study

Implemented since that study: VAE-only `torch.compile` (1.20x, PSNR-gated),
the persistent Inductor/Triton cache across per-unit containers (+15–18% per
GPU window), and `SKIP_BASELINE` (moot in durable-unit production, which never
reaches the baseline stage). Sustained production throughput: **0.85–0.88
output fps** — ~3 h per 3-min chapter, roughly a week of GPU time per DVD.

## Where a unit's time goes (warm cache, local RTX PRO 6000)

From `webapp/data/logs/restore-494e7bba8009.log`, one 750-frame unit ≈ 812 s:

| Phase | Time | Share |
| --- | ---: | ---: |
| VAE encode (compiled, tiled) | ~162 s | ~20% |
| DiT one-step upscale (not compiled) | ~88 s | ~11% |
| **VAE decode (compiled, tiled)** | **~465 s** | **~57%** |
| LAB/post + HEVC write (GPU idle) | ~41 s | ~5% |
| Container start, model load, first-batch warm-up | ~50 s | ~6% |

The workload is **VAE-decode-bound**, not attention-bound: making the DiT free
would buy only ~11%. The local path is close to its quality-neutral ceiling.

## Local recommendations (stacked ceiling ≈ 1.15–1.3x → ~2.3–2.6 h/chapter)

1. **Retest DiT compile under durable units (~+7%, cheapest).** The leak that
   forced `--compile_dit` off (`COMPILE_LEAK_INVESTIGATION.md`) was
   recompilation across differently-shaped chunks *within one process*. Durable
   units run `--chunk_size 0` — one uniformly-batched 754-frame chunk per
   process, identical for every interior unit — so the trigger no longer exists
   there. Gate exactly like the VAE compile (PSNR + flat-VRAM acceptance run);
   let tail units keep the current path if needed.
2. **Warm persistent worker (~+7–10%, architectural).** Each unit pays container
   teardown/start, CUDA init, 6.5 GB model re-materialise and cache replay
   (~17 s between units + ~25–30 s inside each). A resident engine process
   serving units would reclaim it, at the cost of the per-unit crash isolation.
3. **Overlap the per-unit CPU work (~+3–5%).** The post-unit
   `ffprobe -count_frames` validation and (on resume) the serial re-probe of
   every finished unit run while the GPU idles; run them concurrently with the
   next unit. The engine's buffered HEVC write (~41 s GPU-idle, ~63 GiB RAM
   transient) is bit-identical to overlap per the Codex study, but needs an
   upstream patch.
4. **One-run benchmarks (0–10% each, validate by hash/PSNR):** VAE
   `max-autotune` compile mode (VAE is 83% of AI time, shape-stable, cache
   amortises the longer compile), `--tensor_offload_device none`, newer-torch
   image (2.7.1 is old for Blackwell).

Correction to earlier notes: the idle gate's 600 s dwell does **not** re-arm
between back-to-back jobs (`webapp/worker/idle.py` keeps `_idle_since` across a
job) — it only bites at worker start or after a foreign-GPU blip, so it is not
a meaningful lever.

## Cloud recommendations — the ~10x lever (implemented 2026-09-23)

The fleet runs the identical pinned argv (frame-identical verified 2026-09-21),
so parallelism is quality-neutral by construction. The review found ~15–20% of
paid pod time lost to serialization; fixed by the pipelined dispatch now in
`_run_units_cloud` (see `ARCHITECTURE.md`): fleet provisions during stage-1,
slices pre-cut in the background, per-pod upload/restore/download pipelining,
and in-run unit retry instead of whole-run failure + fleet re-provision.

Still open: benchmarking the pre-baked `seedvr2-pod:v3` image against the
default stock-image provision (apt + pip + 7.3 GB HF weight pull per pod;
needs a registry push), and straggler duplication for the final wave.
Operationally, a local worker and a cloud worker can run side by side for
local+16-pod throughput during a backlog.

## Explicitly excluded (they change the output)

Untiled/larger VAE tiles (5–15% upside on the dominant decode phase, but
changes VAE spatial context/blending), FP8/GGUF checkpoints, SageAttention,
different batch/overlap/prepend, faster x265 preset or NVENC intermediates.
If one supervised experiment is ever allowed, untiled VAE decode is the one
worth a blind A/B.

## Validation gate for anything above

Byte-identical decoded-frame hashes for orchestration-only changes; PSNR in the
approved ~49 dB class plus a flat-VRAM multi-unit run and in-motion blind
review for compile/kernel-class changes (`scripts/test_slice_equivalence.sh`,
`scripts/compare_units.sh` already exist for this).

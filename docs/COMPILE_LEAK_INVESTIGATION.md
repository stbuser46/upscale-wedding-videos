# SeedVR2 `torch.compile` VRAM accumulation

Date: 2026-08-04

Pinned image: `seedvr2-cuda:v3` (`sha256:f93569fe95459a1a450d6e822a2a48aed89eea04b9dc05bd34eacef9a0a5acdc`)

Pinned PyTorch: `2.7.1+cu128`

## Result

The fix is to add `--cache_dit --cache_vae` whenever streaming with
`--compile_dit --compile_vae`. No source or image change is needed. These flags
make the existing single-GPU streaming cache retain and reuse one compiled
runner across all chunks. The models are still offloaded to CPU between phases,
so this does not keep both weight sets resident in VRAM.

The fixed invocation is the immutable `seedvr2-cuda:v3` image plus these four
flags:

```text
--compile_dit --compile_vae --cache_dit --cache_vae
```

A separate `seedvr2-cuda:compile-fix` image was intentionally not created: the
conditional implementation requirement permits documenting a pure CLI fix, and
retagging or wrapping the same image would obscure that the executable code is
unchanged.

GPU acceptance testing is pending. The production GPU was occupied throughout
this investigation and no CUDA workload was launched.

## Root cause

This is retained compiler state caused by repeatedly compiling disposable model
instances, not allocator fragmentation and not CUDA graph pools.

The relevant pinned-image control flow is:

1. `/opt/SeedVR2/inference_cli.py:624-727` processes a chunk, yields it, then
   calls ordinary deep memory cleanup.
2. `/opt/SeedVR2/inference_cli.py:1665-1680` creates a persistent
   `runner_cache` for single-file streaming only when `cache_dit` or `cache_vae`
   is enabled. Both were false in the failed job, so `runner_cache` was `None`.
3. `/opt/SeedVR2/inference_cli.py:868-869` consequently forces both model-cache
   booleans false, and lines 918-954 call `prepare_runner()` for every chunk.
4. `/opt/SeedVR2/src/core/model_configuration.py:1226-1232` wraps each newly
   materialized DiT with `torch.compile`; lines 1261-1270 and 1405-1453 do the
   same to each new VAE encoder and decoder.
5. `/opt/SeedVR2/src/optimization/memory_manager.py:1011-1097` and
   `:1100-1162` move uncached models to CPU and clear their parameter storage,
   but do not reset Dynamo or Inductor. `clear_memory()` at lines 220-358 only
   empties allocator/IPC caches and runs Python GC.

The DiT makes this especially expensive. Every forward creates a short-lived
`Cache` (`/opt/SeedVR2/src/models/dit_3b/nadit.py:190-200`). Window attention
stores generated closures in it
(`/opt/SeedVR2/src/models/dit_3b/nablocks/attention/mmattn.py:187-257`). Those
closures capture large CUDA index tensors from `window_idx()` and
`repeat_concat_idx()` (`/opt/SeedVR2/src/models/dit_3b/na.py:320-424` and
`:616-640`). Dynamo specializes resumed frames around these objects. When a
cache/model instance dies, its guards invalidate, while process-global Dynamo
and Inductor compiled entries survive the application's model teardown. A new
compiled wrapper in a later chunk creates more specializations and associated
CUDA constants instead of reusing the already-warmed compiled module.

The failed log, `webapp/data/logs/restore-4f4e5be8540e.log`, directly confirms
this sequence:

- chunk 1 starts at line 2997 and hits Dynamo's recompile limit at lines
  3166-3169; the reported reason is that local `cache_win` was deallocated;
- chunk 2 starts at line 3371 and rematerializes/rewraps both compiled models;
- chunk 3 starts at line 3732, rematerializes/rewraps them again, hits the same
  guard invalidation at lines 3868-3872, and OOMs at lines 3905-3914;
- the OOM reports 92.55 GiB **allocated** and only 286 MiB reserved but unused.
  `empty_cache()` therefore cannot solve it: almost all memory is referenced,
  not stranded in free allocator blocks.

The compiled backend was `inductor` in `default` mode. CPU-only inspection of
PyTorch in the image showed `torch._inductor.config.triton.cudagraphs == False`;
CUDA graphs are enabled by `reduce-overhead`/`max-autotune`, not this default
mode. CUDA graph private pools are therefore not the holder in this incident.

## Chosen fix and semantics

The existing cache path is complete and is intended for streaming:

- CLI flags exist at `/opt/SeedVR2/inference_cli.py:1478-1485`.
- Stable IDs `cli_dit` and `cli_vae` are selected at lines 921-923.
- The first chunk places each fully configured compiled model in the global
  cache (`generation_phases.py:309-324` for VAE and `:631-646` for DiT).
- Later chunks recover the same runner and model objects
  (`model_configuration.py:634-683`, `:1028-1045`, and `:1101-1113`). The
  compile checks explicitly recognize `_orig_mod` and do not wrap them again.
- Cached models are moved to their configured offload target after each phase.
  With the current `none` setting, cleanup deliberately falls back to CPU
  (`memory_manager.py:1051-1063` and `:1129-1141`).

This preserves compiled-path output semantics. It does not change weights,
dtype, seed resets, batch size, temporal overlap, prepend removal, uniform tail
padding, tiling, color correction, or encoding. Per-forward DiT `Cache` objects
are still newly constructed, and generation context data is cleared before the
next chunk (`inference_cli.py:875-894`). The only retained objects are model and
compiled executable state. Loading the same checkpoint anew versus offloading
and restoring the same immutable weights is byte-equivalent at model input;
the same compiled kernels perform the arithmetic.

Caching also removes repeated checkpoint materialization and makes compilation
cost a one-time job cost. It should therefore improve, not reduce, long-job
throughput.

## Alternatives considered

### `torch._dynamo.reset()` between chunks

Rejected as the primary fix. It would preserve fresh-runner lifecycle semantics
and likely release the relevant process-global cache entries if followed by GC
and allocator cleanup, but it deliberately discards compiled code and pays the
full compile/specialization cost on every chunk. It also reaches into private
compiler lifecycle behavior when the application already has a supported reuse
path. It remains a fallback if the GPU acceptance test disproves runner reuse.

### More `empty_cache()`, IPC collection, or allocator tuning

Rejected. These operations already run between chunks. They can return only
unreferenced cached blocks; the OOM showed 92.55 GiB actively allocated and
only 286 MiB reserved-but-free. `expandable_segments:True` was already set.

### CUDA graph pool cleanup

Rejected. Default Inductor mode did not enable CUDA graphs in this image.

### A source patch or replacement image

Rejected for now. Automatically coupling compile and cache flags in the CLI
would be convenient, but it changes code without adding capability. Explicit
flags are smaller, reversible, and keep the production image digest pinned.

## GPU acceptance test

Run from the project root only after the production queue and GPU are empty:

```bash
./scripts/test_compile_fix.sh
```

The script refuses to begin if any catalog job is `preparing`, `running`, or
`assembling`, or if any GPU reports more than 5 GiB in use. It repeats the
preflight immediately before launching CUDA. The catalog is opened with SQLite
`mode=ro`.

It extracts 40 seconds beginning at 5772 seconds from `work/dvd1_title.vob`,
using the same 50p FFV1 preparation and SeedVR2 arguments as `pipeline_v3.sh`.
The 2,000 input frames form three streaming chunks (750 + 750 + 500), and the
restoration uses the four fixed flags above. `nvidia-smi` samples memory and
utilization every 20 seconds to `vram.csv` in a timestamped test directory.

The test fails unless:

- SeedVR2 exits successfully and emits both completion markers;
- the decoded output contains exactly 2,000 frames after first-chunk prepend
  removal and inter-chunk context removal;
- VRAM samples exist for chunks 1 and 3; and
- sampled chunk-3 peak VRAM is no more than 10 GiB above chunk-1 peak.

Expected outcome: three completed chunks, 2,000 output frames, one initial
compile/warm-up sequence followed by runner/model reuse messages, and stable
per-chunk peak VRAM. The test leaves its output and logs under
`work/compile_fix_test_<timestamp>` for review.

To test a separately tagged copy later without editing the script, set
`SEEDVR2_COMPILE_FIX_IMAGE`, for example:

```bash
SEEDVR2_COMPILE_FIX_IMAGE=seedvr2-cuda:compile-fix ./scripts/test_compile_fix.sh
```

## Confidence

Confidence in the static root-cause analysis and CLI wiring is high. Confidence
in the final VRAM bound is medium-high until the required three-chunk GPU test
passes. No claim of production readiness should be made before that acceptance
test.

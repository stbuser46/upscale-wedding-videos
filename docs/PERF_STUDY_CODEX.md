# SeedVR2 performance study: preserve quality, reduce wall time

Date: 2026-08-04

## Executive conclusion

The restoration is not primarily DiT-bound. In the 18-second reference run,
SeedVR2 spends **83.3% of its wall time in the VAE** (24.4% encoding and 58.9%
decoding), only 11.5% in the one-step DiT, 1.8% in LAB/post-processing, and
3.4% in input/output, conversion, cleanup, and other overhead. Attention-only
optimizations therefore cannot transform performance: even making DiT free
would improve the SeedVR2 pass by only 13%.

The best quality-preserving path is:

1. Benchmark the built-in `torch.compile` support on both VAE and DiT, while
   retaining compiled models across streaming chunks. Upstream claims 15–25%
   faster VAE and 20–40% faster DiT. Applied to this measured mix, that implies
   a realistic **1.15–1.25x steady-state SeedVR2 speedup** before other changes.
   This is mathematically the same model but is not guaranteed bit-identical;
   it must pass the validation gate below.
2. Run baseline generation and audio preparation concurrently with the long AI
   pass. This leaves every restored video frame unchanged and hides almost all
   of the reference run's 65.5-second baseline encode, worth about **5% of total
   pipeline wall time**.
3. Pipeline the current CPU conversion/x265 writer with the next GPU chunk and
   remove byte-preserving writer inefficiencies. The first chunk leaves the GPU
   effectively idle for about 34 seconds before chunk 2 starts. This is worth
   roughly **2–3% of SeedVR2 wall time** on long runs.
4. Reuse models, retain small latent tensors on GPU, and avoid decoded-frame
   CPU→GPU→CPU round trips. Together these are likely another **1–2%**, not a
   headline gain.

With no change to the AI arithmetic, a defensible expectation is only
**1.08–1.10x end-to-end** (roughly 20.5–21 minutes instead of about 22.4 minutes
for the reference workflow). If compiled output passes the fidelity gate, the
realistic combined steady-state result is **1.20–1.30x end-to-end**, about
**0.83–0.92 output fps** instead of 0.71 and roughly **7.3–8.0 days** instead of
the current 9.5-day whole-disc extrapolation. The first short compiled run can
be slower because graph compilation is front-loaded; the gain is for long jobs
with compiled objects reused.

A second identical GPU processing independent chapters is the only simple way
to approach 2x throughput without changing the model's arithmetic. It is a
hardware/cost solution, not a single-GPU optimization.

## Scope and evidence

This was an analysis-only investigation. No GPU work was run. No existing file,
pipeline, image, or web application was changed, and nothing was staged or
committed. The image was inspected only with network-disabled, read-only,
CPU-only `docker run` commands.

Evidence inspected:

- `docs/CURRENT_PIPELINE.md`
- `pipeline_v3.sh`
- `docker/seedvr2/Dockerfile`
- `docker/seedvr2/writer-color.patch`
- `docker/seedvr2/streaming-prepend.patch`
- `work/midpoint_v3/seedvr2.log`
- the source inside pinned image `seedvr2-cuda:v3`, corresponding to upstream
  commit `4490bd1f482e026674543386bb2a4d176da245b9`

The image contains PyTorch 2.7.1/CUDA 12.8/cuDNN 9, the 3B FP16 checkpoint, and
the FP16 VAE. At runtime, however, the log says the unified compute dtype is
BF16: VAE weights are converted to BF16; DiT weights remain FP16 while DiT
compute is BF16. That is the current result-defining behavior and should not be
silently changed.

## 1. Where the time goes

### Whole pipeline

The SeedVR2 log provides an exact 1,270.64-second AI duration. The surrounding
FFmpeg stages have no consolidated timing log, so their figures below are
derived from file birth/modify times and the documented approximately
23-minute total. They are accurate enough to identify priorities, but the
audio/mux number is approximate.

| Stage | Reference time | Approx. complete-pipeline share | Evidence |
|---|---:|---:|---|
| Prepare 768×576/50p FFV1 | ~4.9 s | 0.4% | partial-file birth to completed-file mtime |
| Baseline 1920×1440/50p x265 slow | ~65.5 s | 4.9% | partial-file birth to completed-file mtime |
| Container startup before logged AI | ~3.1 s | 0.2% | baseline mtime to first SeedVR2 timestamp |
| SeedVR2 | 1,270.64 s | 94.4% | exact log total |
| Audio decode/FLAC and final mux | approximately 1–2 s on this 18 s sample | ~0.1% | restored-video/log end to original completed mux mtime |
| **Observed total** | **~1,346 s (22:26)** | **100%** | consistent with the documented “approximately 23 minutes” |

The AI pass is therefore the correct primary target. Optimizing deinterlacing
or audio cannot materially change a 9.5-day extrapolation. Baseline overlap is
still worthwhile because it is easy and independent.

### Inside SeedVR2

The two streaming chunks contain 750 new frames, then 150 new + 4 context
frames. Summing the phase totals reported for both chunks gives:

| SeedVR2 work | Time | Share of 1,270.64 s |
|---|---:|---:|
| VAE encode | 309.34 s | 24.35% |
| One-step DiT inference, including load/cleanup | 146.62 s | 11.54% |
| VAE decode | 748.68 s | 58.92% |
| **VAE total** | **1,058.02 s** | **83.27%** |
| LAB/post-processing phase | 23.08 s | 1.82% |
| Read, conversions, x265 write, cleanup, other | 42.92 s | 3.38% |

The first chunk's six VAE decodes are about 92.9 seconds each; the six encodes
are about 38.5–38.8 seconds each; the six DiT calls are about 17.6 seconds each.
The tail chunk pads its 29-frame final batch to 129 frames. Consequently it
computes 258 frames to return 154 (100 padding + 4 batch overlap removed). That
looks wasteful, but removing uniform padding or changing the tail batch changes
the temporal input seen by the model and is not a quality-neutral optimization.
For long material, every full 750-frame chunk is deliberately aligned: after
four prepended frames, 754 frames are exactly covered by six batches with
batch=129, overlap=4, step=125. Only the final partial chunk pays this padding
penalty.

### Observable GPU-idle/host-bound gaps

The log is not a CUDA profiler, so it cannot establish kernel occupancy or find
short idle bubbles inside a CUDA call. It does establish these coarse gaps:

- Startup/input read: 1.95 seconds from the first timestamp to phase 1. The
  OpenCV reader builds a complete CPU float tensor before GPU work starts.
- After chunk 1 post-processing ends at 14:52:22.284, chunk 2 phase 1 does not
  start until 14:52:56.493: **34.21 seconds**. VRAM allocation at the preceding
  checkpoint is only 0.01 GB. Source and timestamps divide the gap into roughly
  2.2 seconds for the final BF16→FP32 CPU result conversion, 12.3 seconds before
  the writer is opened (whole-chunk NumPy multiply/cast), 18.6 seconds in the
  synchronous x265 write, and about a second of cleanup/read/setup.
- The 150-frame tail spends another 6.54 seconds after post-processing in
  conversion, writing, cleanup, and writer finalization.
- Every decoded batch is copied GPU→CPU, then phase 4 copies the decoded sample
  CPU→GPU, copies its matching input CPU→GPU for LAB, and copies the result
  GPU→CPU. LAB itself accounts for only about 2.6 seconds of the 23.1-second
  phase; transfers, normalization, and synchronization dominate the rest.
- Encoded latents are moved GPU→CPU→GPU for DiT; upscaled latents are again
  moved GPU→CPU→GPU for decode. These latent tensors are comparatively small,
  so their measured opportunity is modest.
- Phase barriers are absolute: all batches encode, then all run through DiT,
  then all decode, then all post-process. On one GPU, overlapping these compute
  phases is unlikely to help unless profiling proves unused execution capacity.

The timers use host wall time and do not explicitly call `cuda.synchronize()`.
Phase totals are nonetheless bounded by blocking CPU transfers and are useful;
individual nested timer labels should not be mistaken for an Nsight trace.

### Pipeline-stage serialization

`pipeline_v3.sh` executes preparation (lines 129–150), baseline (152–171),
SeedVR2 (173–205), and audio/mux (207–236) strictly serially.

- The baseline depends on completed FFV1 preparation, but SeedVR2 does **not**
  depend on the baseline. Start both after preparation. Baseline is CPU x265
  work; SeedVR2 is overwhelmingly GPU work. On 32 cores, run the baseline at
  lower CPU/I/O priority or cap its CPU allocation so it does not lengthen VAE
  host work or the SeedVR2 x265-write windows. Its output is independent, so
  scheduling cannot change restored frames.
- Audio depends only on the source VOB and requested interval. Decode/trim/FLAC
  can run concurrently with preparation or AI into a partial file; the final
  operation can stream-copy video and FLAC. With identical FFmpeg arguments,
  the PCM and FLAC content are unchanged. The gain is tiny on the reference
  sample and probably minutes, not hours, on a whole disc.
- Streaming preparation into SeedVR2 could hide the approximately 0.4% prepare
  stage, but complicates atomicity and resumability. Chunk files could preserve
  exact frames; a growing MKV or raw pipe is a poor trade for this project.

## 2. Findings in the pinned inference code

### Built in, but currently disabled

The CLI already exposes the relevant mechanisms:

- `--compile_dit` and `--compile_vae`, with Inductor or CUDA-graphs backends and
  `default`, `reduce-overhead`, or `max-autotune` modes. Both are false in the
  reference log. Triton is installed. VAE compilation wraps the encoder and
  decoder submodules while excluding dynamic `InflatedCausalConv3d` modules.
- `--cache_dit` and `--cache_vae`. Both are false. The second streaming chunk
  therefore creates a new runner and materializes the VAE and DiT again.
- Attention modes `sdpa`, `flash_attn_2`, `flash_attn_3`, `sageattn_2`, and
  `sageattn_3`. The image has neither FlashAttention nor SageAttention, so the
  reference uses SDPA. The SDPA adapter copies cumulative split indices to CPU,
  splits q/k/v into variable-length sequences, and invokes PyTorch SDPA in a
  Python loop. PyTorch may still select a fused SDPA kernel for each call, but
  the adapter is not a fused variable-length attention implementation.
- Tensor offload defaults to CPU. With roughly 40 GB of peak VRAM headroom,
  `--tensor_offload_device none` is an obvious low-risk benchmark for latents,
  although it does not fix the large decoded-frame round trip because decode
  explicitly preallocates its final tensor on CPU on CUDA systems.

There is no explicit CUDA graph capture in the eager reference path. CUDA
graphs are available only through the compile options. The code uses
`@torch.no_grad()` around VAE encode/decode and inference, not a process-wide
`torch.inference_mode()`. cuDNN benchmark and TF32 allowances are already
enabled by the underlying initialization, so simply setting those flags is not
a new opportunity.

### Synchronous I/O and unnecessary copies

Single-GPU streaming is a Python generator. It processes a chunk, yields the
entire CPU tensor, and the caller synchronously converts and writes every frame
before the generator can read or process the next chunk. There is no prefetch,
writer thread/process, pinned-memory queue, non-blocking transfer, or double
buffer.

The FFmpeg path also performs two cancelling channel permutations: the outer
loop converts RGB→BGR, then `FFMPEGVideoWriter.write()` converts BGR→RGB before
writing `rgb24`. It calls `stdin.flush()` for every 1920×1440 frame. Removing
the two permutations and per-frame flush while preserving the exact RGB byte
stream is bit-preserving. More importantly, a bounded CPU writer process can
convert/write chunk N while the GPU starts chunk N+1. RAM use must be bounded:
the current whole-chunk BF16→FP32→NumPy path has a very large transient footprint.

### Tiling and memory

At 1920×1440 with 1024-pixel tiles and 128-pixel overlap, encode and decode each
run four spatial tiles per temporal batch. The implementation uses overlapping
cosine-weighted accumulation. Increasing the tile size or disabling tiling
would reduce repeated border work and Python launches, and upstream explicitly
recommends trying untiled first when VRAM permits. Current peaks—13.25 GB encode,
18.71 GB decode, 55.99 GB DiT on a 94.97 GB device—suggest a larger VAE tile may
fit, but this cannot be inferred safely from linear scaling, and allocator
reservation from DiT leaves only about 34.5 GB shown free during decode.

More importantly, tile size is part of the result: changing crops changes the
VAE's spatial context and blending. It is not bit-identical and is outside the
strict recommendation unless a formal comparison declares it indistinguishable.

### Model/dtype observations

- The sampler already uses the distilled one-step path. There are no redundant
  diffusion steps to remove.
- No BlockSwap is active. That is correct with 96 GB VRAM; enabling it would
  add CPU/GPU traffic and slow the job.
- FP8/GGUF checkpoints would be faster/smaller but change model arithmetic and
  output. They do not meet the requirement.
- Switching BF16 compute to FP16, changing batch size, overlap, uniform padding,
  prepended frames, noise, LAB correction, or chunk boundaries not aligned to
  the 125-frame step can change the restored pixels. None is quality-neutral.

## 3. Fidelity classification

Definitions used here:

- **Bit-identical:** expected to preserve the same model input/output bytes; it
  should still be verified with decoded-frame hashes.
- **Numerically equivalent:** same weights and mathematical operation, but
  floating-point kernel/fusion/reduction order may change low bits.
- **Needs visual validation:** output is intentionally recomputed with different
  spatial/temporal context or an implementation whose error can propagate.
- **Changes quality:** deliberately changes model precision, settings, temporal
  information, or lossy encoding behavior.

| Idea | Fidelity class | Realistic gain | Comment |
|---|---|---:|---|
| Overlap baseline and audio with AI | Bit-identical | ~5% end-to-end | Independent outputs; manage CPU/I/O priority |
| Async exact-byte RGB conversion/x265 writer | Bit-identical | ~2–3% AI | Hides the 34 s inter-chunk host gap |
| Remove RGB↔BGR round trip and per-frame flush | Bit-identical | <1% alone | Preserve identical raw `rgb24` byte stream and FFmpeg command |
| Reuse eager models across aligned chunks | Bit-identical | <1% | Avoids materialize/delete/cleanup; more valuable as compile prerequisite |
| Keep latents on GPU (`tensor_offload=none`) | Bit-identical in principle | likely <1% | Plenty of headroom; hash-test it |
| Keep decoded batch on GPU through LAB, then copy once | Bit-identical in principle | ~1–2% | Requires careful ordering/dtype-preserving refactor |
| Larger chunk that remains a multiple of step=125 | Bit-identical in principle | <1% | Same batch windows; RAM transients are the limiting risk |
| Disable verbose debug after tuning | Bit-identical | negligible–<1% | Current timers do not force CUDA sync; primarily cleaner logs |
| `torch.compile` VAE + DiT, static shapes | Numerically equivalent | 15–25% AI combined | Best candidate; first-run compile cost and graph breaks must be measured |
| CUDA graphs / compile `reduce-overhead` | Numerically equivalent | likely low single digits beyond compile | Static uniform batches are suitable; edge-tile shapes create several graphs |
| Process-wide `torch.inference_mode()` | Numerically equivalent | likely <1–2% | More development risk than `no_grad`; stateful VAE must be checked |
| Newer PyTorch/CUDA/cuDNN with same weights | Numerically equivalent / validate | unknown, plausibly 0–10% | Kernel choices change; use a separate benchmark image |
| FlashAttention 2/3 | Numerically equivalent / validate | perhaps 1–4% total | Only the 11.5% DiT slice is affected; reduction order changes |
| SageAttention 2/3 | Needs visual validation | perhaps 1–5% total | Quantized/approximate attention; not a strict drop-in |
| Larger/no VAE tiles | Needs visual validation | perhaps 5–15% total | Promising because VAE dominates, but tile context/blending changes output |
| Larger temporal batch | Needs visual validation | potentially material | Changes temporal context; may improve quality, but it is a different result |
| Smaller overlap, no uniform padding, different tail batch | Changes quality | tail-dependent | Changes seams/context; disallowed |
| FP8/GGUF, lower resolution/fps, fewer fields | Changes quality | large | Directly violates the requirement |
| Faster x265 preset, lower bitrate, NVENC | Changes quality | only a few % total | Changes the restored intermediate's lossy compression |
| Different color correction/noise/seed/model | Changes quality | variable | Different restoration |
| Second identical GPU for independent chapters | Bit-identical per unchanged job | ~1.8–1.95x throughput | Same job boundaries/settings; high cost, not lower single-job latency |

“Numerically equivalent” is not automatically acceptable under a
non-negotiable-output requirement. Only decoded-frame bit identity is a proof
of identity. If low-bit differences remain, an agreed objective and blind-view
threshold is needed before calling the result indistinguishable.

## 4. Ranked plan by gain and effort

### 1. Compile VAE and DiT and preserve the compiled objects

**Gain:** high; **effort:** medium; **fidelity:** numerically equivalent.

Start with Inductor/default, `dynamic=False`, `fullgraph=False`, both VAE and
DiT compiled. Static 129-frame batches are favorable. The current chunk loop
destroys the runner, so compilation must be paired with model caching/reuse;
otherwise every chunk can pay first-batch compilation again. Benchmark one
complete 750-frame chunk including compile cost, then a second chunk to measure
steady state. Try `max-autotune` only after default is stable; it optimizes long
production runs at the cost of longer compilation.

Using upstream's stated ranges against the measured phase mix:

- VAE 15% faster + DiT 20% faster → about 1.15x AI throughput.
- VAE 25% faster + DiT 40% faster → about 1.25x AI throughput.

These are projections, not measurements on Blackwell. VAE dynamic causal
convolutions are excluded from compile, so the low end is the safer planning
number.

### 2. Overlap the baseline and audio work

**Gain:** medium end-to-end; **effort:** low; **fidelity:** bit-identical.

After FFV1 preparation completes, launch baseline generation at reduced CPU/I/O
priority and start SeedVR2 immediately. Prepare FLAC concurrently in its own
partial file. Wait for all three before final copy-mux/rename. On the reference,
this hides about 65 seconds. On a full disc, the baseline scales to hours but
still finishes far before a roughly 9.5-day AI run.

### 3. Decouple the writer from the GPU chunk loop

**Gain:** medium; **effort:** medium; **fidelity:** bit-identical if the raw byte
stream is preserved.

Use a bounded producer/consumer queue, convert in smaller blocks, remove the
two cancelling channel swaps, and do not flush per frame. Maintain one FFmpeg
process and strict frame order. Start chunk N+1 as soon as chunk N is queued.
This targets the directly observed 34-second gap. Do not change codec, preset,
CRF, pixel conversion filter, or color tags.

### 4. Eliminate avoidable device round trips

**Gain:** low-to-medium; **effort:** medium/high; **fidelity:** bit-identical in
principle.

First benchmark the existing `--tensor_offload_device none` flag. Then, if
profiling justifies code work, keep each decoded sample on GPU through
normalization/LAB and transfer the final batch once to the writer. Keep operation
order and BF16/FP32 conversion points unchanged. The current 56 GB peak leaves
room, but output buffers are large and should be streamed rather than retained.

### 5. Model reuse and aligned chunk sizing

**Gain:** low; **effort:** low/medium; **fidelity:** bit-identical in principle.

Reuse the runner across chunks. If increasing chunk size, keep full chunks a
multiple of 125 new frames so the exact 129/4 temporal windows do not move.
Larger chunks reduce setup frequency but increase a severe CPU RAM transient;
fix the streaming writer first. Model reuse is mostly important because it
makes compile amortization possible.

### 6. Attention backend experiments

**Gain:** low at whole-pipeline level; **effort:** medium; **fidelity:** validate.

FlashAttention is preferable to SageAttention for a strict fidelity trial
because it is not deliberately quantized, but it still changes floating-point
reduction order. DiT is only 11.5% of wall time, and current PyTorch SDPA already
uses fused kernels internally where possible. Do not expect a large overall
gain. SageAttention should remain outside the strict-quality production path.

### 7. Untiled/larger-tile VAE as a separate quality experiment

**Gain:** potentially medium; **effort:** low to test, high to certify;
**fidelity:** needs visual validation.

This attacks the dominant phase and the upstream guidance says to try untiled
first, but it changes VAE context/blending. It must not be folded into a
“quality-neutral” speed release. Test it only as a separately labelled candidate
after the bit/numerical-neutral work.

## 5. Validation gate for any production optimization

1. Establish repeatability by running the unmodified reference twice when the
   GPU is available. Compare decoded video with FFmpeg `framemd5`, audio PCM
   hashes, frame count/timestamps, color metadata, and container duration. If
   the reference is not deterministic, record its natural envelope first.
2. For scheduling/writer/model-cache changes, require identical decoded-frame
   and audio hashes. Container bytes may differ because timestamps/mux metadata
   can differ; decoded essence must not.
3. For compile/CUDA-graph/new-kernel candidates, compare a lossless diagnostic
   before HEVC as well as the final decoded HEVC. Record maximum and mean
   per-channel error, PSNR/SSIM, temporal-difference error, and seam-region
   error. VMAF alone is too insensitive for generated fine detail.
4. Review the existing midpoint material in motion, concentrating on faces,
   jewellery, hair, fine fabrics, continuous zoom, cuts, the first/prepended
   frames, every 125-frame batch join, and every 750-frame chunk join. Use a
   randomized blind A/B or ABX comparison if hashes differ.
5. Measure end-to-end wall time, per-phase time, peak allocated/reserved VRAM,
   peak process RAM, GPU utilization/power, CPU utilization, and disk throughput.
   Use Nsight Systems or the PyTorch profiler on one authorized run; the current
   text log cannot reveal kernel occupancy.
6. Reject any candidate that changes requested frame count, cadence, temporal
   joins, color tags/range, LAB behavior, audio samples, or visibly changes a
   scene. Keep every candidate isolated so gains and fidelity can be attributed.

## 6. Hosted/online alternatives (prices checked 2026-08-04)

### fal.ai SeedVR2

[fal's SeedVR2 video endpoint](https://fal.ai/models/fal-ai/seedvr/upscale/video)
charges **$0.001 per output megapixel-frame**. At 1920×1440 and 50 fps:

`1,920 × 1,440 × 50 × 60 / 1,000,000 × $0.001 = $8.2944 per output minute`.

That is about **$2.49 for the 18-second sample** and **$1,601 for a 3:13:02
disc**, excluding upload/download time. The API exposes target resolution,
seed, and noise scale, but not the pinned checkpoint identity, 129/4 temporal
batching, reversed prep frames, LAB correction, tiled VAE settings, or the same
10-bit HEVC writer. Its documented default noise scale is 0.1 versus the local
0.0. **Quality-match assessment: not a drop-in match; a paid 18-second A/B test
would be required, and exact identity should not be expected.**

### Replicate community SeedVR2

[zsxkib/seedvr2 on Replicate](https://replicate.com/zsxkib/seedvr2) runs on H100
Standard. Replicate lists a typical run as approximately **$0.036 / 24 seconds**,
while its [current hardware tariff](https://replicate.com/pricing) is
**$0.001525 per compute second ($0.0915 per compute minute)**. There is no honest
per-output-minute price without benchmarking this exact 50p workload because
billing follows runtime and runtime varies with input. The wrapper exposes 3B,
one step, seed, and optional wavelet color fix, but not the local LAB path,
129/4 batching, tiled VAE setup, or patched writer. Its page says long clips are
padded/truncated beyond the 121-frame training window, so production material
would need externally controlled chunking. **Quality-match assessment: same
model family, different wrapper and hardware; closer than Topaz, but not the
pinned local result.**

### Topaz cloud

[Topaz's current cloud documentation](https://docs.topazlabs.com/topaz-video/cloud-rendering)
estimates, for SD input at 30 fps, about **31 credits per output minute** for
Starlight models to 1080p. Topaz says price depends on resolution, frame rate,
length, and model; the exact 1920×1440/50p quote is shown in-app. Linear scaling
by frame count alone gives roughly 52 credits/minute at 50p before any 1440p
adjustment. Current subscription credits cost $0.055–$0.125 each and one-time
credits $0.111–$0.250 each, implying approximately **$2.84–$6.46/minute on a
subscription** or **$5.73–$12.92/minute from one-time packs** at that rough
52-credit rate. Starlight jobs are limited to 9,000 frames, about three minutes
at 50p. **Quality-match assessment: no—Topaz Starlight/Proteus/etc. are different
proprietary models with different restoration character. They may be good, but
they cannot preserve the current pinned SeedVR2 output.**

### Overall hosted verdict

No managed endpoint reviewed exposes the full result-defining local stack.
Hosted services may reduce elapsed delivery time through faster or parallel
hardware, but fal and Topaz are costly for three-hour 50p footage, and all three
require quality re-approval. The only credible hosted route to the present
quality is a custom GPU instance running the exact container commit, checkpoints,
patches, arguments, FFmpeg versions, and chunk boundaries. Even then, H100 vs
Blackwell kernel arithmetic may be numerically rather than bit identical, and
large source/intermediate transfers reduce the operational advantage.

## Bottom line

Do not spend effort first on FlashAttention, audio, deinterlacing, or lower
precision. The measured job is a tiled-VAE workload. The practical sequence is
to (1) preserve and reuse compiled VAE/DiT objects, (2) hide independent baseline
and audio work, (3) overlap exact-byte output writing with GPU chunks, and (4)
remove device round trips. Treat compile as a numerically equivalent candidate
that must earn promotion through comparison; treat tile, batch, overlap, dtype,
and model changes as quality changes unless separately re-approved.

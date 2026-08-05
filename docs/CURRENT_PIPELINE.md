# Current restoration pipeline

Status: proven on an 18-second midpoint sample on 2026-08-03.

This document records what was investigated, what was changed, the successful
settings, measured performance, known limitations, and which files matter.

## Objective

Restore PAL wedding DVDs into a cleaner, smoother HD presentation while:

- retaining the correct 4:3 picture;
- preserving all motion represented by the interlaced source;
- avoiding frame-to-frame face and texture flicker;
- preserving colour and legal video levels;
- avoiding another lossy audio generation;
- making long jobs resumable and safe to inspect before committing days of GPU
  time.

The result is a restoration, not a recovery of ground truth. AI can generate a
plausible version of detail lost to DVD resolution and MPEG-2 compression, but
it cannot know exactly what the camera originally captured. The ISO and VOB
files therefore remain the archival sources of truth.

## Source inspected

The main extracted title used during development is `work/dvd1_title.vob`:

- duration: approximately 3:13:02;
- video: PAL MPEG-2, 720x576, 4:3 display aspect ratio;
- pixel aspect ratio: 16:15;
- scan: bottom-field-first interlaced;
- cadence: 25 frames / 50 fields per second;
- audio: 48 kHz stereo AC-3 at 192 kbit/s.

The original disc images are:

- `source/weddind_dvd_1.ISO`
- `source/weddind_dvd_2.ISO`

Keep both ISOs and the extracted VOB. Chapter discovery must use the ISOs
because DVD title/chapter metadata lives in the IFO navigation data and may no
longer be present in a concatenated VOB.

## What was wrong with the earlier approaches

The older scripts and ten-minute outputs remain useful as experiments, but are
not the production path.

### RealESRGAN and CodeFormer pipeline

`pipeline_v2.sh` deinterlaced with `bwdif=send_frame`, producing 25p. A PAL DVD
contains 50 temporal field samples per second, so this discarded half of the
available motion information.

RealESRGAN and CodeFormer then processed still frames independently. Individual
frames could look sharp, but generated texture and faces could change between
adjacent frames, creating shimmer or an artificial appearance in motion.

The face tracker also accumulated stale tracks across a long video. This made
the face pass increasingly expensive and risked associating faces across scene
cuts. The ten-minute job took roughly six hours and produced hundreds of
gigabytes of temporary frames.

### Why a larger still image was not enough

The CodeFormer experiment produced a 3072x2304 file, but resolution alone did
not solve temporal consistency. A smaller, coherent 50p result is preferable
to a larger sequence of independently invented frames.

## Successful v3 design

The production entry point is `pipeline_v3.sh`.

### Stage 1: preserve PAL motion

The source is deinterlaced with bottom-field-first `bwdif=send_field`. This
creates 768x576 square-pixel progressive video at 50 fps and stores it as
lossless FFV1/YUV444.

This is the most important correction relative to v2: both field-time samples
are preserved.

### Stage 2: create an honest baseline

A non-AI 1920x1440/50p, 10-bit HEVC baseline is generated with Lanczos scaling.
Every job therefore has a faithful reference that can be compared with the AI
result in motion.

### Stage 3: temporal AI restoration

The pipeline uses the video-native SeedVR2 3B FP16 model, pinned in
`docker/seedvr2/Dockerfile`.

Production settings on the 96 GB RTX PRO 6000 are:

- output: 1920x1440 at 50 fps;
- model: `seedvr2_ema_3b_fp16.safetensors`;
- temporal batch: 129 frames;
- temporal overlap: 4 frames;
- streaming chunk: 750 new frames;
- four reversed warm-up frames at the start;
- tiled VAE encode/decode;
- LAB colour correction;
- 10-bit HEVC intermediate;
- peak VRAM measured at approximately 56 GB.

The 129-frame setting gives about 2.58 seconds of temporal context. It was both
faster and more coherent than the tested 29- and 81-frame settings on this GPU.

### Local SeedVR2 fixes

Two patches are applied when building the pinned container:

- `docker/seedvr2/writer-color.patch` makes RGB-to-video conversion explicit,
  outputs limited-range BT.709 YUV420P10, and writes the correct colour tags;
- `docker/seedvr2/streaming-prepend.patch` fixes an upstream single-GPU
  streaming issue where four warm-up frames were written into the output
  instead of removed.

Stages write to partial files and rename only after success. Existing completed
stages are reused, so a failed mux does not repeat a multi-hour AI pass.

### Stage 4: audio

The source AC-3 is decoded once into 48 kHz stereo 16-bit FLAC. FLAC does not
undo the loss already present in AC-3, but it avoids introducing another lossy
audio generation and permits sample-accurate trimming.

The audio seek starts five seconds early and is trimmed after decoding. This
avoids beginning on a partial DVD packet. The tested midpoint interval contains
one recoverable damaged AC-3 packet; FFmpeg reports it and continues to produce
a complete 18-second audio stream.

## Completed midpoint reference

The selected interval is 1:36:12-1:36:30 (`start=5772`, `duration=18`). It is
near the middle of the 3:13:02 title and contains a continuous zoom, faces,
hands, jewellery, hair, fabric, fine patterns, and motion.

Command:

```bash
./pipeline_v3.sh work/dvd1_title.vob 5772 18 \
  out/DVD1_midpoint_18s_seedvr2.mkv midpoint_v3
```

Verified restored output:

- exactly 18.000 seconds;
- exactly 900 video frames;
- 1920x1440 at 50 fps;
- HEVC Main 10, YUV420P10;
- limited-range BT.709 with correct transfer, primaries and matrix tags;
- 48 kHz stereo, 16-bit FLAC;
- restored file size: approximately 102 MB.

Performance:

- SeedVR2 processing: 21 minutes 11 seconds;
- complete pipeline including preparation/baseline/mux: approximately 23
  minutes;
- measured SeedVR2 rate: 0.71 output frames per second;
- peak VRAM: approximately 56 GB.

The complete-disc extrapolation was about 9.5 days, which is why the project is
moving to chapter and time-slice selection instead of automatically restoring
an entire DVD.

### 2026-08-05 update: VAE-only torch.compile

The pipeline now compiles the VAE by default (`--compile_vae --cache_vae`),
measured at 1.20x steady-state (about 0.85 output fps, roughly 8 days per
disc, ~3 hours per 3-minute chapter) with 60 GB flat peak VRAM and 49.0 dB
PSNR against the uncompiled reference — visually approved. Compiling the DiT
as well is faster still but recompiles on the differently-shaped tail chunk
and leaks VRAM until multi-chunk jobs OOM; see
`docs/COMPILE_LEAK_INVESTIGATION.md` and the acceptance tests in `scripts/`.
`SEEDVR2_COMPILE=0` restores the original uncompiled behaviour exactly.

## Output inventory

Important files in `out/`:

- `DVD1_midpoint_18s_comparison.mkv`: labelled left/right baseline-versus-AI
  comparison; this is the preferred review file;
- `DVD1_midpoint_18s_seedvr2.mkv`: standalone archival-quality restored sample;
- `*.no_preroll.mkv`: redundant backups made before the safer audio pre-roll;
- `DVD1_midpoint_smoke5s_*`: five-second tuning tests for batch sizes 29, 81
  and 129;
- `YacoobAysha_DVD1_mid10min_cf.mkv`: older 3072x2304/25p CodeFormer test;
- `YacoobAysha_DVD1_mid10min_v2.mkv`: older 1920x1440/25p v2 test.

Important files in `work/midpoint_v3/`:

- `input_50p_ffv1.mkv`: lossless 768x576/50p model input;
- `baseline_1440p50.mkv`: non-AI reference;
- `seedvr2_1440p.mp4`: completed video-only AI intermediate;
- `seedvr2.log`: detailed inference and timing log.

The `work/midpoint_v3/` files make remuxing and investigation possible without
repeating the AI pass. Older `_bench`, `smoke`, `cf_motion` and test directories
are development artefacts, not sources.

## Known limitations and operational rules

- Never delete or overwrite the ISOs as part of a restoration job.
- Review results in motion; still images hide temporal flicker.
- Generated detail is plausible, not historically authoritative.
- The current script resumes whole stages, not individual 15-second SeedVR2
  chunks. True interactive pause/resume requires the worker refactor described
  in `WEB_UI_SPEC.md`.
- Only one restoration job should use the GPU at a time.
- Arbitrary slices must use warm-up/context frames and then trim them so the
  first requested frame is not an AI cold start.
- Output and intermediate files must remain on the same filesystem so final
  renames are atomic.


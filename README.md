# Wedding DVD restoration

## Documentation

- [`docs/CURRENT_PIPELINE.md`](docs/CURRENT_PIPELINE.md) records the source
  analysis, older experiments, final SeedVR2 design, local patches, benchmark,
  output inventory and operational rules.
- [`docs/WEB_UI_SPEC.md`](docs/WEB_UI_SPEC.md) is the specification and phased
  implementation plan for DVD chapter discovery, prioritization, custom time
  slices, queued restoration, cooperative pause/resume, progress/ETA and secure
  remote access.

The next phase is chapter-first. We will scan both original ISOs, review and
prioritize their titles/chapters, and restore selected chapters or time slices.
An automatic complete-disc run is not currently planned.

The quality-first pipeline is `pipeline_v3.sh`. It preserves both temporal
samples from the bottom-field-first PAL DVD (50p), performs restoration with a
video-native SeedVR2 model, and decodes the source AC-3 once into lossless FLAC
for sample-accurate trimming without another lossy audio encode.

It also creates a non-AI 1440p50 baseline beside each job. Always compare the
baseline and restored clips **in motion** before committing to a full disc.

## Midpoint quality test

The source is 3:13:02 long. This test covers 1:36:12–1:36:30, a continuous
mid-film zoom from a group shot into close faces:

```bash
./pipeline_v3.sh work/dvd1_title.vob 5772 18 \
  out/DVD1_midpoint_18s_seedvr2.mkv midpoint_v3
```

The first run builds the CUDA image and downloads the pinned model. Later runs
reuse both. Work stages are resumable; set `FORCE=1` only when intentionally
regenerating a tag.

### Completed reference render

The 1:36:12 midpoint test has been rendered and verified:

- `out/DVD1_midpoint_18s_seedvr2.mkv` — full 1920x1440 restoration
- `out/DVD1_midpoint_18s_comparison.mkv` — labelled side-by-side motion check
- exactly 18.000 seconds / 900 frames / 50 fps
- 10-bit BT.709 HEVC video and 48 kHz stereo 16-bit FLAC audio
- 21m11s of SeedVR2 processing at 0.71 fps; peak VRAM was 56 GB

The source contains one recoverable damaged AC-3 packet in this interval. The
pipeline decodes with a five-second pre-roll, recovers, and produces a complete
18-second FLAC track.

## Quality controls

Defaults favor quality on the 96 GB RTX PRO 6000:

- SeedVR2 3B FP16
- 1920x1440 output at 50 fps
- 129-frame temporal batches with four-frame overlap
- tiled VAE encode/decode
- 10-bit HEVC intermediate and sample-accurate archival FLAC audio
- five-second audio decode pre-roll to avoid partial AC-3 packets on DVD seeks

Environment overrides are available for experiments:

```bash
SEEDVR2_MODEL=seedvr2_ema_7b_fp16.safetensors \
SEEDVR2_BATCH=129 SEEDVR2_RESOLUTION=1440 \
./pipeline_v3.sh input.vob START DURATION output.mkv unique_tag
```

For a long job, use a new tag so previous tests remain intact and resumable.
At the measured speed, the 3:13:02 first disc would take roughly 9.5 days of
continuous GPU time; chapter and slice selection avoids spending that time on
low-priority material.

Keep the original DVD rip permanently. AI restoration can create a cleaner,
more plausible presentation, but it cannot recover factual detail that the DVD
never recorded; the original remains the archival source of truth.

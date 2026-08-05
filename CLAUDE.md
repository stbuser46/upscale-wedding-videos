# Wedding DVD restoration

This project preserves and restores old home DVDs — weddings, birthdays, and other family recordings — using AI upscaling and temporal restoration. It is currently being used to restore PAL wedding DVDs.

## What you can do

- **Run the restoration pipeline** — `pipeline_v3.sh` is the proven quality-first 50p SeedVR2 restoration pipeline that preserves motion and restores temporal quality.
- **Manage conversions remotely** — `webapp/` provides a private web application to catalog DVD chapters, queue restoration jobs, and monitor long-running GPU work from anywhere.
- **Review and validate output** — The webapp shows progress, logs, and completion status; historical blind comparison UI remains in `webui/`.

## Key locations

| Path | What it is |
| --- | --- |
| `source/` | Original ISO images (read-only, never modified) |
| `pipeline_v3.sh` | The production restoration script |
| `webapp/` | Chapter catalog, job queue, authenticated web UI, and GPU worker |
| `webapp/data/` | Generated catalog, media, and job runtime state (gitignored) |
| `docker/` | Pinned CUDA/model containers and upstream patches |
| `models/` | Downloaded AI model weights (gitignored) |
| `docs/` | Pipeline settings, UI specification, and architecture |
| `work/`, `out/` | Legacy pipeline test runs and reference outputs (not managed by webapp) |

## Current status

The webapp implements:

- **Phase 1** — DVD scanner builds a SQLite catalog of all titles and chapters from source ISOs.
- **Phase 2** — Authenticated API and UI for browsing chapters, queueing restoration jobs by chapter or time slice, and monitoring GPU worker progress.

The v3 pipeline preserves both bottom-field-first PAL motion samples as 50p, creates a non-AI 1440p50 baseline, restores temporally with the SeedVR2 3B FP16 model, and muxes sample-accurately trimmed 48 kHz FLAC audio. Its verified 18-second reference is documented in `docs/CURRENT_PIPELINE.md`.

Cancellation is honored only between major pipeline stages; pause/resume is not yet implemented.

## More detail

- [`webapp/README.md`](webapp/README.md) — Setup and operations guide
- [`docs/CURRENT_PIPELINE.md`](docs/CURRENT_PIPELINE.md) — Media analysis, pinned settings, validation, performance
- [`docs/WEB_UI_SPEC.md`](docs/WEB_UI_SPEC.md) — Product and data-model specification
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — Implemented phases mapped to current code

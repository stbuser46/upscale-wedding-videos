# Wedding DVD restoration

This repository preserves and selectively restores two PAL wedding DVDs. The
archival ISOs remain the source of truth; AI output is a cleaner presentation,
not a factual recovery of detail that was never recorded.

## Current entry points

- [`pipeline_v3.sh`](pipeline_v3.sh) is the proven quality-first 50p SeedVR2
  restoration pipeline.
- [`webapp/`](webapp/) is the private chapter catalog and durable restoration
  queue. Phase 1 and Phase 2 are implemented; see its
  [setup and operations guide](webapp/README.md).
- [`docs/CURRENT_PIPELINE.md`](docs/CURRENT_PIPELINE.md) records media analysis,
  pinned settings, validation, performance, and known pipeline limitations.
- [`docs/IDLE_PAUSE_RESUME_PLAN.md`](docs/IDLE_PAUSE_RESUME_PLAN.md) is the
  design for durable pause/resume, idle-only scheduling, automatic backlog
  processing, and safe GPU hand-off on the homeserver (now implemented; the
  document's references to "the NAS" predate the move to the homeserver).
- [`docs/WEB_UI_SPEC.md`](docs/WEB_UI_SPEC.md) is the authoritative product and
  data-model specification.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) maps implemented phases to the
  current code and states what remains.

## Repository layout

| Path | Contents |
| --- | --- |
| `source/` | Irreplaceable original ISO images. Read-only inputs; gitignored. |
| `pipeline_v3.sh` | Production restoration stages and host-worker control seam. |
| `docker/` | Pinned CUDA/model images and local upstream patches. |
| `models/` | Downloaded model weights; gitignored. |
| `webapp/` | New scanner, SQLite catalog, authenticated API/UI, and GPU worker. |
| `webapp/data/` | All new generated catalog/media/job runtime state; gitignored. |
| `webui/` | Historical blind comparison/voting app on port 8092; unchanged. |
| `docs/` | Current pipeline, UI specification, and implemented architecture. |
| `work/`, `out/` | Legacy pipeline intermediates and reference outputs; never managed by the new app. |

Do not modify, move, or delete files in `source/`, existing `work/`, or `out/`.
The new application mounts source ISOs read-only and keeps its generated files
under `webapp/data/`.

## Restoration profile

The v3 pipeline preserves both bottom-field-first PAL motion samples as 50p,
creates a non-AI 1440p50 baseline, restores temporally with the SeedVR2 3B FP16
model, and muxes sample-accurately trimmed 48 kHz FLAC audio. Its verified
18-second reference is documented in `docs/CURRENT_PIPELINE.md`.

The web application is chapter-first: inspect both discs, prioritize meaningful
sections, and queue a DVD chapter or exact title-relative time slice. One host
worker owns the GPU. In the current Phase 2 implementation, cancellation is
honored only between major pipeline stages and pause/resume is not offered.

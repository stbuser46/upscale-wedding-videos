# Wedding DVD restoration

This project preserves and restores old home DVDs — weddings, birthdays, and other family recordings — using AI upscaling and temporal restoration. It is currently being used to restore PAL wedding DVDs.

## What you can do

- **Run the restoration pipeline** — `pipeline_v3.sh` is the proven quality-first 50p SeedVR2 restoration pipeline that preserves motion and restores temporal quality, processing a chapter as a series of durable ~750-frame units.
- **Restore VHS transfers** — `pipeline_vhs.sh` is the sibling pipeline for already-progressive 1080p VHS cassette transfers (crop the 4:3 pillarbox, denoise, downscale toward native, then SeedVR2 super-resolves back to 1440×1080). It reuses the SeedVR2 engine unchanged; only the front-end and audio differ. `scripts/vhs_preview.sh` runs a short before/after bake-off. See `docs/VHS_PIPELINE.md`.
- **Manage conversions remotely** — `webapp/` provides a private web application to catalog DVD chapters, queue restoration jobs, and monitor long-running GPU work from anywhere.
- **Fan out to the cloud** — the worker can slice a chapter into durable units and restore them in parallel on a fleet of rented RunPod GPUs (`webapp/cloud/`). It is **opt-in and dormant by default**: it only runs when the worker is started with `WEDDING_EXECUTOR=cloud`; otherwise every job runs on the local GPU exactly as before. Money-safety hardened and **live-verified end-to-end on real pods**, including a complete multi-unit job restored, assembled, and frame-validated through the pipelined dispatcher (2026-09-23) — see the money-safety and pipelined-dispatch sections in `docs/ARCHITECTURE.md`.
- **Review and validate output** — The webapp shows progress, logs, and completion status, and a **Previews** tab (`/previews`) plays before/after review clips; historical blind comparison UI remains in `webui/`.

## Key locations

| Path | What it is |
| --- | --- |
| `source/` | Original ISO images (read-only, never modified) |
| `pipeline_v3.sh` | The production DVD restoration script |
| `pipeline_vhs.sh` | VHS-transfer restoration (progressive 1080p → crop/denoise/downscale → SeedVR2), sibling of `pipeline_v3.sh` |
| `lib/` | Shared shell helpers, incl. `seedvr2_unit_args.sh` — the single source of truth for the SeedVR2 unit argv used by both the local and cloud paths |
| `webapp/` | Chapter catalog, job queue, authenticated web UI, and GPU worker |
| `webapp/cloud/` | RunPod cloud fan-out: control-plane client, standalone pod CLI, and money-safety reaper |
| `webapp/data/` | Generated catalog, media, and job runtime state (gitignored) |
| `docker/` | Pinned CUDA/model containers and upstream patches (incl. `seedvr2-pod/` for cloud pods) |
| `scripts/` | Pipeline acceptance tests (e.g. the slice/unit equivalence proof) and `vhs_preview.sh` (VHS before/after bake-off) |
| `models/` | Downloaded AI model weights (gitignored) |
| `docs/` | Pipeline settings, UI specification, and architecture |
| `work/`, `out/` | Legacy pipeline test runs and reference outputs (not managed by webapp) |

## Current status

The webapp implements:

- **Phase 1** — DVD scanner builds a SQLite catalog of all titles and chapters from source ISOs.
- **Phase 2** — Authenticated API and UI for browsing chapters, queueing restoration jobs by chapter or time slice, and monitoring GPU worker progress.

The v3 pipeline preserves both bottom-field-first PAL motion samples as 50p, creates a non-AI 1440p50 baseline, restores temporally with the SeedVR2 3B FP16 model in durable ~750-frame units, and muxes sample-accurately trimmed 48 kHz FLAC audio. Its verified 18-second reference is documented in `docs/CURRENT_PIPELINE.md`.

**Restoration progress** — The Yacoob & Aysha wedding is **fully restored on the local RTX PRO 6000**: all 22 chapters (DVD 1: 15, DVD 2: 7) are processed, validated, and delivered to the NAS as per-chapter "Restored HD" files under `nas.home:/volume1/Movies/WeddingFilm/Yacoob And Aysha/`. This is the first complete disc-set restoration; the pipeline is production-proven well beyond the 18-second reference.

**Cloud fan-out** — `webapp/cloud/` runs durable restoration units on rented RunPod GPUs: a stdlib-only control-plane client, a concurrent pod fleet (`fleet.py`) with a spend cap and guaranteed teardown, a remote unit executor (`executor.py`), a chapter slicer, a money-safety reaper, a pod image under `docker/seedvr2-pod/`, and a `cloud_pods` spend ledger surfaced by a read-only "Cloud fleet" UI panel. `lib/seedvr2_unit_args.sh` gives the local and cloud paths one shared argv so they can never drift. The worker selects local vs cloud **per process** via `WEDDING_EXECUTOR` (default `local`) — there is no per-job cloud toggle in the GUI. A standalone CLI (`python -m webapp.cloud.cli`) can also drive a single pod for testing. Dispatch is **pipelined per pod** (upload/restore/download overlap, background pre-slicer, in-run unit retry) with peer-to-peer transfer paths so bulk data crosses the home upstream once per job (warm-cache seeding; stage-1 intermediate staged on the best-ingress pod with per-slice framemd5 identity proofs) — all mock-tested (`webapp/worker/test_cloud_dispatch.py`, `webapp/cloud/test_fleet_cache.py`) and live-verified 2026-09-23, when a real run also caught and fixed an unowned-pod billing leak. The spend cap is a hard ceiling enforced while pods run (first live firing confirmed clean teardown mid-run), teardown is confirmed-only, auto-resume is bounded, and an age-filtered reaper timer is the backstop; see the money-safety section in `docs/ARCHITECTURE.md`.

**VHS pilot** — `pipeline_vhs.sh` (+ `scripts/vhs_preview.sh`) targets the two remaining VHS events (Nosheen wedding, Sheerjeel birthday), which are already-progressive 1080p cassette transfers, not DVDs. The front-end is validated and 20 s Sheerjeel Original+Copy previews are restored and reviewed. The webapp gained a **Previews** tab (`/previews`, data-driven from `webapp/data/previews/manifest.json`, media served with HTTP Range) that plays the before/after clips. Full-file runs are pending source (Original vs Copy) and frame-rate (25p vs 50p) selection. See `docs/VHS_PIPELINE.md` and `docs/REMAINING_SOURCES.md`.

Cancellation is honored only between major pipeline stages and durable units; interactive pause/resume is not yet implemented.

## More detail

- [`webapp/README.md`](webapp/README.md) — Setup and operations guide
- [`docs/CURRENT_PIPELINE.md`](docs/CURRENT_PIPELINE.md) — Media analysis, pinned settings, validation, performance
- [`docs/WEB_UI_SPEC.md`](docs/WEB_UI_SPEC.md) — Product and data-model specification
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — Implemented phases mapped to current code
- [`docs/PERF_REVIEW_2026-09-21.md`](docs/PERF_REVIEW_2026-09-21.md) — Performance review: measured time breakdown, local (~1.15–1.3x) and cloud recommendations
- [`docs/VHS_PIPELINE.md`](docs/VHS_PIPELINE.md) — VHS-transfer restoration recipe, source specs, and previews page
- [`docs/REMAINING_SOURCES.md`](docs/REMAINING_SOURCES.md) — Inventory & plan for the remaining DVD + VHS sources

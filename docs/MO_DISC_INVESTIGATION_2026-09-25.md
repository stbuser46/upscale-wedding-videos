# Mo & Anisha DVD (mo_dvd1.iso) — format investigation, 2026-09-25

Investigated by a background agent during the Gulfraz disc-2 run; findings
verified against the actual VOBs (not container flags). One correction applied
by the operator session (engine gate status — see bottom).

> **To actually run the disc, use [`MO_ANISHA_RUNBOOK.md`](MO_ANISHA_RUNBOOK.md).** It supersedes the "Recommended launch" section below: queue 12 ~10-minute segments with `scripts/queue_segments.py` instead of 4 whole-title jobs.

## Source format (probed)
- PAL 25i, genuinely TOP-FIELD-FIRST: `field_order=tt` + idet over 750-frame
  samples at 5 points = TFF 751 / BFF 0 / Progressive 0. No telecine, no
  progressive segments, no cadence breaks.
- 704×576, SAR 12:11, DAR 4:3 → pinned 768:576 target is exactly right, no crop.
- MPEG-2 ~4.81 Mbps CBR (Ulead authoring, volume "HATHURAN"); audio MP2 48 kHz
  224 kbps (FLAC mux path handles it). `color_space=fcc` quirk: visually nil.
- Titles (disc id 4, chapters 36–39): 10.37 + 46.03 + 16.49 + 36.85 min
  = 109.7 min → 329,194 frames @50p → **441 durable units** (42/185/66/148).

## Pipeline readiness: ZERO code changes needed
- Parity is data-driven end-to-end: `DiscSpec("mo_dvd1.iso", …, "pal", "tff")`
  already registered (webapp/config/settings.py:41), deinterlace_profile →
  bwdif parity=tff (services.py:28–47), snapshot into every job, worker passes
  DEINT_* env (runner.py:431–443). Stage-1 runs locally for cloud jobs too.
- **Live-proven**: job 26 `restore-704adf4741bb` (15 s TFF slice of title 2)
  completed 2026-09-17; output in webapp/data/outputs/.
- TFF has zero speed cost. PAL needs 40 units/10 min (vs NTSC 48) → ~17%
  cheaper per source-minute than Gulfraz.

## Cost & wall-clock (ledger-grounded: $0.61–0.86/unit all-in, 2.1–2.7 min/unit)
- 8-wide: **~21–24 h, ~$290–360** for the disc.
- 12–16 wide (hot-add as stock allows, max_slots=16): **~13–16 h**, similar $.
- Local-only reference: ~99 h, $0.
- Stage-1 transient disk ≈ 90 GB across titles (T2 ≈ 35–40 GB) — fits reserve.
- Biggest job (T2) ≈ $115–160, well under the $250 default cap.

## Quality plan (all ~$0)
- The one Mo hazard (wrong parity → pairwise motion stutter) is already
  retired; re-verify by frame-stepping the existing job-26 clip through a pan.
- Optional paranoia: 15 s local slices of titles 1/3/4 (~15 min each) → also
  yields per-title before/after previews.
- Eyeball the first cloud unit of the first title before leaving it unattended.

## Recommended launch
Queue chapters 37 → 39 → 38 → 36 (T2 first: its long stage-1 overlaps the one
unavoidable bring-up), all start-requested up front, then ONE continuous worker:
```
WEDDING_EXECUTOR=cloud WEDDING_DURABLE_UNITS=1 WEDDING_CLOUD_KEEP_ALIVE=1 \
WEDDING_POD_ENGINE=1 WEDDING_CLOUD_SPEND_CAP_USD=250 WEDDING_CLOUD_MAX_SLOTS=16 \
WEDDING_CLOUD_IMAGE=ghcr.io/stbuser46/seedvr2-pod:v3 \
webapp/.venv/bin/python -m webapp.worker.runner
```
- Keep-alive parks tail pods across the 3 boundaries (captured scarce stock).
- Optionally run the local worker alongside for title 1 (42 units ≈ 9.5 h,
  free, removes a boundary) — safe since the cloud path takes longer anyway.
- Hot-add toward 16 opportunistically; watch the FIRST baked-image bring-up
  (fleet path not yet live-tested with it; fall back to stock image if odd).
- Manual optional: PREPARE_ONLY=1 pre-bake of the next title's stage-1 during
  the current title's GPU phase (~1–1.5 h saved across boundaries).

## Operator correction to the agent's report
The agent advised keeping WEDDING_POD_ENGINE off based on ARCHITECTURE.md's
stale record of the failed canary. As of 2026-09-25 the offload fix PASSED the
full-size acceptance canary (bit-identical, RTX PRO 6000 Blackwell SE — see
`needs fixing.md`) and Gulfraz Segs 03–06 ran with the engine live; the
2.1–2.7 min/unit cadence it measured IS engine-mode. Engine ON is correct for
Mo. (ARCHITECTURE.md needs a doc refresh to record this.)

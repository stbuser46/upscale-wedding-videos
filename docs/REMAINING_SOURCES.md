# Remaining family recordings — source inventory & restoration plan

_Status as of 2026-09-16. The **Yacoob & Aysha** wedding (2 DVDs, 22 chapters) is
already fully restored and delivered. This document covers the **four events that
remain**, all staged on `nas.home:/volume1/Movies/WeddingFilm/`._

> **Preview outcome (2026-09-16):** 5-second midpoint SeedVR2 previews of both
> DVDs were produced and reviewed — **both look good, Gulfraz especially**.
> **Decision: Gulfraz And Fahiza is the next full restoration** — but **not yet**
> (deferred, no start date). When it runs it must use the **NTSC** front-end
> (59.94p, 640×480, SMPTE-170M), since `pipeline_v3.sh` is hardcoded to PAL/BFF.
> Mo and anisha (PAL/TFF) remains a candidate after Gulfraz.

## The four remaining events

| Event | Type | Files on NAS | Container / codec | Resolution | Scan / fps | Fit for `pipeline_v3.sh`? |
|---|---|---|---|---|---|---|
| **Gulfraz And Fahiza** (wedding) | **DVD** | `Wedding ISO 1.iso` (6.3 GB) + `Wedding ISO 2.iso` (2.3 GB) | MPEG-2 program stream (VOB) | **720×480** (DAR 4:3, SAR 8:9) | **29.97i, NTSC, BFF** | ⚠️ DVD, but **NTSC** — needs NTSC handling (see below) |
| **Mo and anisha** (wedding) | **DVD** | `Mo and anisha DVD 1.ISO` (3.8 GB) + a derived `.mp4` | MPEG-2 (VOB); the mp4 is H.264 | 704×576 (DAR 4:3, SAR 12:11) | **25i, PAL, TFF** (mp4 is a 50p bwdif deinterlace) | ⚠️ DVD/PAL, but **top-field-first** — pipeline defaults to BFF |
| **Nosheen and Shahid** (wedding) | **VHS** (cassette transfer) | mp4 only — "Wedding Mix" (Part 1–3) + "Original cassatte" (Part 1–2) | H.264 | **1920×1080** | **25 fps**, progressive | ⚠️ **No** — not a DVD; needs a VHS pipeline |
| **Sheerjeel Birthday** 🎂 | **VHS** (cassette transfer) | mp4 only — `Birthday Original.mp4` + `Birthday Copy Cassatte.mp4` | H.264 | **1920×1080** | **25 fps** | ⚠️ **No** — not a DVD; needs a VHS pipeline |

### Two source classes, two very different starting points

- **DVD (Gulfraz, Mo):** true DVD masters — interlaced MPEG-2. The pipeline
  (`pipeline_v3.sh`) field-splits the interlace, makes a 1440p Lanczos baseline,
  then restores with SeedVR2 to **1920×1440 10-bit HEVC**. **Important caveat:**
  the pipeline is pinned to the Yacoob discs' exact format — **PAL, 768×576, 50p,
  BFF, bt470bg** — and **neither remaining disc matches it**:
  - **Gulfraz is NTSC** (720×480, 29.97i, BFF, SMPTE-170M): the correct restore
    front-end is `bwdif=parity=bff` → **59.94p**, square-pixel **640×480**,
    `in_color_matrix=smpte170m` — _not_ the pinned PAL 50p/576/bt470bg.
  - **Mo is PAL but TFF** (704×576, 25i, top-field-first): the correct parity is
    `bwdif=parity=tff`; running the pinned `parity=bff` reverses its fields and
    causes motion judder.

  Confirmed by `ffprobe` + `idet` on midpoint dumps. For the previews below these
  were handled by injecting a correct per-disc stage-1 deinterlace; a **full
  restoration will need `pipeline_v3.sh` parameterised for standard + field order**
  (today they are hardcoded), or a per-disc wrapper.
- **VHS (Nosheen, Sheerjeel):** cassette transfers that a ripper has **already
  blown up to 1080p25 progressive H.264**. There is no interlaced DVD master and
  no ISO. The true information content is VHS-grade (~240–320 lines), already
  stretched to 1080p — so the restorer's job here is **denoise / detail recovery,
  not a resolution jump**. Running these through the DVD pipeline would be wrong
  (it would downscale 1080→576 and field-split already-progressive frames).

## What we're doing now — the two DVDs

Previewing **Gulfraz** and **Mo** before committing to full restorations:

- Pull ~5 seconds from the **middle** of each disc's main title and run the real
  `pipeline_v3.sh` SeedVR2 restore locally on the RTX PRO 6000.
- Deliver **separate before/after** clips per video:
  - **BEFORE** = the DVD deinterlaced at native size (Mo 768×576 50p; Gulfraz
    640×480 59.94p) — what the source actually is.
  - **AFTER** = SeedVR2-restored 1920×1440 10-bit HEVC.
  - (+ a non-AI 1440p Lanczos baseline as a matched-resolution reference.)
- Midpoints located via `lsdvd`: Gulfraz = single 188-min title, midpoint in
  chapter 7; Mo = title 2 (46 min), midpoint ~1381 s.

If the previews look good, both get the full per-chapter restoration and delivery
to the NAS, exactly like Yacoob & Aysha.

## What's deferred — the two VHS transfers (future pipeline)

**Nosheen and Shahid** and **Sheerjeel Birthday** are **not** being restored yet.
They need a **separate VHS restoration pipeline** — a deliberate, later piece of
work. Design notes for that pipeline, captured now while the differences are fresh:

- **Input is 1080p25 progressive H.264, not interlaced DVD** → skip the DVD
  front-end entirely: no ISO/VOB extraction, no `parity=bff` field-split, no
  576 downscale. Feed frames (or the mp4 slice) straight to SeedVR2.
- **Interlace check per file:** "Wedding Mix" probes as `progressive`; the other
  three probe `field_order=unknown`. A VHS pipeline should **`idet`-detect** and
  only deinterlace if combing is actually present, rather than assuming.
- **Restore intent = clean-up, not upscale:** source is already 1080p; decide the
  SeedVR2 target resolution accordingly (a modest bump, plus denoise), and expect
  VHS artifacts (chroma bleed, head-switching noise at the bottom, dropout) the
  DVD path never had to handle.
- **Frame rate:** these are 25p (fields already merged by the ripper), unlike the
  DVD path's reconstructed 50p. Decide whether to keep 25p or interpolate.
- **Pick the best of each pair:** Nosheen has "Original cassatte" vs "Wedding
  Mix"; Sheerjeel has "Original" vs "Copy". Preview both and restore the
  least-processed / highest-fidelity source.
- **Multi-part assembly:** Nosheen is split across 2–3 parts that must be
  restored and re-joined.
- **Reuse what's source-agnostic:** the SeedVR2 engine itself is not DVD-specific
  — `lib/seedvr2_unit_args.sh`, the `seedvr2-cuda:v3` image, and the cloud/local
  single-unit paths all take an arbitrary video in and give a video out. Only the
  catalog/scan ingestion and the 50 fps / PAL assumptions are DVD-bound.

_Cross-reference: `docs/CURRENT_PIPELINE.md` (the pinned DVD recipe) and
`docs/ARCHITECTURE.md` (implemented phases)._

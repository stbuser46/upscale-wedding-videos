# VHS-transfer restoration pipeline

Status: **pilot** (2026-09-16). New front-end (`pipeline_vhs.sh` +
`scripts/vhs_preview.sh`) built and front-end-validated; first Sheerjeel preview
bake-off in progress. This is the VHS sibling of the proven DVD pipeline
(`docs/CURRENT_PIPELINE.md`); it reuses the **same SeedVR2 engine unchanged** and
only replaces the DVD-specific front-end and audio.

## Why VHS needs a different pipeline

The two remaining VHS events (*Nosheen & Shahid* wedding, *Sheerjeel* birthday)
are **cassette transfers a ripper already digitised**, not DVD masters. Measured
this session with `ffprobe` / `idet` / `cropdetect` on the NAS files:

| Property | DVD path (`pipeline_v3.sh`) | VHS path (`pipeline_vhs.sh`) |
| --- | --- | --- |
| Container / codec | MPEG-2 in VOB (from ISO) | **H.264 mp4** (already digitised) |
| Scan | interlaced 25i, field-split to **50p** | **progressive 25p** (idet 300/300; fields already merged) |
| Resolution move | **upscale** 720×576 → 1440p | **downscale then super-resolve** (the 1080p is a fake upscale) |
| Geometry | 4:3, square-pixel 768×576 | 4:3 **pillarboxed** inside 16:9 → crop `1440×1080` |
| Colour | PAL `bt470bg` / gamma28 | **`bt709`** throughout |
| Audio | AC-3 (DVD-packet preroll trick) | AAC (plain sample-accurate mux) |
| Frame rate out | 50p | 25p (50p optional via RIFE) |

Three facts drive the design:

1. **Already progressive.** No deinterlace, no parity, no field doubling. (If a
   future source ever shows combing, `idet` gates a `bwdif` pass — none of the
   four current files need it.)
2. **The 1080p is a fake upscale** of ~240 lines / ≈333×480 of real VHS detail.
   Feeding a diffusion super-resolver soft, already-stretched 1080p makes it
   sharpen mush. Best practice — **downscale → denoise → AI super-resolve** — both
   averages tape noise and gives SeedVR2 the genuine low-res→HD task it was
   trained for. Default: crop the 4:3 window → light `hqdn3d` → downscale to
   ~540p → SeedVR2 back to 1440×1080.
3. **Content is 4:3 pillarboxed.** `cropdetect` shows the active picture centred
   at `1440×1080` (≈240 px black bars each side) on Sheerjeel Original/Copy and
   Nosheen Original. Crop it first or we waste GPU on black bars and get wrong
   geometry. (Nosheen "Mix" is authored oddly — narrower `1346×1080`, SAR 35:32 —
   another reason to prefer the raw cassettes; override with `VHS_CROP` if used.)

## Measured source inventory (`nas.home:/volume1/Movies/WeddingFilm/`)

| Event | Files | Video | Scan | Geometry |
| --- | --- | --- | --- | --- |
| Sheerjeel — **Original** | `Birthday Original.mp4` (59.5 min) | 1080p25 H.264 8.5 Mbps bt709 | progressive | 4:3 pillarbox `1440×1080` |
| Sheerjeel — Copy | `Birthday Copy Cassatte.mp4` (61 min) | 1080p25 8.2 Mbps | progressive | same |
| Nosheen — Wedding Mix | `Part 1–3.mp4` (~2.7 hr) | 1080p25 8.5 Mbps, SAR 35:32 | progressive | narrower `1346×1080` |
| Nosheen — Original cassatte | `Part 1–2.mp4` (~3 hr) | 1080p25 5.5 Mbps | progressive | 4:3 pillarbox `1440×1080` |

## Engine reuse (no fork of the AI core)

`pipeline_vhs.sh` sources the **same** `lib/seedvr2_unit_args.sh` argv builder,
runs the **same** `seedvr2-cuda:v3` image, supports the **same** `UNIT_*` durable
single-unit mode, persistent inductor cache, 110 GB RAM cap, cancel/free-space
guards, and partial-rename resume as `pipeline_v3.sh`. The SeedVR2 output short
side is a parameter (`SEEDVR2_RESOLUTION`, default **1080** here → 1440×1080 for a
4:3 window); the image already forces BT.709 output regardless of source. Only
stage 1 (front-end) and stage 4 (audio) are VHS-specific. `pipeline_v3.sh` and
the DVD outputs are untouched.

> The webapp GUI/cloud path is intentionally **out of scope** for now: the webapp
> worker's frame math is hardcoded to 50 fps (`webapp/server/services.py:34,102`,
> `webapp/worker/runner.py:526-527,637`, `webapp/worker/slicer.py:27`). The VHS
> pilot is driven from the CLI, which has no such assumption. Bringing VHS into
> the GUI/cloud is a later follow-up that parameterises those `50`s.

## Usage

### Preview bake-off (decide the recipe before committing GPU days)

```bash
scripts/vhs_preview.sh \
  "yimoolla@nas.home:/volume1/Movies/WeddingFilm/Sheerjeel Birthday Original/Birthday Original.mp4" \
  1800 20 sheerjeel_orig
```

Stages a 20 s slice (stream-copy, true source quality) and produces, under
`out/vhs_preview/<tag>/`: `BEFORE` (cropped source), `BASELINE` (non-AI Lanczos),
`A_downscale540` (the chosen method), `B_inplace` (SR with no downscale, sanity
A/B), `A_downscale540_50p` (minterpolate — motion-feel stand-in for RIFE), and a
labelled 2×2 `grid`. Run it on **both** the Original and the Copy to pick the
better source. `EXTRA=480` adds a more-aggressive downscale target.

### Full restoration (after the preview validates the recipe)

Stage the chosen full source under `work/` (input must live inside the project),
then run the whole file — durable units keep it resumable:

```bash
# one-shot (monolithic, internal 750-frame streaming):
./pipeline_vhs.sh work/vhs_src/sheerjeel_original.mkv 0 3569 \
  out/sheerjeel/Sheerjeel-Restored-HD.mkv sheerjeel_orig
```

Deliver to `nas.home:/volume1/Movies/WeddingFilm/Sheerjeel Birthday/…` as a
"Restored HD" file, mirroring the Yacoob & Aysha convention.

## Front-end knobs (`pipeline_vhs.sh`)

| Env | Default | Effect |
| --- | --- | --- |
| `VHS_CROP` | centred 4:3 of full height | crop `W:H:X:Y`; override for odd sources |
| `VHS_INPLACE` | `0` | `1` = skip downscale, SR the native-res crop 1:1 |
| `VHS_PREP_HEIGHT` | `540` | downscale short side before SR (ignored if in-place) |
| `VHS_DENOISE` | `4:3:6:4.5` | `hqdn3d` params, or `none`; kept light on purpose |
| `VHS_FPS` | `25` | prepared-intermediate frame rate |
| `VHS_AUDIO` | `flac` | `flac` \| `copy` \| `none` (video-only) |
| `SEEDVR2_RESOLUTION` | `1080` | SeedVR2 output short side |

Plus every `SEEDVR2_*` / `SKIP_BASELINE` / `FORCE` knob from `pipeline_v3.sh`.

## Open decisions (pending the preview)

- **Source per event:** Sheerjeel Original vs Copy; Nosheen Mix vs Original.
- **25p vs 50p:** the preview ships a minterpolate 50p stand-in; if 50p wins,
  wire a real RIFE stage for the final (minterpolate is preview-only).
- **Downscale target / denoise strength:** 540 default vs 480; `hqdn3d` amount.

## Reviewing previews in the webapp

The preview clips are surfaced in the Flask webapp under a new **Previews** tab
(`/previews`), driven by `webapp/data/previews/manifest.json` (a list of `sets`,
each with `clips` of `{file,label,note}`). Media is served same-origin by
`GET /previews/media/<file>` from `webapp/data/previews/` with HTTP Range support
(seeking), so it satisfies the app's strict CSP (`media-src 'self'`, no inline
style/JS — the page uses `app.css` classes and native `<video>` only). To add a
source, drop browser-safe `.mp4`s in `webapp/data/previews/` and append a set to
the manifest — the route reads it per-request, so **no restart is needed**
(code/template/CSS changes do need a `kill -HUP <gunicorn-master>` reload).
`scripts/vhs_preview.sh` produces the clips; convert HEVC variants to H.264 mp4
for the browser.

## Front-end validation (2026-09-16)

On a 6 s Sheerjeel Original slice, prepare-only produced the expected FFV1
intermediates: `720×540` (downscale) and `1440×1080` (in-place), both yuv444p,
BT.709, SAR 1:1, 25 fps, **progressive** (after `setfield=prog`), exactly
150 frames (6 s × 25). SeedVR2 restoration quality is judged from the preview
bake-off.

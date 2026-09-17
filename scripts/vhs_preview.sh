#!/usr/bin/env bash
# VHS preview bake-off: stage a short slice, restore it several ways, and build
# one labelled comparison so we can decide the recipe BEFORE committing days of
# GPU. See docs/VHS_PIPELINE.md.
#
#   scripts/vhs_preview.sh <source_spec> <start_sec> <dur_sec> <tag>
#
#   <source_spec>  "user@host:/remote/path.mp4" (staged via ssh stream-copy) OR
#                  a local path already under the project.
#
# Variants produced (video-only), all on the SAME cropped 4:3 window:
#   BEFORE          the cropped source as-is (what we start from)
#   BASELINE        non-AI Lanczos to 1440x1080 (isolates the AI's contribution)
#   AI downscale    downscale->SeedVR2->1440x1080  (the chosen method)
#   AI in-place     SeedVR2 on the full-res crop, no downscale (sanity A/B)
#   AI downscale/50 the AI-downscale clip interpolated to 50p (motion-feel A/B;
#                   uses ffmpeg minterpolate for the preview — the real 50p run
#                   would use RIFE)
# Plus a 2x2 labelled grid: <tag>_grid.mkv.
set -euo pipefail

if (( $# < 1 || $# > 4 )); then
  echo "Usage: $0 <source_spec> [start_sec] [dur_sec] [tag]" >&2
  exit 2
fi

PROJ=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SRC=$1
SS=${2:-1800}
DUR=${3:-20}
TAG=${4:-vhs_preview}
FFMPEG_IMAGE=${WEBAPP_FFMPEG_IMAGE:-upscaler-cuda:latest}

# Which downscale-target variants to run beyond the default 540. e.g. EXTRA=480
EXTRA=${EXTRA:-}
# hqdn3d strength shared by every AI variant (kept light on purpose).
VHS_DENOISE=${VHS_DENOISE:-4:3:6:4.5}
export VHS_DENOISE

STAGE_DIR=$PROJ/work/vhs_preview_src
OUT_DIR=$PROJ/out/vhs_preview/$TAG
mkdir -p "$STAGE_DIR" "$OUT_DIR"
STAGED=$STAGE_DIR/$TAG.mkv

# ── 1. Stage the slice ───────────────────────────────────────────────────────
# Stream-copy (no re-encode → true source quality) from the NAS. Input-seek to
# the nearest keyframe ≤ SS, then keep DUR seconds of video only. Exact start is
# irrelevant for a representative preview clip.
if [[ "$SRC" == *:* && "$SRC" != /* ]]; then
  HOST=${SRC%%:*}
  RPATH=${SRC#*:}
  if [[ ! -s "$STAGED" ]]; then
    echo "[stage] $HOST:$RPATH  @${SS}s +${DUR}s  ->  ${STAGED#"$PROJ/"}"
    ssh "$HOST" "ffmpeg -hide_banner -loglevel error -ss $SS -i \"$RPATH\" -t $DUR -map 0:v:0 -c copy -f matroska -" > "$STAGED"
  else
    echo "[stage] reuse ${STAGED#"$PROJ/"}"
  fi
  PSS=0            # staged file already starts at the slice
else
  case "$SRC" in /*) STAGED=$SRC ;; *) STAGED=$PROJ/$SRC ;; esac
  PSS=$SS
fi
if [[ ! -s "$STAGED" ]]; then echo "staging failed: $STAGED" >&2; exit 1; fi
STAGED_REL=${STAGED#"$PROJ/"}

# ── 2. Restore variants via pipeline_vhs.sh (video-only) ─────────────────────
run_variant() {  # <suffix> <extra env assignments...>
  local suffix=$1; shift
  local out="$OUT_DIR/${TAG}_${suffix}.mkv"
  echo "== variant $suffix -> ${out#"$PROJ/"} =="
  env VHS_AUDIO=none "$@" \
    "$PROJ/pipeline_vhs.sh" "$STAGED_REL" "$PSS" "$DUR" \
    "out/vhs_preview/$TAG/${TAG}_${suffix}.mkv" "${TAG}_${suffix}"
}

# A: downscale->SR 540 (the chosen method) + non-AI baseline in the same run.
run_variant A_downscale540 SKIP_BASELINE=0 VHS_INPLACE=0 VHS_PREP_HEIGHT=540
cp -f "$PROJ/work/${TAG}_A_downscale540/baseline_1080p.mkv" "$OUT_DIR/${TAG}_BASELINE.mkv" 2>/dev/null || true

# B: in-place SR at native crop resolution (no downscale).
run_variant B_inplace SKIP_BASELINE=1 VHS_INPLACE=1

# Optional extra downscale targets (e.g. 480 for more noise-averaging).
for h in $EXTRA; do
  run_variant "A_downscale${h}" SKIP_BASELINE=1 VHS_INPLACE=0 VHS_PREP_HEIGHT="$h"
done

# ── 3. BEFORE tile (cropped source, no restoration) ──────────────────────────
BEFORE="$OUT_DIR/${TAG}_BEFORE.mkv"
docker run --rm -v "$PROJ:/proj" --entrypoint ffmpeg "$FFMPEG_IMAGE" \
  -hide_banner -loglevel error -y -ss "$PSS" -t "$DUR" -i "/proj/$STAGED_REL" \
  -vf "crop=w=trunc(ih*4/3/2)*2:h=ih,setsar=1" -an -c:v libx264 -crf 12 -pix_fmt yuv420p \
  "/proj/out/vhs_preview/$TAG/${TAG}_BEFORE.mkv"

# ── 4. 50p motion-feel variant (minterpolate now; RIFE if 50p is chosen) ─────
A_MAIN="$OUT_DIR/${TAG}_A_downscale540.mkv"
docker run --rm -v "$PROJ:/proj" --entrypoint ffmpeg "$FFMPEG_IMAGE" \
  -hide_banner -loglevel error -y -i "/proj/out/vhs_preview/$TAG/${TAG}_A_downscale540.mkv" \
  -vf "minterpolate=fps=50:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1" \
  -an -c:v libx264 -crf 16 -pix_fmt yuv420p \
  "/proj/out/vhs_preview/$TAG/${TAG}_A_downscale540_50p.mkv"

# ── 5. Labelled 2x2 grid: BEFORE | BASELINE / AI-downscale | AI-inplace ──────
FONT=$(docker run --rm --entrypoint sh "$FFMPEG_IMAGE" -c \
  'for f in /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf /usr/share/fonts/truetype/freefont/FreeSans.ttf $(find /usr/share/fonts -name "*.ttf" 2>/dev/null | head -1); do [ -f "$f" ] && { echo "$f"; break; }; done' 2>/dev/null || true)
lbl() {  # tile filter with optional burned-in label
  if [[ -n "$FONT" ]]; then
    printf "scale=720:540,drawtext=fontfile=%s:text='%s':fontcolor=white:fontsize=30:x=12:y=12:box=1:boxcolor=black@0.55" "$FONT" "$1"
  else
    printf "scale=720:540"
  fi
}
echo "[grid] building ${TAG}_grid.mkv"
set +e
docker run --rm -v "$PROJ:/proj" --entrypoint ffmpeg "$FFMPEG_IMAGE" \
  -hide_banner -loglevel error -y \
  -i "/proj/out/vhs_preview/$TAG/${TAG}_BEFORE.mkv" \
  -i "/proj/out/vhs_preview/$TAG/${TAG}_BASELINE.mkv" \
  -i "/proj/out/vhs_preview/$TAG/${TAG}_A_downscale540.mkv" \
  -i "/proj/out/vhs_preview/$TAG/${TAG}_B_inplace.mkv" \
  -filter_complex "[0:v]$(lbl 'BEFORE (source)')[a];[1:v]$(lbl 'BASELINE (no AI)')[b];[2:v]$(lbl 'AI downscale->SR')[c];[3:v]$(lbl 'AI in-place')[d];[a][b][c][d]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0[out]" \
  -map "[out]" -an -c:v libx264 -crf 16 -pix_fmt yuv420p \
  "/proj/out/vhs_preview/$TAG/${TAG}_grid.mkv"
grid_status=$?
set -e
(( grid_status != 0 )) && echo "[grid] grid failed (font? codec?) — standalone variant files are still valid"

echo
echo "DONE — preview outputs in ${OUT_DIR#"$PROJ/"}/"
ls -1 "$OUT_DIR"

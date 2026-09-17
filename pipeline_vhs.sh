#!/usr/bin/env bash
# Quality-first VHS-transfer restoration using SeedVR2.
#
# Usage:
#   ./pipeline_vhs.sh <input> <start_sec> <duration_sec> <output.mkv> [tag]
#
# This is the VHS sibling of pipeline_v3.sh. The SeedVR2 engine is identical and
# shared verbatim (lib/seedvr2_unit_args.sh, the seedvr2-cuda:v3 image, and the
# durable UNIT_* mode below); ONLY the front-end (stage 1) and the audio mux
# (stage 4) differ, because a VHS transfer is nothing like a PAL DVD:
#
#   * The input is ALREADY progressive 25p H.264 (a ripper merged the fields and
#     blew it up to 1080p) — so there is NO deinterlace, no parity, no field
#     doubling. Confirmed via idet on all four remaining sources (300/300
#     progressive). See docs/VHS_PIPELINE.md.
#   * The 1080p is a fake upscale of ~333x480 of real VHS detail. So instead of
#     UPSCALING (as the DVD path does, 576 -> 1440) we DOWNSCALE toward native,
#     denoise, and let SeedVR2 super-resolve back to clean HD. The downscale
#     averages tape noise and gives the model the genuine low-res -> HD task it
#     was trained for, rather than asking it to sharpen soft, stretched mush.
#   * The real picture is 4:3 pillarboxed inside the 16:9 frame — crop it, or we
#     burn GPU on black bars and hand SeedVR2 the wrong geometry.
#   * Colour is BT.709 (not PAL bt470bg); audio is AAC (no AC-3 DVD-packet
#     preroll trick needed).
#
# The job is resumable. Completed stages are reused unless FORCE=1 is set.
# pipeline_v3.sh and all DVD outputs are never touched.
set -euo pipefail

if (( $# < 4 || $# > 5 )); then
  echo "Usage: $0 <input> <start_sec> <duration_sec> <output.mkv> [tag]" >&2
  exit 2
fi

PROJ=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# Pinned SeedVR2 unit argv, shared with pipeline_v3.sh and the cloud executor.
source "$PROJ/lib/seedvr2_unit_args.sh"
IN=$1
SS=$2
DUR=$3
OUT=$4
TAG=${5:-vhs}
IMAGE=${SEEDVR2_IMAGE:-seedvr2-cuda:v3}
FFMPEG_IMAGE=${WEBAPP_FFMPEG_IMAGE:-upscaler-cuda:latest}
# Container RAM cap — same rationale as pipeline_v3.sh (OOM-kill a runaway
# restoration inside its cgroup instead of freezing the host). Override with
# SEEDVR2_MEM_LIMIT; set it empty to disable.
MEM_LIMIT=${SEEDVR2_MEM_LIMIT-110g}
MEM_ARGS=()
if [[ -n "$MEM_LIMIT" ]]; then
  MEM_ARGS=(--memory="$MEM_LIMIT" --memory-swap="$MEM_LIMIT")
fi
MODEL=${SEEDVR2_MODEL:-seedvr2_ema_3b_fp16.safetensors}
# VHS default output short side is 1080 (4:3 -> 1440x1080), not the DVD 1440.
RESOLUTION=${SEEDVR2_RESOLUTION:-1080}
BATCH=${SEEDVR2_BATCH:-129}
CHUNK=${SEEDVR2_CHUNK:-750}
OVERLAP=${SEEDVR2_OVERLAP:-4}
COMPILE=${SEEDVR2_COMPILE:-1}
COMPILE_ARGS=()
if [[ "$COMPILE" == 1 ]]; then
  COMPILE_ARGS=(--compile_vae --cache_vae)
fi
FORCE=${FORCE:-0}
SKIP_BASELINE=${SKIP_BASELINE:-0}
WORK_ROOT=${PIPELINE_WORK_ROOT:-$PROJ/work}
CONTROL_FILE=${PIPELINE_CONTROL_FILE:-}
FREE_SPACE_RESERVE_BYTES=${PIPELINE_FREE_SPACE_RESERVE_BYTES:-0}

# ── VHS front-end knobs ──────────────────────────────────────────────────────
# VHS_CROP     crop geometry "W:H:X:Y" for the 4:3 window. If unset, a centred
#              4:3-of-full-height crop is computed (correct for a square-pixel
#              16:9 transfer: 1920x1080 -> 1440x1080 @ x=240). Override for
#              oddly-authored sources (e.g. Nosheen "Mix" is narrower).
# VHS_INPLACE  1 = skip the downscale; feed the cropped native-res frame to
#              SeedVR2 at 1:1 (denoise+restore in place). Default 0.
# VHS_PREP_HEIGHT  downscale target short side before super-resolution (default
#              540 -> a 2x task to 1440x1080). Ignored when VHS_INPLACE=1.
# VHS_DENOISE  hqdn3d params, or "none" to disable. Light by default so SeedVR2
#              does the heavy lifting (over-denoising strips recoverable detail).
# VHS_FPS      output frame rate of the prepared intermediate (default 25).
# VHS_AUDIO    flac (default, sample-accurate) | copy (stream-copy source AAC) |
#              none (video-only output — used by the preview harness).
VHS_CROP=${VHS_CROP:-}
VHS_INPLACE=${VHS_INPLACE:-0}
VHS_PREP_HEIGHT=${VHS_PREP_HEIGHT:-540}
VHS_DENOISE=${VHS_DENOISE:-4:3:6:4.5}
VHS_FPS=${VHS_FPS:-25}
VHS_AUDIO=${VHS_AUDIO:-flac}

case "$IN" in
  /*) INPUT=$IN ;;
  *) INPUT=$PROJ/$IN ;;
esac
case "$OUT" in
  /*) OUTPUT=$OUT ;;
  *) OUTPUT=$PROJ/$OUT ;;
esac

case "$WORK_ROOT" in
  "$PROJ"/*) ;;
  *) echo "PIPELINE_WORK_ROOT must be inside the project: $WORK_ROOT" >&2; exit 2 ;;
esac

# Persist torch.compile kernels across the per-unit --rm containers, exactly as
# pipeline_v3.sh does. VHS shapes differ from DVD, so the first VHS unit compiles
# fresh and every later unit starts warm. Override with SEEDVR2_INDUCTOR_CACHE.
INDUCTOR_CACHE=${SEEDVR2_INDUCTOR_CACHE-$WORK_ROOT/.inductor_cache}
CACHE_ARGS=()
if [[ -n "$INDUCTOR_CACHE" ]]; then
  case "$INDUCTOR_CACHE" in
    "$PROJ"/*) ;;
    *) echo "SEEDVR2_INDUCTOR_CACHE must be inside the project: $INDUCTOR_CACHE" >&2; exit 2 ;;
  esac
  mkdir -p "$INDUCTOR_CACHE"
  CACHE_ARGS=(-e TORCHINDUCTOR_CACHE_DIR="/proj/${INDUCTOR_CACHE#"$PROJ/"}"
              -e TRITON_CACHE_DIR="/proj/${INDUCTOR_CACHE#"$PROJ/"}/triton")
fi
case "$INPUT" in
  "$PROJ"/*) ;;
  *) echo "Input must be inside the project (stage NAS sources under work/): $INPUT" >&2; exit 2 ;;
esac
case "$OUTPUT" in
  "$PROJ"/*) ;;
  *) echo "Output must be inside the project: $OUTPUT" >&2; exit 2 ;;
esac
if [[ -n "$CONTROL_FILE" ]]; then
  case "$CONTROL_FILE" in
    "$PROJ"/*) ;;
    *) echo "PIPELINE_CONTROL_FILE must be inside the project" >&2; exit 2 ;;
  esac
fi
if [[ ! "$FREE_SPACE_RESERVE_BYTES" =~ ^[0-9]+$ ]]; then
  echo "PIPELINE_FREE_SPACE_RESERVE_BYTES must be a non-negative integer" >&2
  exit 2
fi

if [[ ! -f "$INPUT" ]]; then
  echo "Input not found: $INPUT" >&2
  exit 1
fi
if [[ "$TAG" == */* || "$TAG" == *..* ]]; then
  echo "Tag must be a simple directory name: $TAG" >&2
  exit 2
fi
if (( BATCH < 1 || (BATCH - 1) % 4 != 0 )); then
  echo "SEEDVR2_BATCH must be 1, 5, 9, 13, ...; got $BATCH" >&2
  exit 2
fi

WORK=$WORK_ROOT/$TAG
PREPARED=$WORK/input_prep_ffv1.mkv
RESTORED=$WORK/seedvr2_${RESOLUTION}p.mp4
BASELINE=$WORK/baseline_${RESOLUTION}p.mkv
LOG=$WORK/seedvr2.log
PREPARED_PART=$WORK/input_prep_ffv1.partial.mkv
RESTORED_PART=$WORK/seedvr2_${RESOLUTION}p.partial.mp4
BASELINE_PART=$WORK/baseline_${RESOLUTION}p.partial.mkv
OUTPUT_PART=${OUTPUT%.*}.partial.${OUTPUT##*.}
mkdir -p "$WORK" "$(dirname "$OUTPUT")" "$PROJ/models/seedvr2"

PROJECT_MOUNTS=(-v "$PROJ:/proj")

emit_event() {
  printf 'PIPELINE_EVENT {"type":"%s","stage":"%s"}\n' "$1" "$2"
}

check_cancel() {
  local stage=$1
  if [[ -n "$CONTROL_FILE" && -f "$CONTROL_FILE" ]]; then
    emit_event "cancelled_at_boundary" "$stage"
    echo "Cancellation honored at pipeline stage boundary: $stage"
    exit 75
  fi
}

check_free_space() {
  local stage=$1 available
  available=$(df --output=avail -B1 "$WORK_ROOT" | tail -n 1 | tr -d ' ')
  if (( available < FREE_SPACE_RESERVE_BYTES )); then
    emit_event "safeguard_failed" "$stage"
    echo "Free-space reserve would be crossed before stage $stage" >&2
    exit 76
  fi
}

# Build the stage-1 filter chain. Crop the 4:3 window, denoise lightly, then
# (unless VHS_INPLACE) downscale toward native so SeedVR2 gets a real LR->HR job.
build_prep_vf() {
  local vf
  if [[ -n "$VHS_CROP" ]]; then
    vf="crop=$VHS_CROP"
  else
    # Centred 4:3 of the full frame height; crop auto-centres x when omitted.
    vf="crop=w=trunc(ih*4/3/2)*2:h=ih"
  fi
  if [[ -n "$VHS_DENOISE" && "$VHS_DENOISE" != none ]]; then
    vf="$vf,hqdn3d=$VHS_DENOISE"
  fi
  if [[ "$VHS_INPLACE" != 1 ]]; then
    vf="$vf,scale=-2:$VHS_PREP_HEIGHT:in_range=tv:out_range=tv:flags=lanczos"
  fi
  # setsar=1 (square pixels) + setfield=prog: the source is progressive, but the
  # FFV1 encoder otherwise tags the stream interlaced (field_order=tb), which
  # could tempt a downstream tool to deinterlace already-progressive frames.
  vf="$vf,setsar=1,setfield=prog"
  printf '%s' "$vf"
}

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "[build] $IMAGE"
  docker build -t "$IMAGE" "$PROJ/docker/seedvr2"
fi

if [[ "$FORCE" == 1 ]]; then
  rm -f "$PREPARED" "$RESTORED" "$BASELINE" "$LOG" "$OUTPUT" \
    "$PREPARED_PART" "$RESTORED_PART" "$BASELINE_PART" "$OUTPUT_PART"
fi

check_cancel "prepare_vhs"
check_free_space "prepare_vhs"
emit_event "stage_start" "prepare_vhs"
if [[ ! -s "$PREPARED" ]]; then
  PREP_VF=$(build_prep_vf)
  echo "[1/4] VHS front-end: $PREP_VF -> FFV1 ${VHS_FPS}p"
  rm -f "$PREPARED_PART"
  docker run --rm \
    "${PROJECT_MOUNTS[@]}" \
    --entrypoint ffmpeg "$FFMPEG_IMAGE" \
    -hide_banner -loglevel warning -stats -y \
    -ss "$SS" -t "$DUR" -i "/proj/${INPUT#"$PROJ/"}" \
    -map 0:v:0 -an \
    -vf "$PREP_VF" \
    -r "$VHS_FPS" -c:v ffv1 -level 3 -coder 1 -context 1 -g 1 -pix_fmt yuv444p \
    -colorspace bt709 -color_primaries bt709 -color_trc bt709 -color_range tv \
    "/proj/${PREPARED_PART#"$PROJ/"}"
  mv -f "$PREPARED_PART" "$PREPARED"
else
  echo "[1/4] reuse $PREPARED"
fi
emit_event "stage_complete" "prepare_vhs"
check_cancel "prepare_vhs"

# ── Prepare-only mode ────────────────────────────────────────────────────────
if [[ "${PREPARE_ONLY:-0}" == 1 ]]; then
  echo "PREPARE DONE $PREPARED"
  exit 0
fi

# ── Durable-unit mode ────────────────────────────────────────────────────────
# Identical to pipeline_v3.sh: when UNIT_OUTPUT is set, restore exactly ONE unit
# of the prepared progressive input as a standalone HEVC file and exit. Stage 1
# is reused across a job's units (same WORK dir). A driver plans the units by
# frame index and assembles them (assemble_units.sh).
if [[ -n "${UNIT_OUTPUT:-}" ]]; then
  case "$UNIT_OUTPUT" in
    /*) UOUT=$UNIT_OUTPUT ;;
    *)  UOUT=$PROJ/$UNIT_OUTPUT ;;
  esac
  case "$UOUT" in
    "$PROJ"/*) ;;
    *) echo "UNIT_OUTPUT must be inside the project: $UOUT" >&2; exit 2 ;;
  esac
  : "${UNIT_LOAD_CAP:?UNIT_LOAD_CAP is required in unit mode}"
  UNIT_SKIP=${UNIT_SKIP:-0}
  UNIT_PREPEND=${UNIT_PREPEND:-0}
  UNIT_DROP=${UNIT_DROP:-0}
  UOUT_PART=${UOUT%.*}.partial.${UOUT##*.}
  mkdir -p "$(dirname "$UOUT")"
  check_free_space "seedvr2_restore"
  emit_event "stage_start" "seedvr2_restore"
  if [[ -s "$UOUT" ]]; then
    echo "[unit] reuse $UOUT"
    emit_event "stage_complete" "seedvr2_restore"
    echo "UNIT DONE $UOUT"
    exit 0
  fi
  echo "[unit] skip=$UNIT_SKIP cap=$UNIT_LOAD_CAP prepend=$UNIT_PREPEND drop=$UNIT_DROP -> $UOUT"
  seedvr2_unit_args \
    "/proj/${PREPARED#"$PROJ/"}" \
    "/proj/${UOUT_PART#"$PROJ/"}" \
    /proj/models/seedvr2 \
    "$MODEL" "$RESOLUTION" "$BATCH" "$OVERLAP" \
    "$UNIT_SKIP" "$UNIT_LOAD_CAP" "$UNIT_PREPEND" "$UNIT_DROP" \
    ${COMPILE_ARGS[@]+"${COMPILE_ARGS[@]}"}
  rm -f "$UOUT_PART"
  RESTORE_CONTAINER="wedding-vhs-${TAG}"
  docker rm -f "$RESTORE_CONTAINER" >/dev/null 2>&1 || true
  set +e
  docker run --rm --name "$RESTORE_CONTAINER" --gpus all --ipc=host \
    ${MEM_ARGS[@]+"${MEM_ARGS[@]}"} \
    -e NVIDIA_DRIVER_CAPABILITIES=compute,utility,video \
    -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    ${CACHE_ARGS[@]+"${CACHE_ARGS[@]}"} \
    "${PROJECT_MOUNTS[@]}" \
    "$IMAGE" \
    "${SEEDVR2_UNIT_ARGV[@]}" 2>&1 | tee "$LOG"
  unit_status=${PIPESTATUS[0]}
  set -e
  if (( unit_status != 0 )); then
    echo "SeedVR2 unit failed; see $LOG" >&2
    exit "$unit_status"
  fi
  mv -f "$UOUT_PART" "$UOUT"
  emit_event "stage_complete" "seedvr2_restore"
  echo "UNIT DONE $UOUT"
  exit 0
fi
# ─────────────────────────────────────────────────────────────────────────────

check_free_space "baseline_encode"
emit_event "stage_start" "baseline_encode"
if [[ "$SKIP_BASELINE" == 1 ]]; then
  echo "[2/4] non-AI baseline encode skipped (SKIP_BASELINE=1)"
elif [[ ! -s "$BASELINE" ]]; then
  echo "[2/4] non-AI ${RESOLUTION}p Lanczos baseline (isolates the AI's contribution)"
  rm -f "$BASELINE_PART"
  docker run --rm \
    "${PROJECT_MOUNTS[@]}" \
    --entrypoint ffmpeg "$FFMPEG_IMAGE" \
    -hide_banner -loglevel warning -stats -y \
    -i "/proj/${PREPARED#"$PROJ/"}" \
    -vf "scale=-2:$RESOLUTION:in_range=tv:out_range=tv:flags=lanczos,format=yuv420p10le" \
    -an -c:v libx265 -preset slow -crf 14 \
    -colorspace bt709 -color_primaries bt709 -color_trc bt709 -color_range tv \
    "/proj/${BASELINE_PART#"$PROJ/"}"
  mv -f "$BASELINE_PART" "$BASELINE"
else
  echo "[2/4] reuse $BASELINE"
fi
emit_event "stage_complete" "baseline_encode"
check_cancel "baseline_encode"

check_free_space "seedvr2_restore"
emit_event "stage_start" "seedvr2_restore"
if [[ ! -s "$RESTORED" ]]; then
  echo "[3/4] SeedVR2 temporal restoration ($MODEL, ${RESOLUTION}px short side)"
  rm -f "$RESTORED_PART"
  set +e
  RESTORE_CONTAINER="wedding-vhs-${TAG}"
  docker rm -f "$RESTORE_CONTAINER" >/dev/null 2>&1 || true
  docker run --rm --name "$RESTORE_CONTAINER" --gpus all --ipc=host \
    ${MEM_ARGS[@]+"${MEM_ARGS[@]}"} \
    -e NVIDIA_DRIVER_CAPABILITIES=compute,utility,video \
    -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    ${CACHE_ARGS[@]+"${CACHE_ARGS[@]}"} \
    "${PROJECT_MOUNTS[@]}" \
    "$IMAGE" \
    "/proj/${PREPARED#"$PROJ/"}" \
    --output "/proj/${RESTORED_PART#"$PROJ/"}" \
    --model_dir /proj/models/seedvr2 \
    --dit_model "$MODEL" \
    --resolution "$RESOLUTION" \
    --batch_size "$BATCH" --uniform_batch_size \
    --chunk_size "$CHUNK" --temporal_overlap "$OVERLAP" --prepend_frames 4 \
    --color_correction lab \
    --vae_encode_tiled --vae_decode_tiled \
    --video_backend ffmpeg --10bit --debug \
    ${COMPILE_ARGS[@]+"${COMPILE_ARGS[@]}"} 2>&1 | tee "$LOG"
  seedvr_status=${PIPESTATUS[0]}
  set -e
  if (( seedvr_status != 0 )); then
    echo "SeedVR2 failed; see $LOG" >&2
    exit "$seedvr_status"
  fi
  mv -f "$RESTORED_PART" "$RESTORED"
else
  echo "[3/4] reuse $RESTORED"
fi
emit_event "stage_complete" "seedvr2_restore"
check_cancel "seedvr2_restore"

check_free_space "audio_mux"
emit_event "stage_start" "audio_mux"
if [[ ! -s "$OUTPUT" ]]; then
  rm -f "$OUTPUT_PART"
  if [[ "$VHS_AUDIO" == none ]]; then
    echo "[4/4] no audio (VHS_AUDIO=none): remux restored video to MKV"
    docker run --rm \
      "${PROJECT_MOUNTS[@]}" \
      --entrypoint ffmpeg "$FFMPEG_IMAGE" \
      -hide_banner -loglevel warning -stats -y \
      -i "/proj/${RESTORED#"$PROJ/"}" \
      -map 0:v:0 -an -c:v copy \
      -metadata title="VHS restoration — SeedVR2" \
      "/proj/${OUTPUT_PART#"$PROJ/"}"
  elif [[ "$VHS_AUDIO" == copy ]]; then
    echo "[4/4] mux restored video with stream-copied source audio"
    docker run --rm \
      "${PROJECT_MOUNTS[@]}" \
      --entrypoint ffmpeg "$FFMPEG_IMAGE" \
      -hide_banner -loglevel warning -stats -y \
      -i "/proj/${RESTORED#"$PROJ/"}" \
      -ss "$SS" -t "$DUR" -i "/proj/${INPUT#"$PROJ/"}" \
      -map 0:v:0 -map 1:a:0 -c:v copy -c:a copy -shortest \
      -metadata title="VHS restoration — SeedVR2" \
      "/proj/${OUTPUT_PART#"$PROJ/"}"
  else
    echo "[4/4] mux restored video with sample-accurate archival FLAC audio"
    # Seek a few seconds early, decode, then trim to the exact start in PCM so
    # the AAC decoder never starts mid-frame (guarantees A/V sync on a slice).
    read -r AUDIO_SEEK AUDIO_TRIM AUDIO_READ < <(
      awk -v start="$SS" -v duration="$DUR" 'BEGIN {
        preroll = (start < 5 ? start : 5)
        printf "%.6f %.6f %.6f\n", start - preroll, preroll, duration + preroll
      }'
    )
    docker run --rm \
      "${PROJECT_MOUNTS[@]}" \
      --entrypoint ffmpeg "$FFMPEG_IMAGE" \
      -hide_banner -loglevel warning -stats -y \
      -i "/proj/${RESTORED#"$PROJ/"}" \
      -ss "$AUDIO_SEEK" -t "$AUDIO_READ" -i "/proj/${INPUT#"$PROJ/"}" \
      -map 0:v:0 -map 1:a:0 -c:v copy \
      -af "atrim=start=$AUDIO_TRIM:duration=$DUR,asetpts=N/SR/TB" \
      -c:a flac -sample_fmt s16 -compression_level 8 -shortest \
      -metadata title="VHS restoration — SeedVR2" \
      "/proj/${OUTPUT_PART#"$PROJ/"}"
  fi
  mv -f "$OUTPUT_PART" "$OUTPUT"
else
  echo "[4/4] reuse $OUTPUT"
fi
emit_event "stage_complete" "audio_mux"
check_cancel "audio_mux"

emit_event "pipeline_complete" "complete"
echo "DONE"
echo "  restored: $OUTPUT"
echo "  baseline: $BASELINE"
echo "  log:      $LOG"

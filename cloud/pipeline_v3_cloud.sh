#!/usr/bin/env bash
# Cloud port of pipeline_v3.sh for RunPod pods.
#
# Identical pinned settings and stage commands as pipeline_v3.sh, but every
# stage runs natively inside the pod (cloud GPU pods are already containers,
# so the local script's docker-run wrappers cannot be used). The environment
# is prepared by bootstrap_pod.sh to match the local seedvr2-cuda:v3 image:
# same base image, same ffmpeg 4.4.2, same pinned SeedVR2 commit and patches,
# same pip versions.
#
# Usage:
#   ./pipeline_v3_cloud.sh <input.vob> <start_sec> <duration_sec> <output.mkv> [tag]
#
# Differences from pipeline_v3.sh (deliberate, cloud-only):
#   - No docker; no project-root path restrictions (pod scratch disk).
#   - SKIP_BASELINE=1 by default: the 1440p50 x265 comparison encode is a
#     local A/B artifact the user already approved; on a billed GPU pod it
#     only adds hours of CPU time. Set SKIP_BASELINE=0 to restore it.
set -euo pipefail

if (( $# < 4 || $# > 5 )); then
  echo "Usage: $0 <input.vob> <start_sec> <duration_sec> <output.mkv> [tag]" >&2
  exit 2
fi

IN=$1
SS=$2
DUR=$3
OUTPUT=$4
TAG=${5:-v3}

ROOT=${CLOUD_ROOT:-/workspace}
SEEDVR2_HOME=${SEEDVR2_HOME:-/opt/SeedVR2}
MODEL_DIR=${SEEDVR2_MODEL_DIR:-$ROOT/models/seedvr2}
MODEL=${SEEDVR2_MODEL:-seedvr2_ema_3b_fp16.safetensors}
RESOLUTION=${SEEDVR2_RESOLUTION:-1440}
BATCH=${SEEDVR2_BATCH:-129}
CHUNK=${SEEDVR2_CHUNK:-750}
OVERLAP=${SEEDVR2_OVERLAP:-4}
# VAE-only torch.compile, exactly as pipeline_v3.sh (DiT compile leaks VRAM;
# see docs/COMPILE_LEAK_INVESTIGATION.md).
COMPILE=${SEEDVR2_COMPILE:-1}
FORCE=${FORCE:-0}
SKIP_BASELINE=${SKIP_BASELINE:-1}
WORK_ROOT=${PIPELINE_WORK_ROOT:-$ROOT/work}
CONTROL_FILE=${PIPELINE_CONTROL_FILE:-}
FREE_SPACE_RESERVE_BYTES=${PIPELINE_FREE_SPACE_RESERVE_BYTES:-0}

INPUT=$IN
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
if [[ ! "$FREE_SPACE_RESERVE_BYTES" =~ ^[0-9]+$ ]]; then
  echo "PIPELINE_FREE_SPACE_RESERVE_BYTES must be a non-negative integer" >&2
  exit 2
fi

WORK=$WORK_ROOT/$TAG
DEINTERLACED=$WORK/input_50p_ffv1.mkv
RESTORED=$WORK/seedvr2_${RESOLUTION}p.mp4
BASELINE=$WORK/baseline_1440p50.mkv
LOG=$WORK/seedvr2.log
DEINTERLACED_PART=$WORK/input_50p_ffv1.partial.mkv
RESTORED_PART=$WORK/seedvr2_${RESOLUTION}p.partial.mp4
BASELINE_PART=$WORK/baseline_1440p50.partial.mkv
OUTPUT_PART=${OUTPUT%.*}.partial.${OUTPUT##*.}
mkdir -p "$WORK" "$(dirname "$OUTPUT")" "$MODEL_DIR"

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

if [[ "$FORCE" == 1 ]]; then
  rm -f "$DEINTERLACED" "$RESTORED" "$BASELINE" "$LOG" "$OUTPUT" \
    "$DEINTERLACED_PART" "$RESTORED_PART" "$BASELINE_PART" "$OUTPUT_PART"
fi

check_cancel "prepare_50p"
check_free_space "prepare_50p"
emit_event "stage_start" "prepare_50p"
if [[ ! -s "$DEINTERLACED" ]]; then
  echo "[1/4] BFF PAL -> square-pixel 768x576 50p FFV1"
  rm -f "$DEINTERLACED_PART"
  ffmpeg -hide_banner -loglevel warning -stats -y \
    -ss "$SS" -t "$DUR" -i "$INPUT" \
    -map 0:v:0 -an \
    -vf "bwdif=mode=send_field:parity=bff,scale=768:576:in_range=tv:in_color_matrix=bt470bg:flags=lanczos,setsar=1" \
    -r 50 -c:v ffv1 -level 3 -coder 1 -context 1 -g 1 -pix_fmt yuv444p \
    -colorspace bt470bg -color_primaries bt470bg -color_trc gamma28 -color_range tv \
    "$DEINTERLACED_PART"
  mv -f "$DEINTERLACED_PART" "$DEINTERLACED"
else
  echo "[1/4] reuse $DEINTERLACED"
fi
emit_event "stage_complete" "prepare_50p"
check_cancel "prepare_50p"

check_free_space "baseline_encode"
emit_event "stage_start" "baseline_encode"
if [[ "$SKIP_BASELINE" == 1 ]]; then
  echo "[2/4] baseline encode skipped (SKIP_BASELINE=1)"
elif [[ ! -s "$BASELINE" ]]; then
  echo "[2/4] faithful 1440p50 comparison encode"
  rm -f "$BASELINE_PART"
  ffmpeg -hide_banner -loglevel warning -stats -y \
    -i "$DEINTERLACED" \
    -vf "scale=1920:1440:in_range=tv:out_range=tv:in_color_matrix=bt470bg:out_color_matrix=bt709:flags=lanczos,format=yuv420p10le" \
    -an -c:v libx265 -preset slow -crf 14 \
    -colorspace bt709 -color_primaries bt709 -color_trc bt709 -color_range tv \
    "$BASELINE_PART"
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
  COMPILE_ARGS=()
  if [[ "$COMPILE" == 1 ]]; then
    COMPILE_ARGS=(--compile_vae --cache_vae)
  fi
  set +e
  PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  python "$SEEDVR2_HOME/inference_cli.py" \
    "$DEINTERLACED" \
    --output "$RESTORED_PART" \
    --model_dir "$MODEL_DIR" \
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
  echo "[4/4] mux restored video with sample-accurate archival FLAC audio"
  rm -f "$OUTPUT_PART"
  # Seek a few seconds early so the AC-3 decoder never starts on a partial
  # DVD packet, then trim to the requested sample-accurate start in PCM.
  read -r AUDIO_SEEK AUDIO_TRIM AUDIO_READ < <(
    awk -v start="$SS" -v duration="$DUR" 'BEGIN {
      preroll = (start < 5 ? start : 5)
      printf "%.6f %.6f %.6f\n", start - preroll, preroll, duration + preroll
    }'
  )
  ffmpeg -hide_banner -loglevel warning -stats -y \
    -i "$RESTORED" \
    -ss "$AUDIO_SEEK" -t "$AUDIO_READ" -i "$INPUT" \
    -map 0:v:0 -map 1:a:0 -c:v copy \
    -af "atrim=start=$AUDIO_TRIM:duration=$DUR,asetpts=N/SR/TB" \
    -c:a flac -sample_fmt s16 -compression_level 8 -shortest \
    -metadata title="Wedding DVD restoration — SeedVR2 50p" \
    "$OUTPUT_PART"
  mv -f "$OUTPUT_PART" "$OUTPUT"
else
  echo "[4/4] reuse $OUTPUT"
fi
emit_event "stage_complete" "audio_mux"
check_cancel "audio_mux"

emit_event "pipeline_complete" "complete"
echo "DONE"
echo "  restored: $OUTPUT"
echo "  log:      $LOG"

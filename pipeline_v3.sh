#!/usr/bin/env bash
# Quality-first PAL DVD restoration using 50p deinterlacing and SeedVR2.
#
# Usage:
#   ./pipeline_v3.sh <input.vob> <start_sec> <duration_sec> <output.mkv> [tag]
#
# The job is resumable. Completed stages are reused unless FORCE=1 is set.
# Existing v1/v2 outputs and work directories are never touched.
set -euo pipefail

if (( $# < 4 || $# > 5 )); then
  echo "Usage: $0 <input.vob> <start_sec> <duration_sec> <output.mkv> [tag]" >&2
  exit 2
fi

PROJ=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
IN=$1
SS=$2
DUR=$3
OUT=$4
TAG=${5:-v3}
IMAGE=seedvr2-cuda:v3
MODEL=${SEEDVR2_MODEL:-seedvr2_ema_3b_fp16.safetensors}
RESOLUTION=${SEEDVR2_RESOLUTION:-1440}
BATCH=${SEEDVR2_BATCH:-129}
CHUNK=${SEEDVR2_CHUNK:-750}
OVERLAP=${SEEDVR2_OVERLAP:-4}
FORCE=${FORCE:-0}

case "$IN" in
  /*) INPUT=$IN ;;
  *) INPUT=$PROJ/$IN ;;
esac
case "$OUT" in
  /*) OUTPUT=$OUT ;;
  *) OUTPUT=$PROJ/$OUT ;;
esac

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

WORK=$PROJ/work/$TAG
DEINTERLACED=$WORK/input_50p_ffv1.mkv
RESTORED=$WORK/seedvr2_${RESOLUTION}p.mp4
BASELINE=$WORK/baseline_1440p50.mkv
LOG=$WORK/seedvr2.log
DEINTERLACED_PART=$WORK/input_50p_ffv1.partial.mkv
RESTORED_PART=$WORK/seedvr2_${RESOLUTION}p.partial.mp4
BASELINE_PART=$WORK/baseline_1440p50.partial.mkv
OUTPUT_PART=${OUTPUT%.*}.partial.${OUTPUT##*.}
mkdir -p "$WORK" "$(dirname "$OUTPUT")" "$PROJ/models/seedvr2"

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "[build] $IMAGE"
  docker build -t "$IMAGE" "$PROJ/docker/seedvr2"
fi

if [[ "$FORCE" == 1 ]]; then
  rm -f "$DEINTERLACED" "$RESTORED" "$BASELINE" "$LOG" "$OUTPUT" \
    "$DEINTERLACED_PART" "$RESTORED_PART" "$BASELINE_PART" "$OUTPUT_PART"
fi

if [[ ! -s "$DEINTERLACED" ]]; then
  echo "[1/4] BFF PAL -> square-pixel 768x576 50p FFV1"
  rm -f "$DEINTERLACED_PART"
  docker run --rm \
    -v "$PROJ:/proj" \
    --entrypoint ffmpeg upscaler-cuda:latest \
    -hide_banner -loglevel warning -stats -y \
    -ss "$SS" -t "$DUR" -i "/proj/${INPUT#"$PROJ/"}" \
    -map 0:v:0 -an \
    -vf "bwdif=mode=send_field:parity=bff,scale=768:576:in_range=tv:in_color_matrix=bt470bg:flags=lanczos,setsar=1" \
    -r 50 -c:v ffv1 -level 3 -coder 1 -context 1 -g 1 -pix_fmt yuv444p \
    -colorspace bt470bg -color_primaries bt470bg -color_trc gamma28 -color_range tv \
    "/proj/${DEINTERLACED_PART#"$PROJ/"}"
  mv -f "$DEINTERLACED_PART" "$DEINTERLACED"
else
  echo "[1/4] reuse $DEINTERLACED"
fi

if [[ ! -s "$BASELINE" ]]; then
  echo "[2/4] faithful 1440p50 comparison encode"
  rm -f "$BASELINE_PART"
  docker run --rm \
    -v "$PROJ:/proj" \
    --entrypoint ffmpeg upscaler-cuda:latest \
    -hide_banner -loglevel warning -stats -y \
    -i "/proj/${DEINTERLACED#"$PROJ/"}" \
    -vf "scale=1920:1440:in_range=tv:out_range=tv:in_color_matrix=bt470bg:out_color_matrix=bt709:flags=lanczos,format=yuv420p10le" \
    -an -c:v libx265 -preset slow -crf 14 \
    -colorspace bt709 -color_primaries bt709 -color_trc bt709 -color_range tv \
    "/proj/${BASELINE_PART#"$PROJ/"}"
  mv -f "$BASELINE_PART" "$BASELINE"
else
  echo "[2/4] reuse $BASELINE"
fi

if [[ ! -s "$RESTORED" ]]; then
  echo "[3/4] SeedVR2 temporal restoration ($MODEL, ${RESOLUTION}px short side)"
  rm -f "$RESTORED_PART"
  set +e
  docker run --rm --gpus all --ipc=host \
    -e NVIDIA_DRIVER_CAPABILITIES=compute,utility,video \
    -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    -v "$PROJ:/proj" \
    "$IMAGE" \
    "/proj/${DEINTERLACED#"$PROJ/"}" \
    --output "/proj/${RESTORED_PART#"$PROJ/"}" \
    --model_dir /proj/models/seedvr2 \
    --dit_model "$MODEL" \
    --resolution "$RESOLUTION" \
    --batch_size "$BATCH" --uniform_batch_size \
    --chunk_size "$CHUNK" --temporal_overlap "$OVERLAP" --prepend_frames 4 \
    --color_correction lab \
    --vae_encode_tiled --vae_decode_tiled \
    --video_backend ffmpeg --10bit --debug 2>&1 | tee "$LOG"
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
  docker run --rm \
    -v "$PROJ:/proj" \
    --entrypoint ffmpeg upscaler-cuda:latest \
    -hide_banner -loglevel warning -stats -y \
    -i "/proj/${RESTORED#"$PROJ/"}" \
    -ss "$AUDIO_SEEK" -t "$AUDIO_READ" -i "/proj/${INPUT#"$PROJ/"}" \
    -map 0:v:0 -map 1:a:0 -c:v copy \
    -af "atrim=start=$AUDIO_TRIM:duration=$DUR,asetpts=N/SR/TB" \
    -c:a flac -sample_fmt s16 -compression_level 8 -shortest \
    -metadata title="Wedding DVD restoration — SeedVR2 50p" \
    "/proj/${OUTPUT_PART#"$PROJ/"}"
  mv -f "$OUTPUT_PART" "$OUTPUT"
else
  echo "[4/4] reuse $OUTPUT"
fi

echo "DONE"
echo "  restored: $OUTPUT"
echo "  baseline: $BASELINE"
echo "  log:      $LOG"

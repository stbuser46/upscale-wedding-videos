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
IMAGE=${SEEDVR2_IMAGE:-seedvr2-cuda:v3}
# Cap container RAM (and forbid swap growth) so a runaway restoration process is
# OOM-killed inside its own cgroup instead of exhausting host memory and freezing
# the whole machine — a SeedVR2 process spiked to ~144 GiB and froze the box on
# 2026-09-07. Normal units use ~9 GiB; 64g leaves generous headroom well under
# the host total. Override with SEEDVR2_MEM_LIMIT (e.g. 96g) if a unit ever needs
# more; setting it empty disables the cap.
MEM_LIMIT=${SEEDVR2_MEM_LIMIT-64g}
MEM_ARGS=()
if [[ -n "$MEM_LIMIT" ]]; then
  MEM_ARGS=(--memory="$MEM_LIMIT" --memory-swap="$MEM_LIMIT")
fi
MODEL=${SEEDVR2_MODEL:-seedvr2_ema_3b_fp16.safetensors}
RESOLUTION=${SEEDVR2_RESOLUTION:-1440}
BATCH=${SEEDVR2_BATCH:-129}
CHUNK=${SEEDVR2_CHUNK:-750}
OVERLAP=${SEEDVR2_OVERLAP:-4}
# torch.compile on the VAE ONLY (measured 1.20x steady-state, PSNR 49.0 dB
# vs the uncompiled reference — same quality class the user approved).
# Compiling the DiT as well is faster (1.28x) but leaks VRAM: its dynamic
# attention-window shapes force recompilation on the differently-shaped tail
# chunk, retaining ~27 GB and OOMing multi-chunk jobs (twice observed on
# 2026-08-04/05; see docs/COMPILE_LEAK_INVESTIGATION.md and the flat-memory
# acceptance run in work/compile_fix_test_20260805_025548). The VAE always
# sees fixed 1024px tiles, so its compilation is shape-stable: 60.1 GB peak,
# flat across chunks. SEEDVR2_COMPILE=0 restores the exact pre-compile path.
COMPILE=${SEEDVR2_COMPILE:-1}
COMPILE_ARGS=()
if [[ "$COMPILE" == 1 ]]; then
  COMPILE_ARGS=(--compile_vae --cache_vae)
fi
FORCE=${FORCE:-0}
# The stage-2 baseline is a non-AI 1440p50 x265 comparison encode. It is never
# muxed into the restored output and takes ~30 CPU-minutes per chapter, during
# which the GPU sits idle. SKIP_BASELINE=1 omits it for unattended production
# runs (quality of the restored result is unaffected). Default 0 keeps prior behaviour.
SKIP_BASELINE=${SKIP_BASELINE:-0}
WORK_ROOT=${PIPELINE_WORK_ROOT:-$PROJ/work}
CONTROL_FILE=${PIPELINE_CONTROL_FILE:-}
FREE_SPACE_RESERVE_BYTES=${PIPELINE_FREE_SPACE_RESERVE_BYTES:-0}

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
case "$INPUT" in
  "$PROJ"/*) ;;
  *) echo "Input must be inside the project: $INPUT" >&2; exit 2 ;;
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
DEINTERLACED=$WORK/input_50p_ffv1.mkv
RESTORED=$WORK/seedvr2_${RESOLUTION}p.mp4
BASELINE=$WORK/baseline_1440p50.mkv
LOG=$WORK/seedvr2.log
DEINTERLACED_PART=$WORK/input_50p_ffv1.partial.mkv
RESTORED_PART=$WORK/seedvr2_${RESOLUTION}p.partial.mp4
BASELINE_PART=$WORK/baseline_1440p50.partial.mkv
OUTPUT_PART=${OUTPUT%.*}.partial.${OUTPUT##*.}
mkdir -p "$WORK" "$(dirname "$OUTPUT")" "$PROJ/models/seedvr2"

PROJECT_MOUNTS=(-v "$PROJ:/proj")
if [[ "$INPUT" == "$PROJ/source/"* ]]; then
  # The project mount is writable for generated stages, but archival sources
  # are overmounted read-only in every media container.
  PROJECT_MOUNTS+=(-v "$PROJ/source:/proj/source:ro")
fi

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

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "[build] $IMAGE"
  docker build -t "$IMAGE" "$PROJ/docker/seedvr2"
fi

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
  docker run --rm \
    "${PROJECT_MOUNTS[@]}" \
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
emit_event "stage_complete" "prepare_50p"
check_cancel "prepare_50p"

# ── Durable-unit mode ────────────────────────────────────────────────────────
# When UNIT_OUTPUT is set, restore exactly ONE unit of the prepared 50p input as
# a standalone HEVC file and exit. Stage 1 above is reused across a job's units
# (same WORK dir). The worker orchestrates units, pause/yield between them, and
# final assembly (assemble_units.sh). Stages 2 (baseline) and 4 (mux) are skipped.
#   UNIT_SKIP      first source frame to read (unit_start - context)
#   UNIT_LOAD_CAP  frames to read (context + new frames for this unit)
#   UNIT_PREPEND   reversed warm-up frames (4 for unit 0, else 0) — auto-removed
#   UNIT_DROP      leading context outputs to discard (0 for unit 0, else context)
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
  rm -f "$UOUT_PART"
  RESTORE_CONTAINER="wedding-${TAG}"
  docker rm -f "$RESTORE_CONTAINER" >/dev/null 2>&1 || true
  set +e
  docker run --rm --name "$RESTORE_CONTAINER" --gpus all --ipc=host \
    ${MEM_ARGS[@]+"${MEM_ARGS[@]}"} \
    -e NVIDIA_DRIVER_CAPABILITIES=compute,utility,video \
    -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    "${PROJECT_MOUNTS[@]}" \
    "$IMAGE" \
    "/proj/${DEINTERLACED#"$PROJ/"}" \
    --output "/proj/${UOUT_PART#"$PROJ/"}" \
    --model_dir /proj/models/seedvr2 \
    --dit_model "$MODEL" \
    --resolution "$RESOLUTION" \
    --batch_size "$BATCH" --uniform_batch_size \
    --chunk_size 0 --temporal_overlap "$OVERLAP" \
    --skip_first_frames "$UNIT_SKIP" --load_cap "$UNIT_LOAD_CAP" \
    --prepend_frames "$UNIT_PREPEND" --drop_leading "$UNIT_DROP" \
    --color_correction lab \
    --vae_encode_tiled --vae_decode_tiled \
    --video_backend ffmpeg --10bit --debug \
    ${COMPILE_ARGS[@]+"${COMPILE_ARGS[@]}"} 2>&1 | tee "$LOG"
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
  echo "[2/4] baseline comparison encode skipped (SKIP_BASELINE=1)"
elif [[ ! -s "$BASELINE" ]]; then
  echo "[2/4] faithful 1440p50 comparison encode"
  rm -f "$BASELINE_PART"
  docker run --rm \
    "${PROJECT_MOUNTS[@]}" \
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
emit_event "stage_complete" "baseline_encode"
check_cancel "baseline_encode"

check_free_space "seedvr2_restore"
emit_event "stage_start" "seedvr2_restore"
if [[ ! -s "$RESTORED" ]]; then
  echo "[3/4] SeedVR2 temporal restoration ($MODEL, ${RESOLUTION}px short side)"
  rm -f "$RESTORED_PART"
  set +e
  # Deterministic container name so the worker can stop/remove this GPU
  # container on abnormal exit (systemd stop, crash) instead of orphaning
  # ~60 GB of VRAM. Clear any stale same-named container from a prior crash.
  RESTORE_CONTAINER="wedding-${TAG}"
  docker rm -f "$RESTORE_CONTAINER" >/dev/null 2>&1 || true
  docker run --rm --name "$RESTORE_CONTAINER" --gpus all --ipc=host \
    ${MEM_ARGS[@]+"${MEM_ARGS[@]}"} \
    -e NVIDIA_DRIVER_CAPABILITIES=compute,utility,video \
    -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    "${PROJECT_MOUNTS[@]}" \
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
  docker run --rm \
    "${PROJECT_MOUNTS[@]}" \
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
emit_event "stage_complete" "audio_mux"
check_cancel "audio_mux"

emit_event "pipeline_complete" "complete"
echo "DONE"
echo "  restored: $OUTPUT"
echo "  baseline: $BASELINE"
echo "  log:      $LOG"

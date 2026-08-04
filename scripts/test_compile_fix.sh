#!/usr/bin/env bash
# GPU acceptance test for the SeedVR2 torch.compile streaming fix.
# Run only when the restoration queue and GPU are idle.
set -euo pipefail

PROJ=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DB="$PROJ/webapp/data/catalog.sqlite"
SOURCE="$PROJ/work/dvd1_title.vob"
IMAGE=${SEEDVR2_COMPILE_FIX_IMAGE:-seedvr2-cuda:v3}
MEDIA_IMAGE=${SEEDVR2_MEDIA_IMAGE:-upscaler-cuda:latest}
MODEL=${SEEDVR2_MODEL:-seedvr2_ema_3b_fp16.safetensors}
START_SEC=${SEEDVR2_TEST_START:-5772}
DURATION_SEC=${SEEDVR2_TEST_DURATION:-40}
EXPECTED_FPS=50
CHUNK_SIZE=750
MAX_IDLE_VRAM_MIB=5120
MAX_CHUNK_GROWTH_MIB=10240

if [[ ! "$START_SEC" =~ ^[0-9]+$ || ! "$DURATION_SEC" =~ ^[1-9][0-9]*$ ]]; then
  echo "SEEDVR2_TEST_START and SEEDVR2_TEST_DURATION must be integer seconds" >&2
  exit 2
fi
EXPECTED_FRAMES=$((DURATION_SEC * EXPECTED_FPS))
EXPECTED_CHUNKS=$(((EXPECTED_FRAMES + CHUNK_SIZE - 1) / CHUNK_SIZE))
if (( EXPECTED_CHUNKS < 3 )); then
  echo "Test duration must produce at least three chunks; got $EXPECTED_CHUNKS" >&2
  exit 2
fi

for command in docker nvidia-smi python3 realpath; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Required command not found: $command" >&2
    exit 2
  fi
done

if [[ ! -r "$DB" ]]; then
  echo "Catalog database is not readable: $DB" >&2
  exit 2
fi
if [[ ! -r "$SOURCE" ]]; then
  echo "Test source is not readable: $SOURCE" >&2
  exit 2
fi
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "SeedVR2 image not found: $IMAGE" >&2
  exit 2
fi
if ! docker image inspect "$MEDIA_IMAGE" >/dev/null 2>&1; then
  echo "Media image not found: $MEDIA_IMAGE" >&2
  exit 2
fi

preflight() {
  python3 - "$DB" <<'PY'
import sqlite3
import sys
from pathlib import Path

db_path = Path(sys.argv[1]).resolve()
connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
try:
    active = connection.execute(
        "SELECT public_id, state, COALESCE(stage, '') "
        "FROM jobs WHERE state IN ('preparing', 'running', 'assembling') "
        "ORDER BY id"
    ).fetchall()
finally:
    connection.close()

if active:
    for public_id, state, stage in active:
        print(f"Refusing to start: webapp job {public_id} is {state} ({stage})", file=sys.stderr)
    raise SystemExit(1)
PY

  local gpu_index=0
  local used_mib
  while IFS= read -r used_mib; do
    used_mib=${used_mib//[[:space:]]/}
    if [[ ! "$used_mib" =~ ^[0-9]+$ ]]; then
      echo "Could not parse nvidia-smi memory usage for GPU $gpu_index: $used_mib" >&2
      return 1
    fi
    if (( used_mib > MAX_IDLE_VRAM_MIB )); then
      echo "Refusing to start: GPU $gpu_index is using ${used_mib} MiB (> ${MAX_IDLE_VRAM_MIB} MiB)" >&2
      return 1
    fi
    gpu_index=$((gpu_index + 1))
  done < <(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits)

  if (( gpu_index == 0 )); then
    echo "Refusing to start: nvidia-smi reported no GPUs" >&2
    return 1
  fi
}

echo "[preflight] checking queue and GPU occupancy"
preflight

if [[ -n "${COMPILE_FIX_TEST_DIR:-}" ]]; then
  TEST_DIR=$(realpath -m "$COMPILE_FIX_TEST_DIR")
  case "$TEST_DIR" in
    "$PROJ"/work/*) ;;
    *) echo "COMPILE_FIX_TEST_DIR must be inside $PROJ/work" >&2; exit 2 ;;
  esac
else
  TEST_DIR="$PROJ/work/compile_fix_test_$(date +%Y%m%d_%H%M%S)"
fi

if [[ -e "$TEST_DIR" ]]; then
  echo "Refusing to overwrite existing test directory: $TEST_DIR" >&2
  exit 2
fi
mkdir -p "$TEST_DIR"

INPUT="$TEST_DIR/input_50p_ffv1.mkv"
OUTPUT="$TEST_DIR/seedvr2_1440p.mp4"
RUN_LOG="$TEST_DIR/seedvr2.log"
VRAM_LOG="$TEST_DIR/vram.csv"

echo "[prepare] extracting ${DURATION_SEC}s at ${START_SEC}s to 50 fps FFV1"
docker run --rm \
  -v "$PROJ:/proj" \
  --entrypoint ffmpeg "$MEDIA_IMAGE" \
  -hide_banner -loglevel warning -stats -y \
  -ss "$START_SEC" -t "$DURATION_SEC" -i "/proj/work/dvd1_title.vob" \
  -map 0:v:0 -an \
  -vf "bwdif=mode=send_field:parity=bff,scale=768:576:in_range=tv:in_color_matrix=bt470bg:flags=lanczos,setsar=1" \
  -r "$EXPECTED_FPS" -c:v ffv1 -level 3 -coder 1 -context 1 -g 1 -pix_fmt yuv444p \
  -colorspace bt470bg -color_primaries bt470bg -color_trc gamma28 -color_range tv \
  "/proj/${INPUT#"$PROJ/"}"

probe_frames() {
  local media_path=$1
  docker run --rm \
    -v "$PROJ:/proj:ro" \
    --entrypoint ffprobe "$MEDIA_IMAGE" \
    -v error -select_streams v:0 -count_frames \
    -show_entries stream=nb_read_frames \
    -of default=nokey=1:noprint_wrappers=1 \
    "/proj/${media_path#"$PROJ/"}"
}

input_frames=$(probe_frames "$INPUT" | tr -d '[:space:]')
if [[ "$input_frames" != "$EXPECTED_FRAMES" ]]; then
  echo "Prepared input has $input_frames frames; expected $EXPECTED_FRAMES" >&2
  exit 1
fi

# The CPU preparation can take long enough for queue state to change. Check
# again immediately before granting the test access to the GPU.
echo "[preflight] rechecking immediately before GPU launch"
preflight

printf 'epoch,wall_time,chunk,memory_used_mib,gpu_util_percent\n' >"$VRAM_LOG"
monitor_vram() {
  local chunk stats used util
  while true; do
    chunk=$(grep -oE 'Chunk [0-9]+/[0-9]+' "$RUN_LOG" 2>/dev/null | tail -n 1 | awk '{print $2}' | cut -d/ -f1 || true)
    chunk=${chunk:-0}
    stats=$(nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader,nounits | head -n 1)
    used=${stats%%,*}
    util=${stats#*,}
    used=${used//[[:space:]]/}
    util=${util//[[:space:]]/}
    printf '%s,%s,%s,%s,%s\n' \
      "$(date +%s)" "$(date +%Y-%m-%dT%H:%M:%S%z)" "$chunk" "$used" "$util" >>"$VRAM_LOG"
    sleep 20
  done
}

: >"$RUN_LOG"
monitor_pid=""
stop_monitor() {
  if [[ -n "$monitor_pid" ]] && kill -0 "$monitor_pid" 2>/dev/null; then
    kill "$monitor_pid" 2>/dev/null || true
    wait "$monitor_pid" 2>/dev/null || true
  fi
  monitor_pid=""
}
trap stop_monitor EXIT INT TERM
monitor_vram &
monitor_pid=$!

echo "[restore] running $EXPECTED_CHUNKS chunks with compile + persistent compiled-model cache"
set +e
docker run --rm --gpus all --ipc=host \
  -e NVIDIA_DRIVER_CAPABILITIES=compute,utility,video \
  -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  -v "$PROJ:/proj" \
  "$IMAGE" \
  "/proj/${INPUT#"$PROJ/"}" \
  --output "/proj/${OUTPUT#"$PROJ/"}" \
  --model_dir /proj/models/seedvr2 \
  --dit_model "$MODEL" \
  --resolution 1440 \
  --batch_size 129 --uniform_batch_size \
  --chunk_size "$CHUNK_SIZE" --temporal_overlap 4 --prepend_frames 4 \
  --color_correction lab \
  --vae_encode_tiled --vae_decode_tiled \
  --video_backend ffmpeg --10bit --debug \
  --compile_dit --compile_vae --cache_dit --cache_vae 2>&1 | tee -a "$RUN_LOG"
restore_status=${PIPESTATUS[0]}
set -e
stop_monitor
trap - EXIT INT TERM

if (( restore_status != 0 )); then
  echo "SeedVR2 failed with status $restore_status; see $RUN_LOG" >&2
  exit "$restore_status"
fi
if [[ ! -s "$OUTPUT" ]]; then
  echo "SeedVR2 returned success but output is missing or empty: $OUTPUT" >&2
  exit 1
fi
if ! grep -Fq "Streaming complete: $EXPECTED_FRAMES frames in $EXPECTED_CHUNKS chunks" "$RUN_LOG"; then
  echo "Completion/frame summary missing from SeedVR2 log" >&2
  exit 1
fi
if ! grep -Fq "All upscaling processes completed successfully" "$RUN_LOG"; then
  echo "SeedVR2 success marker missing from log" >&2
  exit 1
fi

output_frames=$(probe_frames "$OUTPUT" | tr -d '[:space:]')
if [[ "$output_frames" != "$EXPECTED_FRAMES" ]]; then
  echo "Output has $output_frames frames; expected $EXPECTED_FRAMES after prepend/context removal" >&2
  exit 1
fi

chunk_peak() {
  local wanted_chunk=$1
  awk -F, -v wanted="$wanted_chunk" '
    NR > 1 && $3 == wanted {
      found = 1
      if ($4 > peak) peak = $4
    }
    END {
      if (!found) exit 1
      print peak
    }
  ' "$VRAM_LOG"
}

chunk1_peak=$(chunk_peak 1) || {
  echo "No VRAM samples were associated with chunk 1; see $VRAM_LOG" >&2
  exit 1
}
chunk3_peak=$(chunk_peak 3) || {
  echo "No VRAM samples were associated with chunk 3; see $VRAM_LOG" >&2
  exit 1
}
growth_mib=$((chunk3_peak - chunk1_peak))
if (( growth_mib > MAX_CHUNK_GROWTH_MIB )); then
  echo "VRAM growth check failed: chunk 1 peak=${chunk1_peak} MiB, chunk 3 peak=${chunk3_peak} MiB (+${growth_mib} MiB)" >&2
  exit 1
fi

echo "PASS: $output_frames frames across $EXPECTED_CHUNKS chunks"
echo "PASS: chunk 1 peak=${chunk1_peak} MiB, chunk 3 peak=${chunk3_peak} MiB (growth=${growth_mib} MiB)"
echo "Output: $OUTPUT"
echo "SeedVR2 log: $RUN_LOG"
echo "VRAM log: $VRAM_LOG"

#!/usr/bin/env bash
# Assemble durable restoration units into the final archival MKV.
#
#   assemble_units.sh <units_manifest> <input.vob> <start_sec> <duration_sec> <output.mkv>
#
# <units_manifest> is a text file listing one validated unit video per line in
# sequence order (bare paths, project-relative or absolute). The units are HEVC
# 10-bit with closed GOPs, so they are concatenated by STREAM COPY (no re-encode
# — bit-exact, proven in the concat spike) into a video-only stream, then muxed
# with sample-accurate 48 kHz FLAC decoded once from the source, exactly like
# pipeline_v3.sh stage 4. The restored frames are never re-encoded here.
set -euo pipefail

if (( $# != 5 )); then
  echo "Usage: $0 <units_manifest> <input.vob> <start_sec> <duration_sec> <output.mkv>" >&2
  exit 2
fi

PROJ=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MANIFEST=$1
IN=$2
SS=$3
DUR=$4
OUT=$5
OUT_PART="${OUT%.mkv}.partial.mkv"
FFMPEG_IMAGE=${WEBAPP_FFMPEG_IMAGE:-upscaler-cuda:latest}

# Mount the project read-write so the container can read units + write output.
PROJECT_MOUNTS=(-v "$PROJ":/proj)

VIDEO_CONCAT="${OUT%.mkv}.video.mkv"
CONCAT_LIST="${OUT%.mkv}.concat.txt"

# Build an ffmpeg concat list of absolute in-container paths, preserving order.
: > "$CONCAT_LIST"
unit_count=0
while IFS= read -r unit; do
  [[ -z "$unit" ]] && continue
  # Resolve to an absolute host path, then map into the /proj mount.
  case "$unit" in
    /*) abs="$unit" ;;
    *)  abs="$PROJ/$unit" ;;
  esac
  if [[ ! -s "$abs" ]]; then
    echo "assemble: missing or empty unit: $abs" >&2
    exit 1
  fi
  printf "file '/proj/%s'\n" "${abs#"$PROJ/"}" >> "$CONCAT_LIST"
  unit_count=$((unit_count + 1))
done < "$MANIFEST"

if (( unit_count == 0 )); then
  echo "assemble: manifest lists no units" >&2
  exit 1
fi
echo "[assemble] concatenating $unit_count units by stream copy"

rm -f "$VIDEO_CONCAT"
docker run --rm "${PROJECT_MOUNTS[@]}" \
  --entrypoint ffmpeg "$FFMPEG_IMAGE" \
  -hide_banner -loglevel warning -y \
  -f concat -safe 0 -i "/proj/${CONCAT_LIST#"$PROJ/"}" \
  -c copy "/proj/${VIDEO_CONCAT#"$PROJ/"}"

echo "[assemble] muxing sample-accurate archival FLAC audio"
# Same pre-roll trick as pipeline_v3.sh stage 4: seek a few seconds early so the
# AC-3 decoder never starts on a partial DVD packet, then trim in PCM.
read -r AUDIO_SEEK AUDIO_TRIM AUDIO_READ < <(
  awk -v start="$SS" -v duration="$DUR" 'BEGIN {
    preroll = (start < 5 ? start : 5)
    printf "%.6f %.6f %.6f\n", start - preroll, preroll, duration + preroll
  }'
)
rm -f "$OUT_PART"
docker run --rm "${PROJECT_MOUNTS[@]}" \
  --entrypoint ffmpeg "$FFMPEG_IMAGE" \
  -hide_banner -loglevel warning -stats -y \
  -i "/proj/${VIDEO_CONCAT#"$PROJ/"}" \
  -ss "$AUDIO_SEEK" -t "$AUDIO_READ" -i "/proj/${IN#"$PROJ/"}" \
  -map 0:v:0 -map 1:a:0 -c:v copy \
  -af "atrim=start=$AUDIO_TRIM:duration=$DUR,asetpts=N/SR/TB" \
  -c:a flac -sample_fmt s16 -compression_level 8 -shortest \
  -metadata title="Wedding DVD restoration — SeedVR2 50p" \
  "/proj/${OUT_PART#"$PROJ/"}"

mv -f "$OUT_PART" "$OUT"
rm -f "$VIDEO_CONCAT" "$CONCAT_LIST"
echo "[assemble] done: $OUT"

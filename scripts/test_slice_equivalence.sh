#!/usr/bin/env bash
# Acceptance test A1: prove that a stream-copy slice of the stage-1 file feeds
# SeedVR2 exactly the frames a whole-file read would.
#
# Today a unit reads the whole ~30 GB stage-1 intermediate and lets the model
# skip into it:
#     inference_cli.py input_50p_ffv1.mkv --skip_first_frames N --load_cap C
# For cloud execution we instead ship only that unit's frames, and the pod runs:
#     inference_cli.py slice.mkv --skip_first_frames 0 --load_cap C
#
# Those are equivalent only if the slice holds byte-identical decoded frames.
# The stage-1 encode is FFV1 with `-g 1`, so every frame is a keyframe and a
# stream copy can cut on an exact frame boundary. This test proves it by
# comparing per-frame MD5s of the decoded video, which is CPU-only: it needs no
# GPU and therefore cannot disturb a running restoration or trip the idle gate.
#
# Usage: scripts/test_slice_equivalence.sh [input.mkv] [fps]
#
# fps defaults to the input's real frame rate (50 for PAL 50p, 60000/1001≈59.94
# for NTSC 59.94p). The slice seek is `-ss skip/fps`, so this MUST match the
# stage-1 output rate or the cut lands on the wrong frame — the exact bug the
# production slicer had when it hardcoded 50 for an NTSC job. Pass fps explicitly
# to prove the failure mode (e.g. `... input.mkv 50` on an NTSC file must FAIL).
set -euo pipefail

PROJ=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
INPUT=${1:-$PROJ/work/verifycache/input_50p_ffv1.mkv}
WORK=$(mktemp -d /tmp/slice-equiv.XXXXXX)
trap 'rm -rf "$WORK"' EXIT

if [[ ! -s "$INPUT" ]]; then
  echo "input not found: $INPUT" >&2
  exit 1
fi

RFR=$(ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate \
  -of default=noprint_wrappers=1:nokey=1 "$INPUT")
# Default fps to the file's real rate (e.g. 60000/1001); allow an explicit
# override as $2 so a mismatched rate can be exercised on purpose.
FPS=${2:-$(awk -v r="$RFR" 'BEGIN{split(r,a,"/"); printf "%.6f", a[2]?a[1]/a[2]:a[1]}')}

echo "input: $INPUT"
echo "fps:   $FPS (input r_frame_rate=$RFR)"
ffprobe -v error -select_streams v:0 \
  -show_entries stream=codec_name,pix_fmt,width,height,r_frame_rate \
  -of default=noprint_wrappers=1:nokey=0 "$INPUT"

# Per-frame MD5 of every decoded frame in the whole file, once.
nice -n 10 ffmpeg -v error -i "$INPUT" -map 0:v:0 -f framemd5 "$WORK/whole.framemd5"
TOTAL=$(grep -c '^[0-9]' "$WORK/whole.framemd5")
echo "decoded frames in whole file: $TOTAL"

status=0
check() {
  local name=$1 skip=$2 cap=$3
  local end=$(( skip + cap ))
  if (( end > TOTAL )); then
    echo "SKIP  $name (needs $end frames, file has $TOTAL)"
    return 0
  fi

  # What the model sees today: frames [skip, skip+cap) of the whole file.
  grep '^[0-9]' "$WORK/whole.framemd5" | sed -n "$(( skip + 1 )),${end}p" \
    | awk '{print $NF}' > "$WORK/$name.expected"

  # What the pod would see: a stream-copy slice, decoded from its own frame 0.
  # Intra-only FFV1 means the cut lands exactly on the requested frame.
  local ss
  ss=$(awk -v s="$skip" -v f="$FPS" 'BEGIN{printf "%.6f", s/f}')
  local dur
  dur=$(awk -v c="$cap" -v f="$FPS" 'BEGIN{printf "%.6f", c/f}')
  nice -n 10 ffmpeg -v error -ss "$ss" -i "$INPUT" -map 0:v:0 -frames:v "$cap" \
    -c copy "$WORK/$name.slice.mkv"
  nice -n 10 ffmpeg -v error -i "$WORK/$name.slice.mkv" -map 0:v:0 \
    -f framemd5 "$WORK/$name.slice.framemd5"
  grep '^[0-9]' "$WORK/$name.slice.framemd5" | awk '{print $NF}' > "$WORK/$name.actual"

  local want got size
  want=$(wc -l < "$WORK/$name.expected")
  got=$(wc -l < "$WORK/$name.actual")
  size=$(stat -c%s "$WORK/$name.slice.mkv")

  if [[ "$want" != "$got" ]]; then
    echo "FAIL  $name skip=$skip cap=$cap: slice has $got frames, expected $want"
    status=1
    return 0
  fi
  if diff -q "$WORK/$name.expected" "$WORK/$name.actual" >/dev/null; then
    printf 'PASS  %-8s skip=%-6s cap=%-4s %s frames byte-identical, slice %.1f MiB\n' \
      "$name" "$skip" "$cap" "$got" "$(awk -v b="$size" 'BEGIN{print b/1048576}')"
  else
    echo "FAIL  $name skip=$skip cap=$cap: decoded frames differ"
    diff "$WORK/$name.expected" "$WORK/$name.actual" | head -5
    status=1
  fi
}

# Unit 0 shape: reads from the very start, no context frames.
check unit0  0    750
# Interior unit shape: starts at unit_start-overlap, reads new+overlap frames.
check interior 246 254
# A second interior offset, to catch an off-by-one that happens to cancel.
check offset   499 401
# Tail-ish shape: a short final unit.
check tail     700 200

echo
if (( status == 0 )); then
  echo "A1 PASS: stream-copy slicing is frame-exact; skip=0 on a slice is"
  echo "         equivalent to skip=N on the whole file."
else
  echo "A1 FAIL: slicing is NOT frame-exact — do not ship unit slices." >&2
fi
exit $status

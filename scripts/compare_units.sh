#!/usr/bin/env bash
# Compare a restored unit against a local reference: frame count, then whether
# the decoded video is bit-identical, then PSNR if it is not.
#
# The project's approved-quality bar is >=45 dB (VAE-compile was approved at
# 49.0 dB; unit-boundary seams at 47.9 dB). Same GPU architecture should be
# bit-identical (SeedVR2 is deterministic at seed 42); a different architecture
# changes only floating-point reduction order and must still clear 45 dB.
#
# Usage: scripts/compare_units.sh <candidate.mkv> <reference.mkv>
set -euo pipefail
CAND=$1
REF=$2
WORK=$(mktemp -d /tmp/cmp-unit.XXXXXX)
trap 'rm -rf "$WORK"' EXIT

fc() { ffprobe -v error -select_streams v:0 -count_frames \
        -show_entries stream=nb_read_frames -of csv=p=0 "$1"; }
cn=$(fc "$CAND"); rn=$(fc "$REF")
echo "candidate: $CAND  ($cn frames, $(stat -c%s "$CAND" | numfmt --to=iec)B)"
echo "reference: $REF  ($rn frames, $(stat -c%s "$REF" | numfmt --to=iec)B)"
if [[ "$cn" != "$rn" ]]; then
  echo "FAIL: frame count differs ($cn vs $rn)"; exit 1
fi

# Bit-identity of decoded frames (independent of container/GOP re-encode).
ffmpeg -v error -i "$CAND" -map 0:v:0 -f framemd5 "$WORK/c.md5"
ffmpeg -v error -i "$REF"  -map 0:v:0 -f framemd5 "$WORK/r.md5"
if diff -q <(grep '^[0-9]' "$WORK/c.md5" | awk '{print $NF}') \
           <(grep '^[0-9]' "$WORK/r.md5" | awk '{print $NF}') >/dev/null; then
  echo "RESULT: BIT-IDENTICAL decoded video ($cn frames) — PASS"
  exit 0
fi

# Not bit-identical: measure PSNR of candidate against reference.
psnr_line=$(ffmpeg -hide_banner -i "$CAND" -i "$REF" -lavfi "[0:v][1:v]psnr" -f null - 2>&1 \
  | tr '\r' '\n' | grep -oE 'average:[0-9.]+|average:inf' | tail -1)
avg=${psnr_line#average:}
echo "not bit-identical; PSNR average = ${avg:-unknown} dB"
if [[ "$avg" == inf ]]; then
  echo "RESULT: PSNR infinite (identical) — PASS"; exit 0
fi
awk -v a="${avg:-0}" 'BEGIN{ if (a+0 >= 45.0) {print "RESULT: PSNR " a " dB >= 45 dB approved bar — PASS"; exit 0} else {print "RESULT: PSNR " a " dB below 45 dB — FAIL"; exit 1} }'

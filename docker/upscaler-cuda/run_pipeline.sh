#!/usr/bin/env bash
# Runs ENTIRELY inside the upscaler container so frames flow through fast OS pipes.
#   run_pipeline.sh <in> <start_sec> <dur_sec|full> <out> [model.pth] [batch] [vcodec]
set -euo pipefail
IN="$1"; SS="${2:-0}"; DUR="${3:-full}"; OUT="$4"
MODEL="${5:-/models/realesr-general-x4v3.pth}"
BATCH="${6:-24}"
VCODEC="${7:-hevc_nvenc}"

IW=768; IH=576; SCALE=4; OW=$((IW*SCALE)); OH=$((IH*SCALE))
DUR_ARGS=(); [ "$DUR" != "full" ] && DUR_ARGS=(-t "$DUR")

# Preserve original AC3 in .mkv; transcode to AAC for browser-friendly .mp4.
case "$OUT" in
  *.mkv) ACODEC=(-c:a copy) ;;
  *)     ACODEC=(-c:a aac -b:a 192k) ;;
esac

ffmpeg -hide_banner -loglevel error -ss "$SS" "${DUR_ARGS[@]}" -i "$IN" -an \
      -vf "bwdif=mode=send_field:parity=bff,scale=${IW}:${IH},setsar=1,format=rgb24" \
      -f rawvideo - \
  | python /opt/upscale_stream.py --model "$MODEL" --width "$IW" --height "$IH" --batch "$BATCH" \
  | ffmpeg -hide_banner -loglevel error \
      -f rawvideo -pix_fmt rgb24 -s "${OW}x${OH}" -r 50 -i - \
      -ss "$SS" "${DUR_ARGS[@]}" -i "$IN" \
      -map 0:v -map 1:a "${ACODEC[@]}" \
      -c:v "$VCODEC" -preset p5 -cq 23 -pix_fmt yuv420p -movflags +faststart \
      "$OUT"

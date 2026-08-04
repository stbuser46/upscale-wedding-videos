#!/usr/bin/env bash
# Streaming upscale pipeline for a test clip (no intermediate PNGs).
# Usage: pipeline_clip.sh <input.vob> <start_sec> <dur_sec> <out.mp4>
set -euo pipefail
IN="$1"; SS="$2"; DUR="$3"; OUT="$4"
PROJ="/home/yacoob/Projects/upscale-wedding-videos"
MODEL="realesr-general-x4v3.pth"
IW=768; IH=576; SCALE=4
OW=$((IW*SCALE)); OH=$((IH*SCALE))
CAPS="-e NVIDIA_DRIVER_CAPABILITIES=all"

# 1) decode + BFF deinterlace to 50fps + square-pixel scale -> raw rgb24
# 2) GPU upscale (streaming, batched)
# 3) NVENC H.264 encode + copy original AC3 audio for the same window
docker run --rm -i -v "$PROJ:/proj" linuxserver/ffmpeg -hide_banner -loglevel error \
      -ss "$SS" -t "$DUR" -i "/proj/${IN#"$PROJ/"}" \
      -an -vf "bwdif=mode=send_field:parity=bff,scale=${IW}:${IH},setsar=1,format=rgb24" \
      -f rawvideo - \
  | docker run --rm -i --gpus all $CAPS \
      -v "$PROJ/docker/upscaler-cuda/upscale_stream.py:/upscale_stream.py" \
      --entrypoint python upscaler-cuda:latest /upscale_stream.py \
      --model "/models/$MODEL" --width "$IW" --height "$IH" --batch 8 \
  | docker run --rm -i --gpus all $CAPS -v "$PROJ:/proj" linuxserver/ffmpeg -hide_banner -loglevel error \
      -f rawvideo -pix_fmt rgb24 -s "${OW}x${OH}" -r 50 -i - \
      -ss "$SS" -t "$DUR" -i "/proj/${IN#"$PROJ/"}" \
      -map 0:v -map 1:a -c:a aac -b:a 192k \
      -c:v h264_nvenc -preset p5 -cq 21 -pix_fmt yuv420p -movflags +faststart \
      "/proj/${OUT#"$PROJ/"}"
echo "wrote $OUT"

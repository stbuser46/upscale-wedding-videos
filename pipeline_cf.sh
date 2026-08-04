#!/usr/bin/env bash
# Full-quality pipeline: deinterlace -> x4plus upscale -> CodeFormer faces -> HEVC + audio.
# Usage: pipeline_cf.sh <in.vob> <start_sec> <dur_sec> <out.mkv> [tag]
set -euo pipefail
PROJ=/home/yacoob/Projects/upscale-wedding-videos
IN="$1"; SS="$2"; DUR="$3"; OUT="$4"; TAG="${5:-cfjob}"
FPS=25
CAPS="-e NVIDIA_DRIVER_CAPABILITIES=all"
W=$PROJ/work/$TAG
rm -rf "$W"; mkdir -p "$W/in"

echo "[1/3] deinterlace 25p + x4plus upscale -> JPEG frames"
docker run --rm --gpus all $CAPS -v "$PROJ:/proj" --entrypoint bash upscaler-cuda:latest -c "
  ffmpeg -hide_banner -loglevel error -ss $SS -t $DUR -i '/proj/${IN#$PROJ/}' -an \
    -vf 'bwdif=mode=send_frame:parity=bff,scale=768:576,setsar=1,format=rgb24' -f rawvideo - \
  | python /opt/upscale_stream.py --model /models/RealESRGAN_x4plus.pth --width 768 --height 576 --batch 24 \
  | ffmpeg -hide_banner -loglevel error -f rawvideo -pix_fmt rgb24 -s 3072x2304 -r $FPS -i - \
      -q:v 2 '/proj/work/$TAG/in/f_%05d.jpg'
"
echo "    frames: $(ls "$W/in" | wc -l)"

echo "[2/3] CodeFormer face restoration (the slow part)"
docker run --rm --gpus all $CAPS -v "$W:/work" codeformer-cuda:latest \
  -w 0.7 -s 1 --face_upsample --input_path /work/in --output_path /work/cf > "$W/codeformer.log" 2>&1
echo "    restored: $(ls "$W/cf/final_results" 2>/dev/null | wc -l)"

echo "[3/3] encode HEVC + mux original audio"
docker run --rm --gpus all $CAPS -v "$PROJ:/proj" --entrypoint ffmpeg upscaler-cuda:latest \
  -hide_banner -loglevel error \
  -framerate $FPS -i "/proj/work/$TAG/cf/final_results/f_%05d.png" \
  -ss "$SS" -t "$DUR" -i "/proj/${IN#$PROJ/}" \
  -map 0:v -map 1:a -c:a copy -c:v hevc_nvenc -preset p5 -cq 20 -pix_fmt yuv420p -movflags +faststart \
  "/proj/${OUT#$PROJ/}"
echo "DONE -> $OUT"

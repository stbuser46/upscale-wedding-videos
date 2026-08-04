#!/usr/bin/env bash
# v2 pipeline (Codex-informed):
#   deinterlace 25p -> light deblock -> explicit PAL BT.601 color -> x4plus 4x
#   -> LOSSLESS png -> tracked CodeFormer faces -> downscale 1440p -> 10-bit HEVC + deband/grain + AC3
# Usage: pipeline_v2.sh <in.vob> <start_sec> <dur_sec> <out.mkv> [tag]
set -euo pipefail
PROJ=/home/yacoob/Projects/upscale-wedding-videos
IN="$1"; SS="$2"; DUR="$3"; OUT="$4"; TAG="${5:-v2}"
FPS=25
CAPS="-e NVIDIA_DRIVER_CAPABILITIES=all"
W=$PROJ/work/$TAG
rm -rf "$W"; mkdir -p "$W/x4" "$W/faces"

echo "[1/3] deint + deblock + PAL-color + x4plus 4x -> lossless PNG"
# NOTE: shares the GPU with other services (e.g. ollama). Small batch + expandable
# segments keeps our footprint low so we coexist instead of OOMing.
docker run --rm --gpus all $CAPS -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  -v "$PROJ:/proj" --entrypoint bash upscaler-cuda:latest -c "
  ffmpeg -hide_banner -loglevel error -ss $SS -t $DUR -i '/proj/${IN#$PROJ/}' -an \
    -vf 'bwdif=mode=send_frame:parity=bff,deblock=filter=weak,scale=768:576:in_range=tv:in_color_matrix=bt470bg:flags=lanczos,setsar=1,format=rgb24' \
    -f rawvideo - \
  | python /opt/upscale_stream.py --model /models/RealESRGAN_x4plus.pth --width 768 --height 576 --batch 6 \
  | ffmpeg -hide_banner -loglevel error -f rawvideo -pix_fmt rgb24 -s 3072x2304 -r $FPS -i - \
      '/proj/work/$TAG/x4/f_%05d.png'
"
echo "    x4 frames: $(ls "$W/x4" | wc -l)"

echo "[2/3] tracked CodeFormer faces (size-gated + temporal EMA)"
docker run --rm --gpus all $CAPS --workdir /opt/CodeFormer \
  -v "$W:/work" -v "$PROJ/docker/codeformer-cuda/vidface.py:/opt/CodeFormer/vidface.py" \
  --entrypoint python codeformer-cuda:latest /opt/CodeFormer/vidface.py \
  --input /work/x4 --output /work/faces -w 0.7 --min-face 90 --ema 0.6 > "$W/vidface.log" 2>&1
echo "    restored frames: $(ls "$W/faces" | wc -l)"

echo "[3/3] downscale 1440p + 10-bit HEVC (deband+grain) + AC3"
docker run --rm --gpus all $CAPS -v "$PROJ:/proj" --entrypoint ffmpeg upscaler-cuda:latest \
  -hide_banner -loglevel error \
  -framerate $FPS -i "/proj/work/$TAG/faces/f_%05d.png" \
  -ss "$SS" -t "$DUR" -i "/proj/${IN#$PROJ/}" \
  -filter_complex "[0:v]scale=1920:1440:out_range=tv:out_color_matrix=bt470bg:flags=lanczos,format=yuv420p10le,deband=range=16:1thr=0.015:2thr=0.015:3thr=0.015,noise=alls=3:allf=t[v]" \
  -map "[v]" -map 1:a -c:a copy \
  -c:v hevc_nvenc -profile:v main10 -pix_fmt p010le -preset p7 -tune hq -rc vbr -cq 20 -b:v 0 \
  -spatial_aq 1 -temporal_aq 1 \
  -colorspace bt470bg -color_primaries bt470bg -color_trc smpte170m -color_range tv \
  -movflags +faststart "/proj/${OUT#$PROJ/}"
echo "DONE -> $OUT"

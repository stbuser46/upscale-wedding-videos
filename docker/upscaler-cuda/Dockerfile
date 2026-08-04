FROM pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime
ENV DEBIAN_FRONTEND=noninteractive

# ffmpeg (with NVENC) lives in the SAME image so the whole
# decode -> GPU upscale -> encode pipe runs inside one container over fast OS
# pipes, instead of shuttling 21MB/frame across Docker's slow stdio bridge.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# spandrel loads Real-ESRGAN/ESRGAN .pth files on modern PyTorch (Blackwell-ready).
# PIL + numpy only for I/O so we avoid OpenCV's system-lib deps.
RUN pip install --no-cache-dir spandrel==0.4.1 pillow numpy

# Bake in the two candidate general-purpose upscaling models.
RUN mkdir -p /models && python - <<'PY'
import urllib.request as u
m = {
 "RealESRGAN_x4plus.pth":   "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth",
 "realesr-general-x4v3.pth":"https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-x4v3.pth",
}
for name,url in m.items():
    print("downloading", name, flush=True)
    u.urlretrieve(url, "/models/"+name)
print("models ready")
PY

COPY upscale.py /opt/upscale.py
COPY upscale_stream.py /opt/upscale_stream.py
COPY run_pipeline.sh /opt/run_pipeline.sh
RUN chmod +x /opt/run_pipeline.sh
WORKDIR /work
ENTRYPOINT ["python", "/opt/upscale.py"]

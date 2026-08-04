FROM pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime

ARG SEEDVR2_COMMIT=4490bd1f482e026674543386bb2a4d176da245b9
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates ffmpeg git libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN git clone https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler.git SeedVR2 \
    && cd SeedVR2 \
    && git checkout "$SEEDVR2_COMMIT" \
    && rm -rf .git

WORKDIR /opt/SeedVR2

# Keep the CUDA 12.8 / Blackwell-compatible PyTorch and torchvision from the
# base image instead of allowing pip to replace them with unrelated wheels.
RUN sed -i '/^torch$/d;/^torchvision$/d' requirements.txt \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir 'huggingface_hub[hf_xet]'

# The upstream CLI writes RGB frames through ffmpeg. Make that conversion
# explicit so the HD result is limited-range BT.709 and carries correct tags.
COPY writer-color.patch /tmp/writer-color.patch
COPY streaming-prepend.patch /tmp/streaming-prepend.patch
RUN git apply /tmp/writer-color.patch \
    && git apply /tmp/streaming-prepend.patch \
    && rm /tmp/writer-color.patch /tmp/streaming-prepend.patch

ENTRYPOINT ["python", "/opt/SeedVR2/inference_cli.py"]

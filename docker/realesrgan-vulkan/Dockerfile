FROM ubuntu:24.04

# Real-ESRGAN ncnn-vulkan: talks to GPU via Vulkan (works on brand-new Blackwell
# GPUs where CUDA/PyTorch wheels don't exist yet). NVIDIA Vulkan ICD is injected
# by the nvidia container runtime at run time (NVIDIA_DRIVER_CAPABILITIES=all).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libvulkan1 libgomp1 wget unzip ca-certificates vulkan-tools \
        libx11-6 libxext6 libxcb1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/realesrgan
RUN wget -q https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-ubuntu.zip \
    && unzip -q realesrgan-ncnn-vulkan-20220424-ubuntu.zip \
    && rm realesrgan-ncnn-vulkan-20220424-ubuntu.zip \
    && chmod +x realesrgan-ncnn-vulkan

ENV NVIDIA_DRIVER_CAPABILITIES=all
ENV PATH="/opt/realesrgan:${PATH}"
WORKDIR /work
ENTRYPOINT ["realesrgan-ncnn-vulkan"]

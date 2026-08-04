FROM pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        git wget libgl1 libglib2.0-0 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt
RUN git clone --depth 1 https://github.com/sczhou/CodeFormer.git
WORKDIR /opt/CodeFormer

# Don't let requirements reinstall/downgrade the CUDA12.8 torch already in the base.
RUN sed -i '/^torch/d;/^torchvision/d' requirements.txt \
    && pip install --no-cache-dir -r requirements.txt

# Old code imports torchvision.transforms.functional_tensor, removed in torchvision>=0.17.
# Blanket-patch every occurrence (vendored basicsr + facexlib in site-packages).
RUN grep -rl "functional_tensor" /opt/CodeFormer /opt/conda/lib/python*/site-packages 2>/dev/null \
    | xargs -r sed -i 's/torchvision\.transforms\.functional_tensor/torchvision.transforms.functional/g' \
    ; python basicsr/setup.py develop

# Pre-fetch face-detection/parsing + CodeFormer weights so runs are offline.
RUN python scripts/download_pretrained_models.py facelib \
    && python scripts/download_pretrained_models.py CodeFormer

WORKDIR /work
ENTRYPOINT ["python", "/opt/CodeFormer/inference_codeformer.py"]

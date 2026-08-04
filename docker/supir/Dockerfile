FROM pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime
ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1
ENV HF_HOME=/models/hf_cache NVIDIA_DRIVER_CAPABILITIES=all

RUN apt-get update && apt-get install -y --no-install-recommends \
        git libgl1 libglib2.0-0 ca-certificates && rm -rf /var/lib/apt/lists/*

COPY SUPIR /opt/SUPIR
WORKDIR /opt/SUPIR

# Keep the Blackwell-ready torch from the base; drop pins that would fight it.
# xformers/triton unneeded (sgm falls back to PyTorch SDP attention).
RUN sed -i '/^torch/d;/^torchvision/d;/^xformers/d;/^triton/d;/^numpy/d' requirements.txt \
 && sed -i '/^fastapi/d;/^gradio/d;/^uvicorn/d;/^wandb/d;/^httpx/d;/^[Mm]arkdown/d' requirements.txt \
 && sed -i 's/^scipy==.*/scipy/' requirements.txt \
 && pip install "huggingface_hub==0.16.4" "numpy==1.26.4" \
 && pip install -r requirements.txt

# LLaVA is imported eagerly at the top of test.py but only used without --no_llava.
# Neutralize the eager import so --no_llava runs without the 15GB captioner.
RUN sed -i 's/^from llava.llava_agent import LLavaAgent/pass  # llava import stripped; use --no_llava/' test.py \
 && sed -i 's/^from CKPT_PTH import LLAVA_MODEL_PATH/LLAVA_MODEL_PATH = None/' test.py

# Wire model paths to the mounted /models volume.
RUN sed -i "s#^SDXL_CLIP1_PATH = .*#SDXL_CLIP1_PATH = 'openai/clip-vit-large-patch14'#" CKPT_PTH.py \
 && sed -i "s#^SDXL_CLIP2_CKPT_PTH = .*#SDXL_CLIP2_CKPT_PTH = '/models/CLIP-ViT-bigG-14_open_clip_pytorch_model.bin'#" CKPT_PTH.py \
 && sed -i "s#^SDXL_CKPT: .*#SDXL_CKPT: /models/sd_xl_base_1.0.safetensors#" options/SUPIR_v0.yaml \
 && sed -i "s#^SUPIR_CKPT_Q: .*#SUPIR_CKPT_Q: /models/SUPIR-v0Q.ckpt#" options/SUPIR_v0.yaml

# The VAE's MemoryEfficientAttnBlock hard-calls xformers (no fallback, unlike the
# UNet). We skip xformers on Blackwell, so route it to PyTorch native SDP attention.
RUN python - <<'PY'
import pathlib
patches = {
    '/opt/SUPIR/sgm/modules/diffusionmodules/model.py': (
        "        out = xformers.ops.memory_efficient_attention(\n"
        "            q, k, v, attn_bias=None, op=self.attention_op\n"
        "        )",
        "        import torch.nn.functional as F\n"
        "        out = F.scaled_dot_product_attention(q, k, v)",
    ),
    '/opt/SUPIR/SUPIR/utils/tilevae.py': (
        "    out = xformers.ops.memory_efficient_attention(\n"
        "        q, k, v, attn_bias=None, op=self.attention_op)",
        "    import torch.nn.functional as F\n"
        "    out = F.scaled_dot_product_attention(q, k, v)",
    ),
}
for path, (old, new) in patches.items():
    p = pathlib.Path(path); s = p.read_text()
    assert old in s, f"pattern not found in {path}"
    p.write_text(s.replace(old, new)); print("patched", path)
PY

ENTRYPOINT ["python", "test.py"]

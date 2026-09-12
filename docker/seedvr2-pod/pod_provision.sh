#!/usr/bin/env bash
# Fetch the pinned SeedVR2 weights onto a pod, then report readiness.
#
# Run once per pod by the orchestrator over ssh, after wait_ssh succeeds. Uses
# upstream's own downloader (SHA256-validated, resumable, with a validation
# cache), so the 7.3 GB comes from Hugging Face at datacenter speed and never
# crosses the home connection. Idempotent: an already-valid file is skipped.
set -euo pipefail

MODEL_DIR=${SEEDVR2_MODEL_DIR:-/opt/models/seedvr2}
mkdir -p "$MODEL_DIR"
cd /opt/SeedVR2

python - <<'PY'
import os, sys
from src.utils.downloads import download_weight

model_dir = os.environ.get("SEEDVR2_MODEL_DIR", "/opt/models/seedvr2")
ok = download_weight(
    "seedvr2_ema_3b_fp16.safetensors",
    "ema_vae_fp16.safetensors",
    model_dir=model_dir,
)
if not ok:
    sys.exit("weight download or SHA256 validation failed")
print("weights validated")
PY

echo "--- $MODEL_DIR ---"
ls -l "$MODEL_DIR"

# Fail loudly now rather than midway through a billed unit.
for f in seedvr2_ema_3b_fp16.safetensors ema_vae_fp16.safetensors; do
  [[ -s "$MODEL_DIR/$f" ]] || { echo "missing $f" >&2; exit 1; }
done

python - <<'PY'
import torch
if not torch.cuda.is_available():
    raise SystemExit("no CUDA device visible on this pod")
p = torch.cuda.get_device_properties(0)
free, total = torch.cuda.mem_get_info()
print(f"gpu={p.name} sm_{p.major}{p.minor} total={total/2**30:.1f}GiB free={free/2**30:.1f}GiB")
# A unit peaks around 60 GiB; refuse a pod that cannot hold it.
if total / 2**30 < 78:
    raise SystemExit(f"GPU too small for the pinned batch of 129: {total/2**30:.1f}GiB")
PY

echo "POD READY"

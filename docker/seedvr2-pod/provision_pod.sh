#!/usr/bin/env bash
# Turn a stock pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime pod into the
# project's verified SeedVR2 environment, then prove it can run a unit.
#
# Run by the orchestrator over ssh AFTER it has rsynced up:
#   /opt/SeedVR2          the 6.2 MB already-patched tree, copied straight out
#                         of the verified local image (pinned upstream commit +
#                         writer-color + streaming-prepend + unit-drop-leading),
#                         so no clone and no patch step can drift
#   /opt/inductor_cache   the 186 MB warm torch.compile cache (optional)
#   /workspace/provision/requirements-pod.txt   the exact 34-package pip delta
#
# Idempotent: every step is guarded by a marker, so a re-run after a dropped
# connection resumes instead of repeating.
set -euo pipefail

# An ssh session does not inherit the image ENV, so establish it explicitly
# before anything tries to find `python`.
export PATH=/usr/local/nvidia/bin:/usr/local/cuda/bin:/opt/conda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-/usr/local/nvidia/lib:/usr/local/nvidia/lib64}
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export TORCHINDUCTOR_CACHE_DIR=${TORCHINDUCTOR_CACHE_DIR:-/opt/inductor_cache}
export TRITON_CACHE_DIR=$TORCHINDUCTOR_CACHE_DIR/triton
export SEEDVR2_MODEL_DIR=${SEEDVR2_MODEL_DIR:-/opt/models/seedvr2}
export DEBIAN_FRONTEND=noninteractive

MARKERS=/workspace/.provision
mkdir -p "$MARKERS" "$SEEDVR2_MODEL_DIR" "$TORCHINDUCTOR_CACHE_DIR" \
         /workspace/slices /workspace/units

step() {
  local name=$1; shift
  if [[ -f "$MARKERS/$name" ]]; then
    echo "[provision] $name: already done"
    return 0
  fi
  echo "[provision] $name"
  "$@"
  touch "$MARKERS/$name"
}

persist_env() {
  # Make the environment available to every later ssh session: /etc/environment
  # is read by PAM for all sessions, profile.d covers login shells.
  local vars=(PATH LD_LIBRARY_PATH PYTORCH_CUDA_ALLOC_CONF
              TORCHINDUCTOR_CACHE_DIR TRITON_CACHE_DIR SEEDVR2_MODEL_DIR)
  : > /etc/environment
  : > /etc/profile.d/seedvr2-pod.sh
  local v
  for v in "${vars[@]}"; do
    printf '%s=%s\n' "$v" "${!v}" >> /etc/environment
    printf 'export %s=%q\n' "$v" "${!v}" >> /etc/profile.d/seedvr2-pod.sh
  done
  chmod 644 /etc/environment /etc/profile.d/seedvr2-pod.sh
}

apt_packages() {
  apt-get update
  # ffmpeg: the CLI writes frames through it. gcc/g++: torch.compile's Inductor
  # needs a host compiler at runtime. rsync: unit slices in, unit files out.
  apt-get install -y --no-install-recommends \
    ca-certificates ffmpeg git libgl1 libglib2.0-0 gcc g++ rsync
  rm -rf /var/lib/apt/lists/*
  ffmpeg -version | head -1
}

pip_packages() {
  pip install --no-cache-dir -r /workspace/provision/requirements-pod.txt
}

verify_tree() {
  [[ -f /opt/SeedVR2/inference_cli.py ]] || { echo "SeedVR2 tree missing" >&2; exit 1; }
  # The unit-drop-leading patch is what durable-unit mode depends on; a pod
  # without it would silently produce units with extra context frames.
  python /opt/SeedVR2/inference_cli.py --help 2>/dev/null \
    | grep -q -- --drop_leading \
    || { echo "SeedVR2 tree lacks --drop_leading; wrong or unpatched tree" >&2; exit 1; }
  echo "SeedVR2 tree OK (--drop_leading present)"
}

download_weights() {
  cd /opt/SeedVR2
  python - <<'PY'
import os, sys
from src.utils.downloads import download_weight
model_dir = os.environ["SEEDVR2_MODEL_DIR"]
if not download_weight(
    "seedvr2_ema_3b_fp16.safetensors",
    "ema_vae_fp16.safetensors",
    model_dir=model_dir,
):
    sys.exit("weight download or SHA256 validation failed")
print("weights validated")
PY
  ls -l "$SEEDVR2_MODEL_DIR"
  local f
  for f in seedvr2_ema_3b_fp16.safetensors ema_vae_fp16.safetensors; do
    [[ -s "$SEEDVR2_MODEL_DIR/$f" ]] || { echo "missing $f" >&2; exit 1; }
  done
}

gpu_guard() {
  python - <<'PY'
import torch, sys
if not torch.cuda.is_available():
    sys.exit("no CUDA device visible on this pod")
p = torch.cuda.get_device_properties(0)
_, total = torch.cuda.mem_get_info()
gib = total / 2**30
print(f"gpu={p.name} sm_{p.major}{p.minor} vram={gib:.1f}GiB")
# A unit peaks near 60 GiB at the pinned batch of 129.
if gib < 78:
    sys.exit(f"GPU too small for the pinned settings: {gib:.1f}GiB")
PY
  free -g | awk '/^Mem:/ {printf "host RAM total=%sGiB available=%sGiB\n", $2, $7}'
  nproc | xargs echo "vCPU:"
}

step persist_env      persist_env
step apt_packages     apt_packages
step pip_packages     pip_packages
step verify_tree      verify_tree
step download_weights download_weights
gpu_guard

echo "POD READY"

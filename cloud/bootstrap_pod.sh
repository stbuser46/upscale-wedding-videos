#!/usr/bin/env bash
# Turn a bare pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime RunPod pod into
# the exact SeedVR2 environment of the local seedvr2-cuda:v3 image:
# same base image, same apt ffmpeg 4.4.2, same pinned upstream commit, same
# two local patches, pip packages pinned to the versions captured from the
# local image (cloud/requirements-cloud.txt).
#
# Idempotent: safe to re-run; finished steps are skipped via marker files.
# Expects this repo's cloud/ directory (including patches/) synced to
# /workspace/cloud by the orchestrator.
set -euo pipefail

SEEDVR2_COMMIT=4490bd1f482e026674543386bb2a4d176da245b9
ROOT=${CLOUD_ROOT:-/workspace}
CLOUD_DIR=$ROOT/cloud
SEEDVR2_HOME=/opt/SeedVR2
MODEL_DIR=$ROOT/models/seedvr2
MARKERS=$ROOT/.bootstrap
mkdir -p "$MARKERS" "$MODEL_DIR"

step() {
  local name=$1; shift
  if [[ -f "$MARKERS/$name" ]]; then
    echo "[bootstrap] $name: done, skipping"
    return 0
  fi
  echo "[bootstrap] $name: running"
  "$@"
  touch "$MARKERS/$name"
}

apt_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y --no-install-recommends \
    ca-certificates ffmpeg git libgl1 libglib2.0-0 gcc g++ rsync
  rm -rf /var/lib/apt/lists/*
  ffmpeg -version | head -1
}

clone_seedvr2() {
  rm -rf "$SEEDVR2_HOME"
  git clone https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler.git "$SEEDVR2_HOME"
  cd "$SEEDVR2_HOME"
  git checkout "$SEEDVR2_COMMIT"
}

apply_patches() {
  cd "$SEEDVR2_HOME"
  git apply "$CLOUD_DIR/patches/writer-color.patch"
  git apply "$CLOUD_DIR/patches/streaming-prepend.patch"
  rm -rf .git
}

pip_packages() {
  pip install --no-cache-dir -r "$CLOUD_DIR/requirements-cloud.txt"
}

download_weights() {
  # Reuses upstream's own downloader: pulls DiT + VAE from the registered
  # HuggingFace repos with SHA256 validation and resume support.
  cd "$SEEDVR2_HOME"
  SEEDVR2_MODEL_DIR="$MODEL_DIR" python - <<'PY'
import os
from src.utils.downloads import download_weight
ok = download_weight(
    "seedvr2_ema_3b_fp16.safetensors",
    "ema_vae_fp16.safetensors",
    model_dir=os.environ["SEEDVR2_MODEL_DIR"],
)
raise SystemExit(0 if ok else 1)
PY
  ls -la "$MODEL_DIR"
}

step apt_packages apt_packages
step clone_seedvr2 clone_seedvr2
step apply_patches apply_patches
step pip_packages pip_packages
step download_weights download_weights

nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv || true
echo "[bootstrap] complete"

#!/usr/bin/env bash
# Bring a RunPod pod up as an ssh host the restoration orchestrator can drive.
#
# RunPod injects the orchestrator's public key as $PUBLIC_KEY. Nothing else is
# authorised: password auth is off and root may only log in by key.
set -euo pipefail

mkdir -p /root/.ssh
chmod 700 /root/.ssh

if [[ -n "${PUBLIC_KEY:-}" ]]; then
  # RunPod may pass several keys separated by newlines.
  printf '%s\n' "$PUBLIC_KEY" >> /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
else
  echo "[pod] WARNING: no PUBLIC_KEY in env; nobody can log in" >&2
fi

# Host keys are not baked into the image, so every pod gets its own.
ssh-keygen -A

sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
grep -q '^AcceptEnv' /etc/ssh/sshd_config || echo 'AcceptEnv LANG LC_*' >> /etc/ssh/sshd_config

# An ssh session does NOT inherit the image's ENV: sshd builds a fresh
# environment, so PATH loses /opt/conda/bin (no `python`) and
# /usr/local/nvidia/bin, LD_LIBRARY_PATH loses the nvidia libs (CUDA fails to
# initialise), and the compile-cache variables vanish. Snapshot them here,
# where they are still correct, into both places sshd consults:
#   /etc/environment  — read by PAM for every session, login or not
#   /etc/profile.d/   — for interactive/login shells
PERSIST_VARS=(PATH LD_LIBRARY_PATH NVIDIA_VISIBLE_DEVICES NVIDIA_DRIVER_CAPABILITIES
              PYTORCH_CUDA_ALLOC_CONF PYTHONUNBUFFERED
              TORCHINDUCTOR_CACHE_DIR TRITON_CACHE_DIR SEEDVR2_MODEL_DIR)
: > /etc/environment
: > /etc/profile.d/seedvr2-pod.sh
for v in "${PERSIST_VARS[@]}"; do
  if [[ -n "${!v:-}" ]]; then
    printf '%s=%s\n' "$v" "${!v}" >> /etc/environment
    printf 'export %s=%q\n' "$v" "${!v}" >> /etc/profile.d/seedvr2-pod.sh
  fi
done
chmod 644 /etc/environment /etc/profile.d/seedvr2-pod.sh

mkdir -p "${SEEDVR2_MODEL_DIR:-/opt/models/seedvr2}" /workspace/units /workspace/slices

nvidia-smi --query-gpu=name,memory.total,compute_cap --format=csv,noheader 2>/dev/null \
  || echo "[pod] nvidia-smi unavailable"

echo "[pod] sshd listening on 22"
exec /usr/sbin/sshd -D -e

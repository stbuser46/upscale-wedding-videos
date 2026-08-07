# Cloud restoration (RunPod)

Parallelizes `pipeline_v3.sh` across rented RTX PRO 6000 Blackwell 96GB pods —
the same GPU as the local server, so the pinned quality settings (batch 129,
1440p, chunk 750, VAE-only compile) run unchanged.

## How it works

- **No registry, no image push.** Pods start from the public
  `pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime` image (the exact base of the
  local `seedvr2-cuda:v3`). `bootstrap_pod.sh` reproduces the local image on
  the pod: same apt ffmpeg 4.4.2, SeedVR2 pinned to commit `4490bd1f`, the two
  local patches, and pip packages pinned to the versions captured from the
  local image (`requirements-cloud.txt`). Weights (6.8 GB + 0.5 GB) download
  on the pod straight from HuggingFace with SHA256 validation — nothing large
  leaves the home connection except the input VOB slice.
- **One chapter (or time slice) per pod.** Each job is a self-contained
  4-stage run producing a validated `.mkv`, exactly like the local worker.
- `pipeline_v3_cloud.sh` is a native (dockerless) port of `pipeline_v3.sh`
  with identical stage commands; cloud pods are already containers. The only
  behavioral difference: `SKIP_BASELINE=1` by default (the x265 comparison
  encode wastes billed pod hours; set `SKIP_BASELINE=0` to restore it).

## Setup

1. Create a RunPod account, add credit, create an API key
   (Settings → API Keys, needs pod create/terminate permission).
2. `echo 'rpa_...' > cloud/.runpod_api_key && chmod 600 cloud/.runpod_api_key`
   (gitignored), or export `RUNPOD_API_KEY`.
3. An ssh keypair is generated automatically at `cloud/keys/` on first use.

## Usage

```bash
# What RTX PRO 6000 stock looks like right now (price + availability):
cloud/runpod.py gpus

# Full single-job flow: create pod, bootstrap, upload, restore, download, terminate.
cloud/runpod.py run-job \
  --input work/dvd1_title.vob.slice.vob \
  --start 60 --duration 18 \
  --output out/cloud_test_18s.mkv --tag cloud-test-18s

# Debugging / manual driving:
cloud/runpod.py create --name test1        # prints pod id
cloud/runpod.py wait-ssh POD_ID
cloud/runpod.py bootstrap POD_ID
cloud/runpod.py ssh POD_ID nvidia-smi
cloud/runpod.py push POD_ID local remote / pull POD_ID remote local
cloud/runpod.py terminate POD_ID
```

On failure `run-job` leaves the pod running for inspection — remember pods
bill until terminated; `cloud/runpod.py list` shows anything still alive.

## Economics (2026-08)

~50 GPU-minutes per minute of footage (VAE-compile path). RTX PRO 6000 96GB:
RunPod Secure ≈ $1.99/hr, Community ≈ $1.69/hr. Remaining footage ≈ 257 min
(DVD1 minus chapter 10, plus DVD2) ≈ 220 GPU-hours ≈ $370–440 plus ~10 min
bootstrap overhead per pod. Vast.ai offers the same GPU at ~$0.72–1.00/hr if
costs matter more than platform polish (not implemented here).

Default cloud tier is SECURE (private datacenters — this is personal family
footage) — use `--cloud COMMUNITY` to trade that for ~15% lower cost.

## Validation

The acceptance test for environment parity: upload the 18s reference
intermediate (`work/midpoint_vaeonly_18s/input_50p_ffv1.mkv`), run stage 3 on
the pod, and PSNR the result against the local approved reference
(`work/midpoint_vaeonly_18s/seedvr2_1440p.mp4`). Same input, same settings,
same GPU model → expected well above the 45 dB approved-quality class.

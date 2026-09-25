# RunPod execution image for durable-unit restoration.
#
# A RunPod pod IS a container and has no Docker daemon, so the pipeline's local
# `docker run` cannot work there. Instead this image — the pinned local
# seedvr2-cuda:v3 plus the means to be driven remotely — becomes the pod's own
# image, and the orchestrator invokes the inference CLI inside it over ssh.
#
# Deliberately NOT baked in: the 7.3 GB of model weights. They are fetched on
# first start by pod_provision.sh using upstream's own SHA256-validated
# downloader, which keeps this image at ~14 GB rather than ~21 GB and avoids
# redistributing the weights.
#
# Also deliberately NOT baked in: lib/seedvr2_unit_args.sh. The orchestrator
# renders the pinned argv from the repo copy at dispatch time, so the pod can
# never run a stale argument set. See lib/seedvr2_unit_args.sh for why.
#
# Build with docker/seedvr2-pod/build.sh (it assembles the context, including
# the warm compile cache, outside the repo).
FROM seedvr2-cuda:v3

RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-server rsync ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/run/sshd /root/.ssh \
    && chmod 700 /root/.ssh

# Warm torch.compile (Inductor/Triton) kernels copied from the homeserver. A pod
# on the same GPU architecture (sm_120) starts warm and skips the ~2-3 minutes
# of VAE compile that otherwise lands on every unit's first batch. A pod on a
# different architecture simply misses the cache and compiles exactly as before.
COPY inductor_cache /opt/inductor_cache

ENV TORCHINDUCTOR_CACHE_DIR=/opt/inductor_cache \
    TRITON_CACHE_DIR=/opt/inductor_cache/triton \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    SEEDVR2_MODEL_DIR=/opt/models/seedvr2

COPY pod_start.sh pod_provision.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/pod_start.sh /usr/local/bin/pod_provision.sh

# Resident warm-worker engine (WEDDING_POD_ENGINE=1): loads the model once and
# restores unit after unit over one persistent ssh session. Lives inside the
# SeedVR2 tree (it imports inference_cli from its own directory). When — and
# only when — a worker runs with the gate on, the fleet also rsyncs the repo
# copy at provision time (FleetConfig.pod_engine), so stock-base pods get it
# too and an updated engine never waits on an image rebuild; gate-off
# provisioning ships nothing extra.
COPY pod_engine.py /opt/SeedVR2/pod_engine.py

# The base image's ENTRYPOINT is the inference CLI itself. A pod must instead
# come up as a long-lived ssh host. RunPod overrides ENTRYPOINT in some paths,
# so the orchestrator also passes dockerEntrypoint/dockerStartCmd explicitly.
ENTRYPOINT []
CMD ["/usr/local/bin/pod_start.sh"]

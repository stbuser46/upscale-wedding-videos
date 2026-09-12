#!/usr/bin/env bash
# Build (and optionally push) the RunPod execution image.
#
# The build context is assembled OUTSIDE the repo, because it has to include
# the 186 MB warm compile cache that lives under the gitignored webapp/data/.
# Sending the repo itself as context would ship hundreds of gigabytes.
#
#   docker/seedvr2-pod/build.sh                      # build locally only
#   docker/seedvr2-pod/build.sh docker.io/USER/wedding-seedvr2:v3   # + push
set -euo pipefail

PROJ=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
HERE=$PROJ/docker/seedvr2-pod
LOCAL_TAG=${POD_IMAGE_LOCAL_TAG:-seedvr2-pod:v3}
REMOTE_TAG=${1:-}
CACHE_SRC=${SEEDVR2_INDUCTOR_CACHE:-$PROJ/webapp/data/restoration_work/.inductor_cache}

if ! docker image inspect seedvr2-cuda:v3 >/dev/null 2>&1; then
  echo "base image seedvr2-cuda:v3 missing; build it first from docker/seedvr2" >&2
  exit 1
fi

CTX=$(mktemp -d /tmp/seedvr2-pod-ctx.XXXXXX)
cleanup() { rm -rf "$CTX"; }
trap cleanup EXIT

cp "$HERE/Dockerfile" "$HERE/pod_start.sh" "$HERE/pod_provision.sh" "$CTX/"

mkdir -p "$CTX/inductor_cache"
if [[ -d "$CACHE_SRC" ]]; then
  # Cache files are root-owned but world-readable; copy contents, not the dir.
  cp -r "$CACHE_SRC/." "$CTX/inductor_cache/"
  echo "[build] warm compile cache: $(du -sh "$CTX/inductor_cache" | cut -f1), $(find "$CTX/inductor_cache" -type f | wc -l) files"
else
  echo "[build] no compile cache at $CACHE_SRC — pods will compile cold on their first unit"
fi

echo "[build] $LOCAL_TAG"
docker build -t "$LOCAL_TAG" "$CTX"

if [[ -n "$REMOTE_TAG" ]]; then
  echo "[build] tag + push $REMOTE_TAG"
  docker tag "$LOCAL_TAG" "$REMOTE_TAG"
  docker push "$REMOTE_TAG"
  echo "[build] pushed $REMOTE_TAG"
  echo "[build] set WEDDING_CLOUD_IMAGE=$REMOTE_TAG for the worker"
fi

docker image inspect "$LOCAL_TAG" \
  --format 'built {{.RepoTags}} size={{.Size}} entrypoint={{.Config.Entrypoint}} cmd={{.Config.Cmd}}'

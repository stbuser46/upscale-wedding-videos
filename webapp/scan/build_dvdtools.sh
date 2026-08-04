#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
docker build -t "${WEBAPP_DVDTOOLS_IMAGE:-wedding-dvdtools:latest}" \
  "$PROJECT_ROOT/webapp/scan"

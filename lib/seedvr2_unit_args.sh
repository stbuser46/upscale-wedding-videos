#!/usr/bin/env bash
# Single source of truth for the pinned SeedVR2 *durable-unit* invocation.
#
# Both execution paths render their command from this one function, so the
# cloud path can never drift from the local one:
#
#   local  — pipeline_v3.sh runs it inside `docker run ... seedvr2-cuda:v3`
#   cloud  — webapp/cloud/executor.py runs it natively inside a RunPod pod
#            (pods have no Docker daemon, so the pod's own image IS our image)
#
# The previous cloud attempt forked the pipeline into pipeline_v3_cloud.sh and
# rotted within weeks: it never gained unit mode, --drop_leading, the 110g RAM
# cap or the persistent compile cache. One builder, no fork.
#
# Usage as a library:
#   source lib/seedvr2_unit_args.sh
#   seedvr2_unit_args <input> <output> <model_dir> <model> <resolution> \
#                     <batch> <overlap> <skip> <cap> <prepend> <drop> [extra...]
#   "${SEEDVR2_UNIT_ARGV[@]}"   # positional input first, then flags
#
# Usage as a command (for non-shell callers, e.g. Python):
#   lib/seedvr2_unit_args.sh print <same 11 args> [extra...]
# prints one argument per line, so a caller can read them without re-encoding
# the pinned settings itself.
#
# Note: these paths are whatever the CALLER's filesystem view is. Locally
# pipeline_v3.sh passes container paths under /proj; on a pod they are real
# paths. The builder is deliberately path-agnostic.

set -euo pipefail

seedvr2_unit_args() {
  if (( $# < 11 )); then
    echo "seedvr2_unit_args: need 11 positional args, got $#" >&2
    return 2
  fi
  local input=$1 output=$2 model_dir=$3 model=$4 resolution=$5 batch=$6 \
        overlap=$7 skip=$8 cap=$9
  local prepend=${10} drop=${11}
  shift 11

  # Pinned production settings, proven on the 18 s midpoint reference and every
  # chapter since. See docs/CURRENT_PIPELINE.md. chunk_size is 0 because a
  # durable unit is by definition a single chunk; the worker does the chunking.
  SEEDVR2_UNIT_ARGV=(
    "$input"
    --output "$output"
    --model_dir "$model_dir"
    --dit_model "$model"
    --resolution "$resolution"
    --batch_size "$batch" --uniform_batch_size
    --chunk_size 0 --temporal_overlap "$overlap"
    --skip_first_frames "$skip" --load_cap "$cap"
    --prepend_frames "$prepend" --drop_leading "$drop"
    --color_correction lab
    --vae_encode_tiled --vae_decode_tiled
    --video_backend ffmpeg --10bit --debug
    "$@"
  )
}

# Allow `lib/seedvr2_unit_args.sh print ...` without affecting `source` users.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  case "${1:-}" in
    print)
      shift
      seedvr2_unit_args "$@"
      printf '%s\n' "${SEEDVR2_UNIT_ARGV[@]}"
      ;;
    *)
      echo "Usage: $0 print <input> <output> <model_dir> <model> <resolution> <batch> <overlap> <skip> <cap> <prepend> <drop> [extra...]" >&2
      exit 2
      ;;
  esac
fi

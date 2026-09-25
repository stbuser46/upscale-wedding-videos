#!/usr/bin/env python
"""Resident SeedVR2 engine for durable-unit pods: load the model ONCE, restore
many units.

Why: every unit previously ran a fresh `inference_cli.py` process, paying
~2-4 minutes of weight load + compile-cache replay + first-batch warm-up at 0%
GPU — 10-18% of paid pod time. This engine keeps the loaded/compiled models
resident between units using upstream's OWN multi-file reuse mechanism (the
`runner_cache` dict that directory mode shares across files), so the per-unit
path executes the exact same `process_single_file` code as a fresh CLI run.

Protocol (line-oriented, driven over one persistent ssh session):
    stdin:  one JSON array per request — the EXACT inference_cli argv
            (e.g. ["/workspace/slices/u.mkv", "--output", ...]); or "EXIT".
    stdout: "ENGINE READY pid=<pid>" once after import (the pid doubles as
            the remote PGID: the client launches this script under `setsid`,
            so `kill -- -<pid>` is the abnormal-shutdown kill barrier that
            takes down the engine AND any ffmpeg children); per request, all
            normal CLI logging passes through unchanged, then
            "ENGINE PID <pid>" (persistence evidence for the identity
            canary) and exactly one of
            "ENGINE DONE <frames>" | "ENGINE ERR <message>".

Identity discipline: the per-request body below mirrors `main()`'s single-file
path statement-for-statement (argument parsing, validation, device list,
weight check, format auto-detection) — the TWO deliberate differences are the
persistent `runner_cache` and forcing `cache_dit` on (see `handle()`), both
engine-only. Acceptance requires decoded-frame hash equality of fresh-process
vs resident output (scripts/test_pod_engine_identity.py) before this is
enabled in production (WEDDING_POD_ENGINE=1).
"""

from __future__ import annotations

import json
import os
import platform
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import shutil  # noqa: E402

import inference_cli as cli  # noqa: E402

DEFAULT_VAE = cli.DEFAULT_VAE

# One persistent cache for the pod's lifetime — upstream's directory-mode
# reuse dict. Holds the materialized/compiled model context ('ctx').
RUNNER_CACHE: dict = {}


def _parse(argv: list[str]):
    old = sys.argv
    try:
        sys.argv = ["inference_cli.py"] + list(argv)
        return cli.parse_arguments()
    finally:
        sys.argv = old


def _validate(args) -> str | None:
    """Mirror main()'s pre-flight validation; return an error string or None."""
    if args.vae_encode_tiled and args.vae_encode_tile_overlap >= args.vae_encode_tile_size:
        return "vae encode tile overlap >= tile size"
    if args.vae_decode_tiled and args.vae_decode_tile_overlap >= args.vae_decode_tile_size:
        return "vae decode tile overlap >= tile size"
    if args.video_backend == "ffmpeg" and shutil.which("ffmpeg") is None:
        return "ffmpeg not in PATH"
    return None


def _device_list(args) -> list[str]:
    if platform.system() == "Darwin":
        return ["0"]
    if args.cuda_device:
        return [d.strip() for d in str(args.cuda_device).split(",") if d.strip()]
    return ["0"]


def handle(argv: list[str]) -> int:
    args = _parse(argv)
    # ENGINE-ONLY residency (Codex round-3 finding #8): the pinned argv carries
    # --cache_vae but not --cache_dit, so without this the DiT would be deleted
    # and rematerialized every request — most of the reload the engine exists
    # to avoid. Forcing cache_dit here keeps the DiT resident (offloaded to CPU
    # between requests) exactly as upstream's directory mode does. The classic
    # one-shot path is untouched: it forces runner_cache=None, which makes both
    # cache flags ineffective. Safety: the historical VRAM leak was
    # compile_dit-specific — cache_dit WITHOUT compile holds the eager model
    # resident — and output identity with the one-shot path is proven by the
    # paid identity canary (scripts/test_pod_engine_identity.py) before the
    # WEDDING_POD_ENGINE gate may be enabled.
    args.cache_dit = True
    cli.debug.enabled = args.debug
    err = _validate(args)
    if err:
        raise RuntimeError(err)
    device_list = _device_list(args)
    if not cli.download_weight(dit_model=args.dit_model, vae_model=DEFAULT_VAE,
                               model_dir=args.model_dir, debug=cli.debug):
        raise RuntimeError("model weights unavailable")

    input_type = cli.get_input_type(args.input)
    if input_type not in ("video", "image"):
        raise RuntimeError(f"unsupported input type for engine: {input_type}")
    format_auto_detected = args.output_format is None
    if format_auto_detected:
        args.output_format = "mp4" if input_type == "video" else "png"

    # THE deliberate difference from one-shot main(): a persistent cache so the
    # model context survives between requests. Single-GPU only.
    runner_cache = RUNNER_CACHE if len(device_list) == 1 else None

    frames = cli.process_single_file(args.input, args, device_list, args.output,
                                     format_auto_detected=format_auto_detected,
                                     runner_cache=runner_cache)
    return int(frames or 0)


def main() -> None:
    # The pid IS the process-group id: the client runs this under `setsid`,
    # making this process a session/group leader, so the reported pid lets the
    # client `kill -- -pid` the whole group (engine + ffmpeg children) and
    # verify death — the abnormal-shutdown kill barrier.
    print(f"ENGINE READY pid={os.getpid()}", flush=True)
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        if line == "EXIT":
            break
        try:
            argv = json.loads(line)
            if not isinstance(argv, list):
                raise ValueError("request must be a JSON array of argv strings")
            frames = handle([str(a) for a in argv])
            # Persistence evidence: the identity canary greps these per-request
            # pid lines to prove ONE process served every request.
            print(f"ENGINE PID {os.getpid()}", flush=True)
            print(f"ENGINE DONE {frames}", flush=True)
        except SystemExit as exc:  # argparse error paths call sys.exit
            print(f"ENGINE ERR argparse/exit: {exc}", flush=True)
        except Exception as exc:  # noqa: BLE001 — report, stay alive for next unit
            import traceback
            traceback.print_exc()
            print(f"ENGINE ERR {type(exc).__name__}: {str(exc)[:300]}", flush=True)
    print("ENGINE EXITING", flush=True)


if __name__ == "__main__":
    main()

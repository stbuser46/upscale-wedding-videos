#!/usr/bin/env python
"""Paid acceptance canary for the warm-worker pod engine (WEDDING_POD_ENGINE=1).

THIS SCRIPT COSTS MONEY WHEN RUN: it drives a real, already-provisioned RunPod
pod over ssh through FOUR full SeedVR2 restores. It is written to be run
MANUALLY by the operator against a canary pod — it is never invoked by the
worker, the tests, or CI, and it never creates or terminates pods itself
(bring the pod up with `python -m webapp.cloud.cli` or adopt one from a paused
fleet, and tear it down yourself afterwards).

What it proves — the no-quality-compromise gate for the resident engine
(Codex round-3 findings #3/#4: same-slice-three-times could PASS while
cross-request state was broken, and never exercised the interior→tail shape
change where cached ctx/compiler state is most exposed):

    A = a FULL-SIZE unit-0-style slice (default cap 750 + prepend 4 =
        754 frames through the model, the six-batch production shape)
    B = a DIFFERENT-CONTENT tail-style slice (different cap, prepend 0,
        drop 4 — production's interior/tail request shape)

    (a) one-shot A   — today's production path, fresh inference_cli process
    (b) one-shot B   — fresh process again, for the tail shape
    (c) engine #1 A  — fresh engine process; PRIMES the resident cache
    (d) engine #2 B  — the RESIDENT-REUSE case: model, compiled kernels and
        upstream runner_cache ctx survived request #1, and the request shape
        AND content both changed underneath them

PASS requires ALL of:
    hash(a) == hash(c)   fresh-process parity on the full production shape
    hash(b) == hash(d)   resident-reuse parity across a shape+content change
    persistence proof    both engine requests report the SAME remote pid
                         ("ENGINE PID <pid>" lines in the log), matching the
                         pid announced at "ENGINE READY pid=..."

Identity is judged exactly the way `webapp/worker/slicer.py::slice_frame_hash`
judges peer-cut slices: a sha256 over the per-frame framemd5 hashes of the
decoded frames (mux metadata and timestamps excluded), all four outputs hashed
by the SAME local decoder. Every output name is unique per invocation, so a
stale file from an earlier run can never satisfy a comparison.

The engine forces --cache_dit on (engine-only; see pod_engine.py::handle), so
this canary is ALSO the identity proof for DiT residency. Per-request wall
times are printed — engine #2 minus one-shot B is the measurable reuse saving
— and the run log is grepped for upstream's "reusing cached" lines as direct
evidence the resident models were actually reused rather than reloaded.

REPEAT THIS MATRIX PER GPU CLASS: a PASS binds only the GPU type it ran on
(compile caches and kernels differ per architecture). Before enabling
WEDDING_POD_ENGINE=1 for a fleet, run this canary once on EVERY GPU type the
fleet may rent (each `WEDDING_CLOUD_GPU_PREFERENCE` entry) and record the
runs. Until then the gate must stay off in production.

Usage (all remote paths assume a normally-provisioned pod: the patched tree at
/opt/SeedVR2 INCLUDING pod_engine.py, weights at /opt/models/seedvr2,
/workspace/{slices,units}):

    webapp/.venv/bin/python scripts/test_pod_engine_identity.py \
        --host 194.68.245.109 --port 22101 \
        --slice-a webapp/data/restoration_work/<job>/slices/unit_00000.mkv \
        --slice-b webapp/data/restoration_work/<job>/slices/unit_00009.mkv \
        --cap-a 750 --prepend-a 4 --drop-a 0 \
        --cap-b 350 --prepend-b 0 --drop-b 4 \
        --workdir /tmp/pod-engine-canary

The two slices MUST be different files (different content); B should be a
real tail slice (or any slice restored with a smaller cap and the interior
prepend/drop shape). Smaller caps for B keep the canary cheaper while still
changing every shape parameter the resident state could have latched onto.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from webapp.cloud.executor import (  # noqa: E402
    PodEngine,
    pod_engine_command,
    pod_engine_remote_killer,
    restore_unit,
    restore_unit_via_engine,
)
from webapp.cloud.runpod_api import DEFAULT_SSH_KEY, rsync, run_ssh  # noqa: E402
from webapp.worker.slicer import slice_frame_hash  # noqa: E402


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Decoded-identity canary: one-shot vs resident engine over "
                    "TWO distinct production shapes (COSTS MONEY — needs a live pod)")
    ap.add_argument("--host", required=True, help="pod ssh host")
    ap.add_argument("--port", required=True, type=int, help="pod ssh port")
    ap.add_argument("--key", type=Path, default=DEFAULT_SSH_KEY,
                    help=f"ssh private key (default {DEFAULT_SSH_KEY})")
    ap.add_argument("--slice-a", required=True, type=Path, dest="slice_a",
                    help="full-size priming slice (FFV1 unit slice, unit-0 style)")
    ap.add_argument("--slice-b", required=True, type=Path, dest="slice_b",
                    help="DIFFERENT slice for the tail-shaped request (e.g. a "
                         "real tail slice from the same job)")
    ap.add_argument("--cap-a", type=int, default=750,
                    help="frames to load from slice A (default 750: full unit)")
    ap.add_argument("--prepend-a", type=int, default=4,
                    help="reversed warm-up frames for A (unit-0 style default 4)")
    ap.add_argument("--drop-a", type=int, default=0,
                    help="context frames dropped from A's output (default 0)")
    ap.add_argument("--cap-b", type=int, default=350,
                    help="frames to load from slice B (default 350: a cheaper "
                         "tail-style cap, different from A)")
    ap.add_argument("--prepend-b", type=int, default=0,
                    help="prepend for B (interior/tail style default 0)")
    ap.add_argument("--drop-b", type=int, default=4,
                    help="context frames dropped from B's output (default 4)")
    ap.add_argument("--model", default="seedvr2_ema_3b_fp16.safetensors")
    ap.add_argument("--resolution", type=int, default=1440)
    ap.add_argument("--batch", type=int, default=129)
    ap.add_argument("--overlap", type=int, default=4)
    ap.add_argument("--max-run-s", type=float, default=3600.0)
    ap.add_argument("--workdir", type=Path, default=Path("/tmp/pod-engine-canary"),
                    help="local dir for pulled outputs + the run log")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    if args.slice_a.resolve() == args.slice_b.resolve():
        print("FAIL: --slice-a and --slice-b must be DIFFERENT files — the "
              "cross-request case needs different content (finding #3)")
        return 2
    endpoint = (args.host, args.port)
    runid = time.strftime("%Y%m%d-%H%M%S")
    workdir: Path = args.workdir
    workdir.mkdir(parents=True, exist_ok=True)
    log_path = workdir / f"canary-{runid}.log"

    unit_a = {"seq": 0, "skip": 0, "cap": args.cap_a,
              "prepend": args.prepend_a, "drop": args.drop_a, "new": args.cap_a}
    unit_b = {"seq": 1, "skip": 0, "cap": args.cap_b,
              "prepend": args.prepend_b, "drop": args.drop_b, "new": args.cap_b}
    slices = {"A": f"canary_{runid}_a.mkv", "B": f"canary_{runid}_b.mkv"}
    # Unique output name per RUN (runid) and per REQUEST, so nothing can
    # accidentally satisfy a comparison with a stale or sibling output.
    outs = {"oneshot_a": f"canary_{runid}_oneshot_a.mkv",
            "oneshot_b": f"canary_{runid}_oneshot_b.mkv",
            "engine_a": f"canary_{runid}_engine_a.mkv",
            "engine_b": f"canary_{runid}_engine_b.mkv"}
    common = dict(model=args.model, resolution=args.resolution,
                  batch=args.batch, overlap=args.overlap, log_path=log_path,
                  max_run_s=args.max_run_s)
    times: dict[str, float] = {}

    for label, local in (("A", args.slice_a), ("B", args.slice_b)):
        print(f"[canary] uploading slice {label}: {local.name} -> "
              f"{args.host}:{args.port}:/workspace/slices/{slices[label]}")
        rsync(endpoint, local, f"/workspace/slices/{slices[label]}",
              upload=True, ssh_key=args.key, timeout=1800)

    t_all = time.monotonic()
    # (a)+(b) one-shot CLI — today's production restore path, fresh process each.
    for tag, sl, unit in (("oneshot_a", "A", unit_a), ("oneshot_b", "B", unit_b)):
        print(f"[canary] one-shot restore {tag} (cap={unit['cap']} "
              f"prepend={unit['prepend']} drop={unit['drop']}) ...")
        res = restore_unit(endpoint, args.key, slice_name=slices[sl],
                           out_name=outs[tag], unit=unit, **common)
        if res.status != 0:
            print(f"FAIL: {tag} restore failed ({res.status}): {res.message}")
            return 2
        times[tag] = res.restore_s
        print(f"[canary] {tag} done in {res.restore_s:.0f}s")

    # (c)+(d) resident engine — ONE process serves both requests; request #2
    # changes shape AND content on top of the resident state, which is the
    # exact case the whole feature must not corrupt.
    engine = PodEngine(pod_engine_command(endpoint, args.key),
                       name=f"{args.host}:{args.port}",
                       remote_killer=pod_engine_remote_killer(endpoint, args.key))
    try:
        for tag, sl, unit in (("engine_a", "A", unit_a), ("engine_b", "B", unit_b)):
            print(f"[canary] engine request {tag} (cap={unit['cap']} "
                  f"prepend={unit['prepend']} drop={unit['drop']}) ...")
            res = restore_unit_via_engine(engine, slice_name=slices[sl],
                                          out_name=outs[tag], unit=unit, **common)
            if res.status != 0:
                print(f"FAIL: {tag} restore failed ({res.status}): {res.message}")
                return 2
            times[tag] = res.restore_s
            print(f"[canary] {tag} done in {res.restore_s:.0f}s "
                  f"(frames={res.frames})")
        if not engine.alive:
            print("FAIL: engine died between requests — request #2 did not "
                  "exercise the resident-reuse case")
            return 2
    finally:
        engine.close()

    # Persistence proof: one process must have served BOTH engine requests.
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    pids = re.findall(r"^ENGINE PID (\d+)\s*$", log_text, flags=re.M)
    ready_pid = engine.remote_pgid
    if len(pids) != 2 or len(set(pids)) != 1 or (
            ready_pid is not None and int(pids[0]) != ready_pid):
        print(f"FAIL: engine persistence unproven — ENGINE PID lines {pids}, "
              f"READY pid {ready_pid}. Expected exactly two identical pids "
              f"matching READY.")
        return 2
    print(f"[canary] persistence proven: both requests served by remote pid {pids[0]}")

    # Reuse evidence (finding #8): upstream logs "... reusing cached model" /
    # "Reusing cached runner template" when the resident DiT/VAE are reused
    # instead of reloaded. Grep-able record, printed for the run log.
    reuse_lines = [ln for ln in log_text.splitlines()
                   if "reusing cached" in ln.lower() or "Reusing pre-initialized" in ln]
    print(f"[canary] resident-reuse log evidence: {len(reuse_lines)} line(s)")
    for ln in reuse_lines[:8]:
        print(f"    {ln.strip()}")
    if not reuse_lines:
        print("[canary] WARNING: no 'reusing cached' lines found — the engine "
              "may be reloading models every request (identity unaffected, "
              "but the throughput saving would be missing; investigate)")

    # Pull all four and hash the DECODED frames locally with one decoder.
    hashes: dict[str, str] = {}
    use_docker = shutil.which("ffmpeg") is None
    for tag, remote_name in outs.items():
        local = workdir / remote_name
        print(f"[canary] pulling {remote_name} ...")
        rsync(endpoint, local, f"/workspace/units/{remote_name}", upload=False,
              ssh_key=args.key, timeout=1800)
        if not local.is_file() or local.stat().st_size == 0:
            print(f"FAIL: pulled {remote_name} is missing or empty")
            return 2
        hashes[tag] = slice_frame_hash(local, use_docker=use_docker,
                                       data_dir=workdir if use_docker else None)
        print(f"[canary] {tag}: {hashes[tag]}")
    run_ssh(endpoint, ["rm", "-f"]
            + [f"/workspace/units/{n}" for n in outs.values()]
            + [f"/workspace/slices/{n}" for n in slices.values()],
            ssh_key=args.key, check=False, timeout=60)

    print(f"[canary] total wall time {time.monotonic() - t_all:.0f}s")
    print(f"[canary] per-request wall times: "
          + ", ".join(f"{k}={v:.0f}s" for k, v in times.items()))
    print(f"[canary] reuse saving on the B shape (one-shot B minus engine B): "
          f"{times['oneshot_b'] - times['engine_b']:+.0f}s")

    failures = []
    if hashes["oneshot_a"] != hashes["engine_a"]:
        failures.append("A: one-shot vs engine#1 differ (fresh-process parity broken)")
    if hashes["oneshot_b"] != hashes["engine_b"]:
        failures.append("B: one-shot vs engine#2 differ (resident-reuse parity broken)")
    if not failures:
        print("PASS: one-shot and resident-engine outputs are decoded-identical "
              "for BOTH the full-size priming shape and the changed tail shape, "
              "on one persistent engine process.")
        print("This PASS binds ONLY this pod's GPU class "
              "— repeat the canary on every fleet GPU type and record the runs "
              "before enabling WEDDING_POD_ENGINE=1 in production.")
        return 0
    print("FAIL: decoded-frame hashes differ — DO NOT enable WEDDING_POD_ENGINE:")
    for f in failures:
        print(f"  {f}")
    for tag, digest in hashes.items():
        print(f"  {tag}: {digest}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

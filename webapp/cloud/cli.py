"""Stage A command line: rent a pod, provision it, run one unit, tear it down.

Deliberately standalone. It touches neither the catalog database nor the running
worker, so the cloud path can be proved end to end for a couple of dollars
before any of the worker integration is written.

    python -m webapp.cloud.cli offers
    python -m webapp.cloud.cli up --gpu "RTX PRO 6000"
    python -m webapp.cloud.cli unit <pod> --input slice.mkv --output unit.mkv \
        --skip 0 --cap 754 --prepend 0 --drop 4
    python -m webapp.cloud.cli status
    python -m webapp.cloud.cli down --all

Every pod is recorded in webapp/data/cloud_pods.json *before* the create call
returns, and `down --all` / `reap` terminate anything this project owns. A pod
left running costs real money, so teardown never depends on a clean exit.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .runpod_api import (
    DEFAULT_SSH_KEY,
    POD_NAME_PREFIX,
    PROJECT_ROOT,
    STOCK_BASE_IMAGE,
    RunpodClient,
    RunpodError,
    ensure_ssh_key,
    rsync,
    run_ssh,
    seedvr2_unit_argv,
    ssh_command,
)

STATE_FILE = PROJECT_ROOT / "webapp" / "data" / "cloud_pods.json"
LOCAL_IMAGE = os.environ.get("SEEDVR2_IMAGE", "seedvr2-cuda:v3")
POD_DIR = PROJECT_ROOT / "docker" / "seedvr2-pod"
INDUCTOR_CACHE = PROJECT_ROOT / "webapp" / "data" / "restoration_work" / ".inductor_cache"

REMOTE_TREE = "/opt/SeedVR2"
REMOTE_CACHE = "/opt/inductor_cache"
REMOTE_PROVISION = "/workspace/provision"
REMOTE_SLICES = "/workspace/slices"
REMOTE_UNITS = "/workspace/units"
REMOTE_MODELS = "/opt/models/seedvr2"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ----------------------------------------------------------------- pod registry


def _load_state() -> dict:
    if STATE_FILE.is_file():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_FILE)


def _record(pod_id: str, **fields) -> None:
    state = _load_state()
    entry = state.get(pod_id, {})
    entry.update(fields)
    state[pod_id] = entry
    _save_state(state)


def _forget(pod_id: str) -> None:
    state = _load_state()
    state.pop(pod_id, None)
    _save_state(state)


# ------------------------------------------------------------------- commands


def cmd_offers(args, client: RunpodClient) -> int:
    bal = client.balance()
    print(f"balance ${bal['balance']:.2f}   current spend ${bal['spend_per_hr']:.2f}/h")
    offers = client.gpu_offers(gpu_count=1, secure=not args.community)
    tier = "COMMUNITY" if args.community else "SECURE"
    print(f"\n{tier} offers usable for a 750-frame unit (>=78GB VRAM, in stock):")
    print(f"{'GPU':26} {'VRAM':>5} {'$/h':>6} {'stock':>7} {'vCPU':>5} {'RAM':>5}  {'$/unit':>7}")
    for o in offers:
        if not o.usable:
            continue
        # 750 frames at the locally measured 0.88 fps, before any speed
        # difference this GPU might bring. Benchmark replaces this guess.
        est = o.price_per_hr * (750 / 0.88) / 3600
        print(f"{o.name[:26]:26} {o.vram_gb:>5} {o.price_per_hr:>6.2f} {str(o.stock):>7} "
              f"{str(o.vcpu):>5} {str(o.ram_gb):>5}  {est:>7.2f}")
    print("\nUnusable (too small, wrong vendor, or out of stock):")
    print("  " + ", ".join(f"{o.name}({o.vram_gb}GB)" for o in offers if not o.usable))
    return 0


def _stage_tree() -> Path:
    """Copy the already-patched SeedVR2 tree out of the verified local image.

    Copied, not re-cloned: this guarantees the pinned upstream commit and all
    three local patches on the pod are the same bytes production runs.
    """
    stage = Path(tempfile.mkdtemp(prefix="seedvr2-tree."))
    cid = subprocess.run(["docker", "create", LOCAL_IMAGE],
                         check=True, capture_output=True, text=True).stdout.strip()
    try:
        subprocess.run(["docker", "cp", f"{cid}:{REMOTE_TREE}", str(stage / "SeedVR2")],
                       check=True, capture_output=True)
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
    if not (stage / "SeedVR2" / "inference_cli.py").is_file():
        raise RunpodError(f"exported tree from {LOCAL_IMAGE} has no inference_cli.py")
    return stage


def _provision(endpoint, ssh_key: Path, *, with_cache: bool) -> None:
    log("staging the patched SeedVR2 tree out of the verified image")
    stage = _stage_tree()
    try:
        run_ssh(endpoint, ["mkdir", "-p", REMOTE_PROVISION, REMOTE_SLICES,
                           REMOTE_UNITS, REMOTE_MODELS, REMOTE_TREE],
                ssh_key=ssh_key)
        log(f"uploading SeedVR2 tree ({_du(stage / 'SeedVR2')})")
        rsync(endpoint, f"{stage / 'SeedVR2'}/", f"{REMOTE_TREE}/",
              upload=True, ssh_key=ssh_key)
        for f in ("requirements-pod.txt", "provision_pod.sh"):
            rsync(endpoint, POD_DIR / f, f"{REMOTE_PROVISION}/{f}",
                  upload=True, ssh_key=ssh_key)
        if with_cache and INDUCTOR_CACHE.is_dir():
            log(f"uploading warm compile cache ({_du(INDUCTOR_CACHE)})")
            run_ssh(endpoint, ["mkdir", "-p", REMOTE_CACHE], ssh_key=ssh_key)
            rsync(endpoint, f"{INDUCTOR_CACHE}/", f"{REMOTE_CACHE}/",
                  upload=True, ssh_key=ssh_key, timeout=1800)
        log("running provision_pod.sh (apt, pip, weights, GPU guard)")
        proc = subprocess.run(
            ssh_command(endpoint, ["bash", f"{REMOTE_PROVISION}/provision_pod.sh"], ssh_key),
            text=True,
        )
        if proc.returncode != 0:
            raise RunpodError(f"provisioning failed with status {proc.returncode}")
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _du(path: Path) -> str:
    out = subprocess.run(["du", "-sh", str(path)], capture_output=True, text=True)
    return out.stdout.split()[0] if out.returncode == 0 else "?"


def cmd_up(args, client: RunpodClient) -> int:
    offers = {o.id: o for o in client.gpu_offers(gpu_count=1, secure=not args.community)}
    wanted = [o for o in offers.values()
              if args.gpu.lower() in o.id.lower() or args.gpu.lower() in o.name.lower()]
    usable = [o for o in wanted if o.usable]
    if not usable:
        print(f"no usable in-stock GPU matches {args.gpu!r}. Try: "
              + ", ".join(o.name for o in offers.values() if o.usable), file=sys.stderr)
        return 2
    offer = usable[0]
    log(f"selected {offer.name} ({offer.vram_gb}GB) at ${offer.price_per_hr:.2f}/h, stock {offer.stock}")

    pub = ensure_ssh_key(args.ssh_key)
    name = args.name or f"{POD_NAME_PREFIX}stageA-{int(time.time())}"

    # Record the intent BEFORE creating, so a crash mid-call still leaves a
    # trace for the reaper rather than an invisible billing pod.
    _record(name, state="creating", gpu=offer.id, price_per_hr=offer.price_per_hr,
            requested_at=datetime.now(timezone.utc).isoformat())
    pod = client.create_pod(
        name=name,
        image=args.image,
        gpu_type_ids=[offer.id],
        public_key=pub,
        container_disk_gb=args.disk,
        cloud_type="COMMUNITY" if args.community else "SECURE",
    )
    pod_id = pod["id"]
    _forget(name)
    _record(pod_id, state="created", name=name, gpu=offer.id,
            price_per_hr=pod.get("costPerHr") or offer.price_per_hr,
            created_at=datetime.now(timezone.utc).isoformat())
    log(f"pod {pod_id} created (${pod.get('costPerHr') or offer.price_per_hr}/h) — billing has started")

    try:
        endpoint, live = client.wait_ssh(pod_id, ssh_key=args.ssh_key,
                                        timeout=args.timeout, log=log)
        log(f"ssh up at {endpoint[0]}:{endpoint[1]}")
        _record(pod_id, state="ready", ssh_host=endpoint[0], ssh_port=endpoint[1],
                machine=(live.get("machine") or {}).get("gpuDisplayName"))
        if not args.no_provision:
            _provision(endpoint, args.ssh_key, with_cache=not args.no_cache)
            _record(pod_id, state="provisioned")
        log(f"POD READY: {pod_id}  ssh {endpoint[0]}:{endpoint[1]}")
        print(pod_id)
        return 0
    except Exception as exc:
        log(f"bring-up failed: {exc}")
        if args.keep_on_failure:
            log(f"leaving pod {pod_id} running for inspection — IT IS STILL BILLING")
            log(f"terminate it with: python -m webapp.cloud.cli down {pod_id}")
        else:
            log(f"terminating pod {pod_id} so it stops billing")
            client.terminate_pod(pod_id)
            _record(pod_id, state="terminated")
        return 1


def cmd_provision(args, client: RunpodClient) -> int:
    pod = client.get_pod(args.pod)
    endpoint = client.ssh_endpoint(pod)
    if not endpoint:
        print(f"pod {args.pod} has no ssh endpoint yet", file=sys.stderr)
        return 1
    _provision(endpoint, args.ssh_key, with_cache=not args.no_cache)
    _record(args.pod, state="provisioned")
    return 0


def cmd_unit(args, client: RunpodClient) -> int:
    """Run exactly one durable unit on a pod: push slice, restore, pull result."""
    local_in = Path(args.input).resolve()
    local_out = Path(args.output).resolve()
    if not local_in.is_file():
        print(f"input slice not found: {local_in}", file=sys.stderr)
        return 2
    local_out.parent.mkdir(parents=True, exist_ok=True)

    pod = client.get_pod(args.pod)
    endpoint = client.ssh_endpoint(pod)
    if not endpoint:
        print(f"pod {args.pod} has no ssh endpoint", file=sys.stderr)
        return 1

    remote_in = f"{REMOTE_SLICES}/{local_in.name}"
    remote_out = f"{REMOTE_UNITS}/{local_out.name}"

    t0 = time.monotonic()
    log(f"uploading slice {local_in.name} ({local_in.stat().st_size / 2**20:.0f} MiB)")
    rsync(endpoint, local_in, remote_in, upload=True, ssh_key=args.ssh_key, timeout=1800)
    t_up = time.monotonic() - t0

    # The pinned argv comes from lib/seedvr2_unit_args.sh — the same builder
    # pipeline_v3.sh uses locally, so the cloud path cannot drift.
    argv = seedvr2_unit_argv(
        input_path=remote_in,
        output_path=remote_out,
        model_dir=REMOTE_MODELS,
        model=args.model,
        resolution=args.resolution,
        batch=args.batch,
        overlap=args.overlap,
        skip=args.skip,
        cap=args.cap,
        prepend=args.prepend,
        drop=args.drop,
    )
    log(f"running unit: skip={args.skip} cap={args.cap} prepend={args.prepend} drop={args.drop}")
    t1 = time.monotonic()
    proc = subprocess.run(
        ssh_command(endpoint, ["python", f"{REMOTE_TREE}/inference_cli.py", *argv], args.ssh_key),
        text=True,
    )
    t_run = time.monotonic() - t1
    if proc.returncode != 0:
        log(f"unit FAILED with status {proc.returncode} after {t_run:.0f}s")
        return proc.returncode

    t2 = time.monotonic()
    log("downloading unit")
    rsync(endpoint, local_out, remote_out, upload=False, ssh_key=args.ssh_key, timeout=1800)
    t_down = time.monotonic() - t2

    frames = _probe_frames(local_out)
    price = (_load_state().get(args.pod, {}) or {}).get("price_per_hr") or 0.0
    total = time.monotonic() - t0
    print()
    print(f"  upload    {t_up:8.1f}s")
    print(f"  restore   {t_run:8.1f}s   ({frames} frames, {frames / t_run:.3f} fps)" if frames else
          f"  restore   {t_run:8.1f}s")
    print(f"  download  {t_down:8.1f}s")
    print(f"  total     {total:8.1f}s")
    if price:
        print(f"  cost      ${price * total / 3600:8.3f}  at ${price:.2f}/h")
    print(f"  output    {local_out} ({local_out.stat().st_size / 2**20:.1f} MiB)")
    return 0


def _probe_frames(path: Path) -> int | None:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return int(out.stdout.strip())
    except (TypeError, ValueError):
        return None


def cmd_status(args, client: RunpodClient) -> int:
    bal = client.balance()
    print(f"balance ${bal['balance']:.2f}   current spend ${bal['spend_per_hr']:.2f}/h")
    live = client.our_pods()
    print(f"\nlive pods owned by this project: {len(live)}")
    for p in live:
        ep = client.ssh_endpoint(p) or ("-", 0)
        print(f"  {p['id']}  {p.get('name'):28} {p.get('desiredStatus'):10} "
              f"${p.get('costPerHr', 0)}/h  ssh {ep[0]}:{ep[1]}")
    state = _load_state()
    stale = [k for k, v in state.items() if v.get("state") not in ("terminated",)
             and k not in {p["id"] for p in live}]
    if stale:
        print(f"\nrecorded but not live (safe to forget): {', '.join(stale)}")
    return 0


def cmd_down(args, client: RunpodClient) -> int:
    if args.all:
        targets = [p["id"] for p in client.our_pods()]
        if not targets:
            print("no live pods owned by this project")
            return 0
    else:
        if not args.pod:
            print("give a pod id or --all", file=sys.stderr)
            return 2
        targets = [args.pod]
    failed = 0
    for pod_id in targets:
        try:
            client.terminate_pod(pod_id)
            _record(pod_id, state="terminated",
                    terminated_at=datetime.now(timezone.utc).isoformat())
            log(f"terminated {pod_id}")
        except RunpodError as exc:
            failed += 1
            log(f"FAILED to terminate {pod_id}: {exc}")
    if failed:
        log("SOME PODS MAY STILL BE BILLING — check https://console.runpod.io/pods")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m webapp.cloud.cli",
                                 description="Stage A cloud-unit tooling")
    ap.add_argument("--ssh-key", type=Path, default=DEFAULT_SSH_KEY)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("offers", help="live GPU catalogue with stock and price")
    p.add_argument("--community", action="store_true")
    p.set_defaults(fn=cmd_offers)

    p = sub.add_parser("up", help="create + wait + provision one pod")
    p.add_argument("--gpu", required=True, help="substring of the GPU name or id")
    p.add_argument("--name")
    p.add_argument("--image", default=STOCK_BASE_IMAGE)
    p.add_argument("--disk", type=int, default=120)
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--community", action="store_true")
    p.add_argument("--no-provision", action="store_true")
    p.add_argument("--no-cache", action="store_true", help="skip the warm compile cache")
    p.add_argument("--keep-on-failure", action="store_true",
                   help="leave a failed pod running (IT KEEPS BILLING)")
    p.set_defaults(fn=cmd_up)

    p = sub.add_parser("provision", help="re-run provisioning on a pod")
    p.add_argument("pod")
    p.add_argument("--no-cache", action="store_true")
    p.set_defaults(fn=cmd_provision)

    p = sub.add_parser("unit", help="run one durable unit on a pod")
    p.add_argument("pod")
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--skip", type=int, required=True)
    p.add_argument("--cap", type=int, required=True)
    p.add_argument("--prepend", type=int, default=0)
    p.add_argument("--drop", type=int, default=0)
    p.add_argument("--model", default="seedvr2_ema_3b_fp16.safetensors")
    p.add_argument("--resolution", type=int, default=1440)
    p.add_argument("--batch", type=int, default=129)
    p.add_argument("--overlap", type=int, default=4)
    p.set_defaults(fn=cmd_unit)

    p = sub.add_parser("status", help="balance plus every pod this project owns")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("down", help="terminate a pod, or every pod we own")
    p.add_argument("pod", nargs="?")
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_down)

    args = ap.parse_args(argv)
    try:
        return args.fn(args, RunpodClient())
    except RunpodError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted — run `status` then `down --all` if a pod is live",
              file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

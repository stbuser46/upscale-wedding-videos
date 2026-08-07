#!/usr/bin/env python3
"""RunPod orchestration for the SeedVR2 restoration pipeline.

Stdlib-only CLI around the RunPod v2 REST API plus ssh/scp/rsync, so one
command can take a chapter from local VOB to a validated restored .mkv on a
rented RTX PRO 6000 pod and tear the pod down again.

Auth: RUNPOD_API_KEY env var, or the first line of cloud/.runpod_api_key
(gitignored, chmod 600).

Subcommands:
  gpus                     list RTX PRO 6000-class GPU types with price/availability
  create                   create a pod ready for ssh (bare pytorch base + sshd)
  list                     list pods
  status POD_ID            show pod status and ssh endpoint
  wait-ssh POD_ID          block until the pod accepts ssh
  ssh POD_ID [CMD...]      open a shell / run a command on the pod
  push POD_ID SRC DST      rsync/scp a file or dir to the pod
  pull POD_ID SRC DST      rsync/scp a file or dir from the pod
  bootstrap POD_ID         sync cloud/ + patches, run bootstrap_pod.sh
  terminate POD_ID         terminate (delete) the pod
  run-job ...              full flow: create -> bootstrap -> upload -> pipeline -> download -> terminate
"""

import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

API_BASE = "https://api.runpod.io/v2"
CLOUD_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(CLOUD_DIR)
KEY_DIR = os.path.join(CLOUD_DIR, "keys")
SSH_KEY = os.path.join(KEY_DIR, "id_ed25519")
DEFAULT_IMAGE = "pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime"
DEFAULT_GPU = "NVIDIA RTX PRO 6000 Blackwell Workstation Edition"

# The bare pytorch image has no sshd; install and start it as the container
# command so the orchestrator can reach the pod. PUBLIC_KEY is set per-pod.
START_CMD = (
    "bash -c 'apt-get update && apt-get install -y --no-install-recommends "
    "openssh-server && mkdir -p /run/sshd /root/.ssh && "
    'echo "$PUBLIC_KEY" > /root/.ssh/authorized_keys && '
    "chmod 700 /root/.ssh && chmod 600 /root/.ssh/authorized_keys && "
    "exec /usr/sbin/sshd -D'"
)


def api_key():
    key = os.environ.get("RUNPOD_API_KEY")
    if not key:
        path = os.path.join(CLOUD_DIR, ".runpod_api_key")
        if os.path.exists(path):
            with open(path) as f:
                key = f.readline().strip()
    if not key:
        sys.exit("No API key: set RUNPOD_API_KEY or write cloud/.runpod_api_key")
    return key


def api(method, path, body=None):
    req = urllib.request.Request(
        API_BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key()}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
            return json.loads(data) if data else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        sys.exit(f"RunPod API {method} {path} -> HTTP {e.code}:\n{detail}")


def ensure_ssh_key():
    if not os.path.exists(SSH_KEY):
        os.makedirs(KEY_DIR, exist_ok=True)
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", SSH_KEY,
             "-C", "wedding-restore-runpod"],
            check=True,
        )
    with open(SSH_KEY + ".pub") as f:
        return f.read().strip()


def ssh_endpoint(pod):
    """Extract (ip, port) for the pod's 22/tcp mapping from the runtime object."""
    runtime = pod.get("runtime") or {}
    ports = runtime.get("ports") or []
    for p in ports:
        private = p.get("privatePort") or p.get("private_port")
        if private in (22, "22"):
            ip = p.get("ip") or p.get("publicIp") or p.get("public_ip")
            port = p.get("publicPort") or p.get("public_port")
            if ip and port:
                return ip, int(port)
    return None


def get_pod(pod_id):
    return api("GET", f"/pods/{pod_id}")


def wait_ssh(pod_id, timeout=900):
    """Poll until the pod is RUNNING and sshd accepts connections."""
    deadline = time.time() + timeout
    endpoint = None
    while time.time() < deadline:
        pod = get_pod(pod_id)
        status = pod.get("status")
        endpoint = ssh_endpoint(pod)
        print(f"  pod {pod_id}: {status}"
              + (f", ssh {endpoint[0]}:{endpoint[1]}" if endpoint else ""))
        if status == "RUNNING" and endpoint:
            try:
                with socket.create_connection(endpoint, timeout=5):
                    pass
                # sshd may still be mid-install; verify a real command runs.
                r = run_ssh(endpoint, ["true"], capture=True, check=False)
                if r.returncode == 0:
                    print(f"  ssh ready: {endpoint[0]}:{endpoint[1]}")
                    return endpoint
            except OSError:
                pass
        elif status in ("EXITED", "TERMINATED", "DEAD", "FAILED"):
            sys.exit(f"Pod {pod_id} reached terminal status {status}")
        time.sleep(10)
    sys.exit(f"Timed out waiting for ssh on pod {pod_id}"
             + (f" (last endpoint {endpoint})" if endpoint else ""))


def ssh_base_args(endpoint):
    ip, port = endpoint
    return [
        "-p", str(port), "-i", SSH_KEY,
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ServerAliveInterval=30",
        "-o", "ConnectTimeout=15",
        f"root@{ip}",
    ]


def run_ssh(endpoint, cmd, capture=False, check=True):
    args = ["ssh"] + ssh_base_args(endpoint)
    if cmd:
        args.append(" ".join(shlex.quote(c) for c in cmd) if isinstance(cmd, list) else cmd)
    return subprocess.run(args, capture_output=capture, text=True, check=check)


def transfer(endpoint, src, dst, upload):
    """rsync (with resume + progress) over the pod's ssh port."""
    ip, port = endpoint
    ssh_cmd = (
        f"ssh -p {port} -i {shlex.quote(SSH_KEY)} -o StrictHostKeyChecking=no "
        "-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
    )
    remote = f"root@{ip}:{dst if upload else src}"
    local = src if upload else dst
    pair = [local, remote] if upload else [remote, local]
    subprocess.run(
        ["rsync", "-a", "--partial", "--info=progress2", "-e", ssh_cmd] + pair,
        check=True,
    )


def create_pod(args):
    pubkey = ensure_ssh_key()
    body = {
        "name": args.name,
        "image": args.image,
        "gpu": {"id": args.gpu_id, "count": 1},
        "disk": args.disk,
        "cloud": args.cloud,
        "ports": ["22/tcp"],
        "env": {"PUBLIC_KEY": pubkey},
        "args": START_CMD,
    }
    if args.data_center:
        body["dataCenterId"] = args.data_center
    pod = api("POST", "/pods", body)
    print(json.dumps(pod, indent=2))
    print(f"\ncreated pod: {pod['id']}")
    return pod["id"]


def bootstrap(pod_id, endpoint=None):
    endpoint = endpoint or wait_ssh(pod_id)
    print("[bootstrap] syncing cloud/ scripts and patches")
    run_ssh(endpoint, ["mkdir", "-p", "/workspace/cloud/patches"])
    for f in ("pipeline_v3_cloud.sh", "bootstrap_pod.sh", "requirements-cloud.txt"):
        transfer(endpoint, os.path.join(CLOUD_DIR, f), "/workspace/cloud/", upload=True)
    for f in ("writer-color.patch", "streaming-prepend.patch"):
        transfer(endpoint, os.path.join(PROJ, "docker", "seedvr2", f),
                 "/workspace/cloud/patches/", upload=True)
    print("[bootstrap] running bootstrap_pod.sh on pod")
    run_ssh(endpoint, "bash /workspace/cloud/bootstrap_pod.sh")
    return endpoint


def run_job(args):
    """Full flow for one restoration interval on a fresh pod."""
    pod_id = args.pod or create_pod(args)
    try:
        endpoint = bootstrap(pod_id)
        remote_input = "/workspace/in/" + os.path.basename(args.input)
        run_ssh(endpoint, ["mkdir", "-p", "/workspace/in", "/workspace/out"])
        print(f"[job] uploading {args.input}")
        transfer(endpoint, args.input, remote_input, upload=True)

        remote_out = f"/workspace/out/{args.tag}.mkv"
        env = (
            f"SKIP_BASELINE={args.skip_baseline} "
            f"PIPELINE_FREE_SPACE_RESERVE_BYTES={5 * 1024**3}"
        )
        pipeline = (
            f"{env} nohup bash /workspace/cloud/pipeline_v3_cloud.sh "
            f"{shlex.quote(remote_input)} {args.start} {args.duration} "
            f"{shlex.quote(remote_out)} {shlex.quote(args.tag)} "
            f"> /workspace/out/{args.tag}.pipeline.log 2>&1 "
            f"& echo started $!"
        )
        print("[job] starting pipeline")
        run_ssh(endpoint, pipeline)

        log = f"/workspace/out/{args.tag}.pipeline.log"
        last_size = 0
        while True:
            time.sleep(60)
            r = run_ssh(
                endpoint,
                f"tail -c +{last_size + 1} {log} | head -c 200000; "
                f"echo; echo __SIZE__$(stat -c%s {log}); "
                f"echo __DONE__$(test -s {shlex.quote(remote_out)} && echo yes || echo no); "
                f"echo __ALIVE__$(pgrep -f pipeline_v3_cloud.sh >/dev/null && echo yes || echo no)",
                capture=True, check=False,
            )
            out = r.stdout or ""
            for line in out.splitlines():
                if line.startswith("__SIZE__"):
                    last_size = int(line[8:] or 0)
                elif not line.startswith(("__DONE__", "__ALIVE__")):
                    print(line)
            done = "__DONE__yes" in out
            alive = "__ALIVE__yes" in out
            if done:
                break
            if not alive:
                sys.exit(f"[job] pipeline exited without producing {remote_out}; "
                         f"pod {pod_id} kept for inspection")

        print("[job] downloading results")
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        transfer(endpoint, remote_out, args.output, upload=False)
        transfer(endpoint, log, args.output + ".pipeline.log", upload=False)
        print(f"[job] done: {args.output}")
    except BaseException:
        print(f"[job] FAILED — pod {pod_id} left running for inspection "
              f"(terminate with: cloud/runpod.py terminate {pod_id})")
        raise
    if not args.keep:
        api("DELETE", f"/pods/{pod_id}")
        print(f"[job] pod {pod_id} terminated")
    else:
        print(f"[job] pod {pod_id} kept (--keep)")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("gpus")
    sub.add_parser("list")

    def pod_arg(sp):
        sp.add_argument("pod_id")

    for name in ("status", "wait-ssh", "terminate", "bootstrap"):
        pod_arg(sub.add_parser(name))

    sp = sub.add_parser("ssh")
    pod_arg(sp)
    sp.add_argument("command", nargs=argparse.REMAINDER)

    for name in ("push", "pull"):
        sp = sub.add_parser(name)
        pod_arg(sp)
        sp.add_argument("src")
        sp.add_argument("dst")

    def create_args(sp):
        sp.add_argument("--name", default="wedding-restore")
        sp.add_argument("--image", default=DEFAULT_IMAGE)
        sp.add_argument("--gpu-id", default=DEFAULT_GPU)
        sp.add_argument("--cloud", default="SECURE", choices=["SECURE", "COMMUNITY"])
        sp.add_argument("--disk", type=int, default=150)
        sp.add_argument("--data-center", default=None)

    create_args(sub.add_parser("create"))

    sp = sub.add_parser("run-job")
    create_args(sp)
    sp.add_argument("--pod", help="reuse an existing pod instead of creating one")
    sp.add_argument("--input", required=True, help="local VOB (or slice) to upload")
    sp.add_argument("--start", required=True, help="start seconds within input")
    sp.add_argument("--duration", required=True, help="duration seconds")
    sp.add_argument("--output", required=True, help="local path for restored .mkv")
    sp.add_argument("--tag", required=True, help="work/output tag (simple name)")
    sp.add_argument("--skip-baseline", default="1", choices=["0", "1"])
    sp.add_argument("--keep", action="store_true", help="do not terminate pod at end")

    args = p.parse_args()

    if args.cmd == "gpus":
        data = api("GET", "/catalog/gpus?include=AVAILABILITY&product=POD")
        for g in data.get("gpus", []):
            if "6000" in g.get("id", "") or "6000" in g.get("name", ""):
                print(json.dumps(g, indent=2))
    elif args.cmd == "list":
        print(json.dumps(api("GET", "/pods"), indent=2))
    elif args.cmd == "status":
        pod = get_pod(args.pod_id)
        print(json.dumps(pod, indent=2))
        ep = ssh_endpoint(pod)
        if ep:
            print(f"\nssh: ssh -p {ep[1]} -i {SSH_KEY} root@{ep[0]}")
    elif args.cmd == "wait-ssh":
        wait_ssh(args.pod_id)
    elif args.cmd == "ssh":
        ep = ssh_endpoint(get_pod(args.pod_id)) or sys.exit("no ssh endpoint yet")
        cmd = " ".join(args.command) if args.command else None
        os.execvp("ssh", ["ssh"] + ssh_base_args(ep) + ([cmd] if cmd else []))
    elif args.cmd == "push":
        ep = ssh_endpoint(get_pod(args.pod_id)) or sys.exit("no ssh endpoint yet")
        transfer(ep, args.src, args.dst, upload=True)
    elif args.cmd == "pull":
        ep = ssh_endpoint(get_pod(args.pod_id)) or sys.exit("no ssh endpoint yet")
        transfer(ep, args.src, args.dst, upload=False)
    elif args.cmd == "bootstrap":
        bootstrap(args.pod_id)
    elif args.cmd == "terminate":
        api("DELETE", f"/pods/{args.pod_id}")
        print(f"terminated {args.pod_id}")
    elif args.cmd == "create":
        create_pod(args)
    elif args.cmd == "run-job":
        run_job(args)


if __name__ == "__main__":
    main()

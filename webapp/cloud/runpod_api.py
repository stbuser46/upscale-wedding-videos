"""Stdlib-only RunPod control-plane client, plus the ssh/rsync plumbing used to
drive a pod.

Ported from the `cloud/runpod.py` deleted in commit 9421770 (recover it with
`git show 9421770^:cloud/runpod.py`). Two things changed in the port:

* **Transport.** The old client spoke `https://api.runpod.io/v2`, which is
  superseded. Pod CRUD is now REST at `https://rest.runpod.io/v1`, and the GPU
  catalogue with its stock levels exists *only* on GraphQL at
  `https://api.runpod.io/graphql`. The request body changed shape too
  (`imageName`/`gpuTypeIds`/`containerDiskInGb`/`cloudType`, not
  `image`/`gpu`/`disk`/`cloud`).
* **Error handling.** The old client called `sys.exit()` from library
  functions. This one raises `RunpodError`, because the restoration worker
  cannot have a helper kill the process in the middle of a job.

Everything here is deliberately dependency-free so it can run inside the
worker's venv without adding packages.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

REST_BASE = "https://rest.runpod.io/v1"
GRAPHQL_URL = "https://api.runpod.io/graphql"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KEY_FILE = PROJECT_ROOT / "webapp" / "data" / "runpod.env"
DEFAULT_SSH_KEY = PROJECT_ROOT / "cloud" / "keys" / "id_ed25519"

# Every pod this project creates carries this prefix, so the reaper can
# recognise its own strays without a database.
POD_NAME_PREFIX = "wedding-"

# A unit peaks near 60 GiB of VRAM and ~63 GiB of host RAM, and ends with a
# 750-frame 10-bit HEVC encode on the CPU. Refuse flavours that cannot hold it.
MIN_VRAM_GB = 78
MIN_RAM_PER_GPU_GB = 80   # a unit peaks ~63 GiB host RAM; 80 leaves headroom without over-filtering machines
MIN_VCPU_PER_GPU = 8

RETRY_STATUS = {408, 429, 500, 502, 503, 504}
TERMINAL_POD_STATUS = {"EXITED", "TERMINATED", "DEAD", "FAILED"}

# A pod running the *stock* pytorch base has no sshd, so the container start
# command installs and starts one. Using the public base avoids pushing a
# 13.6 GB image to a registry; the orchestrator then rsyncs up the 6.2 MB
# already-patched SeedVR2 tree copied out of the verified local image.
STOCK_BASE_IMAGE = "pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime"
STOCK_START_CMD = [
    "bash", "-lc",
    "set -e; "
    "export DEBIAN_FRONTEND=noninteractive; "
    "apt-get update -qq; "
    "apt-get install -y -qq --no-install-recommends openssh-server rsync >/dev/null; "
    "mkdir -p /run/sshd /root/.ssh /workspace; chmod 700 /root/.ssh; "
    'printf "%s\\n" "$PUBLIC_KEY" > /root/.ssh/authorized_keys; '
    "chmod 600 /root/.ssh/authorized_keys; "
    "ssh-keygen -A; "
    "sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config; "
    "sed -i 's/^#\\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config; "
    "exec /usr/sbin/sshd -D -e",
]

# Cloudflare fronts api.runpod.io and rejects urllib's default User-Agent with
# HTTP 403 "error code: 1010" (banned browser signature). Send a real one.
USER_AGENT = "wedding-restoration/1.0 (+https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler)"


class RunpodError(RuntimeError):
    """Any failure talking to RunPod, or a pod reaching a terminal state."""


def load_api_key(key_file: Path | None = None) -> str:
    """Return the API key from the environment, else from a mode-600 key file.

    Never logged, never passed on a command line, never written to a systemd
    unit (where `systemctl show` would expose it).
    """
    key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if key:
        return key

    candidates: list[Path] = []
    if key_file is not None:
        candidates.append(Path(key_file))
    candidates.append(DEFAULT_KEY_FILE)
    candidates.append(PROJECT_ROOT / "cloud" / ".runpod_api_key")

    for path in candidates:
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                name, _, value = line.partition("=")
                if name.strip() == "RUNPOD_API_KEY":
                    return value.strip().strip("'\"")
            else:
                return line
    raise RunpodError(
        "No RunPod API key: set RUNPOD_API_KEY or write "
        f"{DEFAULT_KEY_FILE} containing RUNPOD_API_KEY=..."
    )


@dataclass(frozen=True)
class GpuOffer:
    """One GPU type as the account can actually rent it right now."""

    id: str
    name: str
    vram_gb: int
    price_per_hr: float | None
    stock: str | None
    vcpu: int | None
    ram_gb: int | None
    max_gpus: int | None

    @property
    def usable(self) -> bool:
        return (
            self.vram_gb >= MIN_VRAM_GB
            and self.price_per_hr is not None
            and (self.stock or "").lower() not in {"", "none"}
            # AMD/ROCm cannot run the pinned CUDA image.
            and "instinct" not in self.name.lower()
            and "mi300" not in self.name.lower()
        )


class RunpodClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: int = 60,
        retries: int = 4,
        key_file: Path | None = None,
    ) -> None:
        self._key = api_key or load_api_key(key_file)
        self.timeout = timeout
        self.retries = retries

    # ---------------------------------------------------------------- transport

    def _send(self, req: urllib.request.Request, *, what: str) -> Any:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read()
                    return json.loads(body) if body else None
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:800]
                if exc.code in RETRY_STATUS and attempt < self.retries - 1:
                    last = RunpodError(f"{what}: HTTP {exc.code}: {detail}")
                    time.sleep(2**attempt)
                    continue
                raise RunpodError(f"{what}: HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                last = RunpodError(f"{what}: {exc}")
                if attempt < self.retries - 1:
                    time.sleep(2**attempt)
                    continue
                raise last from exc
        raise last or RunpodError(f"{what}: exhausted retries")

    def _rest(self, method: str, path: str, body: dict | None = None) -> Any:
        req = urllib.request.Request(
            REST_BASE + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        return self._send(req, what=f"{method} {path}")

    def _graphql(self, query: str, variables: dict | None = None) -> Any:
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        req = urllib.request.Request(
            GRAPHQL_URL,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        result = self._send(req, what="graphql")
        if isinstance(result, dict) and result.get("errors"):
            raise RunpodError(f"graphql: {json.dumps(result['errors'])[:500]}")
        return (result or {}).get("data") or {}

    # ------------------------------------------------------------------ account

    def balance(self) -> dict[str, float]:
        data = self._graphql(
            "query { myself { clientBalance currentSpendPerHr } }"
        )
        me = data.get("myself") or {}
        return {
            "balance": float(me.get("clientBalance") or 0.0),
            "spend_per_hr": float(me.get("currentSpendPerHr") or 0.0),
        }

    # ------------------------------------------------------------- gpu catalogue

    def gpu_offers(self, *, gpu_count: int = 1, secure: bool = True) -> list[GpuOffer]:
        """Live GPU catalogue with prices and stock, cheapest usable first.

        Only GraphQL exposes stock levels, and stock is what actually limits how
        wide the fan-out can go.
        """
        data = self._graphql(
            """
            query Offers($n: Int!, $secure: Boolean!) {
              gpuTypes {
                id
                displayName
                memoryInGb
                maxGpuCount
                lowestPrice(input: {gpuCount: $n, secureCloud: $secure}) {
                  uninterruptablePrice
                  stockStatus
                  minVcpu
                  minMemory
                }
              }
            }
            """,
            {"n": gpu_count, "secure": secure},
        )
        offers: list[GpuOffer] = []
        for entry in data.get("gpuTypes") or []:
            price = entry.get("lowestPrice") or {}
            offers.append(
                GpuOffer(
                    id=entry["id"],
                    name=entry.get("displayName") or entry["id"],
                    vram_gb=int(entry.get("memoryInGb") or 0),
                    price_per_hr=(
                        float(price["uninterruptablePrice"])
                        if price.get("uninterruptablePrice") is not None
                        else None
                    ),
                    stock=price.get("stockStatus"),
                    vcpu=price.get("minVcpu"),
                    ram_gb=price.get("minMemory"),
                    max_gpus=entry.get("maxGpuCount"),
                )
            )
        offers.sort(key=lambda o: (o.price_per_hr is None, o.price_per_hr or 0.0))
        return offers

    def usable_gpu_offers(self, **kwargs: Any) -> list[GpuOffer]:
        return [o for o in self.gpu_offers(**kwargs) if o.usable]

    # ----------------------------------------------------------------- pod CRUD

    def create_pod(
        self,
        *,
        name: str,
        image: str,
        gpu_type_ids: Sequence[str],
        public_key: str,
        container_disk_gb: int = 40,
        volume_gb: int = 0,
        cloud_type: str = "SECURE",
        env: dict[str, str] | None = None,
        data_center_ids: Sequence[str] | None = None,
        registry_auth_id: str | None = None,
        start_cmd: Sequence[str] | None = None,
    ) -> dict:
        """Create one single-GPU pod running our execution image.

        `dockerEntrypoint`/`dockerStartCmd` are set explicitly because the base
        image's ENTRYPOINT is the SeedVR2 inference CLI, and a pod must instead
        come up as a long-lived ssh host.
        """
        if not name.startswith(POD_NAME_PREFIX):
            raise RunpodError(
                f"pod name must start with {POD_NAME_PREFIX!r} so the reaper can find it"
            )
        body: dict[str, Any] = {
            "name": name,
            "imageName": image,
            "gpuTypeIds": list(gpu_type_ids),
            "gpuCount": 1,
            "cloudType": cloud_type,
            "containerDiskInGb": container_disk_gb,
            "volumeInGb": volume_gb,
            "ports": ["22/tcp"],
            "env": {"PUBLIC_KEY": public_key, **(env or {})},
            "dockerEntrypoint": [],
            "dockerStartCmd": list(
                start_cmd
                if start_cmd is not None
                else (STOCK_START_CMD if image == STOCK_BASE_IMAGE
                      else ["/usr/local/bin/pod_start.sh"])
            ),
            "interruptible": False,
            "minRAMPerGPU": MIN_RAM_PER_GPU_GB,
            "minVCPUPerGPU": MIN_VCPU_PER_GPU,
            "gpuTypePriority": "availability",
        }
        if volume_gb:
            body["volumeMountPath"] = "/workspace"
        if data_center_ids:
            body["dataCenterIds"] = list(data_center_ids)
        if registry_auth_id:
            body["containerRegistryAuthId"] = registry_auth_id
        pod = self._rest("POST", "/pods", body)
        if not isinstance(pod, dict) or not pod.get("id"):
            raise RunpodError(f"create_pod returned no id: {pod!r}")
        return pod

    def get_pod(self, pod_id: str) -> dict:
        pod = self._rest("GET", f"/pods/{pod_id}")
        if not isinstance(pod, dict):
            raise RunpodError(f"get_pod({pod_id}) returned {pod!r}")
        return pod

    def list_pods(self) -> list[dict]:
        pods = self._rest("GET", "/pods")
        if isinstance(pods, dict):
            pods = pods.get("pods") or pods.get("data") or []
        return list(pods or [])

    def our_pods(self) -> list[dict]:
        return [p for p in self.list_pods() if (p.get("name") or "").startswith(POD_NAME_PREFIX)]

    def terminate_pod(self, pod_id: str) -> bool:
        """Delete a pod so it stops billing. Idempotent: a pod that is already
        gone counts as success, because the caller's goal is 'not billing'."""
        try:
            self._rest("DELETE", f"/pods/{pod_id}")
            return True
        except RunpodError as exc:
            if "HTTP 404" in str(exc) or "HTTP 400" in str(exc):
                return True
            raise

    # -------------------------------------------------------------------- ssh

    @staticmethod
    def ssh_endpoint(pod: dict) -> tuple[str, int] | None:
        """Extract (host, port) for the pod's 22/tcp mapping.

        REST v1 reports `publicIp` plus a `portMappings` object; older/other
        shapes put it in a `ports` array. Handle both.
        """
        ip = pod.get("publicIp") or pod.get("ip")
        mappings = pod.get("portMappings") or {}
        if isinstance(mappings, dict):
            for key in ("22", 22):
                if mappings.get(key):
                    port = int(mappings[key])
                    if ip:
                        return str(ip), port
        for entry in pod.get("ports") or []:
            if not isinstance(entry, dict):
                continue
            private = entry.get("privatePort") or entry.get("private_port")
            if private in (22, "22"):
                host = entry.get("ip") or entry.get("publicIp") or ip
                port = entry.get("publicPort") or entry.get("public_port")
                if host and port:
                    return str(host), int(port)
        return None

    def wait_ssh(
        self,
        pod_id: str,
        *,
        ssh_key: Path = DEFAULT_SSH_KEY,
        timeout: int = 900,
        poll: int = 10,
        log: Any = None,
    ) -> tuple[tuple[str, int], dict]:
        """Poll until the pod is running and sshd answers a real command."""
        deadline = time.monotonic() + timeout
        last_seen = "?"
        while time.monotonic() < deadline:
            pod = self.get_pod(pod_id)
            status = pod.get("desiredStatus") or pod.get("status") or "?"
            endpoint = self.ssh_endpoint(pod)
            last_seen = status
            if log:
                log(f"pod {pod_id}: {status}" + (f" ssh {endpoint[0]}:{endpoint[1]}" if endpoint else ""))
            if str(status).upper() in TERMINAL_POD_STATUS:
                raise RunpodError(f"pod {pod_id} reached terminal status {status}")
            if endpoint:
                try:
                    with socket.create_connection(endpoint, timeout=5):
                        pass
                    if run_ssh(endpoint, ["true"], ssh_key=ssh_key, check=False).returncode == 0:
                        return endpoint, pod
                except OSError:
                    pass
            time.sleep(poll)
        raise RunpodError(
            f"timed out after {timeout}s waiting for ssh on pod {pod_id} (last status {last_seen})"
        )


# ------------------------------------------------------------------ ssh helpers


def ensure_ssh_key(ssh_key: Path = DEFAULT_SSH_KEY) -> str:
    """Return the orchestrator's public key, generating the pair on first use."""
    ssh_key = Path(ssh_key)
    if not ssh_key.exists():
        ssh_key.parent.mkdir(parents=True, exist_ok=True)
        ssh_key.parent.chmod(0o700)
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(ssh_key),
             "-C", "wedding-restoration-orchestrator"],
            check=True,
            capture_output=True,
        )
    ssh_key.chmod(0o600)
    return Path(f"{ssh_key}.pub").read_text(encoding="utf-8").strip()


def ssh_args(endpoint: tuple[str, int], ssh_key: Path = DEFAULT_SSH_KEY) -> list[str]:
    host, port = endpoint
    return [
        "-p", str(port),
        "-i", str(ssh_key),
        # Pods are ephemeral and get a fresh host key every time, so pinning
        # known_hosts would guarantee failure rather than add security.
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=6",
        "-o", "ConnectTimeout=20",
        f"root@{host}",
    ]


def ssh_command(
    endpoint: tuple[str, int],
    command: Sequence[str] | str,
    ssh_key: Path = DEFAULT_SSH_KEY,
) -> list[str]:
    """Build the full argv for running `command` on the pod.

    Returned rather than executed so the worker can hand it to its existing
    `_run_logged`, which already tees output to the job log, beats the
    heartbeat, and honours cancel and shutdown.
    """
    remote = command if isinstance(command, str) else " ".join(shlex.quote(c) for c in command)
    return ["ssh"] + ssh_args(endpoint, ssh_key) + [remote]


def run_ssh(
    endpoint: tuple[str, int],
    command: Sequence[str] | str,
    *,
    ssh_key: Path = DEFAULT_SSH_KEY,
    check: bool = True,
    capture: bool = True,
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ssh_command(endpoint, command, ssh_key),
        check=check,
        capture_output=capture,
        text=True,
        timeout=timeout,
    )


def rsync(
    endpoint: tuple[str, int],
    local: Path,
    remote: str,
    *,
    upload: bool,
    ssh_key: Path = DEFAULT_SSH_KEY,
    timeout: int | None = None,
) -> None:
    """Move one file to or from a pod, resumably."""
    host, port = endpoint
    shell = (
        f"ssh -p {port} -i {shlex.quote(str(ssh_key))} "
        "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        "-o LogLevel=ERROR -o ConnectTimeout=20"
    )
    far = f"root@{host}:{remote}"
    pair = [str(local), far] if upload else [far, str(local)]
    result = subprocess.run(
        ["rsync", "-a", "--partial", "--inplace", "-e", shell, *pair],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        direction = "upload" if upload else "download"
        raise RunpodError(
            f"rsync {direction} failed ({result.returncode}): {result.stderr.strip()[:400]}"
        )


def seedvr2_unit_argv(
    *,
    input_path: str,
    output_path: str,
    model_dir: str,
    model: str,
    resolution: int,
    batch: int,
    overlap: int,
    skip: int,
    cap: int,
    prepend: int,
    drop: int,
    extra: Iterable[str] = ("--compile_vae", "--cache_vae"),
) -> list[str]:
    """Render the pinned unit argv by *calling the shared shell builder*.

    Deliberately not reimplemented here. `lib/seedvr2_unit_args.sh` is the one
    source of truth for the pinned settings, shared with `pipeline_v3.sh`; the
    previous cloud attempt forked those flags and immediately rotted.
    """
    builder = PROJECT_ROOT / "lib" / "seedvr2_unit_args.sh"
    result = subprocess.run(
        [str(builder), "print", input_path, output_path, model_dir, model,
         str(resolution), str(batch), str(overlap), str(skip), str(cap),
         str(prepend), str(drop), *extra],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line != ""]

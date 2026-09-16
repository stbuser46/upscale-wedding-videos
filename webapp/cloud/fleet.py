"""The cloud slot pool: provision N pods concurrently, hand them out as slots,
and guarantee they are all terminated when done.

Design: units are independent, so the backlog is embarrassingly parallel. The
fleet brings up pods concurrently and drops each into a slot queue the moment it
is provisioned — so unit dispatch starts on the first ready pod, not after all N.
Every pod is written to the `cloud_pods` ledger BEFORE the create call returns,
so a crash can never leave a billable pod the reaper can't find.
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from queue import Queue, Empty
from typing import Callable

from webapp.db import connect
from .runpod_api import (
    DEFAULT_SSH_KEY, POD_NAME_PREFIX, RunpodClient, RunpodError,
    ensure_ssh_key, rsync, run_ssh, ssh_command,
)

PROVISION_SCRIPT = "provision_pod.sh"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Slot:
    pod_id: str
    name: str
    endpoint: tuple[str, int]
    gpu_type: str
    hourly_rate: float
    created_at: float  # monotonic, for TTL


@dataclass
class FleetConfig:
    image: str
    gpu_type_ids: list[str]
    cloud_type: str = "SECURE"
    max_slots: int = 16
    spend_cap_usd: float = 250.0
    pod_ttl_s: float = 3600.0          # terminate a pod older than this (unit ~15 min)
    container_disk_gb: int = 40           # a pod uses ~16 GiB (base+weights+cache); 120 caused 'no resources' rejections
    bring_up_attempts: int = 3            # recreate on a create-reject or no-ssh dud
    ssh_timeout_s: int = 300              # working pods answer in ~100 s; give up on a dud fast
    ssh_key: Path = DEFAULT_SSH_KEY
    provision_dir: Path = field(default=None)   # local docker/seedvr2-pod dir
    inductor_cache: Path = field(default=None)  # local warm cache to upload


class CloudFleet:
    def __init__(self, settings, job, config: FleetConfig, *, log: Callable[[str], None] = print):
        self.settings = settings
        self.job = job
        self.cfg = config
        self.log = log
        self.client = RunpodClient()
        self.slot_queue: "Queue[Slot]" = Queue()
        self._pubkey = ensure_ssh_key(config.ssh_key)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._slots: dict[str, Slot] = {}          # pod_id -> Slot (live)
        self._lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._ready = 0
        self._requested = 0

    # ---------------------------------------------------------------- ledger

    def _ledger(self, pod_name: str, **fields) -> None:
        cols = ", ".join(f"{k}=:{k}" for k in fields)
        with connect(self.settings.database_path) as db:
            db.execute(f"UPDATE cloud_pods SET {cols} WHERE name=:name",
                       {**fields, "name": pod_name})

    def _ledger_insert(self, pod_name: str, gpu_type: str, rate: float) -> None:
        with connect(self.settings.database_path) as db:
            db.execute(
                """INSERT INTO cloud_pods (name, gpu_type, hourly_rate, cloud_type,
                                           state, job_id, created_at)
                   VALUES (:name, :gpu, :rate, :cloud, 'creating', :job_id, :now)""",
                {"name": pod_name, "gpu": gpu_type, "rate": rate,
                 "cloud": self.cfg.cloud_type, "job_id": self.job["id"], "now": _now_iso()},
            )

    def spend_so_far(self) -> float:
        with connect(self.settings.database_path) as db:
            rows = db.execute(
                "SELECT hourly_rate, created_at, terminated_at FROM cloud_pods WHERE job_id=?",
                (self.job["id"],),
            ).fetchall()
        now = datetime.now(timezone.utc)
        total = 0.0
        for r in rows:
            if not r["created_at"] or r["hourly_rate"] is None:
                continue
            start = datetime.fromisoformat(r["created_at"])
            end = datetime.fromisoformat(r["terminated_at"]) if r["terminated_at"] else now
            total += r["hourly_rate"] * max(0.0, (end - start).total_seconds()) / 3600.0
        return total

    # ------------------------------------------------------------- provisioning

    def provision(self, n_slots: int) -> None:
        """Start bringing up `n_slots` pods concurrently. Returns immediately;
        ready pods appear on `slot_queue`."""
        self._requested = n_slots
        for i in range(n_slots):
            t = threading.Thread(target=self._bring_up, args=(i,), daemon=True,
                                 name=f"fleet-pod-{i}")
            t.start()
            self._threads.append(t)

    def provisioning_done(self) -> bool:
        """True once every bring-up thread has finished (readied or failed)."""
        return bool(self._threads) and all(not t.is_alive() for t in self._threads)

    def acquire_slot(self, stop_check: Callable[[], bool] = lambda: False, poll: float = 10.0):
        """Block until a slot is free, then return it. Units legitimately queue
        for slots when there are more units than pods (258 units over 16 slots is
        the whole point), so this waits as long as the fleet could still hand one
        out — NOT a fixed timeout. Returns None if `stop_check` fires or the fleet
        can never produce a slot (all bring-ups finished and none succeeded)."""
        while True:
            if stop_check():
                return None
            try:
                return self.slot_queue.get(timeout=poll)
            except Empty:
                with self._lock:
                    no_live_slots = len(self._slots) == 0
                if self.provisioning_done() and no_live_slots:
                    return None  # every pod failed to come up; give up

    def retire_slot(self, slot: "Slot") -> None:
        """Terminate a pod that has no more work, immediately, so it stops
        billing the moment its last unit is done rather than idling until the
        whole job ends. Fixes the tail where early-finishing pods sit idle."""
        with self._lock:
            self._slots.pop(slot.pod_id, None)
            self._ready = max(0, self._ready - 1)
        self._terminate(slot.pod_id, slot.name)
        self.log(f"[fleet] retired idle pod {slot.pod_id} (no more units)")

    def _bring_up(self, index: int) -> None:
        if self._stop.is_set():
            return
        # Spend-cap guard before spending on another pod.
        if self.spend_so_far() >= self.cfg.spend_cap_usd:
            self.log(f"[fleet] spend cap ${self.cfg.spend_cap_usd:.0f} reached; not launching pod {index}")
            return
        name = f"{POD_NAME_PREFIX}{self.job['public_id']}-{index}"
        gpu_type = self.cfg.gpu_type_ids[0]
        rate = 0.0
        offers = {o.id: o for o in self.client.usable_gpu_offers(
            secure=self.cfg.cloud_type == "SECURE")}
        for gid in self.cfg.gpu_type_ids:
            if gid in offers:
                gpu_type, rate = gid, offers[gid].price_per_hr or 0.0
                break
        self._ledger_insert(name, gpu_type, rate)

        # A bring-up can fail two ways that a FRESH pod usually fixes: RunPod
        # rejects the create (the machine it picked is full), or the pod comes up
        # RUNNING but never gets an ssh port mapped (a per-pod fluke). So retry
        # the whole create->ssh cycle on a DIFFERENT machine, terminating the dud
        # each time, rather than patiently waiting 15 min on one that will never
        # answer. Observed: 2 of 5 create-rejected + 1 of 3 no-ssh in one run.
        last_exc: Exception | None = None
        for attempt in range(self.cfg.bring_up_attempts):
            if self._stop.is_set():
                return
            if self.spend_so_far() >= self.cfg.spend_cap_usd:
                self.log(f"[fleet] spend cap reached; stop bringing up pod {index}")
                break
            pod_id = None
            try:
                pod = self._create_pod(name)
                pod_id = pod["id"]
                self._ledger(name, pod_id=pod_id, state="creating",
                             hourly_rate=pod.get("costPerHr") or rate)
                self.log(f"[fleet] pod {index} {pod_id} creating "
                         f"(attempt {attempt + 1}/{self.cfg.bring_up_attempts}, "
                         f"{gpu_type} ${pod.get('costPerHr') or rate}/h)")
                t_ssh = time.monotonic()
                endpoint, live = self.client.wait_ssh(
                    pod_id, ssh_key=self.cfg.ssh_key, timeout=self.cfg.ssh_timeout_s,
                    log=lambda m: None)
                self.log(f"[fleet] pod {index} ssh up at {endpoint[0]}:{endpoint[1]} ({time.monotonic() - t_ssh:.0f}s)")
                if self._stop.is_set():
                    self._terminate(pod_id, name)
                    return
                self._provision_pod(endpoint, index)
                slot = Slot(pod_id=pod_id, name=name, endpoint=endpoint, gpu_type=gpu_type,
                            hourly_rate=pod.get("costPerHr") or rate, created_at=time.monotonic())
                with self._lock:
                    self._slots[pod_id] = slot
                    self._ready += 1
                self._ledger(name, state="ready", ssh_host=endpoint[0], ssh_port=endpoint[1],
                             ready_at=_now_iso(), last_seen_at=_now_iso())
                self.log(f"[fleet] pod {index} {pod_id} READY at {endpoint[0]}:{endpoint[1]}")
                self.slot_queue.put(slot)
                return  # success
            except Exception as exc:
                last_exc = exc
                self.log(f"[fleet] pod {index} attempt {attempt + 1} failed: {str(exc)[:120]}")
                if pod_id is not None:
                    try:
                        self.client.terminate_pod(pod_id)  # never leave the dud billing
                    except RunpodError:
                        pass
        self.log(f"[fleet] pod {index} gave up after {self.cfg.bring_up_attempts} attempts")
        self._ledger(name, state="terminated",
                     error=str(last_exc)[:400] if last_exc else "bring-up failed",
                     terminated_at=_now_iso())

    def _create_pod(self, name: str) -> dict:
        """Create one pod, retrying create-time RunPod 500s a few times (each
        retry lets RunPod pick a different machine)."""
        last: Exception | None = None
        for attempt in range(4):
            if self._stop.is_set():
                raise RunpodError("fleet stopping")
            try:
                return self.client.create_pod(
                    name=name, image=self.cfg.image, gpu_type_ids=self.cfg.gpu_type_ids,
                    public_key=self._pubkey, container_disk_gb=self.cfg.container_disk_gb,
                    cloud_type=self.cfg.cloud_type,
                )
            except RunpodError as exc:
                last = exc
                time.sleep(4 + 4 * attempt)
        raise last or RunpodError("create failed")

    def _cache_tarball(self) -> Path | None:
        """Pack the 9,070-file warm compile cache into ONE tarball, once, shared
        by all pods. Rsyncing thousands of tiny files over ssh is pathologically
        slow (a per-file round trip each) and does not scale to a 16-pod fan-out;
        a single ~180 MB file uploads in seconds."""
        cfg = self.cfg
        if not (cfg.inductor_cache and cfg.inductor_cache.is_dir()):
            return None
        tar = cfg.provision_dir / "inductor_cache.tar"
        with self._cache_lock:
            if not tar.is_file():
                self.log(f"[fleet] packing warm cache into {tar.name} (once)")
                subprocess.run(["tar", "-cf", str(tar), "-C", str(cfg.inductor_cache), "."],
                               check=True, capture_output=True)
        return tar

    def _provision_pod(self, endpoint, index: int) -> None:
        cfg = self.cfg
        pdir = cfg.provision_dir
        plog = self.settings.data_dir / "logs" / f"provision-{self.job['public_id']}-{index}.log"
        plog.parent.mkdir(parents=True, exist_ok=True)

        def step(label, fn):
            t = time.monotonic()
            fn()
            self.log(f"[fleet] pod {index} {label} ({time.monotonic() - t:.0f}s)")

        run_ssh(endpoint, ["mkdir", "-p", "/opt/SeedVR2", "/opt/inductor_cache",
                           "/workspace/provision", "/workspace/slices", "/workspace/units",
                           "/opt/models/seedvr2"], ssh_key=cfg.ssh_key)

        # The pod runs the stock pytorch base; ship the already-patched 6 MB tree
        # copied out of the verified local image (byte-identical to production).
        tree = pdir / "_tree" / "SeedVR2"
        step("uploaded SeedVR2 tree",
             lambda: rsync(endpoint, f"{tree}/", "/opt/SeedVR2/", upload=True,
                           ssh_key=cfg.ssh_key, timeout=600))
        for f in ("requirements-pod.txt", "provision_pod.sh"):
            rsync(endpoint, pdir / f, f"/workspace/provision/{f}", upload=True, ssh_key=cfg.ssh_key)

        tarball = self._cache_tarball()
        if tarball is not None:
            step("uploaded warm cache tarball",
                 lambda: rsync(endpoint, tarball, "/opt/inductor_cache.tar",
                               upload=True, ssh_key=cfg.ssh_key, timeout=600))
            step("unpacked warm cache",
                 lambda: run_ssh(endpoint, ["tar", "-xf", "/opt/inductor_cache.tar",
                                            "-C", "/opt/inductor_cache"], ssh_key=cfg.ssh_key))

        # Stream provisioning (apt/pip/7.3 GB weights) to a per-pod log rather
        # than buffering it, and enforce a hard timeout so a stuck pod fails
        # loudly instead of silently holding a slot.
        t = time.monotonic()
        with plog.open("w", encoding="utf-8") as f:
            proc = subprocess.Popen(
                ssh_command(endpoint, ["bash", "/workspace/provision/provision_pod.sh"], cfg.ssh_key),
                stdout=f, stderr=subprocess.STDOUT, text=True,
            )
            while proc.poll() is None:
                if time.monotonic() - t > 1800:
                    proc.kill()
                    raise RunpodError(f"provisioning timed out after 30 min (see {plog})")
                if self._stop.is_set():
                    proc.kill()
                    raise RunpodError("provisioning aborted (fleet stopping)")
                time.sleep(5)
        tail = plog.read_text(encoding="utf-8", errors="replace")[-600:]
        if proc.returncode != 0 or "POD READY" not in tail:
            raise RunpodError(f"provisioning failed (status {proc.returncode}); tail: {tail[-300:]}")
        self.log(f"[fleet] pod {index} provisioned ({time.monotonic() - t:.0f}s)")

    # -------------------------------------------------------------- teardown

    def _terminate(self, pod_id: str, name: str) -> None:
        try:
            self.client.terminate_pod(pod_id)
        except RunpodError as exc:
            self.log(f"[fleet] WARN terminate {pod_id} failed: {exc}")
        self._ledger(name, state="terminated", terminated_at=_now_iso())

    def terminate_all(self) -> None:
        self._stop.set()
        with self._lock:
            slots = list(self._slots.values())
        for slot in slots:
            self._terminate(slot.pod_id, slot.name)
        # Also sweep any ledger row for this job still creating/ready (a pod that
        # was mid-bring-up when we stopped) plus any live pod the API knows is
        # ours — belt and braces so nothing bills on.
        try:
            for pod in self.client.our_pods():
                if pod.get("name", "").startswith(f"{POD_NAME_PREFIX}{self.job['public_id']}-"):
                    self._terminate(pod["id"], pod["name"])
        except RunpodError:
            pass

    def __enter__(self) -> "CloudFleet":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.terminate_all()

    @property
    def ready(self) -> int:
        with self._lock:
            return self._ready

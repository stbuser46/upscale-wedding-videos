"""The cloud slot pool: provision N pods concurrently, hand them out as slots,
and guarantee they are all terminated when done.

Design: units are independent, so the backlog is embarrassingly parallel. The
fleet brings up pods concurrently and drops each into a slot queue the moment it
is provisioned — so unit dispatch starts on the first ready pod, not after all N.
Every pod is written to the `cloud_pods` ledger BEFORE the create call returns,
so a crash can never leave a billable pod the reaper can't find.
"""

from __future__ import annotations

import random
import shlex
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
# One stage-1 intermediate per job lives on a single "seed" pod; slices are cut
# there and passed pod-to-pod, so the multi-GB file crosses the home upstream
# at most once (see stage_intermediate / slice_from_seed).
REMOTE_INTERMEDIATE = "/workspace/intermediate.mkv"


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
    ledger_id: int | None = None  # this pod's OWN cloud_pods row (per-attempt)


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
    max_tree_upload_s: float = 60.0       # ingress gate: 6 MB tree slower than this = hopeless host
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
        # Warm-cache peer seeding: the tarball crosses the home upstream ONCE
        # (serialized by _cache_home_busy); every later pod pulls it from an
        # already-provisioned pod at datacenter speed. At 16 slots this turns
        # ~3 GB of home upload (competing with unit-slice uploads) into 186 MB.
        self._cache_cv = threading.Condition()
        self._cache_seeds: list[tuple[str, int]] = []   # endpoints holding the tarball
        self._cache_home_busy = False
        self._jobkey: tuple[Path, str] | None = None    # (private key path, pubkey text)
        # Stage-1 intermediate peer store: home ingress speed per pod (measured
        # during provisioning), the pod currently holding the intermediate, and
        # the lazily-started one-at-a-time staging upload.
        self._ingress_hint: dict[tuple[str, int], float] = {}
        # At most 2 concurrent tree uploads: the ingress gate must measure the
        # HOST's route, not self-inflicted contention from 8 parallel probes
        # over one home uplink (Codex round-2 finding #10).
        self._probe_gate = threading.Semaphore(2)
        self._inter_lock = threading.Lock()
        self._inter_seed: tuple[str, int] | None = None
        self._inter_thread: threading.Thread | None = None
        self._inter_attempts = 0
        self._ready = 0
        self._requested = 0
        self._capped = False                       # spend cap tripped -> fleet torn down
        self._no_more_work = threading.Event()     # dispatcher out of units
        self._monitor: threading.Thread | None = None

    # ---------------------------------------------------------------- ledger

    def _ledger(self, pod_name: str, **fields) -> None:
        """Name-based ledger update — ONLY for pods with no known row id (API
        strays swept by terminate_all). Per-attempt updates use _ledger_row."""
        cols = ", ".join(f"{k}=:{k}" for k in fields)
        with connect(self.settings.database_path) as db:
            db.execute(f"UPDATE cloud_pods SET {cols} WHERE name=:name AND terminated_at IS NULL",
                       {**fields, "name": pod_name})

    def _ledger_row(self, row_id: int, **fields) -> None:
        """Update ONE physical pod attempt's row by primary key. Every create
        attempt gets its own row (Codex finding #2: a shared name-keyed row let
        a later attempt overwrite an unconfirmed dud's identity, hiding its
        spend and colliding on the pod_id UNIQUE constraint)."""
        cols = ", ".join(f"{k}=:{k}" for k in fields)
        with connect(self.settings.database_path) as db:
            db.execute(f"UPDATE cloud_pods SET {cols} WHERE id=:row_id",
                       {**fields, "row_id": row_id})

    def _ledger_insert(self, pod_name: str, gpu_type: str, rate: float) -> int:
        with connect(self.settings.database_path) as db:
            cur = db.execute(
                """INSERT INTO cloud_pods (name, gpu_type, hourly_rate, cloud_type,
                                           state, job_id, created_at)
                   VALUES (:name, :gpu, :rate, :cloud, 'creating', :job_id, :now)""",
                {"name": pod_name, "gpu": gpu_type, "rate": rate,
                 "cloud": self.cfg.cloud_type, "job_id": self.job["id"], "now": _now_iso()},
            )
            return int(cur.lastrowid)

    def spend_so_far(self) -> float:
        with connect(self.settings.database_path) as db:
            rows = db.execute(
                "SELECT hourly_rate, created_at, terminated_at FROM cloud_pods WHERE job_id=?",
                (self.job["id"],),
            ).fetchall()
        now = datetime.now(timezone.utc)

        def _aware(ts: str) -> datetime:
            # Tolerate naive timestamps (e.g. a manual SQL fix using SQLite's
            # datetime('now')): treat them as UTC rather than crashing the
            # spend guard — this bit live on 2026-09-24.
            dt = datetime.fromisoformat(ts)
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

        total = 0.0
        for r in rows:
            if not r["created_at"] or r["hourly_rate"] is None:
                continue
            start = _aware(r["created_at"])
            end = _aware(r["terminated_at"]) if r["terminated_at"] else now
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
        # The spend cap is only meaningful if something enforces it WHILE pods
        # run, not just at bring-up: N pods that all launched under the cap then
        # bill for hours would blow past it unchecked. This watchdog is that
        # enforcement — it tears the whole fleet down the moment accrued spend
        # reaches the cap, and retires any pod that has gone too long without
        # finishing a unit (wedged/forgotten).
        if self._monitor is None:
            self._monitor = threading.Thread(target=self._watchdog, daemon=True,
                                              name="fleet-watchdog")
            self._monitor.start()

    @property
    def capped(self) -> bool:
        """True once the spend cap tripped and the fleet was torn down."""
        return self._capped

    def _watchdog(self, poll: float = 15.0) -> None:
        while not self._stop.wait(poll):
            # 1) Hard spend cap: accrued cost across every pod this job has ever
            #    created (running ones counted to 'now'). This is the real
            #    ceiling — bring-up alone can't bound it.
            try:
                spent = self.spend_so_far()
                self._spend_check_failures = 0
            except Exception as exc:
                # FAIL CLOSED: a broken spend query means the cap is
                # unenforceable — blind billing is the one unacceptable state
                # (Codex finding #8: the old 0.0 fallback disabled the guard).
                self._spend_check_failures = getattr(self, "_spend_check_failures", 0) + 1
                self.log(f"[fleet] WARN watchdog spend check failed "
                         f"({self._spend_check_failures}/3): {exc}")
                if self._spend_check_failures >= 3:
                    self._capped = True
                    self.log("[fleet] spend cap UNVERIFIABLE for 3 checks; "
                             "failing closed — tearing down the whole fleet NOW")
                    self.terminate_all()
                    return
                continue
            if spent >= self.cfg.spend_cap_usd:
                self._capped = True
                self.log(f"[fleet] SPEND CAP ${self.cfg.spend_cap_usd:.2f} reached "
                         f"(accrued ${spent:.2f}); tearing down the whole fleet NOW")
                self.terminate_all()
                return
            # 1b) Close 'create unconfirmed' rows once the provider verifiably
            #     does not know the pod: they accrue conservatively by design,
            #     but left open they pile up phantom spend and falsely trip
            #     the cap (observed: 6 rows ≈ $12.5/h of fiction). 3 minutes
            #     is far beyond RunPod's create-visibility window.
            try:
                with connect(self.settings.database_path) as db:
                    stale_rows = db.execute(
                        """SELECT id, name, created_at FROM cloud_pods
                           WHERE job_id=? AND terminated_at IS NULL AND pod_id IS NULL
                             AND state='terminating' AND error LIKE 'create unconfirmed%'""",
                        (self.job["id"],)).fetchall()
                if stale_rows:
                    known = {p.get("name") for p in self.client.our_pods()}
                    cutoff = datetime.now(timezone.utc)
                    for row in stale_rows:
                        age = (cutoff - datetime.fromisoformat(row["created_at"])).total_seconds()
                        if age > 180 and row["name"] not in known:
                            self._ledger_row(row["id"], state="terminated",
                                             terminated_at=_now_iso())
                            self.log(f"[fleet] closed unconfirmed-create row {row['id']} "
                                     f"(provider verified absent)")
            except Exception:
                pass
            # 2) TTL backstop: retire a pod that has run `pod_ttl_s` without
            #    finishing a unit. hand_back() refreshes the clock each completed
            #    unit, so a healthy pod chewing through units never trips this;
            #    only a wedged/forgotten pod does. (Per-unit timeout in the
            #    executor is the finer guard; this catches the coarse case.)
            now = time.monotonic()
            with self._lock:
                stale = [s for s in self._slots.values()
                         if now - s.created_at > self.cfg.pod_ttl_s]
            for slot in stale:
                self.log(f"[fleet] pod {slot.pod_id} exceeded TTL "
                         f"{self.cfg.pod_ttl_s:.0f}s without progress; retiring")
                self.retire_slot(slot)

    def provisioning_done(self) -> bool:
        """True once every bring-up thread has finished (readied or failed)."""
        return bool(self._threads) and all(not t.is_alive() for t in self._threads)

    def request_replacement(self) -> None:
        """A working pod died with units still queued: bring up a replacement
        so fleet capacity recovers instead of only ever shrinking (the
        2026-09-24 run decayed 6→2 pods and had to be manually restarted).
        Bounded by max_slots total replacements, the spend cap, and the usual
        bring-up guards."""
        with self._lock:
            if self._stop.is_set() or self._no_more_work.is_set():
                return
            if getattr(self, "_replacements", 0) >= self.cfg.max_slots:
                self.log("[fleet] replacement budget exhausted; not replacing pod")
                return
            self._replacements = getattr(self, "_replacements", 0) + 1
            index = self._requested + self._replacements + 100  # distinct pod name suffix
        self.log(f"[fleet] bringing up replacement pod {index}")
        t = threading.Thread(target=self._bring_up, args=(index,), daemon=True,
                             name=f"fleet-pod-{index}")
        t.start()
        self._threads.append(t)

    def relay_download(self, bad: tuple[str, int], *, remote_name: str,
                       local_out: Path, should_abort=lambda: False) -> bool:
        """Home cannot reach `bad` (rate-limited/blocked route) but a sibling
        pod usually can: pull the finished unit pod-to-pod, then download it
        from the sibling. Saves a fully-paid restore from being redone
        (2026-09-24: a home-IP rate limit burned three finished units)."""
        remote_path = f"/workspace/units/{remote_name}"
        relay_path = f"/workspace/relay_{remote_name}"
        vias = [e for e in self._live_endpoints() if e != bad]
        for via in vias[:2]:
            if should_abort():
                return False
            try:
                run_ssh(via, ["scp", "-P", str(bad[1]), "-i", "/opt/jobkey",
                              "-o", "StrictHostKeyChecking=no",
                              "-o", "UserKnownHostsFile=/dev/null",
                              f"root@{bad[0]}:{remote_path}", relay_path],
                        ssh_key=self.cfg.ssh_key, timeout=300)
                rsync(via, local_out, relay_path, upload=False,
                      ssh_key=self.cfg.ssh_key, timeout=1800, abort_check=should_abort)
                run_ssh(via, ["rm", "-f", relay_path], ssh_key=self.cfg.ssh_key,
                        timeout=30, check=False)
                if local_out.is_file() and local_out.stat().st_size > 0:
                    self.log(f"[fleet] relayed {remote_name} from {bad[0]} via {via[0]}")
                    return True
            except Exception as exc:
                self.log(f"[fleet] relay via {via[0]} failed: {str(exc)[:80]}")
        return False

    def acquire_slot(self, stop_check: Callable[[], bool] = lambda: False, poll: float = 10.0):
        """Block until a slot is free, then return it. Units legitimately queue
        for slots when there are more units than pods (258 units over 16 slots is
        the whole point), so this waits as long as the fleet could still hand one
        out — NOT a fixed timeout. Returns None if `stop_check` fires or the fleet
        can never produce a slot (all bring-ups finished and none succeeded)."""
        while True:
            # A tripped spend cap or any teardown stops handing out work at once,
            # so no new paid unit dispatches after the ceiling is hit.
            if stop_check() or self._stop.is_set():
                return None
            try:
                slot = self.slot_queue.get(timeout=poll)
                with self._lock:
                    if slot.pod_id not in self._slots:
                        continue  # corpse: retired (e.g. by TTL) while queued
                return slot
            except Empty:
                with self._lock:
                    no_live_slots = len(self._slots) == 0
                if self.provisioning_done() and no_live_slots:
                    return None  # every pod failed to come up; give up

    def mark_no_more_work(self) -> None:
        """The dispatcher has no units left to hand out: retire every pod
        sitting unclaimed in the slot queue, retire any that goes READY later,
        and abandon bring-ups still in flight. Without this, a slow pod that
        becomes READY after the queue drained bills unowned until job end —
        observed live 2026-09-23, where exactly that idle pod burned the
        spend-cap margin while another pod restored the last unit."""
        stranded: list[Slot] = []
        with self._lock:
            self._no_more_work.set()
            while True:
                try:
                    stranded.append(self.slot_queue.get_nowait())
                except Empty:
                    break
        for slot in stranded:
            self.log(f"[fleet] retiring unclaimed pod {slot.pod_id} (no work remains)")
            self.retire_slot(slot)

    def hand_back(self, slot: "Slot") -> None:
        """Return a healthy slot to the queue after it finished a unit, and
        refresh its TTL clock — completing a unit is proof the pod is alive, so
        the watchdog's 'no progress for pod_ttl_s' timer restarts here."""
        slot.created_at = time.monotonic()
        self.slot_queue.put(slot)

    def retire_slot(self, slot: "Slot") -> None:
        """Terminate a pod that has no more work, immediately, so it stops
        billing the moment its last unit is done rather than idling until the
        whole job ends. Fixes the tail where early-finishing pods sit idle."""
        with self._lock:
            self._slots.pop(slot.pod_id, None)
            self._ready = max(0, self._ready - 1)
        self._terminate(slot.pod_id, slot.name, ledger_id=slot.ledger_id)
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
        try:
            offers = {o.id: o for o in self.client.usable_gpu_offers(
                secure=self.cfg.cloud_type == "SECURE")}
            for gid in self.cfg.gpu_type_ids:
                if gid in offers:
                    gpu_type, rate = gid, offers[gid].price_per_hr or 0.0
                    break
        except Exception as exc:
            # A pricing-API blip must not silently cost the slot; create with
            # the preferred type and let the ledger pick up the real rate from
            # the create response (review finding A9).
            self.log(f"[fleet] WARN offers lookup failed for pod {index}: {str(exc)[:80]}")

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
            if self._no_more_work.is_set():
                self.log(f"[fleet] pod {index} bring-up abandoned (no work remains)")
                return
            if self.spend_so_far() >= self.cfg.spend_cap_usd:
                self.log(f"[fleet] spend cap reached; stop bringing up pod {index}")
                break
            pod_id = None
            # One ledger row per PHYSICAL create attempt, inserted before the
            # POST so a crash can never leave a billable pod unrecorded.
            row_id = self._ledger_insert(name, gpu_type, rate)
            try:
                pod = self._create_pod(name)
                pod_id = pod["id"]
                eff_rate = pod.get("costPerHr") or rate
                if not eff_rate or eff_rate <= 0:
                    # FAIL CLOSED on unpriceable pods: a 0.0 rate makes the
                    # spend watchdog blind to this pod forever (Codex round-2
                    # finding #7).
                    raise RunpodError(f"no positive price known for pod {pod_id}; refusing")
                self._ledger_row(row_id, pod_id=pod_id, state="creating", hourly_rate=eff_rate)
                # This pod is now attached to ITS row: close any earlier
                # unconfirmed pid-less row for the same name (a lost-response
                # create this attempt just adopted) so it can't double-accrue.
                try:
                    with connect(self.settings.database_path) as db:
                        db.execute(
                            """UPDATE cloud_pods SET state='terminated', terminated_at=?
                               WHERE name=? AND id != ? AND pod_id IS NULL
                                 AND state='terminating' AND error LIKE 'create unconfirmed%'
                                 AND terminated_at IS NULL""",
                            (_now_iso(), name, row_id))
                except Exception:
                    pass
                self.log(f"[fleet] pod {index} {pod_id} creating "
                         f"(attempt {attempt + 1}/{self.cfg.bring_up_attempts}, "
                         f"{gpu_type} ${pod.get('costPerHr') or rate}/h)")
                t_ssh = time.monotonic()
                endpoint, live = self.client.wait_ssh(
                    pod_id, ssh_key=self.cfg.ssh_key, timeout=self.cfg.ssh_timeout_s,
                    log=lambda m: None)
                self.log(f"[fleet] pod {index} ssh up at {endpoint[0]}:{endpoint[1]} ({time.monotonic() - t_ssh:.0f}s)")
                if self._stop.is_set():
                    self._terminate(pod_id, name, ledger_id=row_id)
                    return
                self._provision_pod(endpoint, index)
                slot = Slot(pod_id=pod_id, name=name, endpoint=endpoint, gpu_type=gpu_type,
                            hourly_rate=pod.get("costPerHr") or rate,
                            created_at=time.monotonic(), ledger_id=row_id)
                # Atomic with mark_no_more_work's drain: either this pod is
                # registered+queued before the drain (and gets drained), or the
                # flag is seen here and it never enters the queue — no window
                # where a READY pod is queued after the drain and bills unowned.
                with self._lock:
                    no_work = self._no_more_work.is_set()
                    if not no_work:
                        self._slots[pod_id] = slot
                        self._ready += 1
                        self.slot_queue.put(slot)
                if no_work:
                    self.log(f"[fleet] pod {index} READY but no work remains; retiring")
                    self._terminate(pod_id, name, ledger_id=row_id)
                    return
                self._ledger_row(row_id, state="ready", ssh_host=endpoint[0], ssh_port=endpoint[1],
                                 ready_at=_now_iso(), last_seen_at=_now_iso())
                self.log(f"[fleet] pod {index} {pod_id} READY at {endpoint[0]}:{endpoint[1]}")
                return  # success
            except Exception as exc:
                last_exc = exc
                self.log(f"[fleet] pod {index} attempt {attempt + 1} failed: {str(exc)[:120]}")
                if pod_id is None:
                    # Nothing confirmed created — but the POST may have landed
                    # with the response lost. Do NOT close the row (a closed
                    # row would hide a billing pod from spend accounting,
                    # Codex round-2 finding #4): mark it 'unconfirmed' so it
                    # keeps accruing conservatively; teardown closes leftover
                    # unconfirmed pid-less rows after the provider sweeps
                    # verify absence.
                    # 'terminating' (schema CHECK allows no custom states): keeps
                    # accruing conservatively; closed by adopt/teardown below.
                    self._ledger_row(row_id, state="terminating",
                                     error=f"create unconfirmed: {str(exc)[:300]}")
                else:
                    try:
                        self.client.terminate_pod(pod_id)  # never leave the dud billing
                        self._ledger_row(row_id, state="terminated",
                                         error=f"dud: {str(exc)[:300]}",
                                         terminated_at=_now_iso())
                    except RunpodError as kill_exc:
                        # UNCONFIRMED dud delete: leave this attempt's own row
                        # 'terminating' so its spend keeps accruing until the
                        # reaper confirms it dead; the slot is abandoned rather
                        # than reused (Codex finding #2).
                        self.log(f"[fleet] pod {index} dud delete UNCONFIRMED "
                                 f"({str(kill_exc)[:80]}); abandoning slot to the reaper")
                        self._ledger_row(row_id, state="terminating",
                                         error=f"dud delete unconfirmed: {str(kill_exc)[:200]}")
                        return
                # Back off between create attempts: immediate blind retries
                # against a contended allocator just collect more 500s.
                time.sleep(4 * (attempt + 1))
        self.log(f"[fleet] pod {index} gave up after {self.cfg.bring_up_attempts} attempts")

    def _create_pod(self, name: str) -> dict:
        """Create ONE pod, idempotently by name.

        Pod creation is a non-idempotent POST: if RunPod created the pod but the
        response was lost, a blind retry would rent a SECOND billable GPU. So the
        transport does not retry the POST (runpod_api marks it non-idempotent),
        and here we bracket the single create with a name lookup: adopt a pod
        that already carries this name (a prior attempt that actually landed)
        rather than create a twin. The outer `_bring_up` loop provides the only
        retry, so at most `bring_up_attempts` (3) creates ever fire per slot —
        not the old 3x4x4 = up-to-48."""
        if self._stop.is_set():
            raise RunpodError("fleet stopping")
        existing = self.client.pod_by_name(name)
        if existing is not None:
            self.log(f"[fleet] adopting existing pod {existing.get('id')} named {name}")
            return existing
        # Re-check right before the POST: the name lookup above can be slow,
        # and a create that fires after teardown started would land a pod
        # after the final sweep (Codex round-2 finding #2).
        if self._stop.is_set():
            raise RunpodError("fleet stopping")
        try:
            return self.client.create_pod(
                name=name, image=self.cfg.image, gpu_type_ids=self.cfg.gpu_type_ids,
                public_key=self._pubkey, container_disk_gb=self.cfg.container_disk_gb,
                cloud_type=self.cfg.cloud_type,
            )
        except RunpodError as exc:
            # The POST may have created the pod despite the error (lost response).
            # Reconcile by name before surfacing the failure, so we never leave a
            # billing twin behind — and adopt it if it landed.
            landed = None
            try:
                landed = self.client.pod_by_name(name)
            except RunpodError:
                pass
            if landed is not None:
                self.log(f"[fleet] create reported error but pod {landed.get('id')} landed; adopting")
                return landed
            raise exc

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

    def _ensure_jobkey(self) -> tuple[Path, str]:
        """One ephemeral ed25519 keypair per fleet run, used ONLY for pod-to-pod
        cache seeding. Job-scoped and short-lived: pods within one job already
        fully trust each other (same media, same code), and the key dies with
        the pods at teardown. It is NOT the operator key in cloud/keys/."""
        with self._cache_lock:
            if self._jobkey is None:
                # Per-JOB path: concurrent cloud workers must not overwrite
                # each other's peer keys (Codex round-2 finding #12).
                priv = self.cfg.provision_dir / f"_jobkey-{self.job['public_id']}"
                pub = Path(f"{priv}.pub")
                priv.unlink(missing_ok=True)
                pub.unlink(missing_ok=True)
                subprocess.run(
                    ["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(priv),
                     "-C", f"wedding-job-{self.job['public_id']}"],
                    check=True, capture_output=True)
                self._jobkey = (priv, pub.read_text().strip())
        return self._jobkey

    def _enable_peer_access(self, endpoint) -> None:
        """Let this pod copy files to/from its job peers (cache tarball, unit
        slices) with the ephemeral job key."""
        priv, pub = self._ensure_jobkey()
        rsync(endpoint, priv, "/opt/jobkey", upload=True, ssh_key=self.cfg.ssh_key, timeout=60)
        run_ssh(endpoint,
                ["bash", "-c",
                 f"chmod 600 /opt/jobkey && mkdir -p /root/.ssh && "
                 f"echo {shlex.quote(pub)} >> /root/.ssh/authorized_keys"],
                ssh_key=self.cfg.ssh_key, timeout=30)

    def _ensure_cache_on_pod(self, endpoint, index: int, step) -> None:
        """Get the warm-cache tarball onto a pod, preferring a peer that already
        has it (datacenter-to-datacenter) over the home upstream. The home link
        is used by at most one pod at a time and, in the common case, exactly
        once per fleet; any peer failure falls back to the home upload, so this
        can never do worse than the old per-pod upload."""
        tarball = self._cache_tarball()
        if tarball is None:
            return
        seed: tuple[str, int] | None = None
        hold_home = False
        with self._cache_cv:
            while seed is None and not hold_home:
                if self._stop.is_set():
                    raise RunpodError("fleet stopping")
                if self._cache_seeds:
                    seed = random.choice(self._cache_seeds)
                elif not self._cache_home_busy:
                    self._cache_home_busy = True
                    hold_home = True
                else:
                    # Keep waiting — no deadline stampede: if the current home
                    # uploader fails, its finally releases the token and wakes
                    # us; if it succeeds we peer-fetch. A concurrent free-for-
                    # all on a slow upstream is the one guaranteed-bad outcome
                    # (review finding A4). Each home attempt is bounded by the
                    # rsync timeout, so this cannot wait forever.
                    self._cache_cv.wait(timeout=5.0)
        fetched = False
        try:
            if seed is not None:
                try:
                    step(f"fetched warm cache from peer pod {seed[0]}",
                         lambda: run_ssh(endpoint, [
                             "scp", "-P", str(seed[1]), "-i", "/opt/jobkey",
                             "-o", "StrictHostKeyChecking=no",
                             "-o", "UserKnownHostsFile=/dev/null",
                             f"root@{seed[0]}:/opt/inductor_cache.tar",
                             "/opt/inductor_cache.tar",
                         ], ssh_key=self.cfg.ssh_key, timeout=300))
                    fetched = True
                except Exception as exc:
                    self.log(f"[fleet] pod {index} peer cache fetch failed "
                             f"({str(exc)[:80]}); falling back to home upload")
            if not fetched:
                t_home = time.monotonic()
                step("uploaded warm cache tarball",
                     lambda: rsync(endpoint, tarball, "/opt/inductor_cache.tar",
                                   upload=True, ssh_key=self.cfg.ssh_key, timeout=600))
                # Measured home->pod throughput doubles as the ingress hint for
                # choosing which pod should hold the stage-1 intermediate.
                self._ingress_hint[endpoint] = time.monotonic() - t_home
        finally:
            with self._cache_cv:
                if hold_home:
                    self._cache_home_busy = False
                self._cache_cv.notify_all()
        step("unpacked warm cache",
             lambda: run_ssh(endpoint, ["tar", "-xf", "/opt/inductor_cache.tar",
                                        "-C", "/opt/inductor_cache"], ssh_key=self.cfg.ssh_key))
        with self._cache_cv:
            self._cache_seeds.append(endpoint)
            self._cache_cv.notify_all()

    # ------------------------------------------- stage-1 intermediate peer store

    def _live_endpoints(self) -> list[tuple[str, int]]:
        with self._lock:
            return [s.endpoint for s in self._slots.values()]

    def start_intermediate_staging(self, local_path: Path) -> None:
        """Begin uploading the stage-1 intermediate ONCE to the live pod with the
        best measured home ingress, in the background. Idempotent; restaged
        automatically (bounded) if the seed pod later dies. Until a seed exists
        the dispatcher simply falls back to per-unit home uploads, so this can
        never make a run slower than the old path."""
        with self._inter_lock:
            if self._inter_seed is not None or self._inter_attempts >= 3:
                return
            if self._inter_thread is not None and self._inter_thread.is_alive():
                return
            self._inter_thread = threading.Thread(
                target=self._stage_intermediate, args=(Path(local_path),),
                daemon=True, name="fleet-intermediate")
            self._inter_thread.start()

    def _stage_intermediate(self, local_path: Path) -> None:
        live = self._live_endpoints()
        if not live or self._stop.is_set():
            # No pod is up yet (e.g. staging requested at dispatch start while
            # the fleet is still provisioning): not an attempt — the next
            # intermediate_seed() call relaunches this thread.
            return
        with self._inter_lock:
            self._inter_attempts += 1
        # Prefer the pod that took the cache tarball fastest from home.
        live.sort(key=lambda e: (self._ingress_hint.get(e) is None,
                                 self._ingress_hint.get(e, 0.0)))
        target = live[0]
        size_gb = local_path.stat().st_size / 1e9 if local_path.is_file() else 0.0
        self.log(f"[fleet] staging stage-1 intermediate ({size_gb:.1f} GB) on {target[0]} "
                 f"(one-time upload; slices will be cut there and passed pod-to-pod)")
        t0 = time.monotonic()
        try:
            # Rate-capped so this multi-GB background upload never starves the
            # first-wave slice uploads that pods are actively waiting on (the
            # observed ~15-min GPU-idle ramp of 2026-09-24).
            rsync(target, local_path, REMOTE_INTERMEDIATE, upload=True,
                  ssh_key=self.cfg.ssh_key, timeout=7200, bwlimit_kbps=3000)
        except Exception as exc:
            self.log(f"[fleet] WARN intermediate staging failed ({str(exc)[:100]}); "
                     f"units continue via home uploads")
            return
        with self._inter_lock:
            self._inter_seed = target
        self.log(f"[fleet] intermediate staged on {target[0]} ({time.monotonic() - t0:.0f}s)")

    def intermediate_seed(self, local_path: Path) -> tuple[str, int] | None:
        """The endpoint currently holding the intermediate, or None. Lazily
        (re)starts staging when there is no seed — covering both a seed pod
        that died and a staging request that fired before any pod was live."""
        with self._inter_lock:
            seed = self._inter_seed
        if seed is None:
            self.start_intermediate_staging(local_path)
            return None
        if seed not in self._live_endpoints():
            with self._inter_lock:
                if self._inter_seed == seed:
                    self._inter_seed = None
            self.log(f"[fleet] intermediate seed {seed[0]} is gone; will restage")
            self.start_intermediate_staging(local_path)
            return None
        return seed

    def slice_from_seed(self, target: tuple[str, int], *, ss: str, cap: int,
                        name: str, expected_hash: str) -> bool:
        """Cut one unit's slice on the seed pod (stream copy — the exact command
        the local slicer uses) and deliver it to `target` datacenter-side, then
        PROVE it is decoded-identical to the locally-cut slice by comparing a
        sha256 over its per-frame framemd5 hashes against `expected_hash`. Any
        failure (including a hash mismatch from e.g. a seek-behaviour difference
        in the pod's ffmpeg) returns False and the caller falls back to the home
        upload — the peer path can silently ship only PROVEN-identical bytes."""
        with self._inter_lock:
            seed = self._inter_seed
        if seed is None:
            return False
        remote_slice = f"/workspace/slices/{name}"
        try:
            run_ssh(seed, ["bash", "-c",
                           f"ffmpeg -v error -y -ss {shlex.quote(ss)} -i {REMOTE_INTERMEDIATE} "
                           f"-map 0:v:0 -frames:v {cap} -c copy {remote_slice}"],
                    ssh_key=self.cfg.ssh_key, timeout=300)
            if target != seed:
                run_ssh(target, ["scp", "-P", str(seed[1]), "-i", "/opt/jobkey",
                                 "-o", "StrictHostKeyChecking=no",
                                 "-o", "UserKnownHostsFile=/dev/null",
                                 f"root@{seed[0]}:{remote_slice}", remote_slice],
                        ssh_key=self.cfg.ssh_key, timeout=300)
                run_ssh(seed, ["rm", "-f", remote_slice], ssh_key=self.cfg.ssh_key, timeout=60)
            probe = run_ssh(target, ["bash", "-c",
                                     f"set -o pipefail; "
                                     f"ffmpeg -v error -i {remote_slice} -f framemd5 - "
                                     f"| grep -v '^#' | awk '{{print $NF}}' | sha256sum"],
                            ssh_key=self.cfg.ssh_key, timeout=300)
            got = probe.stdout.split()[0] if probe.stdout else ""
            if got != expected_hash:
                self.log(f"[fleet] peer slice {name} hash mismatch on {target[0]} "
                         f"(pod ffmpeg differs?); falling back to home upload")
                run_ssh(target, ["rm", "-f", remote_slice], ssh_key=self.cfg.ssh_key,
                        timeout=60, check=False)
                return False
            return True
        except Exception as exc:
            self.log(f"[fleet] peer slice {name} failed ({str(exc)[:80]}); "
                     f"falling back to home upload")
            # Never leave an UNPROVEN candidate at the target path: the
            # fallback rsync's size/mtime quick-check could skip replacing it
            # (Codex C finding). Best effort — the pod may be unreachable.
            try:
                run_ssh(target, ["rm", "-f", remote_slice], ssh_key=self.cfg.ssh_key,
                        timeout=30, check=False)
            except Exception:
                pass
            return False

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
        # This transfer doubles as the INGRESS GATE: a host that cannot take
        # 6 MB inside max_tree_upload_s (~1 Mbit/s) will never sustain slice
        # traffic — refuse it NOW, before 30 minutes of provisioning, instead
        # of adopting it (the 81.27.69.177 host was adopted four times on
        # 2026-09-24 and killed every pod placed on it). The measured time also
        # seeds the ingress hint for EVERY pod, fixing seed selection, which
        # previously only measured the single home cache uploader.
        tree = pdir / "_tree" / "SeedVR2"
        with self._probe_gate:
            t_tree = time.monotonic()
            step("uploaded SeedVR2 tree",
                 lambda: rsync(endpoint, f"{tree}/", "/opt/SeedVR2/", upload=True,
                               ssh_key=cfg.ssh_key, timeout=max(120, int(cfg.max_tree_upload_s) * 2)))
            tree_s = time.monotonic() - t_tree
        self._ingress_hint[endpoint] = tree_s
        if tree_s > cfg.max_tree_upload_s:
            raise RunpodError(
                f"ingress gate: 6 MB tree took {tree_s:.0f}s "
                f"(> {cfg.max_tree_upload_s:.0f}s) — host route too slow, refusing pod")
        for f in ("requirements-pod.txt", "provision_pod.sh"):
            rsync(endpoint, pdir / f, f"/workspace/provision/{f}", upload=True, ssh_key=cfg.ssh_key)

        self._enable_peer_access(endpoint)
        self._ensure_cache_on_pod(endpoint, index, step)

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
                if self._stop.is_set() or self._no_more_work.is_set():
                    proc.kill()
                    raise RunpodError("provisioning aborted (fleet stopping or no work remains)")
                time.sleep(5)
        tail = plog.read_text(encoding="utf-8", errors="replace")[-600:]
        if proc.returncode != 0 or "POD READY" not in tail:
            raise RunpodError(f"provisioning failed (status {proc.returncode}); tail: {tail[-300:]}")
        self.log(f"[fleet] pod {index} provisioned ({time.monotonic() - t:.0f}s)")

    # -------------------------------------------------------------- teardown

    def _terminate(self, pod_id: str, name: str, ledger_id: int | None = None) -> None:
        """Delete a pod and record its fate HONESTLY.

        Only mark the ledger row 'terminated' (with terminated_at set, which is
        what stops spend_so_far counting it and drops it from the live UI) when
        the delete is CONFIRMED — terminate_pod raises otherwise. On an
        unconfirmed delete we leave the row 'terminating' with terminated_at
        NULL, so: (a) spend_so_far keeps counting it — a conservative over-count
        that can only trip the cap EARLIER, never hide cost; (b) it still shows
        as live; (c) the reaper (independent systemd timer) and the next cloud
        worker's reconcile will confirm and finalise it. The old code marked
        every pod terminated even when DELETE threw, which could hide a pod that
        was still billing."""
        # The provider kill comes FIRST and the ledger writes are best-effort:
        # a locked/unavailable SQLite must never stand between a billing pod
        # and its termination (Codex finding #1 — the old order could leave a
        # whole fleet billing during a DB outage with the cap already tripped).
        def _ledger_soft(**fields):
            try:
                if ledger_id is not None:
                    self._ledger_row(ledger_id, **fields)
                else:
                    # Resolve by pod_id (UNIQUE) — a name-based update could
                    # terminally stamp several distinct attempts' rows at once
                    # (Codex round-2 finding #6). Fall back to name only when
                    # the pod has no row at all.
                    with connect(self.settings.database_path) as db:
                        cols = ", ".join(f"{k}=:{k}" for k in fields)
                        cur = db.execute(
                            f"UPDATE cloud_pods SET {cols} WHERE pod_id=:pid AND terminated_at IS NULL",
                            {**fields, "pid": pod_id})
                        if cur.rowcount == 0:
                            self._ledger(name, **fields)
            except Exception as exc:
                self.log(f"[fleet] WARN ledger update failed for {name}: {str(exc)[:80]}")

        _ledger_soft(state="terminating")
        try:
            self.client.terminate_pod(pod_id)  # True or raises
        except RunpodError as exc:
            self.log(f"[fleet] WARN terminate {pod_id} UNCONFIRMED ({exc}); "
                     f"left 'terminating' for the reaper — may still be billing")
            _ledger_soft(error=f"terminate unconfirmed: {str(exc)[:300]}")
            return
        _ledger_soft(state="terminated", terminated_at=_now_iso())

    def terminate_all(self) -> None:
        self._stop.set()
        with self._lock:
            slots = list(self._slots.values())
        # PHASE 1 — provider kills only, no DB in the loop: a locked SQLite
        # must never delay the NEXT pod's kill (Codex round-2 finding #5).
        results: list[tuple["Slot", bool, str]] = []
        for slot in slots:
            try:
                self.client.terminate_pod(slot.pod_id)
                results.append((slot, True, ""))
            except Exception as exc:
                results.append((slot, False, str(exc)[:300]))
                self.log(f"[fleet] WARN terminate {slot.pod_id} UNCONFIRMED: {str(exc)[:80]}")
        # PHASE 2 — best-effort bookkeeping.
        for slot, ok, err in results:
            try:
                if ok:
                    self._ledger_row(slot.ledger_id, state="terminated", terminated_at=_now_iso()) \
                        if slot.ledger_id else self._ledger(slot.name, state="terminated",
                                                            terminated_at=_now_iso())
                else:
                    if slot.ledger_id:
                        self._ledger_row(slot.ledger_id, state="terminating",
                                         error=f"terminate unconfirmed: {err}")
            except Exception as exc:
                self.log(f"[fleet] WARN ledger update failed for {slot.name}: {str(exc)[:80]}")
        # Sweep any live pod the API knows is ours (mid-bring-up strays), then
        # sweep AGAIN after a grace period: a create POST that was in flight
        # when we stopped can land after the first sweep (Codex finding #3).
        for wait_s in (0.0, 10.0):
            if wait_s:
                time.sleep(wait_s)
            try:
                for pod in self.client.our_pods():
                    if pod.get("name", "").startswith(f"{POD_NAME_PREFIX}{self.job['public_id']}-"):
                        self._terminate(pod["id"], pod["name"])
            except Exception:
                pass
        # Provider sweeps verified absence: close leftover pid-less
        # 'unconfirmed' rows for this job so phantom creates stop accruing.
        try:
            with connect(self.settings.database_path) as db:
                db.execute(
                    """UPDATE cloud_pods SET state='terminated', terminated_at=?
                       WHERE job_id=? AND pod_id IS NULL AND state='terminating'
                         AND error LIKE 'create unconfirmed%' AND terminated_at IS NULL""",
                    (_now_iso(), self.job["id"]))
        except Exception:
            pass
        # Per-job peer keypair is dead with its pods.
        try:
            if self._jobkey is not None:
                priv, _ = self._jobkey
                priv.unlink(missing_ok=True)
                Path(f"{priv}.pub").unlink(missing_ok=True)
        except Exception:
            pass

    def __enter__(self) -> "CloudFleet":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.terminate_all()

    @property
    def ready(self) -> int:
        with self._lock:
            return self._ready

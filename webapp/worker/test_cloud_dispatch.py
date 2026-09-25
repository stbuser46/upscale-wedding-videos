"""Local mock test for the pipelined cloud dispatch in _run_units_cloud.

No network, no pods, no money: the fleet and the executor phases are faked, the
DB helpers are patched out, and the SeedVR2 'restore' is a sleep. What IS real
is the dispatch logic under test: the per-pod pipeline (prefetch upload during
restore, download during the next restore), in-run retry with the attempt cap,
requeue-on-pod-death, pause semantics, and provision-before-prepare ordering.

Run:  webapp/.venv/bin/python -m unittest webapp.worker.test_cloud_dispatch -v
"""

from __future__ import annotations

import json
import os
import queue as _queue
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import webapp.worker.runner as runner
import webapp.cloud.fleet as fleet_mod
import webapp.cloud.executor as exec_mod
import webapp.worker.slicer as slicer_mod
from webapp.cloud.executor import UnitResult

RESTORE_S = 0.15     # fake GPU restore duration
TRANSFER_S = 0.05    # fake rsync duration


class FakeSlot:
    def __init__(self, i: int):
        self.pod_id = f"pod{i}"
        self.name = f"wedding-test-{i}"
        self.endpoint = (f"10.0.0.{i}", 22)
        self.gpu_type = "FAKE GPU"
        self.hourly_rate = 1.0
        self.created_at = time.monotonic()


class FakeFleet:
    """Stands in for CloudFleet: slots appear instantly, teardown is recorded."""

    last: "FakeFleet | None" = None

    def __init__(self, settings, job, cfg, log=print):
        self.cfg = cfg
        self.capped = False
        self.produce = getattr(cfg, "produce_slots", None)
        # None = peer slice path off; True = peer copies succeed; False = fail.
        self.peer_slices = getattr(cfg, "peer_slices", None)
        self.slot_queue: "_queue.Queue[FakeSlot]" = _queue.Queue()
        self.retired: list[str] = []
        self.torn_down = False
        self.ready = 0
        self.events: list[tuple[float, str]] = []
        self.peer_slice_calls: list[tuple] = []
        self.staging_started = threading.Event()
        self._provisioned = False
        self._no_more = False
        FakeFleet.last = self

    def _ev(self, name: str) -> None:
        self.events.append((time.monotonic(), name))

    def provision(self, n_slots: int) -> None:
        self._ev("provision")
        count = n_slots if self.produce is None else self.produce
        defer_s = getattr(self.cfg, "defer_last_slot_s", None)
        for i in range(count):
            if defer_s and i == count - 1:
                threading.Timer(defer_s, self._late_put, args=(FakeSlot(i),)).start()
            else:
                self.slot_queue.put(FakeSlot(i))
                self.ready += 1
        self._provisioned = True

    def _late_put(self, slot: FakeSlot) -> None:
        if self._no_more:  # fleet-side READY-but-no-work retire
            self.retired.append(slot.pod_id)
            return
        self.slot_queue.put(slot)
        self.ready += 1

    def mark_no_more_work(self) -> None:
        self._no_more = True
        while True:
            try:
                self.retired.append(self.slot_queue.get_nowait().pod_id)
            except _queue.Empty:
                return

    def acquire_slot(self, stop_check=lambda: False, poll: float = 10.0):
        while True:
            if stop_check():
                return None
            try:
                return self.slot_queue.get(timeout=0.02)
            except _queue.Empty:
                if self._provisioned and self.ready == 0:
                    return None  # every bring-up failed

    def provisioning_done(self) -> bool:
        return self._provisioned

    def request_replacement(self) -> None:
        self.replacements_requested = getattr(self, "replacements_requested", 0) + 1

    def relay_download(self, bad, *, remote_name, local_out, should_abort=lambda: False) -> bool:
        self.relay_calls = getattr(self, "relay_calls", [])
        self.relay_calls.append((bad, remote_name))
        if getattr(self.cfg, "relay_works", False):
            Path(local_out).write_bytes(b"relayed-unit")
            return True
        return False

    def start_intermediate_staging(self, local_path) -> None:
        self.staging_started.set()

    def intermediate_seed(self, local_path):
        if self.peer_slices is None or not self.staging_started.is_set():
            return None
        return ("10.9.9.9", 22999)

    def slice_from_seed(self, target, *, ss, cap, name, expected_hash) -> bool:
        self.peer_slice_calls.append((target, name, ss, cap, expected_hash))
        return bool(self.peer_slices)

    def hand_back(self, slot: FakeSlot) -> None:  # not used by pipelined dispatch
        self.slot_queue.put(slot)

    def retire_slot(self, slot: FakeSlot) -> None:
        self.retired.append(slot.pod_id)

    def terminate_all(self) -> None:
        self.torn_down = True

    def __enter__(self) -> "FakeFleet":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.terminate_all()


class FakePodEngine:
    """Stands in for executor.PodEngine: records lifecycle, no subprocess."""

    instances: list["FakePodEngine"] = []
    barrier_verdict = True  # scripted shutdown_barrier() outcome

    def __init__(self, command, *, name: str = "", ready_timeout_s: float = 120.0,
                 remote_killer=None):
        self.command = list(command)
        self.name = name
        self.ready_timeout_s = ready_timeout_s
        self.remote_killer = remote_killer
        self.started = 0
        self.closed = 0
        self.barrier_calls = 0
        self._dead = False
        FakePodEngine.instances.append(self)

    @property
    def alive(self) -> bool:
        return not self._dead

    def start(self) -> bool:
        self.started += 1
        return not self._dead

    def close(self) -> None:
        self.closed += 1
        self._dead = True

    def shutdown_barrier(self) -> bool:
        self.barrier_calls += 1
        self._dead = True
        return self.barrier_verdict


class FakeExec:
    """Fake executor phases with an event log and scriptable failures."""

    def __init__(self):
        self.lock = threading.Lock()
        self.events: list[tuple[float, str, str]] = []  # (t, event, pod)
        self.fail_upload: dict[int, int] = {}
        self.fail_restore: dict[int, int] = {}
        self.fail_download: dict[int, int] = {}
        self.fail_engine: dict[int, int] = {}
        self.raise_engine: dict[int, int] = {}       # engine path raises (finding #2)
        self.unverified_engine: dict[int, int] = {}  # kill barrier failed (finding #1)
        self.classic_budgets: list[float] = []       # max_run_s seen by restore_unit
        self.restores_done = 0
        self.after_restore = lambda count: None  # hook (e.g. request a pause)

    def _ev(self, name: str, pod: str = "") -> None:
        with self.lock:
            self.events.append((time.monotonic(), name, pod))

    def _consume(self, table: dict[int, int], seq: int) -> bool:
        with self.lock:
            if table.get(seq, 0) > 0:
                table[seq] -= 1
                return True
        return False

    def upload_unit_slice(self, endpoint, ssh_key, *, slice_path, unit, log_path,
                          timeout=1800, should_abort=lambda: False, **kw):
        seq = unit["seq"]
        self._ev(f"up:{seq}:start", endpoint[0])
        time.sleep(TRANSFER_S)
        self._ev(f"up:{seq}:end", endpoint[0])
        if self._consume(self.fail_upload, seq):
            return UnitResult(status=1, message=f"fake upload fail {seq}")
        return UnitResult(status=0, upload_s=TRANSFER_S)

    def restore_unit(self, endpoint, ssh_key, *, slice_name, out_name, unit, model,
                     resolution, batch, overlap, log_path,
                     should_abort=lambda: False, poll=2.0, max_run_s=3600.0):
        seq = unit["seq"]
        with self.lock:
            self.classic_budgets.append(max_run_s)
        self._ev(f"restore:{seq}:start", endpoint[0])
        t0 = time.monotonic()
        while time.monotonic() - t0 < RESTORE_S:
            if should_abort():
                self._ev(f"restore:{seq}:aborted", endpoint[0])
                return UnitResult(status=-1, message="aborted")
            time.sleep(0.005)
        self._ev(f"restore:{seq}:end", endpoint[0])
        with self.lock:
            self.restores_done += 1
            count = self.restores_done
        self.after_restore(count)
        if self._consume(self.fail_restore, seq):
            return UnitResult(status=1, message=f"fake restore fail {seq}")
        return UnitResult(status=0, restore_s=RESTORE_S)

    def restore_unit_via_engine(self, engine, *, slice_name, out_name, unit, model,
                                resolution, batch, overlap, log_path,
                                should_abort=lambda: False, poll=2.0,
                                max_run_s=3600.0, stall_s=900.0):
        seq = unit["seq"]
        pod = engine.name.split(":")[0]  # runner names engines host:port
        if self._consume(self.raise_engine, seq):
            self._ev(f"engine:{seq}:raise", pod)
            raise RuntimeError(f"fake engine explosion {seq}")
        self._ev(f"engine:{seq}:start", pod)
        t0 = time.monotonic()
        while time.monotonic() - t0 < RESTORE_S:
            if should_abort():
                self._ev(f"engine:{seq}:aborted", pod)
                return UnitResult(status=-1, message="aborted")
            time.sleep(0.005)
        if self._consume(self.unverified_engine, seq):
            self._ev(f"engine:{seq}:unverified", pod)
            return UnitResult(status=3,
                              message=f"fake stall {seq}; remote engine death UNVERIFIED")
        if self._consume(self.fail_engine, seq):
            self._ev(f"engine:{seq}:fail", pod)
            return UnitResult(status=1, message=f"fake engine death {seq}")
        self._ev(f"engine:{seq}:end", pod)
        with self.lock:
            self.restores_done += 1
            count = self.restores_done
        self.after_restore(count)
        return UnitResult(status=0, restore_s=RESTORE_S, frames=unit["new"])

    def download_unit(self, endpoint, ssh_key, *, local_out, unit, log_path,
                      should_abort=lambda: False, timeout=1800):
        seq = unit["seq"]
        self._ev(f"down:{seq}:start", endpoint[0])
        time.sleep(TRANSFER_S)
        if self._consume(self.fail_download, seq):
            self._ev(f"down:{seq}:fail", endpoint[0])
            return UnitResult(status=1, message=f"fake download fail {seq}")
        Path(local_out).write_bytes(b"unit")
        self._ev(f"down:{seq}:end", endpoint[0])
        return UnitResult(status=0, download_s=TRANSFER_S)


class CloudDispatchTest(unittest.TestCase):
    FRAMES_TOTAL = 7496  # 10 units of 750 with a 746-frame tail

    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="cloud-dispatch-test-")
        tmp = Path(self._tmp.name)
        self.data_dir = tmp / "data"
        (self.data_dir / "logs").mkdir(parents=True)
        (self.data_dir / "outputs").mkdir()
        source = self.data_dir / "title_sources" / "src.vob"
        source.parent.mkdir()
        source.write_bytes(b"vob")
        self.settings = SimpleNamespace(
            data_dir=self.data_dir,
            database_path=tmp / "unused.sqlite",
            ffmpeg_image="fake-ffmpeg",
            free_space_reserve_bytes=0,
            pipeline_path=tmp / "pipeline_v3.sh",
            source_dir=tmp / "source",
            project_root=tmp,
        )
        snapshot = {"chunk": 750, "overlap": 4, "model": "m.safetensors",
                    "resolution": 1440, "batch": 129, "output_fps": 50.0}
        self.job = {
            "id": 1, "public_id": "restore-test", "display_name": "test chapter",
            "frames_total": self.FRAMES_TOTAL,
            "source_start_ms": 0, "source_end_ms": int(self.FRAMES_TOTAL / 50 * 1000),
            "settings_json": json.dumps(snapshot),
            "output_path": str(self.data_dir / "outputs" / "out.mkv"),
            "log_path": str(self.data_dir / "logs" / "job.log"),
            "source_cache_path": str(source),
        }
        self.units = runner._plan_units(self.FRAMES_TOTAL, 750, 4)
        self.frames_by_seq = {u["seq"]: u["new"] for u in self.units}

        self.fake_exec = FakeExec()
        self.job_state = "running"
        self.chunk_records: list[tuple[int, str]] = []
        self.prepare_times: list[float] = []
        self.assembled = threading.Event()

        self._patches: list[tuple[object, str, object]] = []

        def patch(obj, name, value):
            self._patches.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def fake_probe(settings, path):
            return self.frames_by_seq[int(Path(path).stem.split("_")[1])]

        def fake_slice(stage1, skip, cap, dest, *, fps, ffmpeg_image, data_dir, allow_short=False):
            time.sleep(0.01)
            Path(dest).write_bytes(b"slice")
            return Path(dest)

        def fake_prepare(settings, job, source, start_sec, duration, log_path):
            self.prepare_times.append(time.monotonic())
            work = self.data_dir / "restoration_work" / job["public_id"]
            work.mkdir(parents=True, exist_ok=True)
            (work / "input_50p_ffv1.mkv").write_bytes(b"ffv1")
            return 0

        def fake_record_chunk(settings, job_id, unit, unit_path, state,
                              frame_count=None, checksum=None):
            with threading.Lock():
                self.chunk_records.append((unit["seq"], state))

        patch(fleet_mod, "CloudFleet", FakeFleet)
        patch(slicer_mod, "slice_frame_hash",
              lambda path, **k: f"hash-{Path(path).stem}")
        patch(exec_mod, "upload_unit_slice", self.fake_exec.upload_unit_slice)
        patch(exec_mod, "restore_unit", self.fake_exec.restore_unit)
        patch(exec_mod, "download_unit", self.fake_exec.download_unit)
        patch(exec_mod, "restore_unit_via_engine", self.fake_exec.restore_unit_via_engine)
        patch(exec_mod, "PodEngine", FakePodEngine)
        FakePodEngine.instances = []
        FakePodEngine.barrier_verdict = True
        patch(slicer_mod, "slice_unit", fake_slice)
        patch(runner, "_chunk_valid", lambda *a, **k: False)
        patch(runner, "_units_frames_done", lambda *a, **k: 0)
        patch(runner, "_unit_progress", lambda *a, **k: None)
        patch(runner, "_set_state", lambda *a, **k: None)
        patch(runner, "_set_frames_total", lambda *a, **k: None)
        patch(runner, "_heartbeat", lambda *a, **k: None)
        patch(runner, "_record_chunk", fake_record_chunk)
        patch(runner, "_probe_frame_count", fake_probe)
        patch(runner, "_run_prepare", fake_prepare)
        patch(runner, "_allowed_source", lambda settings, value: Path(value))
        patch(runner, "_job_state", lambda settings, job_id: self.job_state)
        patch(runner, "_assemble",
              lambda *a, **k: (self.assembled.set(), 0)[1])
        patch(runner, "_cloud_fleet_config", lambda settings: SimpleNamespace(
            max_slots=3, spend_cap_usd=100.0, ssh_key=Path("/tmp/fake-key")))

    def tearDown(self):
        for obj, name, original in reversed(self._patches):
            setattr(obj, name, original)
        self._tmp.cleanup()

    # ------------------------------------------------------------------ helpers

    def run_dispatch(self) -> int:
        return runner._run_units_cloud(self.settings, self.job)

    def events(self, prefix: str) -> list[tuple[float, str, str]]:
        return [e for e in self.fake_exec.events if e[1].startswith(prefix)]

    # -------------------------------------------------------------------- tests

    def test_happy_path_pipelines_and_completes_every_unit(self):
        rc = self.run_dispatch()
        self.assertEqual(rc, 0)
        self.assertTrue(self.assembled.is_set())
        valid = sorted(seq for seq, state in self.chunk_records if state == "valid")
        self.assertEqual(valid, [u["seq"] for u in self.units])
        # Restores never double-run a unit on the happy path.
        starts = [e for e in self.events("restore:") if e[1].endswith(":start")]
        self.assertEqual(len(starts), len(self.units))
        # Provision was requested BEFORE stage-1 prepare ran (overlap).
        fleet = FakeFleet.last
        provision_t = [t for t, name in fleet.events if name == "provision"][0]
        self.assertTrue(self.prepare_times and provision_t <= self.prepare_times[0])
        # Pipelining: some unit's upload started while another unit was
        # restoring on the SAME pod.
        overlapped = False
        for rt, rname, rpod in self.events("restore:"):
            if not rname.endswith(":start"):
                continue
            rseq = int(rname.split(":")[1])
            rend = next(t for t, n, p in self.fake_exec.events
                        if n == f"restore:{rseq}:end" and p == rpod)
            for ut, uname, upod in self.events("up:"):
                if uname.endswith(":start") and upod == rpod and rt < ut < rend:
                    overlapped = True
        self.assertTrue(overlapped, "no upload overlapped a restore on the same pod")
        # Every slice was deleted after its unit validated, pods were retired.
        slices_dir = self.data_dir / "restoration_work" / "restore-test" / "slices"
        self.assertEqual(list(slices_dir.glob("*.mkv")), [])
        self.assertEqual(len(fleet.retired), 3)

    def test_restore_failure_retries_on_another_pod(self):
        self.fake_exec.fail_restore = {2: 1}
        rc = self.run_dispatch()
        self.assertEqual(rc, 0)
        starts = [e for e in self.events("restore:2:") if e[1].endswith(":start")]
        self.assertEqual(len(starts), 2, "unit 2 should run twice (fail, then retry)")
        self.assertIn((2, "invalid"), self.chunk_records)
        self.assertIn((2, "valid"), self.chunk_records)

    def test_download_failure_requeues_without_burning_an_attempt(self):
        self.fake_exec.fail_download = {1: 1}
        rc = self.run_dispatch()
        self.assertEqual(rc, 0)
        # Transfer failures are a strike against the POD, not the unit: no
        # 'invalid' attempt is recorded and the unit re-restores elsewhere.
        self.assertNotIn((1, "invalid"), self.chunk_records)
        self.assertIn((1, "valid"), self.chunk_records)
        starts = [e for e in self.events("restore:1:") if e[1].endswith(":start")]
        self.assertEqual(len(starts), 2, "unit 1 must be re-restored after the lost download")

    def test_relay_download_saves_a_finished_restore(self):
        # A blocked home->pod route must not force a paid re-restore: the unit
        # is pulled via a sibling pod instead (2026-09-24 incident class).
        self.fake_exec.fail_download = {1: 9}   # direct download never works
        base = self._with_cfg_extra(relay_works=True)
        try:
            rc = self.run_dispatch()
        finally:
            runner._cloud_fleet_config = base
        self.assertEqual(rc, 0)
        self.assertIn((1, "valid"), self.chunk_records)
        starts = [e for e in self.events("restore:1:") if e[1].endswith(":start")]
        self.assertEqual(len(starts), 1, "the relay must save the ORIGINAL restore")
        self.assertTrue(FakeFleet.last.relay_calls)

    def test_attempt_cap_makes_a_poisoned_unit_terminal(self):
        self.fake_exec.fail_restore = {3: 99}
        with self.assertRaises(runner.WorkerError) as ctx:
            self.run_dispatch()
        self.assertIn("unit 3", str(ctx.exception))
        starts = [e for e in self.events("restore:3:") if e[1].endswith(":start")]
        self.assertEqual(len(starts), 3, "attempt cap is 3 total attempts")
        self.assertFalse(self.assembled.is_set())

    def test_pause_stops_new_restores_but_finishes_in_flight(self):
        pause_at = {"t": None}

        def request_pause(count):
            if count == 3 and pause_at["t"] is None:
                pause_at["t"] = time.monotonic()
                self.job_state = "pause_requested"

        self.fake_exec.after_restore = request_pause
        rc = self.run_dispatch()
        self.assertEqual(rc, runner.STATUS_PAUSED)
        self.assertFalse(self.assembled.is_set())
        # In-flight restores at pause time were allowed to finish (committed),
        # but nothing NEW started afterwards.
        late_starts = [e for e in self.events("restore:")
                       if e[1].endswith(":start") and e[0] > pause_at["t"]]
        self.assertEqual(late_starts, [])
        valid = {seq for seq, state in self.chunk_records if state == "valid"}
        self.assertTrue(0 < len(valid) < len(self.units))

    def _with_cfg_extra(self, **extra):
        """Wrap the patched fleet-config factory to add attributes; returns the
        previous factory for the caller to restore."""
        base = runner._cloud_fleet_config

        def factory(settings):
            cfg = base(settings)
            for k, v in extra.items():
                setattr(cfg, k, v)
            return cfg

        runner._cloud_fleet_config = factory
        return base

    def test_peer_slice_path_avoids_home_uploads(self):
        base = self._with_cfg_extra(peer_slices=True)
        try:
            rc = self.run_dispatch()
        finally:
            runner._cloud_fleet_config = base
        self.assertEqual(rc, 0)
        valid = sorted(seq for seq, state in self.chunk_records if state == "valid")
        self.assertEqual(valid, [u["seq"] for u in self.units])
        self.assertEqual(self.events("up:"), [],
                         "peer path must carry every slice; no home uploads")
        fleet = FakeFleet.last
        self.assertEqual(len(fleet.peer_slice_calls), len(self.units))
        for _, name, ss, cap, expected in fleet.peer_slice_calls:
            self.assertTrue(expected.startswith("hash-unit_"))

    def test_peer_slice_failure_falls_back_to_home_upload(self):
        base = self._with_cfg_extra(peer_slices=False)
        try:
            rc = self.run_dispatch()
        finally:
            runner._cloud_fleet_config = base
        self.assertEqual(rc, 0)
        valid = sorted(seq for seq, state in self.chunk_records if state == "valid")
        self.assertEqual(valid, [u["seq"] for u in self.units])
        ups = [e for e in self.events("up:") if e[1].endswith(":start")]
        self.assertEqual(len(ups), len(self.units),
                         "every unit must fall back to a home upload")

    def test_late_pod_with_no_work_is_retired_not_leaked(self):
        # Live incident 2026-09-23: a slow-provisioning pod went READY after
        # the two-unit queue had drained and billed unowned until the spend cap
        # tripped. The fix: runners with no work mark the fleet, and any
        # unclaimed or late pod is retired instead of queued.
        self.job["frames_total"] = 2246  # 3 units -> 3 slots; the last arrives late
        self.units = runner._plan_units(2246, 750, 4)
        self.frames_by_seq = {u["seq"]: u["new"] for u in self.units}
        base = self._with_cfg_extra(defer_last_slot_s=0.3)
        try:
            rc = self.run_dispatch()
        finally:
            runner._cloud_fleet_config = base
        self.assertEqual(rc, 0)
        fleet = FakeFleet.last
        time.sleep(0.4)  # let the deferred slot land even after dispatch finished
        self.assertIn("pod2", fleet.retired, "the late pod must be retired, not leaked")
        self.assertEqual(len(fleet.retired), 3, "every pod must end retired")
        self.assertTrue(fleet.slot_queue.empty())

    def test_tail_unit_failure_is_retried_not_orphaned(self):
        # Review finding A1: a unit failing in the run's FINAL wave (queue
        # already empty) used to be requeued into a fleet whose runners had all
        # retired — orphaned, failing the job at ~95%. The standby runner must
        # pick it up.
        last_seq = self.units[-1]["seq"]
        self.fake_exec.fail_restore = {last_seq: 1}
        rc = self.run_dispatch()
        self.assertEqual(rc, 0)
        starts = [e for e in self.events(f"restore:{last_seq}:") if e[1].endswith(":start")]
        self.assertEqual(len(starts), 2, "the tail unit must be retried on another pod")
        valid = sorted(seq for seq, state in self.chunk_records if state == "valid")
        self.assertEqual(valid, [u["seq"] for u in self.units])

    def test_no_pods_at_all_fails_with_units_never_ran(self):
        patched = runner._cloud_fleet_config

        def cfg_with_no_slots(settings):
            cfg = patched(settings)
            cfg.produce_slots = 0
            return cfg

        runner._cloud_fleet_config = cfg_with_no_slots
        try:
            with self.assertRaises(runner.WorkerError) as ctx:
                self.run_dispatch()
        finally:
            runner._cloud_fleet_config = patched
        self.assertIn("never ran", str(ctx.exception))

    # ------------------------------------------- warm engine (WEDDING_POD_ENGINE)

    def test_engine_gate_off_never_touches_the_engine(self):
        # The default environment (gate unset) must be byte-identical to the
        # classic path: no engine is even instantiated.
        with mock.patch.dict(os.environ):
            os.environ.pop("WEDDING_POD_ENGINE", None)
            rc = self.run_dispatch()
        self.assertEqual(rc, 0)
        self.assertEqual(FakePodEngine.instances, [])
        self.assertEqual(self.events("engine:"), [])
        starts = [e for e in self.events("restore:") if e[1].endswith(":start")]
        self.assertEqual(len(starts), len(self.units))

    def test_engine_enabled_restores_every_unit_without_one_shot_ssh(self):
        with mock.patch.dict(os.environ, {"WEDDING_POD_ENGINE": "1"}):
            rc = self.run_dispatch()
        self.assertEqual(rc, 0)
        self.assertTrue(self.assembled.is_set())
        valid = sorted(seq for seq, state in self.chunk_records if state == "valid")
        self.assertEqual(valid, [u["seq"] for u in self.units])
        # Every restore ran on the resident engine; the classic path never fired.
        engine_ends = [e for e in self.events("engine:") if e[1].endswith(":end")]
        self.assertEqual(len(engine_ends), len(self.units))
        self.assertEqual(self.events("restore:"), [])
        # One engine per claimed pod, spawned eagerly and closed in the
        # runner's finally before retirement.
        self.assertEqual(len(FakePodEngine.instances), 3)
        for eng in FakePodEngine.instances:
            self.assertGreaterEqual(eng.started, 1)
            self.assertGreaterEqual(eng.closed, 1)
        self.assertEqual(len(FakeFleet.last.retired), 3)

    def test_engine_death_falls_back_to_classic_on_the_same_pod_without_a_strike(self):
        # An engine-layer failure must NOT condemn the pod or burn a unit
        # attempt: the same unit re-runs via the classic one-shot path on the
        # SAME pod, and that verdict decides.
        self.fake_exec.fail_engine = {2: 1}
        with mock.patch.dict(os.environ, {"WEDDING_POD_ENGINE": "1"}):
            rc = self.run_dispatch()
        self.assertEqual(rc, 0)
        valid = sorted(seq for seq, state in self.chunk_records if state == "valid")
        self.assertEqual(valid, [u["seq"] for u in self.units])
        fails = [e for e in self.events("engine:2:") if e[1].endswith(":fail")]
        self.assertEqual(len(fails), 1)
        classic = [e for e in self.events("restore:2:") if e[1].endswith(":start")]
        self.assertEqual(len(classic), 1, "the classic path must re-run the unit once")
        self.assertEqual(classic[0][2], fails[0][2],
                         "the fallback must run on the SAME pod (no requeue, no strike)")
        # No 'invalid' attempt was recorded for the engine-layer failure —
        # the unit's attempt budget is untouched by engine trouble.
        self.assertNotIn((2, "invalid"), self.chunk_records)
        # No pod was condemned: no replacement was ever requested.
        self.assertEqual(getattr(FakeFleet.last, "replacements_requested", 0), 0)
        # The failed pod's engine was closed on failure; the pod itself kept
        # working (its later units ran classic) and retired normally at the end.
        self.assertEqual(len(FakeFleet.last.retired), 3)
        failed_pod = fails[0][2]
        later_classic = [e for e in self.events("restore:")
                         if e[1].endswith(":start") and e[2] == failed_pod]
        self.assertGreaterEqual(len(later_classic), 1)

    def test_engine_exception_falls_back_with_barrier_and_remaining_budget(self):
        # Codex round-3 finding #2: an EXCEPTION escaping the engine path
        # (log I/O, argv build, callback) must behave exactly like a returned
        # failure — kill barrier, classic fallback on the same pod, no unit
        # attempt burned, no pod condemned. Finding #5: the fallback runs on
        # the REMAINDER of the unit's wall-clock budget, not a fresh one.
        self.fake_exec.raise_engine = {2: 1}
        with mock.patch.dict(os.environ, {"WEDDING_POD_ENGINE": "1"}):
            rc = self.run_dispatch()
        self.assertEqual(rc, 0)
        valid = sorted(seq for seq, state in self.chunk_records if state == "valid")
        self.assertEqual(valid, [u["seq"] for u in self.units])
        raises = self.events("engine:2:raise")
        self.assertEqual(len(raises), 1)
        classic = [e for e in self.events("restore:2:") if e[1].endswith(":start")]
        self.assertEqual(len(classic), 1, "the classic path must re-run the unit once")
        self.assertEqual(classic[0][2], raises[0][2],
                         "verified-clean fallback runs on the SAME pod")
        self.assertNotIn((2, "invalid"), self.chunk_records)
        self.assertEqual(getattr(FakeFleet.last, "replacements_requested", 0), 0)
        # The raising pod's engine went through the kill barrier before the
        # fallback started.
        self.assertEqual(sum(e.barrier_calls for e in FakePodEngine.instances), 1)
        # Deadline parity: exactly one classic call is the fallback, and it
        # received strictly less than the full unit budget (min 600 s floor);
        # any later classic restores on that engine-less pod get a fresh one.
        fallback = [b for b in self.fake_exec.classic_budgets if b < 3600.0]
        self.assertEqual(len(fallback), 1)
        self.assertGreaterEqual(fallback[0], 600.0)

    def test_engine_unverified_death_requeues_without_attempt_and_retires_pod(self):
        # Codex round-3 finding #1: when the kill barrier cannot VERIFY the
        # remote engine died, a zombie restore may still hold that GPU — the
        # unit must requeue to ANOTHER pod (no attempt burned, no classic
        # fallback on the suspect pod) and the pod must retire.
        self.fake_exec.unverified_engine = {2: 1}
        with mock.patch.dict(os.environ, {"WEDDING_POD_ENGINE": "1"}):
            rc = self.run_dispatch()
        self.assertEqual(rc, 0)
        valid = sorted(seq for seq, state in self.chunk_records if state == "valid")
        self.assertEqual(valid, [u["seq"] for u in self.units])
        unverified = self.events("engine:2:unverified")
        self.assertEqual(len(unverified), 1)
        bad_pod = unverified[0][2]
        # NOTHING else ran on the suspect pod after the unverified death: no
        # classic fallback for unit 2 anywhere (it re-ran via another engine).
        self.assertEqual(self.events("restore:2:"), [])
        retries = [e for e in self.events("engine:2:") if e[1].endswith(":end")]
        self.assertEqual(len(retries), 1)
        self.assertNotEqual(retries[0][2], bad_pod,
                            "the retry must land on a DIFFERENT pod")
        # No attempt burned: the unit never went 'invalid'.
        self.assertNotIn((2, "invalid"), self.chunk_records)
        # The suspect pod was retired.
        time.sleep(0.1)  # let its runner's finally complete
        bad_ids = [i for i in range(3) if f"10.0.0.{i}" == bad_pod]
        self.assertEqual(len(bad_ids), 1)
        self.assertIn(f"pod{bad_ids[0]}", FakeFleet.last.retired)


if __name__ == "__main__":
    unittest.main(verbosity=2)

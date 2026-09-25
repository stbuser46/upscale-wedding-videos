"""Unit test for warm-cache peer seeding in CloudFleet (no network, no pods).

The property under test: the cache tarball crosses the home upstream at most
once per fleet in the happy path (serialized), later pods fetch it pod-to-pod,
and any peer failure falls back to the home upload rather than failing bring-up.

Run:  webapp/.venv/bin/python -m unittest webapp.cloud.test_fleet_cache -v
"""

from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import webapp.cloud.fleet as fleet_mod
from webapp.cloud.fleet import CloudFleet, FleetConfig
from webapp.cloud.runpod_api import RunpodError


class FleetCacheSeedingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="fleet-cache-test-")
        tmp = Path(self._tmp.name)
        (tmp / "provision").mkdir()
        cache = tmp / "cache"
        cache.mkdir()
        (cache / "kernel.bin").write_bytes(b"x" * 1024)

        self.calls_lock = threading.Lock()
        self.rsync_calls: list[tuple] = []
        self.ssh_calls: list[tuple] = []
        self.fail_scp = False

        def fake_rsync(endpoint, local, remote, *, upload, ssh_key=None, timeout=None, **kw):
            with self.calls_lock:
                self.rsync_calls.append((endpoint, str(local), remote, upload))
            if remote == "/opt/inductor_cache.tar":
                time.sleep(0.05)  # force the threads to contend for the home link

        def fake_run_ssh(endpoint, command, *, ssh_key=None, check=True, capture=True, timeout=None):
            with self.calls_lock:
                self.ssh_calls.append((endpoint, tuple(command) if not isinstance(command, str) else command))
            if not isinstance(command, str) and command[0] == "scp" and self.fail_scp:
                raise RunpodError("fake scp failure")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        self._patches = []

        def patch(name, value):
            self._patches.append((name, getattr(fleet_mod, name)))
            setattr(fleet_mod, name, value)

        patch("rsync", fake_rsync)
        patch("run_ssh", fake_run_ssh)
        patch("RunpodClient", lambda: SimpleNamespace())
        patch("ensure_ssh_key", lambda key: "ssh-ed25519 FAKE")

        cfg = FleetConfig(
            image="fake", gpu_type_ids=["FAKE GPU"],
            provision_dir=tmp / "provision", inductor_cache=cache,
            ssh_key=tmp / "operator_key",
        )
        settings = SimpleNamespace(database_path=tmp / "unused.sqlite", data_dir=tmp)
        self.fleet = CloudFleet(settings, {"id": 1, "public_id": "cachetest"}, cfg,
                                log=lambda m: None)

    def tearDown(self):
        for name, original in self._patches:
            setattr(fleet_mod, name, original)
        self._tmp.cleanup()

    def _run_pods(self, n: int):
        def step(label, fn):
            fn()

        def one(i: int):
            self.fleet._ensure_cache_on_pod((f"10.0.0.{i}", 22000 + i), i, step)

        with ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(one, range(n)))

    def home_uploads(self):
        return [c for c in self.rsync_calls if c[2] == "/opt/inductor_cache.tar"]

    def peer_fetches(self):
        return [c for c in self.ssh_calls if not isinstance(c[1], str) and c[1][0] == "scp"]

    def test_tarball_crosses_home_link_once_then_peers_seed(self):
        self._run_pods(3)
        self.assertEqual(len(self.home_uploads()), 1, "home upload must happen exactly once")
        self.assertEqual(len(self.peer_fetches()), 2, "later pods must fetch from a peer")
        self.assertEqual(len(self.fleet._cache_seeds), 3, "every pod becomes a seed")
        # Each peer fetch pulled from an endpoint that already held the tarball.
        seed_hosts = {f"10.0.0.{i}" for i in range(3)}
        for _, cmd in self.peer_fetches():
            src = next(a for a in cmd if a.startswith("root@"))
            self.assertIn(src.split("@")[1].split(":")[0], seed_hosts)

    def test_peer_failure_falls_back_to_home_upload(self):
        self.fail_scp = True
        self._run_pods(3)
        self.assertEqual(len(self.home_uploads()), 3, "every pod must still get the cache")
        self.assertEqual(len(self.fleet._cache_seeds), 3)

    def test_no_cache_configured_is_a_noop(self):
        self.fleet.cfg.inductor_cache = None
        self._run_pods(2)
        self.assertEqual(self.rsync_calls, [])
        self.assertEqual(self.ssh_calls, [])

    def test_stopping_fleet_aborts_cache_wait(self):
        self.fleet._stop.set()
        with self.assertRaises(RunpodError):
            self.fleet._ensure_cache_on_pod(("10.0.0.9", 22999), 9, lambda label, fn: fn())

    def test_provisioning_ships_pod_engine_only_when_gated_on(self):
        # Codex round-3 finding #7: with WEDDING_POD_ENGINE unset, provisioning
        # must be byte-identical to baseline — no extra fallible pod_engine
        # transfer that could reject a pod the classic path would accept.
        class FakeProvisionPopen:
            def __init__(self, cmd, stdout=None, stderr=None, text=None):
                stdout.write("POD READY\n")
                self.returncode = 0

            def poll(self):
                return 0

            def kill(self):
                pass

        self.fleet.cfg.inductor_cache = None  # cache seeding covered elsewhere
        self.fleet._ensure_jobkey = lambda: (Path("/tmp/fake-jobkey"),
                                             "ssh-ed25519 FAKEJOB")

        def engine_ships():
            return [c for c in self.rsync_calls
                    if c[2] == "/opt/SeedVR2/pod_engine.py"]

        with mock.patch.object(fleet_mod.subprocess, "Popen", FakeProvisionPopen):
            self.assertFalse(self.fleet.cfg.pod_engine, "gate must default OFF")
            self.fleet._provision_pod(("10.0.0.1", 22001), 1)
            self.assertEqual(engine_ships(), [],
                             "gate off: pod_engine.py must NOT be shipped")
            self.fleet.cfg.pod_engine = True
            self.fleet._provision_pod(("10.0.0.2", 22002), 2)
            self.assertEqual(len(engine_ships()), 1,
                             "gate on: pod_engine.py ships once per pod")
            self.assertEqual(engine_ships()[0][0], ("10.0.0.2", 22002))


class FleetIntermediateStoreTest(unittest.TestCase):
    GOOD_HASH = "a" * 64

    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="fleet-inter-test-")
        tmp = Path(self._tmp.name)
        (tmp / "provision").mkdir()
        self.intermediate = tmp / "input_50p_ffv1.mkv"
        self.intermediate.write_bytes(b"f" * 4096)

        self.rsync_calls: list[tuple] = []
        self.ssh_calls: list[tuple] = []
        self.hash_stdout = f"{self.GOOD_HASH}  -\n"
        self.fail_cut = False

        def fake_rsync(endpoint, local, remote, *, upload, ssh_key=None, timeout=None, **kw):
            self.rsync_calls.append((endpoint, str(local), remote, upload))

        def fake_run_ssh(endpoint, command, *, ssh_key=None, check=True, capture=True, timeout=None):
            script = command if isinstance(command, str) else " ".join(map(str, command))
            self.ssh_calls.append((endpoint, script))
            if "ffmpeg" in script and "-c copy" in script and self.fail_cut:
                raise RunpodError("fake cut failure")
            out = self.hash_stdout if "framemd5" in script else ""
            return SimpleNamespace(returncode=0, stdout=out, stderr="")

        self._patches = []

        def patch(name, value):
            self._patches.append((name, getattr(fleet_mod, name)))
            setattr(fleet_mod, name, value)

        patch("rsync", fake_rsync)
        patch("run_ssh", fake_run_ssh)
        patch("RunpodClient", lambda: SimpleNamespace())
        patch("ensure_ssh_key", lambda key: "ssh-ed25519 FAKE")

        cfg = FleetConfig(image="fake", gpu_type_ids=["FAKE GPU"],
                          provision_dir=tmp / "provision", inductor_cache=None,
                          ssh_key=tmp / "operator_key")
        settings = SimpleNamespace(database_path=tmp / "unused.sqlite", data_dir=tmp)
        self.fleet = CloudFleet(settings, {"id": 1, "public_id": "intertest"}, cfg,
                                log=lambda m: None)

    def tearDown(self):
        for name, original in self._patches:
            setattr(fleet_mod, name, original)
        self._tmp.cleanup()

    def _add_slot(self, i: int, ingress_s: float | None = None):
        endpoint = (f"10.1.0.{i}", 30000 + i)
        slot = fleet_mod.Slot(pod_id=f"pod{i}", name=f"wedding-intertest-{i}",
                              endpoint=endpoint, gpu_type="FAKE", hourly_rate=1.0,
                              created_at=time.monotonic())
        with self.fleet._lock:
            self.fleet._slots[slot.pod_id] = slot
        if ingress_s is not None:
            self.fleet._ingress_hint[endpoint] = ingress_s
        return endpoint

    def _stage_and_wait(self):
        self.fleet.start_intermediate_staging(self.intermediate)
        t = self.fleet._inter_thread
        if t is not None:
            t.join(timeout=5)

    def test_staging_targets_best_ingress_pod_once(self):
        slow = self._add_slot(0, ingress_s=120.0)
        fast = self._add_slot(1, ingress_s=8.0)
        self._stage_and_wait()
        uploads = [c for c in self.rsync_calls if c[2] == fleet_mod.REMOTE_INTERMEDIATE]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0][0], fast)
        self.assertEqual(self.fleet.intermediate_seed(self.intermediate), fast)
        # Idempotent: a second call must not upload again.
        self._stage_and_wait()
        self.assertEqual(len([c for c in self.rsync_calls
                              if c[2] == fleet_mod.REMOTE_INTERMEDIATE]), 1)

    def test_staging_before_any_pod_retries_later(self):
        self._stage_and_wait()   # no pods live yet -> silent no-op, no attempt spent
        self.assertEqual(self.fleet._inter_attempts, 0)
        self.assertEqual(self.rsync_calls, [])
        self._add_slot(0, ingress_s=5.0)
        # First seed query relaunches staging; once it lands, the seed appears.
        self.assertIsNone(self.fleet.intermediate_seed(self.intermediate))
        t = self.fleet._inter_thread
        if t is not None:
            t.join(timeout=5)
        self.assertIsNotNone(self.fleet.intermediate_seed(self.intermediate))

    def test_dead_seed_is_dropped_and_restaged(self):
        self._add_slot(0, ingress_s=5.0)
        self._stage_and_wait()
        self.assertIsNotNone(self.fleet.intermediate_seed(self.intermediate))
        with self.fleet._lock:
            self.fleet._slots.clear()   # seed pod died
        self.assertIsNone(self.fleet.intermediate_seed(self.intermediate))
        self.assertLessEqual(self.fleet._inter_attempts, 3)

    def test_mark_no_more_work_drains_unclaimed_slots(self):
        terminated: list[str] = []
        self.fleet._terminate = lambda pod_id, name, ledger_id=None: terminated.append(pod_id)
        for i in range(2):
            slot = fleet_mod.Slot(pod_id=f"idle{i}", name=f"wedding-intertest-{i}",
                                  endpoint=(f"10.2.0.{i}", 31000 + i), gpu_type="FAKE",
                                  hourly_rate=1.0, created_at=time.monotonic())
            self.fleet.slot_queue.put(slot)
        self.fleet.mark_no_more_work()
        self.assertEqual(sorted(terminated), ["idle0", "idle1"])
        self.assertTrue(self.fleet.slot_queue.empty())
        self.assertTrue(self.fleet._no_more_work.is_set())

    def test_slice_from_seed_proves_hash_and_cleans_up(self):
        seed = self._add_slot(0)
        target = self._add_slot(1)
        self.fleet._inter_seed = seed
        ok = self.fleet.slice_from_seed(target, ss="14.920000", cap=754,
                                        name="unit_00001.mkv", expected_hash=self.GOOD_HASH)
        self.assertTrue(ok)
        scripts = [s for _, s in self.ssh_calls]
        self.assertTrue(any("-c copy" in s for s in scripts), "cut must run on the seed")
        self.assertTrue(any("scp" in s for s in scripts), "slice must travel pod-to-pod")
        self.assertTrue(any(s.startswith("rm") or " rm " in s for s in scripts),
                        "seed copy must be cleaned up")
        self.assertTrue(any("framemd5" in s for s in scripts), "identity must be proven")

    def test_slice_hash_mismatch_or_error_returns_false(self):
        seed = self._add_slot(0)
        target = self._add_slot(1)
        self.fleet._inter_seed = seed
        self.hash_stdout = f"{'b' * 64}  -\n"
        self.assertFalse(self.fleet.slice_from_seed(
            target, ss="0.000000", cap=750, name="unit_00000.mkv",
            expected_hash=self.GOOD_HASH))
        self.fail_cut = True
        self.assertFalse(self.fleet.slice_from_seed(
            target, ss="0.000000", cap=750, name="unit_00000.mkv",
            expected_hash=self.GOOD_HASH))


if __name__ == "__main__":
    unittest.main(verbosity=2)

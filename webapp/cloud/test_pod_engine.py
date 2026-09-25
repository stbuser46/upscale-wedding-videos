"""Real-subprocess protocol tests for PodEngine against fake_pod_engine.py.

No network, no pods, no money: the "pod engine" is a local Python child
process speaking the exact line protocol of docker/seedvr2-pod/pod_engine.py.
What IS real is everything PodEngine does: the spawn + READY handshake, the
JSON request line, log passthrough, frame parsing from "ENGINE DONE", the
abort/timeout/stall kills, sudden-death detection, and close() idempotence.

Run:  webapp/.venv/bin/python -m unittest webapp.cloud.test_pod_engine -v
"""

from __future__ import annotations

import io
import sys
import tempfile
import time
import unittest
from pathlib import Path

from webapp.cloud.executor import (
    STATUS_ENGINE_UNVERIFIED,
    PodEngine,
    restore_unit_via_engine,
)

FAKE = Path(__file__).resolve().parent / "fake_pod_engine.py"
ARGV = ["/workspace/slices/unit_00001.mkv", "--output", "/workspace/units/unit_00001.mkv"]
UNIT = {"seq": 0, "skip": 0, "cap": 150, "prepend": 4, "drop": 0, "new": 150}


class RecordingKiller:
    """Stands in for pod_engine_remote_killer's closure: records the pgid it
    was asked to kill and returns a scripted verification verdict."""

    def __init__(self, verdict: bool = True):
        self.verdict = verdict
        self.calls: list[int] = []

    def __call__(self, pgid: int) -> bool:
        self.calls.append(pgid)
        return self.verdict


class PodEngineProtocolTest(unittest.TestCase):
    def engine(self, mode: str, **kw) -> PodEngine:
        eng = PodEngine([sys.executable, "-u", str(FAKE), mode],
                        name=f"fake-{mode}", **kw)
        self.addCleanup(eng.close)
        return eng

    def restore(self, eng: PodEngine, log: io.StringIO | None = None, **kw):
        kw.setdefault("poll", 0.05)
        kw.setdefault("max_run_s", 30.0)
        kw.setdefault("stall_s", 30.0)
        return eng.restore(ARGV, log if log is not None else io.StringIO(), **kw)

    # ---------------------------------------------------------------- happy

    def test_ready_handshake_frames_parse_and_resident_reuse(self):
        eng = self.engine("happy")
        self.assertTrue(eng.alive, "an unstarted engine is worth trying")
        log1, log2 = io.StringIO(), io.StringIO()
        r1 = self.restore(eng, log1)
        self.assertEqual(r1.status, 0)
        self.assertEqual(r1.frames, 754)
        pid = eng._proc.pid
        r2 = self.restore(eng, log2)
        self.assertEqual(r2.status, 0)
        self.assertEqual(r2.frames, 754)
        self.assertEqual(eng._proc.pid, pid, "request #2 must reuse the resident process")
        self.assertTrue(eng.alive)
        # READY appears exactly once (first restore), CLI logging passes through.
        self.assertIn("ENGINE READY", log1.getvalue())
        self.assertIn("Chunk 1/6", log1.getvalue())
        self.assertNotIn("ENGINE READY", log2.getvalue())
        self.assertIn("Written 754/754", log2.getvalue())
        # The pid announced at READY is parsed: it is the remote PGID handle
        # for the kill barrier (and here, the fake IS the local child).
        self.assertEqual(eng.remote_pgid, pid)

    def test_close_sends_exit_for_a_clean_shutdown(self):
        eng = self.engine("happy")
        self.assertEqual(self.restore(eng).status, 0)
        eng.close()
        self.assertFalse(eng.alive)
        self.assertEqual(eng._proc.returncode, 0,
                         "EXIT must let the engine leave cleanly, not be killed")

    # ------------------------------------------------------------- failures

    def test_err_reply_is_status_1_and_leaves_the_process_alive(self):
        eng = self.engine("err")
        res = self.restore(eng)
        self.assertEqual(res.status, 1)
        self.assertIn("boom", res.message)
        # Per protocol the engine survives an ERR; the DISPATCHER closes it
        # (identity discipline) — PodEngine itself reports honestly.
        self.assertTrue(eng.alive)

    def test_sudden_death_is_detected(self):
        eng = self.engine("die")
        res = self.restore(eng)
        self.assertEqual(res.status, 1)
        self.assertIn("died", res.message)
        self.assertFalse(eng.alive)

    def test_garbage_done_line_kills_the_engine(self):
        eng = self.engine("garbage")
        res = self.restore(eng)
        self.assertEqual(res.status, 1)
        self.assertIn("unparseable", res.message)
        self.assertFalse(eng.alive)

    # ------------------------------------------------- kills (stall/time/abort)

    def test_stall_kills_the_process(self):
        eng = self.engine("slow")
        t0 = time.monotonic()
        res = self.restore(eng, stall_s=0.4, max_run_s=60.0)
        self.assertEqual(res.status, 2)
        self.assertIn("silent", res.message)
        self.assertFalse(eng.alive, "a stalled engine must be killed, not reused")
        self.assertLess(time.monotonic() - t0, 10.0, "stall kill must not wait out the sleep")

    def test_wall_clock_timeout_kills_the_process(self):
        eng = self.engine("slow")
        res = self.restore(eng, max_run_s=0.4, stall_s=60.0)
        self.assertEqual(res.status, 2)
        self.assertIn("timed out", res.message)
        self.assertFalse(eng.alive)

    def test_abort_kills_the_process(self):
        eng = self.engine("slow")
        t0 = time.monotonic()
        res = self.restore(eng, should_abort=lambda: time.monotonic() - t0 > 0.2)
        self.assertEqual(res.status, -1)
        self.assertEqual(res.message, "aborted")
        self.assertFalse(eng.alive)

    def test_ready_timeout_is_bounded(self):
        eng = self.engine("no-ready", ready_timeout_s=0.5)
        t0 = time.monotonic()
        res = self.restore(eng)
        self.assertEqual(res.status, 2)
        self.assertIn("READY", res.message)
        self.assertFalse(eng.alive)
        self.assertLess(time.monotonic() - t0, 10.0)

    # -------------------------------------------------- kill barrier (remote)

    def test_barrier_without_remote_killer_is_local_kill(self):
        # For a direct-subprocess engine (tests, local runs) killing the
        # process IS the whole kill; the barrier must verify trivially.
        eng = self.engine("slow")
        res = self.restore(eng, stall_s=0.4, max_run_s=60.0)
        self.assertEqual(res.status, 2)
        self.assertTrue(eng.shutdown_barrier())
        self.assertFalse(eng.alive)

    def test_barrier_kills_remote_group_and_caches_verified_death(self):
        killer = RecordingKiller(verdict=True)
        eng = self.engine("slow", remote_killer=killer)
        res = self.restore(eng, stall_s=0.4, max_run_s=60.0)
        self.assertEqual(res.status, 2)
        self.assertTrue(eng.shutdown_barrier())
        self.assertEqual(killer.calls, [eng.remote_pgid],
                         "the barrier must kill the pgid announced at READY")
        self.assertTrue(eng.shutdown_barrier())
        self.assertEqual(len(killer.calls), 1, "verified death is cached")

    def test_barrier_before_any_request_never_ssh_kills(self):
        # READY timeout: no request was ever sent, so nothing remote can be
        # restoring — the barrier is trivially satisfied without an ssh kill.
        killer = RecordingKiller(verdict=False)
        eng = self.engine("no-ready", ready_timeout_s=0.4, remote_killer=killer)
        res = self.restore(eng)
        self.assertEqual(res.status, 2)
        self.assertTrue(eng.shutdown_barrier())
        self.assertEqual(killer.calls, [])

    def _via_engine(self, eng: PodEngine, **kw):
        with tempfile.TemporaryDirectory(prefix="pod-engine-log-") as tmp:
            kw.setdefault("poll", 0.05)
            kw.setdefault("max_run_s", 30.0)
            kw.setdefault("stall_s", 30.0)
            return restore_unit_via_engine(
                eng, slice_name="unit_00001.mkv", out_name="unit_00001.mkv",
                unit=UNIT, model="m.safetensors", resolution=1440, batch=129,
                overlap=4, log_path=Path(tmp) / "unit.log", **kw)

    def test_restore_unit_via_engine_runs_barrier_and_keeps_status_when_verified(self):
        killer = RecordingKiller(verdict=True)
        eng = self.engine("err", remote_killer=killer)
        res = self._via_engine(eng)
        self.assertEqual(res.status, 1, "verified death keeps the honest status")
        self.assertIn("boom", res.message)
        self.assertEqual(killer.calls, [eng.remote_pgid],
                         "the barrier must run before the caller can fall back")
        self.assertFalse(eng.alive, "an ERR'd engine is killed, not reused")

    def test_unverified_remote_death_escalates_to_status_3(self):
        killer = RecordingKiller(verdict=False)
        eng = self.engine("err", remote_killer=killer)
        res = self._via_engine(eng)
        self.assertEqual(res.status, STATUS_ENGINE_UNVERIFIED)
        self.assertIn("UNVERIFIED", res.message)
        self.assertEqual(killer.calls, [eng.remote_pgid])

    # -------------------------------------------- log writer (blocked disk)

    def test_blocked_log_filesystem_cannot_stop_stall_enforcement(self):
        # Codex round-3 finding #6: log persistence is decoupled from the
        # protocol monitor. A log handle that blocks for a minute per write
        # must not delay the stall kill.
        class BlockedHandle:
            def __init__(self):
                self.attempts = 0

            def write(self, s):
                self.attempts += 1
                time.sleep(60)

        eng = self.engine("slow")
        t0 = time.monotonic()
        res = self.restore(eng, BlockedHandle(), stall_s=0.5, max_run_s=60.0)
        elapsed = time.monotonic() - t0
        self.assertEqual(res.status, 2)
        self.assertIn("silent", res.message)
        self.assertLess(elapsed, 15.0,
                        "a blocked log write must not stall the monitor "
                        "(sync writes would block ~60s per line)")
        self.assertFalse(eng.alive)

    def test_log_lines_still_persist_through_the_writer(self):
        eng = self.engine("happy")
        log = io.StringIO()
        res = self.restore(eng, log)
        self.assertEqual(res.status, 0)
        # close() drains the bounded queue before restore() returns.
        self.assertIn("Chunk 1/6", log.getvalue())
        self.assertIn("ENGINE PID", log.getvalue())

    # ----------------------------------------------------------- close/misc

    def test_close_is_idempotent_and_never_raises(self):
        eng = self.engine("happy")
        self.assertEqual(self.restore(eng).status, 0)
        eng.close()
        eng.close()
        self.assertFalse(eng.alive)
        self.assertEqual(self.restore(eng).status, 1, "restore after close is a dead-engine error")

    def test_close_before_start_is_safe(self):
        eng = PodEngine([sys.executable, "-u", str(FAKE), "happy"], name="unstarted")
        eng.close()
        eng.close()
        self.assertFalse(eng.alive)

    def test_unspawnable_command_is_a_dead_engine_not_an_exception(self):
        eng = PodEngine(["/nonexistent-binary-for-test"], name="unspawnable")
        self.assertFalse(eng.start())
        res = self.restore(eng)
        self.assertEqual(res.status, 1)
        self.assertFalse(eng.alive)


if __name__ == "__main__":
    unittest.main(verbosity=2)

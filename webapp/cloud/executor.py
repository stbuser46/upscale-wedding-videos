"""Run one durable unit on a remote pod: push the slice, restore, pull the result.

Stateless and thread-safe — the fleet dispatcher calls this from one thread per
pod, so many units restore concurrently on different pods. The pinned argv comes
from `seedvr2_unit_argv` (the shared shell builder), so a cloud unit is invoked
byte-for-byte like a local one.

The three phases are exposed separately (`upload_unit_slice`, `restore_unit`,
`download_unit`) so the dispatcher can pipeline them per pod — uploading the
next unit's slice and downloading the previous unit's result while the GPU
restores the current one. `run_unit_remote` composes them sequentially and is
the single-pod path (and the reference for what a full unit round-trip means).
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, IO

from .runpod_api import rsync, run_ssh, seedvr2_unit_argv, ssh_command

REMOTE_SLICES = "/workspace/slices"
REMOTE_UNITS = "/workspace/units"
REMOTE_MODELS = "/opt/models/seedvr2"
POD_ENGINE_PATH = "/opt/SeedVR2/pod_engine.py"

# Engine-path status for "the engine was killed but the REMOTE side's death
# could not be verified": the pod may still be running a zombie restore, so
# the caller must requeue the unit elsewhere (no attempt burned) and retire
# the pod rather than run anything else on it.
STATUS_ENGINE_UNVERIFIED = 3


@dataclass
class UnitResult:
    status: int            # 0 ok; non-zero = remote failure; -1 = aborted;
                           # 3 = engine killed, remote death UNVERIFIED (see
                           # STATUS_ENGINE_UNVERIFIED: requeue + retire pod)
    upload_s: float = 0.0
    restore_s: float = 0.0
    download_s: float = 0.0
    message: str = ""
    frames: int | None = None  # engine path: frames reported by "ENGINE DONE"


def _append_log(log_path: Path, text: str) -> None:
    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        log.write(text)
        log.flush()


def upload_unit_slice(
    endpoint: tuple[str, int],
    ssh_key: Path,
    *,
    slice_path: Path,
    unit: dict,
    log_path: Path,
    timeout: float = 900,
    should_abort: Callable[[], bool] = lambda: False,
) -> UnitResult:
    """Push one unit's slice to the pod. Safe to run while the pod's GPU is
    restoring a different unit (rsync-over-ssh does not touch the GPU)."""
    slice_path = Path(slice_path)
    _append_log(log_path, f"\n=== unit {unit['seq']} upload -> {endpoint[0]}:{endpoint[1]} ===\n")
    t0 = time.monotonic()
    # Retry like the download does: rsync is --partial/resumable, and a
    # transient blip must not cost a fully-provisioned pod plus one of the
    # unit's attempts (observed live 2026-09-24: one failed transfer retired
    # a pod that had just spent 25 min provisioning).
    last_exc: Exception | None = None
    for attempt in range(3):
        if should_abort():
            return UnitResult(status=-1, message="aborted")
        try:
            rsync(endpoint, slice_path, f"{REMOTE_SLICES}/{slice_path.name}",
                  upload=True, ssh_key=ssh_key, timeout=timeout, abort_check=should_abort)
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            _append_log(log_path,
                        f"=== unit {unit['seq']} upload attempt {attempt + 1}/3 failed: {exc} ===\n")
            time.sleep(3 * (attempt + 1))
    if last_exc is not None:
        return UnitResult(status=1, message=f"upload failed after 3 attempts: {last_exc}")
    return UnitResult(status=0, upload_s=time.monotonic() - t0)


def restore_unit(
    endpoint: tuple[str, int],
    ssh_key: Path,
    *,
    slice_name: str,
    out_name: str,
    unit: dict,
    model: str,
    resolution: int,
    batch: int,
    overlap: int,
    log_path: Path,
    should_abort: Callable[[], bool] = lambda: False,
    poll: float = 2.0,
    max_run_s: float = 3600.0,
) -> UnitResult:
    """Run the SeedVR2 restore of an already-uploaded slice on the pod.

    `unit` carries the durable-unit fields skip/cap/prepend/drop. `should_abort`
    is polled while the remote job runs; when it returns True the ssh process is
    terminated (the fleet then terminates the pod, killing the remote worker).

    `max_run_s` bounds the remote restore: a 750-frame unit takes ~15-20 min, so
    a run past an hour means the pod's ssh hung or its GPU stalled. Time out and
    fail (status 2) rather than looping forever — the caller retires the bad pod
    and the unit is retried elsewhere. Without this a single wedged pod hangs the
    whole job (observed once: a pod's ssh died mid-unit and the executor spun for
    25 min with no progress).
    """
    remote_in = f"{REMOTE_SLICES}/{slice_name}"
    remote_out = f"{REMOTE_UNITS}/{out_name}"
    argv = seedvr2_unit_argv(
        input_path=remote_in, output_path=remote_out, model_dir=REMOTE_MODELS,
        model=model, resolution=resolution, batch=batch, overlap=overlap,
        skip=0, cap=unit["cap"], prepend=unit["prepend"], drop=unit["drop"],
    )
    cmd = ssh_command(endpoint, ["python", "/opt/SeedVR2/inference_cli.py", *argv], ssh_key)

    import threading

    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        log.write(f"\n=== unit {unit['seq']} restore on {endpoint[0]}:{endpoint[1]} "
                  f"skip={unit['skip']} cap={unit['cap']} prepend={unit['prepend']} "
                  f"drop={unit['drop']} ===\n")
        log.flush()

        t1 = time.monotonic()
        # Pipe stdout through a reader thread (instead of writing straight to
        # the log) so silence is measurable: a healthy restore prints something
        # at least every few minutes (batch lines ~80 s apart; the write phase
        # is the quietest at ~1-3 min). A pod observed on 2026-09-24 wedged in
        # a cold kernel compile for 40+ min with the GPU idle, billing until
        # the 1-hour timeout — the stall guard catches that class in stall_s.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        last_line = [time.monotonic()]

        def _pump():
            for line in proc.stdout:
                log.write(line)
                last_line[0] = time.monotonic()
            proc.stdout.close()

        pump = threading.Thread(target=_pump, daemon=True, name="restore-pump")
        pump.start()
        aborted = False
        timed_out = False
        stalled = False
        stall_s = 900.0

        def _kill():
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

        while proc.poll() is None:
            if should_abort():
                aborted = True
                _kill()
                break
            if time.monotonic() - t1 > max_run_s:
                timed_out = True
                _kill()
                break
            if time.monotonic() - last_line[0] > stall_s:
                stalled = True
                _kill()
                break
            time.sleep(poll)
        pump.join(timeout=10)
        t_run = time.monotonic() - t1
        if aborted:
            return UnitResult(status=-1, restore_s=t_run, message="aborted")
        if timed_out:
            log.write(f"=== unit {unit['seq']} TIMED OUT after {t_run:.0f}s (pod likely wedged) ===\n")
            return UnitResult(status=2, restore_s=t_run,
                              message=f"remote restore timed out after {t_run:.0f}s")
        if stalled:
            log.write(f"=== unit {unit['seq']} STALLED: no output for {stall_s:.0f}s "
                      f"after {t_run:.0f}s (pod likely wedged) ===\n")
            return UnitResult(status=2, restore_s=t_run,
                              message=f"remote restore silent for {stall_s:.0f}s (wedged)")
        if proc.returncode != 0:
            return UnitResult(status=proc.returncode or 1, restore_s=t_run,
                              message=f"remote restore exited {proc.returncode}")
    return UnitResult(status=0, restore_s=t_run)


def pod_engine_command(endpoint: tuple[str, int], ssh_key: Path) -> list[str]:
    """The argv that spawns a pod's resident engine over one persistent ssh
    session. Isolated so tests can hand PodEngine a local fake instead.

    `setsid` puts the remote engine in its OWN session/process group, whose
    PGID equals the pid the engine reports in its "ENGINE READY pid=N" line.
    That is what makes the abnormal-shutdown kill barrier possible: killing
    `-PGID` takes down the engine AND every child it spawned (ffmpeg writer
    etc.), so a stalled restore can never keep running invisibly after the
    local side gave up (Codex round-3 finding #1)."""
    # --wait is ESSENTIAL: bare setsid forks and exits, ssh sees the command
    # finish and closes the connection, and the orphaned engine reads stdin
    # EOF before any request arrives (exactly how the first paid canary died,
    # 2026-09-25: "ENGINE READY" followed immediately by "ENGINE EXITING").
    # With --wait, setsid stays in the foreground as the session leader's
    # parent, keeping ssh's stdin/stdout wired to the engine.
    return ssh_command(endpoint, ["setsid", "--wait", "python", POD_ENGINE_PATH], ssh_key)


def pod_engine_remote_killer(endpoint: tuple[str, int], ssh_key: Path) -> Callable[[int], bool]:
    """Build the remote kill barrier for a pod's resident engine.

    Returns a callable(pgid) -> bool that best-effort SIGTERM/SIGKILLs the
    engine's whole remote process group over ssh and then VERIFIES death with
    `kill -0` polling. True is returned only when nothing in the group is
    left; False means the pod cannot be proven clean and must not run another
    restore (the dispatcher requeues the unit elsewhere and retires the pod).
    Every ssh call is bounded, and the whole barrier gives up after ~40 s, so
    an unreachable pod costs seconds, not a hang."""

    def _barrier(pgid: int) -> bool:
        group = f"-{int(pgid)}"

        def _ssh(argv: list[str]):
            try:
                return run_ssh(endpoint, argv, ssh_key=ssh_key, check=False, timeout=25)
            except Exception:
                return None  # unreachable / timed out: cannot verify

        _ssh(["kill", "-TERM", "--", group])
        deadline = time.monotonic() + 40.0
        while time.monotonic() < deadline:
            probe = _ssh(["kill", "-0", "--", group])
            if probe is not None and probe.returncode != 0:
                return True  # no such process group: everything is dead
            time.sleep(2.0)
            _ssh(["kill", "-KILL", "--", group])
        return False

    return _barrier


class _BoundedLogWriter:
    """Decouple log persistence from the protocol monitor (Codex round-3
    finding #6): the monitor loop must keep enforcing abort / wall-clock /
    stall even when the log filesystem blocks. Lines land in a BOUNDED queue
    (put never blocks — when full, the OLDEST line is dropped and counted)
    and a daemon thread drains them to the handle. Losing log lines under a
    wedged filesystem is acceptable; losing timeout enforcement is not."""

    _STOP = object()

    def __init__(self, handle: "IO[str]", max_lines: int = 2000):
        self._handle = handle
        self._q: "queue.Queue[object]" = queue.Queue(maxsize=max_lines)
        self._dropped = 0
        self._thread = threading.Thread(target=self._drain, daemon=True,
                                        name="pod-engine-log")
        self._thread.start()

    def write(self, line: str) -> None:
        """Never blocks and never raises: drop-oldest when the queue is full."""
        while True:
            try:
                self._q.put_nowait(line)
                return
            except queue.Full:
                try:
                    self._q.get_nowait()
                    self._dropped += 1
                except queue.Empty:
                    pass

    def _drain(self) -> None:
        while True:
            item = self._q.get()
            if item is self._STOP:
                return
            try:
                self._handle.write(item)
            except Exception:
                pass  # a broken log must never kill the writer

    def close(self, timeout: float = 5.0) -> None:
        """Bounded: give the writer a moment to flush, then abandon it (the
        daemon thread can finish or die with the process; the protocol side
        must not wait on a blocked filesystem)."""
        if self._dropped:
            self.write(f"[pod-engine] WARNING: {self._dropped} log lines dropped "
                       f"(log filesystem too slow for the protocol monitor)\n")
        self.write(self._STOP)  # type: ignore[arg-type]
        self._thread.join(timeout=timeout)


class PodEngine:
    """One persistent `pod_engine.py` process per pod: the SeedVR2 model loads
    ONCE and every later unit skips the 2-4 min weight-load/compile-replay
    window that a fresh `inference_cli.py` pays.

    Protocol (see docker/seedvr2-pod/pod_engine.py): we write one JSON argv
    array per line on stdin; the engine prints "ENGINE READY pid=<pid>" once
    after import (the pid is its remote PGID — it runs under `setsid`),
    passes all CLI logging through stdout per request, and terminates each
    request with exactly one of "ENGINE DONE <frames>" / "ENGINE ERR
    <message>". "EXIT" asks it to quit.

    Lifecycle: the constructor only records the spawn argv and NEVER raises;
    `start()` spawns the process (non-blocking); the READY handshake is
    awaited (bounded) on the first `restore()`. Any violation — abort, wall
    clock, output stall, process death, protocol garbage — kills the PROCESS
    and marks the engine dead; `alive` then stays False forever and the
    dispatcher falls back to the classic one-shot restore path. `close()` is
    idempotent and never raises.

    Kill barrier: killing the local ssh does NOT prove the remote python (or
    its ffmpeg children) died — a zombie restore could overlap the classic
    fallback on the same GPU/output (Codex round-3 finding #1). After any
    abnormal end the caller runs `shutdown_barrier()`, which ssh-kills the
    remote process group (`remote_killer`) and verifies death; only a True
    return makes the pod safe to reuse.

    Threading: one owner thread (the pod's runner) calls restore()/close();
    an internal daemon thread pumps stdout lines into a queue so silence is
    measurable (same stall rationale as `restore_unit`), and a bounded
    writer thread persists log lines so a blocked log filesystem can never
    stop abort/stall/timeout enforcement.
    """

    READY_LINE = "ENGINE READY"
    DONE_PREFIX = "ENGINE DONE"
    ERR_PREFIX = "ENGINE ERR"

    def __init__(self, command: list[str], *, name: str = "pod engine",
                 ready_timeout_s: float = 120.0,
                 remote_killer: Callable[[int], bool] | None = None):
        self.command = list(command)
        self.name = name
        self.ready_timeout_s = ready_timeout_s
        self._proc: subprocess.Popen | None = None
        self._lines: "queue.Queue[str | None]" = queue.Queue()
        self._reader: threading.Thread | None = None
        self._ready = False
        self._dead = False
        self._lock = threading.Lock()
        # Remote kill barrier state (Codex round-3 finding #1).
        self._remote_killer = remote_killer
        self._remote_pgid: int | None = None   # parsed from "ENGINE READY pid=N"
        self._request_sent = False             # a restore request ever reached the engine
        self._remote_verified = False          # remote group death verified once

    @property
    def remote_pgid(self) -> int | None:
        """The remote engine's process-group id (== the pid it reported at
        READY, thanks to setsid), or None before READY / for pid-less fakes."""
        return self._remote_pgid

    # ------------------------------------------------------------- lifecycle

    @property
    def alive(self) -> bool:
        """True while the engine is usable (or not yet spawned — worth trying).
        Once dead, dead forever: the caller switches to the classic path."""
        if self._dead:
            return False
        proc = self._proc
        return proc is None or proc.poll() is None

    def start(self) -> bool:
        """Spawn the engine process (non-blocking; ssh + Python imports run in
        the background). Safe to call repeatedly. Never raises."""
        with self._lock:
            return self._spawn()

    def _spawn(self) -> bool:
        if self._dead:
            return False
        if self._proc is not None:
            return self._proc.poll() is None
        try:
            self._proc = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1)
        except Exception:
            self._dead = True
            return False
        self._reader = threading.Thread(target=self._pump, daemon=True,
                                        name="pod-engine-pump")
        self._reader.start()
        return True

    def _pump(self) -> None:
        """Read every stdout line into the queue; a final None marks EOF (the
        process died or exited) so consumers detect sudden death promptly."""
        proc = self._proc
        try:
            for line in proc.stdout:
                self._lines.put(line)
        except Exception:
            pass
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            self._lines.put(None)

    def _kill(self) -> None:
        """Kill the PROCESS and mark the engine dead. Never raises."""
        self._dead = True
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        except Exception:
            pass
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass

    def close(self) -> None:
        """Polite shutdown: send EXIT, give the engine a moment to leave
        cleanly, then make sure the process is gone. Idempotent; never
        raises. The engine is dead afterwards regardless of what happened."""
        with self._lock:
            proc = self._proc
            if proc is not None and proc.poll() is None and not self._dead:
                try:
                    proc.stdin.write("EXIT\n")
                    proc.stdin.flush()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
            self._kill()

    def shutdown_barrier(self) -> bool:
        """Abnormal-shutdown kill barrier (Codex round-3 finding #1): make
        sure NOTHING of the engine — remote python, its ffmpeg children, the
        whole setsid process group — can still be restoring before the caller
        runs anything else on this pod. Kills the local process, then
        best-effort ssh-kills the remote group and VERIFIES death.

        Returns True only when the remote side is proven dead (or nothing
        remote ever ran: no request was sent, or there is no remote at all —
        the local process WAS the engine, as in tests). On False the pod must
        not be reused: requeue the unit elsewhere and retire the pod.
        Idempotent; never raises."""
        with self._lock:
            self._kill()
        if self._remote_killer is None:
            return True  # local subprocess was the whole engine; _kill() killed it
        if not self._request_sent:
            return True  # no restore was ever asked for; nothing can be running
        if self._remote_pgid is None:
            return False  # a request ran but we never learned the group: unverifiable
        if self._remote_verified:
            return True
        try:
            self._remote_verified = bool(self._remote_killer(self._remote_pgid))
        except Exception:
            self._remote_verified = False
        return self._remote_verified

    # --------------------------------------------------------------- restore

    def restore(
        self,
        argv: list[str],
        log_handle: "IO[str]",
        should_abort: Callable[[], bool] = lambda: False,
        max_run_s: float = 3600.0,
        stall_s: float = 900.0,
        poll: float = 2.0,
    ) -> UnitResult:
        """Run one unit on the resident engine. Same status conventions as
        `restore_unit`: 0 ok (frames parsed from "ENGINE DONE"), 2 stall or
        timeout, -1 aborted, 1 engine error/death. All engine output — CLI
        logging passed through unchanged — is persisted to `log_handle` via a
        bounded writer thread (never inline: a blocked log filesystem must
        not stop abort/stall/timeout enforcement), so the job log reads
        exactly like the classic path's. Stall detection timestamps when a
        line is RECEIVED from the reader queue, independent of log I/O."""
        t0 = time.monotonic()
        writer = _BoundedLogWriter(log_handle)
        try:
            with self._lock:
                return self._restore_locked(argv, writer, should_abort,
                                            max_run_s, stall_s, poll, t0)
        finally:
            writer.close()

    def _restore_locked(self, argv, writer, should_abort, max_run_s, stall_s,
                        poll, t0) -> UnitResult:
        if not self._spawn():
            return UnitResult(status=1, message="engine is dead")
        # READY handshake, first call only. Bounded: the first READY costs
        # Python import time only (the model loads lazily on request #1).
        if not self._ready:
            deadline = t0 + self.ready_timeout_s
            while True:
                if should_abort():
                    self._kill()
                    return UnitResult(status=-1, restore_s=time.monotonic() - t0,
                                      message="aborted")
                if time.monotonic() > deadline:
                    self._kill()
                    return UnitResult(
                        status=2, restore_s=time.monotonic() - t0,
                        message=f"engine not READY within {self.ready_timeout_s:.0f}s")
                try:
                    line = self._lines.get(timeout=min(poll, 1.0))
                except queue.Empty:
                    continue
                if line is None:
                    self._kill()
                    return UnitResult(status=1, restore_s=time.monotonic() - t0,
                                      message="engine died before READY")
                writer.write(line)
                stripped = line.strip()
                if stripped == self.READY_LINE or stripped.startswith(self.READY_LINE + " "):
                    # "ENGINE READY pid=N": N is the remote PGID (setsid),
                    # the handle for the abnormal-shutdown kill barrier.
                    tail = stripped[len(self.READY_LINE):].strip()
                    if tail.startswith("pid="):
                        try:
                            self._remote_pgid = int(tail[4:])
                        except ValueError:
                            pass
                    self._ready = True
                    break
        # Send the request: one JSON argv array per line.
        try:
            self._request_sent = True
            self._proc.stdin.write(json.dumps([str(a) for a in argv]) + "\n")
            self._proc.stdin.flush()
        except Exception as exc:
            self._kill()
            return UnitResult(status=1, restore_s=time.monotonic() - t0,
                              message=f"engine request write failed: {exc}")
        # Pump output until the DONE/ERR terminator, enforcing abort /
        # wall clock / stall exactly like restore_unit (kill on violation).
        last_line = time.monotonic()
        while True:
            now = time.monotonic()
            if should_abort():
                self._kill()
                return UnitResult(status=-1, restore_s=now - t0, message="aborted")
            if now - t0 > max_run_s:
                self._kill()
                return UnitResult(status=2, restore_s=now - t0,
                                  message=f"engine restore timed out after {now - t0:.0f}s")
            if now - last_line > stall_s:
                self._kill()
                return UnitResult(status=2, restore_s=now - t0,
                                  message=f"engine silent for {stall_s:.0f}s (wedged)")
            try:
                line = self._lines.get(timeout=min(poll, 1.0))
            except queue.Empty:
                continue
            if line is None:
                self._kill()
                return UnitResult(status=1, restore_s=time.monotonic() - t0,
                                  message="engine process died mid-restore")
            last_line = time.monotonic()
            writer.write(line)
            stripped = line.strip()
            if stripped.startswith(self.DONE_PREFIX):
                tail = stripped[len(self.DONE_PREFIX):].strip()
                try:
                    frames = int(tail)
                except ValueError:
                    # Protocol garbage where a frame count belongs: do not
                    # trust this engine's state — kill it and let the
                    # classic path re-run the unit.
                    self._kill()
                    return UnitResult(status=1, restore_s=time.monotonic() - t0,
                                      message=f"engine DONE line unparseable: {stripped[:120]}")
                return UnitResult(status=0, restore_s=time.monotonic() - t0,
                                  frames=frames)
            if stripped.startswith(self.ERR_PREFIX):
                # The remote request failed but the engine process itself
                # is alive per protocol. The dispatcher still treats this
                # as an engine-layer failure: it closes the engine and
                # re-runs the unit via the classic path, whose verdict is
                # authoritative (identity discipline: never restore on an
                # engine that just threw).
                return UnitResult(status=1, restore_s=time.monotonic() - t0,
                                  message=stripped[len(self.ERR_PREFIX):].strip()
                                          or "engine error")


def restore_unit_via_engine(
    engine: PodEngine,
    *,
    slice_name: str,
    out_name: str,
    unit: dict,
    model: str,
    resolution: int,
    batch: int,
    overlap: int,
    log_path: Path,
    should_abort: Callable[[], bool] = lambda: False,
    poll: float = 2.0,
    max_run_s: float = 3600.0,
    stall_s: float = 900.0,
) -> UnitResult:
    """Engine-aware sibling of `restore_unit`: same pinned argv (built by the
    ONE shared shell builder, so local/cloud/engine can never drift), same log
    format, same status conventions — but the restore runs on the pod's
    resident engine instead of a fresh `inference_cli.py` process.

    Abnormal shutdown contract (Codex round-3 finding #1): on any engine
    failure (status 1/2) the KILL BARRIER runs before this function returns —
    the remote process group is ssh-killed and its death verified — so the
    classic fallback can never overlap a live zombie restore. If the barrier
    cannot verify death, the status escalates to STATUS_ENGINE_UNVERIFIED (3):
    the caller must requeue the unit elsewhere WITHOUT burning an attempt and
    retire the pod instead of falling back on it.

    `max_run_s` is the unit's TOTAL wall-clock budget; on fallback the caller
    passes only the remaining budget to the classic `restore_unit` (deadline
    parity, finding #5)."""
    remote_in = f"{REMOTE_SLICES}/{slice_name}"
    remote_out = f"{REMOTE_UNITS}/{out_name}"
    argv = seedvr2_unit_argv(
        input_path=remote_in, output_path=remote_out, model_dir=REMOTE_MODELS,
        model=model, resolution=resolution, batch=batch, overlap=overlap,
        skip=0, cap=unit["cap"], prepend=unit["prepend"], drop=unit["drop"],
    )
    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        log.write(f"\n=== unit {unit['seq']} restore (warm engine) on {engine.name} "
                  f"skip={unit['skip']} cap={unit['cap']} prepend={unit['prepend']} "
                  f"drop={unit['drop']} ===\n")
        log.flush()
        res = engine.restore(argv, log, should_abort=should_abort,
                             max_run_s=max_run_s, stall_s=stall_s, poll=poll)
        if res.status in (1, 2):
            # Kill barrier before ANY fallback can start on this pod.
            verified = engine.shutdown_barrier()
            if not verified:
                res = UnitResult(status=STATUS_ENGINE_UNVERIFIED,
                                 restore_s=res.restore_s,
                                 message=f"{res.message}; remote engine death "
                                         f"UNVERIFIED — pod must not be reused")
        if res.status == STATUS_ENGINE_UNVERIFIED:
            log.write(f"=== unit {unit['seq']} ENGINE UNVERIFIED DEATH after "
                      f"{res.restore_s:.0f}s: {res.message} ===\n")
        elif res.status == 2:
            log.write(f"=== unit {unit['seq']} ENGINE STALL/TIMEOUT after "
                      f"{res.restore_s:.0f}s: {res.message} ===\n")
        elif res.status == 1:
            log.write(f"=== unit {unit['seq']} ENGINE FAILURE: {res.message} ===\n")
    return res


def download_unit(
    endpoint: tuple[str, int],
    ssh_key: Path,
    *,
    local_out: Path,
    unit: dict,
    log_path: Path,
    should_abort: Callable[[], bool] = lambda: False,
    timeout: float = 1800,
) -> UnitResult:
    """Pull one restored unit off the pod.

    The unit is ALREADY restored (paid for) on the pod. A transient network
    blip on the pull must not throw that work away and retire the pod — so
    retry a few times while the pod is still alive. rsync is --partial
    --inplace, so each retry resumes the same file rather than restarting.
    """
    local_out = Path(local_out)
    local_out.parent.mkdir(parents=True, exist_ok=True)
    remote_out = f"{REMOTE_UNITS}/{local_out.name}"
    t2 = time.monotonic()
    last_exc: Exception | None = None
    for attempt in range(3):
        if should_abort():
            return UnitResult(status=-1, message="aborted")
        try:
            rsync(endpoint, local_out, remote_out, upload=False, ssh_key=ssh_key,
                  timeout=timeout, abort_check=should_abort)
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            _append_log(log_path,
                        f"=== unit {unit['seq']} download attempt {attempt + 1}/3 failed: {exc} ===\n")
            time.sleep(3 * (attempt + 1))
    if last_exc is not None:
        return UnitResult(status=1, message=f"download failed after 3 attempts: {last_exc}")
    t_down = time.monotonic() - t2

    if not local_out.is_file() or local_out.stat().st_size == 0:
        return UnitResult(status=1, download_s=t_down,
                          message="downloaded unit is missing or empty")
    return UnitResult(status=0, download_s=t_down)


def run_unit_remote(
    endpoint: tuple[str, int],
    ssh_key: Path,
    *,
    slice_path: Path,
    local_out: Path,
    unit: dict,
    model: str,
    resolution: int,
    batch: int,
    overlap: int,
    log_path: Path,
    should_abort: Callable[[], bool] = lambda: False,
    poll: float = 2.0,
    max_run_s: float = 3600.0,
) -> UnitResult:
    """Restore one unit on the pod at `endpoint`: the three phases run back to
    back on one pod (single-pod/CLI path; the fleet dispatcher pipelines the
    phases itself instead of calling this)."""
    slice_path = Path(slice_path)
    local_out = Path(local_out)

    up = upload_unit_slice(endpoint, ssh_key, slice_path=slice_path, unit=unit,
                           log_path=log_path)
    if up.status != 0:
        return up

    run = restore_unit(endpoint, ssh_key, slice_name=slice_path.name, out_name=local_out.name,
                       unit=unit, model=model, resolution=resolution, batch=batch,
                       overlap=overlap, log_path=log_path, should_abort=should_abort,
                       poll=poll, max_run_s=max_run_s)
    if run.status != 0:
        return UnitResult(status=run.status, upload_s=up.upload_s, restore_s=run.restore_s,
                          message=run.message)

    down = download_unit(endpoint, ssh_key, local_out=local_out, unit=unit,
                         log_path=log_path, should_abort=should_abort)
    if down.status != 0:
        return UnitResult(status=down.status, upload_s=up.upload_s, restore_s=run.restore_s,
                          download_s=down.download_s, message=down.message)

    _append_log(log_path, f"=== unit {unit['seq']} done: up {up.upload_s:.0f}s "
                          f"restore {run.restore_s:.0f}s down {down.download_s:.0f}s ===\n")
    return UnitResult(status=0, upload_s=up.upload_s, restore_s=run.restore_s,
                      download_s=down.download_s)

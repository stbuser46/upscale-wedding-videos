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

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .runpod_api import rsync, seedvr2_unit_argv, ssh_command

REMOTE_SLICES = "/workspace/slices"
REMOTE_UNITS = "/workspace/units"
REMOTE_MODELS = "/opt/models/seedvr2"


@dataclass
class UnitResult:
    status: int            # 0 ok; non-zero = remote failure; -1 = aborted
    upload_s: float = 0.0
    restore_s: float = 0.0
    download_s: float = 0.0
    message: str = ""


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
    timeout: float = 1800,
) -> UnitResult:
    """Push one unit's slice to the pod. Safe to run while the pod's GPU is
    restoring a different unit (rsync-over-ssh does not touch the GPU)."""
    slice_path = Path(slice_path)
    _append_log(log_path, f"\n=== unit {unit['seq']} upload -> {endpoint[0]}:{endpoint[1]} ===\n")
    t0 = time.monotonic()
    try:
        rsync(endpoint, slice_path, f"{REMOTE_SLICES}/{slice_path.name}",
              upload=True, ssh_key=ssh_key, timeout=timeout)
    except Exception as exc:
        return UnitResult(status=1, message=f"upload failed: {exc}")
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

    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        log.write(f"\n=== unit {unit['seq']} restore on {endpoint[0]}:{endpoint[1]} "
                  f"skip={unit['skip']} cap={unit['cap']} prepend={unit['prepend']} "
                  f"drop={unit['drop']} ===\n")
        log.flush()

        t1 = time.monotonic()
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, text=True)
        aborted = False
        timed_out = False

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
            time.sleep(poll)
        t_run = time.monotonic() - t1
        if aborted:
            return UnitResult(status=-1, restore_s=t_run, message="aborted")
        if timed_out:
            log.write(f"=== unit {unit['seq']} TIMED OUT after {t_run:.0f}s (pod likely wedged) ===\n")
            return UnitResult(status=2, restore_s=t_run,
                              message=f"remote restore timed out after {t_run:.0f}s")
        if proc.returncode != 0:
            return UnitResult(status=proc.returncode or 1, restore_s=t_run,
                              message=f"remote restore exited {proc.returncode}")
    return UnitResult(status=0, restore_s=t_run)


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
            rsync(endpoint, local_out, remote_out, upload=False, ssh_key=ssh_key, timeout=timeout)
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

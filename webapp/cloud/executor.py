"""Run one durable unit on a remote pod: push the slice, restore, pull the result.

Stateless and thread-safe — the fleet calls this from one thread per slot, so
many units restore concurrently on different pods. The pinned argv comes from
`seedvr2_unit_argv` (the shared shell builder), so a cloud unit is invoked
byte-for-byte like a local one.
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
    """Restore one unit on the pod at `endpoint`.

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
    slice_path = Path(slice_path)
    local_out = Path(local_out)
    local_out.parent.mkdir(parents=True, exist_ok=True)
    remote_in = f"{REMOTE_SLICES}/{slice_path.name}"
    remote_out = f"{REMOTE_UNITS}/{local_out.name}"

    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        log.write(f"\n=== unit {unit['seq']} on {endpoint[0]}:{endpoint[1]} "
                  f"skip={unit['skip']} cap={unit['cap']} prepend={unit['prepend']} "
                  f"drop={unit['drop']} ===\n")
        log.flush()

        t0 = time.monotonic()
        try:
            rsync(endpoint, slice_path, remote_in, upload=True, ssh_key=ssh_key, timeout=1800)
        except Exception as exc:
            return UnitResult(status=1, message=f"upload failed: {exc}")
        t_up = time.monotonic() - t0

        argv = seedvr2_unit_argv(
            input_path=remote_in, output_path=remote_out, model_dir=REMOTE_MODELS,
            model=model, resolution=resolution, batch=batch, overlap=overlap,
            skip=0, cap=unit["cap"], prepend=unit["prepend"], drop=unit["drop"],
        )
        cmd = ssh_command(endpoint, ["python", "/opt/SeedVR2/inference_cli.py", *argv], ssh_key)

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
            return UnitResult(status=-1, upload_s=t_up, restore_s=t_run, message="aborted")
        if timed_out:
            log.write(f"=== unit {unit['seq']} TIMED OUT after {t_run:.0f}s (pod likely wedged) ===\n")
            return UnitResult(status=2, upload_s=t_up, restore_s=t_run,
                              message=f"remote restore timed out after {t_run:.0f}s")
        if proc.returncode != 0:
            return UnitResult(status=proc.returncode or 1, upload_s=t_up, restore_s=t_run,
                              message=f"remote restore exited {proc.returncode}")

        t2 = time.monotonic()
        try:
            rsync(endpoint, local_out, remote_out, upload=False, ssh_key=ssh_key, timeout=1800)
        except Exception as exc:
            return UnitResult(status=1, upload_s=t_up, restore_s=t_run,
                              message=f"download failed: {exc}")
        t_down = time.monotonic() - t2

        if not local_out.is_file() or local_out.stat().st_size == 0:
            return UnitResult(status=1, upload_s=t_up, restore_s=t_run, download_s=t_down,
                              message="downloaded unit is missing or empty")
        log.write(f"=== unit {unit['seq']} done: up {t_up:.0f}s restore {t_run:.0f}s "
                  f"down {t_down:.0f}s ===\n")
        return UnitResult(status=0, upload_s=t_up, restore_s=t_run, download_s=t_down)

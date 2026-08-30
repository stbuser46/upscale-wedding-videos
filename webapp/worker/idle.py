"""GPU idle detection for unattended restoration.

Passive detection only: we never reconfigure or kill another GPU user (e.g. the
torrent stack's ollama). We simply observe the GPU and decide whether it is free
enough to start restoration. When a foreign workload reappears, the worker
yields (handled by the pause/yield path). Thresholds and dwell times are all
configurable via the environment.

The key signal is free VRAM plus the absence of a *foreign* compute process:
SeedVR2 peaks ~60 GiB, and an idle ollama can hold ~56 GiB while reporting 0%
GPU utilization, so utilization alone is unsafe — free memory is what matters.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess
import time


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class IdlePolicy:
    # SeedVR2 needs ~60 GiB; require comfortably more than that free to start.
    min_free_mib: int = 70 * 1024
    # Continuous idle required before starting or auto-resuming (seconds).
    start_dwell_s: float = 600.0
    # After a busy period, wait this long of idle before resuming (seconds).
    resume_dwell_s: float = 300.0

    @classmethod
    def from_env(cls) -> "IdlePolicy":
        return cls(
            min_free_mib=_int_env("WEDDING_IDLE_MIN_FREE_MIB", 70 * 1024),
            start_dwell_s=_float_env("WEDDING_IDLE_START_DWELL_S", 600.0),
            resume_dwell_s=_float_env("WEDDING_IDLE_RESUME_DWELL_S", 300.0),
        )


@dataclass(frozen=True)
class GpuSnapshot:
    ok: bool                     # did nvidia-smi succeed
    free_mib: int
    total_mib: int
    utilization: int
    compute_pids: tuple[int, ...]
    compute_names: tuple[str, ...]


def gpu_snapshot() -> GpuSnapshot:
    """One reading of GPU memory + compute processes via nvidia-smi."""
    try:
        mem = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        free, total, util = (int(p.strip()) for p in mem.stdout.splitlines()[0].split(","))
        apps = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,process_name",
             "--format=csv,noheader"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        pids: list[int] = []
        names: list[str] = []
        for line in apps.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            pid_str, _, name = line.partition(",")
            try:
                pids.append(int(pid_str.strip()))
            except ValueError:
                continue
            names.append(name.strip())
        return GpuSnapshot(True, free, total, util, tuple(pids), tuple(names))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return GpuSnapshot(False, 0, 0, 0, (), ())


def evaluate_idle(policy: IdlePolicy, *, own_pids: frozenset[int] = frozenset()) -> tuple[bool, str]:
    """Is the GPU free enough to start restoration right now?

    own_pids are compute PIDs belonging to our own restoration (excluded from the
    foreign-process check) — used while a job runs to distinguish our container.
    Returns (idle, human_readable_reason).
    """
    snap = gpu_snapshot()
    if not snap.ok:
        return False, "waiting: GPU status unavailable"
    foreign = [
        (pid, name) for pid, name in zip(snap.compute_pids, snap.compute_names)
        if pid not in own_pids
    ]
    if foreign:
        pid, name = foreign[0]
        extra = "" if len(foreign) == 1 else f" (+{len(foreign) - 1} more)"
        return False, f"waiting: foreign GPU process {name} pid {pid}{extra}"
    if snap.free_mib < policy.min_free_mib:
        return False, f"waiting: only {snap.free_mib // 1024} GiB VRAM free, need {policy.min_free_mib // 1024} GiB"
    return True, f"idle: {snap.free_mib // 1024} GiB free, no foreign GPU process"


class IdleGate:
    """Applies hysteresis to evaluate_idle: the GPU must read idle continuously
    for the dwell time before the gate opens, so we never flap on a threshold."""

    def __init__(self, policy: IdlePolicy, *, clock=time.monotonic):
        self.policy = policy
        self._clock = clock
        self._idle_since: float | None = None
        self.last_reason: str = "starting"

    def poll(self, *, own_pids: frozenset[int] = frozenset(), dwell_s: float | None = None) -> bool:
        """Sample once; return True only after the GPU has been continuously idle
        for the dwell period. Any non-idle sample resets the timer."""
        idle, reason = evaluate_idle(self.policy, own_pids=own_pids)
        self.last_reason = reason
        now = self._clock()
        if not idle:
            self._idle_since = None
            return False
        if self._idle_since is None:
            self._idle_since = now
        want = self.policy.start_dwell_s if dwell_s is None else dwell_s
        held = now - self._idle_since
        if held < want:
            self.last_reason = f"idle {int(held)}s/{int(want)}s before start"
            return False
        return True

    def reset(self) -> None:
        self._idle_since = None

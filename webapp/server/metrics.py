"""Lightweight GPU/CPU sampler recorded for the queue page load graph.

One sample every SAMPLE_INTERVAL_SECONDS: a single nvidia-smi query plus
/proc reads. Rows older than RETENTION_HOURS are pruned periodically, so the
table stays bounded (~50k rows) and adds no meaningful load next to a
restoration job.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import subprocess
import threading

from webapp.config.settings import Settings
from webapp.db.database import connect, utc_now

SAMPLE_INTERVAL_SECONDS = 5.0
RETENTION_HOURS = 72
_PRUNE_EVERY_SAMPLES = 720  # roughly hourly

_start_lock = threading.Lock()
_started = False


def _read_cpu_counters() -> tuple[int, int]:
    with open("/proc/stat", encoding="ascii") as stat:
        fields = [int(part) for part in stat.readline().split()[1:]]
    idle = fields[3] + fields[4]  # idle + iowait
    return idle, sum(fields)


def _read_mem_pct() -> float:
    values: dict[str, int] = {}
    with open("/proc/meminfo", encoding="ascii") as meminfo:
        for line in meminfo:
            key, _, rest = line.partition(":")
            values[key] = int(rest.split()[0])
            if "MemTotal" in values and "MemAvailable" in values:
                break
    total = values.get("MemTotal", 0)
    if not total:
        return 0.0
    return round(100.0 * (total - values.get("MemAvailable", 0)) / total, 1)


def _read_gpu() -> tuple[float, int, float] | None:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,power.draw",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4, check=True,
        )
        util, mem, power = (part.strip() for part in result.stdout.splitlines()[0].split(","))
        return float(util), int(float(mem)), float(power)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def _sample_loop(settings: Settings, stop: threading.Event) -> None:
    last_idle, last_total = _read_cpu_counters()
    samples_since_prune = _PRUNE_EVERY_SAMPLES  # prune once at startup
    while not stop.wait(SAMPLE_INTERVAL_SECONDS):
        idle, total = _read_cpu_counters()
        delta_total = total - last_total
        cpu_pct = round(100.0 * (1 - (idle - last_idle) / delta_total), 1) if delta_total > 0 else 0.0
        last_idle, last_total = idle, total
        gpu = _read_gpu()
        try:
            with connect(settings.database_path) as db:
                db.execute(
                    "INSERT INTO system_metrics (ts, cpu_pct, mem_pct, gpu_pct, gpu_mem_mib, gpu_power_w) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (utc_now(), cpu_pct, _read_mem_pct(),
                     gpu[0] if gpu else None, gpu[1] if gpu else None, gpu[2] if gpu else None),
                )
                samples_since_prune += 1
                if samples_since_prune >= _PRUNE_EVERY_SAMPLES:
                    samples_since_prune = 0
                    cutoff = (datetime.now(UTC) - timedelta(hours=RETENTION_HOURS)).isoformat(
                        timespec="milliseconds"
                    )
                    db.execute("DELETE FROM system_metrics WHERE ts < ?", (cutoff,))
        except Exception:
            # Never let a transient DB error (e.g. migration in flight) kill
            # the sampler; the next tick simply tries again.
            pass


def start_sampler(settings: Settings) -> None:
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    thread = threading.Thread(
        target=_sample_loop, args=(settings, threading.Event()),
        name="system-metrics-sampler", daemon=True,
    )
    thread.start()

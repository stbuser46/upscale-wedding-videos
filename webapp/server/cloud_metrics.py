"""Live RunPod telemetry sampler backing the Cloud fleet panel's charts.

Mirrors the pattern in ``webapp/server/metrics.py`` (the local GPU/CPU
sampler): a single background thread samples on a fixed cadence and keeps a
bounded in-memory ring buffer per pod, so any number of browser polls against
``/api/cloud/pod-metrics`` are served instantly from cache rather than
fanning out to the RunPod API.

Telemetry comes **only** from the RunPod GraphQL control-plane
(``myself.pods.runtime``) via ``webapp.cloud.runpod_api.RunpodClient`` — the
same auth/query pattern the cloud fan-out already uses. This module imports
that client but never edits it, and never opens an SSH connection to a pod
(RunPod's own aggressive-probing ban earlier today makes that a hard no for
server-side code).

The sampler only calls RunPod at all while ``cloud_pods`` has a
non-terminated row, so local-only operation (the default) never talks to
RunPod and never requires an API key to be configured.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from collections import deque
import json
import threading
from pathlib import Path

from webapp.cloud.runpod_api import POD_NAME_PREFIX, RunpodClient
from webapp.config.settings import Settings
from webapp.db.database import connect, utc_now

SAMPLE_INTERVAL_SECONDS = 12.0
RETENTION_SECONDS = 2 * 3600  # ring buffer horizon; gunicorn is single-worker
MAX_SAMPLES_PER_POD = int(RETENTION_SECONDS / SAMPLE_INTERVAL_SECONDS) + 5
PERSIST_EVERY_SAMPLES = 5  # flush to disk roughly once a minute
PERSIST_FILENAME = "cloud_pod_metrics.json"

# One call fetches runtime for every pod on the account in a single round
# trip, so the sampler cadence is independent of fleet size.
_RUNTIME_QUERY = """
query {
  myself {
    pods {
      id
      name
      desiredStatus
      costPerHr
      runtime {
        uptimeInSeconds
        gpus { gpuUtilPercent memoryUtilPercent }
        container { cpuPercent memoryPercent }
      }
    }
  }
}
"""

_lock = threading.Lock()
_series: dict[str, deque] = {}  # pod_id -> deque of {"ts", gpu_pct, gpu_mem_pct, cpu_pct, mem_pct}
_names: dict[str, str] = {}  # pod_id -> last known pod name (survives it dropping out of RunPod's list)
_last_sample_at: str | None = None
_last_error: str | None = None
_start_lock = threading.Lock()
_started = False


def _persist_path(settings: Settings) -> Path:
    return settings.data_dir / PERSIST_FILENAME


def _load_persisted(settings: Settings) -> None:
    """Seed the ring buffers from disk so a gunicorn HUP reload doesn't blank
    the charts. Best-effort: any read/parse failure just starts empty."""
    global _last_sample_at
    try:
        raw = json.loads(_persist_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    cutoff = (datetime.now(UTC) - timedelta(seconds=RETENTION_SECONDS)).isoformat(timespec="milliseconds")
    with _lock:
        for pod_id, entry in (raw.get("pods") or {}).items():
            points = [p for p in entry.get("series", []) if p.get("ts", "") >= cutoff]
            if points:
                _series[pod_id] = deque(points, maxlen=MAX_SAMPLES_PER_POD)
                if entry.get("name"):
                    _names[pod_id] = entry["name"]
        # So a freshly reloaded worker reports "stale since <persist time>"
        # rather than "never sampled" until its own first live poll lands.
        if raw.get("saved_at") and _series:
            _last_sample_at = raw["saved_at"]


def _persist(settings: Settings) -> None:
    with _lock:
        payload = {
            "saved_at": utc_now(),
            "pods": {
                pod_id: {"name": _names.get(pod_id), "series": list(points)}
                for pod_id, points in _series.items()
            },
        }
    path = _persist_path(settings)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # best-effort; the in-memory ring buffer stays authoritative


def _any_cloud_pods_active(settings: Settings) -> bool:
    try:
        with connect(settings.database_path) as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cloud_pods'"
            ).fetchone()
            if table is None:
                return False
            row = db.execute("SELECT 1 FROM cloud_pods WHERE state != 'terminated' LIMIT 1").fetchone()
            return row is not None
    except Exception:
        return False


def _sample_once() -> None:
    global _last_sample_at, _last_error
    try:
        client = RunpodClient()
        data = client._graphql(_RUNTIME_QUERY)
    except Exception as exc:
        # RunpodError (bad key, HTTP failure, ...) or anything else transient —
        # never let a failed poll kill the sampler thread.
        with _lock:
            _last_error = str(exc)
        return

    ts = utc_now()
    pods = ((data or {}).get("myself") or {}).get("pods") or []
    with _lock:
        for pod in pods:
            name = pod.get("name") or ""
            pod_id = pod.get("id")
            if not pod_id or not name.startswith(POD_NAME_PREFIX):
                continue  # not one of ours (or an unnamed/stray entry)
            runtime = pod.get("runtime") or {}
            gpus = runtime.get("gpus") or []
            gpu_pct = round(sum(g.get("gpuUtilPercent") or 0 for g in gpus) / len(gpus), 1) if gpus else None
            gpu_mem_pct = round(sum(g.get("memoryUtilPercent") or 0 for g in gpus) / len(gpus), 1) if gpus else None
            container = runtime.get("container") or {}
            sample = {
                "ts": ts,
                "gpu_pct": gpu_pct,
                "gpu_mem_pct": gpu_mem_pct,
                "cpu_pct": container.get("cpuPercent"),
                "mem_pct": container.get("memoryPercent"),
            }
            _names[pod_id] = name
            _series.setdefault(pod_id, deque(maxlen=MAX_SAMPLES_PER_POD)).append(sample)
        _last_error = None
        _last_sample_at = ts


def _sample_loop(settings: Settings, stop: threading.Event) -> None:
    _load_persisted(settings)
    since_persist = 0
    while not stop.wait(SAMPLE_INTERVAL_SECONDS):
        if _any_cloud_pods_active(settings):
            _sample_once()
            since_persist += 1
            if since_persist >= PERSIST_EVERY_SAMPLES:
                since_persist = 0
                _persist(settings)


def start_cloud_metrics_sampler(settings: Settings) -> None:
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    thread = threading.Thread(
        target=_sample_loop, args=(settings, threading.Event()),
        name="cloud-pod-metrics-sampler", daemon=True,
    )
    thread.start()


def snapshot(minutes: float) -> dict:
    """Cached per-pod series for the last ``minutes``, plus staleness info.

    Never touches the network — always instant, always served from whatever
    the background thread last collected (which may be nothing yet, or
    stale if the last RunPod call failed).
    """
    cutoff = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="milliseconds")
    with _lock:
        pods = {
            pod_id: {"name": _names.get(pod_id), "series": [p for p in points if p["ts"] >= cutoff]}
            for pod_id, points in _series.items()
        }
        last_sample_at = _last_sample_at
        last_error = _last_error

    stale = True
    if last_sample_at:
        try:
            parsed = datetime.fromisoformat(last_sample_at)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            stale = (datetime.now(UTC) - parsed).total_seconds() > SAMPLE_INTERVAL_SECONDS * 3
        except ValueError:
            stale = True
    return {"pods": pods, "last_sample_at": last_sample_at, "stale": stale, "last_error": last_error}

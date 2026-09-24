"""Cloud fleet view — a live read-only summary of the RunPod GPU pods.

Backs the "Cloud fleet" panel on the queue page. The pod records are written
by the cloud fan-out worker (``webapp/cloud``); this module only *reads* the
``cloud_pods`` table (LEFT JOINing ``jobs`` for the human-facing public id) and
derives uptime/cost from the ISO8601 timestamps in Python so the frontend can
stay a dumb renderer. It also reads ``job_chunks`` (per-unit state for the
active cloud job) and ``worker_status`` (the worker's own heartbeat text) to
surface a live per-unit progress strip and a provisioning hint tailed from the
pod's own provisioning log — all data the worker already writes for its own
purposes, none of it requiring a worker-side change.

If the ``cloud_pods`` table has not been created yet (an older database that
predates the cloud migration) the endpoint degrades gracefully to
``{"enabled": false, ...}`` rather than 500ing, so local-only operation looks
unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re

from flask import Blueprint, abort, current_app, jsonify, request

from webapp.db import connect
from webapp.server import cloud_metrics
from webapp.server.logtail import unit_pod_hints


cloud = Blueprint("cloud", __name__, url_prefix="/api/cloud")

# A pod BILLS from the moment it is created until it is confirmed terminated —
# provisioning (apt/pip/7.3 GB weight pull, ~15 min) and an unconfirmed teardown
# ('terminating' with no terminated_at) both cost money. Counting only
# ready/running showed $0/h through the costliest warm-up window and hid a pod a
# failed teardown left billing. So "billing" is simply: not confirmed dead.
_BILLING_EXCLUDED_STATES = {"terminated"}


def _settings():
    return current_app.config["WEBAPP_SETTINGS"]


def _max_slots() -> int:
    try:
        return int(os.environ.get("WEDDING_CLOUD_MAX_SLOTS", 16))
    except (TypeError, ValueError):
        return 16


def _spend_cap() -> float:
    try:
        return float(os.environ.get("WEDDING_CLOUD_SPEND_CAP_USD", 250))
    except (TypeError, ValueError):
        return 250.0


def _parse_iso(value) -> datetime | None:
    """Parse an ISO8601 timestamp, treating 'Z' and naive stamps as UTC."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _cost_and_lifetime(hourly_rate: float | None, created_at, terminated_at, now: datetime) -> tuple[float, float]:
    """(cost_usd, lifetime_s) for a pod row — rate x wall-clock lifetime, still
    running (no terminated_at) counted through `now`. Shared by the fleet
    summary and the pod-metrics endpoint so their $ figures can never drift
    apart."""
    created = _parse_iso(created_at)
    ended = _parse_iso(terminated_at) or now
    lifetime_s = max(0.0, (ended - created).total_seconds()) if created else 0.0
    return (hourly_rate or 0.0) * lifetime_s / 3600.0, lifetime_s


def _disabled_payload() -> dict:
    return {
        "enabled": False,
        "summary": {
            "active": 0,
            "max_slots": _max_slots(),
            "rate_per_hr": 0.0,
            "spend_usd": 0.0,
            "spend_cap_usd": _spend_cap(),
        },
        "pods": [],
        "active_job": None,
    }


# Matches the denominator of a "unit 3/5" or "4/5 units done" heartbeat detail
# string (see webapp/worker/runner.py _heartbeat callers) — both phrasings put
# the total unit count after the final slash, so we don't need to distinguish
# them to recover it.
_UNIT_TOTAL_RE = re.compile(r"(\d+)\s*/\s*(\d+)")

# A tqdm progress line looks like "<file>:  73%|████ | 4.6G/6.3G [...]"; pods
# stream provisioning output (apt, pip, weight download) to a per-pod log with
# both bare '\r' (tqdm redraw) and '\n'/'\r\n' (real log lines) as separators.
_TQDM_RE = re.compile(r"([^\s/\\:]+):\s*(\d{1,3})%\|")


def _unit_total_from_detail(detail: str | None) -> int | None:
    if not detail:
        return None
    matches = _UNIT_TOTAL_RE.findall(detail)
    if not matches:
        return None
    return int(matches[-1][1])


def _current_unit_hints(db, job_id: int | None) -> dict[tuple[str, int], int]:
    """(ssh_host, ssh_port) -> currently-running unit sequence, for the given
    job. Reuses the same log-tail parsing ``/api/jobs/<id>/live`` already
    relies on (``webapp.server.logtail.unit_pod_hints``) rather than
    re-deriving pod/unit attribution here — this is the sole place both the
    fleet summary and the pod-metrics tile row read it from, so they can
    never disagree on which pod is doing which unit."""
    if job_id is None:
        return {}
    job_row = db.execute("SELECT log_path FROM jobs WHERE id=?", (job_id,)).fetchone()
    if job_row is None or not job_row["log_path"]:
        return {}
    running = {
        row["sequence"]
        for row in db.execute(
            "SELECT sequence FROM job_chunks WHERE job_id=? AND state='running'", (job_id,)
        ).fetchall()
    }
    if not running:
        return {}
    hints = unit_pod_hints(Path(job_row["log_path"]))
    return {hostport: seq for seq, hostport in hints.items() if seq in running}


def _tail_text(path, size: int = 200) -> str | None:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            length = fh.tell()
            fh.seek(max(0, length - size))
            data = fh.read()
    except OSError:
        return None
    return data.decode("utf-8", errors="replace")


def _provisioning_hint(data_dir, job_public_id: str | None, pod_name: str | None) -> str | None:
    """Best-effort progress hint tailed from provision-<job>-<index>.log.

    Never raises: a missing/unreadable/unparsable log just means no hint,
    which is exactly what a pod that hasn't started logging yet looks like."""
    if not job_public_id or not pod_name:
        return None
    try:
        index = int(pod_name.rsplit("-", 1)[-1])
    except (ValueError, AttributeError):
        return None
    text = _tail_text(data_dir / "logs" / f"provision-{job_public_id}-{index}.log")
    if not text:
        return None
    # tqdm redraws a bar with a bare '\r' and no trailing '\n'; real log lines
    # (apt, our own step markers) end in '\n' (sometimes '\r\n' over the ssh
    # pty). Splitting on either and keeping the last non-blank segment gets the
    # most recent line either way.
    segments = [s.strip() for s in re.split(r"[\r\n]+", text) if s.strip()]
    if not segments:
        return None
    last = segments[-1]
    match = _TQDM_RE.search(last)
    if match:
        name, pct = match.groups()
        return f"downloading {name} {pct}%"
    return last[:80]


@cloud.get("/fleet")
def fleet():
    settings = _settings()
    now = datetime.now(timezone.utc)
    with connect(settings.database_path) as db:
        table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cloud_pods'"
        ).fetchone()
        if table is None:
            # Older database without the cloud migration applied yet.
            return jsonify(_disabled_payload())
        rows = db.execute(
            """SELECT p.pod_id, p.name, p.gpu_type, p.state, p.hourly_rate,
                      p.chunk_sequence, p.error, p.job_id, p.ssh_host, p.ssh_port,
                      p.created_at, p.ready_at, p.terminated_at,
                      j.public_id AS job_public_id
               FROM cloud_pods p
               LEFT JOIN jobs j ON j.id = p.job_id"""
        ).fetchall()
        worker_row = db.execute(
            "SELECT activity, detail, active_job_id, last_beat_at FROM worker_status WHERE id=1"
        ).fetchone()

    pods: list[dict] = []
    active = 0
    rate_per_hr = 0.0
    spend_usd = 0.0
    enabled = False
    # The job whose pods are currently billing is "the" active cloud job —
    # there is at most one, since the worker runs one job at a time.
    active_job_id: int | None = None
    active_job_public_id: str | None = None
    active_job_spend = 0.0

    for row in rows:
        state = row["state"]
        is_billing = state not in _BILLING_EXCLUDED_STATES
        if is_billing:
            enabled = True
            if row["job_id"] is not None:
                active_job_id = row["job_id"]
                active_job_public_id = row["job_public_id"]

        rate = row["hourly_rate"]
        created = _parse_iso(row["created_at"])
        cost, lifetime_s = _cost_and_lifetime(rate, row["created_at"], row["terminated_at"], now)
        spend_usd += cost

        if is_billing:
            active += 1
            rate_per_hr += rate or 0.0

        provisioning_hint = None
        if state == "creating" and row["ready_at"] is None:
            provisioning_hint = _provisioning_hint(settings.data_dir, row["job_public_id"], row["name"])

        pods.append({
            "pod_id": row["pod_id"],
            "name": row["name"],
            "gpu": row["gpu_type"],
            "state": state,
            "job_public_id": row["job_public_id"],
            "unit": row["chunk_sequence"],
            "rate_per_hr": rate,
            "ssh_host": row["ssh_host"],
            "ssh_port": row["ssh_port"],
            "created_at": row["created_at"],
            "ready_at": row["ready_at"],
            "terminated_at": row["terminated_at"],
            "uptime_s": int(round(lifetime_s)),
            "cost_usd": round(cost, 2),
            "provisioning_hint": provisioning_hint,
            "error": row["error"],
            # Sort/accounting helpers (stripped before serialisation).
            "_active": is_billing,
            "_created": created.timestamp() if created else 0.0,
            "_job_id": row["job_id"],
            "_cost": cost,
        })

    if active_job_id is not None:
        active_job_spend = sum(p["_cost"] for p in pods if p["_job_id"] == active_job_id)

    # Billing pods first, then most-recently-created first.
    pods.sort(key=lambda pod: (0 if pod["_active"] else 1, -pod["_created"]))
    for pod in pods:
        del pod["_active"]
        del pod["_created"]
        del pod["_job_id"]
        del pod["_cost"]

    active_job = None
    if active_job_id is not None:
        with connect(settings.database_path) as db:
            chunk_rows = db.execute(
                "SELECT sequence, state, frame_count FROM job_chunks WHERE job_id=? ORDER BY sequence",
                (active_job_id,),
            ).fetchall()
            unit_hints = _current_unit_hints(db, active_job_id)
        chunks_by_seq = {r["sequence"]: {"state": r["state"], "frame_count": r["frame_count"]} for r in chunk_rows}

        # cloud_pods.chunk_sequence (the "unit" field above) is never written
        # by the fan-out worker, so it always reads NULL — overlay the real,
        # currently-running unit recovered from the log tail instead.
        for pod in pods:
            hostport = (pod["ssh_host"], pod["ssh_port"])
            if hostport in unit_hints:
                pod["unit"] = unit_hints[hostport]

        worker_activity = None
        worker_detail = None
        units_total = None
        if worker_row is not None:
            worker_activity = worker_row["activity"]
            worker_detail = worker_row["detail"]
            # Only trust the heartbeat's embedded unit count if it is actually
            # talking about this job — the worker could have already moved on.
            if worker_row["active_job_id"] == active_job_id:
                units_total = _unit_total_from_detail(worker_detail)
        if units_total is None and chunks_by_seq:
            units_total = max(chunks_by_seq) + 1

        units = []
        counts = {"valid": 0, "running": 0, "invalid": 0, "pending": 0}
        span = range(units_total) if units_total is not None else sorted(chunks_by_seq)
        for seq in span:
            info = chunks_by_seq.get(seq)
            state = info["state"] if info else "pending"
            counts[state] = counts.get(state, 0) + 1
            units.append({
                "sequence": seq,
                "state": state,
                "frame_count": info["frame_count"] if info else None,
            })

        active_job = {
            "public_id": active_job_public_id,
            "spend_usd": round(active_job_spend, 2),
            "worker_activity": worker_activity,
            "worker_detail": worker_detail,
            "units_total": units_total,
            "units": units,
            "units_summary": counts,
        }

    return jsonify({
        "enabled": enabled,
        "summary": {
            "active": active,
            "max_slots": _max_slots(),
            "rate_per_hr": round(rate_per_hr, 2),
            "spend_usd": round(spend_usd, 2),
            "spend_cap_usd": _spend_cap(),
        },
        "pods": pods,
        "active_job": active_job,
    })


@cloud.get("/pod-metrics")
def pod_metrics():
    """Live per-pod telemetry (GPU/CPU/memory util + accrued $) for the Cloud
    fleet panel's charts.

    Telemetry itself comes from `cloud_metrics`'s cached sampler (RunPod
    GraphQL control-plane only — never SSH — polled on its own ~12s cadence
    regardless of how often this endpoint is hit). Cost/rate/state come from
    the same `cloud_pods` ledger and `_cost_and_lifetime` helper the fleet
    summary above uses, so the two panels can never disagree on $.
    """
    settings = _settings()
    try:
        minutes = min(180.0, max(1.0, float(request.args.get("minutes", 60))))
    except ValueError:
        abort(400, description="minutes must be a number")

    snap = cloud_metrics.snapshot(minutes)
    now = datetime.now(timezone.utc)

    with connect(settings.database_path) as db:
        table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cloud_pods'"
        ).fetchone()
        rows = []
        if table is not None:
            rows = db.execute(
                "SELECT pod_id, name, state, hourly_rate, created_at, terminated_at, "
                "job_id, ssh_host, ssh_port FROM cloud_pods WHERE pod_id IS NOT NULL"
            ).fetchall()
        # "The" active job is whichever one currently owns a billing pod (at
        # most one — the worker runs one job at a time). Its running-unit
        # hints let each pod tile show which unit it is on right now.
        active_job_id = next(
            (r["job_id"] for r in rows if r["job_id"] is not None and r["state"] not in _BILLING_EXCLUDED_STATES),
            None,
        )
        unit_hints = _current_unit_hints(db, active_job_id)
    by_pod = {row["pod_id"]: row for row in rows}

    pods = []
    fleet_cost = 0.0
    fleet_rate = 0.0
    # The headline must not lump "restoring at 100%" in with pods that are
    # still provisioning (state=creating, GPU legitimately idle) or already
    # torn down (state=terminated, but still inside the sampler's retention
    # window / this request's minutes= lookback). Only READY pods — the ones
    # actually running a unit right now — count toward the honest average.
    ready_gpu: list[float] = []
    ready_cpu: list[float] = []
    ready_count = 0
    full_gpu_count = 0  # ready pods currently at >=95% GPU
    creating_count = 0
    # Union of pods the sampler has telemetry for and pods the ledger knows
    # about — a pod can lead or lag the other source by one sampler tick
    # around create/terminate, and both are worth showing while they exist.
    for pod_id in set(snap["pods"]) | set(by_pod):
        db_row = by_pod.get(pod_id)
        telemetry = snap["pods"].get(pod_id, {})
        name = telemetry.get("name") or (db_row["name"] if db_row else None) or pod_id
        suffix = name.rsplit("-", 1)[-1]
        label = f"-{suffix}" if suffix.isdigit() else f"-{pod_id[-4:]}"

        rate = db_row["hourly_rate"] if db_row else None
        created_at = db_row["created_at"] if db_row else None
        terminated_at = db_row["terminated_at"] if db_row else None
        cost, _ = _cost_and_lifetime(rate, created_at, terminated_at, now)
        state = db_row["state"] if db_row else "unknown"
        billing = db_row is not None and state not in _BILLING_EXCLUDED_STATES
        ssh_host = db_row["ssh_host"] if db_row else None
        ssh_port = db_row["ssh_port"] if db_row else None
        current_unit = unit_hints.get((ssh_host, ssh_port)) if ssh_host is not None else None

        created = _parse_iso(created_at)
        series = []
        for point in telemetry.get("series", []):
            point_cost = None
            if rate is not None and created is not None:
                ts = _parse_iso(point["ts"])
                if ts is not None:
                    point_cost = round(rate * max(0.0, (ts - created).total_seconds()) / 3600.0, 4)
            series.append({**point, "cost_usd": point_cost})

        fleet_cost += cost
        if billing:
            fleet_rate += rate or 0.0
        if billing and state == "creating":
            creating_count += 1
        if billing and state == "ready":
            ready_count += 1
            if series:
                last = series[-1]
                if last.get("gpu_pct") is not None:
                    ready_gpu.append(last["gpu_pct"])
                    if last["gpu_pct"] >= 95:
                        full_gpu_count += 1
                if last.get("cpu_pct") is not None:
                    ready_cpu.append(last["cpu_pct"])

        pods.append({
            "pod_id": pod_id,
            "name": name,
            "label": label,
            "state": state,
            "rate_per_hr": rate,
            "cost_usd": round(cost, 2),
            "current_unit": current_unit,
            "series": series,
        })

    pods.sort(key=lambda p: p["label"])

    last_sample_age_s = None
    if snap["last_sample_at"]:
        parsed = _parse_iso(snap["last_sample_at"])
        if parsed is not None:
            last_sample_age_s = round((now - parsed).total_seconds(), 1)
    return jsonify({
        "enabled": bool(pods),
        "stale": snap["stale"],
        "last_sample_at": snap["last_sample_at"],
        "last_sample_age_s": last_sample_age_s,
        "last_error": snap["last_error"],
        "sample_interval_s": cloud_metrics.SAMPLE_INTERVAL_SECONDS,
        "fleet": {
            "cost_usd": round(fleet_cost, 2),
            "rate_per_hr": round(fleet_rate, 2),
            # Honest headline: averaged over READY pods only (excludes
            # provisioning pods legitimately idle at 0% and terminated pods
            # still visible in the lookback window) — see the ready_gpu/
            # ready_cpu comment above for why the naive all-pod average lied.
            "avg_gpu_pct": round(sum(ready_gpu) / len(ready_gpu), 1) if ready_gpu else None,
            "avg_cpu_pct": round(sum(ready_cpu) / len(ready_cpu), 1) if ready_cpu else None,
            "ready_count": ready_count,
            "full_gpu_count": full_gpu_count,
            "creating_count": creating_count,
        },
        "pods": pods,
    })

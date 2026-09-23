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
import re

from flask import Blueprint, current_app, jsonify

from webapp.db import connect


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

        created = _parse_iso(row["created_at"])
        ended = _parse_iso(row["terminated_at"]) or now
        lifetime_s = max(0.0, (ended - created).total_seconds()) if created else 0.0

        rate = row["hourly_rate"]
        cost = (rate or 0.0) * lifetime_s / 3600.0
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
        chunks_by_seq = {r["sequence"]: {"state": r["state"], "frame_count": r["frame_count"]} for r in chunk_rows}

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

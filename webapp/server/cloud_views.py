"""Cloud fleet view — a live read-only summary of the RunPod GPU pods.

Backs the "Cloud fleet" panel on the queue page. The pod records are written
by the cloud fan-out worker (``webapp/cloud``); this module only *reads* the
``cloud_pods`` table (LEFT JOINing ``jobs`` for the human-facing public id) and
derives uptime/cost from the ISO8601 timestamps in Python so the frontend can
stay a dumb renderer.

If the ``cloud_pods`` table has not been created yet (an older database that
predates the cloud migration) the endpoint degrades gracefully to
``{"enabled": false, ...}`` rather than 500ing, so local-only operation looks
unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os

from flask import Blueprint, current_app, jsonify

from webapp.db import connect


cloud = Blueprint("cloud", __name__, url_prefix="/api/cloud")

# A pod is "active" (billable and doing useful work) while it is provisioned and
# either warming up its model or actually restoring a unit.
_ACTIVE_STATES = {"ready", "running"}


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
    }


@cloud.get("/fleet")
def fleet():
    now = datetime.now(timezone.utc)
    with connect(_settings().database_path) as db:
        table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cloud_pods'"
        ).fetchone()
        if table is None:
            # Older database without the cloud migration applied yet.
            return jsonify(_disabled_payload())
        rows = db.execute(
            """SELECT p.pod_id, p.name, p.gpu_type, p.state, p.hourly_rate,
                      p.chunk_sequence, p.error, p.created_at, p.terminated_at,
                      j.public_id AS job_public_id
               FROM cloud_pods p
               LEFT JOIN jobs j ON j.id = p.job_id"""
        ).fetchall()

    pods: list[dict] = []
    active = 0
    rate_per_hr = 0.0
    spend_usd = 0.0
    enabled = False

    for row in rows:
        state = row["state"]
        if state != "terminated":
            enabled = True
        is_active = state in _ACTIVE_STATES

        created = _parse_iso(row["created_at"])
        ended = _parse_iso(row["terminated_at"]) or now
        lifetime_s = max(0.0, (ended - created).total_seconds()) if created else 0.0

        rate = row["hourly_rate"]
        cost = (rate or 0.0) * lifetime_s / 3600.0
        spend_usd += cost

        if is_active:
            active += 1
            rate_per_hr += rate or 0.0

        pods.append({
            "pod_id": row["pod_id"],
            "name": row["name"],
            "gpu": row["gpu_type"],
            "state": state,
            "job_public_id": row["job_public_id"],
            "unit": row["chunk_sequence"],
            "rate_per_hr": rate,
            "uptime_s": int(round(lifetime_s)),
            "cost_usd": round(cost, 2),
            "error": row["error"],
            # Sort helpers (stripped before serialisation).
            "_active": is_active,
            "_created": created.timestamp() if created else 0.0,
        })

    # Active pods first, then most-recently-created first.
    pods.sort(key=lambda pod: (0 if pod["_active"] else 1, -pod["_created"]))
    for pod in pods:
        del pod["_active"]
        del pod["_created"]

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
    })

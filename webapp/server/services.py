from __future__ import annotations

import json
import sqlite3
from typing import Any

from webapp.db import utc_now


def append_event(
    db: sqlite3.Connection,
    job_id: int,
    event_type: str,
    *,
    state: str | None = None,
    stage: str | None = None,
    message: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    cursor = db.execute(
        """INSERT INTO job_events
           (job_id, event_type, state, stage, message, payload_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (job_id, event_type, state, stage, message, json.dumps(payload or {}, sort_keys=True), utc_now()),
    )
    return int(cursor.lastrowid)


def event_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["payload"] = json.loads(result.pop("payload_json"))
    return result


def job_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["settings"] = json.loads(result.pop("settings_json"))
    duration_ms = result["source_end_ms"] - result["source_start_ms"]
    result["duration_ms"] = duration_ms
    result["progress_percent"] = (
        round(result["frames_done"] * 100 / result["frames_total"], 1)
        if result["frames_total"]
        else 0
    )
    result["can_start"] = result["state"] == "queued" and not result["start_requested"]
    result["can_cancel"] = result["state"] in {
        "queued", "preparing", "running", "assembling", "cancel_requested"
    }
    result["can_retry"] = result["state"] in {"failed", "cancelled", "interrupted"}
    result["pause_available"] = False
    return result

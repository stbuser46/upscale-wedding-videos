from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any
import uuid

from webapp.db import utc_now


PRIORITY_NAMES = {"high": 100, "normal": 50, "low": 10}
# States that keep a job "live" for the per-target unique index and for the
# reconciler's dedup — anything not in this set means the target is free again.
TERMINAL_STATES = ("completed", "failed", "cancelled")


class QueueError(Exception):
    """Raised by the shared enqueue path; `code` maps to an HTTP status in the
    API layer and is inspected by the reconciler (which never uses Flask)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def deinterlace_profile(video_standard: str | None, field_order: str | None) -> dict[str, Any]:
    """Map a disc's broadcast standard + field order to the stage-1 deinterlace
    parameters (pipeline_v3.sh DEINT_* env) and the true progressive output fps.
    Defaults reproduce the original PAL/BFF behaviour."""
    parity = (field_order or "bff").lower()
    if (video_standard or "pal").lower() == "ntsc":
        # 720x480 29.97i -> square-pixel 640x480, 59.94p, SMPTE-170M.
        return {
            "parity": parity, "scale": "640:480", "fps": "60000/1001",
            "in_matrix": "smpte170m", "cs": "smpte170m",
            "primaries": "smpte170m", "trc": "smpte170m",
            "output_fps": 60000 / 1001,
        }
    # PAL 720x576 25i -> square-pixel 768x576, 50p, bt470bg.
    return {
        "parity": parity, "scale": "768:576", "fps": "50",
        "in_matrix": "bt470bg", "cs": "bt470bg",
        "primaries": "bt470bg", "trc": "gamma28",
        "output_fps": 50.0,
    }


def build_job_snapshot(video_standard: str | None = None, field_order: str | None = None) -> dict[str, Any]:
    """The pinned SeedVR2 settings recorded on every job. Single source of truth
    shared by the interactive API and the automatic backlog reconciler. The
    per-disc deinterlace profile + true output_fps ride here so the worker and
    pipeline handle NTSC / top-field-first discs correctly (defaults = PAL/BFF)."""
    profile = deinterlace_profile(video_standard, field_order)
    return {
        "pipeline": "v3",
        "model": "seedvr2_ema_3b_fp16.safetensors",
        "resolution": 1440,
        "batch": 129,
        "chunk": 750,
        "overlap": 4,
        "output_fps": profile["output_fps"],
        "cancel_semantics": "stage_boundary",
        "video_standard": (video_standard or "pal").lower(),
        "field_order": profile["parity"],
        "deinterlace": {
            "parity": profile["parity"], "scale": profile["scale"], "fps": profile["fps"],
            "in_matrix": profile["in_matrix"], "cs": profile["cs"],
            "primaries": profile["primaries"], "trc": profile["trc"],
        },
    }


def resolve_target(db: sqlite3.Connection, target_type: str, target_id: int) -> dict[str, Any]:
    """Look up a chapter/slice for queueing. Raises QueueError on invalid type,
    missing target, or a skipped chapter."""
    if target_type == "chapter":
        row = db.execute(
            """SELECT c.id, c.title_id, c.start_ms, c.end_ms,
                      COALESCE(c.user_label, c.generated_label) AS display_name,
                      c.priority, d.slug, t.title_number, t.source_cache_path,
                      d.video_standard, d.field_order
               FROM chapters c JOIN titles t ON t.id=c.title_id
               JOIN discs d ON d.id=t.disc_id WHERE c.id=?""",
            (target_id,),
        ).fetchone()
        if row is not None and row["priority"] == "skip":
            raise QueueError("skip", "Skipped chapters cannot be queued")
    elif target_type == "slice":
        row = db.execute(
            """SELECT s.id, s.title_id, s.start_ms, s.end_ms, s.name AS display_name,
                      s.priority, d.slug, t.title_number, t.source_cache_path,
                      d.video_standard, d.field_order
               FROM slices s JOIN titles t ON t.id=s.title_id
               JOIN discs d ON d.id=t.disc_id WHERE s.id=?""",
            (target_id,),
        ).fetchone()
    else:
        raise QueueError("invalid", "target_type must be chapter or slice")
    if row is None:
        raise QueueError("not_found", f"{target_type.title()} not found")
    return dict(row)


def find_active_job(db: sqlite3.Connection, target_type: str, target_id: int) -> sqlite3.Row | None:
    return db.execute(
        f"""SELECT * FROM jobs WHERE target_type=? AND target_id=?
            AND state NOT IN ({','.join('?' for _ in TERMINAL_STATES)})""",
        (target_type, target_id, *TERMINAL_STATES),
    ).fetchone()


def insert_job(db, settings, target: dict, target_type: str, target_id: int, position: int) -> sqlite3.Row:
    """Insert one queued job for a resolved target and record its queued event.
    Mirrors the interactive create path exactly; callers own the transaction and
    the duplicate check (the partial unique index is the final guard)."""
    public_id = f"restore-{uuid.uuid4().hex[:12]}"
    duration_ms = target["end_ms"] - target["start_ms"]
    snapshot = build_job_snapshot(target.get("video_standard"), target.get("field_order"))
    frames_total = round(duration_ms / 1000 * snapshot["output_fps"])
    priority = PRIORITY_NAMES[target["priority"]]
    now = utc_now()
    output_path = settings.data_dir / "outputs" / f"{public_id}.mkv"
    baseline_path = settings.data_dir / "restoration_work" / public_id / "baseline_1440p50.mkv"
    log_path = settings.data_dir / "logs" / f"{public_id}.log"
    cursor = db.execute(
        """INSERT INTO jobs
           (public_id, target_type, target_id, title_id, source_start_ms,
            source_end_ms, display_name, settings_json, priority,
            queue_position, start_requested, frames_total, output_path,
            baseline_path, log_path, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            public_id, target_type, target_id, target["title_id"], target["start_ms"],
            target["end_ms"], target["display_name"], json.dumps(snapshot, sort_keys=True),
            priority, position, int(settings.auto_start_jobs), frames_total,
            str(output_path), str(baseline_path), str(log_path), now, now,
        ),
    )
    job_id = int(cursor.lastrowid)
    append_event(
        db, job_id, "state", state="queued", message="Restoration queued",
        payload={"estimated_bytes": round(duration_ms / 1000 * 32_000_000)},
    )
    return db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


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


# Planning rate for jobs that have not produced a measured ETA yet: sustained
# durable-unit throughput with the persistent compile cache (measured 0.88-0.90
# output fps, 2026-09-09). Once a job runs, the worker's measured rate replaces
# estimates derived from this.
PLANNING_FPS = 0.9


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
    # Queued/paused jobs have no live ETA; give the UI a planning estimate for
    # the remaining GPU work so cards never sit on a bare "ETA pending".
    if not result.get("eta_seconds") and result["frames_total"]:
        remaining = max(0, result["frames_total"] - result["frames_done"])
        # Rounded to whole minutes: it is a planning estimate, not a countdown.
        result["estimated_restore_seconds"] = round(remaining / PLANNING_FPS / 60) * 60
    else:
        result["estimated_restore_seconds"] = None
    result["can_start"] = result["state"] == "queued" and not result["start_requested"]
    result["can_cancel"] = result["state"] in {
        "queued", "preparing", "running", "assembling", "cancel_requested"
    }
    result["can_retry"] = result["state"] in {"failed", "cancelled", "interrupted"}
    # Per-job pause/resume affordances; the global capability (durable-unit mode)
    # is reported by /session, and the pause route enforces it server-side.
    result["pause_available"] = result["state"] in {"preparing", "running", "assembling", "resuming"}
    result["resume_available"] = result["state"] in {"paused", "pause_requested"} or (
        result["state"] == "queued" and not result["start_requested"]
    )
    return result

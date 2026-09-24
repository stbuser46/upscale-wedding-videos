from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

from flask import Blueprint, Response, abort, current_app, jsonify, redirect, request, stream_with_context, url_for

from webapp.db import connect, transaction, utc_now
from webapp.scan.catalog import scan_all_discs
from webapp.server.logtail import parse_log_tail, unit_pod_hints
from webapp.server.reconcile import reconcile_backlog
from webapp.server.services import (
    PRIORITY_NAMES,
    QueueError,
    append_event,
    event_dict,
    find_active_job,
    insert_job,
    job_dict,
    resolve_target,
)


api = Blueprint("api", __name__, url_prefix="/api")
_QUEUE_ERROR_STATUS = {"skip": 409, "not_found": 404, "invalid": 400}
JOB_SELECT = """SELECT j.*, d.slug AS disc_slug, d.collection AS disc_collection, t.title_number
                FROM jobs j JOIN titles t ON t.id=j.title_id
                JOIN discs d ON d.id=t.disc_id"""


def _settings():
    return current_app.config["WEBAPP_SETTINGS"]


def _db():
    return connect(_settings().database_path)


def _json_object() -> dict:
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        abort(400, description="Expected a JSON object")
    return value


def _bounded_text(value: object, name: str, maximum: int, *, required: bool = False) -> str:
    if value is None:
        if required:
            abort(400, description=f"{name} is required")
        return ""
    if not isinstance(value, str):
        abort(400, description=f"{name} must be text")
    value = value.strip()
    if required and not value:
        abort(400, description=f"{name} is required")
    if len(value) > maximum:
        abort(400, description=f"{name} must be at most {maximum} characters")
    return value


@api.get("/session")
def session_info():
    from flask import session

    return jsonify({"csrf_token": session["csrf_token"], "phase": 2, "pause_available": _settings().durable_units})


@api.get("/metrics")
def system_metrics():
    try:
        minutes = min(4320, max(5, int(request.args.get("minutes", 60))))
    except ValueError:
        abort(400, description="minutes must be an integer")
    cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
    with _db() as db:
        rows = db.execute(
            "SELECT ts, cpu_pct, mem_pct, gpu_pct, gpu_mem_mib, gpu_power_w, gpu_temp_c "
            "FROM system_metrics WHERE ts >= ? ORDER BY ts",
            (cutoff.isoformat(timespec="milliseconds"),),
        ).fetchall()
    max_points = 720
    if len(rows) > max_points:
        # Average fixed-size buckets so long ranges stay a bounded payload.
        bucket = len(rows) / max_points

        def bucket_mean(chunk, key):
            values = [row[key] for row in chunk if row[key] is not None]
            return round(sum(values) / len(values), 1) if values else None

        sampled = []
        for index in range(max_points):
            chunk = rows[int(index * bucket): int((index + 1) * bucket)] or [rows[-1]]
            sampled.append({
                "ts": chunk[-1]["ts"],
                "cpu_pct": bucket_mean(chunk, "cpu_pct"),
                "mem_pct": bucket_mean(chunk, "mem_pct"),
                "gpu_pct": bucket_mean(chunk, "gpu_pct"),
                "gpu_mem_mib": bucket_mean(chunk, "gpu_mem_mib"),
                "gpu_power_w": bucket_mean(chunk, "gpu_power_w"),
                "gpu_temp_c": bucket_mean(chunk, "gpu_temp_c"),
            })
        rows = sampled
    else:
        rows = [dict(row) for row in rows]
    return jsonify({"minutes": minutes, "samples": rows})


@api.get("/worker/health")
def worker_health():
    with _db() as db:
        row = db.execute("SELECT * FROM worker_status WHERE id=1").fetchone()
    return jsonify(_worker_health_dict(row))


def _parse_ts(value) -> datetime | None:
    """Parse an ISO8601/SQLite timestamp, treating naive stamps as UTC.

    cloud_pods rows mix Python utc_now() (timezone-aware) with SQL
    datetime('now') (naive UTC, written by teardown/reaper paths), and
    subtracting a naive from an aware datetime raises TypeError — which
    500'd the /live endpoint the moment such a row appeared."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _worker_health_dict(row) -> dict:
    if row is None:
        return {"present": False, "alive": False, "activity": "unknown"}
    result = dict(row)
    result["present"] = True
    # The worker beats at least every ~15s while running and every poll interval
    # while idle; treat a >90s gap (or a clean 'stopped') as not alive.
    alive = False
    last_beat = result.get("last_beat_at")
    if last_beat and result.get("activity") != "stopped":
        try:
            delta = (datetime.now(UTC) - datetime.fromisoformat(last_beat)).total_seconds()
            alive = delta <= 90
        except (ValueError, TypeError):
            alive = False
    result["alive"] = alive
    return result


@api.get("/discs")
def discs():
    with _db() as db:
        rows = db.execute(
            """SELECT d.*,
                      COUNT(DISTINCT t.id) AS title_count,
                      COUNT(DISTINCT c.id) AS chapter_count,
                      COALESCE(SUM(CASE WHEN c.proxy_state='ready' THEN 1 ELSE 0 END), 0) AS proxies_ready,
                      COALESCE(SUM(CASE WHEN EXISTS(
                          SELECT 1 FROM jobs j WHERE j.target_type='chapter'
                          AND j.target_id=c.id AND j.state='completed'
                      ) THEN 1 ELSE 0 END), 0) AS chapters_restored
               FROM discs d LEFT JOIN titles t ON t.disc_id=d.id
               LEFT JOIN chapters c ON c.title_id=t.id
               GROUP BY d.id ORDER BY d.id"""
        ).fetchall()
    return jsonify([dict(row) for row in rows])


@api.post("/discs/scan")
def scan_discs():
    ids = scan_all_discs(_settings())
    return jsonify({"disc_ids": ids, "status": "complete"})


@api.get("/discs/<int:disc_id>/titles")
def disc_titles(disc_id: int):
    with _db() as db:
        disc = db.execute("SELECT * FROM discs WHERE id=?", (disc_id,)).fetchone()
        if disc is None:
            abort(404, description="Disc not found")
        rows = db.execute(
            """SELECT t.*, COUNT(c.id) AS chapter_count,
                      COALESCE(SUM(CASE WHEN c.proxy_state='ready' THEN 1 ELSE 0 END), 0) AS proxies_ready,
                      COALESCE(SUM(CASE WHEN EXISTS(
                          SELECT 1 FROM jobs j WHERE j.target_type='chapter'
                          AND j.target_id=c.id AND j.state='completed'
                      ) THEN 1 ELSE 0 END), 0) AS chapters_restored
               FROM titles t LEFT JOIN chapters c ON c.title_id=t.id
               WHERE t.disc_id=? GROUP BY t.id ORDER BY t.duration_ms DESC, t.title_number""",
            (disc_id,),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for key in ("video_json", "audio_json", "subtitles_json", "raw_navigation_json"):
            item[key.removesuffix("_json")] = json.loads(item.pop(key))
        result.append(item)
    return jsonify({"disc": dict(disc), "titles": result})


@api.get("/titles/<int:title_id>/chapters")
def title_chapters(title_id: int):
    with _db() as db:
        title = db.execute(
            """SELECT t.*, d.slug AS disc_slug, d.label AS disc_label
               FROM titles t JOIN discs d ON d.id=t.disc_id WHERE t.id=?""",
            (title_id,),
        ).fetchone()
        if title is None:
            abort(404, description="Title not found")
        rows = db.execute(
            """SELECT c.*,
                      p.id AS proxy_artifact_id, p.relative_path AS proxy_path,
                      th.id AS thumbnail_artifact_id, th.relative_path AS thumbnail_path
               FROM chapters c
               LEFT JOIN artifacts p ON p.chapter_id=c.id AND p.kind='chapter_proxy'
               LEFT JOIN artifacts th ON th.chapter_id=c.id AND th.kind='thumbnail'
               WHERE c.title_id=? ORDER BY c.chapter_number""",
            (title_id,),
        ).fetchall()
        job_rows = db.execute(
            """SELECT target_id, state, public_id, frames_done, frames_total, completed_at
               FROM jobs WHERE target_type='chapter' AND title_id=? ORDER BY id""",
            (title_id,),
        ).fetchall()
    active_states = {"preparing", "running", "assembling", "cancel_requested"}
    restoration: dict[int, dict] = {}
    for job in job_rows:
        info = restoration.setdefault(job["target_id"], {
            "restored": False, "restored_job": None, "restored_at": None,
            "active_state": None, "active_job": None, "active_progress": None,
            "last_state": None,
        })
        info["last_state"] = job["state"]
        if job["state"] == "completed":
            info["restored"] = True
            info["restored_job"] = job["public_id"]
            info["restored_at"] = job["completed_at"]
        elif job["state"] == "queued" or job["state"] in active_states:
            info["active_state"] = job["state"]
            info["active_job"] = job["public_id"]
            if job["frames_total"]:
                info["active_progress"] = round(job["frames_done"] * 100 / job["frames_total"])
    title_item = dict(title)
    for key in ("video_json", "audio_json", "subtitles_json", "raw_navigation_json"):
        title_item[key.removesuffix("_json")] = json.loads(title_item.pop(key))
    chapters = []
    for row in rows:
        item = dict(row)
        item["restoration"] = restoration.get(item["id"])
        chapters.append(item)
    return jsonify({"title": title_item, "chapters": chapters})


@api.get("/titles/<int:title_id>/proxy")
def title_proxy(title_id: int):
    with _db() as db:
        artifact = db.execute(
            "SELECT id FROM artifacts WHERE title_id=? AND kind='title_proxy' AND validation_state='valid'",
            (title_id,),
        ).fetchone()
    if artifact is None:
        abort(404, description="No full-title proxy; play an individual chapter proxy")
    return redirect(url_for("media", artifact_id=artifact["id"]))


@api.patch("/chapters/<int:chapter_id>")
def update_chapter(chapter_id: int):
    data = _json_object()
    allowed = {"name", "priority", "notes"}
    unknown = set(data) - allowed
    if unknown:
        abort(400, description=f"Unsupported fields: {', '.join(sorted(unknown))}")
    updates: list[str] = []
    values: list[object] = []
    if "name" in data:
        updates.append("user_label=?")
        values.append(_bounded_text(data["name"], "name", 120) or None)
    if "priority" in data:
        if data["priority"] not in {*PRIORITY_NAMES, "skip"}:
            abort(400, description="priority must be high, normal, low, or skip")
        updates.append("priority=?")
        values.append(data["priority"])
    if "notes" in data:
        updates.append("notes=?")
        values.append(_bounded_text(data["notes"], "notes", 2000))
    if not updates:
        abort(400, description="No supported fields supplied")
    updates.append("updated_at=?")
    values.extend((utc_now(), chapter_id))
    with _db() as db:
        cursor = db.execute(f"UPDATE chapters SET {', '.join(updates)} WHERE id=?", values)
        if cursor.rowcount == 0:
            abort(404, description="Chapter not found")
        row = db.execute("SELECT * FROM chapters WHERE id=?", (chapter_id,)).fetchone()
    return jsonify(dict(row))


@api.post("/slices")
def create_slice():
    data = _json_object()
    try:
        title_id = int(data["title_id"])
        start_ms = round(float(data["start_seconds"]) * 1000)
        end_ms = round(float(data["end_seconds"]) * 1000)
    except (KeyError, TypeError, ValueError, OverflowError):
        abort(400, description="title_id, start_seconds and end_seconds are required numbers")
    name = _bounded_text(data.get("name"), "name", 120, required=True)
    note = _bounded_text(data.get("note"), "note", 2000)
    priority = data.get("priority", "normal")
    if priority not in PRIORITY_NAMES:
        abort(400, description="priority must be high, normal, or low")
    with _db() as db, transaction(db):
        title = db.execute("SELECT duration_ms FROM titles WHERE id=?", (title_id,)).fetchone()
        if title is None:
            abort(404, description="Title not found")
        if start_ms < 0 or end_ms <= start_ms or end_ms > title["duration_ms"]:
            abort(400, description="Slice must start at or after zero and end inside the title")
        fingerprint = hashlib.sha256(f"{title_id}:{start_ms}:{end_ms}".encode("ascii")).hexdigest()
        existing = db.execute("SELECT * FROM slices WHERE request_fingerprint=?", (fingerprint,)).fetchone()
        if existing is not None:
            return jsonify({"slice": dict(existing), "duplicate": True}), 200
        chapters = [
            row["id"]
            for row in db.execute(
                "SELECT id FROM chapters WHERE title_id=? AND start_ms < ? AND end_ms > ? ORDER BY chapter_number",
                (title_id, end_ms, start_ms),
            )
        ]
        now = utc_now()
        cursor = db.execute(
            """INSERT INTO slices
               (title_id, start_ms, end_ms, name, note, priority,
                source_chapters_json, request_fingerprint, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (title_id, start_ms, end_ms, name, note, priority, json.dumps(chapters), fingerprint, now, now),
        )
        row = db.execute("SELECT * FROM slices WHERE id=?", (cursor.lastrowid,)).fetchone()
    return jsonify({"slice": dict(row), "duplicate": False}), 201


@api.post("/backlog/reconcile")
def backlog_reconcile():
    report = reconcile_backlog(_settings())
    return jsonify(report), 201 if report["created"] else 200


@api.post("/jobs")
def create_jobs():
    data = _json_object()
    targets: list[tuple[str, int]] = []
    if "chapter_ids" in data:
        if not isinstance(data["chapter_ids"], list) or not data["chapter_ids"]:
            abort(400, description="chapter_ids must be a non-empty list")
        try:
            targets = [("chapter", int(item)) for item in data["chapter_ids"]]
        except (TypeError, ValueError):
            abort(400, description="chapter_ids must contain integers")
        if len(targets) > 100:
            abort(400, description="At most 100 chapters may be queued at once")
    else:
        try:
            targets = [(str(data["target_type"]), int(data["target_id"]))]
        except (KeyError, TypeError, ValueError):
            abort(400, description="target_type and target_id are required")
    created: list[dict] = []
    duplicates: list[dict] = []
    settings = _settings()
    with _db() as db, transaction(db):
        position = db.execute("SELECT COALESCE(MAX(queue_position), 0) FROM jobs").fetchone()[0]
        for target_type, target_id in dict.fromkeys(targets):
            try:
                target = resolve_target(db, target_type, target_id)
            except QueueError as exc:
                abort(_QUEUE_ERROR_STATUS.get(exc.code, 400), description=exc.message)
            existing = find_active_job(db, target_type, target_id)
            if existing is not None:
                duplicates.append(job_dict(existing))
                continue
            position += 1
            job = insert_job(db, settings, target, target_type, target_id, position)
            created.append(job_dict(job))
    return jsonify({"created": created, "duplicates": duplicates}), 201 if created else 200


@api.get("/jobs")
def jobs():
    with _db() as db:
        rows = db.execute(
            JOB_SELECT + " ORDER BY CASE WHEN j.state IN ('running','preparing','assembling','cancel_requested') THEN 0 ELSE 1 END, j.priority DESC, j.queue_position, j.created_at DESC"
        ).fetchall()
    return jsonify([job_dict(row) for row in rows])


@api.get("/jobs/<public_id>")
def job_detail(public_id: str):
    with _db() as db:
        row = db.execute(JOB_SELECT + " WHERE j.public_id=?", (public_id,)).fetchone()
        if row is None:
            abort(404, description="Job not found")
        chunks = [dict(item) for item in db.execute("SELECT * FROM job_chunks WHERE job_id=? ORDER BY sequence", (row["id"],))]
        artifacts = [dict(item) for item in db.execute("SELECT * FROM artifacts WHERE job_id=? ORDER BY id", (row["id"],))]
    result = job_dict(row)
    result["chunks"] = chunks
    result["artifacts"] = artifacts
    result["free_space_bytes"] = shutil.disk_usage(_settings().data_dir).free
    return jsonify(result)


@api.get("/jobs/<public_id>/live")
def job_live(public_id: str):
    """Live sub-progress that the DB alone can't show: a durable job only
    advances jobs.frames_done once per ~750-frame unit (~15 min locally), so
    this combines the per-unit chip strip (job_chunks — works for local and
    cloud jobs alike) with a tail-parse of the job's own SeedVR2 log for the
    current unit's phase/batch/frame-write detail. For cloud jobs, unit->pod
    attribution is recovered cheaply from the "=== unit N restore on host:port
    ===" lines the pod executor already prints, matched against this job's
    cloud_pods rows. Safe to poll every few seconds; degrades to empty/None
    fields rather than erroring when a job hasn't started or has no log yet."""
    with _db() as db:
        job = db.execute("SELECT * FROM jobs WHERE public_id=?", (public_id,)).fetchone()
        if job is None:
            abort(404, description="Job not found")
        chunk_rows = db.execute(
            "SELECT sequence, state, frame_count FROM job_chunks WHERE job_id=? ORDER BY sequence",
            (job["id"],),
        ).fetchall()
        pod_rows = db.execute(
            """SELECT pod_id, name, gpu_type, state, hourly_rate, ssh_host, ssh_port,
                      created_at, ready_at, terminated_at
               FROM cloud_pods WHERE job_id=? ORDER BY id""",
            (job["id"],),
        ).fetchall()

    try:
        snapshot = json.loads(job["settings_json"])
    except (TypeError, ValueError):
        snapshot = {}
    chunk_size = snapshot.get("chunk") or 750
    chunks_by_seq = {row["sequence"]: row for row in chunk_rows}
    # job_chunks rows are created incrementally as the worker reaches each
    # unit, so max(sequence)+1 undercounts the total until the last unit has
    # started; frames_total / chunk_size is the true unit count from the start.
    if job["frames_total"] and chunk_size:
        units_total = math.ceil(job["frames_total"] / chunk_size)
    elif chunks_by_seq:
        units_total = max(chunks_by_seq) + 1
    else:
        units_total = None

    log_path = Path(job["log_path"]) if job["log_path"] else None
    log_info = parse_log_tail(log_path)
    hints = unit_pod_hints(log_path) if pod_rows else {}
    pods_by_hostport = {
        (row["ssh_host"], row["ssh_port"]): row for row in pod_rows if row["ssh_host"] is not None
    }

    units = []
    counts = {"valid": 0, "running": 0, "invalid": 0, "pending": 0}
    span = range(units_total) if units_total is not None else sorted(chunks_by_seq)
    for seq in span:
        row = chunks_by_seq.get(seq)
        state = row["state"] if row else "pending"
        counts[state] = counts.get(state, 0) + 1
        pod = None
        pod_row = pods_by_hostport.get(hints.get(seq))
        if pod_row is not None:
            pod = {"pod_id": pod_row["pod_id"], "name": pod_row["name"], "gpu_type": pod_row["gpu_type"]}
        units.append({
            "sequence": seq, "state": state,
            "frame_count": row["frame_count"] if row else None,
            "pod": pod,
        })

    now = datetime.now(UTC)
    pods = []
    for row in pod_rows:
        created = _parse_ts(row["created_at"])
        ended = _parse_ts(row["terminated_at"]) or now
        lifetime_s = max(0.0, (ended - created).total_seconds()) if created else 0.0
        rate = row["hourly_rate"] or 0.0
        pods.append({
            "pod_id": row["pod_id"], "name": row["name"], "gpu_type": row["gpu_type"], "state": row["state"],
            "rate_per_hr": rate, "uptime_s": int(round(lifetime_s)), "cost_usd": round(rate * lifetime_s / 3600.0, 2),
        })

    executor = "cloud" if pod_rows else ("local" if job["worker_pid"] else None)
    return jsonify({
        "public_id": job["public_id"],
        "executor": executor,
        "state": job["state"],
        "stage": job["stage"],
        "units_total": units_total,
        "units": units,
        "units_summary": counts,
        "pods": pods,
        "log": log_info,
    })


@api.get("/jobs/<public_id>/events")
def job_events(public_id: str):
    try:
        after = max(0, int(request.args.get("after", "0")))
    except ValueError:
        abort(400, description="after must be an integer")
    with _db() as db:
        job = db.execute("SELECT id FROM jobs WHERE public_id=?", (public_id,)).fetchone()
        if job is None:
            abort(404, description="Job not found")
        rows = db.execute(
            "SELECT * FROM job_events WHERE job_id=? AND id>? ORDER BY id LIMIT 500",
            (job["id"], after),
        ).fetchall()
    return jsonify([event_dict(row) for row in rows])


@api.get("/jobs/<public_id>/stream")
def job_stream(public_id: str):
    with _db() as db:
        job = db.execute("SELECT id FROM jobs WHERE public_id=?", (public_id,)).fetchone()
    if job is None:
        abort(404, description="Job not found")
    job_id = job["id"]

    @stream_with_context
    def events():
        last_id = 0
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            with _db() as db:
                rows = db.execute(
                    "SELECT * FROM job_events WHERE job_id=? AND id>? ORDER BY id",
                    (job_id, last_id),
                ).fetchall()
            if rows:
                for row in rows:
                    last_id = row["id"]
                    yield f"id: {last_id}\nevent: {row['event_type']}\ndata: {json.dumps(event_dict(row))}\n\n"
            else:
                yield ": keepalive\n\n"
            time.sleep(1)

    return Response(events(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache"})


def _get_job_for_update(db: sqlite3.Connection, public_id: str):
    row = db.execute("SELECT * FROM jobs WHERE public_id=?", (public_id,)).fetchone()
    if row is None:
        abort(404, description="Job not found")
    return row


@api.post("/jobs/<public_id>/start")
def start_job(public_id: str):
    with _db() as db, transaction(db):
        job = _get_job_for_update(db, public_id)
        if job["state"] != "queued":
            abort(409, description=f"Only queued jobs can start; current state is {job['state']}")
        if not job["start_requested"]:
            db.execute("UPDATE jobs SET start_requested=1, auto_resume_count=0, updated_at=? WHERE id=?", (utc_now(), job["id"]))
            append_event(db, job["id"], "command", state="queued", message="Start requested")
    return jsonify({"status": "start_requested"})


@api.post("/jobs/<public_id>/pause")
def pause_job(public_id: str):
    if not _settings().durable_units:
        abort(409, description="Pause requires durable-unit mode (WEDDING_DURABLE_UNITS=1)")
    with _db() as db, transaction(db):
        job = _get_job_for_update(db, public_id)
        state = job["state"]
        if state in {"paused", "pause_requested"}:
            return jsonify({"status": state})
        if state == "queued":
            # Not started yet: hold it so the worker won't claim it.
            db.execute("UPDATE jobs SET start_requested=0, updated_at=? WHERE id=?", (utc_now(), job["id"]))
            append_event(db, job["id"], "command", state="queued", message="Job held; will not start until resumed")
            return jsonify({"status": "held"})
        if state not in {"preparing", "running", "assembling", "resuming"}:
            abort(409, description=f"Job cannot be paused from {state}")
        db.execute("UPDATE jobs SET state='pause_requested', updated_at=? WHERE id=?", (utc_now(), job["id"]))
        append_event(
            db, job["id"], "state", state="pause_requested", stage=job["stage"],
            message="Pause requested; the current unit will finish first",
        )
    return jsonify({"status": "pause_requested", "boundary": "unit"})


@api.post("/jobs/<public_id>/resume")
def resume_job(public_id: str):
    with _db() as db, transaction(db):
        job = _get_job_for_update(db, public_id)
        state = job["state"]
        if state == "paused":
            db.execute(
                "UPDATE jobs SET state='queued', start_requested=1, auto_resume_count=0, updated_at=? WHERE id=?",
                (utc_now(), job["id"]),
            )
            append_event(db, job["id"], "state", state="queued", message="Resumed; will continue from the first unfinished unit")
            return jsonify({"status": "queued"})
        if state == "pause_requested":
            # Cancel a pending pause before it took effect.
            db.execute("UPDATE jobs SET state='running', updated_at=? WHERE id=?", (utc_now(), job["id"]))
            append_event(db, job["id"], "state", state="running", message="Pause cancelled; continuing")
            return jsonify({"status": "running"})
        if state == "queued" and not job["start_requested"]:
            db.execute("UPDATE jobs SET start_requested=1, updated_at=? WHERE id=?", (utc_now(), job["id"]))
            append_event(db, job["id"], "command", state="queued", message="Start requested")
            return jsonify({"status": "start_requested"})
        abort(409, description=f"Job cannot be resumed from {state}")


@api.post("/jobs/<public_id>/cancel")
def cancel_job(public_id: str):
    with _db() as db, transaction(db):
        job = _get_job_for_update(db, public_id)
        if job["state"] in {"completed", "failed", "cancelled"}:
            return jsonify({"status": job["state"]})
        if job["state"] == "queued":
            db.execute(
                "UPDATE jobs SET state='cancelled', start_requested=0, completed_at=?, updated_at=? WHERE id=?",
                (utc_now(), utc_now(), job["id"]),
            )
            append_event(db, job["id"], "state", state="cancelled", message="Queued job cancelled")
            return jsonify({"status": "cancelled"})
        if job["state"] not in {"preparing", "running", "assembling", "cancel_requested"}:
            abort(409, description=f"Job cannot be cancelled from {job['state']}")
        if job["state"] != "cancel_requested":
            db.execute("UPDATE jobs SET state='cancel_requested', updated_at=? WHERE id=?", (utc_now(), job["id"]))
            append_event(
                db, job["id"], "state", state="cancel_requested", stage=job["stage"],
                message="Cancellation requested; the current pipeline stage will finish first",
            )
    return jsonify({"status": "cancel_requested", "boundary": "pipeline_stage"})


@api.post("/jobs/<public_id>/retry")
def retry_job(public_id: str):
    with _db() as db, transaction(db):
        job = _get_job_for_update(db, public_id)
        if job["state"] not in {"failed", "cancelled", "interrupted"}:
            abort(409, description=f"Job cannot be retried from {job['state']}")
        active = db.execute(
            """SELECT public_id FROM jobs WHERE target_type=? AND target_id=? AND id!=?
               AND state NOT IN ('completed', 'failed', 'cancelled')""",
            (job["target_type"], job["target_id"], job["id"]),
        ).fetchone()
        if active is not None:
            abort(409, description=f"Target already has active job {active['public_id']}")
        db.execute(
            """UPDATE jobs SET state='queued', start_requested=0, stage=NULL, error=NULL,
               frames_done=0, fps=NULL, eta_seconds=NULL, completed_at=NULL,
               auto_resume_count=0, lease_expires_at=NULL, updated_at=? WHERE id=?""",
            (utc_now(), job["id"]),
        )
        append_event(db, job["id"], "state", state="queued", message="Retry queued; completed major stages will be reused")
    return jsonify({"status": "queued"})


@api.patch("/jobs/<public_id>/priority")
def update_job_priority(public_id: str):
    data = _json_object()
    priority = data.get("priority")
    if priority not in PRIORITY_NAMES:
        abort(400, description="priority must be high, normal, or low")
    with _db() as db, transaction(db):
        job = _get_job_for_update(db, public_id)
        if job["state"] != "queued":
            abort(409, description="Only queued jobs can change priority")
        db.execute("UPDATE jobs SET priority=?, updated_at=? WHERE id=?", (PRIORITY_NAMES[priority], utc_now(), job["id"]))
        append_event(db, job["id"], "command", state="queued", message=f"Priority changed to {priority}")
    return jsonify({"status": "updated", "priority": PRIORITY_NAMES[priority]})


@api.post("/jobs/<public_id>/move")
def move_job(public_id: str):
    direction = _json_object().get("direction")
    if direction not in {"up", "down"}:
        abort(400, description="direction must be up or down")
    operator = "<" if direction == "up" else ">"
    ordering = "DESC" if direction == "up" else "ASC"
    with _db() as db, transaction(db):
        job = _get_job_for_update(db, public_id)
        if job["state"] != "queued":
            abort(409, description="Only queued jobs can be reordered")
        other = db.execute(
            f"""SELECT id, queue_position FROM jobs WHERE state='queued'
                AND priority=? AND queue_position {operator} ? ORDER BY queue_position {ordering} LIMIT 1""",
            (job["priority"], job["queue_position"]),
        ).fetchone()
        if other is not None:
            db.execute("UPDATE jobs SET queue_position=? WHERE id=?", (other["queue_position"], job["id"]))
            db.execute("UPDATE jobs SET queue_position=? WHERE id=?", (job["queue_position"], other["id"]))
            append_event(db, job["id"], "command", state="queued", message=f"Moved {direction} in queue")
    return jsonify({"status": "updated"})

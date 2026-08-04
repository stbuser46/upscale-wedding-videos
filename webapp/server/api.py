from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

from flask import Blueprint, Response, abort, current_app, jsonify, redirect, request, stream_with_context, url_for

from webapp.db import connect, transaction, utc_now
from webapp.scan.catalog import scan_all_discs
from webapp.server.services import append_event, event_dict, job_dict


api = Blueprint("api", __name__, url_prefix="/api")
PRIORITY_NAMES = {"high": 100, "normal": 50, "low": 10}
JOB_SELECT = """SELECT j.*, d.slug AS disc_slug, t.title_number
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

    return jsonify({"csrf_token": session["csrf_token"], "phase": 2, "pause_available": False})


@api.get("/metrics")
def system_metrics():
    try:
        minutes = min(4320, max(5, int(request.args.get("minutes", 60))))
    except ValueError:
        abort(400, description="minutes must be an integer")
    cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
    with _db() as db:
        rows = db.execute(
            "SELECT ts, cpu_pct, mem_pct, gpu_pct, gpu_mem_mib, gpu_power_w "
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
            })
        rows = sampled
    else:
        rows = [dict(row) for row in rows]
    return jsonify({"minutes": minutes, "samples": rows})


@api.get("/discs")
def discs():
    with _db() as db:
        rows = db.execute(
            """SELECT d.*,
                      COUNT(DISTINCT t.id) AS title_count,
                      COUNT(DISTINCT c.id) AS chapter_count,
                      COALESCE(SUM(CASE WHEN c.proxy_state='ready' THEN 1 ELSE 0 END), 0) AS proxies_ready
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
                      COALESCE(SUM(CASE WHEN c.proxy_state='ready' THEN 1 ELSE 0 END), 0) AS proxies_ready
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
    title_item = dict(title)
    for key in ("video_json", "audio_json", "subtitles_json", "raw_navigation_json"):
        title_item[key.removesuffix("_json")] = json.loads(title_item.pop(key))
    return jsonify({"title": title_item, "chapters": [dict(row) for row in rows]})


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


def _queue_target(db: sqlite3.Connection, target_type: str, target_id: int) -> dict:
    if target_type == "chapter":
        row = db.execute(
            """SELECT c.id, c.title_id, c.start_ms, c.end_ms,
                      COALESCE(c.user_label, c.generated_label) AS display_name,
                      c.priority, d.slug, t.title_number, t.source_cache_path
               FROM chapters c JOIN titles t ON t.id=c.title_id
               JOIN discs d ON d.id=t.disc_id WHERE c.id=?""",
            (target_id,),
        ).fetchone()
        if row is not None and row["priority"] == "skip":
            abort(409, description="Skipped chapters cannot be queued")
    elif target_type == "slice":
        row = db.execute(
            """SELECT s.id, s.title_id, s.start_ms, s.end_ms, s.name AS display_name,
                      s.priority, d.slug, t.title_number, t.source_cache_path
               FROM slices s JOIN titles t ON t.id=s.title_id
               JOIN discs d ON d.id=t.disc_id WHERE s.id=?""",
            (target_id,),
        ).fetchone()
    else:
        abort(400, description="target_type must be chapter or slice")
    if row is None:
        abort(404, description=f"{target_type.title()} not found")
    return dict(row)


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
            target = _queue_target(db, target_type, target_id)
            existing = db.execute(
                """SELECT * FROM jobs WHERE target_type=? AND target_id=?
                   AND state NOT IN ('completed', 'failed', 'cancelled')""",
                (target_type, target_id),
            ).fetchone()
            if existing is not None:
                duplicates.append(job_dict(existing))
                continue
            position += 1
            public_id = f"restore-{uuid.uuid4().hex[:12]}"
            duration_ms = target["end_ms"] - target["start_ms"]
            snapshot = {
                "pipeline": "v3",
                "model": "seedvr2_ema_3b_fp16.safetensors",
                "resolution": 1440,
                "batch": 129,
                "chunk": 750,
                "overlap": 4,
                "output_fps": 50,
                "cancel_semantics": "stage_boundary",
            }
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
                    priority, position, int(settings.auto_start_jobs), round(duration_ms / 20),
                    str(output_path), str(baseline_path), str(log_path), now, now,
                ),
            )
            job_id = int(cursor.lastrowid)
            append_event(
                db, job_id, "state", state="queued", message="Restoration queued",
                payload={"estimated_bytes": round(duration_ms / 1000 * 32_000_000)},
            )
            created.append(job_dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()))
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
            db.execute("UPDATE jobs SET start_requested=1, updated_at=? WHERE id=?", (utc_now(), job["id"]))
            append_event(db, job["id"], "command", state="queued", message="Start requested")
    return jsonify({"status": "start_requested"})


@api.post("/jobs/<public_id>/pause")
def pause_job(public_id: str):
    with _db() as db:
        _get_job_for_update(db, public_id)
    abort(409, description="Pause/resume requires Phase 3 durable chunks and is not available yet")


@api.post("/jobs/<public_id>/resume")
def resume_job(public_id: str):
    with _db() as db:
        _get_job_for_update(db, public_id)
    abort(409, description="Pause/resume requires Phase 3 durable chunks and is not available yet")


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
               frames_done=0, fps=NULL, eta_seconds=NULL, completed_at=NULL, updated_at=? WHERE id=?""",
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

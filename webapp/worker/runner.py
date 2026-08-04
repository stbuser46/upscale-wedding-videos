from __future__ import annotations

import argparse
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import sys
import time
from typing import Any

from webapp.config import Settings, load_settings
from webapp.db import connect, migrate, transaction, utc_now
from webapp.server.services import append_event


PIPELINE_PREFIX = "PIPELINE_EVENT "
PROGRESS_RE = re.compile(r"frame=\s*(\d+).*?fps=\s*([0-9.]+)")
ACTIVE_STATES = ("preparing", "running", "assembling", "cancel_requested")


class WorkerError(RuntimeError):
    pass


class GpuLock:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = path.open("a+")

    def __enter__(self):
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkerError("Another restoration worker already owns the GPU lock") from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(f"{os.getpid()}\n")
        self.handle.flush()
        return self

    def __exit__(self, exc_type, exc, traceback):
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()


def _recover_interrupted(settings: Settings) -> None:
    with connect(settings.database_path) as db, transaction(db):
        rows = db.execute(
            f"SELECT * FROM jobs WHERE state IN ({','.join('?' for _ in ACTIVE_STATES)})",
            ACTIVE_STATES,
        ).fetchall()
        for job in rows:
            previous = job["state"]
            now = utc_now()
            db.execute(
                "UPDATE jobs SET state='interrupted', worker_pid=NULL, updated_at=? WHERE id=?",
                (now, job["id"]),
            )
            append_event(
                db, job["id"], "state", state="interrupted", stage=job["stage"],
                message=f"Worker restarted while job was {previous}; completed major stages remain reusable",
            )
            if previous == "cancel_requested":
                db.execute(
                    "UPDATE jobs SET state='cancelled', start_requested=0, completed_at=?, updated_at=? WHERE id=?",
                    (now, now, job["id"]),
                )
                append_event(db, job["id"], "state", state="cancelled", message="Cancellation completed during worker recovery")
            else:
                db.execute(
                    "UPDATE jobs SET state='queued', start_requested=1, updated_at=? WHERE id=?",
                    (now, job["id"]),
                )
                append_event(db, job["id"], "state", state="queued", message="Interrupted job requeued for major-stage resume")


def _claim_next(settings: Settings):
    with connect(settings.database_path) as db, transaction(db):
        job = db.execute(
            """SELECT j.*, t.source_cache_path, d.slug AS disc_slug, t.title_number
               FROM jobs j JOIN titles t ON t.id=j.title_id
               JOIN discs d ON d.id=t.disc_id
               WHERE j.state='queued' AND j.start_requested=1
               ORDER BY j.priority DESC, j.queue_position, j.created_at LIMIT 1"""
        ).fetchone()
        if job is None:
            return None
        now = utc_now()
        updated = db.execute(
            """UPDATE jobs SET state='preparing', stage='worker_checks', worker_pid=?,
               claimed_at=?, started_at=COALESCE(started_at, ?), updated_at=?
               WHERE id=? AND state='queued' AND start_requested=1""",
            (os.getpid(), now, now, now, job["id"]),
        )
        if updated.rowcount != 1:
            return None
        append_event(db, job["id"], "state", state="preparing", stage="worker_checks", message="Worker claimed job")
        return db.execute(
            """SELECT j.*, t.source_cache_path, d.slug AS disc_slug, t.title_number
               FROM jobs j JOIN titles t ON t.id=j.title_id
               JOIN discs d ON d.id=t.disc_id WHERE j.id=?""",
            (job["id"],),
        ).fetchone()


def _allowed_source(settings: Settings, value: str | None) -> Path:
    if not value:
        raise WorkerError("Title review source is not generated; run proxy generation first")
    path = Path(value).resolve()
    allowed = path.is_relative_to(settings.data_dir / "title_sources") or path.is_relative_to(settings.source_dir)
    if not allowed or not path.is_file():
        raise WorkerError("Catalog title source is missing or outside an allowed read-only location")
    return path


def _touch_cancel(control_path: Path) -> None:
    if control_path.exists():
        return
    control_path.parent.mkdir(parents=True, exist_ok=True)
    partial = control_path.with_suffix(".partial")
    partial.write_text("cancel\n", encoding="ascii")
    os.replace(partial, control_path)


def _job_state(settings: Settings, job_id: int) -> str:
    with connect(settings.database_path) as db:
        row = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
    return row["state"] if row else "cancel_requested"


def _pipeline_event(settings: Settings, job_id: int, event: dict[str, str], started: float) -> None:
    event_type = event.get("type", "pipeline")
    stage = event.get("stage")
    now = utc_now()
    with connect(settings.database_path) as db, transaction(db):
        current = db.execute("SELECT state, frames_total FROM jobs WHERE id=?", (job_id,)).fetchone()
        if current is None:
            return
        state = current["state"]
        if event_type == "stage_start" and state != "cancel_requested":
            state = "assembling" if stage == "audio_mux" else ("preparing" if stage == "prepare_50p" else "running")
            db.execute(
                "UPDATE jobs SET state=?, stage=?, elapsed_seconds=?, updated_at=? WHERE id=?",
                (state, stage, time.monotonic() - started, now, job_id),
            )
        elif event_type == "stage_complete":
            frames_done = current["frames_total"] if stage == "seedvr2_restore" else None
            db.execute(
                """UPDATE jobs SET stage=?, elapsed_seconds=?,
                   frames_done=COALESCE(?, frames_done), updated_at=? WHERE id=?""",
                (stage, time.monotonic() - started, frames_done, now, job_id),
            )
        append_event(
            db, job_id, "stage", state=state, stage=stage,
            message=f"{stage.replace('_', ' ').title()}: {event_type.replace('_', ' ')}",
            payload=event,
        )


def _progress_update(settings: Settings, job_id: int, stage: str | None, frame: int, fps: float, started: float) -> None:
    elapsed = time.monotonic() - started
    with connect(settings.database_path) as db, transaction(db):
        job = db.execute("SELECT frames_total, state FROM jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            return
        # Frame counters from preparation encodes are stage-local. Only expose
        # completed restoration frames as end-to-end progress.
        frames_done = min(frame, job["frames_total"]) if stage == "seedvr2_restore" else 0
        eta = (job["frames_total"] - frames_done) / fps if fps > 0 and stage == "seedvr2_restore" else job["frames_total"] / 0.71
        db.execute(
            """UPDATE jobs SET frames_done=?, fps=?, elapsed_seconds=?, eta_seconds=?, updated_at=?
               WHERE id=?""",
            (frames_done, fps or None, elapsed, max(0, eta), utc_now(), job_id),
        )
        append_event(
            db, job_id, "progress", state=job["state"], stage=stage,
            message=None,
            payload={
                "frames_done": frames_done, "frames_total": job["frames_total"],
                "fps": fps, "elapsed_seconds": round(elapsed, 1), "eta_seconds": round(max(0, eta), 1),
            },
        )


def _system_metrics(settings: Settings) -> dict[str, Any]:
    payload: dict[str, Any] = {"free_disk_bytes": shutil.disk_usage(settings.data_dir).free}
    try:
        result = subprocess.run(
            [
                "nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            check=True, capture_output=True, text=True, timeout=5,
        )
        used, total, utilization = (int(part.strip()) for part in result.stdout.splitlines()[0].split(","))
        payload.update(gpu_memory_mib=used, gpu_memory_total_mib=total, gpu_utilization_percent=utilization)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        payload["gpu_metrics_unavailable"] = True
    return payload


def _run_pipeline(settings: Settings, job) -> int:
    source = _allowed_source(settings, job["source_cache_path"])
    output = Path(job["output_path"]).resolve()
    log = Path(job["log_path"]).resolve()
    for generated in (output, log):
        if not generated.is_relative_to(settings.data_dir):
            raise WorkerError("Generated job path escaped the application data directory")
        generated.parent.mkdir(parents=True, exist_ok=True)
    duration = (job["source_end_ms"] - job["source_start_ms"]) / 1000
    start = job["source_start_ms"] / 1000
    control = settings.data_dir / "control" / f"{job['public_id']}.cancel"
    control.unlink(missing_ok=True)
    log_partial = log.with_suffix(".partial.log")
    command = [
        str(settings.pipeline_path), str(source), f"{start:.3f}", f"{duration:.3f}",
        str(output), job["public_id"],
    ]
    snapshot = json.loads(job["settings_json"])
    environment = os.environ.copy()
    environment.update(
        PIPELINE_WORK_ROOT=str(settings.data_dir / "restoration_work"),
        PIPELINE_CONTROL_FILE=str(control),
        SEEDVR2_MODEL=str(snapshot["model"]),
        SEEDVR2_RESOLUTION=str(snapshot["resolution"]),
        SEEDVR2_BATCH=str(snapshot["batch"]),
        SEEDVR2_CHUNK=str(snapshot["chunk"]),
        SEEDVR2_OVERLAP=str(snapshot["overlap"]),
        FORCE="0",
    )
    started = time.monotonic()
    last_progress = 0.0
    last_metrics = 0.0
    current_stage: str | None = "worker_checks"
    with log_partial.open("a", encoding="utf-8", buffering=1) as log_handle:
        log_handle.write(f"[{utc_now()}] worker command: pipeline_v3.sh <registered-source> {start:.3f} {duration:.3f} <generated-output> {job['public_id']}\n")
        process = subprocess.Popen(
            command,
            cwd=settings.project_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        while process.poll() is None:
            if _job_state(settings, job["id"]) == "cancel_requested":
                _touch_cancel(control)
            ready = selector.select(timeout=1)
            for key, _ in ready:
                line = key.fileobj.readline()
                if not line:
                    continue
                log_handle.write(line)
                clean = line.strip().replace("\r", "")
                if clean.startswith(PIPELINE_PREFIX):
                    try:
                        event = json.loads(clean.removeprefix(PIPELINE_PREFIX))
                        current_stage = event.get("stage", current_stage)
                        _pipeline_event(settings, job["id"], event, started)
                    except json.JSONDecodeError:
                        pass
                else:
                    match = PROGRESS_RE.search(clean)
                    now_mono = time.monotonic()
                    if match and now_mono - last_progress >= 2:
                        _progress_update(settings, job["id"], current_stage, int(match.group(1)), float(match.group(2)), started)
                        last_progress = now_mono
                    elif clean and not clean.startswith("frame="):
                        with connect(settings.database_path) as db:
                            append_event(db, job["id"], "log", state=_job_state(settings, job["id"]), stage=current_stage, message=clean[:4000])
            now_mono = time.monotonic()
            if now_mono - last_metrics >= 15:
                metrics = _system_metrics(settings)
                with connect(settings.database_path) as db:
                    append_event(db, job["id"], "metrics", state=_job_state(settings, job["id"]), stage=current_stage, payload=metrics)
                last_metrics = now_mono
        for line in process.stdout:
            log_handle.write(line)
        status = process.wait()
    os.replace(log_partial, log)
    control.unlink(missing_ok=True)
    with connect(settings.database_path) as db:
        db.execute(
            "UPDATE jobs SET elapsed_seconds=?, worker_pid=NULL, updated_at=? WHERE id=?",
            (time.monotonic() - started, utc_now(), job["id"]),
        )
    return status


def _probe_output(settings: Settings, output: Path) -> dict[str, Any]:
    relative = output.resolve().relative_to(settings.data_dir)
    result = subprocess.run(
        [
            "docker", "run", "--rm", "--network", "none",
            "-v", f"{settings.data_dir}:/data:ro",
            "--entrypoint", "ffprobe", settings.ffmpeg_image,
            "-v", "error", "-count_frames", "-show_entries",
            "format=duration:stream=codec_type,codec_name,pix_fmt,width,height,r_frame_rate,nb_read_frames,color_range,color_space,color_transfer,color_primaries,sample_rate,channels",
            "-of", "json", f"/data/{relative}",
        ],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def _register_file(settings: Settings, db, job_id: int, kind: str, path: Path, mime: str, media: dict[str, Any], duration_ms: int | None, frames: int | None) -> None:
    now = utc_now()
    db.execute(
        """INSERT INTO artifacts
           (job_id, kind, relative_path, mime_type, size_bytes, duration_ms,
            frame_count, validation_state, media_json, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'valid', ?, ?, ?)
           ON CONFLICT(relative_path) DO UPDATE SET size_bytes=excluded.size_bytes,
             duration_ms=excluded.duration_ms, frame_count=excluded.frame_count,
             validation_state='valid', media_json=excluded.media_json, updated_at=excluded.updated_at""",
        (
            job_id, kind, str(path.relative_to(settings.data_dir)), mime, path.stat().st_size,
            duration_ms, frames, json.dumps(media, sort_keys=True), now, now,
        ),
    )


def _validate_and_complete(settings: Settings, job) -> None:
    output = Path(job["output_path"])
    if not output.is_file() or output.stat().st_size == 0:
        raise WorkerError("Pipeline returned success without a restored output")
    media = _probe_output(settings, output)
    expected_duration = (job["source_end_ms"] - job["source_start_ms"]) / 1000
    actual_duration = float(media.get("format", {}).get("duration", 0))
    streams = media.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if abs(actual_duration - expected_duration) > 0.12:
        raise WorkerError(f"Output duration validation failed: expected {expected_duration:.3f}s, found {actual_duration:.3f}s")
    if not video or video.get("codec_name") != "hevc" or video.get("pix_fmt") != "yuv420p10le":
        raise WorkerError("Output video validation failed: expected 10-bit HEVC")
    if not audio or audio.get("codec_name") != "flac" or int(audio.get("sample_rate", 0)) != 48000:
        raise WorkerError("Output audio validation failed: expected 48 kHz FLAC")
    expected_frames = job["frames_total"]
    actual_frames = int(video.get("nb_read_frames") or 0)
    if actual_frames != expected_frames:
        raise WorkerError(f"Output frame validation failed: expected {expected_frames}, found {actual_frames}")
    baseline = Path(job["baseline_path"])
    log = Path(job["log_path"])
    with connect(settings.database_path) as db, transaction(db):
        _register_file(settings, db, job["id"], "restored_output", output, "video/x-matroska", media, round(actual_duration * 1000), actual_frames)
        if baseline.is_file():
            _register_file(settings, db, job["id"], "baseline", baseline, "video/x-matroska", {}, round(actual_duration * 1000), actual_frames)
        if log.is_file():
            _register_file(settings, db, job["id"], "pipeline_log", log, "text/plain", {}, None, None)
        now = utc_now()
        db.execute(
            """UPDATE jobs SET state='completed', stage='complete', frames_done=frames_total,
               fps=NULL, eta_seconds=0, error=NULL, completed_at=?, updated_at=? WHERE id=?""",
            (now, now, job["id"]),
        )
        append_event(
            db, job["id"], "state", state="completed", stage="complete",
            message=f"Validated {actual_frames} frames, {actual_duration:.3f}s, 10-bit HEVC with 48 kHz FLAC",
            payload={"frames": actual_frames, "duration_seconds": actual_duration},
        )


def _finish_error(settings: Settings, job_id: int, message: str, *, cancelled: bool = False) -> None:
    state = "cancelled" if cancelled else "failed"
    with connect(settings.database_path) as db, transaction(db):
        now = utc_now()
        db.execute(
            """UPDATE jobs SET state=?, error=?, worker_pid=NULL, start_requested=0,
               completed_at=?, updated_at=? WHERE id=?""",
            (state, None if cancelled else message, now, now, job_id),
        )
        append_event(db, job_id, "state", state=state, message=message)


def run_job(settings: Settings, job) -> None:
    free = shutil.disk_usage(settings.data_dir).free
    if free < settings.free_space_reserve_bytes:
        _finish_error(
            settings, job["id"],
            f"Free-space safeguard refused job: {free} bytes free, reserve is {settings.free_space_reserve_bytes} bytes",
        )
        return
    try:
        status = _run_pipeline(settings, job)
        if status == 75 or _job_state(settings, job["id"]) == "cancel_requested":
            _finish_error(settings, job["id"], "Cancellation completed at a pipeline stage boundary", cancelled=True)
        elif status != 0:
            _finish_error(settings, job["id"], f"pipeline_v3.sh exited with status {status}")
        else:
            _validate_and_complete(settings, job)
    except Exception as exc:
        _finish_error(settings, job["id"], str(exc))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Single-GPU wedding restoration worker")
    parser.add_argument("--once", action="store_true", help="process at most one eligible job and exit")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    settings = load_settings()
    migrate(settings.database_path)
    with GpuLock(settings.data_dir / "worker" / "gpu.lock"):
        _recover_interrupted(settings)
        while True:
            job = _claim_next(settings)
            if job is not None:
                print(f"Claimed {job['public_id']}: {job['display_name']}", flush=True)
                run_job(settings, job)
                if args.once:
                    break
            elif args.once:
                print("No started job is waiting", flush=True)
                break
            else:
                time.sleep(max(0.2, args.poll_seconds))


if __name__ == "__main__":
    main()

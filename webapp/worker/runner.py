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
import signal
import subprocess
import sys
import time
from typing import Any, Callable, TypeVar

from webapp.config import Settings, load_settings
from webapp.db import TRANSIENT_DB_ERRORS, connect, migrate, retry_db, transaction, utc_now
from webapp.server.services import append_event
from webapp.worker.idle import IdleGate, IdlePolicy, gpu_snapshot


PIPELINE_PREFIX = "PIPELINE_EVENT "
PROGRESS_RE = re.compile(r"frame=\s*(\d+).*?fps=\s*([0-9.]+)")
WRITTEN_RE = re.compile(r"Written\s+(\d+)/(\d+)\s+frames")
CHUNK_RE = re.compile(r"Chunk\s+(\d+)/(\d+):")
STREAM_COMPLETE_RE = re.compile(r"Streaming complete:\s+(\d+)\s+frames")
ACTIVE_STATES = ("preparing", "running", "assembling", "cancel_requested", "pause_requested", "resuming")

# Returned by _run_pipeline when the worker was asked to shut down (SIGTERM,
# e.g. `systemctl --user stop`) mid-run. The job is left in its active state so
# restart recovery requeues it, rather than being marked failed.
STATUS_SHUTDOWN = -100
# Returned by _run_units when a cooperative pause was requested: the current unit
# finished, the GPU is released, and the job is parked in 'paused' for resume.
STATUS_PAUSED = -101
# Returned by _run_units when the GPU is no longer free between units (a foreign
# workload returned): the job is requeued and the idle gate auto-resumes it once
# the GPU is idle again — non-interfering by design.
STATUS_YIELDED = -102

# Set by the SIGTERM/SIGINT handler so long-running loops can exit cleanly and
# release the GPU (container + VRAM) instead of being hard-killed.
_SHUTDOWN = False
# Name of the GPU container for the job currently running, so the signal handler
# and the _run_pipeline finally-path can stop it deterministically.
_ACTIVE_CONTAINER: str | None = None

T = TypeVar("T")


class WorkerError(RuntimeError):
    pass


def _handle_shutdown(signum, _frame) -> None:
    global _SHUTDOWN
    _SHUTDOWN = True
    print(f"Received signal {signum}; shutting down worker and releasing GPU", flush=True)
    if _ACTIVE_CONTAINER is not None:
        _stop_container(_ACTIVE_CONTAINER)
    # A cloud job's pods keep billing until deleted — terminate them on the way
    # out so a stopped worker never leaks paid GPUs.
    if _ACTIVE_FLEET is not None:
        try:
            _ACTIVE_FLEET.terminate_all()
        except Exception as exc:  # never let cleanup crash the handler
            print(f"fleet termination during shutdown failed: {exc}", flush=True)


def _install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)


def _container_name(public_id: str) -> str:
    return f"wedding-{public_id}"


def _stop_container(name: str) -> None:
    """Best-effort stop+remove of the GPU container. `docker stop` sends SIGTERM
    then SIGKILL after the grace period; `--rm` on `docker run` removes it."""
    try:
        subprocess.run(
            ["docker", "stop", "-t", "10", name],
            check=False, capture_output=True, text=True, timeout=40,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _best_effort(operation: Callable[[], T]) -> T | None:
    """Run an observational DB write, retrying briefly then swallowing failures.

    Progress, events, metrics and heartbeats must never crash the worker or, mid
    run, abort handling and orphan the GPU container. Durable state transitions
    (claim, complete, fail) do NOT use this — they must surface their errors."""
    try:
        return retry_db(operation, attempts=3, base_delay=0.2, max_delay=2.0)
    except Exception:
        return None


def _heartbeat(
    settings: Settings,
    activity: str,
    *,
    active_job_id: int | None = None,
    error: str | None = None,
    detail: str | None = None,
    started: bool = False,
) -> None:
    def op() -> None:
        with connect(settings.database_path) as db:
            now = utc_now()
            if started:
                db.execute(
                    "UPDATE worker_status SET pid=?, activity=?, active_job_id=?, detail=?, "
                    "last_beat_at=?, started_at=?, updated_at=? WHERE id=1",
                    (os.getpid(), activity, active_job_id, detail, now, now, now),
                )
            elif error is not None:
                db.execute(
                    "UPDATE worker_status SET pid=?, activity=?, active_job_id=?, detail=?, "
                    "last_beat_at=?, last_error=?, last_error_at=?, updated_at=? WHERE id=1",
                    (os.getpid(), activity, active_job_id, detail, now, error[:2000], now, now),
                )
            else:
                db.execute(
                    "UPDATE worker_status SET pid=?, activity=?, active_job_id=?, detail=?, "
                    "last_beat_at=?, updated_at=? WHERE id=1",
                    (os.getpid(), activity, active_job_id, detail, now, now),
                )
    _best_effort(op)


def _has_startable_job(settings: Settings) -> bool:
    with connect(settings.database_path) as db:
        row = db.execute(
            "SELECT 1 FROM jobs WHERE state='queued' AND start_requested=1 LIMIT 1"
        ).fetchone()
    return row is not None


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
    def op() -> str:
        with connect(settings.database_path) as db:
            row = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row["state"] if row else "cancel_requested"
    result = _best_effort(op)
    # On a transient DB failure, assume the job is still running: it is safer to
    # keep processing than to spuriously treat an unreadable state as a cancel.
    return result if result is not None else "running"


def _pipeline_event(settings: Settings, job_id: int, event: dict[str, str], started: float) -> None:
    event_type = event.get("type", "pipeline")
    stage = event.get("stage")

    def op() -> None:
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
    _best_effort(op)


def _progress_update(settings: Settings, job_id: int, stage: str | None, frame: int, fps: float, started: float) -> None:
    elapsed = time.monotonic() - started

    def op() -> None:
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
    _best_effort(op)


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
        PIPELINE_FREE_SPACE_RESERVE_BYTES=str(settings.free_space_reserve_bytes),
        SEEDVR2_MODEL=str(snapshot["model"]),
        SEEDVR2_RESOLUTION=str(snapshot["resolution"]),
        SEEDVR2_BATCH=str(snapshot["batch"]),
        SEEDVR2_CHUNK=str(snapshot["chunk"]),
        SEEDVR2_OVERLAP=str(snapshot["overlap"]),
        FORCE="0",
    )
    global _ACTIVE_CONTAINER
    started = time.monotonic()
    last_progress = 0.0
    last_metrics = 0.0
    current_stage: str | None = "worker_checks"
    current_chunk = 1
    restore_started: float | None = None
    container = _container_name(job["public_id"])
    status = STATUS_SHUTDOWN
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
        # From here on a GPU container may be launched by the pipeline; record
        # its name so the signal handler and the finally-path below can stop it
        # rather than orphan ~60 GB of VRAM.
        _ACTIVE_CONTAINER = container
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        while process.poll() is None:
            if _SHUTDOWN:
                break
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
                        if event.get("type") == "stage_start" and current_stage == "seedvr2_restore":
                            restore_started = time.monotonic()
                        _pipeline_event(settings, job["id"], event, started)
                    except json.JSONDecodeError:
                        pass
                else:
                    chunk_match = CHUNK_RE.search(clean)
                    if chunk_match:
                        current_chunk = int(chunk_match.group(1))
                    written_match = WRITTEN_RE.search(clean)
                    complete_match = STREAM_COMPLETE_RE.search(clean)
                    match = PROGRESS_RE.search(clean)
                    now_mono = time.monotonic()
                    if (written_match or complete_match) and restore_started is not None:
                        if complete_match:
                            restored_frames = int(complete_match.group(1))
                        else:
                            restored_frames = (current_chunk - 1) * int(snapshot["chunk"]) + int(written_match.group(1))
                        measured_fps = restored_frames / max(0.001, now_mono - restore_started)
                        _progress_update(settings, job["id"], current_stage, restored_frames, measured_fps, started)
                        last_progress = now_mono
                    elif match and now_mono - last_progress >= 2:
                        _progress_update(settings, job["id"], current_stage, int(match.group(1)), float(match.group(2)), started)
                        last_progress = now_mono
                    elif clean and not clean.startswith("frame="):
                        message = clean[:4000]

                        def _log_op(message=message):
                            with connect(settings.database_path) as db:
                                append_event(db, job["id"], "log", state=_job_state(settings, job["id"]), stage=current_stage, message=message)
                        _best_effort(_log_op)
            now_mono = time.monotonic()
            if now_mono - last_metrics >= 15:
                metrics = _system_metrics(settings)
                _heartbeat(settings, "running", active_job_id=job["id"])

                def _metrics_op(metrics=metrics):
                    with connect(settings.database_path) as db, transaction(db):
                        current = db.execute("SELECT state, eta_seconds FROM jobs WHERE id=?", (job["id"],)).fetchone()
                        elapsed = time.monotonic() - started
                        eta = max(0, (current["eta_seconds"] or 0) - 15) if current else None
                        db.execute(
                            "UPDATE jobs SET elapsed_seconds=?, eta_seconds=?, updated_at=? WHERE id=?",
                            (elapsed, eta, utc_now(), job["id"]),
                        )
                        append_event(
                            db, job["id"], "metrics", state=current["state"] if current else None,
                            stage=current_stage, payload=metrics,
                        )
                _best_effort(_metrics_op)
                last_metrics = now_mono
        if _SHUTDOWN:
            # Asked to stop mid-run: leave the job in its active state so restart
            # recovery requeues it. The signal handler already stops the GPU
            # container; run_job skips completion/failure for STATUS_SHUTDOWN.
            return STATUS_SHUTDOWN
        for line in process.stdout:
            log_handle.write(line)
        status = process.wait()
    os.replace(log_partial, log)
    control.unlink(missing_ok=True)

    def _clear_pid_op():
        with connect(settings.database_path) as db:
            db.execute(
                "UPDATE jobs SET elapsed_seconds=?, worker_pid=NULL, updated_at=? WHERE id=?",
                (time.monotonic() - started, utc_now(), job["id"]),
            )
    _best_effort(_clear_pid_op)
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
    # Re-read frames_total: the durable-unit path may have shrunk it to the
    # frames that actually exist in the source (tail-unit reconciliation),
    # and the claim-time row would be stale.
    with connect(settings.database_path) as db:
        expected_frames_row = db.execute(
            "SELECT frames_total FROM jobs WHERE id=?", (job["id"],)
        ).fetchone()
    expected_frames = expected_frames_row[0]
    expected_duration = (job["source_end_ms"] - job["source_start_ms"]) / 1000
    if expected_frames < round(expected_duration * 50):
        expected_duration = expected_frames / 50
    actual_duration = float(media.get("format", {}).get("duration", 0))
    streams = media.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if abs(actual_duration - expected_duration) > 0.12:
        raise WorkerError(f"Output duration validation failed: expected {expected_duration:.3f}s, found {actual_duration:.3f}s")
    if not video or video.get("codec_name") != "hevc" or video.get("pix_fmt") != "yuv420p10le":
        raise WorkerError("Output video validation failed: expected 10-bit HEVC")
    if (
        video.get("color_range") != "tv"
        or video.get("color_space") != "bt709"
        or video.get("color_transfer") != "bt709"
        or video.get("color_primaries") != "bt709"
    ):
        raise WorkerError("Output colour validation failed: expected limited-range BT.709 tags")
    if not audio or audio.get("codec_name") != "flac" or int(audio.get("sample_rate", 0)) != 48000:
        raise WorkerError("Output audio validation failed: expected 48 kHz FLAC")
    actual_frames = int(video.get("nb_read_frames") or 0)
    if actual_frames != expected_frames:
        raise WorkerError(f"Output frame validation failed: expected {expected_frames}, found {actual_frames}")
    baseline = Path(job["baseline_path"])
    log = Path(job["log_path"])
    with connect(settings.database_path) as db, transaction(db):
        _register_file(settings, db, job["id"], "restored_output", output, "video/x-matroska", media, round(actual_duration * 1000), actual_frames)
        if baseline.is_file():
            baseline_media = _probe_output(settings, baseline)
            baseline_video = next(
                (item for item in baseline_media.get("streams", []) if item.get("codec_type") == "video"), None
            )
            baseline_duration = float(baseline_media.get("format", {}).get("duration", 0))
            baseline_frames = int((baseline_video or {}).get("nb_read_frames") or 0)
            if abs(baseline_duration - expected_duration) > 0.12 or baseline_frames != expected_frames:
                raise WorkerError("Baseline validation failed: duration or frame count mismatch")
            _register_file(
                settings, db, job["id"], "baseline", baseline, "video/x-matroska",
                baseline_media, round(baseline_duration * 1000), baseline_frames,
            )
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


def _probe_frame_count(settings: Settings, path: Path) -> int:
    relative = path.resolve().relative_to(settings.data_dir)
    result = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "-v", f"{settings.data_dir}:/data:ro",
         "--entrypoint", "ffprobe", settings.ffmpeg_image, "-v", "error", "-count_frames",
         "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0",
         f"/data/{relative}"],
        check=True, capture_output=True, text=True,
    )
    return int(result.stdout.strip() or 0)


def _plan_units(frames_total: int, chunk: int, overlap: int) -> list[dict[str, int]]:
    """Split a job's output frames into durable units. Unit 0 uses a reversed
    warm-up (prepend); later units carry `overlap` raw context frames that are
    dropped from the output so each unit holds exactly its new frames."""
    units: list[dict[str, int]] = []
    seq = 0
    start = 0
    while start < frames_total:
        new = min(chunk, frames_total - start)
        if seq == 0:
            units.append(dict(seq=0, start=0, new=new, skip=0, cap=new, prepend=overlap, drop=0, ctx=0))
        else:
            units.append(dict(seq=seq, start=start, new=new, skip=start - overlap,
                              cap=new + overlap, prepend=0, drop=overlap, ctx=overlap))
        start += new
        seq += 1
    return units


def _record_chunk(settings, job_id, unit, unit_path: Path, state: str, frame_count: int | None = None) -> None:
    relative = str(unit_path.resolve().relative_to(settings.data_dir))

    def op() -> None:
        with connect(settings.database_path) as db, transaction(db):
            now = utc_now()
            db.execute(
                """INSERT INTO job_chunks
                   (job_id, sequence, source_start_ms, source_end_ms, context_before_frames,
                    warmup_frames, state, frame_count, artifact_path, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_id, sequence) DO UPDATE SET state=excluded.state,
                     frame_count=excluded.frame_count, artifact_path=excluded.artifact_path,
                     updated_at=excluded.updated_at""",
                (job_id, unit["seq"], round(unit["start"] * 20), round((unit["start"] + unit["new"]) * 20),
                 unit["ctx"], unit["prepend"], state, frame_count, relative, now, now),
            )
    retry_db(op, attempts=5, base_delay=0.3)


def _chunk_valid(settings: Settings, job_id: int, unit, unit_path: Path) -> bool:
    """Resume guard: a unit is reusable only if its row is 'valid', the file
    exists, and its actual frame count matches — never trust the row alone."""
    if not unit_path.is_file() or unit_path.stat().st_size == 0:
        return False
    with connect(settings.database_path) as db:
        row = db.execute(
            "SELECT state, frame_count FROM job_chunks WHERE job_id=? AND sequence=?",
            (job_id, unit["seq"]),
        ).fetchone()
    if not row or row["state"] != "valid" or row["frame_count"] != unit["new"]:
        return False
    try:
        return _probe_frame_count(settings, unit_path) == unit["new"]
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def _set_frames_total(settings: Settings, job_id: int, frames_total: int) -> None:
    """Shrink a job to the frames that actually exist in its deinterlaced
    source (DVD chapters do not always cut on exact frame boundaries)."""
    def op() -> None:
        with connect(settings.database_path) as db, transaction(db):
            db.execute(
                "UPDATE jobs SET frames_total=?, updated_at=? WHERE id=?",
                (frames_total, utc_now(), job_id),
            )
    retry_db(op, attempts=5, base_delay=0.3)


def _set_state(settings: Settings, job_id: int, state: str, stage: str | None = None) -> None:
    def op() -> None:
        with connect(settings.database_path) as db, transaction(db):
            db.execute(
                "UPDATE jobs SET state=?, stage=COALESCE(?, stage), updated_at=? WHERE id=?",
                (state, stage, utc_now(), job_id),
            )
            append_event(db, job_id, "state", state=state, stage=stage)
    retry_db(op, attempts=5, base_delay=0.3)


def _yield_job(settings: Settings, job_id: int, reason: str) -> None:
    """Requeue a job that yielded the GPU between units; the idle gate resumes it."""
    def op() -> None:
        with connect(settings.database_path) as db, transaction(db):
            db.execute(
                "UPDATE jobs SET state='queued', start_requested=1, worker_pid=NULL, updated_at=? WHERE id=?",
                (utc_now(), job_id),
            )
            append_event(db, job_id, "state", state="queued", message=f"Yielded GPU between units ({reason}); will resume when idle")
    retry_db(op, attempts=5, base_delay=0.3)


def _should_yield_between_units(policy: IdlePolicy) -> tuple[bool, str]:
    """Between units the current unit's container has exited, so a shortfall of
    free VRAM means a foreign workload returned. Confirm once to avoid a
    transient post-exit reading before deciding to yield."""
    snap = gpu_snapshot()
    if not snap.ok or snap.free_mib >= policy.min_free_mib:
        return False, ""
    time.sleep(3)
    snap = gpu_snapshot()
    if not snap.ok or snap.free_mib >= policy.min_free_mib:
        return False, ""
    return True, f"only {snap.free_mib // 1024} GiB VRAM free"


def _pause_job(settings: Settings, job_id: int) -> None:
    def op() -> None:
        with connect(settings.database_path) as db, transaction(db):
            now = utc_now()
            db.execute(
                "UPDATE jobs SET state='paused', worker_pid=NULL, start_requested=0, updated_at=? WHERE id=?",
                (now, job_id),
            )
            append_event(db, job_id, "state", state="paused", message="Paused after completing the current unit; resume to continue")
    retry_db(op, attempts=5, base_delay=0.3)


def _units_frames_done(settings: Settings, job_id: int) -> int | None:
    total: int | None = None

    def op() -> None:
        nonlocal total
        with connect(settings.database_path) as db, transaction(db):
            total = db.execute(
                "SELECT COALESCE(SUM(frame_count), 0) FROM job_chunks WHERE job_id=? AND state='valid'",
                (job_id,),
            ).fetchone()[0]
            db.execute("UPDATE jobs SET frames_done=?, updated_at=? WHERE id=?", (total, utc_now(), job_id))
    _best_effort(op)
    return total


def _unit_progress(settings: Settings, job, frames_done: int, run_started: float, frames_at_start: int) -> None:
    """Refresh fps/elapsed/ETA from durable-unit throughput. The line-parsing
    progress updates in _run_pipeline only serve the legacy monolithic path,
    so without this a durable job shows "ETA pending" for its whole run. Uses
    the 0.71 fps planning rate until the first unit of this run completes."""
    elapsed = time.monotonic() - run_started
    produced = frames_done - frames_at_start
    fps = produced / elapsed if produced > 0 and elapsed > 0 else None
    remaining = max(0, job["frames_total"] - frames_done)
    eta = remaining / fps if fps else remaining / 0.71

    def op() -> None:
        with connect(settings.database_path) as db, transaction(db):
            db.execute(
                "UPDATE jobs SET fps=?, elapsed_seconds=?, eta_seconds=?, updated_at=? WHERE id=?",
                (round(fps, 3) if fps else None, round(elapsed, 1), round(eta, 1), utc_now(), job["id"]),
            )
    _best_effort(op)


def _run_logged(settings: Settings, job, command: list[str], env: dict, log_path: Path,
                heartbeat_detail: str | None = None) -> int:
    """Run a child process, tee its output to the job log, and honour shutdown
    (release the GPU) and cooperative cancel between the pipeline's stages. Beats
    the worker heartbeat periodically so the health signal stays fresh during a
    unit's long run (a unit takes ~15 min; the UI marks the worker down after 90s
    without a beat)."""
    control = settings.data_dir / "control" / f"{job['public_id']}.cancel"
    last_beat = time.monotonic()
    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        process = subprocess.Popen(
            command, cwd=settings.project_root, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while process.poll() is None:
                if _SHUTDOWN:
                    _stop_container(_container_name(job["public_id"]))
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    return STATUS_SHUTDOWN
                if _job_state(settings, job["id"]) == "cancel_requested":
                    _touch_cancel(control)
                if time.monotonic() - last_beat >= 20:
                    _heartbeat(settings, "running", active_job_id=job["id"], detail=heartbeat_detail)
                    last_beat = time.monotonic()
                for key, _ in selector.select(timeout=1):
                    line = key.fileobj.readline()
                    if line:
                        log.write(line)
            for line in process.stdout:
                log.write(line)
            status = process.wait()
            # If a shutdown was requested, report shutdown even if the child
            # exited on its own SIGTERM first (race): the job must be left for
            # restart recovery, not marked failed.
            if _SHUTDOWN:
                return STATUS_SHUTDOWN
            return status
        finally:
            selector.close()
            if process.poll() is None:
                _stop_container(_container_name(job["public_id"]))
                process.terminate()


def _run_unit(settings, job, source, start_sec, duration, unit, unit_path: Path, log_path: Path,
              heartbeat_detail: str | None = None) -> int:
    snapshot = json.loads(job["settings_json"])
    env = os.environ.copy()
    env.update(
        PIPELINE_WORK_ROOT=str(settings.data_dir / "restoration_work"),
        PIPELINE_FREE_SPACE_RESERVE_BYTES=str(settings.free_space_reserve_bytes),
        SEEDVR2_MODEL=str(snapshot["model"]), SEEDVR2_RESOLUTION=str(snapshot["resolution"]),
        SEEDVR2_BATCH=str(snapshot["batch"]), SEEDVR2_CHUNK=str(snapshot["chunk"]),
        SEEDVR2_OVERLAP=str(snapshot["overlap"]), FORCE="0",
        UNIT_OUTPUT=str(unit_path), UNIT_SKIP=str(unit["skip"]), UNIT_LOAD_CAP=str(unit["cap"]),
        UNIT_PREPEND=str(unit["prepend"]), UNIT_DROP=str(unit["drop"]),
    )
    command = [str(settings.pipeline_path), str(source), f"{start_sec:.3f}", f"{duration:.3f}",
               str(job["output_path"]), job["public_id"]]
    return _run_logged(settings, job, command, env, log_path, heartbeat_detail=heartbeat_detail)


def _assemble(settings, job, manifest: Path, source, start_sec, duration, log_path: Path) -> int:
    env = os.environ.copy()
    env.setdefault("WEBAPP_FFMPEG_IMAGE", "upscaler-cuda:latest")
    command = [str(settings.project_root / "assemble_units.sh"), str(manifest), str(source),
               f"{start_sec:.3f}", f"{duration:.3f}", str(job["output_path"])]
    return _run_logged(settings, job, command, env, log_path, heartbeat_detail="assembling")


def _run_units(settings: Settings, job) -> int:
    """Durable-unit restoration: restore each unit as an independent SeedVR2 run,
    record it in job_chunks, and assemble the validated units losslessly. Resumes
    from the first non-valid unit; releases the GPU cleanly on pause/yield."""
    global _ACTIVE_CONTAINER
    source = _allowed_source(settings, job["source_cache_path"])
    output = Path(job["output_path"]).resolve()
    log = Path(job["log_path"]).resolve()
    for generated in (output, log):
        if not generated.is_relative_to(settings.data_dir):
            raise WorkerError("Generated job path escaped the application data directory")
        generated.parent.mkdir(parents=True, exist_ok=True)
    duration = (job["source_end_ms"] - job["source_start_ms"]) / 1000
    start_sec = job["source_start_ms"] / 1000
    snapshot = json.loads(job["settings_json"])
    chunk = int(snapshot["chunk"])
    overlap = int(snapshot["overlap"])
    units = _plan_units(job["frames_total"], chunk, overlap)
    work = settings.data_dir / "restoration_work" / job["public_id"]
    units_dir = work / "units"
    units_dir.mkdir(parents=True, exist_ok=True)
    container = _container_name(job["public_id"])
    idle_gate_on = os.environ.get("WEDDING_IDLE_GATE") == "1"
    idle_policy = IdlePolicy.from_env()
    _set_state(settings, job["id"], "running", "seedvr2_restore")
    run_started: float | None = None
    frames_at_start = 0

    for unit in units:
        if _SHUTDOWN:
            return STATUS_SHUTDOWN
        state = _job_state(settings, job["id"])
        if state == "cancel_requested":
            return 75
        if state == "pause_requested":
            return STATUS_PAUSED
        # Non-interference: if a foreign workload reclaimed the GPU while the
        # previous unit ran, yield now (the container has exited, VRAM is freed)
        # and let the idle gate resume us later. Skip the check for unit 0 (we
        # only got here because the gate was already open).
        if idle_gate_on and unit["seq"] > 0:
            yield_now, reason = _should_yield_between_units(idle_policy)
            if yield_now:
                _yield_job(settings, job["id"], reason)
                return STATUS_YIELDED
        unit_path = units_dir / f"unit_{unit['seq']:05d}.mkv"
        # Beat while re-validating resumed units: each probe is a docker-run
        # ffprobe (~5-8 s), so a long resume otherwise goes silent for minutes
        # and the UI pill falsely reports the worker down (90 s threshold).
        _heartbeat(settings, "running", active_job_id=job["id"],
                   detail=f"validating unit {unit['seq'] + 1}/{len(units)}")
        if _chunk_valid(settings, job["id"], unit, unit_path):
            _units_frames_done(settings, job["id"])
            continue
        if run_started is None:
            # First unit this run actually executes: measure throughput from
            # here (skipped-valid units would otherwise inflate the rate) and
            # seed a fallback ETA so the UI never sits on "ETA pending".
            run_started = time.monotonic()
            frames_at_start = _units_frames_done(settings, job["id"]) or 0
            _unit_progress(settings, job, frames_at_start, run_started, frames_at_start)
        detail = f"unit {unit['seq'] + 1}/{len(units)}"
        _record_chunk(settings, job["id"], unit, unit_path, "running")
        _heartbeat(settings, "running", active_job_id=job["id"], detail=detail)
        _ACTIVE_CONTAINER = container
        status = _run_unit(settings, job, source, start_sec, duration, unit, unit_path, log,
                           heartbeat_detail=detail)
        _ACTIVE_CONTAINER = None
        if status == STATUS_SHUTDOWN or _SHUTDOWN:
            return STATUS_SHUTDOWN
        if status == 75:
            return 75
        if status != 0:
            _record_chunk(settings, job["id"], unit, unit_path, "invalid")
            raise WorkerError(f"Unit {unit['seq']} failed with status {status}")
        actual = _probe_frame_count(settings, unit_path)
        if actual != unit["new"]:
            shortfall = unit["new"] - actual
            if unit["seq"] == len(units) - 1 and 0 < shortfall <= 50:
                # DVD chapters do not always cut on exact frame boundaries, so
                # the deinterlaced source can run a few frames short of the
                # catalog's timestamp arithmetic. The tail unit already holds
                # every frame that exists — accept it and shrink the job to
                # reality instead of failing at 99% over phantom frames
                # (DVD2 Ch7 died 4 frames short of a 62,830-frame plan).
                _set_frames_total(settings, job["id"], unit["start"] + actual)
                unit = {**unit, "new": actual}
            else:
                _record_chunk(settings, job["id"], unit, unit_path, "invalid")
                raise WorkerError(f"Unit {unit['seq']} produced {actual} frames, expected {unit['new']}")
        _record_chunk(settings, job["id"], unit, unit_path, "valid", frame_count=actual)
        done = _units_frames_done(settings, job["id"])
        if done is not None and run_started is not None:
            _unit_progress(settings, job, done, run_started, frames_at_start)

    if _SHUTDOWN:
        return STATUS_SHUTDOWN
    state = _job_state(settings, job["id"])
    if state == "cancel_requested":
        return 75
    if state == "pause_requested":
        return STATUS_PAUSED
    manifest = work / "units.manifest"
    manifest.write_text("\n".join(str(units_dir / f"unit_{u['seq']:05d}.mkv") for u in units) + "\n")
    _set_state(settings, job["id"], "assembling", "audio_mux")
    status = _assemble(settings, job, manifest, source, start_sec, duration, log)
    if status == STATUS_SHUTDOWN:
        return STATUS_SHUTDOWN
    if status != 0:
        raise WorkerError(f"Assembly failed with status {status}")
    return 0


# ── Cloud fan-out ────────────────────────────────────────────────────────────
# When WEDDING_EXECUTOR=cloud, durable units are dispatched to rented RunPod GPUs
# instead of the local card. Stage 1 (deinterlace) still runs locally on CPU; its
# intra-only FFV1 output is sliced per unit and shipped. Assembly and validation
# stay local and unchanged. The control plane never leaves the homeserver.

_ACTIVE_FLEET = None  # set while a cloud job runs, so shutdown terminates its pods


def _cloud_enabled() -> bool:
    return os.environ.get("WEDDING_EXECUTOR", "local").lower() == "cloud"


def _reconcile_cloud_pods(settings: Settings) -> None:
    """On worker startup terminate every pod this project owns, and mark the
    ledger terminated. A cloud job never survives a worker restart (its in-memory
    thread pool is gone), so any live pod is an orphan billing money — kill it.
    The job itself is requeued by _recover_interrupted and resumes from its valid
    units on fresh pods. This is the money-safety analogue of interrupted-job
    recovery."""
    try:
        from webapp.cloud.runpod_api import RunpodClient, RunpodError
        client = RunpodClient()
        killed = 0
        for pod in client.our_pods():
            try:
                client.terminate_pod(pod["id"])
                killed += 1
            except RunpodError as exc:
                print(f"reconcile: failed to terminate orphan pod {pod.get('id')}: {exc}", flush=True)
        if killed:
            print(f"reconcile: terminated {killed} orphaned cloud pod(s) from a previous run", flush=True)
        with connect(settings.database_path) as db, transaction(db):
            db.execute(
                "UPDATE cloud_pods SET state='terminated', terminated_at=? WHERE state != 'terminated'",
                (utc_now(),),
            )
    except Exception as exc:  # never block startup on cloud reconciliation
        print(f"reconcile: cloud pod reconciliation skipped: {exc}", flush=True)


def _run_prepare(settings: Settings, job, source, start_sec: float, duration: float, log_path: Path) -> int:
    """Run stage-1 deinterlace locally (PREPARE_ONLY) so the FFV1 intermediate
    exists to slice. Reuses pipeline_v3.sh so the exact pinned deinterlace command
    is shared with the local path."""
    env = os.environ.copy()
    env.update(
        PIPELINE_WORK_ROOT=str(settings.data_dir / "restoration_work"),
        PIPELINE_FREE_SPACE_RESERVE_BYTES=str(settings.free_space_reserve_bytes),
        PREPARE_ONLY="1", FORCE="0",
    )
    command = [str(settings.pipeline_path), str(source), f"{start_sec:.3f}", f"{duration:.3f}",
               str(job["output_path"]), job["public_id"]]
    return _run_logged(settings, job, command, env, log_path,
                       heartbeat_detail="preparing 50p (deinterlace)")


def _stage_seedvr2_tree(settings: Settings) -> Path:
    """docker cp the already-patched /opt/SeedVR2 out of the pinned image into the
    pod provisioning dir, so every pod gets byte-identical code (pinned commit +
    all three patches) rather than re-cloning. Staged once per worker run."""
    image = os.environ.get("SEEDVR2_IMAGE", "seedvr2-cuda:v3")
    pdir = settings.project_root / "docker" / "seedvr2-pod"
    dest = pdir / "_tree" / "SeedVR2"
    if (dest / "inference_cli.py").is_file():
        return pdir
    dest.parent.mkdir(parents=True, exist_ok=True)
    cid = subprocess.run(["docker", "create", image], check=True, capture_output=True, text=True).stdout.strip()
    try:
        subprocess.run(["docker", "cp", f"{cid}:/opt/SeedVR2", str(dest)], check=True, capture_output=True)
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
    if not (dest / "inference_cli.py").is_file():
        raise WorkerError(f"staged SeedVR2 tree from {image} has no inference_cli.py")
    return pdir


def _cloud_fleet_config(settings: Settings):
    from webapp.cloud.fleet import FleetConfig
    from webapp.cloud.runpod_api import STOCK_BASE_IMAGE
    prefs = os.environ.get(
        "WEDDING_CLOUD_GPU_PREFERENCE",
        "NVIDIA RTX PRO 6000 Blackwell Server Edition",
    )
    gpu_ids = [p.strip() for p in prefs.split(",") if p.strip()]
    provision_dir = _stage_seedvr2_tree(settings)
    cache = settings.data_dir / "restoration_work" / ".inductor_cache"
    return FleetConfig(
        image=os.environ.get("WEDDING_CLOUD_IMAGE", STOCK_BASE_IMAGE),
        gpu_type_ids=gpu_ids,
        cloud_type=os.environ.get("WEDDING_CLOUD_TIER", "SECURE"),
        max_slots=int(os.environ.get("WEDDING_CLOUD_MAX_SLOTS", "16")),
        spend_cap_usd=float(os.environ.get("WEDDING_CLOUD_SPEND_CAP_USD", "250")),
        provision_dir=provision_dir,
        inductor_cache=cache if cache.is_dir() else None,
    )


def _run_units_cloud(settings: Settings, job) -> int:
    """Restore a job's units concurrently across a fleet of rented GPUs, then
    assemble locally. Mirrors _run_units' durable/resume/validation contract; the
    only difference is many units run at once on pods instead of one at a time
    on the local card."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from webapp.cloud.fleet import CloudFleet
    from webapp.cloud.executor import run_unit_remote
    from webapp.worker.slicer import slice_unit, SliceError
    global _ACTIVE_FLEET

    source = _allowed_source(settings, job["source_cache_path"])
    output = Path(job["output_path"]).resolve()
    log = Path(job["log_path"]).resolve()
    for generated in (output, log):
        if not generated.is_relative_to(settings.data_dir):
            raise WorkerError("Generated job path escaped the application data directory")
        generated.parent.mkdir(parents=True, exist_ok=True)
    duration = (job["source_end_ms"] - job["source_start_ms"]) / 1000
    start_sec = job["source_start_ms"] / 1000
    snapshot = json.loads(job["settings_json"])
    chunk, overlap = int(snapshot["chunk"]), int(snapshot["overlap"])
    model, resolution, batch = str(snapshot["model"]), int(snapshot["resolution"]), int(snapshot["batch"])
    units = _plan_units(job["frames_total"], chunk, overlap)
    work = settings.data_dir / "restoration_work" / job["public_id"]
    units_dir = work / "units"
    slices_dir = work / "slices"
    stage1 = work / "input_50p_ffv1.mkv"
    units_dir.mkdir(parents=True, exist_ok=True)
    slices_dir.mkdir(parents=True, exist_ok=True)

    _set_state(settings, job["id"], "running", "seedvr2_restore")

    # Stage 1 locally (CPU) if not already present, so slices can be cut.
    if not stage1.is_file():
        _heartbeat(settings, "running", active_job_id=job["id"], detail="preparing 50p (deinterlace)")
        rc = _run_prepare(settings, job, source, start_sec, duration, log)
        if rc == STATUS_SHUTDOWN:
            return STATUS_SHUTDOWN
        if rc != 0 or not stage1.is_file():
            raise WorkerError(f"stage-1 prepare failed (status {rc})")

    def stop_reason() -> str | None:
        if _SHUTDOWN:
            return "shutdown"
        state = _job_state(settings, job["id"])
        if state == "cancel_requested":
            return "cancel"
        if state == "pause_requested":
            return "pause"
        return None

    # Resume: skip units already valid on disk.
    pending = []
    for unit in units:
        upath = units_dir / f"unit_{unit['seq']:05d}.mkv"
        if _chunk_valid(settings, job["id"], unit, upath):
            _units_frames_done(settings, job["id"])
        else:
            pending.append(unit)

    if pending:
        reason = stop_reason()
        if reason == "cancel":
            return 75
        if reason == "pause":
            return STATUS_PAUSED
        if reason == "shutdown":
            return STATUS_SHUTDOWN

        cfg = _cloud_fleet_config(settings)
        n_slots = min(cfg.max_slots, len(pending))
        run_started = time.monotonic()
        frames_at_start = _units_frames_done(settings, job["id"]) or 0
        _unit_progress(settings, job, frames_at_start, run_started, frames_at_start)
        errors: list[str] = []
        # Units still waiting to acquire a slot. When this hits zero, a pod that
        # finishes its unit has no more work, so it retires immediately instead
        # of idling (and billing) until the whole job ends.
        import threading as _threading
        dispatch_lock = _threading.Lock()
        to_dispatch = [len(pending)]

        def process(unit) -> None:
            if stop_reason():
                return
            upath = units_dir / f"unit_{unit['seq']:05d}.mkv"
            spath = slices_dir / f"unit_{unit['seq']:05d}.mkv"
            # Wait as long as the fleet can still hand out a slot; units queue for
            # slots by design (many more units than pods). None => aborted or no
            # pod will ever come up.
            slot = fleet.acquire_slot(stop_check=lambda: stop_reason() is not None)
            if slot is None:
                if stop_reason():
                    return
                raise WorkerError("no cloud GPU became available")
            with dispatch_lock:
                to_dispatch[0] -= 1  # this unit no longer needs a slot
            try:
                if stop_reason():
                    return
                slice_unit(stage1, unit["skip"], unit["cap"], spath,
                           ffmpeg_image=settings.ffmpeg_image, data_dir=settings.data_dir)
                _record_chunk(settings, job["id"], unit, upath, "running")
                _heartbeat(settings, "running", active_job_id=job["id"],
                           detail=f"cloud: {fleet.ready}/{n_slots} slots, unit {unit['seq'] + 1}/{len(units)}")
                res = run_unit_remote(
                    slot.endpoint, cfg.ssh_key, slice_path=spath, local_out=upath, unit=unit,
                    model=model, resolution=resolution, batch=batch, overlap=overlap,
                    log_path=log, should_abort=lambda: stop_reason() is not None,
                )
                spath.unlink(missing_ok=True)
                if res.status == -1:  # aborted (stop requested)
                    return
                if res.status != 0:
                    _record_chunk(settings, job["id"], unit, upath, "invalid")
                    raise WorkerError(f"unit {unit['seq']} failed on pod: {res.message}")
                actual = _probe_frame_count(settings, upath)
                local_unit = unit
                if actual != unit["new"]:
                    shortfall = unit["new"] - actual
                    if unit["seq"] == len(units) - 1 and 0 < shortfall <= 50:
                        _set_frames_total(settings, job["id"], unit["start"] + actual)
                        local_unit = {**unit, "new": actual}
                    else:
                        _record_chunk(settings, job["id"], unit, upath, "invalid")
                        raise WorkerError(f"unit {unit['seq']} produced {actual} frames, expected {unit['new']}")
                _record_chunk(settings, job["id"], local_unit, upath, "valid", frame_count=actual)
                done = _units_frames_done(settings, job["id"])
                if done is not None:
                    _unit_progress(settings, job, done, run_started, frames_at_start)
            finally:
                if slot is not None:
                    # More units still waiting for a slot -> hand it back.
                    # None left -> retire the pod now so it stops billing.
                    with dispatch_lock:
                        more_work = to_dispatch[0] > 0
                    if more_work:
                        fleet.slot_queue.put(slot)
                    else:
                        fleet.retire_slot(slot)

        with CloudFleet(settings, job, cfg, log=lambda m: print(m, flush=True)) as fleet:
            _ACTIVE_FLEET = fleet
            try:
                fleet.provision(n_slots)
                with ThreadPoolExecutor(max_workers=n_slots) as ex:
                    futures = {ex.submit(process, u): u for u in pending}
                    for fut in as_completed(futures):
                        exc = fut.exception()
                        if exc is not None:
                            errors.append(str(exc))
            finally:
                _ACTIVE_FLEET = None

        reason = stop_reason()
        if reason == "shutdown":
            return STATUS_SHUTDOWN
        if reason == "cancel":
            return 75
        if reason == "pause":
            return STATUS_PAUSED
        if errors:
            raise WorkerError(f"{len(errors)} unit(s) failed: {errors[0]}")

    # All units valid → assemble locally (lossless concat + FLAC mux).
    reason = stop_reason()
    if reason == "shutdown":
        return STATUS_SHUTDOWN
    if reason == "cancel":
        return 75
    if reason == "pause":
        return STATUS_PAUSED
    manifest = work / "units.manifest"
    manifest.write_text("\n".join(str(units_dir / f"unit_{u['seq']:05d}.mkv") for u in units) + "\n")
    _set_state(settings, job["id"], "assembling", "audio_mux")
    status = _assemble(settings, job, manifest, source, start_sec, duration, log)
    if status == STATUS_SHUTDOWN:
        return STATUS_SHUTDOWN
    if status != 0:
        raise WorkerError(f"Assembly failed with status {status}")
    return 0


def run_job(settings: Settings, job) -> None:
    free = shutil.disk_usage(settings.data_dir).free
    if free < settings.free_space_reserve_bytes:
        _finish_error(
            settings, job["id"],
            f"Free-space safeguard refused job: {free} bytes free, reserve is {settings.free_space_reserve_bytes} bytes",
        )
        return
    global _ACTIVE_CONTAINER
    container = _container_name(job["public_id"])
    durable = os.environ.get("WEDDING_DURABLE_UNITS") == "1"
    try:
        if durable and _cloud_enabled():
            status = _run_units_cloud(settings, job)
        elif durable:
            status = _run_units(settings, job)
        else:
            status = _run_pipeline(settings, job)
    except Exception as exc:
        # An unexpected failure must not leave the GPU container (and ~60 GB of
        # VRAM) running while the job is marked failed.
        _stop_container(container)
        _finish_error(settings, job["id"], str(exc))
        return
    finally:
        _ACTIVE_CONTAINER = None
    if status == STATUS_SHUTDOWN:
        # Worker is stopping mid-run; leave the job active so restart recovery
        # requeues it for major-stage (later, durable-unit) resume.
        return
    if status == STATUS_PAUSED:
        # Cooperative pause: current unit is durably saved; park in 'paused'.
        _pause_job(settings, job["id"])
        return
    if status == STATUS_YIELDED:
        # Auto-yielded between units; already requeued for the idle gate.
        _heartbeat(settings, "idle", detail="yielded GPU; waiting for idle to resume")
        return
    if status == 75 or _job_state(settings, job["id"]) == "cancel_requested":
        _finish_error(settings, job["id"], "Cancellation completed at a pipeline stage boundary", cancelled=True)
    elif status != 0:
        _finish_error(settings, job["id"], f"pipeline_v3.sh exited with status {status}")
    else:
        try:
            _validate_and_complete(settings, job)
        except Exception as exc:
            _finish_error(settings, job["id"], str(exc))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Single-GPU wedding restoration worker")
    parser.add_argument("--once", action="store_true", help="process at most one eligible job and exit")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    settings = load_settings()
    _install_signal_handlers()
    # The database may be briefly unavailable right after a reboot (filesystem
    # not mounted yet). Retry indefinitely instead of dying — this was the
    # historical "worker stopped after SQLite-open errors following a reboot".
    retry_db(
        lambda: migrate(settings.database_path),
        attempts=0, base_delay=1.0, max_delay=30.0,
        on_error=lambda exc, attempt, delay: print(
            f"Database not ready (attempt {attempt}); retrying in {delay:.0f}s: {exc}", flush=True
        ),
    )
    # Passive idle gate: when enabled, only claim a job once the GPU has been
    # continuously idle (no foreign compute process, enough free VRAM) for the
    # dwell time. Prevents starting SeedVR2 into a busy GPU and OOMing — exactly
    # the failure seen when the torrent stack's ollama holds ~56 GiB.
    # The idle gate protects the LOCAL card from being claimed while a foreign
    # workload holds VRAM. Cloud units run on rented GPUs, so the local card is
    # irrelevant — never gate cloud jobs on local idleness.
    idle_gate = (
        IdleGate(IdlePolicy.from_env())
        if os.environ.get("WEDDING_IDLE_GATE") == "1" and not _cloud_enabled()
        else None
    )
    gate_poll_seconds = 2.0
    with GpuLock(settings.data_dir / "worker" / "gpu.lock"):
        _heartbeat(settings, "idle", started=True)
        try:
            retry_db(lambda: _recover_interrupted(settings), attempts=5, base_delay=0.5)
        except Exception as exc:
            _heartbeat(settings, "idle", error=f"interrupted-job recovery failed: {exc}")
            print(f"Recovery failed (continuing): {exc}", flush=True)
        # Kill any cloud pods orphaned by a previous crash before claiming work.
        if _cloud_enabled():
            _reconcile_cloud_pods(settings)
        backoff = max(0.2, args.poll_seconds)
        while not _SHUTDOWN:
            # Idle gate: if work is waiting but the GPU is not idle enough, hold
            # off claiming and report why, rather than starting and OOMing.
            if idle_gate is not None and not args.once:
                startable = _best_effort(lambda: _has_startable_job(settings))
                if startable and not idle_gate.poll():
                    _heartbeat(settings, "waiting", detail=idle_gate.last_reason)
                    time.sleep(gate_poll_seconds)
                    continue
            try:
                job = retry_db(lambda: _claim_next(settings), attempts=5, base_delay=0.5, max_delay=10.0)
            except TRANSIENT_DB_ERRORS as exc:
                _heartbeat(settings, "db_error", error=str(exc))
                print(f"Job claim failed after retries; backing off {backoff:.0f}s: {exc}", flush=True)
                time.sleep(min(30.0, backoff))
                backoff = min(30.0, backoff * 2)
                continue
            backoff = max(0.2, args.poll_seconds)
            if job is not None:
                print(f"Claimed {job['public_id']}: {job['display_name']}", flush=True)
                _heartbeat(settings, "running", active_job_id=job["id"])
                run_job(settings, job)
                _heartbeat(settings, "idle")
                if args.once:
                    break
            elif args.once:
                print("No started job is waiting", flush=True)
                break
            else:
                time.sleep(max(0.2, args.poll_seconds))
        if _SHUTDOWN:
            _heartbeat(settings, "stopped")


if __name__ == "__main__":
    main()

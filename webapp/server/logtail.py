"""Best-effort live-progress extraction from a restoration job's SeedVR2 log.

Both the local worker and the cloud pod executor (webapp/worker, webapp/cloud
— read-only from here) stream the same SeedVR2 stdout into ``jobs.log_path``.
The database only records ``jobs.frames_done`` once per ~750-frame durable
unit, so a job in progress can look frozen for ~15 minutes at a time even
though the GPU is actively working. This module tails the last chunk of the
log file and pulls out the phase/batch/frame-write markers SeedVR2 already
prints, purely for display. It never raises: a missing, empty, or unreadable
log just means "nothing to show yet", which is exactly the state a job that
hasn't started producing output yet is in.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import re

# A durable unit's SeedVR2 output runs from a few hundred KB to low-MB of
# interleaved stdout (local: one unit at a time; cloud: several concurrent
# pods sharing one job log). Tailing this many bytes comfortably covers the
# current unit's phase/batch history without reading multi-MB files on every
# 3-5s poll.
TAIL_BYTES = 200_000

_TS_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2}\.\d{3})\]")
_STAGE_RE = re.compile(r'PIPELINE_EVENT \{"type":"(stage_start|stage_complete)","stage":"([a-z0-9_]+)"\}')
_BATCH_RE = re.compile(r"(Encoding|Upscaling|Decoding) batch (\d+)/(\d+)")
_WRITE_RE = re.compile(r"Wrote (\d+) frames to positions (\d+)-(\d+)")
# Cloud pods announce which unit they are handling and over which host:port
# they were reached, e.g. "=== unit 3 upload -> 1.2.3.4:5678 ===" then later
# "=== unit 3 restore on 1.2.3.4:5678 ... ===". Local jobs never print these.
_UNIT_EVENT_RE = re.compile(r"unit (\d+) (restore on|upload ->)\s+([\d.]+):(\d+)")

_PHASE_LABELS = {
    "Encoding": ("encode", "VAE encoding"),
    "Upscaling": ("dit", "DiT upscaling"),
    "Decoding": ("decode", "VAE decoding"),
}


def _line_timestamp(line: str) -> str | None:
    match = _TS_RE.match(line)
    return match.group(1) if match else None


def tail_lines(path: Path, size: int = TAIL_BYTES) -> list[str]:
    """Non-empty lines from the last ``size`` bytes of ``path``.

    Never raises: a missing/unreadable file (job not started yet, log
    rotated away, permission hiccup) is indistinguishable from "no lines"
    to every caller here."""
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            length = handle.tell()
            handle.seek(max(0, length - size))
            data = handle.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    return [line for line in text.splitlines() if line.strip()]


def parse_log_tail(path: Path | None) -> dict:
    """Best-effort snapshot of "what is the log doing right now".

    Returns a dict that is always safe to jsonify:
      available    -- False if the log has no content yet
      updated_at   -- ISO mtime of the log file, or None
      phase        -- 'prepare' | 'encode' | 'dit' | 'decode' | None
      phase_label  -- human label for the above
      batch        -- {"current", "total", "at"} for the current phase's
                       batch counter, or None
      last_write   -- {"frames", "start", "end", "at"} for the most recently
                       observed "Wrote N frames" line (a recency signal, not
                       necessarily from the current phase), or None
      tail_line    -- the last non-blank log line, as a last-resort display
    """
    result: dict = {
        "available": False, "updated_at": None, "phase": None, "phase_label": None,
        "batch": None, "last_write": None, "tail_line": None,
    }
    if path is None:
        return result
    try:
        result["updated_at"] = datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat(timespec="milliseconds")
    except OSError:
        pass

    lines = tail_lines(path)
    if not lines:
        return result
    result["available"] = True
    result["tail_line"] = lines[-1][:200]

    # A unit's ffmpeg prepare stage (deinterlace/colour) floods the tail with
    # frame=... lines that never match _BATCH_RE, but it would otherwise leave
    # a *stale* batch reading from the previous unit's final decode sitting in
    # the window. Check the most recent structured stage marker first so we
    # report "preparing" honestly instead of a stale SeedVR2 phase.
    last_stage_type = last_stage_name = None
    for line in reversed(lines):
        match = _STAGE_RE.search(line)
        if match:
            last_stage_type, last_stage_name = match.groups()
            break
    if last_stage_type == "stage_start" and last_stage_name == "prepare_50p":
        result["phase"], result["phase_label"] = "prepare", "Preparing unit (deinterlace)"
    else:
        for line in reversed(lines):
            match = _BATCH_RE.search(line)
            if match:
                verb, current, total = match.groups()
                phase, label = _PHASE_LABELS[verb]
                result["phase"], result["phase_label"] = phase, label
                result["batch"] = {"current": int(current), "total": int(total), "at": _line_timestamp(line)}
                break

    for line in reversed(lines):
        match = _WRITE_RE.search(line)
        if match:
            frames, start, end = (int(value) for value in match.groups())
            result["last_write"] = {"frames": frames, "start": start, "end": end, "at": _line_timestamp(line)}
            break

    return result


def unit_pod_hints(path: Path | None) -> dict[int, tuple[str, int]]:
    """Map unit sequence -> (host, port) from the "=== unit N restore/upload
    ===" markers a cloud pod prints. Local jobs never print these, so a local
    job's log (or a job with no log yet) just yields an empty map, which is
    exactly the "no pod attribution" state the caller wants."""
    if path is None:
        return {}
    hints: dict[int, tuple[str, int]] = {}
    for line in tail_lines(path):
        match = _UNIT_EVENT_RE.search(line)
        if not match:
            continue
        sequence_text, kind, host, port_text = match.groups()
        sequence = int(sequence_text)
        # "restore on" is the authoritative, currently-in-use host; an
        # earlier "upload ->" for the same unit is only a fallback.
        if kind.startswith("restore") or sequence not in hints:
            hints[sequence] = (host, int(port_text))
    return hints

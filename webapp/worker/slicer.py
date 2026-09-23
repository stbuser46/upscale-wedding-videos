"""Cut one durable unit's frames out of the stage-1 intermediate as a slice.

The stage-1 deinterlaced file is intra-only FFV1 (every frame a keyframe), so a
unit's frames `[skip, skip+cap)` can be extracted by a stream copy — frame-exact
and nearly free. The pod then restores that slice with `--skip_first_frames 0`,
which A1 (scripts/test_slice_equivalence.sh) proved equivalent to reading the
whole file with `--skip_first_frames skip`. Shipping ~220 MB per unit instead of
the whole ~30 GB intermediate is what makes cloud fan-out practical.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class SliceError(RuntimeError):
    pass


def slice_unit(
    stage1: Path,
    skip: int,
    cap: int,
    dest: Path,
    *,
    fps: float = 50.0,
    ffmpeg_image: str = "linuxserver/ffmpeg:latest",
    data_dir: Path | None = None,
    use_docker: bool = True,
    allow_short: bool = False,
    short_tolerance: int = 50,
) -> Path:
    """Stream-copy frames [skip, skip+cap) of `stage1` into `dest`.

    Runs ffmpeg in the project's pinned image by default (the worker host has no
    system ffmpeg), mounting the data dir read-only for the input and writable
    for the output. Verifies the slice holds exactly `cap` frames before
    returning, so a truncated slice can never be shipped and silently restore
    the wrong footage.

    `allow_short` relaxes that check for the final unit only: a chapter's real
    frame count can fall a few frames below the catalog estimate, so the tail
    slice legitimately runs off the end of `stage1`. When set, a slice up to
    `short_tolerance` frames short of `cap` is accepted (the caller then shrinks
    the unit to the frames that exist, mirroring the local path's post-restore
    tolerance). A larger shortfall still fails.
    """
    stage1 = Path(stage1).resolve()
    dest = Path(dest).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    # keep the real extension so ffmpeg can pick a muxer: name.partial.mkv
    part = dest.parent / f"{dest.stem}.partial{dest.suffix}"
    # `-ss` seeks by TIME, and stage-1 is intra-only FFV1, so the seek is
    # frame-exact ONLY when `fps` matches the stage-1 output frame rate. The
    # caller MUST pass the job's real output fps (50 for PAL, 60000/1001≈59.94
    # for NTSC): frame `skip` sits at PTS skip/fps, and using the wrong fps
    # lands on the wrong frame. For NTSC at the default 50 that error is ~19% of
    # `skip` and grows every unit, yet frame-count validation still passes
    # (interior slices still hold `cap` frames) — so the whole cloud run would
    # silently restore shifted footage. The 6-decimal timestamp is well within
    # half a frame of the true PTS, so it snaps to exactly frame `skip`.
    ss = f"{skip / fps:.6f}"

    if use_docker:
        if data_dir is None:
            raise SliceError("data_dir is required for the dockerised slicer")
        data_dir = Path(data_dir).resolve()
        for p in (stage1, dest):
            if not p.is_relative_to(data_dir):
                raise SliceError(f"slice path escaped the data dir: {p}")
        rel_in = stage1.relative_to(data_dir)
        rel_out = part.relative_to(data_dir)
        cmd = [
            "docker", "run", "--rm", "--network", "none",
            "-v", f"{data_dir}:/data",
            "--entrypoint", "ffmpeg", ffmpeg_image,
            "-v", "error", "-y",
            "-ss", ss, "-i", f"/data/{rel_in}",
            "-map", "0:v:0", "-frames:v", str(cap), "-c", "copy",
            f"/data/{rel_out}",
        ]
    else:
        cmd = [
            "ffmpeg", "-v", "error", "-y",
            "-ss", ss, "-i", str(stage1),
            "-map", "0:v:0", "-frames:v", str(cap), "-c", "copy",
            str(part),
        ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        part.unlink(missing_ok=True)
        raise SliceError(f"ffmpeg slice failed ({result.returncode}): {result.stderr.strip()[:400]}")

    actual = probe_frame_count(part, ffmpeg_image=ffmpeg_image, data_dir=data_dir, use_docker=use_docker)
    if allow_short:
        # Tail unit: accept a bounded shortfall (ran off the end of stage-1),
        # but never an empty, over-length, or grossly short slice.
        if actual <= 0 or actual > cap or (cap - actual) > short_tolerance:
            part.unlink(missing_ok=True)
            raise SliceError(f"tail slice has {actual} frames, expected {cap} "
                             f"within {short_tolerance} (skip={skip})")
    elif actual != cap:
        part.unlink(missing_ok=True)
        raise SliceError(f"slice has {actual} frames, expected {cap} (skip={skip})")
    part.replace(dest)
    return dest


def slice_frame_hash(
    path: Path,
    *,
    ffmpeg_image: str = "linuxserver/ffmpeg:latest",
    data_dir: Path | None = None,
    use_docker: bool = True,
) -> str:
    """sha256 over a slice's per-frame framemd5 hashes (decoded content only —
    mux metadata and timestamps excluded). Computed to byte-match the pod-side
    `ffmpeg -f framemd5 - | grep -v '^#' | awk '{print $NF}' | sha256sum`, so a
    remote-cut slice can be PROVEN decoded-identical to the local cut before a
    pod is allowed to restore it."""
    import hashlib

    path = Path(path).resolve()
    if use_docker:
        if data_dir is None:
            raise SliceError("data_dir is required for the dockerised hasher")
        data_dir = Path(data_dir).resolve()
        rel = path.relative_to(data_dir)
        cmd = [
            "docker", "run", "--rm", "--network", "none",
            "-v", f"{data_dir}:/data:ro",
            "--entrypoint", "ffmpeg", ffmpeg_image,
            "-v", "error", "-i", f"/data/{rel}", "-f", "framemd5", "-",
        ]
    else:
        cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-f", "framemd5", "-"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SliceError(f"framemd5 failed ({result.returncode}): {result.stderr.strip()[:200]}")
    tokens = [line.split()[-1] for line in result.stdout.splitlines()
              if line.strip() and not line.startswith("#")]
    if not tokens:
        raise SliceError(f"framemd5 produced no frame lines for {path}")
    digest = hashlib.sha256("".join(t + "\n" for t in tokens).encode()).hexdigest()
    return digest


def probe_frame_count(
    path: Path,
    *,
    ffmpeg_image: str = "linuxserver/ffmpeg:latest",
    data_dir: Path | None = None,
    use_docker: bool = True,
) -> int:
    """Count decoded video frames in `path` (dockerised ffprobe by default)."""
    path = Path(path).resolve()
    if use_docker:
        data_dir = Path(data_dir).resolve()
        rel = path.relative_to(data_dir)
        cmd = [
            "docker", "run", "--rm", "--network", "none",
            "-v", f"{data_dir}:/data:ro",
            "--entrypoint", "ffprobe", ffmpeg_image,
            "-v", "error", "-select_streams", "v:0", "-count_frames",
            "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0",
            f"/data/{rel}",
        ]
    else:
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
            "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path),
        ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SliceError(f"ffprobe failed ({result.returncode}): {result.stderr.strip()[:200]}")
    try:
        return int(result.stdout.strip())
    except ValueError as exc:
        raise SliceError(f"ffprobe returned non-integer frame count: {result.stdout!r}") from exc

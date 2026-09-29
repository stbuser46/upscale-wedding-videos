#!/usr/bin/env python
"""Queue a disc's titles as ~10-minute restoration segments (the Gulfraz
disc-2 recipe), in viewing order, ready for the cloud worker.

Each segment is a catalog SLICE (a named time window of a title, created the
same way POST /api/slices does, deduplicated by fingerprint) with one job
queued on it — the one-active-job-per-target index forbids several jobs on the
same chapter. Each is a small, independently resumable job: ~40 PAL / ~48 NTSC
durable units, a bounded stage-1 intermediate (~8-10 GB), one output file.
A trailing piece shorter than --min-tail-min is folded into the previous
segment instead of becoming its own tiny job.

Dry run by default (prints the plan, writes nothing). Add --commit to insert.

    webapp/.venv/bin/python scripts/queue_segments.py --chapters 36 37 38 39
    webapp/.venv/bin/python scripts/queue_segments.py --chapters 36 37 38 39 --commit
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from webapp.config import load_settings  # noqa: E402
from webapp.db import connect, utc_now  # noqa: E402
from webapp.server.services import find_active_job, insert_job, resolve_target  # noqa: E402


def ensure_slice(db, title_id: int, start_ms: int, end_ms: int, name: str) -> int:
    """Mirror POST /api/slices: reuse an identical window, else insert one."""
    fingerprint = hashlib.sha256(f"{title_id}:{start_ms}:{end_ms}".encode("ascii")).hexdigest()
    row = db.execute("SELECT id FROM slices WHERE request_fingerprint=?", (fingerprint,)).fetchone()
    if row is not None:
        return int(row[0])
    chapters = [r[0] for r in db.execute(
        "SELECT id FROM chapters WHERE title_id=? AND start_ms < ? AND end_ms > ? ORDER BY chapter_number",
        (title_id, end_ms, start_ms))]
    now = utc_now()
    cur = db.execute(
        """INSERT INTO slices (title_id, start_ms, end_ms, name, note, priority,
               source_chapters_json, request_fingerprint, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'normal', ?, ?, ?, ?)""",
        (title_id, start_ms, end_ms, name, "queued by scripts/queue_segments.py",
         json.dumps(chapters), fingerprint, now, now))
    return int(cur.lastrowid)


def windows(length_ms: int, seg_ms: int, min_tail_ms: int) -> list[tuple[int, int]]:
    cuts = list(range(0, length_ms, seg_ms))
    spans = [(s, min(s + seg_ms, length_ms)) for s in cuts]
    if len(spans) > 1 and spans[-1][1] - spans[-1][0] < min_tail_ms:
        last = spans.pop()
        spans[-1] = (spans[-1][0], last[1])
    return spans


def fmt(ms: int) -> str:
    return f"{ms // 60000:02d}:{(ms // 1000) % 60:02d}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--chapters", type=int, nargs="+", required=True,
                    help="chapter ids in the order they should run")
    ap.add_argument("--segment-min", type=float, default=10.0)
    ap.add_argument("--min-tail-min", type=float, default=2.0)
    ap.add_argument("--label", default="Title {title} Segment {n:02d}",
                    help="display_name pattern; {title}, {n}")
    ap.add_argument("--commit", action="store_true", help="actually insert the jobs")
    args = ap.parse_args()

    settings = load_settings()
    seg_ms = round(args.segment_min * 60000)
    tail_ms = round(args.min_tail_min * 60000)
    with connect(settings.database_path) as db:
        position = db.execute("SELECT COALESCE(MAX(queue_position), 0) FROM jobs").fetchone()[0]
        planned = 0
        for chapter_id in args.chapters:
            target = resolve_target(db, "chapter", chapter_id)
            base = target["start_ms"]
            for n, (s, e) in enumerate(windows(target["end_ms"] - base, seg_ms, tail_ms), 1):
                name = (args.label.format(title=target["title_number"], n=n)
                        + f" ({fmt(s)}–{fmt(e)})")
                print(f"chapter {chapter_id}  {name}  [{target['video_standard']}/"
                      f"{target['field_order']}]  {(e - s) / 60000:.2f} min")
                planned += 1
                if args.commit:
                    slice_id = ensure_slice(db, target["title_id"], base + s, base + e, name)
                    if find_active_job(db, "slice", slice_id) is not None:
                        print("    already queued/active — skipped")
                        planned -= 1
                        continue
                    position += 1
                    job = insert_job(db, settings, resolve_target(db, "slice", slice_id),
                                     "slice", slice_id, position)
                    db.execute("UPDATE jobs SET start_requested=1 WHERE id=?", (job["id"],))
        if args.commit:
            db.commit()
            print(f"queued {planned} segment job(s), start-requested")
        else:
            db.rollback()
            print(f"dry run: {planned} segment job(s) planned — add --commit to queue them")
    return 0


if __name__ == "__main__":
    sys.exit(main())

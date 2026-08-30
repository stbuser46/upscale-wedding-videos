"""Automatic backlog reconciliation.

Finds every chapter that still needs restoring and queues one job for it through
the same validated server-side path the UI uses. Idempotent: the per-target
partial unique index on jobs is the final duplicate guard, and completed
chapters are never re-queued. Chapters whose only history is a failed job are
NOT auto-queued — they are reported for manual review so unattended mode cannot
spin in an infinite retry loop.
"""
from __future__ import annotations

from typing import Any

from webapp.config import Settings
from webapp.db import connect, transaction
from webapp.server.services import insert_job, resolve_target


# Chapters eligible for automatic queueing: not skipped, not already restored,
# no live job, and no prior failure (failures wait for a human).
_CANDIDATE_SQL = """
    SELECT c.id, t.title_number, d.slug AS disc, COALESCE(c.user_label, c.generated_label) AS label
    FROM chapters c
    JOIN titles t ON t.id = c.title_id
    JOIN discs d ON d.id = t.disc_id
    WHERE c.priority != 'skip'
      AND NOT EXISTS (
            SELECT 1 FROM jobs j WHERE j.target_type='chapter' AND j.target_id=c.id
              AND j.state='completed')
      AND NOT EXISTS (
            SELECT 1 FROM jobs j WHERE j.target_type='chapter' AND j.target_id=c.id
              AND j.state NOT IN ('completed', 'failed', 'cancelled'))
      AND NOT EXISTS (
            SELECT 1 FROM jobs j WHERE j.target_type='chapter' AND j.target_id=c.id
              AND j.state='failed')
    ORDER BY t.title_number, c.start_ms
"""

# Chapters left out because a prior job failed — surfaced for manual review.
_NEEDS_REVIEW_SQL = """
    SELECT c.id, t.title_number, d.slug AS disc, COALESCE(c.user_label, c.generated_label) AS label
    FROM chapters c
    JOIN titles t ON t.id = c.title_id
    JOIN discs d ON d.id = t.disc_id
    WHERE c.priority != 'skip'
      AND NOT EXISTS (
            SELECT 1 FROM jobs j WHERE j.target_type='chapter' AND j.target_id=c.id
              AND j.state='completed')
      AND NOT EXISTS (
            SELECT 1 FROM jobs j WHERE j.target_type='chapter' AND j.target_id=c.id
              AND j.state NOT IN ('completed', 'failed', 'cancelled'))
      AND EXISTS (
            SELECT 1 FROM jobs j WHERE j.target_type='chapter' AND j.target_id=c.id
              AND j.state='failed')
    ORDER BY t.title_number, c.start_ms
"""


def reconcile_backlog(settings: Settings) -> dict[str, Any]:
    """Queue a job for every un-restored, un-queued, non-failed chapter.

    Returns a report: the jobs created this run, the chapters skipped for manual
    review (prior failure), and summary counts. Safe to call repeatedly.
    """
    created: list[dict[str, Any]] = []
    with connect(settings.database_path) as db, transaction(db):
        candidates = db.execute(_CANDIDATE_SQL).fetchall()
        needs_review = [dict(r) for r in db.execute(_NEEDS_REVIEW_SQL).fetchall()]
        position = db.execute("SELECT COALESCE(MAX(queue_position), 0) FROM jobs").fetchone()[0]
        for chapter in candidates:
            target = resolve_target(db, "chapter", chapter["id"])
            position += 1
            job = insert_job(db, settings, target, "chapter", chapter["id"], position)
            created.append(
                {
                    "public_id": job["public_id"],
                    "chapter_id": chapter["id"],
                    "disc": chapter["disc"],
                    "title_number": chapter["title_number"],
                    "label": chapter["label"],
                }
            )
    return {
        "created": created,
        "created_count": len(created),
        "needs_review": needs_review,
        "needs_review_count": len(needs_review),
    }

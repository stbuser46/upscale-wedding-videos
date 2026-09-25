-- Allow a second worker_status row (id=2) for the cloud worker, so a local
-- worker (id=1) and a concurrent cloud worker (id=2) each keep an independent
-- heartbeat instead of clobbering one shared row. The original CHECK(id=1)
-- silently rejected every cloud heartbeat insert (Codex round-4 finding #6),
-- leaving the cloud worker with no health signal. SQLite can't ALTER a CHECK,
-- so rebuild the table permitting id IN (1,2).
ALTER TABLE worker_status RENAME TO worker_status_old;

CREATE TABLE worker_status (
    id INTEGER PRIMARY KEY CHECK (id IN (1, 2)),
    pid INTEGER,
    activity TEXT NOT NULL DEFAULT 'starting',
    active_job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    last_beat_at TEXT,
    last_error TEXT,
    last_error_at TEXT,
    started_at TEXT,
    updated_at TEXT
);

INSERT INTO worker_status (id, pid, activity, active_job_id, last_beat_at,
                           last_error, last_error_at, started_at, updated_at)
    SELECT id, pid, activity, active_job_id, last_beat_at,
           last_error, last_error_at, started_at, updated_at FROM worker_status_old;

DROP TABLE worker_status_old;

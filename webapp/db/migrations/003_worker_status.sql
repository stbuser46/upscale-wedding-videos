-- Durable worker health signal. A single row (id=1) the worker updates as a
-- heartbeat so the UI and operators can tell whether the GPU worker is alive,
-- what it is doing, and what the last error was. Survives worker restarts.
CREATE TABLE worker_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    pid INTEGER,
    activity TEXT NOT NULL DEFAULT 'starting',
    active_job_id INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    last_beat_at TEXT,
    last_error TEXT,
    last_error_at TEXT,
    started_at TEXT,
    updated_at TEXT
);

INSERT INTO worker_status (id, activity) VALUES (1, 'starting');

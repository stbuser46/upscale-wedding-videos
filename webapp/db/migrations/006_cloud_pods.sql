-- Cloud (RunPod) pod ledger. One row per pod this project has ever created,
-- so that a crashed orchestrator can never leave a billable GPU running
-- unaccounted for: the reaper reconciles live RunPod pods against this table,
-- and spend is derived by summing rate x lifetime.
--
-- The row is written with state='creating' BEFORE the create API call returns,
-- so even a crash mid-call leaves a trace to reconcile against.
CREATE TABLE cloud_pods (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pod_id          TEXT UNIQUE,            -- RunPod id; NULL until create returns
    name            TEXT NOT NULL,          -- wedding-<public_id>-<seq>, our ownership marker
    provider        TEXT NOT NULL DEFAULT 'runpod',
    gpu_type        TEXT,                   -- gpuTypeId actually rented
    hourly_rate     REAL,                   -- $/h at creation, for spend accounting
    cloud_type      TEXT DEFAULT 'SECURE',
    state           TEXT NOT NULL DEFAULT 'creating'
                    CHECK (state IN ('creating','ready','running','terminating','terminated')),
    job_id          INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    chunk_sequence  INTEGER,                -- unit this pod is currently running, if any
    ssh_host        TEXT,
    ssh_port        INTEGER,
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ready_at        TEXT,
    last_seen_at    TEXT,
    terminated_at   TEXT
);

CREATE INDEX cloud_pods_active ON cloud_pods (state)
    WHERE state NOT IN ('terminated');
CREATE INDEX cloud_pods_job ON cloud_pods (job_id);

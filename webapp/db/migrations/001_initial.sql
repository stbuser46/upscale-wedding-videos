CREATE TABLE discs (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    source_filename TEXT NOT NULL UNIQUE,
    source_path TEXT NOT NULL UNIQUE,
    label TEXT,
    size_bytes INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    scan_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (scan_status IN ('pending', 'scanning', 'complete', 'failed')),
    scan_error TEXT,
    raw_scan_path TEXT,
    scanned_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE titles (
    id INTEGER PRIMARY KEY,
    disc_id INTEGER NOT NULL REFERENCES discs(id) ON DELETE CASCADE,
    title_number INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    angles INTEGER NOT NULL DEFAULT 1,
    video_json TEXT NOT NULL DEFAULT '{}',
    audio_json TEXT NOT NULL DEFAULT '[]',
    subtitles_json TEXT NOT NULL DEFAULT '[]',
    raw_navigation_json TEXT NOT NULL DEFAULT '{}',
    likely_menu INTEGER NOT NULL DEFAULT 0 CHECK (likely_menu IN (0, 1)),
    likely_duplicate INTEGER NOT NULL DEFAULT 0 CHECK (likely_duplicate IN (0, 1)),
    likely_short INTEGER NOT NULL DEFAULT 0 CHECK (likely_short IN (0, 1)),
    source_cache_path TEXT,
    proxy_state TEXT NOT NULL DEFAULT 'missing'
        CHECK (proxy_state IN ('missing', 'generating', 'ready', 'failed')),
    proxy_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (disc_id, title_number)
);

CREATE TABLE chapters (
    id INTEGER PRIMARY KEY,
    title_id INTEGER NOT NULL REFERENCES titles(id) ON DELETE CASCADE,
    chapter_number INTEGER NOT NULL,
    start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK (end_ms > start_ms),
    duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),
    generated_label TEXT NOT NULL,
    user_label TEXT,
    priority TEXT NOT NULL DEFAULT 'normal'
        CHECK (priority IN ('high', 'normal', 'low', 'skip')),
    notes TEXT NOT NULL DEFAULT '',
    proxy_state TEXT NOT NULL DEFAULT 'missing'
        CHECK (proxy_state IN ('missing', 'generating', 'ready', 'failed')),
    proxy_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (title_id, chapter_number)
);

CREATE TABLE slices (
    id INTEGER PRIMARY KEY,
    title_id INTEGER NOT NULL REFERENCES titles(id) ON DELETE RESTRICT,
    start_ms INTEGER NOT NULL CHECK (start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK (end_ms > start_ms),
    name TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    priority TEXT NOT NULL DEFAULT 'normal'
        CHECK (priority IN ('high', 'normal', 'low')),
    source_chapters_json TEXT NOT NULL DEFAULT '[]',
    request_fingerprint TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    target_type TEXT NOT NULL CHECK (target_type IN ('chapter', 'slice')),
    target_id INTEGER NOT NULL,
    title_id INTEGER NOT NULL REFERENCES titles(id) ON DELETE RESTRICT,
    source_start_ms INTEGER NOT NULL CHECK (source_start_ms >= 0),
    source_end_ms INTEGER NOT NULL CHECK (source_end_ms > source_start_ms),
    display_name TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50,
    queue_position INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN (
        'queued', 'preparing', 'running', 'pause_requested', 'paused',
        'resuming', 'assembling', 'completed', 'failed', 'cancel_requested',
        'cancelled', 'interrupted'
    )),
    start_requested INTEGER NOT NULL DEFAULT 0 CHECK (start_requested IN (0, 1)),
    stage TEXT,
    frames_done INTEGER NOT NULL DEFAULT 0,
    frames_total INTEGER NOT NULL DEFAULT 0,
    fps REAL,
    elapsed_seconds REAL NOT NULL DEFAULT 0,
    eta_seconds REAL,
    error TEXT,
    output_path TEXT,
    baseline_path TEXT,
    log_path TEXT,
    worker_pid INTEGER,
    claimed_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX jobs_one_active_target
ON jobs(target_type, target_id)
WHERE state NOT IN ('completed', 'failed', 'cancelled');

CREATE INDEX jobs_queue_order
ON jobs(start_requested, state, priority DESC, queue_position, created_at);

CREATE TABLE job_chunks (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    source_start_ms INTEGER NOT NULL,
    source_end_ms INTEGER NOT NULL,
    context_before_frames INTEGER NOT NULL DEFAULT 0,
    warmup_frames INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'pending',
    frame_count INTEGER,
    checksum TEXT,
    artifact_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_id, sequence)
);

CREATE TABLE job_events (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    state TEXT,
    stage TEXT,
    message TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX job_events_job_order ON job_events(job_id, id);

CREATE TABLE artifacts (
    id INTEGER PRIMARY KEY,
    disc_id INTEGER REFERENCES discs(id) ON DELETE CASCADE,
    title_id INTEGER REFERENCES titles(id) ON DELETE CASCADE,
    chapter_id INTEGER REFERENCES chapters(id) ON DELETE CASCADE,
    job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    duration_ms INTEGER,
    frame_count INTEGER,
    checksum TEXT,
    validation_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (validation_state IN ('pending', 'valid', 'invalid')),
    media_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX artifacts_chapter_kind ON artifacts(chapter_id, kind);
CREATE INDEX artifacts_title_kind ON artifacts(title_id, kind);
CREATE INDEX artifacts_job_kind ON artifacts(job_id, kind);

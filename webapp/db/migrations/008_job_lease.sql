-- Worker lease + bounded auto-resume, so a local and a cloud worker can run
-- against the same queue without stealing each other's live job, and a
-- crash-loop can't rent cloud fleets forever.
--
-- lease_expires_at: the owning worker renews this every ~45 s while it holds
-- the job. Startup/periodic recovery requeues a job only when its lease is
-- stale (expired or NULL) — a fresh lease means a live worker still owns it, so
-- the other worker must not touch it. Fixes the "second worker requeues the
-- first worker's running job" collision.
--
-- auto_resume_count: how many times recovery has auto-requeued this job without
-- a human asking. Recovery stops auto-resuming past a small cap (3) and parks
-- the job as failed, so a repeatable crash can't keep spinning up paid pods.
-- Reset to 0 whenever a human starts/resumes/retries the job, or it completes.
ALTER TABLE jobs ADD COLUMN lease_expires_at TEXT;
ALTER TABLE jobs ADD COLUMN auto_resume_count INTEGER NOT NULL DEFAULT 0;

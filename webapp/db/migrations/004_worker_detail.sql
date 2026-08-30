-- Free-text status detail for the worker heartbeat, e.g. the idle-gate reason
-- it is currently blocked on ("waiting: only 41 GiB VRAM free"). Distinct from
-- last_error, which persists the last failure.
ALTER TABLE worker_status ADD COLUMN detail TEXT;

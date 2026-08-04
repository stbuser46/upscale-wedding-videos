CREATE TABLE system_metrics (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    cpu_pct REAL NOT NULL,
    mem_pct REAL NOT NULL,
    gpu_pct REAL,
    gpu_mem_mib INTEGER,
    gpu_power_w REAL
);

CREATE INDEX idx_system_metrics_ts ON system_metrics(ts);

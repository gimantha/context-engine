CREATE TABLE sync_runs (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN ('reading', 'completed', 'superseded')),
    started_by TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX sync_runs_source_idx ON sync_runs (source_id, started_at);

CREATE TABLE indexing_snapshots (
    source_id TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN ('ok', 'unavailable')),
    expected INTEGER,
    indexed INTEGER,
    indexing INTEGER,
    failed INTEGER,
    missing INTEGER,
    error_code TEXT,
    collected_at TEXT NOT NULL
);

-- Progress reads filter jobs by source on every poll; the expression must match the queries.
CREATE INDEX jobs_source_created_idx
    ON jobs (json_extract(payload_json, '$.sourceId'), created_at);

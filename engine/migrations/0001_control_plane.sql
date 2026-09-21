CREATE TABLE context_spaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    state TEXT NOT NULL CHECK (state IN ('provisioning', 'ready', 'failed', 'deleting')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    operation TEXT NOT NULL CHECK (
        operation IN ('ingestion', 'update', 'deletion', 'enrichment', 'space_deletion')
    ),
    state TEXT NOT NULL CHECK (
        state IN ('accepted', 'queued', 'running', 'retry_wait', 'succeeded', 'failed')
    ),
    idempotency_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    next_attempt_at TEXT,
    lease_token TEXT,
    lease_expires_at TEXT,
    result_json TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (operation, idempotency_key)
);

CREATE INDEX jobs_available_idx
    ON jobs (state, next_attempt_at, lease_expires_at, created_at);

CREATE TABLE outbox_events (
    id TEXT PRIMARY KEY,
    aggregate_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    dispatched_at TEXT,
    UNIQUE (aggregate_id, event_type)
);

CREATE INDEX outbox_pending_idx ON outbox_events (dispatched_at, created_at);

CREATE TABLE source_record_effects (
    idempotency_key TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    source_version TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    UNIQUE (space_id, source_id, source_record_id, source_version, operation)
);

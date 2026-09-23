CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL REFERENCES context_spaces(id),
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ready', 'paused', 'failed')),
    version_ordering TEXT NOT NULL CHECK (version_ordering IN ('numeric', 'lexicographic')),
    audience_mapping_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX sources_space_idx ON sources (space_id, created_at);

CREATE TABLE source_checkpoints (
    source_id TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    cursor TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE staged_uploads (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    principal_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE (source_id, idempotency_key)
);

CREATE TABLE source_records (
    space_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'deleted', 'quarantined')),
    current_version TEXT NOT NULL,
    source_acl_version TEXT NOT NULL,
    content_hash TEXT,
    content_ref TEXT,
    audience_json TEXT NOT NULL,
    quarantine_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (space_id, source_id, source_record_id)
);

CREATE TABLE record_versions (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    source_version TEXT NOT NULL,
    operation TEXT NOT NULL,
    source_acl_version TEXT NOT NULL,
    content_hash TEXT,
    outcome TEXT NOT NULL CHECK (
        outcome IN ('applied', 'replayed', 'ignored_older', 'quarantined')
    ),
    resulting_state TEXT NOT NULL CHECK (resulting_state IN ('active', 'deleted', 'quarantined')),
    job_id TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE INDEX record_versions_record_idx
    ON record_versions (space_id, source_id, source_record_id, applied_at);

-- The effect table now only detects exact replays by idempotency key. Version and content
-- ordering are decided by the record ledger above, which allows repeated deliveries of one
-- version and several ACL changes for the same content version.
CREATE TABLE source_record_effects_v2 (
    idempotency_key TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    source_version TEXT NOT NULL,
    operation TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
INSERT INTO source_record_effects_v2 SELECT * FROM source_record_effects;
DROP TABLE source_record_effects;
ALTER TABLE source_record_effects_v2 RENAME TO source_record_effects;
CREATE INDEX source_record_effects_record_idx
    ON source_record_effects (space_id, source_id, source_record_id, source_version);

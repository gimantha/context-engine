-- Internal access partitions: one per distinct set of mapped audiences in a space.
CREATE TABLE access_partitions (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL REFERENCES context_spaces(id),
    audiences_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (space_id, audiences_json)
);

-- The partition a record should live in; NULL while quarantined or deleted.
ALTER TABLE source_records ADD COLUMN partition_id TEXT REFERENCES access_partitions(id);
CREATE INDEX source_records_partition_idx ON source_records (partition_id, state);

-- Opaque state the private knowledge-backend adapter needs to survive restarts. The engine
-- stores these values but never interprets or serializes them publicly.
CREATE TABLE backend_bindings (
    partition_id TEXT PRIMARY KEY,
    binding TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE backend_record_refs (
    partition_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    reference TEXT NOT NULL UNIQUE,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (partition_id, source_id, record_id)
);

CREATE TABLE backend_identities (
    principal_id TEXT PRIMARY KEY,
    identity TEXT NOT NULL,
    created_at TEXT NOT NULL
);

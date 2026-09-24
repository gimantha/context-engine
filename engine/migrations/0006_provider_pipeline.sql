-- Ledger fields the provider pipeline needs.
ALTER TABLE source_records ADD COLUMN source_url TEXT;
ALTER TABLE source_records ADD COLUMN index_state TEXT NOT NULL DEFAULT 'pending'
    CHECK (index_state IN ('pending', 'indexed', 'failed', 'reconcile_required', 'not_indexed'));
ALTER TABLE source_records ADD COLUMN index_error TEXT;
ALTER TABLE record_versions ADD COLUMN content_ref TEXT;
ALTER TABLE staged_uploads ADD COLUMN released_at TEXT;

-- Where each record physically lives in the knowledge backend. A record may briefly have two
-- locations while it moves between partitions. Writes record their intent before the call so a
-- crash is detected instead of silently repeated (ADR 0006).
CREATE TABLE record_locations (
    space_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    partition_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('writing', 'indexed', 'removing', 'reconcile_required')),
    version TEXT,
    target_version TEXT,
    backend_ref TEXT,
    content_hash TEXT,
    parser_version TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (space_id, source_id, source_record_id, partition_id)
);

CREATE INDEX record_locations_partition_idx ON record_locations (partition_id);

-- Read access materialized in the backend from engine grants and group membership.
CREATE TABLE backend_read_access (
    partition_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    PRIMARY KEY (partition_id, principal_id)
);

CREATE TABLE read_access_sync (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    policy_version INTEGER NOT NULL,
    principal_count INTEGER NOT NULL,
    synced_at TEXT NOT NULL
);

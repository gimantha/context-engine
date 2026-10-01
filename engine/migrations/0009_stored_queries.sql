-- Stored queries and their evidence (M5 slice 2). A query keeps who asked, under which policy
-- version and partitions, with which models, and what came of it; the counts of retrieved and
-- suppressed passages are known only when the query runs, so they are kept for its trace.
-- Evidence is shared between queries that returned the same passage of the same record
-- version, and lives exactly as long as that version's content: the worker deletes it in the
-- transaction that releases the version's staged bytes, and space deletion purges it.
CREATE TABLE queries (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL REFERENCES context_spaces(id),
    principal_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('context', 'answer')),
    question TEXT NOT NULL,
    result_limit INTEGER NOT NULL,
    policy_version TEXT NOT NULL,
    partitions_json TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('completed', 'insufficient_evidence')),
    retrieved_count INTEGER NOT NULL,
    suppressed_count INTEGER NOT NULL,
    models_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX queries_by_space ON queries(space_id, created_at);

CREATE TABLE evidence (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL REFERENCES context_spaces(id),
    source_id TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    source_version TEXT NOT NULL,
    passage TEXT NOT NULL,
    locator_json TEXT NOT NULL,
    source_url TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX evidence_by_record_version
    ON evidence(space_id, source_id, source_record_id, source_version);

CREATE TABLE query_evidence (
    query_id TEXT NOT NULL REFERENCES queries(id),
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    position INTEGER NOT NULL,
    PRIMARY KEY (query_id, evidence_id)
);

CREATE INDEX query_evidence_by_evidence ON query_evidence(evidence_id);

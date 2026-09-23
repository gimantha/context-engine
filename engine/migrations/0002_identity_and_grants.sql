CREATE TABLE principals (
    id TEXT PRIMARY KEY,
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('user', 'service')),
    email TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (issuer, subject)
);

CREATE TABLE grants (
    resource_id TEXT NOT NULL,
    id TEXT NOT NULL,
    principal_id TEXT REFERENCES principals(id) ON DELETE CASCADE,
    group_name TEXT,
    actions_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (resource_id, id),
    CHECK ((principal_id IS NULL) <> (group_name IS NULL))
);

CREATE INDEX grants_principal_idx ON grants (principal_id, resource_id);
CREATE INDEX grants_group_idx ON grants (group_name, resource_id);

CREATE TABLE access_decisions (
    id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    allowed INTEGER NOT NULL CHECK (allowed IN (0, 1)),
    reason_code TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    trace_id TEXT NOT NULL,
    decided_at TEXT NOT NULL
);

CREATE INDEX access_decisions_principal_idx ON access_decisions (principal_id, decided_at);

CREATE TABLE policy_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);

INSERT INTO policy_state (id, version) VALUES (1, 1);

ALTER TABLE jobs ADD COLUMN principal_id TEXT;

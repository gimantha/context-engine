-- Per-space model configuration (M5 slice 1). Each model is stored as JSON: provider, model,
-- endpoint, apiVersion, dimensions, and the key as a stored secret token, either encrypted
-- under the engine's secrets key or a reference resolved at call time. Keys are never stored
-- in the clear. One row per space; every change replaces it and bumps the version, and the
-- principal that made the change is recorded.
CREATE TABLE space_configurations (
    space_id TEXT PRIMARY KEY REFERENCES context_spaces(id),
    embedding_json TEXT,
    language_json TEXT,
    version INTEGER NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

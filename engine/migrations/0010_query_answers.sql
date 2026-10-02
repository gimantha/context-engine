-- Stored answers (M5 slice 3). One row per answered query: the checked answer text, whose
-- citation markers [n] name the query's evidence by position. An answer is written from every
-- passage its query's evidence holds, so it is erased when any of those passages is erased
-- with its record version's content, and with its space on space deletion.
CREATE TABLE query_answers (
    query_id TEXT PRIMARY KEY REFERENCES queries(id),
    answer TEXT NOT NULL,
    created_at TEXT NOT NULL
);

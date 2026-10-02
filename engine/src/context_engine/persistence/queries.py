"""Stored queries, their evidence, and their answers (M5 slices 2 and 3, ADR 0008, ADR 0015).

A query row records who asked, under which policy version and partitions, with which models,
and the outcome; an answered query also has its answer. Evidence rows hold the passage and its
locator, keyed by an id derived from the record version and the passage, so a passage returned
by many queries is stored once. Evidence and answers are erased with their record versions:
`delete_released_evidence` runs inside the transaction that marks a version's staged bytes
released, and `delete_space_queries` inside space deletion's purge, so no passage, and no
answer written from one, outlives the content it was taken from.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from sqlite3 import Connection, Row

from context_engine.domain import (
    EvidenceLocator,
    ModelUse,
    PublicEvidence,
    StoredEvidence,
    StoredQuery,
)

from .database import ControlDatabase


def _timestamp(value: datetime | None = None) -> str:
    """Write a time in ISO form, as every other control-plane table does."""

    return (value or datetime.now(UTC)).isoformat()


def _encode_locator(locator: EvidenceLocator) -> str:
    """Store a locator as JSON in its public field names, leaving out unknown parts.

    The row is read back into the same locator, so a reopened query returns exactly what the
    original response held.
    """

    fields = {
        "chunkIndex": locator.chunk_index,
        "start": locator.start,
        "end": locator.end,
        "sentences": list(locator.sentences) if locator.sentences else None,
        "lines": list(locator.lines) if locator.lines else None,
        "heading": locator.heading,
        "path": locator.path,
    }
    return json.dumps({key: value for key, value in fields.items() if value is not None})


def _pair(value: object) -> tuple[int, int] | None:
    """Read a stored [first, last] range back into a tuple, or None when it was not set."""

    if isinstance(value, list) and len(value) == 2:
        return int(value[0]), int(value[1])
    return None


def _decode_locator(value: str) -> EvidenceLocator:
    """Rebuild a locator from its stored JSON; missing parts stay unknown."""

    fields = json.loads(value)
    return EvidenceLocator(
        chunk_index=fields.get("chunkIndex"),
        start=fields.get("start"),
        end=fields.get("end"),
        sentences=_pair(fields.get("sentences")),
        lines=_pair(fields.get("lines")),
        heading=fields.get("heading"),
        path=fields.get("path"),
    )


def _evidence(row: Row) -> PublicEvidence:
    """Turn an evidence row into public evidence; the read still has to pass the barrier."""

    return PublicEvidence(
        id=row["id"],
        record_id=row["source_record_id"],
        source_id=row["source_id"],
        source_version=row["source_version"],
        passage=row["passage"],
        locator=_decode_locator(row["locator_json"]),
        source_url=row["source_url"],
    )


def _query(row: Row) -> StoredQuery:
    """Turn a query row into a stored query, with its models in a stable order."""

    models = json.loads(row["models_json"])
    return StoredQuery(
        id=row["id"],
        space_id=row["space_id"],
        principal_id=row["principal_id"],
        trace_id=row["trace_id"],
        mode=row["mode"],
        question=row["question"],
        limit=row["result_limit"],
        policy_version=row["policy_version"],
        partitions=tuple(json.loads(row["partitions_json"])),
        outcome=row["outcome"],
        retrieved=row["retrieved_count"],
        suppressed=row["suppressed_count"],
        models=tuple(
            (kind, ModelUse(value["provider"], value["model"], value["configuredBy"]))
            for kind, value in sorted(models.items())
        ),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def delete_released_evidence(connection: Connection, upload_ids: Sequence[str]) -> None:
    """Delete evidence of every record version whose staged content is being released.

    A version is tied to its content through `record_versions.content_ref`, so the evidence
    to drop is exactly the evidence of the versions that pointed at these uploads.
    """

    if not upload_ids:
        return
    marks = ", ".join("?" for _ in upload_ids)
    doomed = f"""
        SELECT e.id FROM evidence AS e
        JOIN record_versions AS v
            ON v.space_id = e.space_id AND v.source_id = e.source_id
            AND v.source_record_id = e.source_record_id AND v.source_version = e.source_version
        WHERE v.content_ref IN ({marks})
    """
    values = list(upload_ids)
    # An answer may draw on any passage it was given, so it goes with the first one erased.
    connection.execute(
        f"""
        DELETE FROM query_answers WHERE query_id IN (
            SELECT query_id FROM query_evidence WHERE evidence_id IN ({doomed})
        )
        """,
        values,
    )
    connection.execute(f"DELETE FROM query_evidence WHERE evidence_id IN ({doomed})", values)
    connection.execute(f"DELETE FROM evidence WHERE id IN ({doomed})", values)


def delete_space_queries(connection: Connection, space_id: str) -> None:
    """Delete every stored query, answer, and evidence row of a space, dependents first."""

    connection.execute(
        "DELETE FROM query_answers WHERE query_id IN (SELECT id FROM queries WHERE space_id = ?)",
        (space_id,),
    )
    connection.execute(
        """
        DELETE FROM query_evidence
        WHERE query_id IN (SELECT id FROM queries WHERE space_id = ?)
            OR evidence_id IN (SELECT id FROM evidence WHERE space_id = ?)
        """,
        (space_id, space_id),
    )
    connection.execute("DELETE FROM evidence WHERE space_id = ?", (space_id,))
    connection.execute("DELETE FROM queries WHERE space_id = ?", (space_id,))


class QueryRepository:
    """Record queries with their evidence and read them back."""

    def __init__(self, database: ControlDatabase) -> None:
        """Use the control database, where the ledger that erasure follows also lives."""

        self.database = database

    def record_query(
        self,
        query: StoredQuery,
        evidence: Sequence[PublicEvidence],
        answer: str | None = None,
    ) -> None:
        """Store a query, the evidence it returned in order, and its answer, in one transaction.

        For an answer, the evidence is every passage the model was given, so the links say
        exactly which versions the answer depends on.
        """

        models = {
            kind: {"provider": use.provider, "model": use.model, "configuredBy": use.configured_by}
            for kind, use in query.models
        }
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO queries(
                    id, space_id, principal_id, trace_id, mode, question, result_limit,
                    policy_version, partitions_json, outcome, retrieved_count, suppressed_count,
                    models_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    query.id,
                    query.space_id,
                    query.principal_id,
                    query.trace_id,
                    query.mode,
                    query.question,
                    query.limit,
                    query.policy_version,
                    json.dumps(list(query.partitions)),
                    query.outcome,
                    query.retrieved,
                    query.suppressed,
                    json.dumps(models, sort_keys=True),
                    _timestamp(query.created_at),
                ),
            )
            for position, item in enumerate(evidence):
                # The id is derived from the version and the passage, so a replay is the same row.
                connection.execute(
                    """
                    INSERT OR IGNORE INTO evidence(
                        id, space_id, source_id, source_record_id, source_version, passage,
                        locator_json, source_url, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.id,
                        query.space_id,
                        item.source_id,
                        item.record_id,
                        item.source_version,
                        item.passage,
                        _encode_locator(item.locator),
                        item.source_url,
                        _timestamp(query.created_at),
                    ),
                )
                connection.execute(
                    "INSERT INTO query_evidence(query_id, evidence_id, position) VALUES (?, ?, ?)",
                    (query.id, item.id, position),
                )
            if answer is not None:
                connection.execute(
                    "INSERT INTO query_answers(query_id, answer, created_at) VALUES (?, ?, ?)",
                    (query.id, answer, _timestamp(query.created_at)),
                )

    def get_query(self, query_id: str) -> StoredQuery | None:
        """Return a stored query when it exists."""

        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM queries WHERE id = ?", (query_id,)).fetchone()
        return _query(row) if row else None

    def query_evidence(self, query_id: str) -> tuple[PublicEvidence, ...]:
        """Return the evidence a query returned that still exists, in its original order."""

        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT e.* FROM query_evidence AS q JOIN evidence AS e ON e.id = q.evidence_id
                WHERE q.query_id = ? ORDER BY q.position
                """,
                (query_id,),
            ).fetchall()
        return tuple(_evidence(row) for row in rows)

    def get_answer(self, query_id: str) -> str | None:
        """Return a query's stored answer, or None when it had none or it was erased."""

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT answer FROM query_answers WHERE query_id = ?", (query_id,)
            ).fetchone()
        return row["answer"] if row else None

    def get_evidence(self, evidence_id: str) -> StoredEvidence | None:
        """Return one evidence row with the space that holds it."""

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM evidence WHERE id = ?", (evidence_id,)
            ).fetchone()
        return StoredEvidence(row["space_id"], _evidence(row)) if row else None

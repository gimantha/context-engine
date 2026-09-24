"""Indexing collector tests against the deterministic backend."""

from __future__ import annotations

import hashlib
from itertools import count
from pathlib import Path

from context_engine.domain import IndexingState, JobOperation, VersionOrdering
from context_engine.knowledge_backend import (
    BackendError,
    BackendErrorCode,
    DummyKnowledgeBackend,
    PrincipalContext,
    SourceRecord,
)
from context_engine.observability import MetricsRegistry
from context_engine.persistence import ControlDatabase, ControlPlaneRepository, SourceRepository
from context_engine.security.partitions import partition_for
from context_engine.worker import IndexingCollector

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
SERVICE = PrincipalContext("prn_engine_service", "trace-collector")


def _ledger(tmp_path):
    database = ControlDatabase(tmp_path / "control.db", MIGRATIONS)
    database.migrate()
    repository = ControlPlaneRepository(database)
    sources = SourceRepository(database)
    space = repository.create_space("Ops", None)
    source = sources.create_source(
        space.id, "Files", "file", VersionOrdering.NUMERIC, {"t": "team", "u": "ops"}
    )
    sequence = count()

    def event(record_id, version, operation="upsert", audience=("t",)):
        key = f"k-{next(sequence):06d}"
        payload = {
            "schemaVersion": "1",
            "spaceId": space.id,
            "sourceId": source.id,
            "sourceRecordId": record_id,
            "sourceVersion": version,
            "operation": operation,
            "sourceObservedAt": "2026-09-24T10:00:00Z",
            "audience": list(audience),
            "sourceAclVersion": "1",
            "idempotencyKey": key,
        }
        if operation == "upsert":
            digest = hashlib.sha256(f"{record_id}:{version}".encode()).hexdigest()
            payload["contentHash"] = f"sha256:{digest}"
        kind = {"upsert": JobOperation.INGESTION, "delete": JobOperation.DELETION}[operation]
        job, _ = repository.enqueue_job(kind, key, payload, "trace", 3, "prn_connector")
        sources.apply_record_event(job, source)

    return sources, space, source, event


async def test_collector_counts_indexed_and_missing_versions_of_active_records(tmp_path):
    sources, space, source, event = _ledger(tmp_path)
    event("doc-a", "1")
    event("doc-b", "1", audience=("t", "u"))
    event("doc-b", "2", audience=("u", "t"))
    event("doc-c", "1", audience=("x",))
    event("doc-d", "1")
    event("doc-d", "2", operation="delete")
    backend = DummyKnowledgeBackend()
    team = partition_for(space.id, ("team",))
    both = partition_for(space.id, ("ops", "team"))
    await backend.ingest(SourceRecord("doc-a", source.id, "1", "a", "sha256:a"), SERVICE, team)
    await backend.ingest(SourceRecord("doc-b", source.id, "1", "b", "sha256:b"), SERVICE, both)
    collector = IndexingCollector(sources, backend, SERVICE, MetricsRegistry())

    assert await collector.collect() == 1
    first = sources.get_indexing_snapshot(source.id)
    await backend.update(SourceRecord("doc-b", source.id, "2", "b2", "sha256:b2"), SERVICE, both)
    await collector.collect()
    second = sources.get_indexing_snapshot(source.id)

    # Quarantined and deleted records are not expected; the stale doc-b version is missing.
    assert first.state is IndexingState.OK
    assert (first.expected, first.indexed, first.indexing, first.failed, first.missing) == (
        2,
        1,
        0,
        0,
        1,
    )
    assert (second.expected, second.indexed, second.missing) == (2, 2, 0)


class _FailingBackend(DummyKnowledgeBackend):
    async def indexing_progress(self, request, principal, authorized_partitions):
        raise BackendError(BackendErrorCode.UNAVAILABLE, "down", retryable=True)


class _UncalledBackend(DummyKnowledgeBackend):
    async def indexing_progress(self, request, principal, authorized_partitions):
        raise AssertionError("the backend must not be asked when nothing is expected")


async def test_collector_stores_unavailable_backend_and_skips_empty_sources(tmp_path):
    sources, _, source, event = _ledger(tmp_path)
    empty = await IndexingCollector(
        sources, _UncalledBackend(), SERVICE, MetricsRegistry()
    ).collect_source(source)
    event("doc-a", "1")
    metrics = MetricsRegistry()
    failed = await IndexingCollector(sources, _FailingBackend(), SERVICE, metrics).collect_source(
        source
    )
    stored = sources.get_indexing_snapshot(source.id)

    assert (empty.state, empty.expected, empty.indexed) == (IndexingState.OK, 0, 0)
    assert failed.state is IndexingState.UNAVAILABLE and failed.error_code == "unavailable"
    assert failed.expected is None
    assert (stored.state, stored.error_code) == (IndexingState.UNAVAILABLE, "unavailable")
    assert metrics.snapshot()["context_engine_indexing_collection_failures_total"] == 1

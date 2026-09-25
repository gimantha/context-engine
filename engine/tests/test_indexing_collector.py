"""Cross-check of the knowledge backend against the ledger."""

from __future__ import annotations

from test_record_indexer import SERVICE_ID, _Harness

from context_engine.domain import IndexingState, IndexState, LocationState
from context_engine.knowledge_backend import (
    AccessPartitionRef,
    BackendError,
    BackendErrorCode,
    BackendReference,
    DummyKnowledgeBackend,
    PrincipalContext,
)
from context_engine.observability import MetricsRegistry
from context_engine.worker import IndexingCollector

SERVICE = PrincipalContext(SERVICE_ID, "trace-check")


def _collector(harness, backend=None, metrics=None):
    return IndexingCollector(
        harness.sources, backend or harness.backend, SERVICE_ID, metrics or MetricsRegistry()
    )


async def test_cross_check_passes_when_the_backend_holds_every_copy(tmp_path):
    harness = _Harness(tmp_path)
    await harness.deliver("doc-a", "1", content=b"alpha")
    await harness.deliver("doc-b", "1", content=b"beta", audience=("t", "u"))

    assert await _collector(harness).collect("trace-1") == 1
    snapshot = harness.sources.get_indexing_snapshot(harness.source.id)

    assert snapshot.state is IndexingState.OK
    assert (snapshot.expected, snapshot.indexed, snapshot.missing) == (2, 2, 0)
    assert harness.record("doc-a").index_state is IndexState.INDEXED


async def test_cross_check_flags_copies_the_backend_lost(tmp_path):
    harness = _Harness(tmp_path)
    await harness.deliver("doc-a", "1", content=b"alpha")
    await harness.deliver("doc-b", "1", content=b"beta")
    [lost] = harness.locations("doc-b")
    await harness.backend.delete(
        BackendReference(lost.backend_ref), SERVICE, AccessPartitionRef(lost.partition_id)
    )
    metrics = MetricsRegistry()

    snapshot = await _collector(harness, metrics=metrics).collect_source(harness.source, "trace")

    assert snapshot.missing == 1
    assert harness.record("doc-b").index_state is IndexState.RECONCILE_REQUIRED
    assert harness.record("doc-b").index_error == "backend_mismatch"
    assert harness.locations("doc-b")[0].state is LocationState.RECONCILE_REQUIRED
    assert harness.record("doc-a").index_state is IndexState.INDEXED
    assert metrics.snapshot()["context_engine_index_mismatches_total"] == 1


async def test_identical_content_under_a_new_version_is_not_flagged(tmp_path):
    harness = _Harness(tmp_path)
    await harness.deliver("doc-a", "1", content=b"same bytes")
    await harness.deliver("doc-a", "2", content=b"same bytes")

    snapshot = await _collector(harness).collect_source(harness.source, "trace")

    assert (snapshot.indexed, snapshot.missing) == (1, 0)
    assert harness.record("doc-a").index_state is IndexState.INDEXED


class _FailingBackend(DummyKnowledgeBackend):
    async def indexing_progress(self, request, principal, authorized_partitions):
        raise BackendError(BackendErrorCode.UNAVAILABLE, "down", retryable=True)


class _UncalledBackend(DummyKnowledgeBackend):
    async def indexing_progress(self, request, principal, authorized_partitions):
        raise AssertionError("the backend must not be asked when nothing is indexed")


async def test_unavailable_backend_is_recorded_and_empty_sources_are_skipped(tmp_path):
    harness = _Harness(tmp_path)
    empty = await _collector(harness, _UncalledBackend()).collect_source(harness.source, "t")
    await harness.deliver("doc-a", "1", content=b"alpha")
    metrics = MetricsRegistry()

    failed = await _collector(harness, _FailingBackend(), metrics).collect_source(
        harness.source, "t"
    )

    assert (empty.state, empty.expected) == (IndexingState.OK, 0)
    assert failed.state is IndexingState.UNAVAILABLE and failed.error_code == "unavailable"
    assert harness.record("doc-a").index_state is IndexState.INDEXED
    assert metrics.snapshot()["context_engine_indexing_collection_failures_total"] == 1

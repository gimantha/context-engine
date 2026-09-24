"""Contract tests for provider-neutral knowledge backends."""

from __future__ import annotations

import asyncio

import pytest

from context_engine.knowledge_backend import (
    AccessPartitionRef,
    BackendError,
    BackendErrorCode,
    DummyKnowledgeBackend,
    EnrichmentRequest,
    ExpectedRecord,
    IndexingProgressRequest,
    PrincipalContext,
    QueryRequest,
)


@pytest.fixture
def principal() -> PrincipalContext:
    return PrincipalContext("principal-alpha", "trace-contract")


@pytest.fixture
def partition() -> AccessPartitionRef:
    return AccessPartitionRef("partition-alpha")


@pytest.mark.asyncio
async def test_ingest_replay_update_and_delete(record_factory, principal, partition):
    backend = DummyKnowledgeBackend()
    first = record_factory("record-1", "1", "Atlas obsoletealpha", entities=("Gateway",))
    ingested = await backend.ingest(first, principal, partition)
    replayed = await backend.ingest(first, principal, partition)

    assert ingested.created is True
    assert replayed.created is False
    assert replayed.backend_reference == ingested.backend_reference

    newer = record_factory("record-1", "2", "Atlas currentalpha", entities=("Gateway",))
    updated = await backend.update(newer, principal, partition)
    assert updated.backend_reference == ingested.backend_reference
    assert (
        await backend.query(QueryRequest("obsoletealpha"), principal, (partition,))
    ).insufficient_evidence
    result = await backend.query(QueryRequest("currentalpha"), principal, (partition,))
    assert [item.source_version for item in result.evidence] == ["2"]

    with pytest.raises(BackendError) as error:
        await backend.update(first, principal, partition)
    assert error.value.code == BackendErrorCode.CONFLICT

    deleted = await backend.delete(ingested.backend_reference, principal, partition)
    assert deleted.deleted is True
    assert deleted.record_id == "record-1"
    assert (
        await backend.query(QueryRequest("currentalpha"), principal, (partition,))
    ).insufficient_evidence


@pytest.mark.asyncio
async def test_query_requires_explicit_partition(principal):
    backend = DummyKnowledgeBackend()
    with pytest.raises(BackendError) as error:
        await backend.query(QueryRequest("anything"), principal, ())
    assert error.value.code == BackendErrorCode.ACCESS_DENIED


@pytest.mark.asyncio
async def test_enrichment_is_partition_scoped(record_factory, principal):
    backend = DummyKnowledgeBackend()
    alpha = AccessPartitionRef("partition-alpha")
    beta = AccessPartitionRef("partition-beta")
    await backend.ingest(
        record_factory("alpha", "1", "Alpha incident", entities=("Gateway",)), principal, alpha
    )
    await backend.ingest(
        record_factory("beta", "1", "Beta incident", entities=("Gateway",)), principal, beta
    )

    enriched = await backend.enrich(EnrichmentRequest("enrich-1", "v1"), principal, (alpha,))
    assert enriched.affected_records == 1
    assert enriched.created_artifacts == ("Gateway:v1",)
    beta_result = await backend.query(QueryRequest("v1"), principal, (beta,))
    assert beta_result.insufficient_evidence


@pytest.mark.asyncio
async def test_concurrent_queries_keep_principal_and_partition_scope(record_factory):
    backend = DummyKnowledgeBackend()
    alpha = AccessPartitionRef("partition-alpha")
    beta = AccessPartitionRef("partition-beta")
    alpha_principal = PrincipalContext("alpha", "trace-alpha")
    beta_principal = PrincipalContext("beta", "trace-beta")
    await backend.ingest(record_factory("alpha", "1", "canary-alpha"), alpha_principal, alpha)
    await backend.ingest(record_factory("beta", "1", "canary-beta"), beta_principal, beta)

    alpha_result, beta_result = await asyncio.gather(
        backend.query(QueryRequest("canary"), alpha_principal, (alpha,)),
        backend.query(QueryRequest("canary"), beta_principal, (beta,)),
    )
    assert [item.record_id for item in alpha_result.evidence] == ["alpha"]
    assert [item.record_id for item in beta_result.evidence] == ["beta"]


@pytest.mark.asyncio
async def test_indexing_progress_requires_explicit_scope(record_factory, principal, partition):
    backend = DummyKnowledgeBackend()
    await backend.ingest(record_factory("record-1", "1", "content"), principal, partition)
    request = IndexingProgressRequest(
        "source-incidents", (ExpectedRecord(partition, "record-1", "1"),)
    )

    progress = await backend.indexing_progress(request, principal, (partition,))
    with pytest.raises(BackendError) as empty:
        await backend.indexing_progress(request, principal, ())
    with pytest.raises(BackendError) as outside:
        await backend.indexing_progress(request, principal, (AccessPartitionRef("other"),))

    assert (progress.expected, progress.indexed, progress.missing) == (1, 1, 0)
    assert empty.value.code == BackendErrorCode.ACCESS_DENIED
    assert outside.value.code == BackendErrorCode.ACCESS_DENIED

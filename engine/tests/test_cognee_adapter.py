"""Private adapter translation and isolation tests."""

from __future__ import annotations

import os
from dataclasses import dataclass
from importlib.util import find_spec

import pytest

from context_engine.config import KnowledgeBackendSettings
from context_engine.knowledge_backend import (
    AccessPartitionRef,
    BackendError,
    BackendErrorCode,
    EnrichmentRequest,
    PrincipalContext,
    QueryRequest,
)
from context_engine.knowledge_backend.providers.cognee import (
    CogneeBackend,
    _apply_native_environment,
    _NativeIngestion,
    assert_runtime_matches_pinned_sdk,
)


@dataclass
class FakeNativeUser:
    id: str


class FakeCogneeRuntime:
    def __init__(self):
        self.calls = []

    async def remember(self, record, binding, user):
        self.calls.append(("remember", record, binding, user))
        return _NativeIngestion("00000000-0000-0000-0000-000000000001", record.record_id, True)

    async def recall(self, request, bindings, user):
        self.calls.append(("recall", request, bindings, user))
        return [
            {
                "text": "authorized passage",
                "score": 0.8,
                "metadata": {
                    "record_id": "record-1",
                    "source_id": "source-1",
                    "source_version": "1",
                    "location": "page:1",
                },
                "dataset_id": "must-not-escape",
            }
        ]

    async def update(self, record, binding, data_id, user):
        self.calls.append(("update", record, binding, data_id, user))

    async def improve(self, binding, user):
        self.calls.append(("improve", binding, user))

    async def forget(self, binding, data_id, user):
        self.calls.append(("forget", binding, data_id, user))

    async def health(self):
        return True, "fake Cognee runtime"


async def resolve_user(principal):
    return FakeNativeUser(principal.principal_id)


@pytest.mark.asyncio
async def test_adapter_translates_native_values(record_factory):
    runtime = FakeCogneeRuntime()
    backend = CogneeBackend(runtime, resolve_user)
    principal = PrincipalContext("principal-1", "trace-1")
    partition = AccessPartitionRef("partition-1")
    record = record_factory("00000000-0000-0000-0000-000000000002", "1", "content")

    ingested = await backend.ingest(record, principal, partition)
    queried = await backend.query(QueryRequest("content"), principal, (partition,))
    updated_record = record_factory(
        record.record_id, "2", "updated content", source_id=record.source_id
    )
    updated = await backend.update(updated_record, principal, partition)
    enriched = await backend.enrich(EnrichmentRequest("op-1", "v1"), principal, (partition,))
    deleted = await backend.delete(ingested.backend_reference, principal, partition)

    assert ingested.record_id == record.record_id
    assert queried.evidence[0].passage == "authorized passage"
    assert "must-not-escape" not in repr(queried)
    assert updated.backend_reference == ingested.backend_reference
    assert enriched.operation_id == "op-1"
    assert deleted.record_id == record.record_id
    assert [call[0] for call in runtime.calls] == [
        "remember",
        "recall",
        "update",
        "improve",
        "forget",
    ]


@pytest.mark.asyncio
async def test_adapter_never_queries_uninitialized_or_empty_scope():
    backend = CogneeBackend(FakeCogneeRuntime(), resolve_user)
    principal = PrincipalContext("principal-1", "trace-1")
    with pytest.raises(BackendError) as empty:
        await backend.query(QueryRequest("content"), principal, ())
    assert empty.value.code == BackendErrorCode.ACCESS_DENIED

    with pytest.raises(BackendError) as uninitialized:
        await backend.query(QueryRequest("content"), principal, (AccessPartitionRef("unknown"),))
    assert uninitialized.value.code == BackendErrorCode.NOT_FOUND


def test_installed_sdk_signature_when_available():
    if find_spec("cognee") is None:
        pytest.skip("private provider dependency is not installed")
    assert_runtime_matches_pinned_sdk()


def test_engine_settings_override_native_environment(monkeypatch, tmp_path):
    settings = KnowledgeBackendSettings(
        telemetry_enabled=True,
        file_logging_enabled=True,
        query_cache_enabled=True,
        access_control_required=True,
        local_content_access_enabled=False,
        remote_content_access_enabled=False,
        raw_graph_query_enabled=False,
        relational_store="engine-relational",
        graph_store="engine-graph",
        vector_store="engine-vector",
        model_provider="engine-model-provider",
        model_name="engine-model",
        model_api_key="engine-model-key",
        embedding_provider="engine-embedding-provider",
        embedding_model="engine-embedding-model",
        embedding_dimensions=42,
        embedding_api_key="engine-embedding-key",
        storage_path=tmp_path,
    )
    monkeypatch.setenv("ENABLE_BACKEND_ACCESS_CONTROL", "false")
    monkeypatch.setenv("LLM_API_KEY", "bypass-key")

    _apply_native_environment(settings)

    assert os.environ["ENABLE_BACKEND_ACCESS_CONTROL"] == "true"
    assert os.environ["LLM_API_KEY"] == "engine-model-key"
    assert os.environ["GRAPH_DATABASE_PROVIDER"] == "engine-graph"
    assert os.environ["VECTOR_DB_PROVIDER"] == "engine-vector"
    assert os.environ["EMBEDDING_DIMENSIONS"] == "42"
    assert os.environ["SYSTEM_ROOT_DIRECTORY"] == str(tmp_path / "system")

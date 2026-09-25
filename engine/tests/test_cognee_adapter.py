"""Private adapter translation and isolation tests."""

from __future__ import annotations

import enum
import os
import sys
import types
from dataclasses import dataclass
from importlib.util import find_spec
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from context_engine.config import KnowledgeBackendSettings
from context_engine.knowledge_backend import (
    AccessPartitionRef,
    BackendError,
    BackendErrorCode,
    EnrichmentRequest,
    ExpectedRecord,
    IndexingProgressRequest,
    PrincipalContext,
    QueryRequest,
)
from context_engine.knowledge_backend.providers.cognee import (
    CogneeBackend,
    CogneeRuntime,
    _apply_native_environment,
    _CogneeBinding,
    _NativeIngestion,
    assert_runtime_matches_pinned_sdk,
)


@dataclass
class FakeNativeUser:
    id: str


class FakeCogneeRuntime:
    def __init__(self):
        self.calls = []
        self.items = {}
        self.states = {}

    async def remember(self, record, binding, user):
        self.calls.append(("remember", record, binding, user))
        native_id = binding.dataset_id or str(uuid5(NAMESPACE_URL, binding.dataset_name))
        self.items.setdefault(native_id, []).append(
            {
                "external_metadata": {
                    "record_id": record.record_id,
                    "source_id": record.source_id,
                    "source_version": record.version,
                },
                "raw_data_location": "must-not-escape",
            }
        )
        item_id = str(uuid5(NAMESPACE_URL, f"{native_id}:{record.source_id}:{record.record_id}"))
        return _NativeIngestion(native_id, item_id, True)

    async def list_items(self, binding, user):
        self.calls.append(("list_items", binding.dataset_id))
        return list(self.items.get(binding.dataset_id, []))

    async def grant_read(self, binding, reader, owner):
        self.calls.append(("grant_read", binding.dataset_id, reader.id, owner.id))

    async def revoke_read(self, binding, reader, owner):
        self.calls.append(("revoke_read", binding.dataset_id, reader.id, owner.id))

    async def processing_states(self, bindings):
        self.calls.append(("processing_states", tuple(item.dataset_id for item in bindings)))
        return {
            item.dataset_id: self.states[item.dataset_id]
            for item in bindings
            if item.dataset_id in self.states
        }

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
    # Updates replace the item; the fake keeps one item id per record, so nothing is dropped.
    assert [call[0] for call in runtime.calls] == [
        "remember",
        "recall",
        "remember",
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
    # Local reads stay confined to the provider's own storage.
    assert os.environ["ACCEPT_LOCAL_FILE_PATH"] == "true"
    assert os.environ["COGNEE_ALLOWED_LOCAL_FILE_ROOTS"] == str(tmp_path / "data")


def test_provider_refuses_local_paths_outside_its_storage(tmp_path):
    if find_spec("cognee") is None:
        pytest.skip("private provider dependency is not installed")
    snapshot = dict(os.environ)
    try:
        _apply_native_environment(KnowledgeBackendSettings(storage_path=tmp_path / "knowledge"))
        from cognee.infrastructure.files.utils.local_path_safety import resolve_local_path

        outside = tmp_path / "outside.txt"
        outside.write_text("must never be read")
        with pytest.raises(ValueError):
            resolve_local_path(outside, must_exist=True)
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


class _NativeStatus:
    def __init__(self, value):
        self.value = value


@pytest.mark.asyncio
async def test_adapter_attributes_native_processing_state_to_source_records(record_factory):
    runtime = FakeCogneeRuntime()
    backend = CogneeBackend(runtime, resolve_user)
    principal = PrincipalContext("engine-service", "trace-indexing")
    done, busy, broken, empty = (
        AccessPartitionRef(name) for name in ("p-done", "p-busy", "p-broken", "p-empty")
    )
    await backend.ingest(record_factory("r-done", "1", "a"), principal, done)
    await backend.ingest(record_factory("r-busy", "1", "b"), principal, busy)
    await backend.ingest(record_factory("r-broken", "1", "c"), principal, broken)
    await backend.ingest(
        record_factory("r-other", "1", "d", source_id="source-other"), principal, done
    )
    native = {ref: backend._bindings.resolve(ref).dataset_id for ref in (done, busy, broken)}
    runtime.states = {
        native[done]: {"status": _NativeStatus("DATASET_PROCESSING_COMPLETED"), "progress": None},
        native[busy]: {"status": "DATASET_PROCESSING_STARTED", "progress": {"total_items": 3}},
        native[broken]: {"status": "DATASET_PROCESSING_ERRORED"},
    }
    request = IndexingProgressRequest(
        "source-incidents",
        (
            ExpectedRecord(done, "r-done", "1"),
            ExpectedRecord(done, "r-done", "2"),
            ExpectedRecord(done, "r-other", "1"),
            ExpectedRecord(busy, "r-busy", "1"),
            ExpectedRecord(broken, "r-broken", "1"),
            ExpectedRecord(empty, "r-never", "1"),
        ),
    )

    progress = await backend.indexing_progress(request, principal, (done, busy, broken, empty))
    with pytest.raises(BackendError) as scoped:
        await backend.indexing_progress(request, principal, (done,))

    # A newer version not yet stored, another source's item, and an empty binding are missing.
    assert (
        progress.expected,
        progress.indexed,
        progress.indexing,
        progress.failed,
        progress.missing,
    ) == (6, 1, 1, 1, 3)
    assert "DATASET" not in repr(progress) and "must-not-escape" not in repr(progress)
    assert scoped.value.code == BackendErrorCode.ACCESS_DENIED


class _VersionedItemRuntime(FakeCogneeRuntime):
    """Give every version its own native item, as the real provider does for new content."""

    async def remember(self, record, binding, user):
        result = await super().remember(record, binding, user)
        item_id = str(uuid5(NAMESPACE_URL, f"{result.data_id}:{record.version}"))
        return _NativeIngestion(result.dataset_id, item_id, True)


@pytest.mark.asyncio
async def test_update_replaces_the_item_and_drops_the_previous_one(record_factory):
    runtime = _VersionedItemRuntime()
    backend = CogneeBackend(runtime, resolve_user)
    principal = PrincipalContext("engine-service", "trace-update")
    partition = AccessPartitionRef("prt_update")

    first = await backend.ingest(record_factory("doc-1", "1", "old"), principal, partition)
    second = await backend.update(record_factory("doc-1", "2", "new"), principal, partition)

    forgotten = [call for call in runtime.calls if call[0] == "forget"]
    assert second.backend_reference != first.backend_reference
    assert len(forgotten) == 1 and forgotten[0][2] == first.backend_reference.value.split(":")[2]
    assert backend._state.get_record_reference(partition.value, "source-incidents", "doc-1") == (
        second.backend_reference.value
    )


@pytest.mark.asyncio
async def test_read_grants_act_as_the_owner_and_need_an_initialized_partition(record_factory):
    runtime = FakeCogneeRuntime()
    backend = CogneeBackend(runtime, resolve_user)
    owner = PrincipalContext("engine-service", "trace-grant")
    reader = PrincipalContext("prn_reader", "trace-grant")
    partition = AccessPartitionRef("prt_grant")

    with pytest.raises(BackendError) as uninitialized:
        await backend.grant_read(partition, reader, owner)
    await backend.ingest(record_factory("doc-1", "1", "a"), owner, partition)
    await backend.grant_read(partition, reader, owner)
    await backend.revoke_read(partition, reader, owner)

    native = backend._bindings.resolve(partition).dataset_id
    assert uninitialized.value.code == BackendErrorCode.NOT_FOUND
    assert [call for call in runtime.calls if call[0] in {"grant_read", "revoke_read"}] == [
        ("grant_read", native, "prn_reader", "engine-service"),
        ("revoke_read", native, "prn_reader", "engine-service"),
    ]


class _StructuredRuntime(FakeCogneeRuntime):
    """Return search entries shaped like the provider's normalized graph results."""

    def __init__(self):
        super().__init__()
        self.entries = []

    async def recall(self, request, bindings, user):
        self.calls.append(("recall", request, bindings, user))
        return self.entries


@pytest.mark.asyncio
async def test_structured_results_resolve_through_recorded_references(record_factory):
    runtime = _StructuredRuntime()
    backend = CogneeBackend(runtime, resolve_user)
    principal = PrincipalContext("prn_reader", "trace-structured")
    partition = AccessPartitionRef("prt_structured")
    ingested = await backend.ingest(record_factory("doc-1", "1", "content"), principal, partition)
    _, unit, item = ingested.backend_reference.value.split(":")

    def chunk(chunk_id, text, item_id, index=None):
        payload = {"text": text, "document_id": item_id}
        if index is not None:
            payload["chunk_index"] = index
        return {"id": chunk_id, "score": 0.5, "payload": payload}

    runtime.entries = [
        {
            "kind": "hybrid",
            "text": "rendered context",
            "dataset_id": unit,
            "raw": {
                "result_object": {
                    "chunks": [
                        chunk("c1", "resolved passage", item, 3),
                        chunk("c2", "orphan passage", "item-the-engine-never-wrote"),
                    ]
                }
            },
        },
        {
            "kind": "hybrid",
            "text": "other unit",
            "dataset_id": "unit-outside-the-request",
            "raw": {"result_object": {"chunks": [chunk("c3", "foreign passage", item)]}},
        },
    ]

    result = await backend.query(QueryRequest("passage"), principal, (partition,))

    [evidence] = result.evidence
    assert (evidence.record_id, evidence.source_id) == ("doc-1", "source-incidents")
    assert (evidence.passage, evidence.location) == ("resolved passage", "chunk:3")
    assert unit not in repr(result) and item not in repr(result)


@pytest.mark.asyncio
async def test_runtime_pins_the_retriever_and_disables_routing(monkeypatch):
    captured = {}

    class SearchType(enum.Enum):
        HYBRID_COMPLETION = "HYBRID_COMPLETION"

    async def recall(**kwargs):
        captured.update(kwargs)
        return []

    fake = types.ModuleType("cognee")
    fake.recall = recall
    for name in ("cognee", "cognee.modules", "cognee.modules.search"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    search_types = types.ModuleType("cognee.modules.search.types")
    search_types.SearchType = SearchType
    monkeypatch.setitem(sys.modules, "cognee.modules.search.types", search_types)
    runtime = CogneeRuntime(KnowledgeBackendSettings())
    monkeypatch.setattr(runtime, "_module", lambda: fake)
    unit = str(uuid4())

    await runtime.recall(QueryRequest("question", 5), (_CogneeBinding("n", unit),), object())

    assert captured["query_type"] is SearchType.HYBRID_COMPLETION
    assert captured["auto_route"] is False
    assert captured["only_context"] is True and captured["include_references"] is True
    assert captured["dataset_ids"] == [UUID(unit)] and captured["top_k"] == 5

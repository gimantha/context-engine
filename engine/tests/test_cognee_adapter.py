"""Private adapter translation and isolation tests."""

from __future__ import annotations

import asyncio
import enum
import logging
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
    _keep_process_logging,
    _native_item_id,
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
        self.written = {}
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
        self.written.setdefault(native_id, []).append(item_id)
        return _NativeIngestion(native_id, item_id, True)

    async def list_items(self, binding, user):
        self.calls.append(("list_items", binding.dataset_id))
        # The live listing returns stored rows with attributes, not mappings or models.
        return [types.SimpleNamespace(**item) for item in self.items.get(binding.dataset_id, [])]

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
        # The live-verified shape of the pinned chunk retriever: one entry per chunk.
        return [
            _chunk_entry(binding.dataset_id, item_id, "authorized passage", 0)
            for binding in bindings
            for item_id in self.written.get(binding.dataset_id, [])
        ] + [
            {
                # A record id supplied by the provider is never trusted.
                "kind": "graph_completion",
                "text": "rendered context",
                "metadata": {"record_id": "record-1", "source_id": "source-1"},
                "dataset_id": bindings[0].dataset_id,
                "raw": {"value": "rendered context"},
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


def _chunk_entry(unit, item_id, text, index=None, chunk_id="chunk-1"):
    metadata = {"data_id": item_id, "chunk_id": chunk_id}
    raw = {"id": chunk_id, "text": text, "document_id": item_id}
    if index is not None:
        metadata["chunk_index"] = index
        raw["chunk_index"] = index
    return {
        "kind": "chunk",
        "search_type": "CHUNKS",
        "text": text,
        "score": None,
        "dataset_id": unit,
        "dataset_name": "must-not-escape",
        "metadata": metadata,
        "raw": raw,
        "source": "graph",
    }


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
    [evidence] = queried.evidence
    assert (evidence.passage, evidence.record_id) == ("authorized passage", record.record_id)
    assert "must-not-escape" not in repr(queried) and "record-1" not in repr(queried)
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

    runtime.entries = [
        _chunk_entry(unit, item, "resolved passage", 3, "c1"),
        _chunk_entry(unit, "item-the-engine-never-wrote", "orphan passage", None, "c2"),
        _chunk_entry("unit-outside-the-request", item, "foreign passage", None, "c3"),
        {**_chunk_entry(unit, item, "unknown kind", None, "c4"), "kind": "graph_completion"},
    ]

    result = await backend.query(QueryRequest("passage"), principal, (partition,))

    [evidence] = result.evidence
    assert (evidence.record_id, evidence.source_id) == ("doc-1", "source-incidents")
    assert (evidence.passage, evidence.location) == ("resolved passage", "chunk:3")
    assert unit not in repr(result) and item not in repr(result)


@pytest.mark.asyncio
async def test_partitions_interleave_by_rank(record_factory):
    runtime = _StructuredRuntime()
    backend = CogneeBackend(runtime, resolve_user)
    principal = PrincipalContext("prn_reader", "trace-interleave")
    first, second = AccessPartitionRef("prt_first"), AccessPartitionRef("prt_second")
    refs = {}
    for partition, record_id in ((first, "a"), (first, "b"), (second, "c"), (second, "d")):
        ingested = await backend.ingest(record_factory(record_id, "1", "x"), principal, partition)
        refs[record_id] = ingested.backend_reference.value.split(":")[1:]
    runtime.entries = [
        _chunk_entry(*refs[name], f"passage {name}", 0, f"chunk-{name}") for name in "abcd"
    ]

    result = await backend.query(QueryRequest("passage", limit=3), principal, (first, second))

    assert [item.record_id for item in result.evidence] == ["a", "c", "b"]


@pytest.mark.asyncio
async def test_runtime_pins_the_retriever_and_disables_routing(monkeypatch):
    captured = {}

    class SearchType(enum.Enum):
        CHUNKS = "CHUNKS"

    async def recall(**kwargs):
        captured.update(kwargs)
        return []

    setups = []

    async def setup():
        setups.append(True)
        await asyncio.sleep(0)

    fake = types.ModuleType("cognee")
    fake.recall = recall
    for name in (
        "cognee",
        "cognee.modules",
        "cognee.modules.search",
        "cognee.modules.engine",
        "cognee.modules.engine.operations",
    ):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    search_types = types.ModuleType("cognee.modules.search.types")
    search_types.SearchType = SearchType
    monkeypatch.setitem(sys.modules, "cognee.modules.search.types", search_types)
    setup_module = types.ModuleType("cognee.modules.engine.operations.setup")
    setup_module.setup = setup
    monkeypatch.setitem(sys.modules, "cognee.modules.engine.operations.setup", setup_module)
    runtime = CogneeRuntime(KnowledgeBackendSettings())
    monkeypatch.setattr(runtime, "_module", lambda: fake)
    unit = str(uuid4())
    bindings = (_CogneeBinding("n", unit),)

    await asyncio.gather(
        runtime.recall(QueryRequest("question", 5), bindings, object()),
        runtime.recall(QueryRequest("question", 5), bindings, object()),
    )

    # The provider's own stores are created once per process, before the first operation.
    assert setups == [True]

    assert captured["query_type"] is SearchType.CHUNKS
    assert captured["auto_route"] is False
    # Context-only mode would merge chunks into one rendered string without item ids.
    assert captured["only_context"] is False
    assert captured["dataset_ids"] == [UUID(unit)] and captured["top_k"] == 5


def test_provider_import_cannot_replace_the_engines_log_handlers(tmp_path):
    root = logging.getLogger()
    before = list(root.handlers), root.level
    engine = logging.StreamHandler()
    try:
        root.handlers[:] = [engine]
        root.setLevel(logging.INFO)
        provider_file = logging.FileHandler(tmp_path / "provider.log")
        with _keep_process_logging():
            # What the provider does on import.
            root.handlers.clear()
            root.addHandler(logging.StreamHandler())
            root.addHandler(provider_file)
            root.setLevel(logging.NOTSET)
        handlers, level = list(root.handlers), root.level
    finally:
        provider_file.close()
        root.handlers[:] = before[0]
        root.setLevel(before[1])

    assert handlers == [provider_file, engine]
    assert level == logging.INFO


@pytest.mark.asyncio
async def test_runtime_pins_the_item_id_of_every_write(monkeypatch, record_factory):
    captured = {}
    listed = []

    @dataclass
    class DataItem:
        data: object
        data_id: object = None
        label: object = None
        external_metadata: object = None

    async def remember(**kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(
            status="completed", dataset_id=str(uuid4()), items=list(listed)
        )

    fake = types.ModuleType("cognee")
    fake.remember = remember
    for name in ("cognee", "cognee.tasks", "cognee.tasks.ingestion"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    data_item = types.ModuleType("cognee.tasks.ingestion.data_item")
    data_item.DataItem = DataItem
    monkeypatch.setitem(sys.modules, "cognee.tasks.ingestion.data_item", data_item)
    runtime = CogneeRuntime(KnowledgeBackendSettings())
    monkeypatch.setattr(runtime, "_module", lambda: fake)
    monkeypatch.setattr(runtime, "_prepared", True)
    binding = _CogneeBinding("context-engine-unit", str(uuid4()))
    record = record_factory("doc", "2", "second version")
    pinned = _native_item_id(binding, record)

    # The provider lists every item its run touched; an older item comes first here.
    listed[:] = [{"id": str(uuid4())}, {"id": str(pinned)}]
    written = await runtime.remember(record, binding, object())
    assert captured["data"].data_id == pinned and written.data_id == str(pinned)
    # Content travels as an upload, never as a string the provider could read as a path.
    upload = captured["data"].data
    assert upload.file.read() == b"second version" and "second version" not in repr(upload)
    assert upload.filename.startswith("<")

    listed[:] = [{"id": str(uuid4())}]
    with pytest.raises(BackendError) as unconfirmed:
        await runtime.remember(record, binding, object())
    assert unconfirmed.value.code == BackendErrorCode.PARTIAL_WRITE

    # Each version gets its own item; retrying the same version reuses it.
    older = record_factory("doc", "1", "second version")
    assert _native_item_id(binding, older) != pinned
    assert _native_item_id(binding, record) == pinned


def test_runtime_refuses_a_second_storage_root_in_one_process(monkeypatch, tmp_path):
    import context_engine.knowledge_backend.providers.cognee as adapter

    monkeypatch.setattr(adapter, "_loaded_storage", (tmp_path / "first").resolve())
    runtime = CogneeRuntime(KnowledgeBackendSettings(storage_path=tmp_path / "second"))
    with pytest.raises(BackendError) as refused:
        runtime._module()
    assert refused.value.code == BackendErrorCode.UNSUPPORTED

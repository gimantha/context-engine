"""Private Cognee 1.5.4 mapping for the engine-owned backend port."""

from __future__ import annotations

import hashlib
import inspect
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from context_engine.config import KnowledgeBackendSettings

from ..errors import BackendError, BackendErrorCode
from ..types import (
    AccessPartitionRef,
    BackendCapabilities,
    BackendHealth,
    BackendReference,
    DeletionResult,
    EnrichmentRequest,
    EnrichmentResult,
    EvidenceItem,
    ExpectedRecord,
    IndexingProgress,
    IndexingProgressRequest,
    IngestionResult,
    PrincipalContext,
    QueryRequest,
    QueryResult,
    SourceRecord,
)


@dataclass(frozen=True, slots=True)
class _CogneeBinding:
    dataset_name: str
    dataset_id: str | None = None


@dataclass(frozen=True, slots=True)
class _NativeIngestion:
    dataset_id: str
    data_id: str
    created: bool


class _CogneeRuntimePort(Protocol):
    """Private seam between engine translation and native SDK calls."""

    async def remember(
        self, record: SourceRecord, binding: _CogneeBinding, user: Any
    ) -> _NativeIngestion:
        """Invoke native ingestion and return stable native identifiers."""

        ...

    async def recall(
        self, request: QueryRequest, bindings: tuple[_CogneeBinding, ...], user: Any
    ) -> list[Any]:
        """Invoke native retrieval within explicit bindings."""

        ...

    async def update(
        self, record: SourceRecord, binding: _CogneeBinding, data_id: str, user: Any
    ) -> None:
        """Invoke native update for one bound record."""

        ...

    async def improve(self, binding: _CogneeBinding, user: Any) -> Any:
        """Invoke explicit native enrichment for one binding."""

        ...

    async def forget(self, binding: _CogneeBinding, data_id: str, user: Any) -> Any:
        """Invoke native deletion for one bound record."""

        ...

    async def list_items(self, binding: _CogneeBinding, user: Any) -> list[Any]:
        """List the native content items stored in one binding."""

        ...

    async def processing_states(self, bindings: tuple[_CogneeBinding, ...]) -> dict[str, Any]:
        """Return the latest native processing-run state keyed by native binding id."""

        ...

    async def health(self) -> tuple[bool, str]:
        """Report native runtime readiness and a safe detail string."""

        ...


class _BindingStore:
    """Spike-only binding store; M1 replaces this with control-database persistence."""

    def __init__(self, prefix: str = "context-engine") -> None:
        self._prefix = prefix
        self._bindings: dict[str, _CogneeBinding] = {}

    def resolve(self, partition: AccessPartitionRef) -> _CogneeBinding:
        """Return or deterministically create the binding for a partition."""

        current = self._bindings.get(partition.value)
        if current:
            return current
        # Hash the engine identifier so the native name discloses no caller-controlled value.
        suffix = hashlib.sha256(partition.value.encode()).hexdigest()[:20]
        binding = _CogneeBinding(dataset_name=f"{self._prefix}-{suffix}")
        self._bindings[partition.value] = binding
        return binding

    def set_dataset_id(self, partition: AccessPartitionRef, dataset_id: str) -> None:
        """Attach the native isolation identifier learned during ingestion."""

        current = self.resolve(partition)
        self._bindings[partition.value] = _CogneeBinding(current.dataset_name, dataset_id)


def _native_reference(dataset_id: str, data_id: str) -> BackendReference:
    return BackendReference(f"cognee:{dataset_id}:{data_id}")


def _parse_reference(reference: BackendReference) -> tuple[str, str]:
    prefix, separator, remainder = reference.value.partition(":")
    dataset_id, second_separator, data_id = remainder.partition(":")
    if prefix != "cognee" or not separator or not second_separator or not dataset_id or not data_id:
        raise BackendError(BackendErrorCode.INVALID_INPUT, "Invalid backend reference")
    return dataset_id, data_id


class CogneeBackend:
    """Private adapter that translates all native values at the port boundary."""

    def __init__(
        self,
        runtime: _CogneeRuntimePort,
        user_resolver: Callable[[PrincipalContext], Awaitable[Any]],
        bindings: _BindingStore | None = None,
    ) -> None:
        self._runtime = runtime
        self._user_resolver = user_resolver
        self._bindings = bindings or _BindingStore()
        self._record_references: dict[tuple[str, str], BackendReference] = {}
        self._reference_records: dict[str, str] = {}

    @staticmethod
    def _partitions(value: tuple[AccessPartitionRef, ...]) -> None:
        if not value:
            raise BackendError(
                BackendErrorCode.ACCESS_DENIED,
                "At least one authorized access partition is required",
            )

    async def ingest(
        self,
        record: SourceRecord,
        principal: PrincipalContext,
        partition: AccessPartitionRef,
    ) -> IngestionResult:
        """Translate and ingest one engine record in an explicit partition."""

        self._partitions((partition,))
        try:
            user = await self._user_resolver(principal)
            result = await self._runtime.remember(record, self._bindings.resolve(partition), user)
            self._bindings.set_dataset_id(partition, result.dataset_id)
            reference = _native_reference(result.dataset_id, result.data_id)
            self._record_references[(partition.value, record.record_id)] = reference
            self._reference_records[reference.value] = record.record_id
            return IngestionResult(
                record.record_id,
                record.version,
                reference,
                result.created,
            )
        except BackendError:
            raise
        except Exception as exc:
            raise _translate_error(exc) from exc

    async def query(
        self,
        request: QueryRequest,
        principal: PrincipalContext,
        authorized_partitions: tuple[AccessPartitionRef, ...],
    ) -> QueryResult:
        """Retrieve and translate evidence from authorized bindings."""

        self._partitions(authorized_partitions)
        bindings = tuple(self._bindings.resolve(item) for item in authorized_partitions)
        if any(binding.dataset_id is None for binding in bindings):
            raise BackendError(BackendErrorCode.NOT_FOUND, "Access partition is not initialized")
        try:
            user = await self._user_resolver(principal)
            native_results = await self._runtime.recall(request, bindings, user)
            evidence = tuple(
                _translate_evidence(item, index) for index, item in enumerate(native_results)
            )
            return QueryResult(evidence[: request.limit], insufficient_evidence=not evidence)
        except BackendError:
            raise
        except Exception as exc:
            raise _translate_error(exc) from exc

    async def update(
        self,
        record: SourceRecord,
        principal: PrincipalContext,
        partition: AccessPartitionRef,
    ) -> IngestionResult:
        """Update a previously bound engine record."""

        self._partitions((partition,))
        binding = self._bindings.resolve(partition)
        if binding.dataset_id is None:
            raise BackendError(BackendErrorCode.NOT_FOUND, "Access partition is not initialized")
        reference = self._record_references.get((partition.value, record.record_id))
        if reference is None:
            raise BackendError(BackendErrorCode.NOT_FOUND, "Record binding is not initialized")
        _, data_id = _parse_reference(reference)
        try:
            user = await self._user_resolver(principal)
            await self._runtime.update(record, binding, data_id, user)
            return IngestionResult(record.record_id, record.version, reference, created=False)
        except BackendError:
            raise
        except Exception as exc:
            raise _translate_error(exc) from exc

    async def enrich(
        self,
        request: EnrichmentRequest,
        principal: PrincipalContext,
        authorized_partitions: tuple[AccessPartitionRef, ...],
    ) -> EnrichmentResult:
        """Run explicit native enrichment for authorized bindings."""

        self._partitions(authorized_partitions)
        bindings = tuple(self._bindings.resolve(item) for item in authorized_partitions)
        if any(binding.dataset_id is None for binding in bindings):
            raise BackendError(BackendErrorCode.NOT_FOUND, "Access partition is not initialized")
        try:
            user = await self._user_resolver(principal)
            for binding in bindings:
                await self._runtime.improve(binding, user)
            return EnrichmentResult(request.operation_id, affected_records=0)
        except BackendError:
            raise
        except Exception as exc:
            raise _translate_error(exc) from exc

    async def delete(
        self,
        reference: BackendReference,
        principal: PrincipalContext,
        partition: AccessPartitionRef,
    ) -> DeletionResult:
        """Delete one bound record after validating its partition scope."""

        self._partitions((partition,))
        dataset_id, data_id = _parse_reference(reference)
        binding = self._bindings.resolve(partition)
        # A valid opaque reference is still unusable outside its engine-resolved partition.
        if binding.dataset_id != dataset_id:
            raise BackendError(BackendErrorCode.ACCESS_DENIED, "Backend reference is out of scope")
        try:
            user = await self._user_resolver(principal)
            await self._runtime.forget(binding, data_id, user)
            record_id = self._reference_records.pop(reference.value, "unknown")
            if record_id != "unknown":
                self._record_references.pop((partition.value, record_id), None)
            return DeletionResult(record_id=record_id, deleted=True)
        except BackendError:
            raise
        except Exception as exc:
            raise _translate_error(exc) from exc

    async def indexing_progress(
        self,
        request: IndexingProgressRequest,
        principal: PrincipalContext,
        authorized_partitions: tuple[AccessPartitionRef, ...],
    ) -> IndexingProgress:
        """Attribute native per-binding processing state to one source's record versions.

        Native status is kept per binding run, not per item, so a present item takes the state
        of its binding's latest run. Items are matched to the source through the engine
        metadata attached at ingestion. Uninitialized bindings hold nothing.
        """

        self._partitions(authorized_partitions)
        allowed = {item.value for item in authorized_partitions}
        if any(item.partition.value not in allowed for item in request.records):
            raise BackendError(BackendErrorCode.ACCESS_DENIED, "Record partition is out of scope")
        grouped: dict[str, list[ExpectedRecord]] = {}
        for item in request.records:
            grouped.setdefault(item.partition.value, []).append(item)
        bindings = {value: self._bindings.resolve(AccessPartitionRef(value)) for value in grouped}
        initialized = tuple(binding for binding in bindings.values() if binding.dataset_id)
        counts = {"indexed": 0, "indexing": 0, "failed": 0, "missing": 0}
        try:
            user = await self._user_resolver(principal)
            states = await self._runtime.processing_states(initialized) if initialized else {}
            for value, items in grouped.items():
                binding = bindings[value]
                if binding.dataset_id is None:
                    counts["missing"] += len(items)
                    continue
                present = set()
                for entry in await self._runtime.list_items(binding, user):
                    metadata = _item_metadata(entry)
                    if metadata.get("source_id") == request.source_id:
                        present.add(
                            (str(metadata.get("record_id")), str(metadata.get("source_version")))
                        )
                state = _run_state(states.get(binding.dataset_id))
                for item in items:
                    key = (item.record_id, item.version)
                    counts[state if key in present else "missing"] += 1
            return IndexingProgress(expected=len(request.records), **counts)
        except BackendError:
            raise
        except Exception as exc:
            raise _translate_error(exc) from exc

    async def health(self) -> BackendHealth:
        """Translate native runtime readiness into engine health."""

        try:
            ready, detail = await self._runtime.health()
            return BackendHealth(
                ready,
                detail,
                BackendCapabilities(True, True, True, isolation_enforced=True),
            )
        except Exception as exc:
            return BackendHealth(False, f"provider unavailable: {type(exc).__name__}")


class CogneeRuntime:
    """Lazy SDK wrapper. All Cognee imports and native calls stay in this module."""

    def __init__(self, settings: KnowledgeBackendSettings | None = None) -> None:
        self._settings = settings or KnowledgeBackendSettings.from_env()

    def _module(self) -> Any:
        """Load the SDK only after applying the engine-owned configuration."""

        _apply_native_environment(self._settings)
        try:
            import cognee
        except ImportError as exc:
            raise BackendError(
                BackendErrorCode.UNAVAILABLE,
                "The private provider dependency is not installed",
            ) from exc
        return cognee

    async def remember(
        self, record: SourceRecord, binding: _CogneeBinding, user: Any
    ) -> _NativeIngestion:
        """Create the native content item and invoke native ingestion."""

        cognee = self._module()
        from cognee.tasks.ingestion.data_item import DataItem

        native_record = DataItem(
            data=record.content,
            label=record.title,
            external_metadata={
                "record_id": record.record_id,
                "source_id": record.source_id,
                "source_version": record.version,
                "content_hash": record.content_hash,
                **dict(record.attributes),
            },
        )
        kwargs: dict[str, Any] = {
            "data": native_record,
            "dataset_name": binding.dataset_name,
            "user": user,
            "self_improvement": False,
            "run_in_background": False,
            "raise_on_error": True,
        }
        if binding.dataset_id:
            kwargs["dataset_id"] = UUID(binding.dataset_id)
        result = await cognee.remember(**kwargs)
        if getattr(result, "status", None) == "errored":
            raise BackendError(BackendErrorCode.PARTIAL_WRITE, "Provider ingestion failed")
        dataset_id = getattr(result, "dataset_id", None)
        items = getattr(result, "items", None) or []
        data_id = next((item.get("id") for item in items if item.get("id")), None)
        if not dataset_id or not data_id:
            raise BackendError(
                BackendErrorCode.UNSUPPORTED,
                "Provider did not return stable dataset and record identifiers",
            )
        return _NativeIngestion(str(dataset_id), str(data_id), created=True)

    async def recall(
        self, request: QueryRequest, bindings: tuple[_CogneeBinding, ...], user: Any
    ) -> list[Any]:
        """Invoke native retrieval with explicit binding identifiers."""

        cognee = self._module()
        ids = [UUID(item.dataset_id) for item in bindings if item.dataset_id]
        return await cognee.recall(
            query_text=request.text,
            dataset_ids=ids,
            top_k=request.limit,
            only_context=True,
            include_references=True,
            user=user,
        )

    async def update(
        self, record: SourceRecord, binding: _CogneeBinding, data_id: str, user: Any
    ) -> None:
        """Invoke the native update operation for one bound record."""

        cognee = self._module()
        await cognee.update(
            data_id=UUID(data_id),
            data=record.content,
            dataset_id=UUID(binding.dataset_id),
            user=user,
            chunk_level_diff=True,
        )

    async def improve(self, binding: _CogneeBinding, user: Any) -> Any:
        """Invoke explicit native enrichment for one binding."""

        cognee = self._module()
        return await cognee.improve(dataset=UUID(binding.dataset_id), user=user)

    async def forget(self, binding: _CogneeBinding, data_id: str, user: Any) -> Any:
        """Invoke native deletion for one explicitly bound record."""

        cognee = self._module()
        return await cognee.forget(
            data_id=UUID(data_id),
            dataset_id=UUID(binding.dataset_id),
            user=user,
        )

    async def list_items(self, binding: _CogneeBinding, user: Any) -> list[Any]:
        """List native content items in one binding with the resolved user."""

        cognee = self._module()
        return list(await cognee.datasets.list_data(UUID(binding.dataset_id), user=user))

    async def processing_states(self, bindings: tuple[_CogneeBinding, ...]) -> dict[str, Any]:
        """Read the latest processing-run state per binding from the native status API."""

        cognee = self._module()
        ids = [UUID(item.dataset_id) for item in bindings if item.dataset_id]
        # With no pipeline names the SDK returns a flat map for its processing pipeline.
        result = await cognee.datasets.get_progress(ids)
        return {str(key): value for key, value in (result or {}).items()}

    async def health(self) -> tuple[bool, str]:
        """Load the pinned SDK and return an engine-owned readiness detail."""

        self._module()
        return True, "knowledge provider ready"


def _translate_evidence(value: Any, index: int) -> EvidenceItem:
    if hasattr(value, "model_dump"):
        raw = value.model_dump()
    elif isinstance(value, Mapping):
        raw = dict(value)
    else:
        raw = {"text": str(value)}

    passage = str(raw.get("text") or raw.get("content") or raw.get("result") or "")
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}
    external_metadata = (
        metadata.get("external_metadata")
        if isinstance(metadata.get("external_metadata"), Mapping)
        else raw.get("external_metadata")
        if isinstance(raw.get("external_metadata"), Mapping)
        else {}
    )
    metadata = {**metadata, **external_metadata}
    record_id = str(
        metadata.get("record_id")
        or metadata.get("source_record_id")
        or raw.get("record_id")
        or f"unresolved-{index}"
    )
    source_id = str(metadata.get("source_id") or raw.get("source_id") or "unresolved")
    source_version = str(
        metadata.get("source_version") or raw.get("source_version") or "unresolved"
    )
    digest = hashlib.sha256(
        f"{record_id}\0{source_version}\0{index}\0{passage}".encode()
    ).hexdigest()[:24]
    return EvidenceItem(
        evidence_id=f"evi_{digest}",
        record_id=record_id,
        source_id=source_id,
        source_version=source_version,
        passage=passage,
        score=float(raw.get("score") or 0.0),
        location=str(metadata.get("location")) if metadata.get("location") else None,
    )


_PROCESSING_PIPELINE = "cognify_pipeline"


def _item_metadata(entry: Any) -> Mapping[str, Any]:
    """Return the engine metadata attached to a native content item."""

    raw = entry.model_dump() if hasattr(entry, "model_dump") else entry
    if not isinstance(raw, Mapping):
        return {}
    metadata = raw.get("external_metadata")
    return metadata if isinstance(metadata, Mapping) else {}


def _run_state(value: Any) -> str:
    """Map a native processing-run state to an engine indexing state name."""

    if isinstance(value, Mapping) and "status" not in value and _PROCESSING_PIPELINE in value:
        value = value[_PROCESSING_PIPELINE]
    status = value.get("status") if isinstance(value, Mapping) else value
    name = str(getattr(status, "value", status) or "").upper()
    if name.endswith("COMPLETED"):
        return "indexed"
    if name.endswith("ERRORED"):
        return "failed"
    # A started, initiated, or not-yet-recorded run means the items are still processing.
    return "indexing"


def _translate_error(exc: Exception) -> BackendError:
    name = type(exc).__name__.lower()
    if "permission" in name or "unauthorized" in name or "forbidden" in name:
        return BackendError(BackendErrorCode.ACCESS_DENIED, "Knowledge access denied")
    if "timeout" in name:
        return BackendError(BackendErrorCode.TIMEOUT, "Knowledge backend timed out", retryable=True)
    if "notfound" in name or "not_found" in name:
        return BackendError(BackendErrorCode.NOT_FOUND, "Knowledge resource not found")
    if "validation" in name or isinstance(exc, (TypeError, ValueError)):
        return BackendError(BackendErrorCode.INVALID_INPUT, "Knowledge request is invalid")
    return BackendError(
        BackendErrorCode.UNAVAILABLE,
        "Knowledge backend operation failed",
        retryable=True,
    )


def _native_bool(value: bool) -> str:
    """Format an engine boolean for the native provider environment."""

    return "true" if value else "false"


def _apply_native_environment(settings: KnowledgeBackendSettings) -> None:
    """Translate engine settings into native names inside the private boundary."""

    storage = settings.storage_path.resolve()
    native_values = {
        "TELEMETRY_DISABLED": _native_bool(not settings.telemetry_enabled),
        "COGNEE_LOG_FILE": _native_bool(settings.file_logging_enabled),
        "COGNEE_LOGS_DIR": str(storage / "logs"),
        "CACHING": _native_bool(settings.query_cache_enabled),
        "ENABLE_BACKEND_ACCESS_CONTROL": _native_bool(settings.access_control_required),
        "REQUIRE_AUTHENTICATION": _native_bool(settings.access_control_required),
        "ACCEPT_LOCAL_FILE_PATH": _native_bool(settings.local_content_access_enabled),
        "ALLOW_HTTP_REQUESTS": _native_bool(settings.remote_content_access_enabled),
        "ALLOW_CYPHER_QUERY": _native_bool(settings.raw_graph_query_enabled),
        "DB_PROVIDER": settings.relational_store,
        "GRAPH_DATABASE_PROVIDER": settings.graph_store,
        "GRAPH_DATASET_DATABASE_HANDLER": settings.graph_store,
        "VECTOR_DB_PROVIDER": settings.vector_store,
        "VECTOR_DATASET_DATABASE_HANDLER": settings.vector_store,
        "LLM_PROVIDER": settings.model_provider,
        "LLM_MODEL": settings.model_name,
        "LLM_API_KEY": settings.model_api_key,
        "EMBEDDING_PROVIDER": settings.embedding_provider,
        "EMBEDDING_MODEL": settings.embedding_model,
        "EMBEDDING_DIMENSIONS": str(settings.embedding_dimensions),
        "EMBEDDING_API_KEY": settings.embedding_api_key,
        "SYSTEM_ROOT_DIRECTORY": str(storage / "system"),
        "DATA_ROOT_DIRECTORY": str(storage / "data"),
        "CACHE_ROOT_DIRECTORY": str(storage / "cache"),
    }
    # Overwrite inherited native settings so callers cannot bypass engine policy.
    os.environ.update(native_values)


def assert_runtime_matches_pinned_sdk(runtime: CogneeRuntime | None = None) -> None:
    """Fail fast when the installed SDK signatures drift from the M0 mapping."""

    cognee = (runtime or CogneeRuntime())._module()
    required = {
        "remember": {"data", "dataset_name", "dataset_id", "self_improvement"},
        "recall": {"query_text", "dataset_ids", "top_k", "user"},
        "update": {"data_id", "data", "dataset_id", "user"},
        "forget": {"data_id", "dataset_id", "user"},
        "improve": {"dataset"},
    }
    for operation, parameters in required.items():
        function = getattr(cognee, operation, None)
        if function is None:
            raise RuntimeError(f"Pinned provider is missing {operation}")
        actual = set(inspect.signature(function).parameters)
        missing = parameters - actual
        if missing:
            raise RuntimeError(
                f"Pinned provider {operation} signature is missing {sorted(missing)}"
            )
    status_api = getattr(cognee, "datasets", None)
    for operation, parameters in {
        "get_progress": {"dataset_ids", "pipeline_names"},
        "list_data": {"dataset_id", "user"},
    }.items():
        function = getattr(status_api, operation, None)
        if function is None:
            raise RuntimeError(f"Pinned provider status API is missing {operation}")
        missing = parameters - set(inspect.signature(function).parameters)
        if missing:
            raise RuntimeError(
                f"Pinned provider {operation} signature is missing {sorted(missing)}"
            )

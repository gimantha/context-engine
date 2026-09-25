"""Private Cognee 1.5.4 mapping for the engine-owned backend port."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from context_engine.config import KnowledgeBackendSettings

from ..errors import BackendError, BackendErrorCode
from ..state import BackendStateStore, InMemoryBackendState
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

    async def grant_read(self, binding: _CogneeBinding, reader: Any, owner: Any) -> None:
        """Give a native user read permission on one binding, acting as its owner."""

        ...

    async def revoke_read(self, binding: _CogneeBinding, reader: Any, owner: Any) -> None:
        """Remove a native user's read permission on one binding, acting as its owner."""

        ...

    async def improve(self, binding: _CogneeBinding, user: Any) -> Any:
        """Invoke explicit native enrichment for one binding."""

        ...

    async def forget(self, binding: _CogneeBinding, data_id: str, user: Any) -> Any:
        """Invoke native deletion for one bound record."""

        ...

    async def ensure_user(self, handle: str) -> Any:
        """Return the native user with this deterministic handle, creating it once."""

        ...

    async def get_user(self, native_id: str) -> Any:
        """Return a native user by its native identifier."""

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
    """Resolve partition bindings through the engine's durable backend state."""

    def __init__(self, state: BackendStateStore, prefix: str = "context-engine") -> None:
        self._state = state
        self._prefix = prefix

    def resolve(self, partition: AccessPartitionRef) -> _CogneeBinding:
        """Return the stored binding, or the deterministic unbound one for a new partition."""

        stored = self._state.get_binding(partition.value)
        if stored:
            value = json.loads(stored)
            return _CogneeBinding(dataset_name=value["name"], dataset_id=value.get("id"))
        # Hash the engine identifier so the native name discloses no caller-controlled value.
        suffix = hashlib.sha256(partition.value.encode()).hexdigest()[:20]
        return _CogneeBinding(dataset_name=f"{self._prefix}-{suffix}")

    def set_dataset_id(self, partition: AccessPartitionRef, dataset_id: str) -> None:
        """Persist the native isolation identifier learned during the first ingestion.

        A partition that is already bound must never move to another native unit: that would
        leave its earlier content behind under an identifier the engine no longer tracks.
        """

        current = self.resolve(partition)
        if current.dataset_id == dataset_id:
            return
        if current.dataset_id is not None:
            raise BackendError(
                BackendErrorCode.PARTIAL_WRITE,
                "Provider bound the partition to an unexpected isolation unit",
            )
        encoded = json.dumps({"name": current.dataset_name, "id": dataset_id}, sort_keys=True)
        self._state.put_binding(partition.value, encoded)


class CogneeIdentityResolver:
    """Map engine principals to native users durably; there is never a default user.

    Each principal gets its own ordinary native account under a deterministic handle, so a
    crash between creating the account and recording it recovers on the next call.
    """

    def __init__(self, runtime: _CogneeRuntimePort, state: BackendStateStore) -> None:
        self._runtime = runtime
        self._state = state

    async def __call__(self, principal: PrincipalContext) -> Any:
        """Return the native user for an engine principal."""

        stored = self._state.get_identity(principal.principal_id)
        if stored is not None:
            return await self._runtime.get_user(stored)
        user = await self._runtime.ensure_user(_native_handle(principal.principal_id))
        native_id = str(getattr(user, "id", "") or "")
        if not native_id:
            raise BackendError(
                BackendErrorCode.UNSUPPORTED, "Provider did not return a stable identity"
            )
        effective = self._state.claim_identity(principal.principal_id, native_id)
        # A concurrent resolver may have claimed first; everyone uses the stored identity.
        return user if effective == native_id else await self._runtime.get_user(effective)


def _native_handle(principal_id: str) -> str:
    """Return a deterministic native login handle that reveals nothing about the principal."""

    digest = hashlib.sha256(principal_id.encode()).hexdigest()[:32]
    return f"engine-{digest}@context-engine.invalid"


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
        state: BackendStateStore | None = None,
    ) -> None:
        self._runtime = runtime
        self._user_resolver = user_resolver
        # Production passes the control-database store; the in-memory default serves tests.
        self._state = state or InMemoryBackendState()
        self._bindings = _BindingStore(self._state)

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
            self._state.put_record_reference(
                partition.value, record.source_id, record.record_id, reference.value
            )
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
            units = {
                binding.dataset_id: partition.value
                for binding, partition in zip(bindings, authorized_partitions, strict=True)
                if binding.dataset_id
            }
            evidence = self._translate_results(native_results, units)
            return QueryResult(evidence[: request.limit], insufficient_evidence=not evidence)
        except BackendError:
            raise
        except Exception as exc:
            raise _translate_error(exc) from exc

    def _translate_results(
        self, native_results: list[Any], units: dict[str, str]
    ) -> tuple[EvidenceItem, ...]:
        """Turn retrieved native chunks into engine evidence through durable references.

        Every chunk names the native item it came from. Only items the engine wrote, whose
        reference is still recorded, and that sit in an authorized partition become evidence;
        anything else, including leftovers of replaced versions, stays out (ADR 0008). The
        version is left to the engine, which takes it from the ledger.
        """

        evidence: dict[str, EvidenceItem] = {}
        single_unit = next(iter(units)) if len(units) == 1 else None
        for index, native in enumerate(native_results):
            entry = _as_mapping(native)
            chunks = _native_chunks(entry)
            if not chunks:
                legacy = _translate_evidence(native, index)
                if not legacy.record_id.startswith("unresolved"):
                    evidence.setdefault(legacy.evidence_id, legacy)
                continue
            raw = _as_mapping(entry.get("raw"))
            unit = str(entry.get("dataset_id") or raw.get("dataset_id") or single_unit or "")
            partition = units.get(unit)
            if partition is None:
                continue
            for chunk in chunks:
                payload = _as_mapping(chunk.get("payload")) or chunk
                item_id = payload.get("document_id")
                passage = str(payload.get("text") or "").strip()
                if not item_id or not passage:
                    continue
                location = self._state.find_record(_native_reference(unit, str(item_id)).value)
                if location is None or location[0] != partition:
                    continue
                _, source_id, record_id = location
                chunk_id = str(chunk.get("id") or payload.get("id") or "")
                digest = hashlib.sha256(
                    f"{partition}\0{source_id}\0{record_id}\0{chunk_id}\0{passage}".encode()
                ).hexdigest()[:24]
                chunk_index = payload.get("chunk_index")
                item = EvidenceItem(
                    evidence_id=f"evi_{digest}",
                    record_id=record_id,
                    source_id=source_id,
                    source_version="",
                    passage=passage,
                    score=float(chunk.get("score") or 0.0),
                    location=f"chunk:{chunk_index}" if chunk_index is not None else None,
                )
                evidence.setdefault(item.evidence_id, item)
        return tuple(evidence.values())

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
        stored = self._state.get_record_reference(
            partition.value, record.source_id, record.record_id
        )
        if stored is None:
            raise BackendError(BackendErrorCode.NOT_FOUND, "Record binding is not initialized")
        _, previous_item = _parse_reference(BackendReference(stored))
        try:
            user = await self._user_resolver(principal)
            # Replace rather than edit in place: the new item carries the new version in its
            # metadata, which evidence lineage and progress rely on (ADR 0007).
            result = await self._runtime.remember(record, binding, user)
            if result.dataset_id != binding.dataset_id:
                raise BackendError(
                    BackendErrorCode.PARTIAL_WRITE,
                    "Provider wrote the replacement to an unexpected isolation unit",
                )
            reference = _native_reference(result.dataset_id, result.data_id)
            self._state.put_record_reference(
                partition.value, record.source_id, record.record_id, reference.value
            )
            if result.data_id != previous_item:
                await self._runtime.forget(binding, previous_item, user)
            return IngestionResult(record.record_id, record.version, reference, created=False)
        except BackendError:
            raise
        except Exception as exc:
            raise _translate_error(exc) from exc

    async def grant_read(
        self,
        partition: AccessPartitionRef,
        reader: PrincipalContext,
        principal: PrincipalContext,
    ) -> None:
        """Give a reader native read permission on the partition's binding."""

        await self._change_read(partition, reader, principal, grant=True)

    async def revoke_read(
        self,
        partition: AccessPartitionRef,
        reader: PrincipalContext,
        principal: PrincipalContext,
    ) -> None:
        """Remove a reader's native read permission on the partition's binding."""

        await self._change_read(partition, reader, principal, grant=False)

    async def _change_read(
        self,
        partition: AccessPartitionRef,
        reader: PrincipalContext,
        principal: PrincipalContext,
        *,
        grant: bool,
    ) -> None:
        self._partitions((partition,))
        binding = self._bindings.resolve(partition)
        if binding.dataset_id is None:
            raise BackendError(BackendErrorCode.NOT_FOUND, "Access partition is not initialized")
        try:
            owner = await self._user_resolver(principal)
            user = await self._user_resolver(reader)
            if grant:
                await self._runtime.grant_read(binding, user, owner)
            else:
                await self._runtime.revoke_read(binding, user, owner)
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
            location = self._state.find_record(reference.value)
            record_id = location[2] if location and location[0] == partition.value else "unknown"
            self._state.delete_record_reference(reference.value)
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
        """Invoke native retrieval with the pinned retriever and explicit binding identifiers."""

        cognee = self._module()
        from cognee.modules.search.types import SearchType

        ids = [UUID(item.dataset_id) for item in bindings if item.dataset_id]
        # A fixed retriever keeps result shapes stable, and turning routing off means no
        # question can be routed to raw graph queries however it is phrased.
        return await cognee.recall(
            query_text=request.text,
            query_type=SearchType.HYBRID_COMPLETION,
            auto_route=False,
            dataset_ids=ids,
            top_k=request.limit,
            only_context=True,
            include_references=True,
            user=user,
        )

    async def grant_read(self, binding: _CogneeBinding, reader: Any, owner: Any) -> None:
        """Give read permission on one binding through the native sharing API."""

        self._module()
        from cognee.modules.users.permissions.methods import (
            authorized_give_permission_on_datasets,
        )

        await authorized_give_permission_on_datasets(
            reader.id, [UUID(binding.dataset_id)], "read", owner.id
        )

    async def revoke_read(self, binding: _CogneeBinding, reader: Any, owner: Any) -> None:
        """Remove read permission on one binding through the native sharing API."""

        self._module()
        from cognee.modules.users.permissions.methods import (
            authorized_revoke_permission_on_datasets,
        )

        await authorized_revoke_permission_on_datasets(
            reader.id, [UUID(binding.dataset_id)], "read", owner.id
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

    async def ensure_user(self, handle: str) -> Any:
        """Find the ordinary native account for a handle, creating it on first use."""

        self._module()
        from cognee.modules.users.methods import create_user, get_user_by_email

        existing = await get_user_by_email(handle)
        if existing is not None:
            return existing
        try:
            # The account is an internal handle; its password is never used or stored.
            return await create_user(handle, secrets.token_urlsafe(32))
        except Exception as exc:
            if "alreadyexists" not in type(exc).__name__.lower():
                raise
            # Another process created it between the lookup and the create.
            concurrent = await get_user_by_email(handle)
            if concurrent is None:
                raise
            return concurrent

    async def get_user(self, native_id: str) -> Any:
        """Load a native account by its native identifier."""

        self._module()
        from cognee.modules.users.methods import get_user

        return await get_user(UUID(native_id))

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


def _as_mapping(value: Any) -> dict[str, Any]:
    """Return a plain mapping for a native model, mapping, or anything else."""

    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _native_chunks(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the retrieved chunks of one native search entry, wherever the SDK put them."""

    raw = _as_mapping(entry.get("raw"))
    for container in (raw.get("result_object"), entry.get("result_object"), raw):
        chunks = _as_mapping(container).get("chunks")
        if isinstance(chunks, list):
            return [_as_mapping(chunk) for chunk in chunks]
    return []


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
        "recall": {
            "query_text",
            "query_type",
            "auto_route",
            "dataset_ids",
            "top_k",
            "only_context",
            "include_references",
            "user",
        },
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
    from cognee.modules.users import methods as user_methods
    from cognee.modules.users.permissions import methods as permission_methods

    for operation in (
        "authorized_give_permission_on_datasets",
        "authorized_revoke_permission_on_datasets",
    ):
        function = getattr(permission_methods, operation, None)
        if function is None:
            raise RuntimeError(f"Pinned provider permission API is missing {operation}")
        expected = {"principal_id", "dataset_ids", "permission_name", "owner_id"}
        missing = expected - set(inspect.signature(function).parameters)
        if missing:
            raise RuntimeError(
                f"Pinned provider {operation} signature is missing {sorted(missing)}"
            )

    for operation, parameters in {
        "create_user": {"email", "password"},
        "get_user": {"user_id"},
        "get_user_by_email": {"user_email"},
    }.items():
        function = getattr(user_methods, operation, None)
        if function is None:
            raise RuntimeError(f"Pinned provider identity API is missing {operation}")
        missing = parameters - set(inspect.signature(function).parameters)
        if missing:
            raise RuntimeError(
                f"Pinned provider {operation} signature is missing {sorted(missing)}"
            )
    from cognee.modules.search.types import SearchType

    if not hasattr(SearchType, "HYBRID_COMPLETION"):
        raise RuntimeError("Pinned provider no longer offers the hybrid retriever")
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

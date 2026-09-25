"""Typed domain values for the control plane."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

# The root resource owns every context space. Grants on it apply engine-wide.
ROOT_RESOURCE_ID = "engine"


def partition_key(space_id: str, audiences: Iterable[str]) -> str:
    """Return the internal partition identifier for records sharing exactly these audiences.

    The same audiences always yield the same partition, whatever order the tags arrived in.
    The identifier is private: it never appears in a public payload.
    """

    unique = sorted(set(audiences))
    if not space_id or not unique:
        raise ValueError("a partition needs a space and at least one audience")
    canonical = json.dumps([space_id, unique], separators=(",", ":"))
    return "prt_" + hashlib.sha256(canonical.encode()).hexdigest()[:32]


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(UTC)


class Action(StrEnum):
    """Fine-grained engine actions that grants bind to resources."""

    SPACE_MANAGE = "space.manage"
    SOURCE_MANAGE = "source.manage"
    INGEST_WRITE = "ingest.write"
    CONTEXT_READ = "context.read"
    EVIDENCE_READ = "evidence.read"
    TRACE_READ = "trace.read"
    CONTEXT_ENRICH = "context.enrich"
    RECORD_DELETE = "record.delete"
    ACCESS_MANAGE = "access.manage"


class PrincipalKind(StrEnum):
    """Whether a principal is a person or a service identity."""

    USER = "user"
    SERVICE = "service"


class SourceState(StrEnum):
    """Whether a registered source currently accepts deliveries."""

    READY = "ready"
    PAUSED = "paused"
    FAILED = "failed"


class VersionOrdering(StrEnum):
    """How a source's version strings compare (ADR 0006)."""

    NUMERIC = "numeric"
    LEXICOGRAPHIC = "lexicographic"


class RecordState(StrEnum):
    """Lifecycle state of a source record in the authoritative ledger."""

    ACTIVE = "active"
    DELETED = "deleted"
    QUARANTINED = "quarantined"


class RecordOutcome(StrEnum):
    """What applying one source event did to the ledger."""

    APPLIED = "applied"
    REPLAYED = "replayed"
    IGNORED_OLDER = "ignored_older"
    QUARANTINED = "quarantined"


class IndexState(StrEnum):
    """Whether the knowledge backend matches the ledger for one record."""

    PENDING = "pending"
    INDEXED = "indexed"
    FAILED = "failed"
    RECONCILE_REQUIRED = "reconcile_required"
    NOT_INDEXED = "not_indexed"


class LocationState(StrEnum):
    """State of one physical copy of a record in one backend partition."""

    WRITING = "writing"
    INDEXED = "indexed"
    REMOVING = "removing"
    RECONCILE_REQUIRED = "reconcile_required"


class SpaceState(StrEnum):
    """Lifecycle states of a public context space."""

    PROVISIONING = "provisioning"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"


class JobOperation(StrEnum):
    """Asynchronous operations supported by the control plane."""

    INGESTION = "ingestion"
    UPDATE = "update"
    DELETION = "deletion"
    ENRICHMENT = "enrichment"
    SPACE_DELETION = "space_deletion"


class JobState(StrEnum):
    """Internal durable states of an asynchronous job."""

    ACCEPTED = "accepted"
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def required_action(operation: JobOperation) -> Action:
    """Return the action a job's principal must still hold when the job executes.

    Source-delivered ingestion, update, and delete events are all deliveries and need
    `ingest.write`. A human record deletion arrives with the records API in a later
    milestone and will carry `record.delete` explicitly.
    """

    return {
        JobOperation.INGESTION: Action.INGEST_WRITE,
        JobOperation.UPDATE: Action.INGEST_WRITE,
        JobOperation.DELETION: Action.INGEST_WRITE,
        JobOperation.ENRICHMENT: Action.CONTEXT_ENRICH,
        JobOperation.SPACE_DELETION: Action.SPACE_MANAGE,
    }[operation]


@dataclass(frozen=True, slots=True)
class Principal:
    """Durable engine principal resolved from a verified identity."""

    id: str
    issuer: str
    subject: str
    kind: PrincipalKind
    email: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Grant:
    """Bind a principal or a group to actions on one engine resource."""

    id: str
    resource_id: str
    actions: frozenset[Action]
    principal_id: str | None
    group: str | None
    created_by: str
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        # A grant names exactly one subject so effective permissions stay explainable.
        if (self.principal_id is None) == (self.group is None):
            raise ValueError("grant must name exactly one of principal_id or group")
        if not self.actions:
            raise ValueError("grant must contain at least one action")


@dataclass(frozen=True, slots=True)
class Source:
    """Registered origin that delivers versioned records into one context space."""

    id: str
    space_id: str
    name: str
    type: str
    state: SourceState
    version_ordering: VersionOrdering
    # Trusted source audience tags map to engine audiences; unmapped tags quarantine a record.
    audience_mapping: tuple[tuple[str, str], ...]
    created_at: datetime
    updated_at: datetime

    def version_key(self, version: str) -> tuple[int, int | str]:
        """Return a comparable key for a version string under this source's ordering."""

        if self.version_ordering is VersionOrdering.NUMERIC:
            try:
                return (0, int(version))
            except ValueError as exc:
                raise ValueError("version is not numeric") from exc
        return (1, version)

    def map_audience(self, tags: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Split source audience tags into mapped engine audiences and unmapped tags."""

        mapping = dict(self.audience_mapping)
        mapped = sorted({mapping[tag] for tag in tags if tag in mapping})
        unmapped = sorted({tag for tag in tags if tag not in mapping})
        return tuple(mapped), tuple(unmapped)


@dataclass(frozen=True, slots=True)
class SourceCheckpoint:
    """Connector-owned cursor persisted by the engine for inspection and recovery."""

    source_id: str
    cursor: str
    updated_by: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StagedUpload:
    """Validated bytes staged for one source before an ingestion event references them."""

    id: str
    source_id: str
    principal_id: str
    idempotency_key: str
    content_type: str
    size_bytes: int
    content_hash: str
    created_at: datetime
    expires_at: datetime

    def is_expired(self, now: datetime) -> bool:
        """Return whether the staged object may no longer be referenced."""

        return self.expires_at <= now


@dataclass(frozen=True, slots=True)
class RecordStatus:
    """Current ledger state of one source record, without content."""

    space_id: str
    source_id: str
    source_record_id: str
    state: RecordState
    current_version: str
    source_acl_version: str
    content_hash: str | None
    content_ref: str | None
    audience: tuple[str, ...]
    quarantine_reason: str | None
    created_at: datetime
    updated_at: datetime
    # Private: the partition the record should live in, or None while quarantined or deleted.
    partition_id: str | None = None
    source_url: str | None = None
    index_state: IndexState = IndexState.PENDING
    index_error: str | None = None


@dataclass(frozen=True, slots=True)
class RecordLocation:
    """One physical copy of a record in a backend partition; private reconciliation state."""

    space_id: str
    source_id: str
    source_record_id: str
    partition_id: str
    state: LocationState
    version: str | None
    target_version: str | None
    backend_ref: str | None
    content_hash: str | None
    parser_version: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AccessPartition:
    """Private isolation unit: one set of mapped audiences within one context space."""

    id: str
    space_id: str
    audiences: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RecordTransition:
    """Result of applying one source event to the ledger."""

    outcome: RecordOutcome
    state: RecordState
    current_version: str
    reason: str | None = None


class SyncRunState(StrEnum):
    """Whether a connector is still reading a source for one sync run."""

    READING = "reading"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"


class IndexingState(StrEnum):
    """Outcome of the last collection of a source's indexing progress."""

    OK = "ok"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class SyncRun:
    """Connector-declared window during which it reads a source."""

    id: str
    source_id: str
    state: SyncRunState
    started_by: str
    started_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class JobCounts:
    """Delivery counts for a source in the public job-state model."""

    queued: int = 0
    running: int = 0
    succeeded: int = 0
    failed: int = 0

    @property
    def total(self) -> int:
        """Return every delivery counted."""

        return self.queued + self.running + self.succeeded + self.failed

    @property
    def finished(self) -> int:
        """Return deliveries that will not progress further."""

        return self.succeeded + self.failed


@dataclass(frozen=True, slots=True)
class RecordCounts:
    """Ledger record counts for a source by lifecycle state."""

    active: int = 0
    quarantined: int = 0
    deleted: int = 0


@dataclass(frozen=True, slots=True)
class IndexingSnapshot:
    """Last collected indexing progress of a source, in engine terms only."""

    source_id: str
    state: IndexingState
    collected_at: datetime
    expected: int | None = None
    indexed: int | None = None
    indexing: int | None = None
    failed: int | None = None
    missing: int | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class SourceProgress:
    """Reading, processing, ledger, and indexing state of one source."""

    source_id: str
    sync_run: SyncRun | None
    processing_since: datetime | None
    jobs: JobCounts
    records: RecordCounts
    indexing: IndexingSnapshot | None


@dataclass(frozen=True, slots=True)
class ContextSpace:
    """Public context-space metadata stored by the control plane."""

    id: str
    name: str
    description: str | None
    state: SpaceState
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class IngestionCommand:
    """Validated application command for one source-record event."""

    schema_version: str
    space_id: str
    source_id: str
    source_record_id: str
    source_version: str
    operation: str
    source_observed_at: datetime
    audience: tuple[str, ...]
    source_acl_version: str
    idempotency_key: str
    content_type: str | None = None
    content: str | None = None
    title: str | None = None
    content_ref: str | None = None
    source_url: str | None = None
    content_hash: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical durable payload for the worker."""

        value: dict[str, Any] = {
            "schemaVersion": self.schema_version,
            "spaceId": self.space_id,
            "sourceId": self.source_id,
            "sourceRecordId": self.source_record_id,
            "sourceVersion": self.source_version,
            "operation": self.operation,
            "sourceObservedAt": self.source_observed_at.isoformat(),
            "audience": list(self.audience),
            "sourceAclVersion": self.source_acl_version,
            "idempotencyKey": self.idempotency_key,
        }
        optional = {
            "contentType": self.content_type,
            "content": self.content,
            "title": self.title,
            "contentRef": self.content_ref,
            "sourceUrl": self.source_url,
            "contentHash": self.content_hash,
        }
        value.update({key: item for key, item in optional.items() if item is not None})
        return value

    @property
    def job_operation(self) -> JobOperation:
        """Map the ingestion event operation to a job operation."""

        return {
            "upsert": JobOperation.INGESTION,
            "acl_changed": JobOperation.UPDATE,
            "delete": JobOperation.DELETION,
        }[self.operation]


@dataclass(frozen=True, slots=True)
class Job:
    """Durable asynchronous job including private execution metadata."""

    id: str
    operation: JobOperation
    state: JobState
    idempotency_key: str
    payload: dict[str, Any]
    payload_hash: str
    trace_id: str
    principal_id: str | None
    attempt_count: int
    max_attempts: int
    next_attempt_at: datetime | None
    lease_token: str | None
    lease_expires_at: datetime | None
    result: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def public_state(self) -> str:
        """Map internal scheduling states to the stable REST state model."""

        # Scheduling details are private; callers only need to know that work is queued.
        if self.state in {JobState.ACCEPTED, JobState.RETRY_WAIT}:
            return JobState.QUEUED.value
        return self.state.value

    @property
    def space_id(self) -> str | None:
        """Return the context space the job belongs to when its payload names one."""

        value = self.payload.get("spaceId")
        return value if isinstance(value, str) and value else None

    @property
    def source_id(self) -> str | None:
        """Return the source the job delivers for when its payload names one."""

        value = self.payload.get("sourceId")
        return value if isinstance(value, str) and value else None

    @property
    def resource_id(self) -> str | None:
        """Return the most specific engine resource the job's authorization is checked on."""

        return self.source_id or self.space_id

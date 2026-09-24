"""Application-facing persistence interfaces."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from context_engine.domain import (
    Action,
    ContextSpace,
    Grant,
    IndexingSnapshot,
    Job,
    JobCounts,
    JobOperation,
    JobState,
    Principal,
    RecordCounts,
    RecordStatus,
    Source,
    SourceCheckpoint,
    SourceState,
    StagedUpload,
    SyncRun,
    VersionOrdering,
)


class ControlPlaneStore(Protocol):
    """Persistence operations required by the application service."""

    def create_space(self, name: str, description: str | None) -> ContextSpace:
        """Persist and return a new context space."""

        ...

    def list_spaces(self) -> tuple[ContextSpace, ...]:
        """Return every context space; the service filters by visibility."""

        ...

    def get_space(self, space_id: str) -> ContextSpace | None:
        """Return one context space when it exists."""

        ...

    def enqueue_job(
        self,
        operation: JobOperation,
        idempotency_key: str,
        payload: dict[str, Any],
        trace_id: str,
        max_attempts: int,
        principal_id: str,
    ) -> tuple[Job, bool]:
        """Atomically persist a job and its outbox event for the accepting principal."""

        ...

    def get_job(self, job_id: str) -> Job | None:
        """Return one durable job when it exists."""

        ...

    def list_source_jobs(self, source_id: str, state: JobState | None = None) -> tuple[Job, ...]:
        """Return jobs delivered for one source, optionally filtered by state."""

        ...

    def count_source_jobs(self, source_id: str, since: datetime | None = None) -> JobCounts:
        """Count a source's deliveries by public state, optionally since a time."""

        ...


class SourceStore(Protocol):
    """Source registration, checkpoint, staging, and ledger persistence."""

    def create_source(
        self,
        space_id: str,
        name: str,
        type_: str,
        version_ordering: VersionOrdering,
        audience_mapping: dict[str, str],
    ) -> Source:
        """Register a source in a space."""

        ...

    def list_sources(self, space_id: str) -> tuple[Source, ...]:
        """Return the sources in a space."""

        ...

    def get_source(self, source_id: str) -> Source | None:
        """Return one source when it exists."""

        ...

    def update_source(
        self,
        source_id: str,
        *,
        name: str | None = None,
        state: SourceState | None = None,
        audience_mapping: dict[str, str] | None = None,
    ) -> Source | None:
        """Change source metadata or delivery state."""

        ...

    def get_checkpoint(self, source_id: str) -> SourceCheckpoint | None:
        """Return the connector cursor for a source."""

        ...

    def put_checkpoint(self, source_id: str, cursor: str, updated_by: str) -> SourceCheckpoint:
        """Store the connector cursor for a source."""

        ...

    def create_upload(
        self,
        source_id: str,
        principal_id: str,
        idempotency_key: str,
        content_type: str,
        size_bytes: int,
        content_hash: str,
        ttl_seconds: int,
    ) -> tuple[StagedUpload, bool]:
        """Record a staged upload or return an identical replay."""

        ...

    def get_upload(self, upload_id: str) -> StagedUpload | None:
        """Return one staged upload when it exists."""

        ...

    def get_record(
        self, space_id: str, source_id: str, source_record_id: str
    ) -> RecordStatus | None:
        """Return the ledger state of one record."""

        ...

    def open_sync_run(self, source_id: str, principal_id: str) -> SyncRun:
        """Start a reading window, superseding an unfinished one."""

        ...

    def get_sync_run(self, run_id: str) -> SyncRun | None:
        """Return one sync run when it exists."""

        ...

    def latest_sync_run(self, source_id: str) -> SyncRun | None:
        """Return the most recent sync run of a source."""

        ...

    def complete_sync_run(self, run_id: str) -> SyncRun | None:
        """Mark a reading run completed."""

        ...

    def record_counts(self, source_id: str) -> RecordCounts:
        """Count a source's ledger records by lifecycle state."""

        ...

    def get_indexing_snapshot(self, source_id: str) -> IndexingSnapshot | None:
        """Return the last collected indexing progress of a source."""

        ...


class StagedBytes(Protocol):
    """Byte storage for staged uploads."""

    def write(self, upload_id: str, data: bytes) -> object:
        """Persist the staged bytes atomically."""

        ...

    def exists(self, upload_id: str) -> bool:
        """Return whether the staged bytes are present."""

        ...


def utc_now() -> datetime:
    """Return the current UTC time; a seam for tests."""

    from datetime import UTC

    return datetime.now(UTC)


class GrantStore(Protocol):
    """Grant and principal persistence used by access-management commands."""

    def get_principal(self, principal_id: str) -> Principal | None:
        """Return a principal by engine identifier when it exists."""

        ...

    def resource_chain(self, resource_id: str) -> tuple[str, ...] | None:
        """Return the resource and its ancestors, or None when unknown."""

        ...

    def list_grants(self, resource_id: str) -> tuple[Grant, ...]:
        """Return every grant on one resource."""

        ...

    def get_grant(self, resource_id: str, grant_id: str) -> Grant | None:
        """Return one grant when it exists."""

        ...

    def put_grant(
        self,
        resource_id: str,
        grant_id: str,
        actions: frozenset[Action],
        principal_id: str | None,
        group: str | None,
        created_by: str,
    ) -> Grant:
        """Create or replace a grant."""

        ...

    def delete_grant(self, resource_id: str, grant_id: str) -> bool:
        """Delete a grant and report whether it existed."""

        ...

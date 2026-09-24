"""Application service used by the REST API and future external interfaces.

Every command and query receives the authenticated principal resolved by the transport and
decides access through the authorizer before touching durable state. Invisible resources
answer with not-found so denials reveal nothing about what exists.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from context_engine.domain import (
    ROOT_RESOURCE_ID,
    Action,
    ContextSpace,
    Grant,
    IngestionCommand,
    Job,
    JobState,
    RecordStatus,
    Source,
    SourceCheckpoint,
    SourceProgress,
    SourceState,
    StagedUpload,
    SyncRun,
    SyncRunState,
    VersionOrdering,
)
from context_engine.observability import MetricsRegistry
from context_engine.persistence import IdempotencyConflict
from context_engine.security.authorization import Authorizer
from context_engine.security.identity import AuthenticatedPrincipal

from .errors import (
    AccessDeniedError,
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    UnsupportedContentTypeError,
    ValidationError,
)
from .ports import ControlPlaneStore, GrantStore, SourceStore, StagedBytes, utc_now

_MANAGE_SOURCES = frozenset({Action.SOURCE_MANAGE, Action.SPACE_MANAGE})
_INSPECT_DELIVERIES = frozenset({Action.SOURCE_MANAGE, Action.SPACE_MANAGE, Action.INGEST_WRITE})


@dataclass(frozen=True, slots=True)
class UploadPolicy:
    """Limits applied to staged uploads before an ingestion event may reference them."""

    max_bytes: int
    ttl_seconds: int
    content_types: frozenset[str]


class ContextEngineService:
    """Coordinate public commands and queries over engine-owned ports."""

    def __init__(
        self,
        store: ControlPlaneStore,
        grants: GrantStore,
        authorizer: Authorizer,
        metrics: MetricsRegistry,
        sources: SourceStore,
        staging: StagedBytes,
        upload_policy: UploadPolicy,
        max_job_attempts: int = 5,
        *,
        indexing_enabled: bool = False,
    ) -> None:
        self._store = store
        self._grants = grants
        self._authorizer = authorizer
        self._metrics = metrics
        self._sources = sources
        self._staging = staging
        self._upload_policy = upload_policy
        self._max_job_attempts = max_job_attempts
        self._indexing_enabled = indexing_enabled

    # Authorization helpers

    def _visible(self, principal: AuthenticatedPrincipal, resource_id: str) -> bool:
        return self._authorizer.is_visible(principal.principal_id, principal.groups, resource_id)

    def _require(self, principal: AuthenticatedPrincipal, action: Action, resource_id: str) -> None:
        """Fail closed: unknown or invisible resources are not-found, visible denials are 403."""

        if resource_id != ROOT_RESOURCE_ID and not self._visible(principal, resource_id):
            raise NotFoundError()
        decision = self._authorizer.decide(
            principal.principal_id, principal.groups, action, resource_id, principal.trace_id
        )
        if not decision.allowed:
            raise AccessDeniedError()

    def _require_any(
        self, principal: AuthenticatedPrincipal, actions: frozenset[Action], resource_id: str
    ) -> None:
        """Allow when the principal holds any of the actions; record one decision either way."""

        if not self._visible(principal, resource_id):
            raise NotFoundError()
        held = self._authorizer.effective_actions(
            principal.principal_id, principal.groups, resource_id
        )
        matched = sorted(actions & (held or frozenset()), key=lambda item: item.value)
        # Record the action that granted access, or the primary action when denied.
        action = matched[0] if matched else sorted(actions, key=lambda item: item.value)[0]
        decision = self._authorizer.decide(
            principal.principal_id, principal.groups, action, resource_id, principal.trace_id
        )
        if not decision.allowed:
            raise AccessDeniedError()

    def _visible_source(self, principal: AuthenticatedPrincipal, source_id: str) -> Source:
        source = self._sources.get_source(source_id)
        if source is None or not self._visible(principal, source_id):
            raise NotFoundError("Source not found")
        return source

    # Identity and permissions

    def effective_permissions(
        self, principal: AuthenticatedPrincipal, resource_id: str
    ) -> frozenset[Action]:
        """Return the caller's own effective actions on a visible resource."""

        actions = self._authorizer.effective_actions(
            principal.principal_id, principal.groups, resource_id
        )
        if actions is None or (not actions and resource_id != ROOT_RESOURCE_ID):
            raise NotFoundError()
        return actions

    # Context spaces

    def create_context_space(
        self, principal: AuthenticatedPrincipal, name: str, description: str | None
    ) -> ContextSpace:
        """Create a context space for a principal holding space.manage on the root."""

        self._require(principal, Action.SPACE_MANAGE, ROOT_RESOURCE_ID)
        normalized = name.strip()
        if not normalized:
            raise ValidationError("Context-space name is required")
        space = self._store.create_space(normalized, description)
        self._metrics.increment("context_engine_spaces_created_total")
        return space

    def list_context_spaces(self, principal: AuthenticatedPrincipal) -> tuple[ContextSpace, ...]:
        """List only the context spaces on which the principal holds some action."""

        return tuple(
            space for space in self._store.list_spaces() if self._visible(principal, space.id)
        )

    def get_context_space(self, principal: AuthenticatedPrincipal, space_id: str) -> ContextSpace:
        """Return a visible context space or a generic not-found error."""

        space = self._store.get_space(space_id)
        if space is None or not self._visible(principal, space_id):
            raise NotFoundError("Context space not found")
        return space

    # Sources

    def register_source(
        self,
        principal: AuthenticatedPrincipal,
        space_id: str,
        name: str,
        type_: str,
        version_ordering: VersionOrdering,
        audience_mapping: dict[str, str],
    ) -> Source:
        """Register a source in a space for a principal that manages sources or the space."""

        if self._store.get_space(space_id) is None:
            raise NotFoundError("Context space not found")
        self._require_any(principal, _MANAGE_SOURCES, space_id)
        source = self._sources.create_source(
            space_id, name.strip(), type_.strip(), version_ordering, audience_mapping
        )
        self._metrics.increment("context_engine_sources_registered_total")
        return source

    def list_sources(self, principal: AuthenticatedPrincipal, space_id: str) -> tuple[Source, ...]:
        """List the sources of a visible space."""

        self.get_context_space(principal, space_id)
        return self._sources.list_sources(space_id)

    def get_source(self, principal: AuthenticatedPrincipal, source_id: str) -> Source:
        """Return a visible source."""

        return self._visible_source(principal, source_id)

    def update_source(
        self,
        principal: AuthenticatedPrincipal,
        source_id: str,
        *,
        name: str | None,
        state: SourceState | None,
        audience_mapping: dict[str, str] | None,
    ) -> Source:
        """Rename, pause or resume, or remap a source."""

        self._visible_source(principal, source_id)
        self._require_any(principal, _MANAGE_SOURCES, source_id)
        updated = self._sources.update_source(
            source_id, name=name, state=state, audience_mapping=audience_mapping
        )
        if updated is None:
            raise NotFoundError("Source not found")
        return updated

    # Checkpoints

    def get_checkpoint(self, principal: AuthenticatedPrincipal, source_id: str) -> SourceCheckpoint:
        """Return the stored connector cursor for a visible source."""

        self._visible_source(principal, source_id)
        checkpoint = self._sources.get_checkpoint(source_id)
        if checkpoint is None:
            raise NotFoundError("Checkpoint not found")
        return checkpoint

    def put_checkpoint(
        self, principal: AuthenticatedPrincipal, source_id: str, cursor: str
    ) -> SourceCheckpoint:
        """Store the connector cursor; requires delivery rights on the source."""

        self._visible_source(principal, source_id)
        self._require(principal, Action.INGEST_WRITE, source_id)
        return self._sources.put_checkpoint(source_id, cursor, principal.principal_id)

    # Sync runs and progress

    def open_sync_run(self, principal: AuthenticatedPrincipal, source_id: str) -> SyncRun:
        """Mark a source as being read by its connector."""

        source = self.authorize_upload(principal, source_id)
        run = self._sources.open_sync_run(source.id, principal.principal_id)
        self._metrics.increment("context_engine_sync_runs_opened_total")
        return run

    def complete_sync_run(
        self, principal: AuthenticatedPrincipal, source_id: str, run_id: str
    ) -> SyncRun:
        """Mark a sync run's reading as completed; repeating the call is harmless."""

        self._visible_source(principal, source_id)
        self._require(principal, Action.INGEST_WRITE, source_id)
        run = self._sources.get_sync_run(run_id)
        if run is None or run.source_id != source_id:
            raise NotFoundError("Sync run not found")
        if run.state is SyncRunState.SUPERSEDED:
            raise ConflictError("Sync run was superseded by a newer run")
        if run.state is SyncRunState.COMPLETED:
            return run
        completed = self._sources.complete_sync_run(run_id)
        if completed is None:
            raise NotFoundError("Sync run not found")
        return completed

    def _progress_for(self, source: Source) -> SourceProgress:
        run = self._sources.latest_sync_run(source.id)
        # Processing is measured over the latest run's window so a new scan starts from zero.
        since = run.started_at if run else None
        return SourceProgress(
            source_id=source.id,
            sync_run=run,
            processing_since=since,
            jobs=self._store.count_source_jobs(source.id, since),
            records=self._sources.record_counts(source.id),
            # The ledger is the source of truth for indexing (ADR 0010 granularity decision).
            indexing=self._sources.index_snapshot(source.id) if self._indexing_enabled else None,
        )

    def source_progress(self, principal: AuthenticatedPrincipal, source_id: str) -> SourceProgress:
        """Return a source's pipeline progress to principals who deliver or manage it.

        Counts reveal record volume across every audience, so readers are not enough.
        """

        source = self._visible_source(principal, source_id)
        self._require_any(principal, _INSPECT_DELIVERIES, source_id)
        return self._progress_for(source)

    def space_progress(
        self, principal: AuthenticatedPrincipal, space_id: str
    ) -> tuple[SourceProgress, ...]:
        """Return progress for the sources in a visible space the principal may inspect."""

        self.get_context_space(principal, space_id)
        result: list[SourceProgress] = []
        for source in self._sources.list_sources(space_id):
            actions = self._authorizer.effective_actions(
                principal.principal_id, principal.groups, source.id
            )
            if actions and actions & _INSPECT_DELIVERIES:
                result.append(self._progress_for(source))
        return tuple(result)

    # Staged uploads

    def authorize_upload(self, principal: AuthenticatedPrincipal, source_id: str) -> Source:
        """Check delivery rights on a ready source before any delivery work begins."""

        source = self._visible_source(principal, source_id)
        self._require(principal, Action.INGEST_WRITE, source_id)
        if source.state is not SourceState.READY:
            raise ConflictError("Source is not accepting deliveries")
        return source

    def stage_upload(
        self,
        principal: AuthenticatedPrincipal,
        source_id: str,
        content_type: str,
        data: bytes,
        idempotency_key: str,
    ) -> StagedUpload:
        """Validate and stage content bytes for a later ingestion event."""

        self.authorize_upload(principal, source_id)
        normalized_type = content_type.split(";", 1)[0].strip().lower()
        if not normalized_type or normalized_type not in self._upload_policy.content_types:
            raise UnsupportedContentTypeError()
        if not data:
            raise ValidationError("Upload body is empty")
        if len(data) > self._upload_policy.max_bytes:
            raise PayloadTooLargeError()
        content_hash = "sha256:" + hashlib.sha256(data).hexdigest()
        try:
            upload, created = self._sources.create_upload(
                source_id,
                principal.principal_id,
                idempotency_key,
                normalized_type,
                len(data),
                content_hash,
                self._upload_policy.ttl_seconds,
            )
        except IdempotencyConflict as exc:
            raise ConflictError("Idempotency key is already bound to different content") from exc
        if created or not self._staging.exists(upload.id):
            # Metadata first, bytes second: a retried upload with the same key rewrites the bytes.
            self._staging.write(upload.id, data)
            self._metrics.increment("context_engine_uploads_staged_total")
        return upload

    # Ingestion and jobs

    def accept_ingestion(
        self,
        principal: AuthenticatedPrincipal,
        command: IngestionCommand,
        header_idempotency_key: str,
    ) -> Job:
        """Validate, authorize, and durably accept an idempotent ingestion command.

        The connector's delivery right is checked on the source, so the body cannot select a
        space or source the credential is not bound to (threat T02). Staged content is verified
        against the event's type and hash before the job exists (threat T12).
        """

        if header_idempotency_key != command.idempotency_key:
            raise ValidationError("Idempotency key header and body must match")
        # A connector may hold rights on the source alone, so the source is the visibility
        # anchor; its registered space must match the body.
        source = self._sources.get_source(command.source_id)
        if (
            source is None
            or source.space_id != command.space_id
            or not self._visible(principal, source.id)
        ):
            raise NotFoundError("Source not found")
        self._require(principal, Action.INGEST_WRITE, source.id)
        if source.state is not SourceState.READY:
            raise ConflictError("Source is not accepting deliveries")
        try:
            source.version_key(command.source_version)
        except ValueError as exc:
            raise ValidationError("Source version does not match the source ordering") from exc
        if command.operation == "upsert":
            self._verify_staged_content(source, command)
        try:
            job, created = self._store.enqueue_job(
                command.job_operation,
                command.idempotency_key,
                command.to_payload(),
                principal.trace_id,
                self._max_job_attempts,
                principal.principal_id,
            )
        except IdempotencyConflict as exc:
            raise ConflictError("Idempotency key is already bound to another request") from exc
        metric = (
            "context_engine_jobs_accepted_total"
            if created
            else "context_engine_jobs_replayed_total"
        )
        self._metrics.increment(metric)
        return job

    def _verify_staged_content(self, source: Source, command: IngestionCommand) -> None:
        upload = self._sources.get_upload(command.content_ref or "")
        if (
            upload is None
            or upload.source_id != source.id
            or upload.is_expired(utc_now())
            or upload.content_hash != command.content_hash
            or upload.content_type != (command.content_type or "").split(";", 1)[0].strip().lower()
            or not self._staging.exists(upload.id)
        ):
            # One message for every mismatch so a caller cannot probe which check failed.
            raise ValidationError("Staged content does not match the ingestion event")

    def get_job(self, principal: AuthenticatedPrincipal, job_id: str) -> Job:
        """Return a job to its accepting principal or to a principal managing its resource."""

        job = self._store.get_job(job_id)
        if job is None:
            raise NotFoundError("Job not found")
        if job.principal_id == principal.principal_id:
            return job
        resource_id = job.resource_id
        actions = (
            self._authorizer.effective_actions(
                principal.principal_id, principal.groups, resource_id
            )
            if resource_id
            else None
        )
        if not actions or not actions & _INSPECT_DELIVERIES:
            raise NotFoundError("Job not found")
        return job

    def list_source_jobs(
        self, principal: AuthenticatedPrincipal, source_id: str, state: JobState | None
    ) -> tuple[Job, ...]:
        """List deliveries for a source, for example failed jobs awaiting inspection."""

        self._visible_source(principal, source_id)
        self._require_any(principal, _INSPECT_DELIVERIES, source_id)
        return self._store.list_source_jobs(source_id, state)

    def get_record_status(
        self, principal: AuthenticatedPrincipal, source_id: str, record_id: str
    ) -> RecordStatus:
        """Return a record's lifecycle state without content or backend details."""

        source = self._visible_source(principal, source_id)
        record = self._sources.get_record(source.space_id, source.id, record_id)
        if record is None:
            raise NotFoundError("Record not found")
        return record

    # Grants

    def list_grants(self, principal: AuthenticatedPrincipal, resource_id: str) -> tuple[Grant, ...]:
        """List grants on a resource for a principal holding access.manage on it."""

        if self._grants.resource_chain(resource_id) is None:
            raise NotFoundError()
        self._require(principal, Action.ACCESS_MANAGE, resource_id)
        return self._grants.list_grants(resource_id)

    def put_grant(
        self,
        principal: AuthenticatedPrincipal,
        resource_id: str,
        grant_id: str,
        actions: frozenset[Action],
        target_principal_id: str | None,
        target_group: str | None,
    ) -> Grant:
        """Create or replace a grant; the actor comes from the token, the target from the body."""

        if self._grants.resource_chain(resource_id) is None:
            raise NotFoundError()
        self._require(principal, Action.ACCESS_MANAGE, resource_id)
        if (target_principal_id is None) == (target_group is None):
            raise ValidationError("Grant must name exactly one of principalId or group")
        if not actions:
            raise ValidationError("Grant must contain at least one action")
        if (
            target_principal_id is not None
            and self._grants.get_principal(target_principal_id) is None
        ):
            raise ValidationError("Grant target is unknown")
        grant = self._grants.put_grant(
            resource_id,
            grant_id,
            actions,
            target_principal_id,
            target_group,
            principal.principal_id,
        )
        self._metrics.increment("context_engine_grants_changed_total")
        return grant

    def delete_grant(
        self, principal: AuthenticatedPrincipal, resource_id: str, grant_id: str
    ) -> None:
        """Delete a grant for a principal holding access.manage on the resource."""

        if self._grants.resource_chain(resource_id) is None:
            raise NotFoundError()
        self._require(principal, Action.ACCESS_MANAGE, resource_id)
        if not self._grants.delete_grant(resource_id, grant_id):
            raise NotFoundError("Grant not found")
        self._metrics.increment("context_engine_grants_changed_total")

"""Application service used by the REST API and future external interfaces."""

from __future__ import annotations

from context_engine.domain import ContextSpace, IngestionCommand, Job
from context_engine.observability import MetricsRegistry
from context_engine.persistence import IdempotencyConflict

from .errors import ConflictError, NotFoundError, ValidationError
from .ports import ControlPlaneStore


class ContextEngineService:
    """Coordinate public commands and queries over engine-owned ports."""

    def __init__(
        self,
        store: ControlPlaneStore,
        metrics: MetricsRegistry,
        max_job_attempts: int = 5,
    ) -> None:
        self._store = store
        self._metrics = metrics
        self._max_job_attempts = max_job_attempts

    def create_context_space(self, name: str, description: str | None) -> ContextSpace:
        """Validate and create a context space."""

        normalized = name.strip()
        if not normalized:
            raise ValidationError("Context-space name is required")
        space = self._store.create_space(normalized, description)
        self._metrics.increment("context_engine_spaces_created_total")
        return space

    def list_context_spaces(self) -> tuple[ContextSpace, ...]:
        """List context spaces available through the current store."""

        return self._store.list_spaces()

    def get_context_space(self, space_id: str) -> ContextSpace:
        """Return a context space or raise a stable not-found error."""

        space = self._store.get_space(space_id)
        if space is None:
            raise NotFoundError("Context space not found")
        return space

    def accept_ingestion(
        self,
        command: IngestionCommand,
        header_idempotency_key: str,
        trace_id: str,
    ) -> Job:
        """Validate and durably accept an idempotent ingestion command."""

        if header_idempotency_key != command.idempotency_key:
            raise ValidationError("Idempotency key header and body must match")
        if self._store.get_space(command.space_id) is None:
            raise NotFoundError("Context space not found")
        try:
            job, created = self._store.enqueue_job(
                command.job_operation,
                command.idempotency_key,
                command.to_payload(),
                trace_id,
                self._max_job_attempts,
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

    def get_job(self, job_id: str) -> Job:
        """Return public job state or raise a stable not-found error."""

        job = self._store.get_job(job_id)
        if job is None:
            raise NotFoundError("Job not found")
        return job

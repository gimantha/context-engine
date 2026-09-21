"""Application-facing persistence interfaces."""

from __future__ import annotations

from typing import Any, Protocol

from context_engine.domain import ContextSpace, Job, JobOperation


class ControlPlaneStore(Protocol):
    """Persistence operations required by the application service."""

    def create_space(self, name: str, description: str | None) -> ContextSpace:
        """Persist and return a new context space."""

        ...

    def list_spaces(self) -> tuple[ContextSpace, ...]:
        """Return context spaces visible to the current application scope."""

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
    ) -> tuple[Job, bool]:
        """Atomically persist a job and its outbox event."""

        ...

    def get_job(self, job_id: str) -> Job | None:
        """Return one durable job when it exists."""

        ...

"""Ledger lifecycle handler; provider execution is introduced in M4."""

from __future__ import annotations

from typing import Any, Protocol

from context_engine.domain import Job, Source
from context_engine.persistence import SourceRepository


class JobHandler(Protocol):
    """Execute one claimed job and return its durable result."""

    async def handle(self, job: Job) -> dict[str, Any]:
        """Apply the job's idempotent effect."""

        ...


class InjectedWorkerCrash(RuntimeError):
    """Test-only process interruption after a durable effect is committed."""


class TerminalJobError(RuntimeError):
    """Fail a job permanently; retrying cannot make it valid."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class LifecycleJobHandler:
    """Apply a delivered source event to the authoritative record ledger."""

    def __init__(
        self,
        sources: SourceRepository,
        *,
        crash_after_effect_once: bool = False,
    ) -> None:
        self._sources = sources
        self._crash_after_effect_once = crash_after_effect_once

    def _source(self, job: Job) -> Source:
        source = self._sources.get_source(job.source_id) if job.source_id else None
        if source is None or source.space_id != job.space_id:
            raise TerminalJobError("source_unknown", "Job source is not registered")
        return source

    async def handle(self, job: Job) -> dict[str, Any]:
        """Apply ordering, replay, deletion, and quarantine rules and return an engine result."""

        source = self._source(job)
        try:
            transition = self._sources.apply_record_event(job, source)
        except ValueError as exc:
            raise TerminalJobError(
                "invalid_version", "Source version violates the ordering policy"
            ) from exc
        if self._crash_after_effect_once:
            self._crash_after_effect_once = False
            raise InjectedWorkerCrash("injected crash after durable effect")
        return {
            "recordId": job.payload["sourceRecordId"],
            "sourceVersion": job.payload["sourceVersion"],
            "outcome": transition.outcome.value,
            "state": transition.state.value,
        }

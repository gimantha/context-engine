"""Apply delivered source events to the ledger, then converge the knowledge backend."""

from __future__ import annotations

from typing import Any, Protocol

from context_engine.domain import IndexState, Job, Source
from context_engine.persistence import SourceRepository, StagingStore


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


class RecordConverger(Protocol):
    """Bring one record's knowledge-backend copies in line with the ledger."""

    async def converge(self, source: Source, source_record_id: str, trace_id: str) -> IndexState:
        """Converge one record and return its index state."""

        ...


class LifecycleJobHandler:
    """Apply a delivered source event to the ledger and, when configured, the backend.

    Without an indexer the worker stays ledger-only. Staged bytes that no live record needs
    are released once the backend has caught up.
    """

    def __init__(
        self,
        sources: SourceRepository,
        *,
        indexer: RecordConverger | None = None,
        staging: StagingStore | None = None,
        crash_after_effect_once: bool = False,
    ) -> None:
        self._sources = sources
        self._indexer = indexer
        self._staging = staging
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
        record_id = job.payload["sourceRecordId"]
        # Converge on every attempt, replays included: a crash may have happened after the
        # ledger committed but before the backend caught up.
        index_state = (
            await self._indexer.converge(source, record_id, job.trace_id)
            if self._indexer is not None
            else None
        )
        if self._staging is not None:
            released = self._sources.release_candidates(source.space_id, source.id, record_id)
            for upload_id in released:
                self._staging.delete(upload_id)
            self._sources.mark_released(released)
        return {
            "recordId": record_id,
            "sourceVersion": job.payload["sourceVersion"],
            "outcome": transition.outcome.value,
            "state": transition.state.value,
            "indexState": index_state.value if index_state is not None else None,
        }

"""M1 durable ledger handler; provider execution is introduced in M4."""

from __future__ import annotations

from typing import Any, Protocol

from context_engine.domain import Job
from context_engine.persistence import ControlPlaneRepository


class JobHandler(Protocol):
    """Execute one claimed job and return its durable result."""

    async def handle(self, job: Job) -> dict[str, Any]:
        """Apply the job's idempotent effect."""

        ...


class InjectedWorkerCrash(RuntimeError):
    """Test-only process interruption after a durable effect is committed."""


class LedgerJobHandler:
    """Apply the M1 ledger-only source-record effect."""

    def __init__(
        self,
        repository: ControlPlaneRepository,
        *,
        crash_after_effect_once: bool = False,
    ) -> None:
        self._repository = repository
        self._crash_after_effect_once = crash_after_effect_once

    async def handle(self, job: Job) -> dict[str, Any]:
        """Record the effect and return an engine-owned result."""

        created = self._repository.record_source_effect(job)
        if self._crash_after_effect_once:
            self._crash_after_effect_once = False
            raise InjectedWorkerCrash("injected crash after durable effect")
        return {
            "recordId": job.payload["sourceRecordId"],
            "sourceVersion": job.payload["sourceVersion"],
            "effectCreated": created,
        }

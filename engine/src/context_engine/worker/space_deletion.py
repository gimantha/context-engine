"""Delete a context space: every record, every partition, then the space itself (ADR 0007).

Space deletion is a job because a space may hold thousands of records whose backend copies
must each be removed and checked. The handler is idempotent at every step, so a retried job
continues where the previous attempt stopped: sources already paused stay paused, records
already tombstoned are skipped, copies already gone pass their absence check, and a unit the
backend no longer has counts as removed.

A copy that a single deletion could not prove removed does not stop the job: ADR 0007's
fallback for that case is to drop the containing unit whole and retire its binding, which
space deletion does for every partition anyway. After the units are gone each such copy is
checked once more. Only a copy that is still searchable then fails the job, which leaves the
space in the `deleting` state rather than purging rows that still describe content.
"""

from __future__ import annotations

from typing import Any

from context_engine.domain import Job, RecordState, Source, SourceState, SpaceState
from context_engine.knowledge_backend import AccessPartitionRef, KnowledgeBackend, PrincipalContext
from context_engine.observability import MetricsRegistry, get_logger, log_event
from context_engine.persistence import ControlPlaneRepository, SourceRepository, StagingStore

from .handler import RecordConverger, TerminalJobError

logger = get_logger(__name__)

# Convergence outcomes that the whole-unit removal below resolves instead of failing the job.
_UNRESOLVED_COPY = frozenset({"residue_found", "reconcile_required"})


class SpaceDeletionJobHandler:
    """Remove a space's content from the backend and its rows from the control plane."""

    def __init__(
        self,
        sources: SourceRepository,
        control_plane: ControlPlaneRepository,
        staging: StagingStore,
        service_principal_id: str,
        metrics: MetricsRegistry,
        *,
        backend: KnowledgeBackend | None = None,
        indexer: RecordConverger | None = None,
    ) -> None:
        self._sources = sources
        self._control_plane = control_plane
        self._staging = staging
        self._service_principal_id = service_principal_id
        self._metrics = metrics
        # Without a backend the worker is ledger-only and deletion touches rows and bytes.
        self._backend = backend
        self._indexer = indexer

    async def handle(self, job: Job) -> dict[str, Any]:
        """Delete the space the job names and return what was removed.

        Order matters: sources are paused first so no delivery lands mid-deletion, records are
        tombstoned and their copies removed with the same checks as a single deletion, units
        are dropped only once empty, staged bytes go before their rows, and the space's rows go
        last.
        """

        space_id = job.space_id
        if space_id is None:
            raise TerminalJobError("invalid_job", "Space deletion job names no context space")
        space = self._control_plane.get_space(space_id)
        if space is None:
            return {"spaceId": space_id, "state": "deleted", "alreadyRemoved": True}
        if space.state is not SpaceState.DELETING:
            self._control_plane.set_space_state(space_id, SpaceState.DELETING)
        # Copies are removed as the engine's service identity, like every backend write.
        principal = PrincipalContext(self._service_principal_id, job.trace_id)

        sources = self._sources.list_sources(space_id)
        records = 0
        unresolved: list[tuple[Source, str]] = []
        for source in sources:
            if source.state is SourceState.READY:
                self._sources.update_source(source.id, state=SourceState.PAUSED)
            for record in self._sources.list_source_records(source.id):
                if record.state is not RecordState.DELETED:
                    self._sources.delete_record_as_engine(job, source, record.source_record_id)
                    records += 1
                if self._indexer is None:
                    continue
                try:
                    await self._indexer.converge(source, record.source_record_id, job.trace_id)
                except TerminalJobError as exc:
                    if exc.code not in _UNRESOLVED_COPY:
                        raise
                    unresolved.append((source, record.source_record_id))

        partitions = self._sources.list_partitions(space_id)
        if self._backend is not None:
            for partition in partitions:
                await self._backend.delete_partition(AccessPartitionRef(partition.id), principal)
        if self._indexer is not None:
            for source, record_id in unresolved:
                await self._indexer.purge(source, record_id, job.trace_id)

        for upload_id in self._sources.list_space_uploads(space_id):
            self._staging.delete(upload_id)
        self._control_plane.purge_space(space_id)
        self._metrics.increment("context_engine_spaces_deleted_total")
        log_event(logger, "space_deleted", resource_id=space_id, trace_id=job.trace_id)
        return {
            "spaceId": space_id,
            "state": "deleted",
            "sources": len(sources),
            "records": records,
            "partitions": len(partitions),
        }

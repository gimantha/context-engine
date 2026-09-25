"""Cross-check the knowledge backend against the ledger on a schedule (ADR 0010).

The ledger is the source of truth for indexing. This check asks the backend whether it still
holds every copy the engine believes is indexed, using the version whose content was actually
written. When the per-source counts disagree, it probes each copy to find the ones missing or
stuck in a failed provider run and marks them reconcile-required. The backend is never queried
on a request path; the stored snapshot records the last check.
"""

from __future__ import annotations

from datetime import UTC, datetime

from context_engine.domain import (
    IndexingSnapshot,
    IndexingState,
    IndexState,
    LocationState,
    RecordLocation,
    Source,
)
from context_engine.knowledge_backend import (
    AccessPartitionRef,
    BackendError,
    ExpectedRecord,
    IndexingProgressRequest,
    KnowledgeBackend,
    PrincipalContext,
)
from context_engine.observability import MetricsRegistry, get_logger, log_event
from context_engine.persistence import SourceRepository

logger = get_logger(__name__)


def _expected(location: RecordLocation) -> ExpectedRecord:
    return ExpectedRecord(
        AccessPartitionRef(location.partition_id),
        location.source_record_id,
        location.written_version or location.version or "",
    )


class IndexingCollector:
    """Compare what the backend holds with what the ledger says it holds."""

    def __init__(
        self,
        sources: SourceRepository,
        backend: KnowledgeBackend,
        service_principal_id: str,
        metrics: MetricsRegistry,
    ) -> None:
        self._sources = sources
        self._backend = backend
        self._service_principal_id = service_principal_id
        self._metrics = metrics

    async def collect_source(self, source: Source, trace_id: str) -> IndexingSnapshot:
        """Check one source, flag disagreeing copies, and store the snapshot."""

        principal = PrincipalContext(self._service_principal_id, trace_id)
        locations = self._sources.list_indexed_locations(source.id)
        now = datetime.now(UTC)
        if not locations:
            snapshot = IndexingSnapshot(source.id, IndexingState.OK, now, 0, 0, 0, 0, 0)
            self._sources.put_indexing_snapshot(snapshot)
            return snapshot
        expected = tuple(_expected(location) for location in locations)
        partitions = tuple({item.partition.value: item.partition for item in expected}.values())
        try:
            progress = await self._backend.indexing_progress(
                IndexingProgressRequest(source.id, expected), principal, partitions
            )
            if progress.missing or progress.failed:
                await self._flag_disagreements(source, locations, principal)
        except BackendError as exc:
            self._metrics.increment("context_engine_indexing_collection_failures_total")
            log_event(logger, "indexing_check_failed", error_code=exc.code.value, trace_id=trace_id)
            snapshot = IndexingSnapshot(
                source.id, IndexingState.UNAVAILABLE, now, error_code=exc.code.value
            )
            self._sources.put_indexing_snapshot(snapshot)
            return snapshot
        snapshot = IndexingSnapshot(
            source.id,
            IndexingState.OK,
            now,
            progress.expected,
            progress.indexed,
            progress.indexing,
            progress.failed,
            progress.missing,
        )
        self._sources.put_indexing_snapshot(snapshot)
        return snapshot

    async def _flag_disagreements(
        self,
        source: Source,
        locations: tuple[RecordLocation, ...],
        principal: PrincipalContext,
    ) -> None:
        for location in locations:
            item = _expected(location)
            single = await self._backend.indexing_progress(
                IndexingProgressRequest(source.id, (item,)), principal, (item.partition,)
            )
            if not (single.missing or single.failed):
                continue
            self._sources.set_location_state(
                location.space_id,
                location.source_id,
                location.source_record_id,
                location.partition_id,
                LocationState.RECONCILE_REQUIRED,
            )
            self._sources.set_index_state(
                location.space_id,
                location.source_id,
                location.source_record_id,
                IndexState.RECONCILE_REQUIRED,
                "backend_mismatch",
            )
            self._metrics.increment("context_engine_index_mismatches_total")
            log_event(
                logger,
                "index_mismatch_found",
                error_code="backend_mismatch",
                trace_id=principal.trace_id,
            )

    async def collect(self, trace_id: str) -> int:
        """Check every registered source and return how many were checked."""

        sources = self._sources.list_all_sources()
        for source in sources:
            await self.collect_source(source, trace_id)
        self._metrics.increment("context_engine_indexing_collections_total")
        return len(sources)

"""Background collection of per-source indexing progress from the knowledge backend.

Progress reads are polled often, so the backend is never queried on the request path. This
collector asks the backend on its own schedule and stores an engine-owned snapshot that the
progress API reads. The worker enables it once provider-backed ingestion is configured.
"""

from __future__ import annotations

from datetime import UTC, datetime

from context_engine.domain import IndexingSnapshot, IndexingState, Source
from context_engine.knowledge_backend import (
    BackendError,
    ExpectedRecord,
    IndexingProgressRequest,
    KnowledgeBackend,
    PrincipalContext,
)
from context_engine.observability import MetricsRegistry, get_logger, log_event
from context_engine.persistence import SourceRepository
from context_engine.security.partitions import partition_for

logger = get_logger(__name__)


class IndexingCollector:
    """Refresh the indexing snapshot of every source from the knowledge backend."""

    def __init__(
        self,
        sources: SourceRepository,
        backend: KnowledgeBackend,
        principal: PrincipalContext,
        metrics: MetricsRegistry,
    ) -> None:
        self._sources = sources
        self._backend = backend
        self._principal = principal
        self._metrics = metrics

    async def collect_source(self, source: Source) -> IndexingSnapshot:
        """Collect and store one source's snapshot; backend failures are stored, not raised."""

        records = self._sources.list_active_records(source.id)
        now = datetime.now(UTC)
        expected = tuple(
            ExpectedRecord(
                partition_for(source.space_id, record.audience),
                record.source_record_id,
                record.current_version,
            )
            for record in records
        )
        if not expected:
            # Nothing should be indexed, so there is nothing to ask the backend.
            snapshot = IndexingSnapshot(source.id, IndexingState.OK, now, 0, 0, 0, 0, 0)
        else:
            partitions = tuple({item.partition.value: item.partition for item in expected}.values())
            try:
                progress = await self._backend.indexing_progress(
                    IndexingProgressRequest(source.id, expected), self._principal, partitions
                )
            except BackendError as exc:
                self._metrics.increment("context_engine_indexing_collection_failures_total")
                log_event(
                    logger,
                    "indexing_collection_failed",
                    error_code=exc.code.value,
                    trace_id=self._principal.trace_id,
                )
                snapshot = IndexingSnapshot(
                    source.id, IndexingState.UNAVAILABLE, now, error_code=exc.code.value
                )
            else:
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

    async def collect(self) -> int:
        """Collect every registered source and return how many were collected."""

        sources = self._sources.list_all_sources()
        for source in sources:
            await self.collect_source(source)
        self._metrics.increment("context_engine_indexing_collections_total")
        return len(sources)

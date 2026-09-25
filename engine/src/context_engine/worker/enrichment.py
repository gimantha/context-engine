"""Run explicit, versioned enrichment of a context space, one partition at a time (ADR 0004)."""

from __future__ import annotations

from typing import Any

from context_engine.domain import Job
from context_engine.knowledge_backend import (
    AccessPartitionRef,
    BackendError,
    EnrichmentRequest,
    KnowledgeBackend,
    PrincipalContext,
)
from context_engine.observability import MetricsRegistry
from context_engine.persistence import SourceRepository

from .handler import TerminalJobError


class EnrichmentJobHandler:
    """Enrich each partition of a space separately, as the engine's service identity."""

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

    async def handle(self, job: Job) -> dict[str, Any]:
        """Enrich every partition that holds content and return what was produced.

        Partitions are enriched one call at a time so derived data can never span two
        reader sets, whatever the backend supports.
        """

        space_id = job.space_id
        if space_id is None:
            raise TerminalJobError("invalid_job", "Enrichment job names no context space")
        version = str(job.payload.get("pipelineVersion") or "enrich@1")
        indexed = self._sources.indexed_partitions(space_id)
        partitions = [
            AccessPartitionRef(item.id)
            for item in self._sources.list_partitions(space_id)
            if item.id in indexed
        ]
        principal = PrincipalContext(self._service_principal_id, job.trace_id)
        affected = 0
        artifacts = 0
        for partition in partitions:
            try:
                result = await self._backend.enrich(
                    EnrichmentRequest(job.id, version), principal, (partition,)
                )
            except BackendError as exc:
                if exc.retryable:
                    raise
                raise TerminalJobError(
                    f"backend_{exc.code.value}", "Knowledge backend rejected the enrichment"
                ) from exc
            affected += result.affected_records
            artifacts += len(result.created_artifacts)
        self._metrics.increment("context_engine_enrichments_completed_total")
        return {
            "operation": "enrichment",
            "pipelineVersion": version,
            "partitions": len(partitions),
            "affectedRecords": affected,
            "createdArtifacts": artifacts,
        }

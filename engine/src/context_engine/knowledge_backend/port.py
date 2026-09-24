"""The only backend interface available to application services."""

from __future__ import annotations

from typing import Protocol

from .types import (
    AccessPartitionRef,
    BackendHealth,
    BackendReference,
    DeletionResult,
    EnrichmentRequest,
    EnrichmentResult,
    IndexingProgress,
    IndexingProgressRequest,
    IngestionResult,
    PrincipalContext,
    QueryRequest,
    QueryResult,
    SourceRecord,
)


class KnowledgeBackend(Protocol):
    """Internal provider-neutral interface used by worker pipelines."""

    async def ingest(
        self,
        record: SourceRecord,
        principal: PrincipalContext,
        partition: AccessPartitionRef,
    ) -> IngestionResult:
        """Store a new source record in one explicit access partition."""

        ...

    async def query(
        self,
        request: QueryRequest,
        principal: PrincipalContext,
        authorized_partitions: tuple[AccessPartitionRef, ...],
    ) -> QueryResult:
        """Retrieve evidence from explicitly authorized partitions."""

        ...

    async def update(
        self,
        record: SourceRecord,
        principal: PrincipalContext,
        partition: AccessPartitionRef,
    ) -> IngestionResult:
        """Replace an existing source record in its access partition.

        On success the new version is searchable and the previous one is not. The returned
        reference may differ from the previous one and replaces it.
        """

        ...

    async def enrich(
        self,
        request: EnrichmentRequest,
        principal: PrincipalContext,
        authorized_partitions: tuple[AccessPartitionRef, ...],
    ) -> EnrichmentResult:
        """Run explicit enrichment within authorized partitions."""

        ...

    async def delete(
        self,
        reference: BackendReference,
        principal: PrincipalContext,
        partition: AccessPartitionRef,
    ) -> DeletionResult:
        """Delete one referenced record from its access partition."""

        ...

    async def grant_read(
        self,
        partition: AccessPartitionRef,
        reader: PrincipalContext,
        principal: PrincipalContext,
    ) -> None:
        """Let a reader query one partition; the acting principal must own it.

        Raises a not-found error while the partition holds nothing yet.
        """

        ...

    async def revoke_read(
        self,
        partition: AccessPartitionRef,
        reader: PrincipalContext,
        principal: PrincipalContext,
    ) -> None:
        """Remove a reader's access to one partition; the acting principal must own it."""

        ...

    async def indexing_progress(
        self,
        request: IndexingProgressRequest,
        principal: PrincipalContext,
        authorized_partitions: tuple[AccessPartitionRef, ...],
    ) -> IndexingProgress:
        """Report how many expected record versions are indexed, in progress, failed, or absent.

        Every expected record must sit in one of the explicitly authorized partitions.
        """

        ...

    async def health(self) -> BackendHealth:
        """Report backend readiness and supported capabilities."""

        ...

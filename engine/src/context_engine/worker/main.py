"""Worker process entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import time
from uuid import uuid4

from context_engine.config import KnowledgeBackendSettings, Settings
from context_engine.domain import JobOperation
from context_engine.knowledge_backend.factory import build_knowledge_backend
from context_engine.observability import MetricsRegistry, configure_logging
from context_engine.persistence import (
    AuthorizationRepository,
    ControlDatabase,
    ControlPlaneRepository,
    ReadAccessRepository,
    SourceRepository,
    SqliteBackendState,
    StagingStore,
)
from context_engine.security.authorization import Authorizer
from context_engine.security.identity import build_token_verifier

from .enrichment import EnrichmentJobHandler
from .handler import JobHandler, LifecycleJobHandler, OperationDispatcher
from .indexer import RecordIndexer
from .indexing import IndexingCollector
from .read_access import ReadAccessSynchronizer
from .reauthorize import JobAuthorizer
from .runtime import JobWorker

_BACKEND_MODES = ("none", "provider")


async def _run(args: argparse.Namespace, settings: Settings) -> None:
    if settings.knowledge_backend not in _BACKEND_MODES:
        raise RuntimeError(
            f"Knowledge backend mode {settings.knowledge_backend!r} is not supported; "
            f"supported modes: {', '.join(_BACKEND_MODES)}"
        )
    database = ControlDatabase(settings.database_path, settings.migrations_path)
    database.migrate()
    repository = ControlPlaneRepository(database)
    authorization = AuthorizationRepository(database)
    sources = SourceRepository(database)
    staging = StagingStore(settings.staging_path)
    metrics = MetricsRegistry()
    # The worker loads the same identity registry as the API so it can resolve current groups.
    verifier = build_token_verifier(settings)
    read_access: ReadAccessSynchronizer | None = None
    indexer: RecordIndexer | None = None
    collector: IndexingCollector | None = None
    enrichment: EnrichmentJobHandler | None = None
    if settings.knowledge_backend == "provider":
        backend_settings = KnowledgeBackendSettings.from_env()
        backend = build_knowledge_backend(backend_settings, SqliteBackendState(database))
        read_access = ReadAccessSynchronizer(
            authorization,
            sources,
            ReadAccessRepository(database),
            backend,
            verifier,
            backend_settings.service_principal_id,
            metrics,
        )
        indexer = RecordIndexer(
            sources,
            backend,
            staging,
            backend_settings.service_principal_id,
            metrics,
            on_partition_bound=read_access.sync_partition,
        )
        collector = IndexingCollector(
            sources, backend, backend_settings.service_principal_id, metrics
        )
        enrichment = EnrichmentJobHandler(
            sources, backend, backend_settings.service_principal_id, metrics
        )
    lifecycle = LifecycleJobHandler(sources, indexer=indexer, staging=staging)
    handlers: dict[JobOperation, JobHandler] = {
        JobOperation.INGESTION: lifecycle,
        JobOperation.UPDATE: lifecycle,
        JobOperation.DELETION: lifecycle,
    }
    if enrichment is not None:
        handlers[JobOperation.ENRICHMENT] = enrichment
    worker = JobWorker(
        repository,
        OperationDispatcher(handlers),
        metrics,
        JobAuthorizer(Authorizer(authorization, metrics), authorization, verifier),
        lease_seconds=settings.worker_lease_seconds,
    )
    if args.check:
        print("Context Engine worker startup check passed.")
        return
    if read_access is not None:
        # Group membership comes from the identity registry, which may change across restarts.
        await read_access.sync_if_changed(f"trace_{uuid4().hex}", force=True)
    if args.once:
        await worker.run_once()
        return
    last_check = float("-inf")
    while True:
        if read_access is not None:
            await read_access.sync_if_changed(f"trace_{uuid4().hex}")
        if (
            collector is not None
            and time.monotonic() - last_check >= settings.indexing_check_seconds
        ):
            # The ledger is authoritative; this only flags copies the backend lost.
            await collector.collect(f"trace_{uuid4().hex}")
            last_check = time.monotonic()
        processed = await worker.run_once()
        if not processed:
            await asyncio.sleep(settings.worker_poll_seconds)


def main() -> None:
    """Run the durable worker loop or perform a startup-only validation."""

    parser = argparse.ArgumentParser(description="Run the Context Engine worker")
    parser.add_argument("--once", action="store_true", help="Process at most one job")
    parser.add_argument("--check", action="store_true", help="Migrate and validate startup")
    args = parser.parse_args()
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    asyncio.run(_run(args, settings))


if __name__ == "__main__":
    main()

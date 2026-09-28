"""Worker composition and loop, shared by the worker process and the single-process server."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from context_engine.config import KnowledgeBackendSettings, Settings
from context_engine.domain import JobOperation
from context_engine.knowledge_backend import KnowledgeBackend
from context_engine.knowledge_backend.factory import build_knowledge_backend
from context_engine.observability import MetricsRegistry
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

BACKEND_MODES = ("none", "provider")


@dataclass(frozen=True, slots=True)
class WorkerRuntime:
    """Everything the worker loop runs, plus the backend it writes through."""

    worker: JobWorker
    read_access: ReadAccessSynchronizer | None
    collector: IndexingCollector | None
    backend: KnowledgeBackend | None


def build_worker_runtime(
    settings: Settings,
    database: ControlDatabase,
    metrics: MetricsRegistry | None = None,
) -> WorkerRuntime:
    """Wire the worker's handlers for the configured knowledge backend mode."""

    if settings.knowledge_backend not in BACKEND_MODES:
        raise RuntimeError(
            f"Knowledge backend mode {settings.knowledge_backend!r} is not supported; "
            f"supported modes: {', '.join(BACKEND_MODES)}"
        )
    repository = ControlPlaneRepository(database)
    authorization = AuthorizationRepository(database)
    sources = SourceRepository(database)
    staging = StagingStore(settings.staging_path)
    metrics = metrics or MetricsRegistry()
    # The worker loads the same identity registry as the API so it can resolve current groups.
    verifier = build_token_verifier(settings)
    backend: KnowledgeBackend | None = None
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
    return WorkerRuntime(worker, read_access, collector, backend)


async def synchronize_read_access(runtime: WorkerRuntime) -> None:
    """Reconcile backend read access at startup; group membership may have changed since."""

    if runtime.read_access is not None:
        await runtime.read_access.sync_if_changed(f"trace_{uuid4().hex}", force=True)


async def run_worker_loop(
    runtime: WorkerRuntime,
    settings: Settings,
    stop: asyncio.Event | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> None:
    """Process jobs until `stop` is set, or forever when there is none.

    `heartbeat` is called after every completed pass, so a supervisor can tell a working
    loop from one that keeps failing.
    """

    await synchronize_read_access(runtime)
    last_check = float("-inf")
    while stop is None or not stop.is_set():
        if runtime.read_access is not None:
            await runtime.read_access.sync_if_changed(f"trace_{uuid4().hex}")
        if (
            runtime.collector is not None
            and time.monotonic() - last_check >= settings.indexing_check_seconds
        ):
            # The ledger is authoritative; this only flags copies the backend lost.
            await runtime.collector.collect(f"trace_{uuid4().hex}")
            last_check = time.monotonic()
        processed = await runtime.worker.run_once()
        if heartbeat is not None:
            heartbeat()
        if not processed:
            await wait_or_stop(settings.worker_poll_seconds, stop)


async def wait_or_stop(seconds: float, stop: asyncio.Event | None) -> None:
    """Sleep for `seconds`, returning early when `stop` is set."""

    if stop is None:
        await asyncio.sleep(seconds)
        return
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass

"""Single-process entrypoint: the REST API and the worker run in one process.

The local knowledge-backend stores take a per-process lock, so in provider mode the API's
queries and the worker's writes must share a process (ADR 0002 revision). The worker loop
runs as a background task on the server's event loop, and both use one backend.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import uvicorn
from fastapi import FastAPI

from context_engine.api import create_app
from context_engine.api.main import refuse_static_auth_off_loopback
from context_engine.config import Settings
from context_engine.observability import MetricsRegistry, configure_logging, get_logger, log_event
from context_engine.persistence import ControlDatabase
from context_engine.worker.process import (
    WorkerRuntime,
    build_worker_runtime,
    run_worker_loop,
    wait_or_stop,
)

logger = get_logger(__name__)


class BackgroundWorker:
    """Run the worker loop as a task in the serving process and report its health.

    A failing pass is logged, the loop restarts after a short delay, and readiness reports
    unavailable until a pass completes again. On shutdown the current job gets up to the
    lease period to finish. After that it is cancelled, and its expired lease lets the next
    start pick the job up again.
    """

    def __init__(self, runtime: WorkerRuntime, settings: Settings) -> None:
        self._runtime = runtime
        self._settings = settings
        # A floor keeps a persistent failure from spinning when polling is set very low.
        self._restart_delay = max(settings.worker_poll_seconds, 0.1)
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._failing = False

    @property
    def runtime(self) -> WorkerRuntime:
        """Return the worker runtime this loop drives."""

        return self._runtime

    def healthy(self) -> bool:
        """Report whether the loop is running and its latest pass completed."""

        return self._task is not None and not self._task.done() and not self._failing

    def start(self) -> None:
        """Start the loop on the running event loop."""

        self._stop = asyncio.Event()
        self._failing = False
        self._task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        """Ask the loop to finish its current pass, cancelling it after the lease period."""

        if self._task is None or self._stop is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(
                asyncio.shield(self._task), timeout=self._settings.worker_lease_seconds
            )
        except TimeoutError:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    def _completed_pass(self) -> None:
        self._failing = False

    async def _supervise(self) -> None:
        stop = self._stop
        assert stop is not None
        while not stop.is_set():
            try:
                await run_worker_loop(
                    self._runtime, self._settings, stop, heartbeat=self._completed_pass
                )
            except Exception as exc:
                # Job failures are handled inside the loop; this is the loop itself failing,
                # for example on an unavailable database. The API keeps serving meanwhile.
                self._failing = True
                log_event(logger, "worker_loop_failed", error_code=type(exc).__name__)
                await wait_or_stop(self._restart_delay, stop)


def create_serve_app(settings: Settings | None = None) -> FastAPI:
    """Build the REST application with the worker loop attached to its lifespan."""

    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    database = ControlDatabase(settings.database_path, settings.migrations_path)
    database.migrate()
    metrics = MetricsRegistry()
    runtime = build_worker_runtime(settings, database, metrics)
    background = BackgroundWorker(runtime, settings)
    # One backend serves the API's queries and the worker's writes.
    app = create_app(
        settings,
        database=database,
        metrics=metrics,
        knowledge_backend=runtime.backend,
        readiness_checks=(background.healthy,),
    )
    serving = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        async with serving(application):
            background.start()
            try:
                yield
            finally:
                await background.stop()

    app.router.lifespan_context = lifespan
    app.state.background_worker = background
    return app


def main() -> None:
    """Serve the REST API with the worker in the same process, or check startup."""

    parser = argparse.ArgumentParser(
        description="Run the Context Engine REST API and worker in one process"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument("--check", action="store_true", help="Migrate and validate startup")
    args = parser.parse_args()
    settings = Settings.from_env()
    if not args.check:
        refuse_static_auth_off_loopback(settings, args.host)
    app = create_serve_app(settings)
    if args.check:
        print("Context Engine API and worker startup check passed.")
        return
    uvicorn.run(app, host=args.host, port=args.port, log_config=None)


if __name__ == "__main__":
    main()

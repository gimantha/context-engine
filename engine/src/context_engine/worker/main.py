"""Worker process entrypoint."""

from __future__ import annotations

import argparse
import asyncio

from context_engine.config import Settings
from context_engine.observability import MetricsRegistry, configure_logging
from context_engine.persistence import ControlDatabase, ControlPlaneRepository

from .handler import LedgerJobHandler
from .runtime import JobWorker


async def _run(args: argparse.Namespace, settings: Settings) -> None:
    database = ControlDatabase(settings.database_path, settings.migrations_path)
    database.migrate()
    repository = ControlPlaneRepository(database)
    metrics = MetricsRegistry()
    worker = JobWorker(
        repository,
        LedgerJobHandler(repository),
        metrics,
        lease_seconds=settings.worker_lease_seconds,
    )
    if args.check:
        print("Context Engine worker startup check passed.")
        return
    if args.once:
        await worker.run_once()
        return
    while True:
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

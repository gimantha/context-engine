"""Worker process entrypoint."""

from __future__ import annotations

import argparse
import asyncio

from context_engine.config import Settings
from context_engine.observability import MetricsRegistry, configure_logging
from context_engine.persistence import (
    AuthorizationRepository,
    ControlDatabase,
    ControlPlaneRepository,
    SourceRepository,
)
from context_engine.security.authorization import Authorizer
from context_engine.security.identity import build_token_verifier

from .handler import LifecycleJobHandler
from .reauthorize import JobAuthorizer
from .runtime import JobWorker


async def _run(args: argparse.Namespace, settings: Settings) -> None:
    database = ControlDatabase(settings.database_path, settings.migrations_path)
    database.migrate()
    repository = ControlPlaneRepository(database)
    authorization = AuthorizationRepository(database)
    metrics = MetricsRegistry()
    # The worker loads the same identity registry as the API so it can resolve current groups.
    verifier = build_token_verifier(settings)
    worker = JobWorker(
        repository,
        LifecycleJobHandler(SourceRepository(database)),
        metrics,
        JobAuthorizer(Authorizer(authorization, metrics), authorization, verifier),
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

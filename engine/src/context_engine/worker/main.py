"""Worker process entrypoint."""

from __future__ import annotations

import argparse
import asyncio

from context_engine.config import Settings
from context_engine.observability import configure_logging
from context_engine.persistence import ControlDatabase

from .process import build_worker_runtime, run_worker_loop, synchronize_read_access


async def _run(args: argparse.Namespace, settings: Settings) -> None:
    database = ControlDatabase(settings.database_path, settings.migrations_path)
    database.migrate()
    runtime = build_worker_runtime(settings, database)
    if args.check:
        print("Context Engine worker startup check passed.")
        return
    if args.once:
        await synchronize_read_access(runtime)
        await runtime.worker.run_once()
        return
    await run_worker_loop(runtime, settings)


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

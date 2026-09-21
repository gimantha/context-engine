"""Control-database migration entrypoint."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from context_engine.config import Settings

from .database import ControlDatabase


def main() -> None:
    """Apply migrations to the configured or command-line database path."""

    parser = argparse.ArgumentParser(description="Apply Context Engine database migrations")
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.database:
        settings = replace(settings, database_path=args.database)
    applied = ControlDatabase(settings.database_path, settings.migrations_path).migrate()
    print(f"Applied {len(applied)} migration(s).")


if __name__ == "__main__":
    main()

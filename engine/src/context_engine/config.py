"""Environment-backed process settings with safe local defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    """Configure API, worker, database, and migration behavior."""

    database_path: Path
    migrations_path: Path
    log_level: str = "INFO"
    worker_poll_seconds: float = 1.0
    worker_lease_seconds: int = 30
    worker_max_attempts: int = 5

    @classmethod
    def from_env(cls) -> Settings:
        """Load settings from environment variables and safe local defaults."""

        default_migrations = Path(__file__).resolve().parents[2] / "migrations"
        return cls(
            database_path=Path(
                os.getenv("CONTEXT_ENGINE_DB_PATH", ".context-engine/control.db")
            ).expanduser(),
            migrations_path=Path(
                os.getenv("CONTEXT_ENGINE_MIGRATIONS_PATH", str(default_migrations))
            ).expanduser(),
            log_level=os.getenv("CONTEXT_ENGINE_LOG_LEVEL", "INFO").upper(),
            worker_poll_seconds=float(os.getenv("CONTEXT_ENGINE_WORKER_POLL_SECONDS", "1")),
            worker_lease_seconds=int(os.getenv("CONTEXT_ENGINE_WORKER_LEASE_SECONDS", "30")),
            worker_max_attempts=int(os.getenv("CONTEXT_ENGINE_WORKER_MAX_ATTEMPTS", "5")),
        )

"""Environment-backed process settings with safe local defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean environment value or return the supplied default."""

    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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


@dataclass(frozen=True, slots=True)
class KnowledgeBackendSettings:
    """Configure the private knowledge backend with engine-owned settings."""

    telemetry_enabled: bool = False
    file_logging_enabled: bool = False
    query_cache_enabled: bool = False
    access_control_required: bool = True
    local_content_access_enabled: bool = False
    remote_content_access_enabled: bool = False
    raw_graph_query_enabled: bool = False
    relational_store: str = "sqlite"
    graph_store: str = "kuzu"
    vector_store: str = "lancedb"
    model_provider: str = "openai"
    model_name: str = "openai/gpt-5-mini"
    model_api_key: str = ""
    embedding_provider: str = "openai"
    embedding_model: str = "openai/text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_api_key: str = ""
    storage_path: Path = Path(".context-engine/knowledge")
    live_test_enabled: bool = False
    live_test_password: str = ""

    @classmethod
    def from_env(cls) -> KnowledgeBackendSettings:
        """Load backend settings exclusively from engine-owned environment names."""

        return cls(
            telemetry_enabled=_env_bool("CONTEXT_ENGINE_TELEMETRY_ENABLED", False),
            file_logging_enabled=_env_bool("CONTEXT_ENGINE_FILE_LOGGING_ENABLED", False),
            query_cache_enabled=_env_bool("CONTEXT_ENGINE_QUERY_CACHE_ENABLED", False),
            access_control_required=_env_bool("CONTEXT_ENGINE_ACCESS_CONTROL_REQUIRED", True),
            local_content_access_enabled=_env_bool(
                "CONTEXT_ENGINE_LOCAL_CONTENT_ACCESS_ENABLED", False
            ),
            remote_content_access_enabled=_env_bool(
                "CONTEXT_ENGINE_REMOTE_CONTENT_ACCESS_ENABLED", False
            ),
            raw_graph_query_enabled=_env_bool("CONTEXT_ENGINE_RAW_GRAPH_QUERY_ENABLED", False),
            relational_store=os.getenv("CONTEXT_ENGINE_RELATIONAL_STORE", "sqlite"),
            graph_store=os.getenv("CONTEXT_ENGINE_GRAPH_STORE", "kuzu"),
            vector_store=os.getenv("CONTEXT_ENGINE_VECTOR_STORE", "lancedb"),
            model_provider=os.getenv("CONTEXT_ENGINE_MODEL_PROVIDER", "openai"),
            model_name=os.getenv("CONTEXT_ENGINE_MODEL_NAME", "openai/gpt-5-mini"),
            model_api_key=os.getenv("CONTEXT_ENGINE_MODEL_API_KEY", ""),
            embedding_provider=os.getenv("CONTEXT_ENGINE_EMBEDDING_PROVIDER", "openai"),
            embedding_model=os.getenv(
                "CONTEXT_ENGINE_EMBEDDING_MODEL", "openai/text-embedding-3-small"
            ),
            embedding_dimensions=int(os.getenv("CONTEXT_ENGINE_EMBEDDING_DIMENSIONS", "1536")),
            embedding_api_key=os.getenv("CONTEXT_ENGINE_EMBEDDING_API_KEY", ""),
            storage_path=Path(
                os.getenv("CONTEXT_ENGINE_KNOWLEDGE_STORAGE_PATH", ".context-engine/knowledge")
            ).expanduser(),
            live_test_enabled=_env_bool("CONTEXT_ENGINE_RUN_LIVE_PROVIDER", False),
            live_test_password=os.getenv("CONTEXT_ENGINE_LIVE_TEST_PASSWORD", ""),
        )

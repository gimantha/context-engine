"""Build the knowledge backend for processes that serve reads.

The application layer owns this wiring so the REST package never imports the backend port or
a provider. The backend reads and writes its durable state in the control database.
"""

from __future__ import annotations

from context_engine.config import KnowledgeBackendSettings, Settings
from context_engine.knowledge_backend import KnowledgeBackend
from context_engine.knowledge_backend.factory import build_knowledge_backend
from context_engine.persistence import ControlDatabase, SqliteBackendState

KNOWLEDGE_BACKEND_MODES = ("none", "provider")


def build_query_backend(settings: Settings, database: ControlDatabase) -> KnowledgeBackend | None:
    """Return the configured backend, or None while the engine runs ledger-only."""

    if settings.knowledge_backend not in KNOWLEDGE_BACKEND_MODES:
        raise RuntimeError(
            f"Knowledge backend mode {settings.knowledge_backend!r} is not supported; "
            f"supported modes: {', '.join(KNOWLEDGE_BACKEND_MODES)}"
        )
    if settings.knowledge_backend != "provider":
        return None
    return build_knowledge_backend(
        KnowledgeBackendSettings.from_env(), SqliteBackendState(database)
    )

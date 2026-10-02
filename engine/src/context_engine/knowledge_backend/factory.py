"""Build the configured knowledge backend without exposing the provider to callers."""

from __future__ import annotations

from context_engine.config import KnowledgeBackendSettings

from .port import AnswerWriter, KnowledgeBackend
from .state import BackendStateStore


def build_knowledge_backend(
    settings: KnowledgeBackendSettings, state: BackendStateStore
) -> KnowledgeBackend:
    """Return the private provider backend over the engine's durable backend state.

    The provider SDK is imported lazily, so building the backend does not require it.
    """

    from .providers.cognee import CogneeBackend, CogneeIdentityResolver, CogneeRuntime

    runtime = CogneeRuntime(settings)
    return CogneeBackend(runtime, CogneeIdentityResolver(runtime, state), state)


def build_answer_writer(settings: KnowledgeBackendSettings) -> AnswerWriter:
    """Return the private provider's answer writer for the engine's provider settings.

    It gets a runtime of its own over the same settings and storage root; the provider binds
    one root per process, so the two never disagree, and answers touch none of its stores.
    """

    from .providers.cognee import CogneeAnswerWriter, CogneeRuntime

    return CogneeAnswerWriter(CogneeRuntime(settings))

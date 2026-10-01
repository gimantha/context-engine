"""Turn a space's stored model configuration into the selection a backend call carries.

A space's models are stored with their keys as tokens (M5 slice 1). Every call that runs a
model reveals the tokens just before the call, so a key exists in the clear only in memory
and only for that call. A space without a configuration resolves to `None`, which tells the
backend to use the engine's environment settings; that is the agreed fallback, so spaces
indexed before configuration existed keep working.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from context_engine.domain import ConfiguredModel, SpaceConfiguration
from context_engine.knowledge_backend import ModelSelection, ModelSettings
from context_engine.security.secrets import SecretStore


@dataclass(frozen=True, slots=True)
class ModelInput:
    """A model as an administrator submits it: with a literal key or a reference to one.

    Exactly one of `api_key` and `api_key_ref` is set. A literal key is encrypted before it is
    stored; that is how standalone deployments hold keys, and how Devant deployments hold them
    until the control plane serves secrets.
    """

    provider: str
    model: str
    api_key: str | None = None
    api_key_ref: str | None = None
    endpoint: str | None = None
    api_version: str | None = None
    dimensions: int | None = None


@dataclass(frozen=True, slots=True)
class SpaceConfigurationView:
    """What a reader of the configuration may see: models, key kinds, and the lock.

    Key kinds say whether each key is encrypted or a reference, without revealing either.
    `embedding_locked` tells the caller the embedding model can no longer change.
    """

    configuration: SpaceConfiguration | None
    embedding_locked: bool
    embedding_key_kind: str | None
    language_key_kind: str | None


class SpaceConfigurationReader(Protocol):
    """The one persistence call the resolver needs."""

    def get_space_configuration(self, space_id: str) -> SpaceConfiguration | None:
        """Return the space's model configuration when one has been set."""

        ...


class SpaceModelResolver:
    """Resolve a space's configured models, keys included, for one backend call."""

    def __init__(self, configurations: SpaceConfigurationReader, secrets: SecretStore) -> None:
        self._configurations = configurations
        self._secrets = secrets

    async def resolve(self, space_id: str) -> ModelSelection | None:
        """Return the space's models with their keys revealed, or `None` for the default.

        Raises `SecretError` when a key cannot be revealed, for example when the control
        plane is unreachable; callers decide whether that fails the request or retries a job.
        """

        configuration = self._configurations.get_space_configuration(space_id)
        if configuration is None:
            return None
        return ModelSelection(
            language_model=await self._settings(configuration.language_model),
            embedding_model=await self._settings(configuration.embedding_model),
        )

    async def _settings(self, configured: ConfiguredModel | None) -> ModelSettings | None:
        if configured is None:
            return None
        return ModelSettings(
            provider=configured.provider,
            model=configured.model,
            api_key=await self._secrets.reveal(configured.secret),
            endpoint=configured.endpoint,
            api_version=configured.api_version,
            dimensions=configured.dimensions,
        )

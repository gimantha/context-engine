"""Secrets the engine holds by reference or under encryption, never in the clear at rest.

A space's model keys are held one of two ways (ADR 0014). Under the control plane, the
engine stores a reference and resolves it when a call needs the key. Standalone, and under
the control plane until it serves secrets, a literal key is sent and the engine encrypts it
with a master key from the environment, storing only the ciphertext. Whatever form is
stored, the key exists in the clear only in memory, for the duration of the call that uses
it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
from typing import Protocol
from urllib import error, parse, request

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENCRYPTED_PREFIX = "enc:v1:"
ENV_PREFIX = "env:"
CONTROL_PLANE_PREFIX = "cp:"
_KEY_BYTES = 32
_NONCE_BYTES = 12


class SecretError(Exception):
    """A secret could not be stored or revealed; the code and message are caller-safe."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SecretVault:
    """Encrypt secrets at rest with AES-GCM under one master key.

    Each value gets a fresh random nonce, so equal keys never produce equal ciphertexts and
    a stored token reveals nothing about another. The token carries a version prefix so the
    scheme can change later without breaking stored values.
    """

    def __init__(self, master_key: bytes) -> None:
        if len(master_key) != _KEY_BYTES:
            raise SecretError("secrets_key_invalid", "The secrets key must be 32 bytes")
        self._cipher = AESGCM(master_key)

    @classmethod
    def from_setting(cls, encoded: str) -> SecretVault:
        """Build a vault from the base64url master key in the engine's settings."""

        try:
            raw = base64.urlsafe_b64decode(encoded.strip() + "=" * (-len(encoded.strip()) % 4))
        except (ValueError, TypeError) as exc:
            raise SecretError("secrets_key_invalid", "The secrets key is not base64url") from exc
        return cls(raw)

    @staticmethod
    def generate_setting() -> str:
        """Return a new random master key in the form the setting expects."""

        return base64.urlsafe_b64encode(secrets.token_bytes(_KEY_BYTES)).decode().rstrip("=")

    def seal(self, value: str) -> str:
        """Encrypt a value into a stored token."""

        nonce = secrets.token_bytes(_NONCE_BYTES)
        sealed = self._cipher.encrypt(nonce, value.encode("utf-8"), None)
        return ENCRYPTED_PREFIX + base64.urlsafe_b64encode(nonce + sealed).decode()

    def open(self, token: str) -> str:
        """Decrypt a stored token; a token sealed under another key is refused."""

        if not token.startswith(ENCRYPTED_PREFIX):
            raise SecretError("secret_invalid", "Stored secret is not encrypted")
        try:
            raw = base64.urlsafe_b64decode(token[len(ENCRYPTED_PREFIX) :])
            nonce, sealed = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
            return self._cipher.decrypt(nonce, sealed, None).decode("utf-8")
        except (ValueError, InvalidTag) as exc:
            raise SecretError("secret_invalid", "Stored secret cannot be decrypted") from exc


class SecretResolver(Protocol):
    """Turn a reference into a secret value at call time."""

    async def resolve(self, reference: str) -> str:
        """Return the value the reference names, or raise `SecretError`."""

        ...


class EnvSecretResolver:
    """Resolve `env:NAME` references from the engine's environment.

    This covers local development and the live tests until the control plane serves
    secrets; the value never enters the control database.
    """

    async def resolve(self, reference: str) -> str:
        """Return the named environment variable, or fail closed when it is unset."""

        value = os.environ.get(reference)
        if not value:
            raise SecretError("secret_unavailable", "A referenced secret is not available")
        return value


class ControlPlaneSecretResolver:
    """Resolve `cp:<id>` references from the control plane's secret endpoint.

    The engine calls `GET <base>/secrets/<id>` with its own service credential, which is the
    contract agreed on 2026-09-29; the control plane implements it. The request runs in a
    worker thread so it never blocks the event loop.
    """

    def __init__(self, base_url: str, token: str, timeout_seconds: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds

    async def resolve(self, reference: str) -> str:
        """Fetch the secret value; every failure is reported as unavailable."""

        url = f"{self._base_url}/secrets/{parse.quote(reference, safe='')}"
        try:
            body = await asyncio.to_thread(self._fetch, url)
            value = json.loads(body).get("value")
        except (error.URLError, TimeoutError, ValueError, OSError) as exc:
            raise SecretError("secret_unavailable", "A referenced secret is not available") from exc
        if not isinstance(value, str) or not value:
            raise SecretError("secret_unavailable", "A referenced secret is not available")
        return value

    def _fetch(self, url: str) -> bytes:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        with request.urlopen(request.Request(url, headers=headers), timeout=self._timeout) as reply:
            return reply.read()


class SecretStore:
    """Store keys as tokens and reveal them for a call.

    A literal key becomes an encrypted token, which needs the vault; without a master key
    the engine refuses literal keys rather than storing them in the clear. A reference is
    stored as given and resolved through the resolver for its prefix.
    """

    def __init__(
        self, vault: SecretVault | None, resolvers: dict[str, SecretResolver] | None = None
    ) -> None:
        self._vault = vault
        self._resolvers = resolvers or {}

    @property
    def accepts_literals(self) -> bool:
        """Whether a literal key can be stored, which needs the master key."""

        return self._vault is not None

    def is_reference(self, value: str) -> bool:
        """Whether a value names a secret held elsewhere."""

        return any(value.startswith(prefix) for prefix in self._resolvers)

    def store_literal(self, value: str) -> str:
        """Encrypt a literal key into a stored token."""

        if self._vault is None:
            raise SecretError(
                "secrets_not_configured", "Storing keys needs the engine's secrets key"
            )
        return self._vault.seal(value)

    def store_reference(self, reference: str) -> str:
        """Accept a reference whose prefix has a resolver."""

        if not self.is_reference(reference):
            raise SecretError("secret_reference_invalid", "The key reference is not supported")
        return reference

    def kind(self, stored: str) -> str:
        """Describe a stored token without revealing anything: encrypted or a reference."""

        if stored.startswith(ENCRYPTED_PREFIX):
            return "encrypted"
        for prefix in self._resolvers:
            if stored.startswith(prefix):
                return "reference"
        return "unknown"

    async def reveal(self, stored: str) -> str:
        """Return the key a stored token stands for."""

        if stored.startswith(ENCRYPTED_PREFIX):
            if self._vault is None:
                raise SecretError(
                    "secrets_not_configured",
                    "Revealing a stored key needs the engine's secrets key",
                )
            return self._vault.open(stored)
        for prefix, resolver in self._resolvers.items():
            if stored.startswith(prefix):
                return await resolver.resolve(stored[len(prefix) :])
        raise SecretError("secret_invalid", "Stored secret has an unknown form")


def build_secret_store(
    secrets_key: str, control_plane_url: str, control_plane_token: str
) -> SecretStore:
    """Build the store from settings: an optional vault, plus the resolvers that apply."""

    vault = SecretVault.from_setting(secrets_key) if secrets_key else None
    resolvers: dict[str, SecretResolver] = {ENV_PREFIX: EnvSecretResolver()}
    if control_plane_url:
        resolvers[CONTROL_PLANE_PREFIX] = ControlPlaneSecretResolver(
            control_plane_url, control_plane_token
        )
    return SecretStore(vault, resolvers)

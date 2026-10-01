"""Keys at rest and by reference: the vault, the resolvers, and the store that dispatches."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from context_engine.security.secrets import (
    CONTROL_PLANE_PREFIX,
    ENV_PREFIX,
    ControlPlaneSecretResolver,
    EnvSecretResolver,
    SecretError,
    SecretStore,
    SecretVault,
    build_secret_store,
)


def test_vault_round_trips_and_refuses_other_keys_and_tampering():
    vault = SecretVault.from_setting(SecretVault.generate_setting())
    token = vault.seal("sk-live-secret")

    assert token.startswith("enc:v1:") and "sk-live" not in token
    assert vault.seal("sk-live-secret") != token, "a fresh nonce hides equal keys"
    assert vault.open(token) == "sk-live-secret"

    other = SecretVault.from_setting(SecretVault.generate_setting())
    with pytest.raises(SecretError) as wrong_key:
        other.open(token)
    assert wrong_key.value.code == "secret_invalid"
    with pytest.raises(SecretError):
        vault.open(token[:-4] + "AAAA")
    with pytest.raises(SecretError) as short:
        SecretVault(b"too-short")
    assert short.value.code == "secrets_key_invalid"


async def test_env_resolver_reads_the_environment_and_fails_closed(monkeypatch):
    monkeypatch.setenv("CONTEXT_ENGINE_TEST_MODEL_KEY", "from-env")
    assert await EnvSecretResolver().resolve("CONTEXT_ENGINE_TEST_MODEL_KEY") == "from-env"
    monkeypatch.delenv("CONTEXT_ENGINE_TEST_MODEL_KEY")
    with pytest.raises(SecretError) as missing:
        await EnvSecretResolver().resolve("CONTEXT_ENGINE_TEST_MODEL_KEY")
    assert missing.value.code == "secret_unavailable"


class _SecretEndpoint(BaseHTTPRequestHandler):
    """The agreed control-plane contract: GET /secrets/{id} with a bearer token."""

    def do_GET(self) -> None:  # noqa: N802 - the base class names it so
        if self.headers.get("Authorization") != "Bearer engine-service-token":
            self.send_response(401)
            self.end_headers()
            return
        if self.path == "/secrets/secret-01":
            body = json.dumps({"value": "sk-from-control-plane"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def control_plane():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SecretEndpoint)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()


async def test_control_plane_resolver_follows_the_agreed_contract(control_plane):
    resolver = ControlPlaneSecretResolver(control_plane + "/", "engine-service-token")
    assert await resolver.resolve("secret-01") == "sk-from-control-plane"

    with pytest.raises(SecretError) as unknown:
        await resolver.resolve("secret-02")
    assert unknown.value.code == "secret_unavailable"

    with pytest.raises(SecretError) as denied:
        await ControlPlaneSecretResolver(control_plane, "wrong-token").resolve("secret-01")
    assert denied.value.code == "secret_unavailable"

    with pytest.raises(SecretError):
        await ControlPlaneSecretResolver("http://127.0.0.1:9", "t", timeout_seconds=1).resolve("x")


async def test_store_dispatches_by_form_and_refuses_literals_without_a_vault(monkeypatch):
    vault = SecretVault.from_setting(SecretVault.generate_setting())
    store = SecretStore(vault, {ENV_PREFIX: EnvSecretResolver()})
    monkeypatch.setenv("CONTEXT_ENGINE_TEST_REF", "from-env")

    sealed = store.store_literal("sk-literal")
    assert store.kind(sealed) == "encrypted" and await store.reveal(sealed) == "sk-literal"
    reference = store.store_reference("env:CONTEXT_ENGINE_TEST_REF")
    assert store.kind(reference) == "reference" and await store.reveal(reference) == "from-env"
    assert store.kind("something-else") == "unknown"
    with pytest.raises(SecretError) as bad_reference:
        store.store_reference("vault:whatever")
    assert bad_reference.value.code == "secret_reference_invalid"
    with pytest.raises(SecretError) as unknown_form:
        await store.reveal("something-else")
    assert unknown_form.value.code == "secret_invalid"

    without_vault = SecretStore(None, {ENV_PREFIX: EnvSecretResolver()})
    assert not without_vault.accepts_literals
    with pytest.raises(SecretError) as no_vault:
        without_vault.store_literal("sk-literal")
    assert no_vault.value.code == "secrets_not_configured"
    with pytest.raises(SecretError):
        await without_vault.reveal(sealed)


def test_build_secret_store_from_settings():
    store = build_secret_store("", "", "")
    assert not store.accepts_literals
    assert store.is_reference("env:X") and not store.is_reference("cp:x")

    with_control_plane = build_secret_store(SecretVault.generate_setting(), "http://cp", "t")
    assert with_control_plane.accepts_literals
    assert with_control_plane.is_reference(CONTROL_PLANE_PREFIX + "x")

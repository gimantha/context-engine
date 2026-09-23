"""Static token verifier and authentication-mode tests."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from context_engine.config import Settings
from context_engine.domain import Action, PrincipalKind
from context_engine.security.identity import (
    AuthenticationError,
    StaticTokenVerifier,
    build_token_verifier,
    is_loopback_host,
)

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
TOKEN = "unit-test-token-0123456789abcdef"


def _write(path: Path, tokens: list[dict]) -> Path:
    path.write_text(json.dumps({"version": "1", "tokens": tokens}))
    return path


def _entry(**overrides) -> dict:
    value = {
        "token": TOKEN,
        "issuer": "static://test",
        "subject": "alice",
        "kind": "user",
        "email": "alice@example.invalid",
        "groups": ["readers"],
        "bootstrapActions": ["space.manage"],
    }
    value.update(overrides)
    return value


@pytest.mark.asyncio
async def test_static_verifier_returns_identity_for_known_token(tmp_path):
    verifier = StaticTokenVerifier.from_file(_write(tmp_path / "tokens.json", [_entry()]))

    identity = await verifier.verify(TOKEN)

    assert identity.issuer == "static://test"
    assert identity.subject == "alice"
    assert identity.kind is PrincipalKind.USER
    assert identity.groups == frozenset({"readers"})
    assert verifier.groups_for("static://test", "alice") == frozenset({"readers"})
    assert verifier.groups_for("static://test", "nobody") == frozenset()
    assert verifier.identities[0].bootstrap_actions == frozenset({Action.SPACE_MANAGE})


@pytest.mark.asyncio
async def test_static_verifier_rejects_unknown_and_expired_tokens(tmp_path):
    verifier = StaticTokenVerifier.from_file(
        _write(
            tmp_path / "tokens.json",
            [
                _entry(),
                _entry(
                    token="expired-token-0123456789abcdef",
                    subject="bob",
                    expiresAt="2000-01-01T00:00:00Z",
                ),
            ],
        )
    )

    with pytest.raises(AuthenticationError):
        await verifier.verify("unknown-token-0123456789abcdef")
    with pytest.raises(AuthenticationError):
        await verifier.verify("expired-token-0123456789abcdef")
    with pytest.raises(AuthenticationError):
        await verifier.verify("")


@pytest.mark.asyncio
async def test_missing_token_file_fails_closed(tmp_path):
    verifier = StaticTokenVerifier.from_file(tmp_path / "absent.json")

    assert verifier.identities == ()
    with pytest.raises(AuthenticationError):
        await verifier.verify(TOKEN)


@pytest.mark.parametrize(
    "tokens",
    [
        [_entry(token="short")],
        [_entry(issuer="")],
        [_entry(kind="robot")],
        [_entry(bootstrapActions=["delete.everything"])],
        [_entry(), _entry(token="another-token-0123456789abcdef")],
        [_entry(expiresAt="2027-01-01T00:00:00")],
    ],
)
def test_malformed_token_files_refuse_to_load(tmp_path, tokens):
    with pytest.raises(ValueError):
        StaticTokenVerifier.from_file(_write(tmp_path / "tokens.json", tokens))


def test_build_token_verifier_rejects_unknown_modes(tmp_path):
    settings = Settings(
        database_path=tmp_path / "control.db",
        migrations_path=MIGRATIONS,
        static_tokens_path=tmp_path / "tokens.json",
    )
    assert build_token_verifier(settings).identities == ()
    with pytest.raises(RuntimeError):
        build_token_verifier(replace(settings, auth_mode="none"))


def test_loopback_detection():
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("::1")
    assert is_loopback_host("localhost")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("10.0.0.5")
    assert not is_loopback_host("example.invalid")

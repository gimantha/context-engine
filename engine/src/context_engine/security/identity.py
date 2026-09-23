"""Token verification and the static pre-shared-token identity mode (ADR 0009)."""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from context_engine.config import Settings
from context_engine.domain import Action, Principal, PrincipalKind
from context_engine.observability import get_logger, log_event

logger = get_logger(__name__)

_MIN_TOKEN_LENGTH = 16
_SUPPORTED_MODES = ("static",)


class AuthenticationError(Exception):
    """Indicate that a bearer credential could not be verified."""


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    """Identity facts a verifier vouches for after validating a credential."""

    issuer: str
    subject: str
    kind: PrincipalKind
    email: str | None
    groups: frozenset[str]
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    """Engine principal resolved for one request, never taken from a request body."""

    principal_id: str
    kind: PrincipalKind
    email: str | None
    groups: frozenset[str]
    trace_id: str


class TokenVerifier(Protocol):
    """Validate a bearer credential and return the identity it proves."""

    async def verify(self, bearer_token: str) -> VerifiedIdentity:
        """Return a verified identity or raise `AuthenticationError`; never partial claims."""

        ...


class GroupResolver(Protocol):
    """Resolve current group membership for a principal outside a request."""

    def groups_for(self, issuer: str, subject: str) -> frozenset[str]:
        """Return the groups currently attributed to the identity, or an empty set."""

        ...


@dataclass(frozen=True, slots=True)
class StaticIdentity:
    """One pre-shared token and the identity plus bootstrap grants it stands for."""

    token: str
    identity: VerifiedIdentity
    bootstrap_actions: frozenset[Action]


def _parse_datetime(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"static token {field} must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"static token {field} must carry a timezone")
    return parsed.astimezone(UTC)


def _parse_identity(entry: Any, index: int) -> StaticIdentity:
    if not isinstance(entry, dict):
        raise ValueError(f"static token entry {index} must be an object")
    token = entry.get("token")
    if not isinstance(token, str) or len(token) < _MIN_TOKEN_LENGTH:
        raise ValueError(
            f"static token entry {index} needs a token of at least {_MIN_TOKEN_LENGTH} characters"
        )
    issuer = entry.get("issuer")
    subject = entry.get("subject")
    if not isinstance(issuer, str) or not issuer.strip():
        raise ValueError(f"static token entry {index} needs an issuer")
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError(f"static token entry {index} needs a subject")
    try:
        kind = PrincipalKind(entry.get("kind", "user"))
    except ValueError as exc:
        raise ValueError(f"static token entry {index} has an unknown kind") from exc
    email = entry.get("email")
    if email is not None and not isinstance(email, str):
        raise ValueError(f"static token entry {index} email must be a string")
    groups = entry.get("groups", [])
    if not isinstance(groups, list) or not all(isinstance(item, str) and item for item in groups):
        raise ValueError(f"static token entry {index} groups must be a list of names")
    raw_actions = entry.get("bootstrapActions", [])
    if not isinstance(raw_actions, list):
        raise ValueError(f"static token entry {index} bootstrapActions must be a list")
    try:
        actions = frozenset(Action(item) for item in raw_actions)
    except ValueError as exc:
        raise ValueError(f"static token entry {index} has an unknown bootstrap action") from exc
    return StaticIdentity(
        token=token,
        identity=VerifiedIdentity(
            issuer=issuer.strip(),
            subject=subject.strip(),
            kind=kind,
            email=email,
            groups=frozenset(groups),
            expires_at=_parse_datetime(entry.get("expiresAt"), "expiresAt"),
        ),
        bootstrap_actions=actions,
    )


class StaticTokenVerifier:
    """Map pre-shared tokens to fixed identities for local development and tests.

    The token file is the only identity source in static mode. A missing file yields an
    empty verifier, so every request fails closed rather than falling back to a default user.
    """

    def __init__(self, identities: tuple[StaticIdentity, ...]) -> None:
        seen: set[tuple[str, str]] = set()
        for item in identities:
            key = (item.identity.issuer, item.identity.subject)
            if key in seen:
                raise ValueError("static token file repeats an issuer and subject pair")
            seen.add(key)
        self._identities = identities

    @classmethod
    def from_file(cls, path: Path) -> StaticTokenVerifier:
        """Load the token file, or return an empty verifier when it does not exist."""

        if not path.exists():
            log_event(logger, "static_tokens_missing", path=str(path))
            return cls(())
        try:
            document = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError(f"static token file is not valid JSON: {path}") from exc
        if not isinstance(document, dict) or document.get("version") != "1":
            raise ValueError("static token file must declare version '1'")
        entries = document.get("tokens")
        if not isinstance(entries, list):
            raise ValueError("static token file must contain a tokens list")
        return cls(tuple(_parse_identity(entry, index) for index, entry in enumerate(entries)))

    @property
    def identities(self) -> tuple[StaticIdentity, ...]:
        """Return the configured identities for bootstrap provisioning."""

        return self._identities

    async def verify(self, bearer_token: str) -> VerifiedIdentity:
        """Match the token in constant time per entry and enforce its expiry."""

        candidate = bearer_token.encode()
        matched: StaticIdentity | None = None
        for item in self._identities:
            # Compare every entry so response timing does not reveal which token prefix matched.
            if hmac.compare_digest(candidate, item.token.encode()):
                matched = item
        if matched is None:
            raise AuthenticationError("unknown credential")
        expires_at = matched.identity.expires_at
        if expires_at is not None and expires_at <= datetime.now(UTC):
            raise AuthenticationError("expired credential")
        return matched.identity

    def groups_for(self, issuer: str, subject: str) -> frozenset[str]:
        """Return the groups configured for an identity, or none when it is unknown."""

        for item in self._identities:
            if item.identity.issuer == issuer and item.identity.subject == subject:
                return item.identity.groups
        return frozenset()


class PrincipalRegistry(Protocol):
    """Durable principal and bootstrap-grant storage used at startup."""

    def resolve_principal(
        self, issuer: str, subject: str, kind: PrincipalKind, email: str | None
    ) -> Principal:
        """Return or create the principal for an identity."""

        ...

    def ensure_bootstrap_grant(self, principal_id: str, actions: frozenset[Action]) -> bool:
        """Create the root bootstrap grant once and report whether it was created."""

        ...


def provision_static_identities(verifier: StaticTokenVerifier, registry: PrincipalRegistry) -> int:
    """Register every static identity so grants can target it before its first request.

    Bootstrap grants are created only once per principal; later edits through the grants API
    are never overwritten by a restart.
    """

    created = 0
    for item in verifier.identities:
        identity = item.identity
        principal = registry.resolve_principal(
            identity.issuer, identity.subject, identity.kind, identity.email
        )
        if registry.ensure_bootstrap_grant(principal.id, item.bootstrap_actions):
            created += 1
            log_event(logger, "bootstrap_grant_created", principal_id=principal.id)
    return created


def build_token_verifier(settings: Settings) -> StaticTokenVerifier:
    """Construct the verifier for the configured mode; unknown modes refuse to start."""

    if settings.auth_mode not in _SUPPORTED_MODES:
        raise RuntimeError(
            f"Authentication mode {settings.auth_mode!r} is not available in this milestone; "
            f"supported modes: {', '.join(_SUPPORTED_MODES)}"
        )
    return StaticTokenVerifier.from_file(settings.static_tokens_path)


def is_loopback_host(host: str) -> bool:
    """Return whether a bind address only serves the local machine."""

    import ipaddress

    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

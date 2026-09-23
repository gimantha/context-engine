"""Grant resolution, decision recording, and principal identity tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from context_engine.domain import ROOT_RESOURCE_ID, Action, Grant, PrincipalKind
from context_engine.observability import MetricsRegistry
from context_engine.persistence import (
    AuthorizationRepository,
    ControlDatabase,
    ControlPlaneRepository,
)
from context_engine.security.authorization import Authorizer, effective_actions

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def _grant(resource: str, actions: set[Action], principal_id=None, group=None) -> Grant:
    now = datetime.now(UTC)
    return Grant("g", resource, frozenset(actions), principal_id, group, "test", now, now)


def test_effective_actions_unions_principal_and_group_grants():
    grants = [
        _grant("spc_1", {Action.CONTEXT_READ}, principal_id="prn_a"),
        _grant("spc_1", {Action.INGEST_WRITE}, group="writers"),
        _grant("spc_1", {Action.ACCESS_MANAGE}, principal_id="prn_other"),
        _grant("spc_1", {Action.RECORD_DELETE}, group="admins"),
    ]

    actions = effective_actions(grants, "prn_a", frozenset({"writers"}))

    assert actions == frozenset({Action.CONTEXT_READ, Action.INGEST_WRITE})
    assert effective_actions(grants, "prn_none", frozenset()) == frozenset()


def _repositories(tmp_path):
    database = ControlDatabase(tmp_path / "control.db", MIGRATIONS)
    database.migrate()
    return ControlPlaneRepository(database), AuthorizationRepository(database)


def test_principals_are_keyed_on_issuer_and_subject(tmp_path):
    _, authorization = _repositories(tmp_path)

    first = authorization.resolve_principal("iss", "sub", PrincipalKind.USER, "a@example.invalid")
    renamed = authorization.resolve_principal("iss", "sub", PrincipalKind.USER, "b@example.invalid")
    other_issuer = authorization.resolve_principal("iss2", "sub", PrincipalKind.USER, None)

    assert first.id.startswith("prn_")
    assert renamed.id == first.id
    assert renamed.email == "b@example.invalid"
    assert other_issuer.id != first.id
    assert authorization.get_principal(first.id) == renamed


def test_decisions_record_reason_codes_and_inherit_root_grants(tmp_path):
    control, authorization = _repositories(tmp_path)
    metrics = MetricsRegistry()
    authorizer = Authorizer(authorization, metrics)
    principal = authorization.resolve_principal("iss", "alice", PrincipalKind.USER, None)
    space = control.create_space("Ops", None)
    groups = frozenset({"readers"})

    unknown = authorizer.decide(principal.id, groups, Action.CONTEXT_READ, "spc_missing", "t1")
    none = authorizer.decide(principal.id, groups, Action.CONTEXT_READ, space.id, "t2")
    version_before = authorization.policy_version()
    authorization.put_grant(
        space.id, "readers", frozenset({Action.CONTEXT_READ}), None, "readers", "t"
    )
    partial = authorizer.decide(principal.id, groups, Action.INGEST_WRITE, space.id, "t3")
    allowed = authorizer.decide(principal.id, groups, Action.CONTEXT_READ, space.id, "t4")
    authorization.put_grant(
        ROOT_RESOURCE_ID, "root", frozenset({Action.SPACE_MANAGE}), principal.id, None, "t"
    )
    inherited = authorizer.decide(principal.id, groups, Action.SPACE_MANAGE, space.id, "t5")

    assert (unknown.allowed, unknown.reason_code) == (False, "resource_not_found")
    assert (none.allowed, none.reason_code) == (False, "no_grants")
    assert (partial.allowed, partial.reason_code) == (False, "action_not_granted")
    assert allowed.allowed and allowed.reason_code == "allowed"
    assert inherited.allowed and Action.CONTEXT_READ in inherited.actions
    assert authorization.policy_version() == version_before + 2
    assert allowed.policy_version == version_before + 1
    assert authorization.count_decisions() == 5
    assert authorization.count_decisions("action_not_granted") == 1
    assert metrics.snapshot()["context_engine_access_denied_total"] == 3
    assert authorizer.is_visible(principal.id, groups, space.id)
    stranger = authorization.resolve_principal("iss", "stranger", PrincipalKind.USER, None)
    assert not authorizer.is_visible(stranger.id, frozenset({"readers"}), "spc_missing")
    assert not authorizer.is_visible(stranger.id, frozenset(), space.id)
    assert authorizer.is_visible(stranger.id, frozenset({"readers"}), space.id)


def test_bootstrap_grant_is_created_once_and_never_overwritten(tmp_path):
    _, authorization = _repositories(tmp_path)
    principal = authorization.resolve_principal("iss", "admin", PrincipalKind.USER, None)

    assert authorization.ensure_bootstrap_grant(principal.id, frozenset({Action.ACCESS_MANAGE}))
    assert not authorization.ensure_bootstrap_grant(principal.id, frozenset({Action.ACCESS_MANAGE}))
    grant_id = f"bootstrap-{principal.id}"
    authorization.put_grant(
        ROOT_RESOURCE_ID, grant_id, frozenset({Action.CONTEXT_READ}), principal.id, None, "admin"
    )
    assert not authorization.ensure_bootstrap_grant(principal.id, frozenset({Action.ACCESS_MANAGE}))

    assert authorization.get_grant(ROOT_RESOURCE_ID, grant_id).actions == frozenset(
        {Action.CONTEXT_READ}
    )
    assert authorization.delete_grant(ROOT_RESOURCE_ID, grant_id)
    assert not authorization.delete_grant(ROOT_RESOURCE_ID, grant_id)
    assert authorization.ensure_bootstrap_grant(principal.id, frozenset({Action.ACCESS_MANAGE}))

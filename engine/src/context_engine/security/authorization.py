"""Grant-based authorization decisions with stable, recorded reason codes."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from context_engine.domain import Action, Grant
from context_engine.observability import MetricsRegistry, get_logger, log_event

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AccessDecision:
    """Outcome of one authorization check on one resource."""

    allowed: bool
    reason_code: str
    policy_version: int
    actions: frozenset[Action]


class AuthorizationStore(Protocol):
    """Durable grant and decision storage required by the authorizer."""

    def policy_version(self) -> int:
        """Return the current monotonic policy version."""

        ...

    def resource_chain(self, resource_id: str) -> tuple[str, ...] | None:
        """Return the resource and its ancestors, or None when the resource does not exist."""

        ...

    def grants_for(
        self, resource_ids: tuple[str, ...], principal_id: str, groups: frozenset[str]
    ) -> tuple[Grant, ...]:
        """Return grants on the given resources that name the principal or its groups."""

        ...

    def record_decision(
        self,
        principal_id: str,
        action: Action,
        resource_id: str,
        allowed: bool,
        reason_code: str,
        policy_version: int,
        trace_id: str,
    ) -> None:
        """Persist one access decision for audit."""

        ...


def effective_actions(
    grants: Iterable[Grant], principal_id: str, groups: frozenset[str]
) -> frozenset[Action]:
    """Union the actions of every grant that names the principal or one of its groups."""

    actions: set[Action] = set()
    for grant in grants:
        if grant.principal_id == principal_id or (
            grant.group is not None and grant.group in groups
        ):
            actions.update(grant.actions)
    return frozenset(actions)


class Authorizer:
    """Resolve effective actions from grants and record every explicit decision."""

    def __init__(self, store: AuthorizationStore, metrics: MetricsRegistry) -> None:
        self._store = store
        self._metrics = metrics

    def policy_version(self) -> int:
        """Return the current policy version."""

        return self._store.policy_version()

    def effective_actions(
        self, principal_id: str, groups: frozenset[str], resource_id: str
    ) -> frozenset[Action] | None:
        """Return the principal's actions on a resource, or None when it does not exist."""

        chain = self._store.resource_chain(resource_id)
        if chain is None:
            return None
        return effective_actions(
            self._store.grants_for(chain, principal_id, groups), principal_id, groups
        )

    def is_visible(self, principal_id: str, groups: frozenset[str], resource_id: str) -> bool:
        """Return whether the principal holds any action on the resource.

        Visibility is not recorded as a decision; callers use it to answer with a generic
        not-found instead of revealing that a resource exists.
        """

        actions = self.effective_actions(principal_id, groups, resource_id)
        return bool(actions)

    def decide(
        self,
        principal_id: str,
        groups: frozenset[str],
        action: Action,
        resource_id: str,
        trace_id: str,
    ) -> AccessDecision:
        """Decide one action on one resource and durably record the outcome."""

        version = self._store.policy_version()
        actions = self.effective_actions(principal_id, groups, resource_id)
        if actions is None:
            allowed, reason, held = False, "resource_not_found", frozenset()
        elif action in actions:
            allowed, reason, held = True, "allowed", actions
        elif actions:
            allowed, reason, held = False, "action_not_granted", actions
        else:
            allowed, reason, held = False, "no_grants", actions
        self._store.record_decision(
            principal_id, action, resource_id, allowed, reason, version, trace_id
        )
        self._metrics.increment(
            "context_engine_access_allowed_total"
            if allowed
            else "context_engine_access_denied_total"
        )
        log_event(
            logger,
            "access_decision",
            principal_id=principal_id,
            action=action.value,
            resource_id=resource_id,
            allowed=allowed,
            reason_code=reason,
            policy_version=version,
            trace_id=trace_id,
        )
        return AccessDecision(allowed, reason, version, held)

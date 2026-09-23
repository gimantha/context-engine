"""Re-check a job's authorization at execution time (threat T06)."""

from __future__ import annotations

from typing import Protocol

from context_engine.domain import Job, Principal, required_action
from context_engine.security.authorization import AccessDecision, Authorizer
from context_engine.security.identity import GroupResolver


class PrincipalLookup(Protocol):
    """Read access to durable principals."""

    def get_principal(self, principal_id: str) -> Principal | None:
        """Return a principal by engine identifier when it exists."""

        ...


class JobAuthorizer:
    """Decide whether a claimed job's principal still holds the action it needs."""

    def __init__(
        self, authorizer: Authorizer, principals: PrincipalLookup, groups: GroupResolver
    ) -> None:
        self._authorizer = authorizer
        self._principals = principals
        self._groups = groups

    def authorize(self, job: Job) -> AccessDecision:
        """Return an allowed decision only when principal, space, and grant all still hold."""

        version = 0
        resource_id = job.resource_id
        if job.principal_id is None or resource_id is None:
            # Jobs accepted without an identity or target cannot be made valid by retrying.
            return AccessDecision(False, "missing_principal_or_resource", version, frozenset())
        principal = self._principals.get_principal(job.principal_id)
        if principal is None:
            return AccessDecision(False, "principal_unknown", version, frozenset())
        groups = self._groups.groups_for(principal.issuer, principal.subject)
        # The check targets the source when the payload names one, so a revoked source
        # binding stops the job even if the principal still holds rights elsewhere.
        return self._authorizer.decide(
            principal.id, groups, required_action(job.operation), resource_id, job.trace_id
        )

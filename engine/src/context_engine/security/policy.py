"""Pure authorization decision used by all future interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from context_engine.knowledge_backend.types import AccessPartitionRef


class Action(StrEnum):
    """Fine-grained actions understood by the engine policy service."""

    CONTEXT_READ = "context.read"
    INGEST_WRITE = "ingest.write"
    CONTEXT_ENRICH = "context.enrich"
    RECORD_DELETE = "record.delete"
    EVIDENCE_READ = "evidence.read"
    TRACE_READ = "trace.read"
    ACCESS_MANAGE = "access.manage"


@dataclass(frozen=True, slots=True)
class PolicyInput:
    """Trusted identity, grants, and audience facts for one decision."""

    principal_id: str
    action: Action
    space_id: str
    granted_actions: frozenset[Action]
    principal_audiences: frozenset[str]
    partition_audiences: tuple[tuple[AccessPartitionRef, frozenset[str]], ...]
    policy_version: str


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Caller-safe authorization outcome and resolved internal scope."""

    allowed: bool
    reason_code: str
    policy_version: str
    partitions: tuple[AccessPartitionRef, ...] = ()


def authorize(value: PolicyInput) -> PolicyDecision:
    """Resolve allowed internal partitions or return a deny reason."""

    if not value.principal_id or not value.space_id or not value.policy_version:
        return PolicyDecision(False, "invalid_context", value.policy_version)
    if value.action not in value.granted_actions:
        return PolicyDecision(False, "action_not_granted", value.policy_version)
    partitions = tuple(
        partition
        for partition, audiences in value.partition_audiences
        if audiences and audiences.issubset(value.principal_audiences)
    )
    if value.action in {Action.CONTEXT_READ, Action.EVIDENCE_READ, Action.TRACE_READ}:
        if not partitions:
            return PolicyDecision(False, "no_compatible_audience", value.policy_version)
    return PolicyDecision(True, "allowed", value.policy_version, partitions)

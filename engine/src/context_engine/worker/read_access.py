"""Materialize backend read access from engine grants and group membership (ADR 0011).

Engine policy stays authoritative and is checked on every request. Backend read access is
defense in depth: it is recomputed whenever grants or principals change, and a backend
failure only delays it, because the next check runs a full synchronization.
"""

from __future__ import annotations

from context_engine.domain import AccessPartition, Action, PrincipalKind
from context_engine.knowledge_backend import (
    AccessPartitionRef,
    BackendError,
    BackendErrorCode,
    KnowledgeBackend,
    PrincipalContext,
)
from context_engine.observability import MetricsRegistry, get_logger, log_event
from context_engine.persistence import (
    AuthorizationRepository,
    ReadAccessRepository,
    SourceRepository,
)
from context_engine.security.authorization import Authorizer
from context_engine.security.identity import GroupResolver

logger = get_logger(__name__)


class ReadAccessSynchronizer:
    """Grant and revoke backend read access so it matches who may read each partition."""

    def __init__(
        self,
        authorization: AuthorizationRepository,
        sources: SourceRepository,
        applied: ReadAccessRepository,
        backend: KnowledgeBackend,
        groups: GroupResolver,
        service_principal_id: str,
        metrics: MetricsRegistry,
    ) -> None:
        self._authorization = authorization
        self._authorizer = Authorizer(authorization, metrics)
        self._sources = sources
        self._applied = applied
        self._backend = backend
        self._groups = groups
        self._service_principal_id = service_principal_id
        self._metrics = metrics

    def readers(self, partition: AccessPartition) -> frozenset[str]:
        """Return the principals who may read a partition under current engine policy.

        A reader holds `context.read` on the space and belongs to at least one of the
        partition's audiences. Service identities never read.
        """

        audiences = set(partition.audiences)
        readers: set[str] = set()
        for principal in self._authorization.list_principals():
            if principal.kind is PrincipalKind.SERVICE:
                continue
            groups = self._groups.groups_for(principal.issuer, principal.subject)
            if not groups & audiences:
                continue
            actions = self._authorizer.effective_actions(principal.id, groups, partition.space_id)
            if actions and Action.CONTEXT_READ in actions:
                readers.add(principal.id)
        return frozenset(readers)

    async def sync_partition(self, partition_id: str, trace_id: str) -> bool:
        """Apply the difference for one partition; return whether it fully succeeded."""

        partition = self._sources.get_partition(partition_id)
        if partition is None:
            return True
        owner = PrincipalContext(self._service_principal_id, trace_id)
        reference = AccessPartitionRef(partition_id)
        desired = self.readers(partition)
        applied = self._applied.applied_readers(partition_id)
        try:
            for principal_id in sorted(desired - applied):
                try:
                    await self._backend.grant_read(
                        reference, PrincipalContext(principal_id, trace_id), owner
                    )
                except BackendError as exc:
                    if exc.code is BackendErrorCode.NOT_FOUND:
                        # Nothing is stored yet; the first write triggers another pass.
                        return True
                    raise
                self._applied.record_grant(partition_id, principal_id)
                self._metrics.increment("context_engine_read_grants_total")
            for principal_id in sorted(applied - desired):
                await self._backend.revoke_read(
                    reference, PrincipalContext(principal_id, trace_id), owner
                )
                self._applied.remove_grant(partition_id, principal_id)
                self._metrics.increment("context_engine_read_revocations_total")
        except BackendError as exc:
            # Engine policy still governs access; make the next check retry everything.
            self._applied.invalidate()
            log_event(
                logger, "read_access_sync_failed", error_code=exc.code.value, trace_id=trace_id
            )
            return False
        return True

    async def sync_if_changed(self, trace_id: str, *, force: bool = False) -> bool:
        """Run a full synchronization when grants or principals changed since the last one."""

        fingerprint = (
            self._authorization.policy_version(),
            len(self._authorization.list_principals()),
        )
        if not force and self._applied.last_synced() == fingerprint:
            return False
        succeeded = True
        for partition in self._sources.list_all_partitions():
            succeeded = await self.sync_partition(partition.id, trace_id) and succeeded
        if succeeded:
            self._applied.set_synced(*fingerprint)
        return succeeded

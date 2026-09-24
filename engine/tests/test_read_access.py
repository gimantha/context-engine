"""Backend read access materialized from engine grants and group membership."""

from __future__ import annotations

from pathlib import Path

from context_engine.domain import Action, PrincipalKind, VersionOrdering
from context_engine.knowledge_backend import (
    AccessPartitionRef,
    BackendError,
    BackendErrorCode,
    DummyKnowledgeBackend,
    PrincipalContext,
    SourceRecord,
)
from context_engine.observability import MetricsRegistry
from context_engine.persistence import (
    AuthorizationRepository,
    ControlDatabase,
    ControlPlaneRepository,
    ReadAccessRepository,
    SourceRepository,
)
from context_engine.worker import ReadAccessSynchronizer

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
SERVICE = PrincipalContext("context-engine-service", "trace-sync")


class _Groups:
    def __init__(self, groups):
        self.groups = groups

    def groups_for(self, issuer, subject):
        return frozenset(self.groups.get(subject, ()))


class _Setup:
    def __init__(self, tmp_path, backend=None):
        database = ControlDatabase(tmp_path / "control.db", MIGRATIONS)
        database.migrate()
        self.authorization = AuthorizationRepository(database)
        self.sources = SourceRepository(database)
        self.applied = ReadAccessRepository(database)
        self.space = ControlPlaneRepository(database).create_space("Ops", None)
        self.sources.create_source(self.space.id, "Files", "file", VersionOrdering.NUMERIC, {})
        self.groups = _Groups(
            {
                "alice": {"staff", "team"},
                "bob": {"staff", "ops"},
                "carol": {"team"},
                "connector": {"staff", "team"},
            }
        )
        self.ids = {
            name: self.authorization.resolve_principal(
                "static://t",
                name,
                PrincipalKind.SERVICE if name == "connector" else PrincipalKind.USER,
                None,
            ).id
            for name in ("alice", "bob", "carol", "connector")
        }
        self.authorization.put_grant(
            self.space.id, "staff-read", frozenset({Action.CONTEXT_READ}), None, "staff", "t"
        )
        self.team = self.sources.ensure_partition(self.space.id, ("team",))
        self.both = self.sources.ensure_partition(self.space.id, ("ops", "team"))
        self.backend = backend or DummyKnowledgeBackend()
        self.sync = ReadAccessSynchronizer(
            self.authorization,
            self.sources,
            self.applied,
            self.backend,
            self.groups,
            SERVICE.principal_id,
            MetricsRegistry(),
        )

    async def store_in(self, partition_id):
        content = f"content for {partition_id}"
        record = SourceRecord(partition_id, "src", "1", content, "sha256:x")
        await self.backend.ingest(record, SERVICE, AccessPartitionRef(partition_id))

    def readers(self, partition_id):
        return self.backend.readers(AccessPartitionRef(partition_id))


async def test_readers_hold_the_read_action_and_share_an_audience(tmp_path):
    setup = _Setup(tmp_path)
    await setup.store_in(setup.team)
    await setup.store_in(setup.both)

    changed = await setup.sync.sync_if_changed("trace-1", force=True)
    unchanged = await setup.sync.sync_if_changed("trace-2")

    # Carol shares the audience but lacks the read action; service identities never read.
    assert changed and not unchanged
    assert setup.readers(setup.team) == {setup.ids["alice"]}
    assert setup.readers(setup.both) == {setup.ids["alice"], setup.ids["bob"]}
    assert setup.applied.applied_readers(setup.both) == setup.readers(setup.both)


async def test_revoked_grants_and_new_principals_are_applied(tmp_path):
    setup = _Setup(tmp_path)
    await setup.store_in(setup.team)
    await setup.sync.sync_if_changed("trace-1", force=True)

    setup.groups.groups["dave"] = {"staff", "team"}
    dave = setup.authorization.resolve_principal("static://t", "dave", PrincipalKind.USER, None)
    assert await setup.sync.sync_if_changed("trace-2")
    with_dave = setup.readers(setup.team)
    setup.authorization.delete_grant(setup.space.id, "staff-read")
    assert await setup.sync.sync_if_changed("trace-3")

    assert with_dave == {setup.ids["alice"], dave.id}
    assert setup.readers(setup.team) == frozenset()
    assert setup.applied.applied_readers(setup.team) == frozenset()


async def test_empty_partitions_wait_for_their_first_write(tmp_path):
    setup = _Setup(tmp_path)

    assert await setup.sync.sync_if_changed("trace-1", force=True)
    await setup.store_in(setup.team)
    assert await setup.sync.sync_partition(setup.team, "trace-2")

    assert setup.readers(setup.team) == {setup.ids["alice"]}
    assert setup.applied.applied_readers(setup.both) == frozenset()


class _DownBackend(DummyKnowledgeBackend):
    async def grant_read(self, partition, reader, principal):
        raise BackendError(BackendErrorCode.UNAVAILABLE, "down", retryable=True)


async def test_backend_failures_force_a_full_retry(tmp_path):
    setup = _Setup(tmp_path, _DownBackend())
    await setup.store_in(setup.team)

    assert not await setup.sync.sync_if_changed("trace-1", force=True)
    assert setup.applied.last_synced() is None

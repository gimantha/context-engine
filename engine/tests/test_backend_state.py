"""Durable backend state, adapter restarts, and principal-to-identity mapping."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from test_cognee_adapter import FakeCogneeRuntime, resolve_user

from context_engine.knowledge_backend import (
    AccessPartitionRef,
    BackendError,
    BackendErrorCode,
    InMemoryBackendState,
    PrincipalContext,
    QueryRequest,
)
from context_engine.knowledge_backend.providers.cognee import (
    CogneeBackend,
    CogneeIdentityResolver,
    _NativeIngestion,
)
from context_engine.persistence import ControlDatabase, SqliteBackendState

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
SERVICE = PrincipalContext("context-engine-service", "trace-state")


def _sqlite_state(tmp_path) -> SqliteBackendState:
    database = ControlDatabase(tmp_path / "control.db", MIGRATIONS)
    database.migrate()
    return SqliteBackendState(database)


@pytest.fixture(params=["memory", "sqlite"])
def state(request, tmp_path):
    return InMemoryBackendState() if request.param == "memory" else _sqlite_state(tmp_path)


def test_backend_state_round_trips(state):
    assert state.get_binding("p1") is None
    state.put_binding("p1", "binding-1")
    state.put_binding("p1", "binding-2")
    state.put_record_reference("p1", "s1", "r1", "ref-1")
    state.put_record_reference("p1", "s1", "r1", "ref-2")
    state.put_record_reference("p1", "s2", "r1", "ref-3")

    assert state.get_binding("p1") == "binding-2"
    assert state.get_record_reference("p1", "s1", "r1") == "ref-2"
    assert state.find_record("ref-1") is None
    assert state.find_record("ref-2") == ("p1", "s1", "r1")
    assert state.find_record("ref-3") == ("p1", "s2", "r1")
    state.delete_record_reference("ref-2")
    assert state.get_record_reference("p1", "s1", "r1") is None
    assert state.claim_identity("prn_a", "native-1") == "native-1"
    assert state.claim_identity("prn_a", "native-2") == "native-1"
    assert state.get_identity("prn_a") == "native-1"
    assert state.get_identity("prn_b") is None


async def test_adapter_bindings_and_references_survive_a_restart(tmp_path, record_factory):
    state = _sqlite_state(tmp_path)
    runtime = FakeCogneeRuntime()
    partition = AccessPartitionRef("prt_restart")
    record = record_factory("doc-1", "1", "content")
    same_id_other_source = record_factory("doc-1", "1", "other", source_id="source-other")

    first = CogneeBackend(runtime, resolve_user, state)
    ingested = await first.ingest(record, SERVICE, partition)
    other = await first.ingest(same_id_other_source, SERVICE, partition)
    # A fresh adapter over the same database behaves as after a process restart.
    second = CogneeBackend(runtime, resolve_user, SqliteBackendState(state.database))
    updated = await second.update(record_factory("doc-1", "2", "new"), SERVICE, partition)
    queried = await second.query(QueryRequest("content"), SERVICE, (partition,))
    deleted = await second.delete(ingested.backend_reference, SERVICE, partition)

    assert updated.backend_reference == ingested.backend_reference
    assert other.backend_reference != ingested.backend_reference
    assert queried.evidence
    assert deleted.record_id == "doc-1"
    assert state.find_record(ingested.backend_reference.value) is None
    assert state.find_record(other.backend_reference.value) == (
        partition.value,
        "source-other",
        "doc-1",
    )


class _RebindingRuntime(FakeCogneeRuntime):
    async def remember(self, record, binding, user):
        result = await super().remember(record, binding, user)
        return _NativeIngestion(str(uuid4()), result.data_id, True)


async def test_adapter_refuses_to_move_a_bound_partition(record_factory):
    backend = CogneeBackend(_RebindingRuntime(), resolve_user)
    partition = AccessPartitionRef("prt_bound")
    await backend.ingest(record_factory("doc-1", "1", "a"), SERVICE, partition)

    with pytest.raises(BackendError) as error:
        await backend.ingest(record_factory("doc-2", "1", "b"), SERVICE, partition)

    assert error.value.code == BackendErrorCode.PARTIAL_WRITE


@dataclass
class _NativeUser:
    id: str
    handle: str


class _IdentityRuntime:
    def __init__(self):
        self.users: dict[str, _NativeUser] = {}
        self.created: list[str] = []

    async def ensure_user(self, handle):
        for user in self.users.values():
            if user.handle == handle:
                return user
        user = _NativeUser(str(uuid4()), handle)
        self.users[user.id] = user
        self.created.append(handle)
        return user

    async def get_user(self, native_id):
        return self.users[native_id]


async def test_identity_resolver_maps_each_principal_once_and_durably(tmp_path):
    state = _sqlite_state(tmp_path)
    runtime = _IdentityRuntime()
    alpha = PrincipalContext("prn_alpha", "trace")
    beta = PrincipalContext("prn_beta", "trace")

    first = await CogneeIdentityResolver(runtime, state)(alpha)
    after_restart = await CogneeIdentityResolver(runtime, SqliteBackendState(state.database))(alpha)
    other = await CogneeIdentityResolver(runtime, state)(beta)

    assert first.id == after_restart.id != other.id
    assert len(runtime.created) == 2
    assert "prn_alpha" not in first.handle and first.handle.endswith("@context-engine.invalid")
    assert state.get_identity("prn_alpha") == first.id


class _RacingState(InMemoryBackendState):
    """Another resolver claims the principal between our lookup and our claim."""

    def claim_identity(self, principal_id, identity):
        return super().claim_identity(principal_id, "native-claimed-first")


async def test_identity_resolver_uses_the_identity_claimed_first():
    runtime = _IdentityRuntime()
    runtime.users["native-claimed-first"] = _NativeUser("native-claimed-first", "earlier")

    user = await CogneeIdentityResolver(runtime, _RacingState())(PrincipalContext("prn_a", "t"))

    assert user.id == "native-claimed-first"

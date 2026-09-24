"""Ledger partition assignment from mapped record audiences."""

from __future__ import annotations

import hashlib
from itertools import count
from pathlib import Path

import pytest

from context_engine.domain import JobOperation, VersionOrdering, partition_key
from context_engine.persistence import ControlDatabase, ControlPlaneRepository, SourceRepository

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
OPERATIONS = {
    "upsert": JobOperation.INGESTION,
    "acl_changed": JobOperation.UPDATE,
    "delete": JobOperation.DELETION,
}


def _ledger(tmp_path):
    database = ControlDatabase(tmp_path / "control.db", MIGRATIONS)
    database.migrate()
    repository = ControlPlaneRepository(database)
    sources = SourceRepository(database)
    space = repository.create_space("Ops", None)
    source = sources.create_source(
        space.id, "Files", "file", VersionOrdering.NUMERIC, {"t": "team", "u": "ops"}
    )
    sequence = count()

    def event(record_id, version, operation="upsert", audience=("t",), acl_version="1"):
        key = f"k-{next(sequence):06d}"
        payload = {
            "schemaVersion": "1",
            "spaceId": space.id,
            "sourceId": source.id,
            "sourceRecordId": record_id,
            "sourceVersion": version,
            "operation": operation,
            "sourceObservedAt": "2026-09-24T10:00:00Z",
            "audience": list(audience),
            "sourceAclVersion": acl_version,
            "idempotencyKey": key,
        }
        if operation == "upsert":
            digest = hashlib.sha256(f"{record_id}:{version}".encode()).hexdigest()
            payload["contentHash"] = f"sha256:{digest}"
        job, _ = repository.enqueue_job(OPERATIONS[operation], key, payload, "t", 3, "prn_c")
        sources.apply_record_event(job, source)
        return sources.get_record(space.id, source.id, record_id)

    return sources, space, event


def test_partition_key_is_order_independent_and_scoped_to_a_space():
    assert partition_key("spc_1", ("ops", "team")) == partition_key("spc_1", ("team", "ops", "ops"))
    assert partition_key("spc_1", ("team",)) != partition_key("spc_2", ("team",))
    assert partition_key("spc_1", ("team",)).startswith("prt_")
    with pytest.raises(ValueError):
        partition_key("spc_1", ())


def test_active_records_live_in_the_partition_of_their_mapped_audiences(tmp_path):
    sources, space, event = _ledger(tmp_path)
    both = partition_key(space.id, ("ops", "team"))
    team = partition_key(space.id, ("team",))

    first = event("doc-a", "1", audience=("t", "u"))
    second = event("doc-b", "1", audience=("u", "t"))
    narrow = event("doc-c", "1")
    quarantined = event("doc-q", "1", audience=("x",))
    moved = event("doc-a", "1", operation="acl_changed", audience=("t",), acl_version="2")
    deleted = event("doc-c", "2", operation="delete")

    assert first.partition_id == second.partition_id == both
    assert narrow.partition_id == team
    assert quarantined.partition_id is None
    # An ACL change points the record at its new partition; moving the content is later work.
    assert moved.partition_id == team
    assert deleted.partition_id is None
    assert {item.id: item.audiences for item in sources.list_partitions(space.id)} == {
        both: ("ops", "team"),
        team: ("team",),
    }
    assert sources.get_partition(team).space_id == space.id
    assert sources.get_partition("prt_missing") is None

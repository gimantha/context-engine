"""Deleting a space: every record removed and checked, every partition dropped, every row gone,
with the space refusing new work while the job runs and residue keeping it alive."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from test_api import ADMIN, MEMBER, MIGRATIONS, READER, SERVICE, _auth, _grant
from test_query_api import SERVICE_ID, _Stack

from context_engine.domain import JobOperation
from context_engine.knowledge_backend import (
    BackendError,
    BackendErrorCode,
    DeletionResult,
    DummyKnowledgeBackend,
)
from context_engine.observability import MetricsRegistry
from context_engine.persistence import (
    AuthorizationRepository,
    ControlDatabase,
    ControlPlaneRepository,
    SourceRepository,
    StagingStore,
)
from context_engine.security.authorization import Authorizer
from context_engine.security.identity import StaticTokenVerifier
from context_engine.worker import (
    JobAuthorizer,
    JobWorker,
    LifecycleJobHandler,
    OperationDispatcher,
    RecordIndexer,
    SpaceDeletionJobHandler,
)

# Every table that holds rows for a space, with the column that names it.
SPACE_TABLES = {
    "context_spaces": "id",
    "space_configurations": "space_id",
    "sources": "space_id",
    "source_records": "space_id",
    "record_versions": "space_id",
    "source_record_effects": "space_id",
    "access_partitions": "space_id",
    "record_locations": "space_id",
    "queries": "space_id",
    "evidence": "space_id",
}
SOURCE_TABLES = ("source_checkpoints", "staged_uploads", "sync_runs", "indexing_snapshots")
PARTITION_TABLES = ("backend_bindings", "backend_record_refs", "backend_read_access")


class _LeakingBackend(DummyKnowledgeBackend):
    """A backend whose removals claim success while the copies stay searchable."""

    def __init__(self):
        super().__init__()
        self.leak = True

    async def delete(self, reference, principal, partition):
        if self.leak:
            return DeletionResult(record_id="leaked", deleted=True)
        return await super().delete(reference, principal, partition)

    async def delete_partition(self, partition, principal):
        if not self.leak:
            await super().delete_partition(partition, principal)


class _FlakyBackend(DummyKnowledgeBackend):
    """A backend whose first partition removal fails with a retryable error."""

    def __init__(self):
        super().__init__()
        self.failures = 1
        self.removed = []

    async def delete_partition(self, partition, principal):
        if self.failures:
            self.failures -= 1
            raise BackendError(BackendErrorCode.UNAVAILABLE, "down", retryable=True)
        self.removed.append(partition.value)
        await super().delete_partition(partition, principal)


class _DeletionStack(_Stack):
    """The query stack with the space-deletion handler registered, in either worker mode."""

    def __init__(self, tmp_path, backend=None, *, provider=True):
        super().__init__(tmp_path, backend, provider=provider)
        self.provider = provider

    def worker(self):
        database = ControlDatabase(self.tmp_path / "control.db", MIGRATIONS)
        sources = SourceRepository(database)
        authorization = AuthorizationRepository(database)
        repository = ControlPlaneRepository(database)
        metrics = MetricsRegistry()
        staging = StagingStore(self.tmp_path / "staging")
        indexer = (
            RecordIndexer(sources, self.backend, staging, SERVICE_ID, metrics)
            if self.provider
            else None
        )
        lifecycle = LifecycleJobHandler(sources, indexer=indexer, staging=staging)
        deletion = SpaceDeletionJobHandler(
            sources,
            repository,
            staging,
            SERVICE_ID,
            metrics,
            backend=self.backend if self.provider else None,
            indexer=indexer,
        )
        return JobWorker(
            repository,
            OperationDispatcher(
                {
                    JobOperation.INGESTION: lifecycle,
                    JobOperation.UPDATE: lifecycle,
                    JobOperation.DELETION: lifecycle,
                    JobOperation.SPACE_DELETION: deletion,
                }
            ),
            metrics,
            JobAuthorizer(
                Authorizer(authorization, metrics),
                authorization,
                StaticTokenVerifier.from_file(self.tmp_path / "tokens.json"),
            ),
            lease_seconds=5,
        )

    def delete_space(self, space, token=ADMIN):
        return self.client.delete(f"/v1/spaces/{space['id']}", headers=_auth(token))

    def job(self, response, token=ADMIN):
        return self.client.get(response.json()["statusUrl"], headers=_auth(token))

    def space_status(self, space, token=ADMIN):
        return self.client.get(f"/v1/spaces/{space['id']}", headers=_auth(token)).status_code

    def rows(self, space, source):
        """Count every row in the control database that still describes the space."""

        connection = sqlite3.connect(self.tmp_path / "control.db")
        try:
            count = 0
            for table, column in SPACE_TABLES.items():
                count += connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (space["id"],)
                ).fetchone()[0]
            for table in SOURCE_TABLES:
                count += connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE source_id = ?", (source["id"],)
                ).fetchone()[0]
            for table in PARTITION_TABLES:
                count += connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE partition_id LIKE ?",
                    (f"%{space['id']}%",),
                ).fetchone()[0]
            count += connection.execute(
                "SELECT COUNT(*) FROM grants WHERE resource_id IN (?, ?)",
                (space["id"], source["id"]),
            ).fetchone()[0]
            # Each test database holds one space, so every link belongs to it.
            count += connection.execute("SELECT COUNT(*) FROM query_evidence").fetchone()[0]
            count += connection.execute("SELECT COUNT(*) FROM query_answers").fetchone()[0]
            return count
        finally:
            connection.close()

    def staged_files(self):
        return [path for path in (self.tmp_path / "staging").rglob("*") if path.is_file()]


def _populate(stack):
    """Two indexed records in two partitions, a configuration, and the reader's grant."""

    space, source = stack.setup()
    stack.deliver(space, source, "doc-a", "1", content=b"gateway rollback runbook")
    stack.deliver(
        space, source, "doc-b", "1", content=b"ops escalation", audience=("source-group:ops",)
    )
    configured = stack.client.put(
        f"/v1/spaces/{space['id']}/configuration",
        headers=_auth(ADMIN),
        json={"llm": {"provider": "openai", "model": "gpt-5", "apiKeyRef": "env:TEST_MODEL_KEY"}},
    )
    assert configured.status_code == 200, configured.text
    return space, source


async def test_deleting_a_space_removes_content_rows_bytes_and_grants(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_MODEL_KEY", "sk-test")
    stack = _DeletionStack(tmp_path)
    with stack.client:
        space, source = _populate(stack)
        await stack.drain()
        assert stack.rows(space, source) > 0 and stack.staged_files()
        found = stack.query(MEMBER, space, "rollback")
        assert found.status_code == 200 and found.json()["evidence"], found.text
        answered = stack.query(MEMBER, space, "rollback", mode="answer")
        assert answered.status_code == 200 and answered.json()["answer"], answered.text

        accepted = stack.delete_space(space)
        assert accepted.status_code == 202, accepted.text
        assert accepted.headers["Location"] == accepted.json()["statusUrl"]
        marked = stack.client.get(f"/v1/spaces/{space['id']}", headers=_auth(ADMIN))
        assert marked.json()["state"] == "deleting"

        # While the job runs, the space takes no new work and the request replays the job.
        again = stack.delete_space(space)
        assert again.status_code == 202 and again.json()["jobId"] == accepted.json()["jobId"]
        refused_query = stack.query(MEMBER, space, "rollback")
        refused_source = stack.client.post(
            f"/v1/spaces/{space['id']}/sources",
            headers=_auth(ADMIN),
            json={"name": "Late", "type": "file", "audienceMapping": {}},
        )
        refused_configuration = stack.client.put(
            f"/v1/spaces/{space['id']}/configuration",
            headers=_auth(ADMIN),
            json={"llm": {"provider": "openai", "model": "gpt-5", "apiKey": "sk-late"}},
        )
        refused_enrichment = stack.client.post(
            f"/v1/spaces/{space['id']}/enrichments",
            headers=_auth(ADMIN, **{"Idempotency-Key": "enrich-late"}),
        )
        assert refused_query.status_code == 409 and refused_source.status_code == 409
        assert refused_configuration.status_code == 409 and refused_enrichment.status_code == 409

        await stack.drain()

        job = stack.job(accepted).json()
        assert job["state"] == "succeeded", job
        assert stack.space_status(space) == 404
        assert stack.rows(space, source) == 0
        assert stack.staged_files() == []
        assert stack.backend._records == {} and stack.backend._readers == {}
        gone = stack.query(MEMBER, space, "rollback")
        assert gone.status_code == 404


async def test_a_late_delivery_to_a_deleting_space_is_refused(tmp_path):
    stack = _DeletionStack(tmp_path)
    with stack.client:
        space, source = stack.setup()
        stack.deliver(space, source, "doc-a", "1", content=b"first")
        await stack.drain()
        assert stack.delete_space(space).status_code == 202

        late = stack.client.post(
            "/v1/ingestions",
            headers=_auth(SERVICE, **{"Idempotency-Key": "evt-late"}),
            json={
                "schemaVersion": "1",
                "spaceId": space["id"],
                "sourceId": source["id"],
                "sourceRecordId": "doc-c",
                "sourceVersion": "1",
                "sourceObservedAt": "2026-10-01T00:00:00Z",
                "operation": "delete",
                "audience": ["source-group:research"],
                "sourceAclVersion": "1",
                "idempotencyKey": "evt-late",
            },
        )
        assert late.status_code == 409, late.text
        await stack.drain()
        assert stack.space_status(space) == 404


async def test_ledger_only_deletion_removes_rows_and_bytes(tmp_path):
    stack = _DeletionStack(tmp_path, provider=False)
    with stack.client:
        space, source = stack.setup()
        stack.deliver(space, source, "doc-a", "1", content=b"ledger only")
        await stack.drain()
        assert stack.staged_files()

        accepted = stack.delete_space(space)
        await stack.drain()

        assert stack.job(accepted).json()["state"] == "succeeded"
        assert stack.rows(space, source) == 0 and stack.staged_files() == []


async def test_residue_keeps_the_space_until_a_retry_succeeds(tmp_path):
    backend = _LeakingBackend()
    stack = _DeletionStack(tmp_path, backend)
    with stack.client:
        space, source = stack.setup()
        stack.deliver(space, source, "doc-a", "1", content=b"sticky content")
        await stack.drain()

        first = stack.delete_space(space)
        await stack.drain()

        failed = stack.job(first).json()
        assert failed["state"] == "failed" and failed["error"]["code"] == "residue_found"
        still_there = stack.client.get(f"/v1/spaces/{space['id']}", headers=_auth(ADMIN))
        assert still_there.status_code == 200 and still_there.json()["state"] == "deleting"
        assert stack.rows(space, source) > 0, "rows stay while a copy may still be searchable"
        assert any(stack.backend._records.values())

        # After a failed job the request queues a new attempt rather than replaying the failure.
        backend.leak = False
        second = stack.delete_space(space)
        assert second.status_code == 202 and second.json()["jobId"] != first.json()["jobId"]
        await stack.drain()

        assert stack.job(second).json()["state"] == "succeeded"
        assert stack.rows(space, source) == 0 and stack.backend._records == {}


async def test_a_transient_backend_failure_is_retried_without_losing_progress(tmp_path):
    backend = _FlakyBackend()
    stack = _DeletionStack(tmp_path, backend)
    with stack.client:
        space, source = stack.setup()
        stack.deliver(space, source, "doc-a", "1", content=b"retry me")
        await stack.drain()

        accepted = stack.delete_space(space)
        worker = stack.worker()
        assert await worker.run_once(), "the first attempt runs and fails on the partition"
        pending = stack.job(accepted).json()
        assert pending["state"] == "queued" and pending["attemptCount"] == 1, pending
        assert stack.space_status(space) == 200

        later = datetime.now(UTC) + timedelta(minutes=5)
        while await worker.run_once(now=later):
            pass

        assert stack.job(accepted).json()["state"] == "succeeded"
        assert backend.removed and stack.rows(space, source) == 0


def test_deletion_needs_space_manage(tmp_path):
    stack = _DeletionStack(tmp_path)
    with stack.client:
        space, _ = stack.setup()
        assert stack.delete_space(space, READER).status_code == 403
        assert stack.delete_space(space, MEMBER).status_code == 403
        assert stack.delete_space({"id": "spc_missing"}).status_code == 404
        _grant(stack.client, space["id"], "mgr", {"group": "readers", "actions": ["space.manage"]})
        assert stack.delete_space(space, MEMBER).status_code == 202

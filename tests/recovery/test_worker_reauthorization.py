"""A permission change between enqueue and execution must stop the job (threat T06)."""

from __future__ import annotations

from pathlib import Path

import pytest

from context_engine.domain import Action, JobOperation, JobState, PrincipalKind, VersionOrdering
from context_engine.observability import MetricsRegistry
from context_engine.persistence import (
    AuthorizationRepository,
    ControlDatabase,
    ControlPlaneRepository,
    SourceRepository,
)
from context_engine.security.authorization import Authorizer
from context_engine.worker import JobAuthorizer, JobWorker, LifecycleJobHandler

MIGRATIONS = Path(__file__).resolve().parents[2] / "engine/migrations"


class _Groups:
    def __init__(self, groups: frozenset[str]) -> None:
        self.groups = groups

    def groups_for(self, issuer: str, subject: str) -> frozenset[str]:
        return self.groups


def _setup(tmp_path):
    database = ControlDatabase(tmp_path / "control.db", MIGRATIONS)
    database.migrate()
    repository = ControlPlaneRepository(database)
    authorization = AuthorizationRepository(database)
    space = repository.create_space("Ops", None)
    source = SourceRepository(database).create_source(
        space.id, "Files", "file", VersionOrdering.NUMERIC, {"t": "team"}
    )
    principal = authorization.resolve_principal("iss", "connector", PrincipalKind.SERVICE, None)
    payload = {
        "spaceId": space.id,
        "sourceId": source.id,
        "sourceRecordId": "record-1",
        "sourceVersion": "1",
        "operation": "upsert",
        "sourceAclVersion": "1",
        "audience": ["t"],
        "contentHash": "sha256:" + "a" * 64,
    }
    return repository, authorization, source, principal, payload


def _worker(repository, authorization, groups: frozenset[str]):
    metrics = MetricsRegistry()
    worker = JobWorker(
        repository,
        LifecycleJobHandler(SourceRepository(repository.database)),
        metrics,
        JobAuthorizer(Authorizer(authorization, metrics), authorization, _Groups(groups)),
        lease_seconds=5,
    )
    return worker, metrics


@pytest.mark.asyncio
async def test_revoked_grant_fails_job_without_applying_its_effect(tmp_path):
    repository, authorization, source, principal, payload = _setup(tmp_path)
    authorization.put_grant(
        source.id, "svc", frozenset({Action.INGEST_WRITE}), principal.id, None, "t"
    )
    job, _ = repository.enqueue_job(
        JobOperation.INGESTION, "k:record-1:1", payload, "trace-revoke", 3, principal.id
    )
    assert authorization.delete_grant(source.id, "svc")
    worker, metrics = _worker(repository, authorization, frozenset())

    assert await worker.run_once()

    failed = repository.get_job(job.id)
    assert failed.state is JobState.FAILED
    assert failed.error_code == "authorization_revoked"
    assert failed.result is None
    assert failed.lease_token is None
    assert repository.count_source_effects() == 0
    assert metrics.snapshot()["context_engine_jobs_denied_total"] == 1
    assert authorization.count_decisions("no_grants") == 1
    # A terminal denial is not runnable again.
    assert not await worker.run_once()


@pytest.mark.asyncio
async def test_lost_group_membership_denies_job_and_current_membership_allows_it(tmp_path):
    repository, authorization, source, principal, payload = _setup(tmp_path)
    authorization.put_grant(
        source.id, "writers", frozenset({Action.INGEST_WRITE}), None, "writers", "t"
    )
    denied_job, _ = repository.enqueue_job(
        JobOperation.INGESTION, "k:record-1:1", payload, "trace-a", 3, principal.id
    )
    worker, _ = _worker(repository, authorization, frozenset())
    assert await worker.run_once()
    assert repository.get_job(denied_job.id).error_code == "authorization_revoked"

    allowed_job, _ = repository.enqueue_job(
        JobOperation.INGESTION,
        "k:record-2:1",
        {**payload, "sourceRecordId": "record-2"},
        "trace-b",
        3,
        principal.id,
    )
    worker, _ = _worker(repository, authorization, frozenset({"writers"}))
    assert await worker.run_once()
    assert repository.get_job(allowed_job.id).state is JobState.SUCCEEDED
    assert repository.count_source_effects() == 1


@pytest.mark.asyncio
async def test_job_without_principal_is_denied(tmp_path):
    repository, authorization, source, principal, payload = _setup(tmp_path)
    job, _ = repository.enqueue_job(
        JobOperation.INGESTION, "k:record-1:1", payload, "trace-c", 3, principal.id
    )
    # Simulate a legacy row accepted before identity existed.
    with repository.database.transaction() as connection:
        connection.execute("UPDATE jobs SET principal_id = NULL WHERE id = ?", (job.id,))
    worker, _ = _worker(repository, authorization, frozenset())

    assert await worker.run_once()
    assert repository.get_job(job.id).error_code == "authorization_revoked"
    assert repository.count_source_effects() == 0

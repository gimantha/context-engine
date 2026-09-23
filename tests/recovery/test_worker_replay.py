"""Worker crash-replay and idempotent-effect recovery test."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
from context_engine.worker import InjectedWorkerCrash, JobAuthorizer, JobWorker, LifecycleJobHandler

MIGRATIONS = Path(__file__).resolve().parents[2] / "engine/migrations"


class _NoGroups:
    def groups_for(self, issuer: str, subject: str) -> frozenset[str]:
        return frozenset()


@pytest.mark.asyncio
async def test_replay_after_crash_is_idempotent_and_never_reports_false_completion(tmp_path):
    database = ControlDatabase(tmp_path / "control.db", MIGRATIONS)
    database.migrate()
    repository = ControlPlaneRepository(database)
    authorization = AuthorizationRepository(database)
    sources = SourceRepository(database)
    space = repository.create_space("Ops", None)
    source = sources.create_source(
        space.id, "Files", "file", VersionOrdering.NUMERIC, {"t": "team"}
    )
    principal = authorization.resolve_principal("iss", "connector", PrincipalKind.SERVICE, None)
    authorization.put_grant(
        source.id, "svc", frozenset({Action.INGEST_WRITE}), principal.id, None, "t"
    )
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
    accepted, _ = repository.enqueue_job(
        JobOperation.INGESTION, "source-1:record-1:1", payload, "trace-recovery", 3, principal.id
    )
    metrics = MetricsRegistry()
    worker = JobWorker(
        repository,
        LifecycleJobHandler(sources, crash_after_effect_once=True),
        metrics,
        JobAuthorizer(Authorizer(authorization, metrics), authorization, _NoGroups()),
        lease_seconds=1,
    )
    first_attempt = datetime.now(UTC)

    with pytest.raises(InjectedWorkerCrash):
        await worker.run_once(first_attempt)

    interrupted = repository.get_job(accepted.id)
    assert interrupted.state is JobState.RUNNING
    assert interrupted.result is None
    assert repository.count_source_effects() == 1

    assert await worker.run_once(first_attempt + timedelta(seconds=2))
    completed = repository.get_job(accepted.id)
    assert completed.state is JobState.SUCCEEDED
    assert completed.attempt_count == 2
    assert completed.result["outcome"] == "replayed"
    assert completed.result["state"] == "active"
    assert repository.count_source_effects() == 1
    assert sources.get_record(space.id, source.id, "record-1").state.value == "active"

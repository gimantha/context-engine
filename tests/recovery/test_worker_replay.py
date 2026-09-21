"""Worker crash-replay and idempotent-effect recovery test."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from context_engine.domain import JobOperation, JobState
from context_engine.observability import MetricsRegistry
from context_engine.persistence import ControlDatabase, ControlPlaneRepository
from context_engine.worker import InjectedWorkerCrash, JobWorker, LedgerJobHandler

MIGRATIONS = Path(__file__).resolve().parents[2] / "engine/migrations"


@pytest.mark.asyncio
async def test_replay_after_crash_is_idempotent_and_never_reports_false_completion(tmp_path):
    database = ControlDatabase(tmp_path / "control.db", MIGRATIONS)
    database.migrate()
    repository = ControlPlaneRepository(database)
    payload = {
        "spaceId": "space-1",
        "sourceId": "source-1",
        "sourceRecordId": "record-1",
        "sourceVersion": "1",
        "operation": "upsert",
    }
    accepted, _ = repository.enqueue_job(
        JobOperation.INGESTION, "source-1:record-1:1", payload, "trace-recovery", 3
    )
    metrics = MetricsRegistry()
    worker = JobWorker(
        repository,
        LedgerJobHandler(repository, crash_after_effect_once=True),
        metrics,
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
    assert completed.result["effectCreated"] is False
    assert repository.count_source_effects() == 1

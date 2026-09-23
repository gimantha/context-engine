"""SQLite repository for resources, transactional outbox, jobs, and effects."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from sqlite3 import Connection, Row
from typing import Any
from uuid import uuid4

from context_engine.domain import ContextSpace, Job, JobOperation, JobState, SpaceState

from .database import ControlDatabase


class IdempotencyConflict(Exception):
    """Indicate reuse of an idempotency key with a different payload."""


class EffectConflict(Exception):
    """Indicate conflicting durable effects for one source-record version."""


def _timestamp(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat()


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _space(row: Row) -> ContextSpace:
    return ContextSpace(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        state=SpaceState(row["state"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _job(row: Row) -> Job:
    return Job(
        id=row["id"],
        operation=JobOperation(row["operation"]),
        state=JobState(row["state"]),
        idempotency_key=row["idempotency_key"],
        payload=json.loads(row["payload_json"]),
        payload_hash=row["payload_hash"],
        trace_id=row["trace_id"],
        principal_id=row["principal_id"],
        attempt_count=row["attempt_count"],
        max_attempts=row["max_attempts"],
        next_attempt_at=_datetime(row["next_attempt_at"]),
        lease_token=row["lease_token"],
        lease_expires_at=_datetime(row["lease_expires_at"]),
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        error_code=row["error_code"],
        error_message=row["error_message"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def insert_source_effect(connection: Connection, job: Job) -> bool:
    """Insert the idempotent effect row for a job inside the caller's transaction.

    Returns True when the effect is new and False for an exact replay of the same idempotency
    key. Raises `EffectConflict` when that key was already applied with a different payload.
    """

    payload = job.payload
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO source_record_effects(
            idempotency_key, space_id, source_id, source_record_id,
            source_version, operation, payload_hash, applied_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job.idempotency_key,
            payload["spaceId"],
            payload["sourceId"],
            payload["sourceRecordId"],
            payload["sourceVersion"],
            payload["operation"],
            job.payload_hash,
            _timestamp(),
        ),
    )
    if cursor.rowcount == 1:
        return True
    # The same idempotency key must carry the same payload; anything else is a conflict.
    existing = connection.execute(
        "SELECT payload_hash FROM source_record_effects WHERE idempotency_key = ?",
        (job.idempotency_key,),
    ).fetchone()
    if existing and existing["payload_hash"] != job.payload_hash:
        raise EffectConflict
    return False


class ControlPlaneRepository:
    """Persist spaces and coordinate durable asynchronous job execution."""

    def __init__(self, database: ControlDatabase) -> None:
        self.database = database

    def create_space(self, name: str, description: str | None) -> ContextSpace:
        """Create a ready context space and return its domain model."""

        now = _timestamp()
        space_id = f"spc_{uuid4().hex}"
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO context_spaces(id, name, description, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (space_id, name, description, SpaceState.READY.value, now, now),
            )
            row = connection.execute(
                "SELECT * FROM context_spaces WHERE id = ?", (space_id,)
            ).fetchone()
        return _space(row)

    def list_spaces(self) -> tuple[ContextSpace, ...]:
        """Return all context spaces in deterministic creation order."""

        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM context_spaces ORDER BY created_at, id"
            ).fetchall()
        return tuple(_space(row) for row in rows)

    def get_space(self, space_id: str) -> ContextSpace | None:
        """Return a context space by engine ID when it exists."""

        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM context_spaces WHERE id = ?", (space_id,)
            ).fetchone()
        return _space(row) if row else None

    def enqueue_job(
        self,
        operation: JobOperation,
        idempotency_key: str,
        payload: dict[str, Any],
        trace_id: str,
        max_attempts: int,
        principal_id: str,
    ) -> tuple[Job, bool]:
        """Atomically insert an idempotent job and matching outbox event.

        The boolean result is true for a new job and false for an exact replay. The accepting
        principal is stored so the worker can reauthorize the job when it executes.
        """

        payload_json = _canonical(payload)
        payload_hash = _digest(payload_json)
        now = _timestamp()
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE operation = ? AND idempotency_key = ?",
                (operation.value, idempotency_key),
            ).fetchone()
            if existing:
                if existing["payload_hash"] != payload_hash:
                    raise IdempotencyConflict
                return _job(existing), False

            # The job and its outbox event share this transaction, so accepted work cannot be lost.
            job_id = f"job_{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO jobs(
                    id, operation, state, idempotency_key, payload_json, payload_hash,
                    trace_id, principal_id, max_attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    operation.value,
                    JobState.ACCEPTED.value,
                    idempotency_key,
                    payload_json,
                    payload_hash,
                    trace_id,
                    principal_id,
                    max_attempts,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO outbox_events(
                    id, aggregate_id, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (f"evt_{uuid4().hex}", job_id, "job.accepted", payload_json, now),
            )
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job(row), True

    def get_job(self, job_id: str) -> Job | None:
        """Return a durable job by engine ID when it exists."""

        with self.database.connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job(row) if row else None

    def dispatch_outbox(self, limit: int = 100) -> int:
        """Move pending outbox jobs into the local runnable queue."""

        now = _timestamp()
        with self.database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT id, aggregate_id FROM outbox_events
                WHERE dispatched_at IS NULL
                ORDER BY created_at, id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            for row in rows:
                # SQLite is the M1 queue; publishing atomically makes the job runnable.
                connection.execute(
                    """
                    UPDATE jobs SET state = ?, updated_at = ?
                    WHERE id = ? AND state = ?
                    """,
                    (
                        JobState.QUEUED.value,
                        now,
                        row["aggregate_id"],
                        JobState.ACCEPTED.value,
                    ),
                )
                connection.execute(
                    "UPDATE outbox_events SET dispatched_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
        return len(rows)

    def claim_job(self, lease_seconds: int, now: datetime | None = None) -> Job | None:
        """Claim the next runnable job with a unique expiring lease."""

        instant = now or datetime.now(UTC)
        timestamp = _timestamp(instant)
        expires_at = _timestamp(instant + timedelta(seconds=lease_seconds))
        token = uuid4().hex
        with self.database.transaction() as connection:
            # A worker that dies on its last permitted attempt must not leave a permanent lease.
            connection.execute(
                """
                UPDATE jobs
                SET state = ?, error_code = ?, error_message = ?, updated_at = ?
                WHERE state = ? AND lease_expires_at <= ? AND attempt_count >= max_attempts
                """,
                (
                    JobState.FAILED.value,
                    "attempts_exhausted",
                    "Job attempts exhausted after worker interruption",
                    timestamp,
                    JobState.RUNNING.value,
                    timestamp,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE attempt_count < max_attempts AND (
                    (state IN (?, ?) AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                    OR (state = ? AND lease_expires_at <= ?)
                )
                ORDER BY created_at, id
                LIMIT 1
                """,
                (
                    JobState.QUEUED.value,
                    JobState.RETRY_WAIT.value,
                    timestamp,
                    JobState.RUNNING.value,
                    timestamp,
                ),
            ).fetchone()
            if row is None:
                return None
            # Reclaiming an expired job replaces the token, preventing its old worker from winning.
            connection.execute(
                """
                UPDATE jobs
                SET state = ?, attempt_count = attempt_count + 1,
                    lease_token = ?, lease_expires_at = ?, next_attempt_at = NULL,
                    error_code = NULL, error_message = NULL, updated_at = ?
                WHERE id = ?
                """,
                (JobState.RUNNING.value, token, expires_at, timestamp, row["id"]),
            )
            claimed = connection.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        return _job(claimed)

    def complete_job(self, job: Job, result: dict[str, Any]) -> bool:
        """Complete a running job only when its lease token is current."""

        now = _timestamp()
        result_json = _canonical(result)
        with self.database.transaction() as connection:
            # The lease token acts as a compare-and-set guard against stale worker completion.
            cursor = connection.execute(
                """
                UPDATE jobs
                SET state = ?, result_json = ?, lease_token = NULL, lease_expires_at = NULL,
                    error_code = NULL, error_message = NULL, updated_at = ?
                WHERE id = ? AND state = ? AND lease_token = ?
                """,
                (
                    JobState.SUCCEEDED.value,
                    result_json,
                    now,
                    job.id,
                    JobState.RUNNING.value,
                    job.lease_token,
                ),
            )
        return cursor.rowcount == 1

    def retry_or_fail_job(
        self,
        job: Job,
        error_code: str,
        error_message: str,
        retry_at: datetime,
    ) -> JobState:
        """Schedule a retry or terminally fail a leased job."""

        final = job.attempt_count >= job.max_attempts
        state = JobState.FAILED if final else JobState.RETRY_WAIT
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET state = ?, next_attempt_at = ?, lease_token = NULL, lease_expires_at = NULL,
                    error_code = ?, error_message = ?, updated_at = ?
                WHERE id = ? AND state = ? AND lease_token = ?
                """,
                (
                    state.value,
                    None if final else _timestamp(retry_at),
                    error_code,
                    error_message,
                    _timestamp(),
                    job.id,
                    JobState.RUNNING.value,
                    job.lease_token,
                ),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("Job lease was lost before recording failure")
        return state

    def fail_job(self, job: Job, error_code: str, error_message: str) -> bool:
        """Terminally fail a leased job regardless of remaining attempts.

        Used when the job's authorization no longer holds; retrying cannot make it valid.
        """

        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET state = ?, next_attempt_at = NULL, lease_token = NULL, lease_expires_at = NULL,
                    error_code = ?, error_message = ?, updated_at = ?
                WHERE id = ? AND state = ? AND lease_token = ?
                """,
                (
                    JobState.FAILED.value,
                    error_code,
                    error_message,
                    _timestamp(),
                    job.id,
                    JobState.RUNNING.value,
                    job.lease_token,
                ),
            )
        return cursor.rowcount == 1

    def record_source_effect(self, job: Job) -> bool:
        """Record one idempotent source-record effect for crash recovery."""

        with self.database.transaction() as connection:
            return insert_source_effect(connection, job)

    def list_source_jobs(self, source_id: str, state: JobState | None = None) -> tuple[Job, ...]:
        """Return jobs delivered for one source, newest first, optionally filtered by state."""

        clause = "json_extract(payload_json, '$.sourceId') = ?"
        parameters: list[str] = [source_id]
        if state is not None:
            clause += " AND state = ?"
            parameters.append(state.value)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM jobs WHERE {clause} ORDER BY created_at DESC, id LIMIT 200",
                parameters,
            ).fetchall()
        return tuple(_job(row) for row in rows)

    def count_source_effects(self) -> int:
        """Return the number of durable source-record effects."""

        with self.database.connection() as connection:
            return connection.execute("SELECT COUNT(*) FROM source_record_effects").fetchone()[0]

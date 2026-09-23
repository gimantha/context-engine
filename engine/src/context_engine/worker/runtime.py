"""Leased worker execution with retry and crash recovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from context_engine.domain import JobState
from context_engine.observability import MetricsRegistry, get_logger, log_event
from context_engine.persistence import ControlPlaneRepository, EffectConflict

from .handler import InjectedWorkerCrash, JobHandler, TerminalJobError
from .reauthorize import JobAuthorizer

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Calculate bounded exponential retry delays."""

    initial_delay_seconds: float = 1.0
    maximum_delay_seconds: float = 60.0

    def delay(self, attempt: int) -> float:
        """Return the retry delay for a one-based attempt number."""

        return min(self.maximum_delay_seconds, self.initial_delay_seconds * (2 ** (attempt - 1)))


class JobWorker:
    """Dispatch, lease, execute, and finalize durable jobs."""

    def __init__(
        self,
        repository: ControlPlaneRepository,
        handler: JobHandler,
        metrics: MetricsRegistry,
        job_authorizer: JobAuthorizer,
        *,
        lease_seconds: int = 30,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._repository = repository
        self._handler = handler
        self._metrics = metrics
        self._job_authorizer = job_authorizer
        self._lease_seconds = lease_seconds
        self._retry_policy = retry_policy or RetryPolicy()

    async def run_once(self, now: datetime | None = None) -> bool:
        """Process at most one available job and report whether one ran."""

        self._repository.dispatch_outbox()
        job = self._repository.claim_job(self._lease_seconds, now)
        if job is None:
            return False
        log_event(
            logger,
            "job_started",
            job_id=job.id,
            operation=job.operation.value,
            attempt=job.attempt_count,
            trace_id=job.trace_id,
        )
        self._metrics.increment("context_engine_jobs_started_total")
        # Permissions may have changed while the job waited; re-decide before any effect.
        decision = self._job_authorizer.authorize(job)
        if not decision.allowed:
            if not self._repository.fail_job(
                job, "authorization_revoked", "Job authorization is no longer valid"
            ):
                raise RuntimeError("Job lease was lost before recording denial")
            self._metrics.increment("context_engine_jobs_denied_total")
            log_event(
                logger,
                "job_denied",
                job_id=job.id,
                operation=job.operation.value,
                reason_code=decision.reason_code,
                trace_id=job.trace_id,
            )
            return True
        try:
            result = await self._handler.handle(job)
        except InjectedWorkerCrash:
            # Deliberately retain the running lease so recovery follows the real crash path.
            self._metrics.increment("context_engine_worker_interruptions_total")
            raise
        except TerminalJobError as exc:
            if not self._repository.fail_job(job, exc.code, exc.message):
                raise RuntimeError("Job lease was lost before recording failure") from exc
            self._metrics.increment("context_engine_jobs_failed_total")
            log_event(
                logger,
                "job_failed",
                job_id=job.id,
                operation=job.operation.value,
                error_code=exc.code,
                trace_id=job.trace_id,
            )
            return True
        except EffectConflict as exc:
            # A version delivered with different content cannot become valid by retrying.
            if not self._repository.fail_job(
                job, "effect_conflict", "Record effect conflicts with existing state"
            ):
                raise RuntimeError("Job lease was lost before recording failure") from exc
            self._metrics.increment("context_engine_jobs_failed_total")
            log_event(
                logger,
                "job_failed",
                job_id=job.id,
                operation=job.operation.value,
                error_code="effect_conflict",
                trace_id=job.trace_id,
            )
            return True
        except Exception as exc:
            delay = self._retry_policy.delay(job.attempt_count)
            state = self._repository.retry_or_fail_job(
                job,
                "worker_error",
                "Job execution failed",
                datetime.now(UTC) + timedelta(seconds=delay),
            )
            log_event(
                logger,
                "job_execution_failed",
                job_id=job.id,
                operation=job.operation.value,
                attempt=job.attempt_count,
                error_code=type(exc).__name__,
                trace_id=job.trace_id,
            )
            self._metrics.increment(
                "context_engine_jobs_failed_total"
                if state is JobState.FAILED
                else "context_engine_jobs_retried_total"
            )
            return True

        if not self._repository.complete_job(job, result):
            raise RuntimeError("Job lease was lost before completion")
        self._metrics.increment("context_engine_jobs_succeeded_total")
        log_event(
            logger,
            "job_succeeded",
            job_id=job.id,
            operation=job.operation.value,
            attempt=job.attempt_count,
            trace_id=job.trace_id,
        )
        return True

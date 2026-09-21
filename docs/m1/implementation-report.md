# M1 Runnable Engine and Durable Control Plane

**Status:** Implemented for the provider-neutral control plane

**Date:** 2026-09-18

**Release limitation:** Live-provider security verification from M0 remains open

## Delivered behavior

The public process is a FastAPI REST service under `engine/src/context_engine/api/`. Client applications call this API and do not access the knowledge-backend port. REST handlers use `ContextEngineService`, which operates on engine-owned domain values and a control-plane store.

The implemented REST slice provides:

- `GET /v1/health/live`
- `GET /v1/health/ready`
- `POST /v1/spaces`
- `GET /v1/spaces`
- `GET /v1/spaces/{spaceId}`
- `POST /v1/ingestions`
- `GET /v1/jobs/{jobId}`

The independent worker dispatches accepted jobs from the transactional outbox, claims them with an expiring lease, records durable attempts, and applies an idempotent source-record effect. Provider execution is intentionally absent from this M1 handler and belongs to the later provider-backed worker pipeline.

## Persistence and failure model

Migration `engine/migrations/0001_control_plane.sql` creates context spaces, jobs, outbox events, and the source-record effect ledger. Job and outbox creation occur in one SQLite transaction. A dispatch transaction changes an accepted job to queued while marking its outbox event delivered.

Internal job states are `accepted`, `queued`, `running`, `retry_wait`, `succeeded`, and `failed`. Public responses map `accepted` and `retry_wait` to `queued`. A worker claim records a unique lease token, expiry, and attempt count. Only the current lease holder can complete or reschedule a job.

The retry policy uses bounded exponential delay. A process interruption leaves the job in `running`; another worker can reclaim it only after the lease expires. Exhausted interrupted jobs become failed and cannot remain indefinitely runnable.

The recovery test injects a crash after the durable record effect commits but before the job completes. The first attempt remains `running` with no result. The retry observes the existing effect, does not create a duplicate, and then marks the job succeeded. This proves the M1 gate for the local control plane without claiming provider-side idempotency.

## Operational surface

- `context-engine-migrate` applies forward-only SQL migrations.
- `context-engine-api` runs the REST process; `--check` validates startup and migrations.
- `context-engine-worker` runs the worker; `--check` validates startup and `--once` handles at most one job.
- Every HTTP request receives an `X-Trace-Id`; accepted jobs retain that trace ID.
- Structured logs serialize only allowlisted operational fields and omit request bodies and credentials.
- Process-local counters cover HTTP requests, accepted/replayed jobs, attempts, retries, interruptions, failures, and successes.
- `/internal/metrics` exposes the safe counters in Prometheus text format and is excluded from the public API schema.

## Automated enforcement

The provider-boundary checker now rejects native provider imports in provider-neutral layers, rejects knowledge-backend imports in API and MCP packages, and rejects private terminology or identifiers in public contracts. CI validates formatting, lint, migrations, API startup, worker startup, OpenAPI and JSON schemas, architecture rules, and all non-live tests.

## Acceptance evidence

| M1 gate | Result | Evidence |
| --- | --- | --- |
| API and worker start independently | Pass | `test_entrypoints.py` and both `--check` commands |
| Database migrations run in CI | Pass | `context-engine-migrate` CI step and migration tests |
| OpenAPI validates in CI | Pass | Existing contract test in the complete test suite |
| Transaction/outbox acceptance is atomic | Pass | Control-plane repository and outbox test |
| Crash replay does not duplicate a record | Pass for M1 ledger | `tests/recovery/test_worker_replay.py` |
| Interrupted attempt does not report false completion | Pass | Recovery test observes `running` with no result before retry |
| Public interface does not import the backend | Pass | Architecture checker and generated API schema test |
| Logs and metrics avoid content and credentials | Pass by allowlisted design | Observability modules and API tests |

Validation performed on 2026-09-18:

```text
ruff format --check: passed
ruff check: passed
pytest -m "not live_provider": 25 passed, 1 provider-extra check skipped, 1 live test deselected
provider boundary check: passed
API startup check: passed
worker startup check: passed
bound HTTP liveness/readiness smoke test: passed
```

## Deferred work

M2 adds authentication, principal mapping, policy enforcement, grants, and authorization rechecks. M3 adds source registration, connector delivery, staging, checkpoints, and full ingestion lifecycle. M4 replaces the M1 ledger-only job effect with the provider-backed worker pipeline. The M0 live isolation and deletion gate must pass before the provider-backed path can be considered release-ready.

# ADR 0010: Source progress observability

**Status:** Accepted; indexing collection is enabled with provider-backed ingestion in M4

**Date:** 2026-09-24

## Context

Operators and connector owners need to see how far a source has progressed: whether its connector is still reading, how much delivered work the engine has applied, and how much of it the knowledge backend has indexed. The knowledge backend keeps processing status per native isolation unit, not per engine source, and some of its status calls are expensive. A progress bar polls continuously.

## Decision

- **A separate read-only API.** `GET /v1/progress/sources/{sourceId}` and `GET /v1/progress/spaces/{spaceId}` live in their own router. They never mutate state. Unlike the process-level `/internal/metrics` counters, they are scoped to a resource and authorized per caller.
- **Reading is a marker, not a percentage.** The connector opens a sync run when it starts reading a source and completes it when it has read everything. Opening a new run supersedes an unfinished one, so a crashed connector can always start again. No change to the ingestion envelope is needed.
- **Processing is measured by the engine.** The processing figure counts the source's deliveries since the latest run started, in the public job-state model. The percentage is finished deliveries, succeeded or failed, over all deliveries in that window.
- **Indexing is measured by the backend, off the request path.** The knowledge-backend port gains `indexing_progress`, which counts expected record versions as indexed, indexing, failed, or missing within explicit partitions. A worker collector asks the backend on its own schedule and stores an engine-owned snapshot per source. The API reads only the snapshot. The percentage is indexed over expected, where expected means the source's active records at their current versions.
- **The provider mapping attributes per-unit status to records.** The private adapter lists native items in each binding, matches them to the source through the engine metadata attached at ingestion, and gives a present item the state of its binding's latest processing run. Native identifiers, statuses, and pipeline names never leave the adapter.
- **Progress needs delivery or management rights.** Readers are not enough, because the counts reveal record volume across every audience, including records a reader cannot see.

## Consequences

- Indexing reports `not_collected` until M4 wires a knowledge backend into the worker and persists partition bindings.
- Backend status is per binding run, so a failure marks every present item in that binding as failed. Per-item failure detail is not available from the provider's public status API.
- Every source progress read records an access decision. Polling therefore adds audit rows; retention belongs with the audit work in M7.
- Space progress filters sources silently, like listing spaces, so it records no decisions.

## Granularity decision

Reviewed on 2026-09-24 after noting that the provider reports status per native isolation unit. The public unit stays the source.

- **No per-partition progress.** Exposing progress per partition would reveal how many reader groups a space has and how many records sit behind each, would name internal policy outputs callers cannot address, and would mix several sources' work in one figure.
- **M4 makes the ledger the source of truth for indexing.** Provider writes are synchronous per record, so the worker records each record version's indexing outcome in the ledger when the write returns. Per-source indexing is then exact per item.
- **The collector becomes a cross-check in M4.** It keeps reading the provider's status through `indexing_progress`. A record the ledger calls indexed but the provider reports missing, or inside an errored run, moves to reconcile-required.
- **Optional space-level figure.** Summing provider run progress across a space's partitions gives an exact provider-native number without naming partitions. If added, it belongs on the space progress route behind space management rights.
- **Per-partition detail stays in private adapter diagnostics.**

## Validation

- `engine/tests/test_progress_api.py`
- `engine/tests/test_indexing_collector.py`
- `engine/tests/test_cognee_adapter.py::test_adapter_attributes_native_processing_state_to_source_records`
- `engine/tests/test_backend_contract.py::test_indexing_progress_requires_explicit_scope`

## Revision (2026-09-24, M4 slice 2)

Indexing progress now comes from the ledger, as the granularity decision above anticipated. When the worker runs with the provider backend (`CONTEXT_ENGINE_KNOWLEDGE_BACKEND=provider`), each active record carries an index state (`pending`, `indexed`, `failed`, or `reconcile_required`), and the progress route counts them. With the backend disabled, indexing still reports `not_collected`. The collector snapshots remain in place for the cross-check planned in slice 3.

## Revision (2026-09-25, M4 slice 3)

The collector now runs in the worker as a cross-check every `CONTEXT_ENGINE_INDEXING_CHECK_SECONDS` (default 300). It asks the backend about every copy the ledger calls indexed, using the version whose content was actually written. That matters when a new version with identical content was confirmed without a rewrite and the provider's metadata still names the earlier version. When a source's counts disagree, it probes each copy and marks those that are missing or in a failed provider run as reconcile-required with `backend_mismatch`.

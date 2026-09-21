# ADR 0006: Authoritative ledger, idempotency and version ordering

**Status:** Accepted

**Date:** 2026-09-18

## Context

Provider writes span relational, vector and graph stores and cannot share one transaction with the engine control database. Source deliveries can be duplicated or arrive out of order.

## Decision

The engine control database is authoritative for accepted work, source versions, audience state, jobs and opaque backend references. The provider indexes are derived state.

An ingestion is unique by source, source record, version and idempotency key. An identical replay returns the existing result. The same version with different content is a conflict. An older version cannot replace a newer version or resurrect a deleted record.

M0 uses deterministic in-memory state to prove these semantics. M1 will persist them using a transactional outbox, job attempts and reconciliation.

## Consequences

- Provider success is not reported as durable engine success until ledger state is committed.
- Partial writes are recorded as reconcile-required, not silently retried as new work.
- Sources with non-numeric versions must define a comparison policy at registration.

## Validation

- Replay, update, conflict and cache invalidation in `engine/tests/test_backend_contract.py`.
- Ordered fields in `contracts/schemas/ingestion-event.schema.json`.

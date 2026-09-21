# ADR 0003: Source-audience routing and access partitions

**Status:** Accepted for the engine model; provider validation pending

**Date:** 2026-09-18

## Context

Records with incompatible readers must not contribute to the same searchable or derived context. Callers must not manipulate the storage isolation mechanism directly.

## Decision

The policy service maps a normalized, trusted source audience to an internal access partition. A partition represents one compatible reader set within a context space. It is not a public resource or grant target.

Before every backend operation, the application service supplies:

- A verified `PrincipalContext`.
- One target partition for a write/delete, or an explicit tuple of authorized partitions for a query/enrichment.

Unknown audiences enter quarantine. ACL restriction creates or selects the narrower partition and closes old read access before migration completes. Expansion requires an authorized policy change and a verified target binding.

## Consequences

- Query cache keys include principal and the exact sorted partition set.
- Partition identifiers cannot appear in API, MCP, UI, event or error payloads.
- A future sizing policy may merge only audiences proven equivalent.

## Validation

- `engine/src/context_engine/security/policy.py`
- `tests/isolation/test_two_audience.py`
- `tests/isolation/fixtures/two-audience.json`

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

## Revision (2026-09-24, M4 slice 1)

- **A partition's audiences are alternatives.** A principal may read a partition when it belongs to any of the partition's audiences, which matches how source systems share a file with several groups. The earlier rule required membership in every audience; it is replaced. Intersection rules ("members of both A and B") are not supported.
- **Engine audiences are principal groups.** Each source maps its trusted audience tags to engine audience names, and a principal belongs to an audience when that name is one of its groups.
- **Partitions are deterministic and durable.** The partition identifier is a hash of the space and the sorted, de-duplicated mapped audiences, so records with the same audiences always share one partition. The ledger stores each active record's partition and registers partitions in `access_partitions`. Quarantined and deleted records have no partition. An ACL change that changes the audiences points the record at its new partition; moving the content is a worker step.
- Validation: `engine/tests/test_policy.py::test_any_listed_audience_may_read_a_partition` and `engine/tests/test_ledger_partitions.py`.

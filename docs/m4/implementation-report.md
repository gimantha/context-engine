# M4 Knowledge-Backend Pipeline and Graph Lifecycle

**Status:** Slice 1 of 3 implemented; the live-provider gate is still not run

**Date:** 2026-09-24

**Release limitation:** Provider-backed execution is not wired into the worker yet, and the M0 live isolation, stale-artifact, and deletion checks remain unverified

## Plan

M4 is delivered in three slices, each leaving the full non-live suite passing.

1. **Foundations:** reader semantics, durable partitions, durable backend state, and the engine's service identity as owner of every provider unit. Delivered in this report.
2. **Provider writes:** text extraction, ingest, update, and delete through the port with a visibility barrier and residue checks, partition moves on ACL changes, per-principal read grants, per-record indexing state in the ledger, deletion of staged bytes, and reconcile-required handling.
3. **Reads:** a context-mode query endpoint with evidence translation, versioned enrichment jobs, and the indexing collector as a cross-check (ADR 0010).

## Slice 1 delivered

- **Any listed audience may read.** Policy now allows a partition when the principal belongs to any of its audiences (ADR 0003 revision). Engine audiences are principal group names.
- **Every active record has a partition.** The ledger computes a deterministic partition key from the space and the record's mapped audiences, registers it in `access_partitions`, and stores it on the record. Quarantined and deleted records have none. The indexing collector now reads the stored partition.
- **The adapter's state is durable.** A provider-neutral `BackendStateStore` protocol has an in-memory implementation for tests and a SQLite implementation for the engine. The private adapter keeps partition bindings, record references, and identities there instead of process memory. Record references are keyed by partition, source, and record, so two sources may use the same record identifier.
- **Bound partitions never move.** If the provider ever reports a different native unit for a partition that is already bound, the adapter raises a partial-write error instead of silently rebinding.
- **The service identity owns provider units.** A durable resolver maps each engine principal to its own ordinary native account under a deterministic handle. The live test now ingests as the service identity and grants read access per audience before checking isolation (ADR 0005 revision).
- **Pinned-SDK guard.** The signature check now covers the provider's user creation and lookup calls.

Migration `0005_partitions_and_backend_state.sql` adds `access_partitions`, `source_records.partition_id`, `backend_bindings`, `backend_record_refs`, and `backend_identities`.

## Gate status

| M4 gate item | Status | Evidence |
| --- | --- | --- |
| Live isolation, update, and deletion checks against real stores | **Not run** | Needs model and embedding credentials; the test is rewritten for the service-owner model |
| Records map to deterministic partitions | Pass | `test_ledger_partitions.py` |
| Bindings, references, and identities survive a restart | Pass | `test_backend_state.py` |
| No default provider identity | Pass | Resolver tests; every principal gets its own account |
| Query the ingested record through its context space | Slice 3 | |
| Enrichment creates traceable derived data | Slice 3 | |
| Replacing a record supersedes old answers | Slice 2 and live gate | |
| Deleting a record removes every searchable artifact | Slice 2 and live gate | |
| Swapping in a test backend needs no application change | Holds so far | Dummy backend passes the shared contract tests |
| No public payload exposes provider terms | Pass | Boundary check and contract tests |

Validation performed on 2026-09-24 from `engine/`:

```text
ruff format --check: passed
ruff check: passed
pytest -m "not live_provider": 73 passed, 1 provider-extra check skipped, 1 live test deselected
context-engine-api --check: passed
context-engine-worker --check: passed
context-engine-migrate: applied 5 migrations to a fresh database
provider boundary check: passed
```

## Running the live gate

Follow the README live-provider section with explicit model and embedding credentials. The test ingests three records as the service identity, grants read access to two reader identities, and checks cross-reader isolation, provider-side denial, update supersession, and post-delete absence. It does not yet inspect the raw text files and derived stores directly; that residue scan arrives with the slice 2 delete path.

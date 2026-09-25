# M4 Knowledge-Backend Pipeline and Graph Lifecycle

**Status:** All three slices implemented; the live-provider gate passed on 2026-09-25

**Date:** 2026-09-25

**Release limitation:** With the local embedded stores, the API and the worker must run in one process; separate processes cannot share the graph store (ADR 0002 revision). Provider search history keeps questions and passages after deletion (threat model T19), and live concurrency and partial-write injection have not been run

## Plan

M4 is delivered in three slices, each leaving the full non-live suite passing.

1. **Foundations:** reader semantics, durable partitions, durable backend state, and the engine's service identity as owner of every provider unit. Delivered in this report.
2. **Provider writes:** text extraction, ingest, update, and delete through the port with a visibility barrier and residue checks, partition moves on ACL changes, per-principal read grants, per-record indexing state in the ledger, deletion of staged bytes, and reconcile-required handling.
3. **Reads**, agreed on 2026-09-24:
   - A context-mode `POST /v1/queries` that checks `context.read`, resolves the caller's partitions with the any-audience rule, queries the backend as the caller, and translates results into engine evidence. Answer mode stays in M5.
   - A read-time visibility barrier: only records that are active and indexed at their current version and partition, per `record_locations`, may appear; passages without resolvable lineage are dropped (ADR 0008).
   - The adapter pins its retriever and disables the provider's automatic routing before any query route exists.
   - Enrichment jobs through `POST /v1/spaces/{spaceId}/enrichments`, run per partition as the service identity, with the job handler dispatching by operation.
   - The indexing collector started in provider mode as a cross-check that marks disagreeing records reconcile-required (ADR 0010).
   - The record-status route tightened to delivery or management rights.
   - Wiring: the backend is built in the application layer through the port and factory and injected into the service, so the API package still never imports it. The API process then needs provider mode, the provider extra, and model keys.

## Slice 1 delivered

- **Any listed audience may read.** Policy now allows a partition when the principal belongs to any of its audiences (ADR 0003 revision). Engine audiences are principal group names.
- **Every active record has a partition.** The ledger computes a deterministic partition key from the space and the record's mapped audiences, registers it in `access_partitions`, and stores it on the record. Quarantined and deleted records have none. The indexing collector now reads the stored partition.
- **The adapter's state is durable.** A provider-neutral `BackendStateStore` protocol has an in-memory implementation for tests and a SQLite implementation for the engine. The private adapter keeps partition bindings, record references, and identities there instead of process memory. Record references are keyed by partition, source, and record, so two sources may use the same record identifier.
- **Bound partitions never move.** If the provider ever reports a different native unit for a partition that is already bound, the adapter raises a partial-write error instead of silently rebinding.
- **The service identity owns provider units.** A durable resolver maps each engine principal to its own ordinary native account under a deterministic handle. The live test now ingests as the service identity and grants read access per audience before checking isolation (ADR 0005 revision).
- **Pinned-SDK guard.** The signature check now covers the provider's user creation and lookup calls.

Migration `0005_partitions_and_backend_state.sql` adds `access_partitions`, `source_records.partition_id`, `backend_bindings`, `backend_record_refs`, and `backend_identities`.

## Slice 2 delivered

- **Provider mode.** `CONTEXT_ENGINE_KNOWLEDGE_BACKEND=provider` makes the worker build the private backend over the control database's backend state. The default, `none`, keeps the worker ledger-only.
- **Text extraction.** Plain text, Markdown, HTML, and JSON become normalized text with a recorded parser version (`plain@1`, `markdown@1`, `html@1`, `json@1`). PDF is accepted for staging but fails indexing with `extraction_unsupported` until a pinned parser is added. The worker re-checks the staged bytes against the content hash before extracting.
- **Convergence per record.** After every ledger transition the worker writes, replaces, moves, or removes the record's backend copies until they match the ledger (ADR 0007 revision). Physical copies are tracked in `record_locations`.
- **Crash safety.** Writes record their intent before calling the backend. After a crash, an absent version is written again and a present but unrecorded one makes the record reconcile-required. Partial-write errors do the same.
- **Residue checks.** Every removal and replacement is followed by a check that the old version is gone from its partition.
- **Replace-style updates.** The private adapter's update writes a new item and drops the previous one, so metadata always carries the current version.
- **Per-record index state.** Each record carries `pending`, `indexed`, `failed`, `reconcile_required`, or `not_indexed`, with a stable error code. The record-status route exposes both, and source progress now counts indexing from the ledger when the backend is enabled.
- **Read access.** The worker grants and revokes backend read access so it matches engine policy: `context.read` on the space plus membership in one of the partition's audiences (ADR 0011).
- **Staged bytes released.** Uploads that no live record needs are deleted after convergence: every version of a deleted record and superseded versions of a live one.

Migration `0006_provider_pipeline.sql` adds the ledger fields, `record_locations`, `backend_read_access`, and `read_access_sync`.

## Slice 3 delivered

- **Context queries.** `POST /v1/queries` checks `context.read`, resolves the caller's readable partitions with the any-audience rule, queries the backend as the caller, and returns engine evidence with record, source, version, passage, location, and source URL. Readers outside every audience receive an insufficient-evidence result. Answer mode is rejected until M5.
- **Visibility barrier.** A passage is returned only when its record is active, indexed, in an allowed partition, and physically present there at its current version (ADR 0008 revision).
- **Lineage through engine references.** The adapter resolves each retrieved chunk through the native item it names and the durable references the engine recorded. Unknown, replaced, or out-of-scope items are dropped.
- **Pinned retriever.** The adapter uses the provider's chunk retriever with automatic routing off. The hybrid retriever was replaced after the live run showed it returns no chunk lineage (ADR 0008 revision).
- **Backend refusals.** When the backend refuses a partition engine policy allows, the query falls back to one partition at a time and the worker is told to resynchronize read access.
- **Enrichment jobs.** `POST /v1/spaces/{spaceId}/enrichments` needs `context.enrich`. The worker enriches each partition separately as the service identity and records the result on the job (ADR 0004 revision). Jobs are routed by operation; unsupported operations fail terminally.
- **Cross-check.** The worker checks the backend against the ledger on a schedule, using the version whose content was written, and flags lost copies as reconcile-required (ADR 0010 revision).
- **Record status tightened.** The route now needs delivery or management rights, like progress.
- **Wiring.** The application layer builds the backend and injects it into the service; the API package still never imports it. The API process needs provider mode, the provider extra, and model keys to serve queries.

Migration `0007_written_versions.sql` adds `record_locations.written_version`.

## Gate status

| M4 gate item | Status | Evidence |
| --- | --- | --- |
| Live isolation, update, and deletion checks against real stores | Pass on 2026-09-25 | `test_live_cognee_provider.py` with residue scan; `tests/end-to-end/test_live_provider_path.py` |
| Records map to deterministic partitions | Pass | `test_ledger_partitions.py` |
| Bindings, references, and identities survive a restart | Pass | `test_backend_state.py` |
| No default provider identity | Pass | Resolver tests; every principal gets its own account |
| Query the ingested record through its context space | Pass, live in one process | `test_query_api.py`; live end-to-end test |
| Enrichment creates traceable derived data | Partly: enrichment runs per partition live and is recorded on the job; the provider adapter reports no artifact lineage yet | `test_query_api.py` enrichment test; live end-to-end test |
| Replacing a record supersedes old answers | Pass, live | `test_record_indexer.py`; both live tests |
| Deleting a record removes every searchable artifact | Pass, live, for every live store; bytes remain in uncompacted storage and provider search history (ADR 0007 revision) | `test_record_indexer.py`; live residue scan |
| Partial writes are never silently repeated | Pass | Crash-before and crash-after tests in `test_record_indexer.py` |
| Backend read access follows engine policy | Pass, live | `test_read_access.py`; live tests grant and query per reader |
| Swapping in a test backend needs no application change | Holds so far | Dummy backend passes the shared contract tests |
| No public payload exposes provider terms | Pass | Boundary check and contract tests |

Validation performed on 2026-09-25 from `engine/`, after the live fixes, with the provider extra installed:

```text
ruff format --check: passed
ruff check: passed
pytest -m "not live_provider": 124 passed, 2 live tests skipped
pytest -m live_provider with model credentials: 2 passed
context-engine-migrate: applied 7 migrations to a fresh database
context-engine-api --check and context-engine-worker --check in provider mode: passed
provider boundary check: passed
```

## Live verification (2026-09-25)

The first live runs exposed defects that the deterministic backend could not show. All are fixed and covered by tests. The spike report lists them in order.

- **Chunk retriever.** The hybrid retriever returned rendered context with no chunk lineage, so every query failed safe as insufficient evidence. The adapter now pins the chunk retriever (ADR 0008 revision).
- **Pinned item ids.** The provider's write result lists every item of the unit, so updates in a shared partition kept the old item and deletes removed the wrong one. Each write now pins a derived item id, and the result must confirm it (ADR 0007 revision).
- **Row listing.** Items come back as stored rows, so the absence checks and the scheduled cross-check read no metadata. The cross-check marked every record reconcile-required, and every later change to those records failed. Fixed (ADR 0007 revision).
- **Uploads, not strings.** The provider reads a string that looks like a path or address as one. Record text now travels as an upload, and provider local reads are confined to its data directory (ADR 0002 revision, T13).
- **Provider setup and storage.** The runtime creates the provider's stores before the first operation, and it refuses a second storage root in one process.
- **Logging.** Importing the provider replaced the engine's log handlers. They are restored, and third-party records below warning are dropped (T14).

Open after the live run:

- **One process only.** With the local embedded stores, the API and the worker cannot run as separate processes: the worker's graph-store lock makes every API query fail. Choosing a multi-process topology is open (ADR 0002 revision).
- **Provider search history.** It keeps every question and the passages returned, and deletion does not clear them (T19).
- **Physical erasure.** Deleted text stays in uncompacted vector and graph storage until compaction (ADR 0007 revision).
- **Live concurrency and partial-write injection.** Neither has been run.

## Running the live gate

Follow the README live-provider section with explicit model and embedding credentials, then run `pytest -m live_provider` from `engine/`. Two tests run:

- **Two-audience lifecycle.** It ingests three records as the service identity and grants read access to two readers. It then checks isolation, provider-side denial, update supersession, and post-delete absence, and scans the provider's live stores for residue of replaced and deleted versions.
- **End to end.** It drives the REST API and the worker in one process through delivery, indexing, queries, replacement, an audience move, deletion, enrichment, the cross-check, and progress.

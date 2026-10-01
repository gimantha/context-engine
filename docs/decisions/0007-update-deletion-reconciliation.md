# ADR 0007: Update, deletion and reconciliation semantics

**Status:** Conditional — live stores verified clean on 2026-09-25; provider search history and physical compaction remain open

**Date:** 2026-09-18

## Context

An update or deletion can remove a relational record while leaving chunks, embeddings, graph edges, summaries or cached answers searchable.

## Decision

The public lifecycle uses engine operations: update a source record and delete a specific engine record. The private provider maps these to its native operations using opaque backend references.

An update succeeds only when the new version is visible and the old version is inaccessible through every supported read path. A deletion succeeds only when the record and its derived artifacts are inaccessible. Residual failures keep the job failed or reconcile-required.

## Fallbacks

- Unsafe in-place update: close visibility, rebuild into a clean binding, atomically switch the ledger and retire the old binding.
- Incomplete record deletion: quarantine the containing partition, rebuild allowed records and retire the old binding.

## Consequences

The API returns an asynchronous job for destructive lifecycle work. No public delete-everything operation is provided; the widest destructive operation is the deletion of one space (revision of 2026-10-01).

## Validation

- Dummy-backend lifecycle contract tests pass.
- The live provider test covers ingest, query, specific-record delete and post-delete query.
- Full residue inspection remains required before this ADR can be accepted.

## Revision (2026-09-24, M4 slice 2)

- **The worker converges each record.** After every ledger transition, replays included, the worker compares where the ledger says a record belongs with where its copies are (`record_locations`) and writes, replaces, moves, or removes copies until they match. All provider writes run as the engine's service identity.
- **Updates replace the item.** The private adapter writes the new version as a new item and then drops the previous one, so item metadata always carries the current version. New content under a new version number with the same content hash is not rewritten; only the ledger's version changes.
- **Moves write before they remove.** An ACL change that changes a record's audiences writes it into the new partition, then removes the old copy. The old audience loses access through engine policy as soon as the ledger changes.
- **Every removal and replacement is checked.** After a delete or a replacement, the worker asks the backend whether the old version is still present in that partition. If it is, the copy is marked reconcile-required and the job fails with `residue_found`. The check covers the provider's item listing; it does not inspect raw files or derived graph data directly, which remains part of the live residue scan.
- **Writes record their intent first.** A crash between a provider write and its confirmation leaves a `writing` copy. The next attempt checks the backend: if the version is absent, it writes again; if it is present, the record becomes reconcile-required instead of being written twice (ADR 0006). Partial-write errors from the backend have the same effect.
- **Unindexable versions leave nothing older behind.** If the current version cannot be read or extracted, every older copy is removed and the record is marked failed with a stable code.
- **Staged bytes are released when no live record needs them:** every version of a deleted record, and superseded versions of a live one. The current version of a quarantined record is kept.
- Validation: `engine/tests/test_record_indexer.py` and `engine/tests/test_extraction.py`.

## Revision (2026-09-25, live verification)

The first runs against the pinned provider found two defects that the deterministic backend could not show. Both are fixed.

- **Every write now names its own item.** The provider's write result lists every item its run touched in the unit, not only the one just written. The adapter took the first entry. In a unit that already held other records, an update therefore kept the old item, recorded the old id as current, and skipped removing it. A later delete then removed the old item and left the new copy searchable but untracked. The adapter now pins the item id, derived from the binding, record, version, and content hash. It fails the write as a partial write unless the provider's result includes that id. A retried write lands on the same item instead of creating a second one.
- **The presence check reads real items.** The provider lists items as stored rows, not mappings or models, so the adapter read no engine metadata from them. Every absence check after a removal passed without evidence, and the scheduled cross-check reported every indexed record as lost. The adapter now reads the metadata from the rows, and the test double returns rows.
- **Content is handed over as an upload.** The provider keeps the string form of its input in its run history, which deletion never clears. Record text now travels as an upload whose string form carries no content (ADR 0002 revision).
- **Live residue scan.** After an update and a delete, the live test checks that no trace of the replaced or deleted version remains in live provider state. It checks the raw files, the provider's relational rows, every vector table, and the unit's graph. It first proves the scan can see a record that still exists. The scan passed on 2026-09-25.

Two retention items remain open. Neither is reachable through any engine read path.

- **Physical residue until compaction.** Deleted text stays in the bytes of vector-store data fragments and graph-store pages until those stores compact. Live rows and graph nodes no longer hold it. Nothing compacts today, so the bytes stay until later writes happen to reuse the space. Physical erasure is planned for M7. Owner: Operations. What each store needs, as found on 2026-09-25:
  - **Vector store.** A delete only records which rows are gone. The data files are rewritten when the store's optimize step runs, and old versions are removed only with a zero retention period; the default keeps seven days. The provider runs this step only during one internal migration.
  - **Relational store.** Deleted rows stay in free pages because secure delete is off. A vacuum removes them; on a copy of the test store, it did.
  - **Graph store.** Checkpoints only make pending writes durable, and the pinned graph engine offers no vacuum or compaction. A guarantee needs the partition's graph rebuilt from its live data, as in the deletion fallback above.
- **Provider search history.** Every provider query records the question and the passages it returned, per isolation unit, and deletion does not clear either. Question text and passages of deleted records therefore persist in the provider's relational store. The fix is either to purge the history the engine's queries create, or to call the provider's retrieval below the layer that records it. Both depend on provider internals, so the choice is open (threat model T19). Owner: Backend owner.

Validation: `engine/tests/test_live_cognee_provider.py`, run with explicit model and embedding credentials, and the pinned-id, row-listing, and upload tests in `engine/tests/test_cognee_adapter.py`.

## Revision (2026-10-01, space deletion)

`DELETE /v1/spaces/{spaceId}` queues a `space_deletion` job for a principal with `space.manage`. The space moves to `deleting` at once, and the service refuses new deliveries, queries, enrichment, and configuration for it. The job, in `engine/src/context_engine/worker/space_deletion.py`, is idempotent at every step so a retry continues where the last attempt stopped:

1. Pause every source, so no delivery lands mid-deletion.
2. Tombstone every record at its current version on the engine's behalf, through the same ledger transition as a source's delete event (ADR 0006), keyed by the deletion job. A later resend of the same version cannot bring the record back; a strictly newer version from the source still can, which is right for a source that keeps changing after its space is gone.
3. Converge each record with the record indexer, which removes each copy and checks its absence as for a single deletion.
4. Drop each partition's isolation unit whole through the new port method `delete_partition`, then forget the binding and its references. This is the fallback above, applied at space scope: the unit, its graph, its vectors, and its read grants go together.
5. For a copy that step 3 left reconcile-required or found as residue, check absence once more now that its unit is gone, and drop its row. Nothing is written or deleted blindly. A copy still searchable fails the job with `residue_found`, and the space stays in `deleting` with its rows intact; a new attempt can be queued once the cause is fixed.
6. Delete the staged bytes, then every row of the space in one transaction: sources, records, versions, effects, partitions, bindings, read access, grants, and configuration. Jobs, outbox events, and access decisions stay as the audit trail.

In ledger-only mode the job skips steps 3 to 5. The private adapter implements `delete_partition` with the provider's whole-unit removal as the unit's owner, and treats a unit the provider no longer has as removed. Physical erasure of the removed unit's bytes remains the M7 item above.

Validation: `engine/tests/test_space_deletion.py` covers full deletion across two partitions with configuration, grants, staged bytes, and every control-database row checked gone; the 409s while deleting; ledger-only mode; residue keeping the space until a retry succeeds; a retryable backend failure continuing the job; and the permission rules.

Live run on 2026-10-01 against the pinned provider, through `context-engine-serve` with the local profile: one record indexed into a new space, then the space deleted. The job succeeded on its first attempt. The unit's row in the provider's relational store, its vector directory, its graph file, and the record's raw data file were all removed, while the two other spaces' units stayed untouched. A byte-level scan of every provider file for the record's text found nothing, which is more than record-level deletion gives: with the unit's files gone there are no fragments or pages left to compact. What remains are two rows of the provider's run history (`pipeline_runs`) for the removed unit, whose `run_info` holds the string form of the input: `<engine record>` with the engine's record id and source id, no content. Clearing the provider's history for the engine's own runs and queries is the open T19 item, planned for M5 slice 4.

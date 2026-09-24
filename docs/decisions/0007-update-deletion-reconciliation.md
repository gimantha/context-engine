# ADR 0007: Update, deletion and reconciliation semantics

**Status:** Conditional — real provider residue scan pending

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

The API returns an asynchronous job for destructive lifecycle work. No public delete-everything operation is provided.

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

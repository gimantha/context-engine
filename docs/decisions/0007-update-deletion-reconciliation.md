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

# ADR 0008: Evidence and provenance translation

**Status:** Accepted for the engine model; native mapping pending

**Date:** 2026-09-18

## Context

Provider search results and graph objects are unstable public contracts and can contain native IDs or inaccessible metadata. A source URL alone is not evidence.

## Decision

The adapter translates native results into `EvidenceItem` values containing:

- Engine evidence ID.
- Engine source and record IDs.
- Source version.
- Exact passage and location when available.
- Engine graph references and a relevance score.

Native IDs are stored only as opaque backend references for reconciliation. Public evidence is access-checked on every read, including after a query completes. Missing lineage produces unresolved internal evidence that must be suppressed from public results until reconciled.

The caller-safe trace lists selected engine evidence IDs and outcome. It never includes private chain of thought, raw provider traces or native identifiers.

## Consequences

- The ingestion pipeline must attach engine source metadata before indexing.
- Citations are suppressed when provider output cannot be mapped to a source version.
- Provider response shape changes are contained in one translator and its fixtures.

## Validation

- `engine/tests/test_cognee_adapter.py` verifies native fields do not escape.
- `engine/tests/test_public_boundary.py` verifies safe serialization.
- OpenAPI evidence and trace schemas use engine identifiers only.

## Revision (2026-09-25, M4 slice 3)

- **Queries run as the caller.** Engine policy resolves the partitions the caller may read, then the backend is asked as the caller, so the provider's own read check applies as defense in depth. If the backend refuses a partition the engine allows, the engine asks one partition at a time, skips refusals, and makes the worker resynchronize read access.
- **Lineage comes from the engine's own references.** The private adapter resolves each retrieved chunk through the native item it names and the durable references the engine recorded when it wrote that item. Chunks from items the engine did not write, from replaced versions whose reference is gone, or from partitions outside the request are dropped.
- **The ledger has the last word.** Before returning, the query service keeps a passage only when its record is active, indexed, in a partition the caller may read, and physically present there at its current version. The public version always comes from the ledger. Denied readers receive an insufficient-evidence result with no hint of what exists.
- **The retriever is pinned.** The adapter always uses the provider's chunk retriever with automatic routing off, so no question can be routed to raw graph queries.
- Validation: `engine/tests/test_query_api.py` and the structured-result and pinned-retriever tests in `engine/tests/test_cognee_adapter.py`.

## Revision (2026-09-25, live verification)

The first live run showed that the hybrid retriever cannot carry lineage. In the context-only mode the engine needs, the provider returns one rendered context string per isolation unit. The retrieved chunks are not attached, and that retriever produces no source references. Every passage was therefore unattributable, and queries failed safe with insufficient evidence.

- **The chunk retriever replaces it.** With context-only mode off, the provider returns one entry per retrieved chunk. Each entry names its isolation unit, the native item it came from, the chunk identifier and position, and the chunk text. That is exactly what reference resolution and the ledger barrier need. The retriever calls no model at query time.
- **Graph context is not evidence.** Entity and relation text the provider derives cannot be traced to one record, so it could never pass the barrier. Graph-aware ranking and answer generation are revisited in M5 with answer mode.
- **Only chunk entries count.** Entries of any other kind are dropped, and so is the earlier fallback that read record identifiers from provider metadata. The engine now trusts only references it recorded itself.
- **Ranking across partitions.** The provider ranks chunks within each isolation unit but returns no comparable score. The adapter interleaves partitions by rank so one partition cannot crowd out the others, and derives the internal score from that rank.
- Validation: the live two-audience and lifecycle test in `engine/tests/test_live_cognee_provider.py` passed against the pinned provider on 2026-09-25, together with the chunk-shape and interleaving tests in `engine/tests/test_cognee_adapter.py`.

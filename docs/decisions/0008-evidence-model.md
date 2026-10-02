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

## Revision (2026-10-01, M5 slice 2)

Queries are stored, evidence can be reopened, and every passage is placed in its record version.

- **Placement.** The provider's chunks are exact slices of the text the engine sent, but carry no offsets. The engine rebuilds that text from the current version's staged bytes, which it keeps while the record is live, and finds each passage by exact match (`engine/src/context_engine/provenance/`). It stores no second copy of the content. The text is used only when it comes from the same parser version that produced the indexed copy, and extraction output for a parser version must stay byte-identical.
- **Typed locator.** Each evidence item carries a `locator`: the chunk index, the character range in code points, the sentence range under the engine's own sentence rules, and by type the source lines (plain text and Markdown, counted as editors count), the nearest heading (Markdown and HTML), or the JSON path of the smallest value holding the passage. Each part is set only when it is certain. A passage repeated in its record is placed only when chunk order settles which occurrence it is, that is, when every occurrence was retrieved under distinct chunk indexes; otherwise it keeps its chunk index alone. The compact `location` string stays as `chunk:<n>` for earlier clients.
- **Stored queries.** Each query is stored with its asker, trace id, question, policy version, partitions, outcome, the counts of retrieved and suppressed passages, and the model that served it: the space's embedding model, or the environment's. A caller outside every audience is stored too, with no partitions and no evidence. Evidence ids are derived from the record version, chunk index, and passage, so a passage returned by many queries is one row.
- **Reads.** Only the asker can reopen a query, with `context.read` still held; anyone else gets 404. Evidence by id needs `context.read` or `evidence.read` on the space, an audience of the record's current partition, and the record still at the evidence's version; `evidence.read` opens citations without allowing questions. Every refusal is the same 404. Each read re-runs the ledger barrier, so evidence that moved out of reach, was replaced, or was deleted drops out.
- **Erasure.** Evidence holds passages, so it lives exactly as long as its version's content. The worker deletes it in the transaction that marks the version's staged bytes released, and space deletion purges queries and evidence with the space. Query rows stay until the retention sweep of slice 4.

Validation: `engine/tests/test_provenance.py` (extraction structure, sentences, headings, JSON paths, repeats), `engine/tests/test_stored_queries.py` (storage, reopening, access, barrier on read, erasure, contract conformance), and the multi-chunk check in `tests/end-to-end/test_live_provider_path.py`, which passed against the pinned provider on 2026-10-01: every passage of a record split into several chunks matched the text at the engine's offsets, chunk order matched text order, and the stored query reopened with the same evidence.

## Revision (2026-10-02, chunk size)

Passages are the provider's chunks, and until now their size was always the provider's default: as large as the models accept, about 8,000 tokens with the default embedding model. That made passages long to read as citations and let one chunk fill most of an answer's budget.

- **A chunk size setting.** `CONTEXT_ENGINE_CHUNK_TOKENS`, at least 128, sets the target chunk size for new writes. Unset keeps the provider's default. The adapter passes it as the provider write call's `chunk_size`, capped at the limit the provider computes for the space's own models, the smaller of the embedding model's input limit and half the language model's output budget, so no space gets chunks its embedding model cannot take.
- **The provider's chunking setters are inert.** In the pinned version, `config.set_chunk_size`, `set_chunk_overlap`, `set_chunk_strategy`, and `set_chunk_engine` write a configuration that nothing reads: its only reader, `get_chunk_engine()`, has no callers, and the write pipeline chunks with its sentence-joining chunker sized by the write call's argument.
- **No overlap, same chunker.** Evidence placement (revision of 2026-10-01) relies on ordered, non-overlapping exact slices, so the engine keeps the provider's chunker and adds no overlap.
- **Existing content.** Records keep the chunks they were written with until their next version; a partition may hold chunks of both sizes, which retrieval and placement handle alike.

Validation: the chunk-size and cap tests and the pinned-SDK check in `engine/tests/test_cognee_adapter.py`, and the end-to-end live test, which now indexes with 1,024-token chunks. On 2026-10-02 it passed against the pinned provider: the 43,583-character record came back as at least four distinct chunks among ten passages, where the default gives two or three, and every passage matched the text at the engine's offsets, in chunk order.

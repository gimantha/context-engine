# M5 Provenance, Evidence, Mediated Query, and Traces

**Status:** Slice 1 delivered on 2026-09-29; slices 2 to 6 planned

**Date:** 2026-09-29

## Plan

M5 turns the one-shot context query of M4 into a stored query with evidence a caller can reopen, an explanation of how that evidence was chosen, and an answer whose citations are checked rather than trusted. The contract already defines the query, evidence, and trace read routes, and the `evidence.read` and `trace.read` actions exist since M2. Each slice leaves the non-live suite passing.

1. **Space configuration.** `PUT` and `GET /v1/spaces/{spaceId}/configuration`, requiring `space.manage`, in the shape the control-plane UI already sends: an embedding model and a language model, each with provider and model name. The engine stores a secret reference for each key, never the key: keys are held by the control plane and resolved at call time. `GET` returns provider and model only. The space's models drive answers, the provider's extraction during indexing, and its embeddings, passed per call to the provider. The embedding model is fixed once the space has indexed content, until M7 adds reindexing. A space without configuration falls back to the engine's environment settings, so existing spaces keep working. Every stored query records the provider and model that answered it.
2. **Stored queries and evidence reads.** Persist each query with its policy version, partitions, outcome, and returned evidence, and serve `GET /queries/{queryId}`, `/queries/{queryId}/evidence`, and `/evidence/{evidenceId}`. Evidence location becomes a typed structure: the chunk index, the character range in the record version's extracted text, the sentence range the engine computes over it, and an anchor whose kind depends on the type (`line` for text and Markdown, `heading` where one is known, `path` for JSON). The provider's chunks are exact slices of the text the engine sent, so a passage is located by exact match, with chunk order resolving repeats. Tracing depth per type is recorded in the lineage appendix. Every read re-runs the visibility barrier. Stored passage text is tied to its record version: when the worker releases a superseded or deleted version, it removes that version's evidence rows, so erasure stays with the ledger.
3. **Answer mode.** An answer-generator port in the application layer, with a provider-backed implementation that calls the provider's model client with the space's configuration, and a deterministic one for tests. The prompt holds only evidence that passed the barrier, each passage labelled with its evidence id. Citations are checked against that set and unknown ones dropped; an answer with no surviving citation is returned as insufficient evidence, never as prose with invented sources. The provider's own completion mode stays unused because it retrieves on its own.
4. **Traces and retention.** A caller-safe trace of evidence selection: partitions considered, passages retrieved and suppressed, and the outcome. An administrator trace exposing the access decisions recorded under the query's trace id, for `trace.read`. A per-space query listing with time, principal, and outcome filters. A retention setting and worker sweep that removes old queries and purges the provider's search history for the engine's queries, which closes threat T19.
5. **Lineage across partitions.** A live probe of what the provider's per-partition graph reports as each record's entities, then entities recorded per record in the engine, and the read-time join agreed for ADR 0004: linked records' passages arrive as second-hop evidence with lineage, and the evidence's graph path is filled in. The gate test runs a two-audience query and checks that no link crosses into a partition the caller cannot read.
6. **Page citations.** Adds the `page` anchor kind for PDFs, the one type with fixed pages; Word and Google Docs get headings, slides get slide numbers. The engine extracts PDF text itself with pypdf, the library the provider's own PDF loader uses and which the provider extra already installs, recording each page's character range per record version. At query time a passage is located in that text and cited as a page, or as a page range when it spans one; a passage that cannot be located keeps its chunk location. The provider's loader is not used for this because its chunks carry no page field, only "Page N:" text markers that the chunker may split or merge.

## Slice 1 delivered

- **Configuration route.** `PUT` and `GET /v1/spaces/{spaceId}/configuration` in the control plane's shape. `PUT` replaces the whole configuration and needs `space.manage`; `GET` needs `source.manage` or `space.manage` and returns provider, model, how each key is held, whether the embedding is locked, the version, and the engine's store placement. The control plane's `storage` and `sources` fields are accepted and ignored.
- **Keys.** References (`env:<NAME>`, `cp:<id>`) are stored as given and resolved at call time; the control-plane resolver calls the agreed `GET <base>/secrets/<id>` with the engine's service credential. Literal keys are encrypted with AES-GCM under `CONTEXT_ENGINE_SECRETS_KEY` and refused when no master key is set; this is how a standalone deployment holds keys, and how a Devant deployment holds them until its control plane serves secrets (user decisions, 2026-09-29 and 2026-09-30). A model resent without a key keeps its stored key, so a read configuration can be sent back. Keys never appear in string forms, logs, or errors (ADR 0014, threat model T20).
- **Models per call.** Every model-running backend call carries an optional `ModelSelection`, resolved from the space's configuration just before the call. The worker resolves per write and per enrichment job, the query service per query. The private adapter translates to the provider's per-call settings, and applies them through its context variables for enrichment, which takes none. Unconfigured spaces resolve to `None` and use the environment settings.
- **Embedding lock.** Changing, adding, or removing the embedding model is refused with 409 once the space holds indexed content; the key may still change.
- **Contract 0.9.0** adds the route and its schemas, with an example.

Migration `0008_space_configurations.sql` adds `space_configurations`. `cryptography` becomes an engine dependency at the locked version.

Validation performed on 2026-09-29 from `engine/`:

```text
ruff format --check and ruff check: passed
pytest -m "not live_provider": 155 passed, 2 live tests deselected
context-engine-api, -worker, and -serve --check: passed
context-engine-migrate: 8 migrations on a fresh database
provider boundary check: passed
```

Live verification on 2026-09-29, with the environment's OpenAI models passed explicitly as a per-call selection for the shared record's ingest and for one reader's query, alongside calls with no selection:

```text
pytest -m live_provider: 2 passed in 3 min 9 s
```

Both paths produced the same isolation, replacement, deletion, and residue results as before, so a selection that names the environment's own models behaves like the default. A selection naming a different model was not run live; that needs a second configured provider.

## Space deletion delivered

Added before slice 2 so local and test environments can be cleared through the API, and because the contract has listed `DELETE /spaces/{spaceId}` since M1 without an implementation.

- **Route.** `DELETE /v1/spaces/{spaceId}` needs `space.manage` and answers 202 with a `space_deletion` job. The space is marked `deleting` at once; deliveries, queries, enrichment, source registration, and configuration on it answer 409 from then on. Repeating the request returns the running job; after a failed job it queues a new attempt.
- **Job.** `worker/space_deletion.py` pauses the sources, tombstones every record at its current version through the ledger (keyed by the job, so a retry replays as a no-op), converges each record with the indexer, drops every partition's isolation unit whole through the new `delete_partition` port method, re-checks copies that single deletion left unproven, deletes the staged bytes, and removes the space's rows in one transaction. Jobs and access decisions remain as the audit trail. The sequence, and why a reconcile-required copy no longer blocks a space deletion, is the ADR 0007 revision of 2026-10-01.
- **Backend.** `delete_partition` on the port, the deterministic backend, and the private adapter (whole-unit removal as the unit's owner; a unit the provider no longer has counts as removed), with `delete_partition` on both backend state stores to forget the binding and references.
- **Contract 0.10.0** documents the route's behaviour and error responses.

`engine/tests/test_space_deletion.py` (6 tests) covers full deletion with every control-database row, staged file, grant, and backend record checked gone; the 409s while deleting; ledger-only mode; residue keeping the space until a retry succeeds; a retryable backend failure resuming the job; and permissions.

Live run on 2026-10-01 through `context-engine-serve` with the local profile: one record indexed into a fresh space with the environment's OpenAI models, then `DELETE /v1/spaces/{id}`. The job succeeded on its first attempt; the space answered 404, every control-database row of it was gone, its staged file was deleted, and the provider's unit row, vector directory, graph file, and raw data file were removed while the two other local spaces' units stayed. A byte-level scan of every provider file for the record's text found nothing. Two provider run-history rows for the removed unit remain, holding `<engine record>` with the engine's record and source ids and no content; clearing that history is the T19 work of slice 4.

Validation performed on 2026-10-01 from `engine/`:

```text
ruff format --check and ruff check: passed
pytest -m "not live_provider": 161 passed, 2 live tests deselected
context-engine-api, -worker, and -serve --check: passed
provider boundary check: passed
```

## Decisions

- **Answers use the space's models** (user, 2026-09-29), and so do extraction and embeddings.
- **Keys stay with the control plane** (user); the engine holds references. How a reference is resolved, a control-plane secret endpoint called with the engine's service credential or model calls proxied through the control plane, is decided in slice 1.
- **Unconfigured spaces use the environment settings** as a fallback.
- **Stored passages are kept, tied to their record version,** and erased with it.
- **No query cache in M5.** The engine has no answer cache and the provider's cache is disabled; recorded against threat T05.
- **PDF extraction uses pypdf** in the engine, declared as an engine dependency.

## Gate

| M5 gate item | Plan |
| --- | --- |
| Each citation opens to the exact authorized source version and chunk or page | Slices 2 and 6 |
| Queries spanning disjoint audiences return only eligible evidence and derived graph paths | Slice 5 and the existing barrier |
| An unsupported answer presents no invented citations | Slice 3 |
| Two traces: access decisions for administrators, evidence selection for callers | Slice 4 |

Live runs with credentials: answer mode with a configured model, and the entity probe for slice 5. The open items from the M4 report stay listed here until closed: the multi-process topology, due before M6, and the provider search history, which slice 4 closes.

## Lineage appendix: how far a passage can be traced, per type

The provider's default chunker joins whole sentences into chunks and keeps each chunk's text as an exact slice of the document text, but records no offsets, pages, or sentences. A passage can therefore be traced only as far as the text the engine itself holds allows, by locating it with an exact match.

| Type | Extracted by | Text the chunker sees | Reachable trace |
| --- | --- | --- | --- |
| Plain text, Markdown | Engine | Normalized text | Sentence; raw line with a line map |
| HTML | Engine | Visible text, blocks as line breaks | Sentence in the extracted text; no HTML source position |
| JSON | Engine | Canonical re-serialization | Key path, not sentences |
| PDF | Engine (slice 6) | Page text with page ranges | Page and sentence |
| PDF, Word, slides, spreadsheets | Provider loaders | Derived text the engine never holds | Chunk index only |
| CSV | Provider | Rows rewritten as `column: value` pairs | Row |
| Images, audio, video | Provider, through a model | A description or transcript | None to the source |

Consequences: extraction stays in the engine for every type we cite; a sentence longer than the token budget, or one crossing the provider's one-million-character read blocks, may be cut at a chunk edge, so sentence numbering tolerates partial sentences; and the engine segments sentences with its own rules rather than the provider's, which split on any period.


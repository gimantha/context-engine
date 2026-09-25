# M0 Spike Report

**Status:** Engine spike implemented; live provider matrix passed on 2026-09-25

**Date:** 2026-09-18

## Environment

| Item | Value |
| --- | --- |
| Host platform | macOS development workspace |
| Python | 3.12.11 |
| Package manager | `uv` |
| Cognee pin | 1.5.4, optional private-provider dependency |
| Relational store for live spike | SQLite |
| Vector store for live spike | LanceDB |
| Graph store for live spike | Kuzu |
| Backend access control | Required/enabled for live spike |
| Session behavior | Disabled for document corpus |
| Implicit enrichment | Disabled by adapter ingestion mapping |

## Implemented artifacts

- Provider-neutral `KnowledgeBackend` protocol and immutable engine values.
- Deterministic dummy backend with partition-scoped records, derived artifacts and query cache.
- Private Cognee adapter with lazy imports, explicit identity/isolation mapping, stable error translation and SDK signature guard.
- Pure authorization decision function.
- Two-audience fixture with overlapping graph entities and unique canary facts.
- Contract and isolation tests for replay, ordering, update, deletion, enrichment, concurrency and revocation.
- Draft OpenAPI, ingestion event and MCP contracts.
- Public serialization guard and provider-leak lint.
- Eight architecture decisions and a threat model.

## Provider mapping observations

The mapping was implemented against the tagged Cognee 1.5.4 SDK source:

| Engine operation | Private provider behavior | Important observation |
| --- | --- | --- |
| Ingest | Native permanent-memory write with implicit enrichment disabled | Result must contain both native isolation and record IDs; absence is treated as unsupported |
| Query | Native retrieval with explicit user and explicit native isolation IDs | The adapter requests context and references, then translates results into engine evidence |
| Update | Native incremental update with explicit record, isolation and user IDs | Safe use depends on the live stale-artifact scan |
| Enrich | Native enrichment separately for each authorized internal partition | Cross-partition provider-side enrichment is not used |
| Delete | Native specific-record deletion with explicit isolation and user IDs | Success still requires a post-delete retrieval/residue check |

Native operation names and identifiers remain inside the private provider module.

## Automated verification

This table is updated by the implementation run:

| Check | Result | Evidence |
| --- | --- | --- |
| Python formatting and lint | Pass | 19 files formatted; Ruff reported no violations |
| Unit and contract tests | Pass | 18 passed; live-provider test deselected |
| Provider-boundary lint | Pass | No private provider term in public contracts and no forbidden import |
| Cognee 1.5.4 SDK signature compatibility | Pass | Installed package reports 1.5.4 and all mapped signatures match |
| Live ingest/query/delete | Pass on 2026-09-25 | `test_live_cognee_provider.py` and `tests/end-to-end/test_live_provider_path.py`; see the update below |
| Live two-audience graph isolation | Pass on 2026-09-25 | Each reader retrieved only its own and shared records; the provider refused an ungranted partition; each partition has its own graph unit |
| Live partial-write/update residue scan | Update and deletion residue pass; partial-write injection not run live | Live stores hold no trace of replaced or deleted versions; bytes remain in uncompacted storage and in provider search history |

## Known findings

1. The real provider requires a configured LLM for graph extraction; no compatible credentials or local model endpoint were present in the workspace environment.
2. Native ingestion must return a stable record ID. The adapter fails closed when it does not.
3. The adapter does not use native defaults for user or isolation scope.
4. The live topology is an architecture-spike selection and is not a production topology recommendation.
5. The provider's own source warns that deletion and updates span multiple stores. Engine success therefore remains conditional on residue checks and reconciliation.

## Evidence hashes

- `engine/uv.lock`: `f8c1507666141266b5cc3ac61bac3475b6a3a0108710b2f58cb25488682035cb`
- Two-audience fixture: `b265ac08dbba7056b062286f7d79b6543647cc6deb8b7eac5f4442b5aa42af56`

## Reproduction

From `engine/`:

```bash
uv sync --group dev
uv run ruff check src tests ../tests
uv run pytest -m "not live_provider"
```

After configuring an explicit LLM and embedding provider:

```bash
CONTEXT_ENGINE_RUN_LIVE_PROVIDER=true uv run --extra knowledge-provider pytest -m live_provider -v
```

Do not place credentials in this report or committed environment files.

## Update (2026-09-24)

The live test was rewritten for M4 slice 1. The engine's service identity now ingests every record and grants read access per reader before the isolation checks, which is how the worker will use the provider. It has still not been run: no model credentials are present in this workspace.

## Update (2026-09-25, live run)

The live matrix ran with OpenAI `gpt-5-mini` for extraction and `text-embedding-3-small` at 1536 dimensions, configured explicitly through engine settings. Credentials stayed in the ignored local profile.

Final results, from `engine/` with the profile loaded:

```text
pytest -m live_provider: 2 passed
  tests/test_live_cognee_provider.py: two-audience lifecycle with residue scan
  tests/end-to-end/test_live_provider_path.py: REST API and worker, in one process
pytest -m "not live_provider": 124 passed, 2 live tests skipped
```

The first runs failed in ways the deterministic backend could not show. Each was fixed and is covered by a test.

1. **Local files refused.** The provider stores text and reads it back as a local file, so refusing all local paths failed every ingestion. Local reads are now confined to the provider's data directory, and record text is handed over as an upload (ADR 0002 revision).
2. **Retriever without lineage.** The hybrid retriever returns rendered context without the chunks it used, so no passage could be traced to a record, and every query returned insufficient evidence. The adapter now pins the chunk retriever (ADR 0008 revision).
3. **Wrong item on update.** The provider's write result lists every item of the unit, and the adapter took the first. Updates in a shared partition kept the old item, and deletes then removed the wrong one. The adapter now pins each item's id (ADR 0007 revision).
4. **Presence check without evidence.** Items are listed as stored rows, so no engine metadata was read. Absence checks passed without evidence, and the scheduled cross-check marked every record reconcile-required. Fixed (ADR 0007 revision).
5. **Provider stores not created.** The worker failed every job because the provider's relational store did not exist yet. The runtime now runs the provider's setup once per process.
6. **Engine logging replaced.** Importing the provider removed the engine's log handlers and printed query text. The engine's handlers are restored, and third-party chatter is dropped.
7. **Two processes cannot share the stores.** The embedded graph store is locked by one process, so the API cannot query while the worker runs. This is an open topology decision (ADR 0002 revision).

Open items: live concurrent-request isolation and partial-write injection, compaction for physical erasure, provider search-history retention (threat model T19), and a multi-process topology.

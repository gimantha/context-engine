# M0 Spike Report

**Status:** Engine spike implemented; live provider execution pending credentials

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
| Live ingest/query/delete | Blocked | No engine model credential or configured local model endpoint was present |
| Live two-audience graph isolation | Blocked | Requires configured model and embedding providers |
| Live partial-write/update residue scan | Blocked | Requires configured model and embedding providers |

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

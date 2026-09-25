# M0 Threat Model

**Status:** Reviewed for the architecture spike

**Date:** 2026-09-18

**Scope:** Context ingestion, authorization, knowledge-backend boundary, query/evidence access and record lifecycle.

## Assets

- Source content and source ACL metadata.
- Derived chunks, embeddings, graph nodes/edges, summaries and cached answers.
- Context-space, source, record, job, grant, evidence and trace metadata.
- User, agent and connector identities.
- Provider credentials, model credentials and staged-object access.
- Audit and access-decision records.

## Trust boundaries

```text
Caller / Connector
        |
        | authenticated public contract
        v
API or MCP facade -----> policy decision service
        |                         |
        | engine commands        | authorized internal partitions
        v                         v
Application service ------> knowledge-backend port
        |                         |
        | ledger/jobs            | private native mapping
        v                         v
Control database          Provider stores + model services
```

The browser, connectors, MCP clients, staged-object service, queue, model provider and all provider stores are separate trust boundaries. A successful check at one boundary does not replace checks at another.

## Threat analysis

| ID | Threat | Impact | Mitigation | M0 evidence | Residual risk / owner |
| --- | --- | --- | --- | --- | --- |
| T01 | Request body supplies another principal | Cross-user access | Build principal only from verified auth context; immutable `PrincipalContext` | Port requires explicit context; policy tests; M2 REST dependency builds the principal from the bearer credential only, schemas reject identity fields (`test_api.py`) | Identity-provider verifiers in M6 (ADR 0009) / Identity owner |
| T02 | Connector selects another space or source | Cross-tenant ingestion | Authenticated connector registration overrides envelope claims | M3 checks `ingest.write` on the source named in the body and rejects a source outside the named space (`test_sources_api.py`) | Ballerina connector credentials in its own workstream / Integration owner |
| T03 | Backend uses default identity or broad scope | Cross-audience retrieval | No optional data-operation identity; explicit partition list; adapter rejects empty scope | Backend and adapter tests; M4 resolver gives every principal its own native account and never a default user (`test_backend_state.py`); live run on 2026-09-25: two readers saw only their own and shared records, and the provider refused a partition the reader held no grant for (`test_live_cognee_provider.py`) | Live concurrent-request isolation pending / Backend owner |
| T04 | Shared entity links restricted records | Derived information leak | Partition graph extraction and enrichment; prohibit cross-partition persistence | Golden fixture repeats entities; dummy tests pass; live: each partition has its own graph unit, and the live test finds no trace of a deleted record in its unit's graph | Enrichment-derived links not yet inspected live / Backend owner |
| T05 | Query cache survives revocation | Stale access | Cache key includes principal and partition set; invalidate on writes; policy recheck | Dummy revocation and update tests | Distributed cache design in M5 / Query owner |
| T06 | Permission changes while job waits | Unauthorized write/delete | Reauthorize at worker execution time | M2 worker re-decides the job's action before any effect; revoked grants fail the job terminally (`tests/recovery/test_worker_reauthorization.py`) | Provider-side effects arrive in M4 / Worker owner |
| T07 | Evidence endpoint bypasses current access | Restricted passage disclosure | Recheck evidence access on every request | Public contract and ADR 0008; M4 queries apply policy and a ledger barrier on every request (`test_query_api.py`) | Evidence and trace endpoints in M5 / API owner |
| T08 | Native identifier or error reaches a client | Internal topology leak | Private wrapper types, error translation, serializer guard, contract lint | Boundary, adapter and contract tests | New provider response shapes / Backend owner |
| T09 | Partial provider write becomes searchable | Inconsistent or leaked content | Authoritative ledger, visibility state and reconciliation; stable partial-write error | ADR 0006; M4 records write intent and marks unconfirmed writes reconcile-required (`test_record_indexer.py` crash tests) | Live failure injection pending / Persistence owner |
| T10 | Update leaves stale graph/vector artifacts | Stale confidential content | Post-update residue scan; rebuild fallback | Dummy lifecycle test; M4 replace-style updates with an absence check on the old version (`test_record_indexer.py`); live residue scan of raw files, relational rows, vector tables, and graph after replacement passed on 2026-09-25, after pinning native item ids (ADR 0007 revision) | Bytes in uncompacted vector and graph storage until M7 erasure; provider search history (T19) / Backend owner |
| T11 | Delete leaves searchable artifacts | Erasure/access failure | Specific-record delete, post-delete scan, quarantine/rebuild fallback | Dummy lifecycle test and ADR 0007; M4 absence check after every removal and staged-byte release (`test_record_indexer.py`); live residue scan after deletion passed on 2026-09-25 (`test_live_cognee_provider.py`) | Compaction for physical erasure in M7; provider search history (T19) / Backend owner |
| T12 | Staged object is replayed or substituted | Ingest unintended content | Scoped expiry, content hash, type and size verification | M3 uploads are scoped to a source, size and type limited, hashed, expire, and must match the event before a job exists (`test_sources_api.py`); M4 re-checks the hash before indexing and releases unneeded bytes | Expired-orphan sweep in M7 / Ingestion owner |
| T13 | URL causes server-side request forgery | Internal network access | Source URL is display provenance; engine never fetches arbitrary supplied URLs | Contract separates `sourceUrl` and `contentRef`; M3 accepts bytes only through staged uploads. Live testing showed the provider reads a text item as a local path or web address when it looks like one; a document whose text was the path of the provider's own database was loaded as that file. M4 hands every record to the provider as an upload, which is stored as given, and confines provider local reads to its data directory (ADR 0002 revision, `test_cognee_adapter.py`) | Connector-side fetch allowlists / Ingestion owner |
| T14 | Model or telemetry service receives secrets | External disclosure | Minimize content, disable vendor telemetry for tests, redact logs, document retention | Secret-free types and report rules; importing the provider replaced the engine's log handlers, dropping audit fields, and its console output carried query text. The adapter restores the engine's handlers and the engine drops third-party records below warning (`test_logging.py`, `test_cognee_adapter.py`) | Provider configuration review / Operations owner |
| T15 | Raw graph/Cypher surface bypasses policy | Bulk disclosure or mutation | No raw graph API/MCP tool; provider service is private | OpenAPI/MCP contracts and leak lint; M4 pins the provider's chunk retriever with automatic routing off (`test_cognee_adapter.py`) | Dependency upgrades / Security owner |
| T16 | Forged backend reference targets another partition | Cross-partition deletion | Reference is private and checked against current partition binding | Adapter out-of-scope check; M4 bindings and references are durable and a bound partition cannot move (`test_backend_state.py`) | Live verification pending / Persistence owner |
| T17 | Denial reveals a restricted title or existence | Enumeration | Stable generic error codes/messages | Denied example and adapter translation; M2 returns not-found for invisible resources and a fixed access-denied message otherwise (`test_api.py`); source progress counts and record status require delivery or management rights, not read access (`test_progress_api.py`, `test_sources_api.py`) | Evidence and trace routes in M5 / API owner |
| T18 | Dependency update changes native defaults | Silent isolation regression | Exact pin, signature check, shared contract suite and upgrade ADR evidence | `pyproject.toml` pin and adapter assertion | Lock refresh review / Backend owner |
| T19 | Provider search history keeps questions and returned passages | Deleted or revoked content and question text persist in provider storage | Purge the history the engine's queries create, or retrieve below the layer that records it | Found in the live run on 2026-09-25: every provider query stores its question and passages per isolation unit, and deletion does not clear them; unreachable through engine read paths | Choice between purging and a lower-level retrieval call (ADR 0007 revision) / Backend owner |

## High-severity gate

T03, T04, T05, T09, T10 and T11 are high severity. M0 cannot receive an unconditional go decision until the real provider/store combination passes the isolation, partial-failure, update and deletion tests. The dummy backend proves the engine contract but cannot prove native store behavior.

## Logging and evidence handling

- Do not log content, query text, source titles, tokens, credentials or native IDs in public logs.
- Test reports use fixture hashes, trace IDs and stable test names.
- Private provider diagnostics may record native operation names and versions but must redact content and credentials.
- Failed authorization logs record principal, engine resource, action, policy version and reason code only.

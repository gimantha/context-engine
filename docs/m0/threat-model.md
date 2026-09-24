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
| T03 | Backend uses default identity or broad scope | Cross-audience retrieval | No optional data-operation identity; explicit partition list; adapter rejects empty scope | Backend and adapter tests; M4 resolver gives every principal its own native account and never a default user (`test_backend_state.py`) | Live native-call audit pending / Backend owner |
| T04 | Shared entity links restricted records | Derived information leak | Partition graph extraction and enrichment; prohibit cross-partition persistence | Golden fixture repeats entities; dummy tests pass | Live graph inspection pending / Backend owner |
| T05 | Query cache survives revocation | Stale access | Cache key includes principal and partition set; invalidate on writes; policy recheck | Dummy revocation and update tests | Distributed cache design in M5 / Query owner |
| T06 | Permission changes while job waits | Unauthorized write/delete | Reauthorize at worker execution time | M2 worker re-decides the job's action before any effect; revoked grants fail the job terminally (`tests/recovery/test_worker_reauthorization.py`) | Provider-side effects arrive in M4 / Worker owner |
| T07 | Evidence endpoint bypasses current access | Restricted passage disclosure | Recheck evidence access on every request | Public contract and ADR 0008 | Endpoint implementation in M5 / API owner |
| T08 | Native identifier or error reaches a client | Internal topology leak | Private wrapper types, error translation, serializer guard, contract lint | Boundary, adapter and contract tests | New provider response shapes / Backend owner |
| T09 | Partial provider write becomes searchable | Inconsistent or leaked content | Authoritative ledger, visibility state and reconciliation; stable partial-write error | ADR 0006; M4 records write intent and marks unconfirmed writes reconcile-required (`test_record_indexer.py` crash tests) | Live failure injection pending / Persistence owner |
| T10 | Update leaves stale graph/vector artifacts | Stale confidential content | Post-update residue scan; rebuild fallback | Dummy lifecycle test; M4 replace-style updates with an absence check on the old version (`test_record_indexer.py`) | Live raw-file and graph residue scan pending / Backend owner |
| T11 | Delete leaves searchable artifacts | Erasure/access failure | Specific-record delete, post-delete scan, quarantine/rebuild fallback | Dummy lifecycle test and ADR 0007; M4 absence check after every removal and staged-byte release (`test_record_indexer.py`) | Live raw-file and graph residue scan pending / Backend owner |
| T12 | Staged object is replayed or substituted | Ingest unintended content | Scoped expiry, content hash, type and size verification | M3 uploads are scoped to a source, size and type limited, hashed, expire, and must match the event before a job exists (`test_sources_api.py`); M4 re-checks the hash before indexing and releases unneeded bytes | Expired-orphan sweep in M7 / Ingestion owner |
| T13 | URL causes server-side request forgery | Internal network access | Source URL is display provenance; engine never fetches arbitrary supplied URLs | Contract separates `sourceUrl` and `contentRef`; M3 accepts bytes only through staged uploads and the adapter keeps provider loaders disabled | Connector-side fetch allowlists / Ingestion owner |
| T14 | Model or telemetry service receives secrets | External disclosure | Minimize content, disable vendor telemetry for tests, redact logs, document retention | Secret-free types and report rules | Provider configuration review / Operations owner |
| T15 | Raw graph/Cypher surface bypasses policy | Bulk disclosure or mutation | No raw graph API/MCP tool; provider service is private | OpenAPI/MCP contracts and leak lint | Dependency upgrades / Security owner |
| T16 | Forged backend reference targets another partition | Cross-partition deletion | Reference is private and checked against current partition binding | Adapter out-of-scope check; M4 bindings and references are durable and a bound partition cannot move (`test_backend_state.py`) | Live verification pending / Persistence owner |
| T17 | Denial reveals a restricted title or existence | Enumeration | Stable generic error codes/messages | Denied example and adapter translation; M2 returns not-found for invisible resources and a fixed access-denied message otherwise (`test_api.py`); source progress counts require delivery or management rights, not read access (`test_progress_api.py`) | Record-status route still answers any caller who can see the source; query and evidence routes in M5 / API owner |
| T18 | Dependency update changes native defaults | Silent isolation regression | Exact pin, signature check, shared contract suite and upgrade ADR evidence | `pyproject.toml` pin and adapter assertion | Lock refresh review / Backend owner |

## High-severity gate

T03, T04, T05, T09, T10 and T11 are high severity. M0 cannot receive an unconditional go decision until the real provider/store combination passes the isolation, partial-failure, update and deletion tests. The dummy backend proves the engine contract but cannot prove native store behavior.

## Logging and evidence handling

- Do not log content, query text, source titles, tokens, credentials or native IDs in public logs.
- Test reports use fixture hashes, trace IDs and stable test names.
- Private provider diagnostics may record native operation names and versions but must redact content and credentials.
- Failed authorization logs record principal, engine resource, action, policy version and reason code only.

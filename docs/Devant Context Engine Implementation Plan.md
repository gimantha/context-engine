# Context Engine — Standalone Implementation Plan

**Purpose:** Build a reusable context engine that turns external content into a governed, queryable context graph.

**Audience:** Implementation team or a coding agent working milestone by milestone.

**Repository root:** `context-engine/`.

**Scope:** Ballerina source integrations; Python orchestration with a pluggable knowledge backend; provenance, evidence and decision traces; authentication and access control; HTTP and MCP interfaces; and a query UI. Cognee is the initial private backend provider.

## 1. Implementation rules

1. Inspect the repository, any `AGENTS.md` files, and the pinned backend-provider release before changing code. Treat the endpoint paths and module names below as the proposed contracts to implement, rather than existing interfaces.
2. Deliver the milestones in order. For each, commit code, migrations, OpenAPI/schema changes, examples, and the acceptance evidence. Keep the service runnable after each milestone.
3. Keep every Cognee concept behind a Python knowledge-backend port. Public contracts, domain models, database-facing application interfaces, Ballerina envelopes, UI labels, MCP tools, logs and metrics must use engine-owned terms. They must never expose Cognee names, types, identifiers or verbs, including `dataset`, `DataItem`, `remember`, `recall`, `improve` and `forget`. The private provider maps engine operations such as ingest, query, enrich and delete to Cognee. Ballerina owns connectors, extraction where appropriate, source checkpoints and delivery. Both runtimes live in this monorepo.
4. Expose one application service for authorization, commands and queries. HTTP, MCP and UI backends must use that service, so they cannot acquire different access or citation behavior.
5. Choose a concrete relational, graph, vector, embedding and LLM configuration in an early spike. Verify the *combination* under concurrent reads, deletion and permission changes; individual provider support alone is insufficient.
6. Do not enable provider session memory in the initial document corpus. Configure implicit provider-side enrichment explicitly so an engine enrichment job cannot accidentally run the same processing twice.

## 2. Architecture and ownership

| Component | Owns | Boundary |
| --- | --- | --- |
| Ballerina integrations | Source authentication, polling/webhooks/CDC, source IDs and versions, normalization, checkpoints, delivery and backoff | Sends signed or authenticated ingestion envelopes to the engine; cannot grant users read access |
| Python API | Authentication, request validation, access decisions, public contracts, query mediation | Accepts only engine resource IDs and never exposes or accepts backend identifiers |
| Python worker | Durable ingestion, enrichment, deletion, reindex and ACL migration jobs | Rechecks current permissions and source registration at execution time |
| Knowledge backend port | Engine-owned ingest, query, enrich, update and delete operations with engine-owned request/response types | The only boundary through which application code accesses a knowledge provider |
| Cognee provider | Maps the knowledge-backend port to the pinned Cognee package and translates provider errors, IDs and metadata | Private implementation detail; provider concepts never escape this module |
| Control database | Resource registry, source-to-data ledger, job state, policy/grant state, provenance and trace indexes | Transactionally coordinates jobs; never assumes its commit also commits graph/vector writes |
| Query UI | Context-space selection, question/answer flow, evidence and trace exploration, access management | Uses the public engine API; no direct provider or database access |
| MCP facade | Authorized agent tools that reuse application services | Revalidates caller and permissions per tool invocation |

The public resource is a **context space**. The engine internally divides a space into **access partitions** so content with incompatible effective readers cannot cross-pollinate derived entities, edges, summaries or answer caches. Access partitions are an engine-owned implementation concept: callers cannot create them, select them, grant access to them or see their identifiers. The policy service derives them from trusted source audience rules, and the knowledge-backend provider maps them to its own isolation primitive. For the initial Cognee provider, that private mapping uses Cognee datasets and dataset permissions. No Cognee dataset name, UUID, permission or default may cross the provider boundary.

## 3. Monorepo structure

```text
context-engine/
├── contracts/
│   ├── openapi/                       # versioned HTTP surface
│   ├── schemas/                       # ingestion events and response objects
│   ├── mcp/                           # tool contracts and examples
│   └── examples/
├── integrations/
│   └── ballerina/
│       ├── common/                    # auth, envelope, retries, checkpoints
│       ├── file-source/               # first end-to-end source
│       └── connectors/                # additional source-specific projects
├── engine/
│   ├── pyproject.toml
│   ├── src/context_engine/
│   │   ├── api/                       # HTTP routes and request validation
│   │   ├── application/               # shared commands and queries
│   │   ├── knowledge_backend/         # engine port and private provider adapters
│   │   ├── ingestion/                 # envelopes, dedup, routing, lifecycle
│   │   ├── provenance/                # source lineage, evidence, traces
│   │   ├── security/                  # identity adapter, ACLs, grants, policy
│   │   ├── persistence/               # ledgers and transactional outbox
│   │   ├── worker/                    # durable job execution/reconciliation
│   │   └── mcp/                       # authorized MCP facade
│   ├── migrations/
│   └── tests/
├── ui/                                # query workbench and access screens
├── tests/
│   ├── contract/
│   ├── end-to-end/
│   ├── isolation/
│   └── recovery/
├── local/                             # reproducible developer stack
├── docs/                              # decisions, API guide, operator runbooks
└── .github/workflows/                 # or the repository's actual CI system
```

Build the `file-source` vertical slice first. Subsequent Ballerina connectors conform to the same envelope and lifecycle contracts. Add connector directories only as each source is implemented.

## 4. Contracts and data model

### 4.1 Ingestion envelope

Version the event contract and require a registered source, stable record identity, operation and source version. For example:

```json
{
  "schemaVersion": "1",
  "spaceId": "space-uuid",
  "sourceId": "source-uuid",
  "sourceRecordId": "record-id-at-source",
  "sourceVersion": "84",
  "operation": "upsert",
  "contentType": "application/pdf",
  "contentRef": "opaque-staged-object-id",
  "sourceUrl": "https://example.invalid/doc/84",
  "contentHash": "sha256:...",
  "sourceObservedAt": "2026-09-18T10:00:00Z",
  "audience": ["source-group:research"],
  "sourceAclVersion": "31",
  "idempotencyKey": "source-uuid:record-id-at-source:84"
}
```

Support `upsert`, `delete` and `acl_changed`. For `delete` and `acl_changed`, omit content as appropriate but require stable target and ordering metadata. Distinguish source event time from time observed by the connector and ingested by the engine. For large bytes, accept a bounded upload or scoped staged object reference, then validate type, size, expiry and content hash. A supplied URL is display provenance only: the engine must not fetch arbitrary URLs on behalf of callers. The authenticated connector identity and configured source binding determine the allowed space and source; the request body cannot select a different one.

The control database starts with `spaces`, `access_partitions`, `backend_bindings`, `sources`, `source_checkpoints`, `source_records`, `record_versions`, `audience_bindings`, `grants`, `jobs`, `job_attempts`, `outbox`, `provenance_links`, `evidence_links`, `query_traces` and `audit_events`. A record ledger maps `(space_id, source_id, source_record_id, version)` to audience, source ACL version, internal access-partition ID, opaque backend record reference, content hash, processing configuration and lifecycle state. Backend bindings and record references are private persistence details exposed only to the knowledge-backend provider. Define unique keys for idempotency and a version-order policy for sources with non-numeric cursors.

### 4.2 Provenance, evidence and traces

| Layer | Minimum information | Where it is captured |
| --- | --- | --- |
| Source provenance | Connector, source and record IDs, version, content hash, original URL/title, observed/ingested time, source ACL version, extraction run | Event and durable source ledger |
| Derived provenance | Internal access-partition ID; opaque backend references; chunk offsets or page; source chunk links; extraction pipeline, prompt/model and embedding version; enrichment job | Knowledge-backend hooks plus provenance/evidence tables; translate provider metadata into engine-owned fields before storage outside the provider module |
| Query evidence | Actual retrieved passages and graph elements used, relevance/route details, source versions, citation IDs and access check result | Query service and evidence store |
| Access decision trace | Principal, action, resource, effective grant IDs/policy version, allow or deny, reason code and timestamp | Authorization engine and audit log |
| Answer decision trace | Requested context space, resolved audience policy, retrieval strategy, evidence IDs, graph hops, thresholds, answer model/prompt version and outcome | Query service; no private chain of thought or backend identifiers |

Do not equate a source URL with evidence. Map every displayed claim or passage to a versioned source and authorized chunk/graph element. The provider must translate its source annotations and edge evidence into the engine evidence model; provider-native metadata and identifiers remain private. Test exactly what the pinned provider returns. A response with missing support must indicate `insufficient_evidence` or suppress the unsupported citation. Evidence detail and traces are separately access checked, including after permissions change. Restrict audit retention and redact sensitive text, secrets, denied document titles and internal tokens.

### 4.3 Authentication and permissions

- Validate OIDC access tokens or service credentials against a configurable identity provider. Map the immutable issuer/subject to an engine principal; do not accept principal IDs supplied by the client. The UI may use a secured browser session backed by the same identity. The engine does not need to store user passwords or mint a competing identity system.
- Represent users, service identities and agents as principals; resolve group and role membership from an adapter or engine-managed assignments. Record the authority and last synchronization time. Use explicit public resource scopes such as `space`, `source` and `record`. Access partitions are internal policy outputs and are never grant targets.
- Engine actions: `space.manage`, `source.manage`, `ingest.write`, `context.read`, `evidence.read`, `trace.read`, `context.enrich`, `record.delete`, and `access.manage`. Keep write, delete and access administration distinct. Roles are named bundles of these actions; grants bind a principal or group to a role or action on an engine resource. Inherited space grants and source restrictions yield effective permissions; explicit deny/source restrictions take precedence if enabled by the chosen policy model.
- Treat connector permissions to **write from a source** separately from permissions for humans or agents to **read the resulting context**. Map trusted source audiences to internal access partitions using configured group mappings. Effective read access requires both an engine grant and membership in the permitted source audience. Reject grants that would widen access beyond source policy; sharing outside that audience requires an explicit, separately authorized source-policy change and reindex. If ACL data is unavailable or cannot be mapped, quarantine that record from general retrieval. Never use a connector's service credential as a query principal.
- An ACL restriction, revoked grant or removed group membership must close query access immediately, including evidence endpoints, in-flight jobs, caches and MCP. Migrate or rebuild affected content under the new audience; until the move is verified, fail closed for the affected records or internal access partition. Broadening access requires an authorized grant and a rebuilt, verified partition. Re-evaluate job permissions when a queued job runs.
- Resolve authorized internal access partitions before invoking the knowledge-backend port. The private Cognee provider must always pass explicit user and dataset context and must reject defaults that select a default user, default dataset, all datasets or elevated access. Cognee permissions provide defense in depth; engine policy remains authoritative. Provider IDs and permissions never appear in application-service results.

## 5. Proposed API surface

All routes are `/v1`, versioned in OpenAPI, authenticated unless explicitly health/discovery endpoints. Mutations are asynchronous where they touch embeddings, graphs or external content: return `202`, a job ID and status URL. Use an idempotency key and stable error codes; never return success for a queued operation that later fails. Public requests and responses contain only engine resource IDs; internal access-partition and backend identifiers are prohibited.

| Area | Proposed routes | Required behavior |
| --- | --- | --- |
| Authentication | `GET /auth/me`; `GET /auth/session`; optional browser login/callback/logout endpoints if the UI hosts sessions | Return current principal, token/session expiry and effective identity; login itself follows the selected OIDC provider. Service credentials use its machine flow. |
| Context spaces | `POST/GET /spaces`; `GET/PATCH/DELETE /spaces/{spaceId}` | Manage the public context boundary and list only visible spaces. The engine derives any internal storage partitions from source audience policy. |
| Sources | `POST/GET /spaces/{spaceId}/sources`; `PATCH /sources/{sourceId}`; `GET /sources/{sourceId}/checkpoints` | Register connector identity, source type and ACL mapping; administrative actions audited. |
| Ingestion | `POST /ingestions`; `POST /uploads`; `GET /jobs/{jobId}` | Authenticated connector delivery; accept bytes or staged content, validate the envelope and queue a normalized record. |
| Update / delete events | `POST /ingestions` with `upsert`, `acl_changed` or `delete`; `GET /sources/{sourceId}/records/{recordId}` | Enforce ordered versions and a visibility barrier while the private provider performs the corresponding update or replacement; return record lifecycle without exposing content or backend operations. |
| Query | `POST /queries`; `GET /queries/{queryId}` | Input question, allowed context-space selection, `mode: context|answer`, bounded limits. Resolve eligible internal partitions server-side and return authorized passages, an answer if requested, citations, evidence IDs, trace ID and `insufficient_evidence` state. |
| Evidence and traces | `GET /queries/{queryId}/evidence`; `GET /evidence/{evidenceId}`; `GET /queries/{queryId}/trace` | Recheck current ACL; provide source version, passage/page and bounded supporting graph path. Show caller-safe answer trace; admin access for security trace. |
| Context enrichment | `POST /spaces/{spaceId}/enrichments`; `GET /jobs/{jobId}` | Authorized, rate-limited asynchronous enrichment of a context space. Resolve internal partitions server-side and record pipeline/model version plus new derived lineage. |
| Record deletion | `DELETE /records/{recordId}`; `GET /jobs/{jobId}` | Explicit engine record target, idempotency and strict `record.delete` permission; remove indexed and derived artifacts plus evidence references, then reconcile completion. No public delete-everything operation. |
| Permission inspection | `GET /auth/permissions?resourceId=...`; `POST /authorization/check` (privileged when checking another principal) | Return effective actions and explainable decision codes, without disclosing inaccessible resources. |
| Grant management | `GET /resources/{resourceId}/grants`; `PUT /resources/{resourceId}/grants/{grantId}`; `DELETE /resources/{resourceId}/grants/{grantId}` | `access.manage` required; grant/revoke principal or group roles, audit actor/reason, update internal policy mappings as needed and invalidate caches. |
| Audit and operations | `GET /audit-events` (privileged); `GET /health/live`; `GET /health/ready` | Filter/redact audit by scope; readiness checks worker dependencies without exposing credentials. |

The canonical ingestion contract keeps Ballerina events, uploads and retries consistent. Do not add aliases based on provider terminology. Only expose auth token creation/revocation APIs if this engine explicitly owns service credentials; otherwise use the chosen identity provider's credential lifecycle.

### MCP surface

Build an engine-owned MCP server using the same application services as HTTP. Start with `list_context_spaces`, `query_context`, `get_evidence` and `explain_query` (the last returns the caller-safe answer trace). Add `ingest_context`, `enrich_context` and `delete_context_record` only for principals granted their corresponding actions; these return job handles. Support an authenticated remote transport according to a pinned MCP specification and test with a real MCP client. Validate authentication and authorization on **every tool call and resource read**, including repeated use of handles. Bind job and query handles to the requesting principal and resource; do not allow internal partition selection, raw graph access or global deletion. Do not expose any provider-supplied MCP service, tool name, resource or schema as the public surface.

## 6. Milestones and acceptance gates

### M0 — Architecture spike and contracts

**Implement:** Define the engine-owned knowledge-backend port first, with provider-neutral ingest, query, enrich, update and delete operations plus engine evidence and error types. Pin Cognee and its transitive dependencies only inside the private provider implementation. Verify the provider mapping for explicit identity and isolation context, stable record identity/metadata, evidence output, graph element links, update behavior, custom Python tasks and simultaneous restricted access partitions against the selected real stores. Write ADRs for provider combination, source ACL routing, graph derivation boundary, identity mode, authoritative ledger, deletion semantics and the anti-corruption boundary. Draft OpenAPI, event schema, MCP contracts and threat model, then check that none contains provider terminology or identifiers.

**Gate:** A two-audience fixture cannot cross-read raw chunks, shared entities, graph paths or citations. A deleted record cannot be found through any index or cached answer. Any failed assumption has a recorded fallback before M1.

### M1 — Runnable engine and durable control plane

**Implement:** Create the monorepo skeleton, local stack, pinned builds, migrations, API/worker entrypoints, typed domain models, transaction/outbox handoff, job state machine, retry policy and CI contract checks. Model failure after queue acceptance and partial backend writes. Add trace IDs, metrics and secret-safe structured logs. Add an architectural test that prevents API, application, domain, connector, UI and MCP packages from importing the Cognee provider or serializing its types. Add a contract lint rule that rejects provider terminology and backend identifiers in OpenAPI, event schemas, MCP schemas and examples.

**Gate:** API and worker start independently; database migrations and OpenAPI validation run in CI; replaying a job after an injected worker crash neither duplicates a record nor reports false completion.

### M2 — Authentication, resource scopes and grants

**Implement:** Add pluggable token verifier, principal/group mapping, policy decision function, internal access-partition-to-audience binding and grant APIs. Cover `context.read`, `ingest.write`, `context.enrich`, `record.delete`, `access.manage`, `trace.read` and `evidence.read` separately. Record access decisions with reason codes. Add negative tests for forged principal fields, expired tokens, lost group membership, privilege escalation via grant, and permission change between job enqueue and execution.

**Gate:** Grant, query and revoke a role; the same principal loses HTTP, UI and MCP read access on revocation. Every provider call is made through the knowledge-backend port with explicit authorized identity and internal access partitions. Denied access reveals no restricted title, passage, entity, partition, backend identifier or job details.

### M3 — Ballerina source integration and ingestion lifecycle

**Implement:** Build reusable Ballerina client/envelope/checkpoint helpers and one file-source connector end to end. Add initial scan, incremental upsert, delete and ACL change delivery, source-version ordering, exponential retry and dead-letter inspection. Implement registration, upload/staging, content validation, idempotency, source ledger and status APIs. Make extraction explicit about parser version and original page/offset where available.

**Gate:** Initial sync and re-sync converge to one active record per source version. Duplicate and out-of-order events do not resurrect deleted content. A failed upload is retryable, a malformed/oversized file is rejected, and a bad audience goes to quarantine.

### M4 — Knowledge-backend pipeline and graph lifecycle

**Implement:** Implement the knowledge-backend port and the private Cognee provider with controlled metadata and explicit isolation/identity mapping, then translate returned data, chunk and graph references into engine-owned provenance types. Support update and delete through measured provider operations and ledger reconciliation. Make enrichment a deliberate versioned engine job; record created artifacts and invalidate stale answer caches. Translate provider errors into stable engine errors. Separate provider configuration from application code, add bounded concurrency and operation timeouts.

**Gate:** Query the ingested record through its context space; enrichment creates traceable derived data; replacing the record removes or supersedes old answers; deleting a specific record removes or marks inaccessible all corresponding searchable artifacts. Swapping in a test backend requires no change to application services or public contracts. No HTTP, MCP, UI or event payload exposes Cognee terminology, identifiers or errors.

### M5 — Provenance, evidence, mediated query and traces

**Implement:** Define evidence/citation response types and source-version lineage. Implement context-only retrieval and answer generation through one query service. Persist bounded query traces, engine graph element references and actual evidence; reconstruct safe passages and graph views from server-resolved access partitions. Recheck evidence permissions on access. Show no-evidence and partial-evidence behavior. Add indexing for trace search and a retention policy.

**Gate:** Each returned citation opens to the exact authorized source version and chunk/page. Queries spanning disjoint audiences return only eligible evidence and derived graph paths. An unsupported answer does not present invented citations. Two traces are available: an access decision to authorized administrators and a caller-safe explanation of evidence selection.

### M6 — HTTP, MCP and query UI

**Implement:** Complete the API table and publish OpenAPI and examples. Implement the MCP facade with read tools first, then guarded write tools. Build a query workbench with a context-space selector, context versus answer mode, inline source citations, evidence drawer, bounded graph view, query trace, jobs/status and a grant/revoke screen. Use one identity/session flow and the same backend checks for UI, HTTP and MCP.

**Gate:** A user can ingest through the Ballerina connector, query in the UI and through HTTP/MCP, open a citation and trace, grant a second user read permission and revoke it. A second user cannot access the same evidence, query handle or source link after revocation. Tool responses and UI screens expose neither internal access partitions nor provider details.

### M7 — Engine readiness and recovery

**Implement:** Add isolation, authorization, lifecycle and retrieval-quality fixtures; concurrency/load budgets; retry/backpressure and poison-event behavior; backups/restore tests for the control database and provider stores; reconciliation for drift between the ledger and graph/vector indexes; audit and trace retention; dependency scanning and a private-provider upgrade playbook. Document provider and model versioning, cost/latency metrics, reindex procedure and operational failure handling without leaking provider fields into public telemetry.

**Gate:** Automated tests cover token expiry, source ACL tightening, deleted-source resurrection, permission revocation during a running query, partial job failure and restore/rebuild. A clean environment can be brought up from documented instructions; a restored environment passes isolation and citation checks. Record measured throughput, retrieval quality and query latency targets before declaring the engine ready.

## 7. Definition of done for the first release

- A Ballerina source delivers creates, updates, deletes and ACL changes with durable checkpoints; the Python worker consistently reflects them in the correct internal access partitions.
- Ingestion, query, enrichment and deletion run through the engine-owned knowledge-backend port with explicit identity and isolation context. The Cognee provider is version-pinned and private; public contracts, persistence-facing application types, telemetry, UI and MCP expose no Cognee terminology, identifiers, errors or defaults.
- Every returned evidence item is tied to source version and passage or graph lineage; query explanations and access decision traces reveal only authorized information.
- Authentication, grants and revocations produce identical effective decisions for HTTP, UI and MCP; cross-audience and stale-access tests pass.
- The UI supports querying, citations, provenance, job visibility and controlled sharing. API and MCP examples work against a clean local stack.
- Recovery and deletion are demonstrated across relational, graph, vector, caches and evidence indexes; any unsupported erasure or ACL guarantee is explicitly blocked from release.

## 8. Private Cognee provider references

Use the following only to implement and verify the private provider. Their names and concepts must be translated at the knowledge-backend boundary and must not appear in public contracts, UI, MCP, Ballerina integrations, application services or domain types.

- [Cognee: Remember](https://docs.cognee.ai/core-concepts/main-operations/remember) — permanent ingestion, `DataItem`, user/dataset behavior and default improvement.
- [Cognee: ACL and dataset permissions](https://docs.cognee.ai/core-concepts/multi-user-mode/permissions-system/acl) — dataset-scoped access model.
- [Cognee: Search basics](https://docs.cognee.ai/guides/search-basics) — `recall`, context-only retrieval and evidence options.
- [Cognee: Forget](https://docs.cognee.ai/core-concepts/main-operations/forget) — item/dataset deletion and cache behavior.
- [Cognee: Custom pipelines](https://docs.cognee.ai/guides/custom-tasks-pipelines) — Python extension points.
- [Cognee: MCP tools](https://docs.cognee.ai/cognee-mcp/mcp-tools) — compare built-in tool coverage with the engine-owned MCP contract.

Document verified SDK signatures, provider versions, term mappings and test findings in provider-specific ADRs under `docs/decisions/` when implementation begins; upstream documentation can change. Keep those ADRs explicitly marked as private provider implementation material.

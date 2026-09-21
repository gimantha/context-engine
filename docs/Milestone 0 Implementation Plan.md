# Milestone 0 — Architecture Spike and Contracts Implementation Plan

**Status:** Proposed

**Parent plan:** [Context Engine — Standalone Implementation Plan](./Devant%20Context%20Engine%20Implementation%20Plan.md)

**Milestone:** M0 — Architecture spike and contracts

**Primary outcome:** Prove the security and lifecycle assumptions on which M1 depends, and publish provider-neutral contracts that later milestones can implement without exposing the knowledge backend.

## 1. Objective

Milestone 0 must answer the high-risk architecture questions before production implementation begins:

1. Can one context space safely contain records for multiple audiences without evidence, graph relationships, summaries, caches or errors crossing audience boundaries?
2. Can the engine ingest, query, update, enrich and delete content through a provider-neutral Python port?
3. Can a record be updated or deleted without stale searchable or derived artifacts remaining accessible?
4. Can the engine preserve source-to-evidence lineage using stable engine identifiers while keeping provider identifiers private?
5. Can HTTP, event and MCP contracts describe the product entirely with engine-owned concepts?
6. Is the selected relational, vector, graph, embedding and model configuration reproducible and operable as one tested combination?

M0 ends with a go, conditional-go or stop decision for M1. A conditional-go decision must name the failed assumption, the chosen fallback and the work that M1 must carry forward.

## 2. Non-negotiable boundaries

- The public resource is a **context space**. Callers do not create, select or inspect internal access partitions.
- `engine/src/context_engine/knowledge_backend/` owns the only interface through which application code may use a knowledge provider.
- Public APIs, schemas, MCP tools, UI copy, Ballerina integrations, domain types, application-service results, errors, logs and metrics use engine-owned terminology.
- Provider package imports, native types, native identifiers, native permissions and native operation names remain inside the private provider implementation and its tests.
- The policy service resolves authorized access partitions before a backend call. The backend cannot choose a broader default.
- Every query, ingestion, enrichment, update and deletion call carries explicit engine identity and isolation context.
- Source credentials grant delivery rights only. They never grant human or agent read rights.
- Unknown or unmappable source audiences fail closed and enter quarantine.
- M0 may contain throwaway spike code, but its contracts, ADRs, fixtures and evidence must be retained and reviewed.

## 3. Required deliverables

| Deliverable | Expected location | Completion evidence |
| --- | --- | --- |
| Engine glossary and provider-boundary rules | `docs/decisions/0001-knowledge-backend-boundary.md` | Approved terms, dependency rules and explicit prohibited leaks |
| Knowledge-backend port prototype | `engine/src/context_engine/knowledge_backend/` | Provider-neutral types and a dummy backend pass contract tests |
| Private Cognee provider spike | Private provider package below `knowledge_backend/` | Port contract tests run against the pinned real provider |
| Reproducible dependency set | `engine/pyproject.toml` and lock file | Clean environment installs the exact tested versions |
| Provider/store configuration decision | `docs/decisions/0002-provider-and-store-topology.md` | Selected topology, tested versions, rejected alternatives and evidence |
| Access and derivation decisions | ADRs 0003–0004 | Audience routing and graph derivation boundaries are explicit |
| Identity, ledger and deletion decisions | ADRs 0005–0007 | Identity propagation, source of truth and erasure behavior are explicit |
| Evidence translation decision | `docs/decisions/0008-evidence-model.md` | Provider output maps to engine evidence without leaking native IDs |
| Draft HTTP contract | `contracts/openapi/` | OpenAPI validation passes and provider-leak lint passes |
| Draft ingestion/event schemas | `contracts/schemas/` | Valid and invalid examples are tested |
| Draft MCP contracts | `contracts/mcp/` | Tool schemas validate and reuse engine-owned application operations |
| Two-audience golden fixture | `tests/isolation/fixtures/` | Repeatable fixture covers shared entities and distinct secrets |
| Spike and test report | `docs/m0/spike-report.md` | Commands, versions, results, failures and measurements are recorded |
| Threat model | `docs/m0/threat-model.md` | Threats, trust boundaries, mitigations and residual risks are reviewed |
| M0 decision record | `docs/m0/exit-decision.md` | Every exit criterion has evidence and an owner for open risks |

File names may follow repository conventions discovered during implementation, but the information and acceptance evidence are required.

## 4. Work packages

### M0.1 — Inventory assumptions and freeze terminology

Create a short inventory before writing the port:

- Python and Ballerina versions and package/build tooling.
- Candidate relational, vector and graph stores.
- Embedding and answer-model providers, model versions and credential requirements.
- Identity-provider assumptions and the minimum principal fields required by the engine.
- Local services needed for the spike and their persistence locations.
- Provider features that need verification: explicit identity, isolation, ingestion, query, evidence, update, enrichment, deletion, caching and concurrent requests.

Define the engine glossary at the same time:

| Engine term | Meaning |
| --- | --- |
| Context space | Public container for governed context |
| Source | Registered origin that delivers versioned records |
| Source record | Engine identity for one logical item at a source |
| Access partition | Internal isolation unit derived from compatible source audiences |
| Evidence | Authorized, source-linked support returned by a query |
| Enrichment | Explicit engine job that produces additional derived context |
| Backend reference | Opaque, private handle stored for reconciliation and never serialized publicly |

Add a prohibited-terms rule for public contracts. The rule must cover the provider product name, native type names, native operation names, native isolation terminology and native identifiers. Provider-specific ADRs and provider tests are the only permitted exceptions.

**Done when:** the glossary, dependency direction and exceptions are recorded in ADR 0001 and accepted before public schema design begins.

### M0.2 — Define the knowledge-backend port

Prototype a Python port with engine-owned request and result types. The exact Python shape may change, but it must cover these operations:

```text
ingest(record, principal_context, access_partition) -> ingestion_result
query(request, principal_context, authorized_partitions) -> query_result
update(record, principal_context, source_binding) -> update_result
enrich(request, principal_context, authorized_partitions) -> enrichment_result
delete(record_ref, principal_context, access_partition) -> deletion_result
health() -> backend_health
```

Define at least these provider-neutral types:

- `PrincipalContext`: immutable engine principal and request/trace correlation.
- `AccessPartitionRef`: internal engine identifier, unavailable to API serializers.
- `SourceRecord`: stable engine record ID, source version, normalized content reference and source metadata.
- `EvidenceItem`: engine evidence ID, source record/version, passage or page location and engine graph references.
- `BackendReference`: opaque value restricted to persistence and provider packages.
- `BackendCapabilities`: explicit supported operations and safety characteristics.
- `BackendError`: stable categories such as unavailable, timeout, invalid input, conflict, partial write and unsupported operation.

The port must not accept an optional identity or optional isolation context for data operations. It must not expose a method that queries all content, deletes all content or returns raw graph/provider objects. Results must be copied into engine-owned immutable types before leaving the provider module.

Build a deterministic dummy backend and a shared contract-test suite. The dummy proves that application code can use the port without importing the real provider.

**Done when:** the dummy backend passes the shared contract tests, forbidden imports are absent outside the provider package and serializers cannot encode `BackendReference` or `AccessPartitionRef`.

### M0.3 — Pin and implement the private provider spike

Keep this work private to the provider package and provider-specific ADRs.

1. Pin the Cognee package and transitive dependencies in a lock file.
2. Record exact Python, provider, store, embedding-model and answer-model versions.
3. Implement the port mapping for ingest, query, update, enrichment and deletion.
4. Disable or explicitly configure implicit enrichment and session behavior.
5. Reject missing identity or isolation context before calling provider code.
6. Translate provider errors into `BackendError` categories.
7. Translate native evidence and graph metadata into engine evidence types.
8. Store native identifiers only as opaque backend references.
9. Capture partial-write behavior across relational, vector and graph stores.
10. Verify cleanup requirements when an operation times out or fails partway through.

For every mapped operation, record:

- Inputs supplied by the engine.
- Native calls made by the provider adapter.
- Stores changed.
- Stable identifiers returned after translation.
- Observable partial-failure states.
- Retry and idempotency behavior.
- Cleanup or reconciliation needed.
- Whether caches or derived artifacts can retain stale content.

**Done when:** the real provider passes the shared port contract tests and the spike report contains no unexplained provider behavior.

### M0.4 — Prove isolation with a two-audience fixture

Create one context space with three audience classes:

- Audience Alpha can read an Alpha-only incident record.
- Audience Beta can read a Beta-only incident record.
- Both audiences can read a shared runbook.

Use deliberately overlapping entities in all records, such as the same gateway, region and service name. Put a distinct canary fact in each restricted record. Questions must attempt to retrieve both direct text and derived relationships involving those shared entities.

Run this matrix through the knowledge-backend port:

| Scenario | Required result |
| --- | --- |
| Alpha queries its canary | Alpha evidence is returned with the correct source version |
| Alpha queries Beta's canary | No Beta title, passage, entity, relationship, citation or error detail appears |
| Beta queries Alpha's canary | No Alpha information appears |
| Either audience queries the shared runbook | Shared evidence is returned |
| Query spans shared and restricted entities | Every returned path and passage remains inside the caller's authorized partitions |
| Same request runs concurrently as Alpha and Beta | Results remain isolated and carry the correct trace/principal |
| Alpha access is revoked between two queries | The second query and evidence lookup fail closed |
| A restricted record moves to a narrower audience | The old audience loses access before migration is reported complete |
| Query has no authorized evidence | The engine returns `insufficient_evidence` without restricted hints |
| Provider call omits identity or isolation context | The adapter rejects it before execution |

Inspect raw chunks, graph nodes/edges, summaries, citations, caches, traces and error payloads. A query-only check is insufficient.

**Done when:** the entire matrix passes repeatedly, including concurrent runs, or the exit decision selects and validates a safer isolation fallback.

### M0.5 — Prove record lifecycle and deletion

Use stable engine record IDs and ordered source versions to exercise:

1. Initial ingestion.
2. Identical replay.
3. Newer-version update.
4. Out-of-order older update.
5. Audience restriction.
6. Audience expansion after authorization.
7. Explicit enrichment.
8. Specific-record deletion.
9. Retry after an injected partial failure.

After an update, the old content must not be returned as a passage, graph path, summary, citation or cached answer. After deletion, search all relevant stores and caches using the old text, record ID, source URL, canary fact and derived entities. Record whether the provider guarantees physical erasure, logical inaccessibility or eventual cleanup; do not claim a stronger guarantee than the evidence supports.

If safe in-place update cannot be proven, select a visibility-barrier plus reindex strategy. If deletion leaves unrecoverable searchable artifacts, M0 cannot pass without a containment design that blocks them from every read path.

**Done when:** replay, ordering, update and delete outcomes are repeatable, and ADR 0007 defines the supported guarantee and reconciliation behavior.

### M0.6 — Draft provider-neutral contracts

Draft enough contract surface to validate naming, identity, asynchronous jobs and evidence behavior. Implementation is not required in M0.

The HTTP draft should cover:

- Context-space creation, inspection and deletion.
- Source registration and checkpoint inspection.
- Ingestion and upload acceptance.
- Job status.
- Query and query status.
- Evidence and caller-safe query trace retrieval.
- Context-space enrichment.
- Specific-record deletion.
- Permission inspection and grant management.
- Health and readiness.

The event schema must require a stable space, source, source record, source version, operation, audience, source ACL version and idempotency key. The authenticated connector binding remains authoritative for the allowed space and source.

The MCP draft should begin with `list_context_spaces`, `query_context`, `get_evidence` and `explain_query`. Any write tool must return an engine job handle and use the same application operation as HTTP.

Contract rules:

- No internal access-partition identifier is accepted or returned.
- No backend identifier, error, type, operation or configuration is accepted or returned.
- Mutations that touch external content or indexes return `202` with an engine job ID.
- Query evidence uses engine evidence IDs and source lineage.
- No-evidence behavior is explicit and stable.
- Error codes reveal no inaccessible resource names or content.
- Public examples contain both allowed and denied cases.

Add automated schema validation and a provider-leak lint check over OpenAPI, event schemas, MCP schemas and public examples.

**Done when:** all draft contracts validate, the leak check passes and each operation maps to an engine application command or query without reference to a provider API.

### M0.7 — Threat model and authorization spike

Document trust boundaries for callers, browser sessions, service identities, Ballerina connectors, staged objects, API/worker communication, the control database, queues, model providers and knowledge stores.

At minimum, analyze:

| Threat | Required mitigation to validate |
| --- | --- |
| Caller supplies another principal ID | Principal comes only from verified authentication context |
| Connector selects another space/source | Authenticated connector binding overrides request claims |
| Backend default broadens a query | Adapter requires explicit identity and isolation context |
| Restricted content influences shared derived data | Derivation runs inside compatible access partitions |
| Revocation races a query or queued job | Permission is rechecked at execution and evidence access time |
| Cached answer survives revocation | Cache key and lookup include policy/identity scope; revocation invalidates or bypasses it |
| Staged object reference is replayed or substituted | Scope, expiry, size, type and content hash are verified |
| Error or trace reveals restricted content | Stable redacted engine errors and separately authorized traces |
| Native provider ID reaches a public payload | Type boundary, serializer tests and contract lint reject it |
| Partial write creates orphaned searchable data | Ledger state, reconciliation and fail-closed visibility barrier |
| Model prompt or telemetry exports sensitive text | Minimize/redact payloads and document provider retention settings |

Prototype the authorization decision as a pure function using principal, action, engine resource, source audience and policy version. It should return allow/deny plus a reason code and resolved internal partitions. Do not implement full identity-provider integration in M0.

**Done when:** the threat model identifies an owner and mitigation for every high-severity threat, and the isolation/lifecycle tests exercise the security-critical mitigations.

### M0.8 — Write and approve architecture decisions

Create these ADRs with status, context, decision, alternatives, consequences, validation evidence and rollback/fallback:

1. Knowledge-backend boundary and engine terminology.
2. Provider and relational/vector/graph store topology.
3. Source-audience routing and access-partition sizing.
4. Graph derivation and cache isolation boundary.
5. Principal mapping and provider identity propagation.
6. Authoritative ledger, idempotency and version ordering.
7. Update, deletion and reconciliation semantics.
8. Evidence/provenance translation and safe query traces.

An ADR is not accepted if it relies only on upstream documentation for a security or lifecycle guarantee. Link the relevant executable test and spike result.

**Done when:** all eight decisions are accepted or explicitly marked blocked with an implemented, tested fallback.

## 5. Execution order

1. Complete M0.1 and approve the glossary/boundary.
2. Implement the provider-neutral port and dummy backend in M0.2.
3. Draft contracts against the engine port in M0.6.
4. Pin the real provider and implement the private spike in M0.3.
5. Run isolation and lifecycle work in M0.4–M0.5 against the real store combination.
6. Update the threat model and authorization spike in M0.7 with observed behavior.
7. Finalize ADRs and the spike report in M0.8.
8. Run the complete M0 gate and record the exit decision.

Do not finalize public contracts around behavior that has not passed the real-provider spike. Do not begin the M1 control-plane implementation while an isolation or deletion assumption remains unresolved.

## 6. Validation and evidence

Every spike run recorded in `docs/m0/spike-report.md` must include:

- Commit or working-tree identifier.
- Date and operator.
- Python and dependency-lock hashes.
- Provider, store and model versions.
- Configuration profile with secrets removed.
- Exact test command.
- Fixture version and content hashes.
- Pass/fail result and elapsed time.
- Observed store changes and cleanup result.
- Relevant trace IDs with sensitive data redacted.
- Known nondeterminism and rerun count.

Tests must fail if:

- A backend call lacks explicit identity or isolation context.
- A caller can select an internal partition.
- Any provider-native field is serialized by a public contract.
- A denied query reveals a restricted title, passage, entity, citation, graph path, trace or error detail.
- A deleted record remains retrievable through any supported read path.
- An older source version replaces or resurrects a newer/deleted record.

## 7. Exit gate

M0 passes only when all of the following are true:

- [ ] The knowledge-backend port uses only engine-owned types and supports a dummy and real provider.
- [ ] The real provider and all transitive dependencies are pinned reproducibly.
- [ ] Every real-provider data operation has explicit identity and isolation context.
- [ ] The two-audience fixture cannot cross-read chunks, entities, graph paths, summaries, citations, caches, traces or errors.
- [ ] Concurrent requests preserve principal and audience isolation.
- [ ] Update and out-of-order event behavior is measured and documented.
- [ ] Specific-record deletion removes or makes inaccessible every supported searchable and derived artifact.
- [ ] Evidence maps to stable engine source/version identifiers without exposing backend references.
- [ ] OpenAPI, event and MCP drafts validate and pass provider-leak linting.
- [ ] The threat model has no unowned high-severity risk.
- [ ] All required ADRs contain linked executable evidence.
- [ ] Failed assumptions have tested fallbacks.
- [ ] The exit decision states go, conditional-go or stop and names the reviewers.

The primary security gate is binary: if content or derived information crosses an audience boundary, M0 fails.

## 8. Preselected fallbacks

Use these fallbacks when a preferred behavior cannot be proven:

| Failed assumption | Fallback to test |
| --- | --- |
| Graph derivation cannot be isolated within one context space | Separate backend isolation units per compatible audience and prohibit cross-partition derivation |
| Provider identity context is unsafe or implicit | Use engine-controlled service identity plus mandatory engine-side partition filtering, then verify provider isolation as defense in depth |
| In-place update leaves stale artifacts | Apply a visibility barrier, delete/reindex into a new backend binding and atomically switch the ledger |
| Specific-record deletion is incomplete | Quarantine the containing internal partition, rebuild allowed records into a clean binding and retire the old binding |
| Enrichment mixes incompatible audiences | Disable enrichment until it can run independently per access partition |
| Provider evidence lacks stable lineage | Maintain engine-owned chunk/source mappings during ingestion and suppress unsupported citations |
| Store combination fails concurrent isolation | Change the provider/store topology and rerun the complete gate |

A fallback is acceptable only after it passes the same isolation, lifecycle and evidence tests as the preferred design.

## 9. Out of scope for M0

- Production-ready API and worker processes.
- Full control-database migrations and durable outbox implementation.
- Complete OIDC integration or browser sessions.
- Production Ballerina connectors.
- Query UI implementation.
- Full MCP server transport.
- Production deployment, autoscaling, backup automation and SLOs.
- Performance optimization beyond recording a repeatable baseline and identifying obvious blockers.

M0 may create minimal harnesses for these areas only when needed to prove a contract or security property.

# M2 Authentication, Resource Scopes, and Grants

**Status:** Implemented for the static authentication mode

**Date:** 2026-09-23

**Release limitation:** Live-provider security verification from M0 remains open; identity-provider verifiers arrive in M6 (ADR 0009)

## Delivered behavior

Every REST route except the two health checks requires a bearer credential. The API builds an `AuthenticatedPrincipal` from the verified credential in one dependency and passes it to `ContextEngineService`. No request body, query, or custom header can name the caller's identity. A principal identifier in a body is only ever the target of a grant.

- `TokenVerifier` protocol in `engine/src/context_engine/security/identity.py` with a static pre-shared-token implementation loaded from `CONTEXT_ENGINE_STATIC_TOKENS_PATH`. Tokens are compared in constant time per entry, must be at least 16 characters, and may carry an expiry, groups, and bootstrap actions. A missing file yields an empty verifier so every request fails closed.
- `CONTEXT_ENGINE_AUTH_MODE` accepts `static`. Any other value refuses to start. Static mode refuses to serve on a non-loopback address.
- Principals are keyed on `(issuer, subject)` and receive an opaque `prn_` identifier. Email is an attribute that may change without changing the principal. Static identities are provisioned at startup so grants can target them before their first request.
- Grants bind a principal or a group to actions on a resource. The root resource `engine` is the parent of every context space, so root grants apply engine-wide. Identities with `bootstrapActions` receive a one-time root grant at first start; later edits through the API are never overwritten.
- The `Authorizer` resolves effective actions from grants on a resource and its ancestors, records every explicit decision in `access_decisions` with a reason code (`allowed`, `action_not_granted`, `no_grants`, `resource_not_found`) and the policy version, and emits an allowlisted log event. Visibility checks used for not-found answers are not recorded.
- A monotonic policy version in `policy_state` increments inside the same transaction as every grant change.
- The worker re-decides the job's required action for the accepting principal before applying any effect. A denied job fails terminally with `authorization_revoked` and no ledger effect is written.

## REST surface

| Route | Requirement |
| --- | --- |
| `GET /v1/health/live`, `GET /v1/health/ready` | None |
| `GET /v1/auth/me` | Verified credential |
| `GET /v1/auth/permissions?resourceId=` | Any action on the resource, otherwise not-found |
| `POST /v1/spaces` | `space.manage` on `engine` |
| `GET /v1/spaces` | Lists only spaces on which the caller holds some action |
| `GET /v1/spaces/{spaceId}` | Any action on the space, otherwise not-found |
| `POST /v1/ingestions` | `ingest.write` on the space; invisible space is not-found |
| `GET /v1/jobs/{jobId}` | Accepting principal, or `ingest.write` or `space.manage` on the job's space |
| `GET /v1/resources/{resourceId}/grants` | `access.manage` on the resource |
| `PUT /v1/resources/{resourceId}/grants/{grantId}` | `access.manage` on the resource; body names exactly one of `principalId` or `group` and only engine actions |
| `DELETE /v1/resources/{resourceId}/grants/{grantId}` | `access.manage` on the resource |

Denials follow one rule. A resource the caller holds no action on answers `404 not_found`. A visible resource the caller lacks the specific action on answers `403 access_denied` with the fixed message from the contract example. Missing or unverifiable credentials answer `401 unauthenticated` with a `WWW-Authenticate: Bearer` header. None of these reveal names, content, or existence.

## Persistence

Migration `engine/migrations/0002_identity_and_grants.sql` adds `principals`, `grants`, `access_decisions`, `policy_state`, and a `principal_id` column on `jobs`. Grants are keyed on `(resource_id, id)` so grant identifiers are caller-chosen and scoped to a resource.

## Acceptance evidence

| M2 gate | Result | Evidence |
| --- | --- | --- |
| Grant, read, revoke closes access | Pass | `test_api.py::test_grant_read_and_revoke_closes_access` |
| Same principal loses access on every existing surface | Pass for REST and worker | Grant revocation in `test_api.py`; worker denial in `tests/recovery/test_worker_reauthorization.py` |
| Denied access reveals no title, passage, entity, partition, backend id, or job detail | Pass | Not-found for invisible resources, fixed access-denied message, generic 401 in `test_api.py` |
| Forged principal fields rejected | Pass | Ingestion body with `principalId` is rejected with 400; identity comes from the credential only |
| Expired tokens rejected | Pass | `test_identity.py::test_static_verifier_rejects_unknown_and_expired_tokens` |
| Lost group membership closes access | Pass | REST in `test_api.py::test_group_grant_and_lost_membership`; worker in `test_worker_reauthorization.py` |
| Privilege escalation via grant rejected | Pass | Reader cannot grant on its space or on `engine`, unknown target and both-subject bodies rejected |
| Permission change between enqueue and execution stops the job | Pass | Revoked grant fails the job with no ledger effect and no retry |
| Access decisions recorded with reason codes | Pass | `test_authorization.py::test_decisions_record_reason_codes_and_inherit_root_grants` |
| Every provider call carries explicit identity and partitions | Unchanged from M0 | Port signatures and adapter tests; no provider-backed execution exists yet |

Validation performed on 2026-09-23 from `engine/`:

```text
ruff format --check: passed
ruff check: passed
pytest -m "not live_provider": 50 passed, 1 provider-extra check skipped, 1 live test deselected
context-engine-api --check: passed
context-engine-worker --check: passed
context-engine-api --host 0.0.0.0 in static mode: refused to start
context-engine-migrate: applied 2 migrations to a fresh database
provider boundary check: passed
```

## Deliberately not in this slice

- **Access-partition-to-audience binding.** The plan lists it under M2, but the partition key depends on whether a record's readers are the flat `audience` list in today's envelope or a structured allow list with users, groups, domains, and intersections. That decision is open. The existing pure `authorize` function for partitions is unchanged and still tested with the dummy backend.
- **Identity-provider verifiers.** Organization OIDC tokens, gateway assertions, connection service credentials, and agent actor claims are M6 work behind the same protocol (ADR 0009).
- **Source bindings for service principals.** A connector's credential is checked for `ingest.write` on the space. Binding it to registered sources arrives with source registration in M3.
- **Metrics endpoint authentication.** `/internal/metrics` remains unauthenticated and excluded from the public schema, as in M1. It exposes counters only.
- **Query, evidence, UI, and MCP surfaces.** They do not exist yet. When they arrive they use the same application service and authorizer, which is how the gate's "every surface" clause will be met.

## Operational notes

- `local/m2.env.example` and `local/m2.static-tokens.example.json` are the local profile. Copy the token file to `engine/.context-engine/static-tokens.json` and replace every placeholder before use; the directory is ignored by Git.
- Startup logs `static_tokens_missing` when the file is absent and `bootstrap_grant_created` when a root grant is provisioned. Neither event carries a token.
- `access_decisions` grows by one row per explicit decision. A retention policy belongs with the audit work in M7.

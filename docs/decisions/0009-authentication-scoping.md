# ADR 0009: Authentication scoping across milestones

**Status:** Accepted

**Date:** 2026-09-23

## Context

Milestone 2 must enforce identity, grants, and policy on every public route, but the identity-provider integrations for the target platform (organization OIDC tokens, gateway-issued assertions, connection service credentials, and agent actor claims) are not needed until the UI, connector, and MCP surfaces exist. Waiting for them would delay grants and policy enforcement. Shipping an "authentication off" switch instead would create a request path with no identity, which AGENT.md, ADR 0005, and threat T01 forbid.

## Decision

The engine depends on a `TokenVerifier` protocol and never on a specific identity provider.

- Milestone 2 ships identity, principals, grants, policy enforcement, access-decision recording, and worker-time reauthorization using a **static verifier**. The static verifier maps pre-shared tokens from a local configuration file to fixed identities. Tests and local runs need no identity provider, network, or credentials.
- The identity-provider verifiers are deferred to Milestone 6, where the UI, connectors, and MCP facade need them. They are swap-in implementations of the same protocol.
- There is **no mode without a verifier**. `CONTEXT_ENGINE_AUTH_MODE` accepts `static` now and will accept the provider-backed mode later. Any other value refuses to start. Static mode refuses to serve on a non-loopback address.
- Every request builds its `PrincipalContext` from the verified token. No schema carries the caller's own identity. A principal identifier in a request body is only ever the target of a grant.
- Principals are keyed on the pair `(issuer, subject)` and receive an opaque engine identifier. Email is an attribute used for later identity joins, never the key.

## Consequences

- The M2 grant, revoke, and denial gate is proven with the static verifier. That proof does not depend on who signed the token.
- Local `curl` calls need an `Authorization: Bearer` header from the token file.
- Group membership in M2 comes from the static identity. The provider-backed mode will source it from identity claims or a directory sync.
- When the provider-backed verifiers arrive, the principal resolver may need an issuer alias table because the same person can appear under two issuers.

## Validation

- `engine/tests/test_identity.py`
- `engine/tests/test_authorization.py`
- `engine/tests/test_api.py`
- `tests/recovery/test_worker_reauthorization.py`

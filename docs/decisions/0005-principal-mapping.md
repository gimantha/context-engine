# ADR 0005: Principal mapping and provider identity

**Status:** Accepted for the engine model; provider validation pending

**Date:** 2026-09-18

## Context

Accepting a principal identifier from a request body or allowing the provider's default user would permit confused-deputy and cross-tenant access.

## Decision

The engine constructs `PrincipalContext` only from authenticated request or job context. It contains an immutable engine principal ID and trace ID. Public payloads cannot override it.

The private provider owns the mapping from engine principal to native user. Every native data operation receives the resolved user explicitly. The adapter rejects empty principal or isolation context and never uses a default user.

Queued jobs re-resolve authorization and native identity when execution begins. Connector service identities can ingest only for registered sources and are never query principals.

## Consequences

- Native user IDs remain private provider state.
- Identity mapping must be durable and unique in M1.
- Revocation is enforced by engine policy even when provider permissions lag.

## Validation

- Required parameters in `KnowledgeBackend`.
- Empty-scope tests in `engine/tests/test_backend_contract.py` and `test_cognee_adapter.py`.
- Pure decision function in `engine/src/context_engine/security/policy.py`.

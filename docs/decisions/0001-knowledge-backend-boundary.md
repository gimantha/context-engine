# ADR 0001: Knowledge-backend boundary

**Status:** Accepted

**Date:** 2026-09-18

## Context

The engine needs a knowledge graph implementation without making its product language, contracts or authorization model depend on one provider. Native provider types and defaults can also bypass the engine's identity and audience checks.

## Decision

Application code depends on the `KnowledgeBackend` protocol and engine-owned immutable values in `engine/src/context_engine/knowledge_backend/`. Provider implementations live below `knowledge_backend/providers/` and are the only modules allowed to import provider packages.

The public glossary is:

- Context space: public governed context resource.
- Source and source record: origin and stable logical content identity.
- Access partition: internal isolation unit derived from compatible audiences.
- Evidence: authorized source-linked support returned by a query.
- Enrichment: explicit generation of additional derived context.
- Backend reference: opaque reconciliation handle that cannot be publicly serialized.

Public contracts and application layers must not contain provider product names, native operation/type names, native isolation terminology or native identifiers. The contract lint and serializer guard enforce this rule.

## Consequences

- The adapter must translate all requests, results, evidence and errors.
- Internal partition and backend references require dedicated non-serializable types.
- Provider upgrades can change only the private implementation when the port remains stable.
- Some provider features will remain unavailable until an engine-owned operation is designed for them.

## Validation

- `engine/tests/test_public_boundary.py`
- `engine/tests/test_cognee_adapter.py`
- `scripts/check_provider_boundary.py`

# ADR 0002: Initial provider and store topology

**Status:** Conditional — live test pending

**Date:** 2026-09-18

## Context

M0 must test one concrete provider/store combination. Support claims for the individual stores are insufficient; the combination must pass isolation, update and deletion tests.

## Decision

Pin Cognee `1.5.4` inside the private provider extra. Use its tagged local defaults for the first spike:

- Relational state: SQLite.
- Vector index: LanceDB with its per-isolation-unit handler.
- Graph index: Kuzu with its per-isolation-unit handler.
- Backend access control: enabled.
- Session cache: disabled.
- Implicit self-improvement: disabled by the adapter.
- Local/URL ingestion by the provider: disabled in a hosted configuration; the engine passes normalized content.

The embedding and LLM models must be supplied explicitly in the live test environment. Do not allow either to silently fall back to an unrelated provider.

## Alternatives

- PostgreSQL, PGVector and Neo4j are deferred until local semantics pass.
- A shared graph database without a proven isolation handler is rejected.
- Provider HTTP/MCP services are rejected as public engine surfaces.

## Consequences

The topology is suitable for a reproducible architecture spike, not a production recommendation. A production topology requires capacity, backup and multi-process testing in later milestones.

## Validation

- Pinned SDK signature check in the private provider module.
- Opt-in live test in `engine/tests/test_live_cognee_provider.py`.
- Tagged upstream configuration: `https://github.com/topoteretes/cognee/blob/v1.5.4/.env.template`.

This ADR becomes accepted only after the live two-audience and lifecycle matrix passes.

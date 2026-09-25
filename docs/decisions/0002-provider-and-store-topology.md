# ADR 0002: Initial provider and store topology

**Status:** Accepted for single-process local use (2026-09-25); separate API and worker processes are not supported by this topology

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

## Revision (2026-09-25, live verification)

The live two-audience and lifecycle matrix passed against this topology on 2026-09-25, with OpenAI `gpt-5-mini` and `text-embedding-3-small` configured explicitly. So did an end-to-end run through the REST API and the worker. The runs also showed where the topology and the provider's defaults differ from what M0 assumed.

- **One process owns the embedded stores.** Each partition's graph store takes an exclusive file lock, and the provider keeps it open after use. With the API and the worker as separate processes, the worker holds the lock and every query in the API fails as unavailable. Even the chunk retriever opens the graph. The live end-to-end test therefore runs the API and the worker in one process. A multi-process deployment needs either a server graph store, one of the deferred alternatives above, or provider reads served from the process that writes. That choice is open. Owner: Backend owner.
- **The adapter creates the provider's stores.** The provider does not create its relational store on first use. The runtime runs the provider's own setup once per process before the first operation.
- **One storage root per process.** The provider reads its storage roots once per process. The runtime refuses a second root instead of silently sharing the first one's stores.
- **Record text travels as an upload.** Given a string, the provider treats it as a local path, web address, or object-store key whenever it looks like one. In the live run, a document whose text was the path of the provider's own database was loaded as that file. The load failed only because no loader matched the file type. The adapter now hands each record over as an upload, which is stored exactly as given. This replaces the M0 assumption that provider local ingestion could stay disabled. The provider reads its own stored copy back through its local-file loader, so local reads stay enabled but are confined to the provider's data directory.
- **The engine owns process logging.** Importing the provider replaced every root log handler, which dropped the fields of the engine's audit events. Its console output also carried query text. The adapter restores the engine's handlers after import, and the engine keeps third-party records only at warning level and above.

Validation: `engine/tests/test_live_cognee_provider.py` and `tests/end-to-end/test_live_provider_path.py`, run with `pytest -m live_provider`, plus the upload, setup, storage-root, and logging tests in `engine/tests/test_cognee_adapter.py` and `engine/tests/test_logging.py`.

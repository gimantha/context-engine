# ADR 0008: Evidence and provenance translation

**Status:** Accepted for the engine model; native mapping pending

**Date:** 2026-09-18

## Context

Provider search results and graph objects are unstable public contracts and can contain native IDs or inaccessible metadata. A source URL alone is not evidence.

## Decision

The adapter translates native results into `EvidenceItem` values containing:

- Engine evidence ID.
- Engine source and record IDs.
- Source version.
- Exact passage and location when available.
- Engine graph references and a relevance score.

Native IDs are stored only as opaque backend references for reconciliation. Public evidence is access-checked on every read, including after a query completes. Missing lineage produces unresolved internal evidence that must be suppressed from public results until reconciled.

The caller-safe trace lists selected engine evidence IDs and outcome. It never includes private chain of thought, raw provider traces or native identifiers.

## Consequences

- The ingestion pipeline must attach engine source metadata before indexing.
- Citations are suppressed when provider output cannot be mapped to a source version.
- Provider response shape changes are contained in one translator and its fixtures.

## Validation

- `engine/tests/test_cognee_adapter.py` verifies native fields do not escape.
- `engine/tests/test_public_boundary.py` verifies safe serialization.
- OpenAPI evidence and trace schemas use engine identifiers only.

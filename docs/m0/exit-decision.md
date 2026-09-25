# M0 Exit Decision

**Decision:** M1 control-plane work authorized; live security verification remains required

**Date:** 2026-09-18

**Reviewers:** Unassigned

## Gate status

| Criterion | Status | Evidence |
| --- | --- | --- |
| Provider-neutral port supports dummy and real adapters | Pass | Backend protocol, dummy backend and private adapter tests |
| Provider and transitive dependencies pinned | Pass | `engine/pyproject.toml` and `engine/uv.lock` |
| Explicit identity and isolation on every operation | Pass at engine/adapter boundary | Port signatures and adapter tests |
| Two-audience raw/derived isolation | Live pass on 2026-09-25 for retrieved passages and per-partition graphs | Golden fixture and isolation test; `test_live_cognee_provider.py`; `tests/end-to-end/test_live_provider_path.py` |
| Concurrent request isolation | Dummy implemented; live pending | Backend contract test |
| Update and out-of-order behavior | Live pass on 2026-09-25 for replacement; ordering is decided by the ledger before the provider is called | Lifecycle contract test; both live tests |
| Specific-record deletion and residue scan | Live pass on 2026-09-25 for every live store; bytes remain in uncompacted storage and in provider search history | Lifecycle test, ADR 0007 revision, and the live residue scan |
| Engine-owned evidence mapping | Pass for mapped fixture | Evidence types and adapter translation test |
| OpenAPI, event and MCP contracts validate | Pass | Contract tests |
| Provider terminology absent from public contracts | Pass | Boundary script |
| Threat model has owners for high risks | Complete | `docs/m0/threat-model.md` |
| Required ADRs link evidence | Complete with conditional statuses | `docs/decisions/` |
| Failed assumptions have fallbacks | Complete | ADRs 0004 and 0007 |

## Decision rule

- **Go:** all dummy and live gates pass with no cross-audience or deletion residue.
- **Conditional go:** implementation checks pass, and a non-security live limitation has an accepted fallback and owner.
- **Stop:** any audience crossing occurs, deleted content remains accessible without a proven quarantine/rebuild fallback, or provider scope can default broader than the engine request.

The dependency installation and automated M0 verification are complete. The user authorized the provider-neutral M1 control-plane implementation while the live gate remains open. Provider-backed execution, production readiness, and any security claim based on isolation, stale-artifact removal, or deletion remain blocked because the dummy backend cannot prove those properties. Update this decision after the opt-in live-provider matrix passes with explicitly configured language and embedding models.

## Live gate run (2026-09-25)

The opt-in live matrix passed on 2026-09-25 with explicitly configured language and embedding models. The spike report records the run and the seven defects it exposed, all of which are fixed.

- **No audience crossing occurred.** Each reader retrieved only its own and shared records, before and after replacement, an audience move, deletion, and enrichment. The provider itself refused a partition the reader held no grant for.
- **Deleted content is not accessible through any engine read path.** No trace of a replaced or deleted version remains in the provider's raw files, relational rows, vector tables, or partition graph.
- **Provider scope cannot default broader than the request.** Every call names its partitions explicitly, and there is no default provider identity.

These items keep the result short of an unconditional go:

- Live concurrent-request isolation and partial-write injection have not been run.
- Deleted text remains in uncompacted vector and graph storage and in the provider's search history (threat model T19), outside every engine read path.
- The local topology serves the API and the worker from one process only (ADR 0002 revision).

The decision above stays as written until reviewers weigh these items against the decision rule.

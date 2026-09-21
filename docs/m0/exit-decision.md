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
| Two-audience raw/derived isolation | Dummy implemented; live pending | Golden fixture and isolation test |
| Concurrent request isolation | Dummy implemented; live pending | Backend contract test |
| Update and out-of-order behavior | Dummy implemented; live pending | Lifecycle contract test |
| Specific-record deletion and residue scan | Dummy implemented; live pending | Lifecycle test and ADR 0007 |
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

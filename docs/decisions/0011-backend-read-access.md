# ADR 0011: Backend read access materialized from engine policy

**Status:** Accepted for the engine model; live provider validation pending

**Date:** 2026-09-24

## Context

The engine owns every provider isolation unit through its service identity (ADR 0005). For the provider's own permission checks to act as defense in depth, each reader needs native read access to exactly the partitions engine policy lets it read, and nothing more.

## Decision

- **Readers are computed from engine state.** A principal may read a partition when it holds `context.read` on the partition's space and belongs to at least one of the partition's audiences (ADR 0003). Service identities never read.
- **Access is applied as a difference.** `backend_read_access` records what the backend currently grants. The worker grants what is missing and revokes what is no longer allowed, always acting as the owning service identity.
- **Changes trigger a full pass.** The worker compares the policy version and the principal count with the last applied values on every loop and runs a full pass when either changed. It also runs one at startup, because group membership comes from the identity registry. The first write into a new partition triggers a pass for that partition.
- **Failures only delay.** A backend failure during a pass clears the recorded state, so the next loop retries everything. Engine policy is checked on every request and remains authoritative, so a lagging revocation never grants engine access.

## Consequences

- Revocation reaches the provider at the worker's next loop, not instantly. Engine checks close access immediately.
- A pass enumerates every principal for every partition. That is fine for the current scale; a large deployment will need incremental passes driven by the specific grant or group that changed.
- Group changes inside the identity registry are only seen at worker startup until identity-provider sync arrives in M6.

## Validation

- `engine/tests/test_read_access.py`
- `engine/tests/test_cognee_adapter.py::test_read_grants_act_as_the_owner_and_need_an_initialized_partition`
- The live test grants read access per reader through the same provider calls.

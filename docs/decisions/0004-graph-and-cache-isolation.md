# ADR 0004: Graph derivation and cache isolation

**Status:** Conditional — real provider validation pending

**Date:** 2026-09-18

## Context

Shared entity names can connect restricted records through graph edges, summaries or cached answers even when raw retrieval is filtered correctly.

## Decision

Graph extraction, enrichment, summaries and answer caches run within internal access-partition boundaries. Cross-partition derivation is disabled. A multi-partition query may combine separately retrieved, authorized evidence only in the engine query service; it may not persist cross-partition graph artifacts.

Cache keys include principal, policy version, context space, authorized partition set, query mode and processing version. Revocation bypasses or invalidates older cache entries.

## Fallback

If the selected graph/vector combination cannot isolate derived artifacts, provision a separate native isolation unit per access partition and prohibit provider-side multi-partition derivation.

## Validation

The golden fixture deliberately repeats `Gateway` and `Colombo` across two restricted records and a shared runbook. Tests inspect passages and graph paths, not only final answers.

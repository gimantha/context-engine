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

## Revision (2026-09-25, M4 slice 3)

Enrichment is an explicit, versioned job (`POST /v1/spaces/{spaceId}/enrichments`, pipeline `enrich@1`). The worker calls the backend once per partition that holds indexed content, as the engine's service identity, so derived data cannot span two reader sets even if a backend would allow it. The job result records the pipeline version, partitions, affected records, and created artifacts. The engine keeps no answer cache yet, and the provider's cache is disabled by configuration.

## Planned revision (M5, agreed 2026-09-29)

One isolation unit per access partition means the provider's graph never connects records that different reader sets may see, even for a reader who may see both. The cost is deferred, not accepted for good: as long as the adapter pins the chunk retriever (ADR 0008 revision), no query uses the graph for evidence. M5 closes the gap in the engine rather than in the provider. After retrieval and the visibility barrier, the engine joins the caller's authorized evidence on shared entities and adds the linked records' passages, from their own partitions, as second-hop evidence with lineage. Every hop is a record the caller may read, so no store ever holds a cross-partition link and threat T04 stays closed. A shared graph filtered at query time remains rejected under this provider, because entity nodes and derived text would mix reader sets. Per-group materialized copies remain the fallback if read-time linking proves too slow. Until then, coarse audience mappings keep the number of partitions, and so the gap, small.

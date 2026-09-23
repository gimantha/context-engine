# M3 Source Registration and Ingestion Lifecycle (engine half)

**Status:** Implemented for the engine; the Ballerina file-source connector is developed in a separate workstream

**Date:** 2026-09-23

**Release limitation:** Live-provider security verification from M0 remains open; no provider-backed execution exists yet

## Scope of this slice

The plan's M3 covers both the Ballerina connector and the engine's ingestion lifecycle. This slice delivers the engine half and everything a connector needs to talk to: source registration, delivery rights, staged uploads, checkpoints, the authoritative record ledger, record status, and dead-letter inspection. A reference connector written against the REST API in `tests/end-to-end/test_ingestion_lifecycle.py` proves the gate without the Ballerina code.

Two design choices were made deliberately and are open to revision:

- **The envelope keeps its flat `audience` list.** Each source declares an audience mapping from source tags to engine audiences. Unmapped tags quarantine the record. A structured allow list with users, groups, domains, and intersections can extend the envelope later without re-keying anything, because the partition binding is still deferred.
- **The engine never fetches a URL, and provider loaders stay disabled.** A URL source is a registered source like any other; its connector fetches, hashes, stages, and delivers. This follows threat T13 and ADR 0002 and keeps content hashes, size and type validation, and version ordering in the engine.

## Delivered behavior

**Sources.** `POST /v1/spaces/{spaceId}/sources` registers a source with a name, a type, an audience mapping, and a version ordering (`numeric` by default, `lexicographic` on request). Sources are resources in the grant chain: source, then space, then root. A connector's delivery right is an `ingest.write` grant on the source, so the ingestion body cannot select a space or source the credential is not bound to (threat T02). Sources can be paused and resumed; a paused source refuses uploads and events with a conflict.

**Staged uploads.** `POST /v1/sources/{sourceId}/uploads` accepts raw bytes with an idempotency key. Delivery rights are checked before the body is read. Size is capped while streaming, the content type must be on the configured allowlist, empty bodies are rejected, bytes are hashed with SHA-256 and written atomically under the staging directory, and every upload expires. Identical replays return the same upload; a different body under the same key is a conflict.

**Ingestion acceptance.** An upsert must reference a staged upload that belongs to the same source, has not expired, still has its bytes, and matches the event's content type and hash. Every mismatch answers with one generic message so a caller cannot probe which check failed (threat T12). Versions must satisfy the source's ordering before a job is created.

**The record ledger.** `persistence/sources.py` applies each delivered event in one transaction with the idempotent effect row and a version-history row. Rules follow ADR 0006: a newer version replaces, an older version is ignored, the same version with the same content is a replay, the same version with different content fails the job terminally, a delete writes a tombstone at its version, an older or equal upsert after a delete is ignored, a strictly newer upsert re-creates the record, and ACL changes apply to the current content only and are ordered by the source ACL version. Records whose audience tags are not all mapped are quarantined; a later mapped ACL change releases them.

**Checkpoints, status, dead letters.** Connectors store their cursor with `PUT /v1/sources/{sourceId}/checkpoints`. `GET /v1/sources/{sourceId}/records/{recordId}` exposes state, current version, ACL version, content hash, and quarantine reason, never content, audiences, or backend references. `GET /v1/sources/{sourceId}/jobs?state=failed` lists failed deliveries with their error codes.

**Worker.** `LifecycleJobHandler` replaces the M1 ledger-only handler. Terminal failures (`source_unknown`, `invalid_version`, `effect_conflict`) fail the job without retries. Reauthorization now targets the source named in the payload, so a revoked source binding stops a queued job even if the principal keeps rights elsewhere.

## Persistence

Migration `0003_sources_and_ledger.sql` adds `sources`, `source_checkpoints`, `staged_uploads`, `source_records`, and `record_versions`. It also rebuilds `source_record_effects` so it detects exact replays by idempotency key only; version and content ordering moved into the ledger, which allows repeated deliveries of one version and several ACL changes for one content version.

## Acceptance evidence

| M3 gate | Result | Evidence |
| --- | --- | --- |
| Initial sync and re-sync converge to one active record per source version | Pass | `test_ingestion_lifecycle.py::test_sync_converges_and_never_resurrects_deleted_content` |
| Duplicate and out-of-order events do not resurrect deleted content | Pass | Same test: late v2 and v4 upserts after a v4 delete stay ignored; v5 re-creates |
| A failed upload is retryable | Pass | Same key and bytes return the same upload; `test_sources_api.py::test_uploads_validate_type_size_and_replay` |
| A malformed or oversized file is rejected | Pass | 415 for a disallowed type, 400 for an empty body, 413 over the limit |
| A bad audience goes to quarantine | Pass | `test_ingestion_lifecycle.py::test_unmapped_audience_quarantines_until_a_mapped_acl_change` |
| Same version with different content is a conflict | Pass | Terminal `effect_conflict` job in the lifecycle test |
| Connector cannot deliver outside its source binding | Pass | `test_sources_api.py::test_ingestion_rejects_mismatched_staged_content_and_bad_versions` |
| Crash replay stays idempotent with the new ledger | Pass | `tests/recovery/test_worker_replay.py` |
| Revoked source binding stops a queued job | Pass | `tests/recovery/test_worker_reauthorization.py` |
| Ballerina file-source connector end to end | Not in this slice | Separate workstream; the end-to-end test is the reference connector |

Validation performed on 2026-09-23 from `engine/`:

```text
ruff format --check: passed
ruff check: passed
pytest -m "not live_provider": 56 passed, 1 provider-extra check skipped, 1 live test deselected
context-engine-api --check: passed
context-engine-worker --check: passed
context-engine-migrate: applied 3 migrations to a fresh database
provider boundary check: passed
```

## Deliberately not in this slice

- **Ballerina connector and common helpers.** Owned elsewhere. The envelope, upload, checkpoint, and status contracts it needs are published in the OpenAPI document.
- **URL connector.** Not built. It is a connector conforming to the same contracts, not an engine feature.
- **Access-partition-to-audience binding.** Still waiting on the ACL shape decision. The ledger stores mapped engine audiences per record so the binding can be computed later.
- **Staged-object cleanup.** Expired uploads are refused but not yet deleted from disk. This belongs with retention work in M7.
- **Extraction and parser versions.** Bytes are staged as delivered. Text extraction and parser lineage arrive with the provider-backed pipeline in M4.

## Operational notes

- `local/m3.env.example` adds the staging path, upload size limit, upload time to live, and the content type allowlist.
- The staging directory lives under `.context-engine/`, which Git ignores.
- Failed deliveries stay in the jobs table with their error code. There is no automatic replay of dead letters; a connector re-delivers with a new idempotency key once the cause is fixed.

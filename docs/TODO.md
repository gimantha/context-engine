# Context Engine TODO

Work agreed in principle but not yet scheduled in a milestone. Each item says why it matters, what to build, and what must be settled first.

## One-call ingestion for connectors

**Status:** Proposed on 2026-09-25. The two-call path stays until the Ballerina connector owner agrees to retire it.

### Why

Today a connector makes two calls for every upsert:

1. It uploads the bytes to `POST /v1/sources/{sourceId}/uploads` and receives an `uploadId` and `contentHash`.
2. It sends an ingestion event to `POST /v1/ingestions` that names the upload in `contentRef` and repeats its hash and content type. The engine answers `202 Accepted` with the `jobId` and `statusUrl`.

The staging store itself stays. The worker reads each record's current version from it for audience moves, quarantine releases, retries, and partition rebuilds, and it keeps content out of the job table. Only the connector-facing upload step is unnecessary. The implementation plan's API table already says ingestion should "accept bytes or staged content"; M3 built only the staged half.

One call gives connectors a simpler protocol and makes accepting an event and storing its content a single step. It also removes orphaned and expired uploads, and with them most of the planned M7 upload sweep.

### Target design

- **Route.** `POST /v1/sources/{sourceId}/ingestions` takes a `multipart/form-data` body: an `event` part with the ingestion envelope, then a `content` part with the raw bytes. Deletes and ACL changes send only the event part.
- **Authorization before the bytes.** With the source in the path, the engine checks `ingest.write` before it reads the body, as the upload route does today. The event part comes first, so a malformed event is rejected before the file is read. A `sourceId` in the event must match the path.
- **Internal staging.** The engine streams the content part into the staging store under the same type allowlist, size limit, and SHA-256 hashing as the upload route. It then records the upload and queues the job together, so an accepted event always has its content. The job payload keeps a reference, never the content.
- **Engine-computed hash.** The engine hashes the bytes itself. A `contentHash` in the event becomes an optional integrity check that must match when present. Same-version conflict detection keeps comparing the engine's hashes.
- **Idempotency over event and bytes.** Resending with the same key, event, and bytes returns the original job. The same key with a different event or different bytes is a conflict.
- **Response.** `202 Accepted` with `jobId`, `statusUrl`, and a `Location` header, as today.

### Recovery after a lost response

The job id arrives only in the response. Today a connector that loses it resends the small event with the same key and gets the same job back. With one call, that resend would carry the file again. A lookup lets the connector check first:

- `GET /v1/sources/{sourceId}/ingestions/{idempotencyKey}` returns the job accepted under that key, or `404` when there is none. It needs delivery rights on the source.
- After a lost response, the connector looks up its key. If a job exists, it polls that job. Otherwise, it resends the whole request.
- A lookup that arrives while the first request is still being stored can answer `404`. The resend is still safe, because the idempotency key prevents a second job.
- Optionally, support `Expect: 100-continue`, so the engine can answer a resend of an accepted key from the headers before the file is sent. Confirm that the Ballerina HTTP client supports it first.

### Compatibility

- Keep `POST /v1/sources/{sourceId}/uploads` and `POST /v1/ingestions` working while the connector moves over. Retire them only with the connector owner's agreement. A separate upload step may still be worth keeping for very large or resumable uploads that go straight to object storage in a hosted deployment.
- The event schema requires `contentRef` and `contentHash` for upserts. The one-call route needs a variant without them.

### Work list

- **API.** The multipart route and the lookup route in `engine/src/context_engine/api/app.py`, with the size limit enforced while streaming.
- **Application.** An ingestion command in `engine/src/context_engine/application/service.py` that stages the bytes and queues the job, sharing the upload route's checks.
- **Persistence.** A lookup of jobs by source and idempotency key.
- **Contracts.** OpenAPI routes and examples, the event schema variant, and the planned MCP ingest tool, all under `contracts/`.
- **Tests.** Acceptance, replay, a conflict with different bytes under the same key, authorization before the body is read, oversized and disallowed content, lookup after a lost response, and the end-to-end connector test on the one-call path.
- **Docs.** The M3 report's ingestion section, the README delivery walkthrough, and threat model rows T12 and T13.

### Open questions

- Does the Ballerina connector owner agree to move to one call, and on what timeline?
- Should the two-call routes stay as an optional path, or be removed once the connector has moved?

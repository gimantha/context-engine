# Context Engine TODO

Work agreed in principle but not yet scheduled in a milestone. Each item says why it matters, what to build, and what must be settled first.

## One-call ingestion for connectors

**Status:** Built on 2026-09-28, alongside the two-call path. Retiring the two-call path still needs the Ballerina connector owner's agreement.

### Why

Before this change, a connector made two calls for every upsert:

1. It uploads the bytes to `POST /v1/sources/{sourceId}/uploads` and receives an `uploadId` and `contentHash`.
2. It sends an ingestion event to `POST /v1/ingestions` that names the upload in `contentRef` and repeats its hash and content type. The engine answers `202 Accepted` with the `jobId` and `statusUrl`.

The staging store itself stays. The worker reads each record's current version from it for audience moves, quarantine releases, retries, and partition rebuilds, and it keeps content out of the job table. Only the connector-facing upload step is unnecessary. The implementation plan's API table already says ingestion should "accept bytes or staged content"; M3 built only the staged half.

One call gives connectors a simpler protocol and makes accepting an event and storing its content a single step. It also removes orphaned and expired uploads, and with them most of the planned M7 upload sweep.

### Target design

- **Route.** `POST /v1/sources/{sourceId}/ingestions` takes a `multipart/form-data` body: an `event` part with the ingestion envelope, then a `content` part with the raw bytes. Deletes and ACL changes send only the event part.
- **Authorization before the bytes.** With the source in the path, the engine checks `ingest.write` before it reads the body, as the upload route does today. The event part comes first, so a malformed event is rejected before the file is read. A `sourceId` in the event must match the path.
- **Internal staging.** The engine streams the content part into the staging store under the same type allowlist, size limit, and SHA-256 hashing as the upload route. Every check that can refuse the event runs before the bytes are staged, and the job is queued only after they are, so an accepted event always has its content. The job payload keeps a reference, never the content.
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
- The staged event schema still requires `contentRef` and `contentHash` for upserts. One-call events use `contracts/schemas/ingestion-direct-event.schema.json`, which has no `contentRef`.

### Work list

Done on 2026-09-28:

- **API.** The multipart route and the lookup route in `engine/src/context_engine/api/app.py`. The body is parsed as it streams by `engine/src/context_engine/api/multipart.py`, so the event is validated before its content is read and the size limit holds while the bytes arrive.
- **Application.** `accept_direct_ingestion` and `find_ingestion` in `engine/src/context_engine/application/service.py`, sharing the upload route's checks. The content is staged under the event's key in its own namespace.
- **Persistence.** A lookup of jobs by source and idempotency key.
- **Contracts.** OpenAPI 0.8.0 with both routes, the one-call event schema, and its example.
- **Tests.** `engine/tests/test_direct_ingestion.py` covers acceptance, replay, conflicts over event and bytes, declared hash and type checks, request shape, limits, rights checked before the body is read, an invalid event rejected before its content is read, a refused event staging nothing, and the lookup. The reference-connector lifecycle tests now run over both paths. A curl run against a real server staged a 5.5 MB file with the correct hash.
- **Docs.** The README delivery walkthrough, AGENT.md, the M3 report revision, and threat model row T12.

Remaining:

- **MCP ingest tool.** The planned tool in `contracts/mcp/` still takes a `contentRef`. Decide how an MCP client delivers content when the MCP facade is built in M6.
- **`Expect: 100-continue`.** Optional, once the Ballerina HTTP client is confirmed to support it.
- **Retiring the two-call path.** Only with the connector owner's agreement.

### Open questions

- Does the Ballerina connector owner agree to move to one call, and on what timeline?
- Should the two-call routes stay as an optional path, or be removed once the connector has moved?

## Run the API and the worker as separate processes in provider mode

**Status:** Option 1 is built: `context-engine-serve` runs the API and the worker in one process (2026-09-25). Choosing between options 2 and 3 is still open, and it must be settled before the M6 gate, which needs queries in the UI while the connector is ingesting.

### Problem

The local topology gives each partition an embedded graph store that takes an exclusive file lock. The provider keeps the store open after use. When the API and the worker run as separate processes, the worker holds the lock, and every query in the API fails as unavailable:

```text
RuntimeError: IO exception: Could not set lock on file : …/<unit>.pkl (Error: Resource temporarily unavailable)
```

Even the chunk retriever opens the graph, because the provider first checks whether the graph is empty. The worker side works: in the same two-process run, indexing, updates, audience moves, deletes, enrichment, and progress all succeeded.

Only a single process works. In provider mode, local development and the integration control plane UI on port 8000 must use `context-engine-serve`, not separate `context-engine-api` and `context-engine-worker` processes.

### Options

1. **One local process.** Add an entrypoint that runs the worker loop inside the API process. It unblocks local work and the control plane UI quickly. It is not a scaling topology: one process owns both reads and writes, and a slow provider call in a job competes with queries.
2. **A server graph store.** The provider can isolate each partition in Neo4j, with a database per partition on Enterprise or one container per partition on Community, or in Postgres, with a database or a schema per partition. Its Postgres graph adapter ships in a package it calls "demo". ADR 0002 rejects any shared store without a proven isolation handler, so the live isolation and lifecycle matrix, including the residue scan, has to pass again on the chosen store. The vector store's behavior across processes needs the same check.
3. **The worker serves provider reads.** The API forwards queries over an internal channel to the process that owns the provider. The embedded stores stay, but queries now depend on the worker being up, and long jobs must not block them.
4. **Rejected: sharing the embedded store.** The embedded engine allows either one read-write process or several read-only ones, not both. Releasing and reopening the store around every operation would mean changing how the provider holds its connections.

### Recommendation

Build option 1 now for local development and the control plane UI. Choose between options 2 and 3 before M6, since that decision sets the production topology. Record the choice as a revision of ADR 0002.

### Work list

Done on 2026-09-25:

- **Entrypoint.** `context-engine-serve` serves the API and runs the worker loop, the indexing collector, and read-access synchronization as a background task on the same event loop, with one backend and one metrics registry. Readiness reports unavailable while the loop is failing, and the loop restarts on its own.
- **Docs.** The README local setup, AGENT.md, and the ADR 0002 revision.
- **Tests.** `engine/tests/test_serve.py` covers in-process job processing, readiness, shutdown, and startup checks. A live run of the command passed the two-reader scenario against the pinned provider.

Remaining:

- **Topology decision.** Choose option 2 or 3 and record it in ADR 0002.
- **Two-process test.** After that choice, a live test with separate processes and queries running during ingestion.

### Open questions

- Which production deployment is expected: a hosted graph service, or engine-managed stores?
- Is a Neo4j or Postgres dependency acceptable for the first release?

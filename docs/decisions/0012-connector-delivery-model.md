# ADR 0012: Connector delivery model

**Status:** Proposed

**Date:** 2026-09-22, revised 2026-09-28

## Context

Source connectors such as a file source, Salesforce, HubSpot, Slack, and databases deliver records to the engine. Most sources are record-oriented: an object, row, message, or contact whose meaningful payload is a modest amount of text. The engine accepts deliveries at an asynchronous REST boundary that validates the event and queues a durable job.

The first draft of this ADR had connectors put the record text inline in the ingestion event. The engine never accepted that: its service and worker read content only from the staging store, so every inline upsert failed. The content would also have stayed in every job and outbox row, since staged-byte release never touches job payloads. The engine has since gained a one-call route that takes the event and its content in one request and stages the content itself (M3 report revision, 2026-09-28).

An earlier draft still proposed a gRPC transport with client-streaming upload. The transport is never the bottleneck: accepting a delivery only validates it and queues a job, and throughput is set by the control database and by provider work in the worker.

## Decision

Connectors deliver every record through the engine's one-call route, `POST /v1/sources/{sourceId}/ingestions`.

- **One request per record.** The body is multipart: the event as JSON first, then the content bytes. Deletes and ACL changes send only the event. The engine stages the content, so there is no separate upload whose 24-hour expiry a later resend could outlive.
- **The connector normalizes, the engine extracts.** A connector turns a source record into content with a content type on the engine's allowlist. Salesforce records become a JSON projection of the configured fields; uploaded files pass through as they are.
- **Versions are numeric epoch milliseconds.** Upserts use the source's modification time, and deletes use their commit time, so both share one time axis and a delete orders after the record's last change. Sources are registered with numeric ordering. A content hash is never used as a version: it neither is numeric nor only grows.
- **Resends are exact replays.** The observed time comes from the source, never the clock at send time, and the idempotency key is derived from the source, operation, record, and version. The same record state therefore always produces the same event, and the engine returns the original job. Keys are `<sourceId>:<operation>:<sha256>`, which stays under the engine's 200-character header limit however long the record id is.
- **The event carries the content hash.** The engine checks it against the bytes it received.
- **After a lost reply, look before resending.** When the connection fails, the sink asks the engine for the delivery by its key before reporting a failure.
- **A shared client owns the HTTP contract.** `modules/core` in `integrations/ballerina` is the one place that knows the engine's routes; connectors only change how records are read and normalized.

## Alternatives

- **Inline content in the event:** rejected. It duplicates content into durable job and outbox rows that are never released, and it needs a second content path through the service and worker.
- **Upload first, then send the event:** kept as the engine's two-call path, but not used by connectors. It costs an extra call per record, and resending an event after its upload expires fails.
- **gRPC with streaming upload:** rejected. Accepting a delivery is bound by queueing, not transport, and gRPC would double the public surface and duplicate idempotency, authentication, tracing, and error handling already solved for REST.
- **A message broker between connectors and the engine:** deferred. The engine already has a durable outbox and job queue; a broker is a larger topology decision.

## Consequences

- Connectors need no engine change to add a source; they reuse the shared client and event.
- Every content type a connector sends must be on the engine's upload allowlist, which is plain text, Markdown, HTML, JSON, and PDF by default. Anything else is refused with 415.
- Version and observed time are required fields of a connector's record, so a connector cannot fall back to a value that differs between resends.

## Validation

- `integrations/ballerina/modules/core/tests/client_test.bal` checks the request on the wire: part order, the event without `contentRef`, the content bytes, the header key, and which engine errors count as a rejection of the record.
- A local run delivered uploaded files through the connector host into a running engine, including a changed re-upload at a newer version (PR description, 2026-09-28).
- `engine/tests/test_direct_ingestion.py` covers the engine side of the one-call route.

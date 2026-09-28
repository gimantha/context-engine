# ADR 0013: Connector interface, manager, and scheduling

**Status:** Proposed

**Date:** 2026-09-23, revised 2026-09-28

## Context

The engine ingests from many source types with different triggering models. A file upload is a one-off user action, while SaaS and data sources such as Salesforce, Google Drive, databases, and Slack are configured once and then produce new or changed records over time. All of them end in the same operation: deliver a normalized record to the engine ([ADR 0012](0012-connector-delivery-model.md)).

An initial design gave each connector its own process, with one set of configurable values and a blocking poll loop that a single failed fetch aborted. That does not scale to many connections. The host must run any number of configured connections, schedule polling centrally, use event listeners for sources that push changes, such as Salesforce Change Data Capture, and later take its configuration from a database with cross-node coordination.

## Decision

A `ConnectorManager` (the `manager` module) holds a registry of connector types and runs any number of instances against one engine client. A host process wires it together.

- **Two modalities.** Connectors implement one or both of two narrow shapes the manager can drive:

  ```ballerina
  public type PollConnector object {
      public function fetch(string cursor) returns FetchResult|error;
  };
  public type ListenConnector object {
      public function listen(Sink sink, CheckpointStore checkpoints) returns error?;
  };
  ```

  The manager owns the poll loop, so a poll connector exposes one `fetch` cycle. A listen connector attaches its listeners and returns; the host keeps the process alive.
- **Scheduling with `ballerina/task`.** Each poll instance is a recurring `task:Job` at its configured interval. A job runs one fetch, delivery, and checkpoint cycle and logs errors instead of propagating them, so one bad poll never stops the schedule.
- **A poll cursor only passes delivered records.** Every record a poll returns carries its own position. The job delivers records in order and saves the position of the last one the engine accepted, or refused for a reason in the record itself (400, 413, 415, or 422). Any other failure stops the batch, and the next tick resends from there; resends are exact replays (ADR 0012). Saving the batch's final cursor after a partial failure would have skipped the failed records for good.
- **Registry and factories.** A connector type registers a name plus a poll factory, a listen factory, or both, and the manager runs whichever are present. `salesforce` sets both, so one configuration entry is scheduled and attached. Registration is explicit in the host's `main`.
- **One package with submodules.** Everything ships as one Ballerina package, `wso2/context_engine_connectors`. The root module is the host, and the connector SDK (`modules/core`), the runtime (`modules/manager`), the Salesforce connector (`modules/salesforce`), and the upload endpoint (`modules/file_source`) are submodules. A connector depends only on `core`. Registration is compile-time and Ballerina has no runtime connector discovery, so separate packages would bring publishing steps without an external consumer.
- **Durable checkpoints in the engine.** Each instance gets a `CheckpointStore` holding named positions: the poll cursor, and a replay id per change channel. The default store keeps them in the engine's own checkpoint for the instance's source, as one small JSON object, so a restarted host resumes where it stopped. The engine stores one checkpoint per source, so the manager refuses two instances with the same source, as well as two with the same instance id. The in-memory store remains for tests.
- **Configuration.** The manager runs whatever instances a `ConfigProvider` returns. `EnvConfigProvider` decodes a JSON array from an environment variable; unset or empty means no managed connectors, so a host that only serves uploads still starts. A later database-backed provider can implement the same interface and lease connections so exactly one node runs each.

### Salesforce

- **The poll is the backfill.** It pages by `(CreatedDate, Id)`: rows strictly after the saved position, in that order. Paging by `CreatedDate` alone with a strict comparison and a batch limit skipped records that shared the boundary timestamp, such as a bulk insert of hundreds of records in one second. Cursor values and record ids are checked before they are placed in a query.
- **The change listener handles every change.** Creates, updates, and undeletes re-fetch the full record and deliver it, so the content is identical to a backfill of the same record and a record delivered by both paths is a replay. Handling creates here also covers records that commit after the poll has passed their `CreatedDate`. Deletes deliver the removal with the commit time as the version.
- **Every record in an event.** One change event can list many records, for example a transaction that deletes 50 Accounts. The library's `metadata.recordId` holds only the first, so the listener reads the full list from the event's change header. An event with no usable record id is skipped and logged, never turned into a delete of a made-up record.
- **Durable replay positions.** The library resumes a subscription from its coordinator's stored checkpoint and uses `replayFrom` only when none exists. The connector supplies a coordinator that stores the checkpoint in the instance's checkpoint store, so deletes made while the host was down arrive after a restart.
- **Transient failures are retried in the handler.** The library records an event's replay position once it has been dispatched, whether or not handling succeeded. The handlers therefore retry transient engine failures up to four times, waiting 1, 2, then 4 seconds, before logging the record as not delivered.

### File uploads

The upload endpoint is a host built-in, not a managed connector. It serves `POST /files` and delivers into one configured space and source; the engine binds each source to one space, so the earlier per-request space in the path could only ever work for one space. Each file's version is its receipt time in epoch milliseconds, so a later upload of the same file orders after an earlier one. The endpoint listens on loopback by default, refuses to start on another address without an API key, and is off unless configured.

## Consequences

- Running more connections is configuration, not new processes. Listeners and pollers share one host.
- The host's engine credential should hold `ingest.write` on its sources and nothing else. The host has no default credential, so an administrator token is never used by accident.
- Each source is written by exactly one instance. Scaling one source across nodes needs the database-backed provider and its leasing.
- Residual risks: a change whose delivery still fails after the handler's retries is lost from the change stream once later events are checkpointed, and change events the library does not dispatch, such as gap events, are not handled. A periodic full resync would recover both. A crash between the library recording a replay position and the handler finishing loses that change too.

## Validation

- `bal build` and `bal test` in `integrations/ballerina` pass: 24 tests across `core`, `manager`, `salesforce`, and `file_source`.
- `modules/manager/tests/manager_test.bal` covers cursor advance on success, transient failure, and rejection; duplicate instance and source refusal; and the engine-backed checkpoint store surviving a restart against a stand-in for the checkpoint routes.
- `modules/salesforce/tests/salesforce_test.bal` covers epoch-millisecond versions, keyset queries, cursor validation against query injection, every record id in an event, delete commit times, and the replay coordinator.
- A local run of the host against a running engine exercised the upload endpoint's key check, the engine's 415 passing through, a two-file upload, a changed re-upload, and the connector credential being refused anything but delivery. The engine's checkpoint routes stored and returned the combined positions with that credential.

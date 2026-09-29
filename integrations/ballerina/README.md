# Ballerina integrations

Source connectors that deliver records into the Context Engine through its one-call ingestion route, `POST /v1/sources/{sourceId}/ingestions`. Each delivery is one multipart request: the event first, then the content. The engine stages and hashes the content itself and queues a durable job.

This is a single Ballerina package, `wso2/context_engine_connectors`. The root module is the runtime host, and the framework and each connector are submodules under `modules/`. One `bal build` compiles everything into one executable. See [ADR 0012](../../docs/decisions/0012-connector-delivery-model.md) for the delivery model and [ADR 0013](../../docs/decisions/0013-connector-manager.md) for the connector interface, manager, and scheduling.

## Manager, types, and instances

A single runtime host runs any number of configured connections. It builds one engine client, registers the connector types it enables, loads the instance configurations from a `ConfigProvider`, and hands them to the `ConnectorManager`:

```ballerina
manager:ConnectorManager connectorManager = new (engineClient);
connectorManager.register(salesforce:salesforceType());

manager:ConfigProvider provider = new manager:EnvConfigProvider();
check connectorManager.'start(check provider.provide());
```

A connector type provides a poll factory, a listen factory, or both, and the manager runs whichever are present:

- A **poll** factory builds a `PollConnector { fetch(cursor) }`. The manager schedules `fetch` as a recurring `ballerina/task` job and delivers each record in order. The saved cursor only moves past records the engine accepted, or refused for a reason in the record itself; any other failure stops the batch and the next tick resends from there.
- A **listen** factory builds a `ListenConnector { listen(sink, checkpoints) }`. The manager calls `listen` once; the connector attaches its listeners and returns while the host stays alive.

Every instance gets a checkpoint store. By default it keeps the instance's positions, the poll cursor and any change-event replay ids, in the engine's checkpoint for the instance's source, so a restarted host resumes where it stopped. Because the engine keeps one checkpoint per source, each instance needs its own source; the manager refuses duplicates.

## Configuration

`ConfigProvider.provide()` returns the instances to run. `EnvConfigProvider` decodes a JSON array of `ConnectorInstanceConfig` from an environment variable, `CONNECTOR_CONFIGS` by default. Unset or empty means no managed connectors.

```json
[
  {
    "instanceId": "sf-account",
    "connectorType": "salesforce",
    "destination": {
      "spaceId": "<space id>",
      "sourceId": "<source id>",
      "audience": ["source-group:sales"],
      "sourceAclVersion": "1"
    },
    "settings": {
      "auth": { "authType": "client_credentials", "clientId": "…", "clientSecret": "…" },
      "baseUrl": "https://<instance>.my.salesforce.com",
      "sobject": "Account", "fields": ["Name", "Description"]
    }
  }
]
```

`settings` is the type-specific bag each connector factory decodes into its own record.

## Layout

- `Ballerina.toml`, `main.bal`: the root module, the host that registers types, loads configurations, starts the manager, optionally serves uploads, and keeps the process alive.
- `modules/core/`: the connector SDK. `EngineClient` is the only code that knows the engine's routes; `RecordSink` builds and delivers events. It also holds `SourceRecord`, `Destination`, the `PollConnector` and `ListenConnector` shapes, `CheckpointStore`, and the registration types. Connectors depend on this alone.
- `modules/manager/`: the runtime. It holds the `ConnectorManager`, the `task`-based `PollJob`, the checkpoint stores, and `EnvConfigProvider`.
- `modules/salesforce/`: the Salesforce connector, one type with both factories. The poll is the backfill and owns creates; the change listener handles updates, undeletes, and deletes.
- `modules/file_source/`: the file-upload endpoint, a host built-in.

## Salesforce

One `salesforce` configuration drives the full sync:

- **The poll backfills** by paging through the object by `(CreatedDate, Id)`, and owns creates.
- **The change listener delivers updates, undeletes, and deletes.** For an update or undelete it re-fetches the full record, so the content is identical to a backfill and a record delivered by both paths is a replay. An update that changed none of the projected `fields` is dropped, since it would only re-deliver identical content under a new version. Creates are left to the poll. It reads every record id in an event, not just the first.
- **Versions are the record's `SystemModstamp`** in epoch milliseconds, and deletes use their commit time. Register the source with numeric version ordering, the default.
- **Replay positions are stored durably**, so after a restart the listener resumes where it stopped. `replayFrom` applies only to the very first run.

The token endpoint is derived from `baseUrl`, the My Domain URL. Enable Change Data Capture for the object in Setup; the channel is derived from `sobject`, for example `Account` becomes `/data/AccountChangeEvent`. `settings.auth` selects the OAuth2 flow. The poll client and the change listener use the same settings.

### Auth flows

`settings.auth.authType` selects one of three OAuth2 flows:

- **`client_credentials`** is the server-to-server flow. It takes `clientId` and `clientSecret` and uses no refresh token, so mandatory refresh-token rotation does not apply. Enable Client Credentials Flow on the Connected App and set a run-as user with Read on the object, API Enabled, and CDC access.
- **`refresh_token`** takes `clientId`, `clientSecret`, and `refreshToken`. Mandatory Refresh Token Rotation is supported in one process: the REST client and the change listener each cache the rotated token in memory and refresh with the latest. Run a single replica; more than one replica needs a shared token store. The configured `refreshToken` is a seed reused on restart, so under strict rotation prefer `client_credentials`, or run poll-only or listen-only per refresh token.
- **`bearer`** takes a pre-obtained `token`. The token is static and is not refreshed, so it stops working when it expires. It is useful for short-lived tests.

```json
"auth": { "authType": "refresh_token", "clientId": "…", "clientSecret": "…", "refreshToken": "…" }
"auth": { "authType": "bearer", "token": "…" }
```

## File uploads

The host can serve `POST /files` and deliver each uploaded file into one configured space and source. Each file's name is its record id; its version is the time the upload was received. It is off unless `fileUploadEnabled` is set, listens on `127.0.0.1` by default, and refuses to listen on any other address without `fileUploadApiKey`. Callers then send `Authorization: Bearer <key>`.

Files pass through unmodified, so each part needs a content type the engine accepts: plain text, Markdown, HTML, JSON, or PDF by default. Other types are refused with 415.

## Build and run

1. **Start the engine** from `engine/`, with the API and the worker in one process:

   ```bash
   uv run context-engine-serve --host 127.0.0.1 --port 8000
   ```

2. **Give the host its own identity.** Add a service entry to the engine's static token file, for example `{"token": "<host token>", "issuer": "static://local", "subject": "connector-host", "kind": "service"}`. Don't reuse an administrator token.

3. **Create a space and a source,** then grant the host delivery on the source only:

   ```bash
   curl -H "Authorization: Bearer <admin token>" -H "Content-Type: application/json" \
     -d '{"name": "Incident response"}' http://127.0.0.1:8000/v1/spaces
   curl -H "Authorization: Bearer <admin token>" -H "Content-Type: application/json" \
     -d '{"name": "Uploads", "type": "file", "audienceMapping": {"src:uploads": "research"}}' \
     http://127.0.0.1:8000/v1/spaces/<space id>/sources
   curl -X PUT -H "Authorization: Bearer <admin token>" -H "Content-Type: application/json" \
     -d '{"principalId": "<host principal id>", "actions": ["ingest.write"]}' \
     http://127.0.0.1:8000/v1/resources/<source id>/grants/connector-host
   ```

   The host's principal id is the `id` that `GET /v1/auth/me` returns for its token.

4. **Configure and run the host.** Put the settings in a `Config.toml`, which Git ignores:

   ```toml
   engineBaseUrl = "http://127.0.0.1:8000"
   engineToken = "<host token>"
   fileUploadEnabled = true
   fileUploadSpaceId = "<space id>"
   fileUploadSourceId = "<source id>"
   fileUploadAudience = ["src:uploads"]
   ```

   Then start it, with managed connectors, if any, in `CONNECTOR_CONFIGS`:

   ```bash
   bal run
   ```

5. **Upload a file:**

   ```bash
   echo "Confirm the rollback checkpoint." > note.txt
   curl -F "file=@note.txt;type=text/plain" http://127.0.0.1:9090/files
   ```

## Tests

```bash
bal test
```

The tests cover the request the client puts on the wire, cursor advance and checkpoint durability in the manager, the Salesforce connector's versions, paging, cursor validation, and change-event handling, and the upload endpoint's safety rules. They run against in-process stand-ins for the engine, so no engine or Salesforce org is needed.

## Adding a connector

Add a submodule under `modules/<name>/` that imports `context_engine_connectors.core`, implement `PollConnector`, `ListenConnector`, or both, and expose a `ConnectorType` with a name and its factories; `salesforceType` is the example. Import the submodule in the host's `main.bal`, register it, and reference it from configuration by its type name. Give every record a numeric, growing `sourceVersion` and a `sourceObservedAt` taken from the source, and give poll records a `pollCursor`.

# Salesforce connector

Registry type: `salesforce`. One configuration drives a full sync of one SObject, running
**both** a SOQL poll (backfill) and a Change Data Capture listener — the manager schedules the
poll and attaches the listener for a single `salesforce` config.

## How it syncs

- **The poll backfills** by paging through the object by `(CreatedDate, Id)`, and owns creates.
- **The change listener delivers updates, undeletes, and deletes.** For an update or undelete
  it re-fetches the full record, so the content is identical to a backfill and a record
  delivered by both paths is a replay. An update that changed none of the projected `fields`
  is dropped, since it would only re-deliver identical content under a new version. Creates are
  left to the poll. It reads every record id in an event, not just the first.
- **Versions are the record's `SystemModstamp`** in epoch milliseconds, and deletes use their
  commit time. Register the source with numeric version ordering, the default.
- **Replay positions are stored durably**, so after a restart the listener resumes where it
  stopped. `replayFrom` applies only to the very first run.

## Setup

The token endpoint is derived from `baseUrl`, the My Domain URL. Enable Change Data Capture for
the object in Setup; the channel is derived from `sobject`, for example `Account` becomes
`/data/AccountChangeEvent`. The poll client and the change listener use the same settings.

## Auth flows

`settings.auth.authType` selects one of three OAuth2 flows:

- **`client_credentials`** is the server-to-server flow. It takes `clientId` and `clientSecret`
  and uses no refresh token, so mandatory refresh-token rotation does not apply. Enable Client
  Credentials Flow on the Connected App and set a run-as user with Read on the object, API
  Enabled, and CDC access.
- **`refresh_token`** takes `clientId`, `clientSecret`, and `refreshToken`. Mandatory Refresh
  Token Rotation is supported in one process: the REST client and the change listener each cache
  the rotated token in memory and refresh with the latest. Run a single replica; more than one
  replica needs a shared token store. The configured `refreshToken` is a seed reused on restart,
  so under strict rotation prefer `client_credentials`, or run poll-only or listen-only per
  refresh token.
- **`bearer`** takes a pre-obtained `token`. The token is static and is not refreshed, so it
  stops working when it expires. It is useful for short-lived tests.

```json
"auth": { "authType": "client_credentials", "clientId": "…", "clientSecret": "…" }
"auth": { "authType": "refresh_token", "clientId": "…", "clientSecret": "…", "refreshToken": "…" }
"auth": { "authType": "bearer", "token": "…" }
```

## Settings

| Field | Default | Meaning |
|---|---|---|
| `auth` | — | one of the flows above |
| `baseUrl` | — | My Domain URL, e.g. `https://<instance>.my.salesforce.com` |
| `apiVersion` | `59.0` | Salesforce REST API version |
| `sobject` | — | the object to sync, e.g. `Account`; must have CDC enabled |
| `fields` | — | business fields ingested as content |
| `replayFrom` | `-1` | first-run CDC start: `-1` tip, `-2` last 72h, or a replayId |
| `batchSize` | `200` | max records per backfill poll |

## Config example

```json
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
    "sobject": "Account",
    "fields": ["Name", "Description"]
  }
}
```

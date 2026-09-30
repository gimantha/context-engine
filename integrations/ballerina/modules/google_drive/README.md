# Google Drive connector

Registry type: `google_drive`. One configuration keeps a Context Engine source in sync with the
files under a Drive folder. It is **poll-only**: the manager schedules `fetch`, so no public
webhook is needed.

## How it syncs

- **The poll backfills the folder subtree first.** On its first run it bookmarks the Drive
  change log, then walks the folder breadth-first and delivers files ordered by id,
  `backfillBatchSize` per poll. The backfill is **resumable**: each poll saves its position (the
  last delivered id), so a restart mid-backfill continues after the last delivered file instead
  of re-downloading everything. When the last file is delivered it hands off to the changes phase
  from the bookmark, so a file edited during a long backfill is re-caught right after — a clean
  replay under the same version.
- **After the backfill it follows the Changes API.** Each poll captures the next resume token,
  lists the changes since the saved one, and turns each into an upsert (re-download) or a delete.
  A removed or trashed file becomes a delete; a file no longer under the folder is skipped, not
  deleted, so unrelated drive activity does not produce a flood of deletes. Upserts and deletes
  ride the same batch and apply in change order.
- **Versions are the file's `modifiedTime`** in epoch milliseconds, and deletes use the change's
  commit time, on the same axis, so a delete orders after the file's last upsert. Register the
  source with numeric version ordering, the default.
- **The resume token is stored durably**, so after a restart the changes phase continues where
  it stopped and deletes made while the host was down still arrive.

The cursor carries the phase: `""` on the first poll, `backfill|<token>|<lastId>` while
backfilling, then `changes|<token>`.

## Content

Content handling is a pluggable `ContentExtractor` selected by MIME type:

- **Regular files** (PDF, text, images, ...) are downloaded as-is under their own content type.
- **Google Workspace native files** are exported: Docs, Sheets, and Slides to their Office
  formats (`.docx`, `.xlsx`, `.pptx`), which preserve the most structure. Other native types
  (folders, Forms, Drawings, shortcuts) are skipped.

The engine only accepts and extracts text from its allowlist (plain text, Markdown, HTML, JSON;
PDF and other binaries are staged but not yet extracted), so anything it can't handle —
including the Office exports for now — is refused and the poll skips that record. When the engine
adds extraction for those formats, the same deliveries flow through with no connector change.
Drive caps an export at 10 MB; a larger native file's export fails and that record is logged and
skipped.

## Auth flows

`settings.auth.authType` selects one of three flows, each a pluggable `DriveAuthFlow`:

- **`refresh_token`** takes `clientId`, `clientSecret`, and `refreshToken`. The client refreshes
  access tokens on its own; the recommended flow for a long-running sync.
- **`bearer`** takes a pre-obtained `token`. Static and not refreshed, so it stops working when
  it expires. Useful for short-lived tests.
- **`service_account`** takes `clientEmail` and `privateKeyPath` (the service account's
  `private_key` saved as a PEM file), and an optional `subject` to impersonate under domain-wide
  delegation. A service account is its own identity, so share the folder with its `clientEmail`
  (or use domain-wide delegation). The connector mints an access token from a signed JWT
  assertion and rebuilds the client before the token expires.

```json
"auth": { "authType": "refresh_token", "clientId": "…", "clientSecret": "…", "refreshToken": "…" }
"auth": { "authType": "bearer", "token": "…" }
"auth": { "authType": "service_account", "clientEmail": "svc@<project>.iam.gserviceaccount.com", "privateKeyPath": "/path/to/key.pem" }
```

## Settings

| Field | Default | Meaning |
|---|---|---|
| `auth` | — | one of the flows above |
| `folderId` | — | the folder to sync (id from `drive.google.com/drive/folders/<id>`); its whole subtree is synced |
| `pageSize` | `200` | max changes requested per Changes API page |
| `backfillBatchSize` | `100` | files delivered per backfill poll (also the resume granularity) |

## Config example

```json
{
  "instanceId": "gdrive-handbook",
  "connectorType": "google_drive",
  "pollIntervalSeconds": 60,
  "destination": {
    "spaceId": "<space id>",
    "sourceId": "<source id>",
    "audience": ["src:drive"],
    "sourceAclVersion": "1"
  },
  "settings": {
    "auth": { "authType": "refresh_token", "clientId": "…", "clientSecret": "…", "refreshToken": "…" },
    "folderId": "<drive folder id>"
  }
}
```

## Forcing a fresh backfill

The backfill only runs when the source's stored cursor is empty. To re-backfill an existing
source, reset its checkpoint (stop the host first):

```bash
curl -X PUT -H "Authorization: Bearer <engine token>" -H "Content-Type: application/json" \
  -d '{"cursor":"{}"}' http://127.0.0.1:8000/v1/sources/<source id>/checkpoints
```

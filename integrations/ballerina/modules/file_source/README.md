# File upload endpoint

A host built-in, not a managed connector: it serves `POST /files` and delivers each uploaded
file into one configured space and source. It is configured through `Config.toml`, not
`CONNECTOR_CONFIGS`.

## Behavior

Each file's name is its record id; its version is the time the upload was received, in epoch
milliseconds, so a later upload of the same file always orders after an earlier one. Files pass
through unmodified, so each part needs a content type the engine accepts (plain text, Markdown,
HTML, JSON, or PDF by default); other types are refused with 415.

It is off unless `fileUploadEnabled` is set, listens on `127.0.0.1` by default, and refuses to
listen on any other address without `fileUploadApiKey`. When a key is set, callers send
`Authorization: Bearer <key>`.

## Config (`Config.toml`)

```toml
fileUploadEnabled = true
fileUploadHost = "127.0.0.1"     # default; any other address requires fileUploadApiKey
fileUploadPort = 9090            # default
fileUploadSpaceId = "<space id>"
fileUploadSourceId = "<source id>"
fileUploadAudience = ["src:uploads"]
fileUploadSourceAclVersion = "1" # default
fileUploadApiKey = ""            # required off loopback
```

The source must belong to the space, and the host's `engineToken` needs `ingest.write` on it.

## Upload a file

```bash
echo "Confirm the rollback checkpoint." > note.txt
curl -F "file=@note.txt;type=text/plain" http://127.0.0.1:9090/files
```

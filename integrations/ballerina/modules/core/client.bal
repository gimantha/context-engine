// Shared Ballerina client for the Context Engine REST ingestion boundary.
// Connectors normalize a source record and submit it through this client, which is
// the single place that knows the engine's HTTP contract. Only engine-owned,
// provider-neutral concepts appear here.
//
// The engine ingests an `upsert` in two steps: the content is first staged as raw
// bytes (`POST /v1/sources/{id}/uploads`), then the ingestion event references the
// staged object by `contentRef` (inline content is no longer accepted). A `delete`
// carries only identity and is submitted directly.

import ballerina/http;

# Canonical Context Engine ingestion event.
#
# Optional fields are omitted from the JSON payload when unset. For an `upsert`,
# provide `contentType`, `contentRef` (a staged object id), and `contentHash`.
public type IngestionEvent record {|
    # Envelope schema version; the engine currently accepts "1".
    string schemaVersion = "1";
    # Target context space.
    string spaceId;
    # Registered source that produced the record.
    string sourceId;
    # Stable logical identity of the record within its source.
    string sourceRecordId;
    # Monotonic version of this record state.
    string sourceVersion;
    # One of "upsert", "delete", or "acl_changed".
    string operation;
    # MIME type of the inline content, e.g. "text/plain".
    string contentType?;
    # Inline, normalized textual content of the record.
    string content?;
    # Optional human-readable title.
    string title?;
    # Opaque reference to externally staged content (large-binary path).
    string contentRef?;
    # Optional canonical source URL for lineage.
    string sourceUrl?;
    # "sha256:<hex>" digest of the inline content, when known.
    string contentHash?;
    # RFC 3339 timestamp at which the source produced the record.
    string sourceObservedAt;
    # Trusted source audience used by engine policy for routing.
    string[] audience;
    # Version of the source ACL that authorized this audience.
    string sourceAclVersion;
    # Idempotency key; identical retries must reuse the same value.
    string idempotencyKey;
|};

# Job handle returned by the engine for accepted asynchronous work.
public type JobAccepted record {|
    # Engine identifier for the accepted job.
    string jobId;
    # Relative URL for polling the job's status.
    string statusUrl;
|};

# Handle for content staged with the engine, referenced by an ingestion `upsert`.
#
# The engine verifies the ingestion event's `contentRef`, `contentHash`, and
# `contentType` against the staged object, so those three fields must be echoed
# from this handle verbatim.
public type StagedUpload record {
    # Opaque staged-object id to send as the event's `contentRef`.
    string uploadId;
    # Content type the engine recorded for the staged bytes.
    string contentType;
    # "sha256:<hex>" digest the engine computed over the staged bytes.
    string contentHash;
};

# Configuration for the engine client.
public type EngineClientConfig record {|
    # Base URL of the engine, e.g. "http://127.0.0.1:8000".
    string baseUrl;
    # Optional bearer token for authenticated deployments.
    string? bearerToken = ();
|};

# Thin REST client that submits ingestion events to the Context Engine.
public isolated client class EngineClient {
    private final http:Client engineApi;

    # Initialize the client against the engine base URL.
    #
    # + config - endpoint and optional credential configuration
    public isolated function init(EngineClientConfig config) returns error? {
        http:ClientConfiguration clientConfig = {};
        string? token = config.bearerToken;
        if token is string {
            // Source credentials authorize delivery only; they never grant read access.
            clientConfig.auth = <http:BearerTokenConfig>{token: token};
        }
        self.engineApi = check new (config.baseUrl, clientConfig);
    }

    # Submit one ingestion event and return the engine job handle.
    #
    # The engine deduplicates by `Idempotency-Key`, so replays of the same event
    # return the original job rather than creating a new one.
    #
    # + ingestionEvent - the canonical ingestion envelope
    # + return - the accepted job handle, or an error for a non-2xx response
    remote isolated function ingest(IngestionEvent ingestionEvent) returns JobAccepted|error {
        map<string> headers = {"Idempotency-Key": ingestionEvent.idempotencyKey};
        return self.engineApi->post("/v1/ingestions", ingestionEvent, headers);
    }

    # Stage content as raw bytes before an `upsert` references it.
    #
    # POSTs the bytes to the source's upload endpoint under `contentType`. The
    # engine dedupes by `Idempotency-Key`, so re-staging the same content returns
    # the original handle.
    #
    # + sourceId - registered source the content belongs to
    # + contentType - MIME type recorded for the staged bytes
    # + data - the raw content bytes to stage
    # + idempotencyKey - stable key so replays reuse the original staged object
    # + return - the staged-object handle, or an error for a non-2xx response
    remote isolated function stageUpload(string sourceId, string contentType, byte[] data,
            string idempotencyKey) returns StagedUpload|error {
        map<string> headers = {"Idempotency-Key": idempotencyKey};
        return self.engineApi->post(string `/v1/sources/${sourceId}/uploads`, data, headers,
                mediaType = contentType);
    }
}

# Build a deterministic idempotency key for a source-record version.
#
# Mirrors the engine's natural record identity
# `(spaceId, sourceId, sourceRecordId, sourceVersion)`, so the same record sent
# to different spaces does not collide.
#
# + spaceId - target context space
# + sourceId - registered source identifier
# + recordId - stable record identity within the source
# + sourceVersion - monotonic version of this record state
# + return - a stable "spaceId:sourceId:recordId:sourceVersion" idempotency key
public isolated function idempotencyKey(string spaceId, string sourceId, string recordId,
        string sourceVersion) returns string {
    return string `${spaceId}:${sourceId}:${recordId}:${sourceVersion}`;
}

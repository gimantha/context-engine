// Shared Ballerina client for the Context Engine REST boundary.
//
// Connectors normalize a source record and deliver it through this client, which is
// the single place that knows the engine's HTTP contract. Only engine-owned,
// provider-neutral concepts appear here.
//
// Every delivery uses the engine's one-call route,
// `POST /v1/sources/{sourceId}/ingestions`: a multipart body with the event first
// and, for an upsert, the content bytes second. The engine stages and hashes the
// content itself, so there is no separate upload whose expiry a replay could outlive.

import ballerina/crypto;
import ballerina/http;
import ballerina/mime;

# Ingestion event for the one-call route.
#
# Optional fields are omitted from the JSON payload when unset. The event carries no
# content reference: the content travels in the same request. `contentHash`, when set,
# is checked by the engine against the bytes it received.
public type IngestionEvent record {|
    # Envelope schema version; the engine currently accepts "1".
    string schemaVersion = "1";
    # Target context space.
    string spaceId;
    # Registered source that produced the record.
    string sourceId;
    # Stable logical identity of the record within its source.
    string sourceRecordId;
    # Version of this record state under the source's version ordering.
    string sourceVersion;
    # One of "upsert", "delete", or "acl_changed".
    string operation;
    # "sha256:<hex>" digest of the content, checked against the content part.
    string contentHash?;
    # Optional canonical source URL for lineage.
    string sourceUrl?;
    # RFC 3339 timestamp at which the source produced the record.
    string sourceObservedAt;
    # Source audience labels, mapped to engine audiences by the source.
    string[] audience;
    # Version of the source ACL that authorized this audience.
    string sourceAclVersion;
    # Idempotency key; identical resends must reuse the same value.
    string idempotencyKey;
|};

# Job handle returned by the engine for accepted asynchronous work.
public type JobAccepted record {
    # Engine identifier for the accepted job.
    string jobId;
    # Relative URL for polling the job's status.
    string statusUrl;
};

# Configuration for the engine client.
public type EngineClientConfig record {|
    # Base URL of the engine, e.g. "http://127.0.0.1:8000".
    string baseUrl;
    # Bearer token of the connector's own identity. It should hold `ingest.write` on the
    # connector's sources and nothing more: source credentials authorize delivery only.
    string bearerToken;
|};

# Thin REST client for deliveries and checkpoints.
public isolated client class EngineClient {
    private final http:Client engineApi;

    # Initialize the client against the engine base URL.
    #
    # + config - endpoint and credential configuration
    # + return - an error if the HTTP client cannot be created
    public isolated function init(EngineClientConfig config) returns error? {
        self.engineApi = check new (config.baseUrl, {auth: {token: config.bearerToken}});
    }

    # Deliver one event, with its content for an upsert, in a single request.
    #
    # The engine deduplicates by the idempotency key over both the event and the bytes,
    # so resending the same pair returns the original job.
    #
    # + ingestionEvent - the event to deliver
    # + content - the content bytes of an upsert; `()` for deletes and ACL changes
    # + contentType - MIME type of `content`
    # + return - the accepted job handle, or an error for a non-2xx response
    remote isolated function deliver(IngestionEvent ingestionEvent, byte[]? content = (),
            string contentType = "application/octet-stream") returns JobAccepted|error {
        mime:Entity eventPart = new;
        eventPart.setJson(ingestionEvent.toJson());
        eventPart.setContentDisposition(formPart("event"));
        mime:Entity[] parts = [eventPart];
        if content is byte[] {
            mime:Entity contentPart = new;
            contentPart.setByteArray(content, contentType);
            contentPart.setContentDisposition(formPart("content", "record"));
            parts.push(contentPart);
        }
        http:Request request = new;
        request.setBodyParts(parts, mime:MULTIPART_FORM_DATA);
        request.setHeader("Idempotency-Key", ingestionEvent.idempotencyKey);
        return self.engineApi->post(string `/v1/sources/${ingestionEvent.sourceId}/ingestions`,
                request);
    }

    # Return the job a delivery created under a key, or `()` if the engine has none.
    #
    # Used after a lost reply: if the delivery was accepted, the job is found here and the
    # content need not be sent again.
    #
    # + sourceId - the source the delivery was sent to
    # + idempotencyKey - the delivery's idempotency key
    # + return - the job handle, `()` when no delivery used the key, or an error
    remote isolated function findDelivery(string sourceId, string idempotencyKey)
            returns JobAccepted|error? {
        http:Response response =
            check self.engineApi->get(string `/v1/sources/${sourceId}/ingestions/${idempotencyKey}`);
        if response.statusCode == 404 {
            return ();
        }
        if response.statusCode != 200 {
            return error(string `delivery lookup failed with status ${response.statusCode}`);
        }
        json body = check response.getJsonPayload();
        return body.cloneWithType();
    }

    # Return the source's stored checkpoint, or `()` if none has been saved.
    #
    # + sourceId - the source whose checkpoint to read
    # + return - the stored cursor, `()`, or an error
    remote isolated function getCheckpoint(string sourceId) returns string?|error {
        http:Response response = check self.engineApi->get(string `/v1/sources/${sourceId}/checkpoints`);
        if response.statusCode == 404 {
            return ();
        }
        if response.statusCode != 200 {
            return error(string `checkpoint read failed with status ${response.statusCode}`);
        }
        json body = check response.getJsonPayload();
        return (check body.cursor).toString();
    }

    # Store the source's checkpoint in the engine, which keeps it durably.
    #
    # + sourceId - the source whose checkpoint to write
    # + cursor - the cursor to store; the engine accepts up to 4000 characters
    # + return - an error if the engine refuses it
    remote isolated function putCheckpoint(string sourceId, string cursor) returns error? {
        http:Response response =
            check self.engineApi->put(string `/v1/sources/${sourceId}/checkpoints`, {cursor});
        if response.statusCode != 200 {
            return error(string `checkpoint write failed with status ${response.statusCode}`);
        }
    }
}

// A form-data content disposition for one multipart part.
isolated function formPart(string name, string? fileName = ()) returns mime:ContentDisposition {
    mime:ContentDisposition disposition = new;
    disposition.disposition = "form-data";
    disposition.name = name;
    if fileName is string {
        disposition.fileName = fileName;
    }
    return disposition;
}

# Build a deterministic idempotency key for one delivery.
#
# The engine keeps delivery keys unique per operation across all sources, and caps the
# header at 200 characters. The key therefore starts with the source id and hashes the
# record identity and version, so it stays short and collision-free however long the
# record id is. The record id is length-prefixed before hashing, so no two
# (record, version) pairs share an input.
#
# + sourceId - registered source identifier
# + operation - the event's operation
# + recordId - stable record identity within the source
# + sourceVersion - version of this record state
# + return - a stable "<sourceId>:<operation>:<sha256>" key
public isolated function idempotencyKey(string sourceId, string operation, string recordId,
        string sourceVersion) returns string {
    string identity = string `${recordId.length()}:${recordId}:${sourceVersion}`;
    string digest = crypto:hashSha256(identity.toBytes()).toBase16();
    return string `${sourceId}:${operation}:${digest}`;
}

# Report whether the engine refused one record for a reason in the record itself.
#
# The engine answers 400, 413, 415, or 422 when the event or its content is invalid:
# resending the same record can never succeed, so a poll moves past it. Every other
# failure, such as a paused source, a missing grant, a server error, or a network
# fault, is about the connection or the source as a whole, so the caller stops and
# retries from the same position rather than skipping records.
#
# + err - the delivery error
# + return - true when the record itself was rejected
public isolated function isRecordRejection(error err) returns boolean {
    if err is http:ApplicationResponseError {
        int status = err.detail().statusCode;
        return status == 400 || status == 413 || status == 415 || status == 422;
    }
    return false;
}

// The single place that turns a connector's `SourceRecord` into an ingestion event
// and delivers it.

import ballerina/crypto;
import ballerina/http;

# Where connectors deliver records. The runtime provides a `RecordSink`; tests can
# supply their own implementation.
public type Sink client object {
    # Deliver one record as an upsert.
    #
    # + sourceRecord - the normalized record
    # + return - the accepted job handle, or an error
    remote function ingest(SourceRecord sourceRecord) returns JobAccepted|error;

    # Deliver the deletion of one record.
    #
    # + recordId - the removed record's identity within its source
    # + sourceVersion - version of the deletion; it must order after the record's last
    #   upsert, or the engine ignores it as older
    # + sourceObservedAt - time the source removed the record (RFC 3339)
    # + return - the accepted job handle, or an error
    remote function remove(string recordId, string sourceVersion, string sourceObservedAt)
            returns JobAccepted|error;
};

# Delivers normalized records into the engine for one destination.
#
# Owns everything provider-neutral: hashing content, deriving the idempotency key, and
# building the event. It is isolated, so change listeners may call it from concurrent
# strands.
public isolated client class RecordSink {
    *Sink;

    private final EngineClient engineClient;
    private final readonly & Destination destination;

    # Create a sink bound to an engine client and a destination.
    #
    # + engineClient - the shared engine client
    # + destination - the engine-side space and source this connector writes to
    public isolated function init(EngineClient engineClient, Destination destination) {
        self.engineClient = engineClient;
        self.destination = destination.cloneReadOnly();
    }

    # Deliver one record as an upsert, with its content in the same request.
    #
    # The event carries the content's SHA-256 so the engine can check the bytes it
    # received. The key covers the source, record, and version, so a resend of the same
    # state replays the original job.
    #
    # + sourceRecord - the normalized record
    # + return - the accepted job handle, or an error
    remote isolated function ingest(SourceRecord sourceRecord) returns JobAccepted|error {
        byte[] bytes = check contentOf(sourceRecord);
        IngestionEvent ingestionEvent = {
            spaceId: self.destination.spaceId,
            sourceId: self.destination.sourceId,
            sourceRecordId: sourceRecord.recordId,
            sourceVersion: sourceRecord.sourceVersion,
            operation: "upsert",
            contentHash: "sha256:" + crypto:hashSha256(bytes).toBase16(),
            sourceObservedAt: sourceRecord.sourceObservedAt,
            audience: sourceRecord?.audience ?: self.destination.audience,
            sourceAclVersion: self.destination.sourceAclVersion,
            idempotencyKey: idempotencyKey(self.destination.sourceId, "upsert",
                    sourceRecord.recordId, sourceRecord.sourceVersion)
        };
        string? sourceUrl = sourceRecord?.sourceUrl;
        if sourceUrl is string {
            ingestionEvent.sourceUrl = sourceUrl;
        }
        return self.send(ingestionEvent, bytes, sourceRecord.contentType);
    }

    # Deliver the deletion of one record; a delete carries no content.
    #
    # + recordId - the removed record's identity within its source
    # + sourceVersion - version of the deletion, ordered after the last upsert
    # + sourceObservedAt - time the source removed the record (RFC 3339)
    # + return - the accepted job handle, or an error
    remote isolated function remove(string recordId, string sourceVersion, string sourceObservedAt)
            returns JobAccepted|error {
        IngestionEvent ingestionEvent = {
            spaceId: self.destination.spaceId,
            sourceId: self.destination.sourceId,
            sourceRecordId: recordId,
            sourceVersion,
            operation: "delete",
            sourceObservedAt,
            audience: self.destination.audience,
            sourceAclVersion: self.destination.sourceAclVersion,
            idempotencyKey: idempotencyKey(self.destination.sourceId, "delete", recordId,
                    sourceVersion)
        };
        return self.send(ingestionEvent);
    }

    // Deliver, and after a lost reply ask the engine whether it accepted the delivery.
    // A transport failure can happen after the engine stored the event; finding the job
    // by its key avoids sending the content again.
    private isolated function send(IngestionEvent ingestionEvent, byte[]? content = (),
            string contentType = "application/octet-stream") returns JobAccepted|error {
        JobAccepted|error accepted = self.engineClient->deliver(ingestionEvent, content, contentType);
        if accepted is JobAccepted || accepted is http:ApplicationResponseError {
            return accepted;
        }
        JobAccepted|error? found = self.engineClient->findDelivery(ingestionEvent.sourceId,
                ingestionEvent.idempotencyKey);
        return found is JobAccepted ? found : accepted;
    }
}

// The bytes to deliver: raw bytes as given, or text as UTF-8.
isolated function contentOf(SourceRecord sourceRecord) returns byte[]|error {
    byte[]? rawBytes = sourceRecord?.contentBytes;
    if rawBytes is byte[] {
        return rawBytes;
    }
    string? text = sourceRecord?.content;
    if text is string {
        return text.toBytes();
    }
    return error("source record has neither content nor contentBytes");
}

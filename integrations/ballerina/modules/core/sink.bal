// The single place that turns a connector's `SourceRecord` into a canonical
// ingestion event and submits it.

import ballerina/crypto;
import ballerina/time;

# Ingests normalized records into the engine.
#
# Provided to each connector by the runtime; it owns everything provider-neutral:
# hashing content, deriving the idempotency key, and building the envelope.
public client class RecordSink {
    private final EngineClient engineClient;
    private final Destination destination;

    # Create a sink bound to an engine client and a destination.
    #
    # + engineClient - the shared engine client
    # + destination - the engine-side space/source this connector writes to
    public function init(EngineClient engineClient, Destination destination) {
        self.engineClient = engineClient;
        self.destination = destination;
    }

    # Normalize and ingest one source record as an `upsert`.
    #
    # + sourceRecord - the normalized record to ingest
    # + return - the accepted job handle, or an error
    remote function ingest(SourceRecord sourceRecord) returns JobAccepted|error {
        // The engine no longer accepts inline content: an upsert stages the content
        // bytes first, then references the staged object. Text content is staged as
        // UTF-8; binary sources (e.g. file uploads) supply raw `contentBytes`.
        byte[]? rawBytes = sourceRecord?.contentBytes;
        string? text = sourceRecord?.content;
        byte[] bytes;
        if rawBytes is byte[] {
            bytes = rawBytes;
        } else if text is string {
            bytes = text.toBytes();
        } else {
            return error("source record has neither content nor contentBytes");
        }
        // A content-addressed hash gives a stable version when the source has none.
        byte[] digest = crypto:hashSha256(bytes);
        string hashHex = digest.toBase16().toLowerAscii();
        string sourceVersion = sourceRecord?.sourceVersion ?: hashHex;
        string[] audience = sourceRecord?.audience ?: self.destination.audience;
        string key = idempotencyKey(self.destination.spaceId, self.destination.sourceId,
                sourceRecord.recordId, sourceVersion);

        // Stage the content, then echo the engine's own contentRef/hash/type so the
        // event verifies against the staged object exactly.
        StagedUpload upload = check self.engineClient->stageUpload(self.destination.sourceId,
                sourceRecord.contentType, bytes, key);
        IngestionEvent ingestionEvent = {
            spaceId: self.destination.spaceId,
            sourceId: self.destination.sourceId,
            sourceRecordId: sourceRecord.recordId,
            sourceVersion: sourceVersion,
            operation: "upsert",
            contentType: upload.contentType,
            contentRef: upload.uploadId,
            contentHash: upload.contentHash,
            sourceObservedAt: sourceRecord?.sourceObservedAt ?: time:utcToString(time:utcNow()),
            audience: audience,
            sourceAclVersion: self.destination.sourceAclVersion,
            idempotencyKey: key
        };
        string? title = sourceRecord?.title;
        if title is string {
            ingestionEvent.title = title;
        }
        string? sourceUrl = sourceRecord?.sourceUrl;
        if sourceUrl is string {
            ingestionEvent.sourceUrl = sourceUrl;
        }
        return self.engineClient->ingest(ingestionEvent);
    }

    # Ingest a deletion of a source record.
    #
    # A `delete` carries only the record's identity; the engine requires no
    # content for it. Used by change-capture connectors when a source record is
    # removed.
    #
    # + recordId - stable logical identity of the removed record within its source
    # + sourceVersion - monotonic version of this deletion (e.g. a source change number)
    # + sourceObservedAt - time the source produced this deletion (RFC 3339); defaults
    #   to now(). Supplying the source's real timestamp keeps replays byte-identical.
    # + return - the accepted job handle, or an error
    remote function remove(string recordId, string sourceVersion, string? sourceObservedAt = ())
            returns JobAccepted|error {
        IngestionEvent ingestionEvent = {
            spaceId: self.destination.spaceId,
            sourceId: self.destination.sourceId,
            sourceRecordId: recordId,
            sourceVersion: sourceVersion,
            operation: "delete",
            sourceObservedAt: sourceObservedAt ?: time:utcToString(time:utcNow()),
            audience: self.destination.audience,
            sourceAclVersion: self.destination.sourceAclVersion,
            idempotencyKey: idempotencyKey(self.destination.spaceId, self.destination.sourceId,
                    recordId, sourceVersion)
        };
        return self.engineClient->ingest(ingestionEvent);
    }
}

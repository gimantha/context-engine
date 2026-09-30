// Provider-neutral data a connector produces and is configured with.

# The change a record expresses. Its values are the wire operations, so the sink and the poll
# job compare against them directly.
public enum Operation {
    # Create or update the record with its content.
    UPSERT = "upsert",
    # Remove the record; it carries no content.
    DELETE = "delete"
}

# A normalized source record produced by a connector.
#
# The connector supplies the record's content, version, and observed time; the sink
# hashes the content, derives the idempotency key, and delivers it. The version and
# observed time are required, not defaulted: both are part of the event the engine
# deduplicates on, so they must be the same every time the same record state is sent.
# A default such as the current time would turn every retry into a conflict.
public type SourceRecord record {|
    # Stable logical identity of the record within its source.
    string recordId;
    # Normalized textual content of the record. Provide this or `contentBytes`; text is
    # sent as UTF-8.
    string content?;
    # Raw binary content, for non-text sources such as file uploads. Provide this or
    # `content`. Sent as-is under `contentType`.
    byte[] contentBytes?;
    # Operation for this change: `UPSERT` (default) or `DELETE`. A delete carries no content
    # and uses only `recordId`, `sourceVersion`, and `sourceObservedAt`; the poll job routes
    # it to `Sink.remove` instead of `Sink.ingest`. This lets a poll connector express
    # removals in the same ordered batch as upserts (a change feed interleaves both).
    Operation operation = UPSERT;
    # Version of this record state, compared by the engine under the source's version
    # ordering. Connectors use epoch milliseconds so versions are numeric and only grow;
    # register such sources with numeric ordering.
    string sourceVersion;
    # Time the source produced this record state (RFC 3339). Taken from the source, never
    # the clock at send time, so a resend of the same state is a clean replay.
    string sourceObservedAt;
    # MIME type of the content. It must be on the engine's allowlist.
    string contentType = "text/plain";
    # Optional canonical source URL for lineage.
    string sourceUrl?;
    # Optional per-record audience override; defaults to the connector audience.
    string[] audience?;
    # Poll position just after this record, set by poll connectors. The poll job saves it
    # once the record is delivered, so a failure later in a batch resumes after the last
    # delivered record instead of skipping the rest of the batch.
    string pollCursor?;
|};

# The engine-side destination a connector writes to.
#
# Identifies the target space and the source identity and authorization every record
# from this connector instance is delivered under. One source belongs to one space, and
# each running instance must write to its own source.
public type Destination record {|
    # Target context space that owns the ingested records.
    string spaceId;
    # Registered source identifier for the connector.
    string sourceId;
    # Source audience labels, mapped to engine audiences by the source's mapping.
    string[] audience;
    # Version of the source ACL that authorizes the audience.
    string sourceAclVersion;
|};

# Durable storage for a connector's resume positions: poll cursors and change-event
# replay ids.
#
# Implementations must be safe for concurrent use, because a poll job and a change
# listener of the same instance save positions independently.
public type CheckpointStore isolated object {
    # Return the stored position for a key, or `()` if none has been saved.
    #
    # + key - the position's name within the instance, such as "poll"
    # + return - the stored position, `()`, or an error if it cannot be read
    public isolated function load(string key) returns string?|error;

    # Persist the position for a key.
    #
    # + key - the position's name within the instance
    # + value - the position to store
    # + return - an error if the position cannot be written
    public isolated function save(string key, string value) returns error?;
};

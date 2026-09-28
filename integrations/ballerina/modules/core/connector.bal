// The connector contract for Context Engine source integrations.
//
// The managed runtime drives connectors by their modality:
//   - Pull connectors implement `PollConnector`; the `ConnectorManager` schedules
//     `fetch` on an interval (see `poll_job.bal`).
//   - Push or streaming connectors implement `ListenConnector`; the manager calls
//     `listen` once to attach listeners, such as a SaaS change-event listener.
//
// The framework owns everything provider-neutral behind `Sink` (see `sink.bal`):
// building the ingestion event, hashing content, deriving the idempotency key, and
// delivering it. Connectors only decide how records are produced and normalized.

# A pull connector. The manager owns the loop and schedules `fetch` on an interval;
# each call returns the records changed since `cursor` plus the next cursor. A failing
# `fetch` is logged and retried on the next tick rather than stopping the connector.
public type PollConnector object {
    # Fetch records changed since `cursor`.
    #
    # + cursor - the last saved cursor ("" on the first poll)
    # + return - new records plus the next cursor, or an error
    public function fetch(string cursor) returns FetchResult|error;
};

# A push or streaming connector. `listen` attaches its listeners into the sink and
# returns promptly; the manager keeps the runtime alive, so `listen` must not block.
public type ListenConnector object {
    # Attach listeners that deliver into `sink`, then return.
    #
    # + sink - the delivery sink provided by the runtime
    # + checkpoints - durable storage for the listener's resume position, so a restart
    #   continues where it stopped instead of skipping what changed while it was down
    # + return - an error if a listener fails to attach or start
    public function listen(Sink sink, CheckpointStore checkpoints) returns error?;
};

# Result of one poll: the records to ingest and the next checkpoint cursor.
public type FetchResult record {|
    # Records discovered since the supplied cursor, in cursor order.
    SourceRecord[] records;
    # Cursor to save once every record is delivered, and to pass to the next poll.
    string cursor;
|};

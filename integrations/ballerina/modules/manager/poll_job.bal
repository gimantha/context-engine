// The scheduled unit of work for a managed pull connector.

import ballerina/log;
import ballerina/task;

import context_engine_connectors.core;

// The checkpoint position name of a poll cursor.
const string POLL_POSITION = "poll";

# One scheduled poll cycle for a pull connector.
#
# Runs `fetch`, delivers each record in order, and saves how far delivery got. The
# cursor only moves past records the engine accepted, or refused for a reason in the
# record itself: a failure of any other kind stops the batch, and the next tick
# resends from the last delivered record. Delivery is idempotent, so resending records
# the engine already has is a clean replay. Errors are logged rather than propagated,
# so one bad poll never stops the schedule.
class PollJob {
    *task:Job;

    private final core:PollConnector connector;
    private final core:Sink sink;
    private final core:CheckpointStore checkpoints;
    private final string instanceId;
    private string cursor;

    function init(core:PollConnector connector, core:Sink sink, string instanceId,
            core:CheckpointStore checkpoints, string cursor) {
        self.connector = connector;
        self.sink = sink;
        self.instanceId = instanceId;
        self.checkpoints = checkpoints;
        self.cursor = cursor;
    }

    public function execute() {
        core:FetchResult|error result = self.connector.fetch(self.cursor);
        if result is error {
            log:printError("poll failed", 'error = result, instance = self.instanceId);
            return;
        }
        string reached = self.deliver(result);
        if reached == self.cursor {
            return;
        }
        error? saved = self.checkpoints.save(POLL_POSITION, reached);
        if saved is error {
            // The cursor stays where it was; the next tick resends and replays.
            log:printError("checkpoint persist failed", 'error = saved, instance = self.instanceId,
                    cursor = reached);
            return;
        }
        self.cursor = reached;
    }

    // Deliver the batch in order and return the cursor delivery reached.
    function deliver(core:FetchResult result) returns string {
        string reached = self.cursor;
        foreach core:SourceRecord sourceRecord in result.records {
            core:JobAccepted|error accepted = self.sink->ingest(sourceRecord);
            if accepted is error {
                if !core:isRecordRejection(accepted) {
                    log:printError("delivery failed; the next poll resends from here",
                            'error = accepted, instance = self.instanceId,
                            recordId = sourceRecord.recordId);
                    return reached;
                }
                // Resending the same record cannot succeed, so the poll moves past it.
                log:printError("record rejected by the engine; skipping it", 'error = accepted,
                        instance = self.instanceId, recordId = sourceRecord.recordId);
            }
            string? position = sourceRecord?.pollCursor;
            if position is string {
                reached = position;
            }
        }
        return result.cursor;
    }
}

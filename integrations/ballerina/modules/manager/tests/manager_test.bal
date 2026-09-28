// Tests for the manager: how far a poll cursor advances, instance validation, the
// engine-backed checkpoint store, and configuration sourcing.

import ballerina/http;
import ballerina/test;

import context_engine_connectors.core;

// A sink that fails chosen records with a chosen error and remembers what it accepted.
client class FakeSink {
    *core:Sink;

    private final map<error> failures;
    string[] delivered = [];

    function init(map<error> failures = {}) {
        self.failures = failures;
    }

    remote function ingest(core:SourceRecord sourceRecord) returns core:JobAccepted|error {
        error? failure = self.failures[sourceRecord.recordId];
        if failure is error {
            return failure;
        }
        self.delivered.push(sourceRecord.recordId);
        return {jobId: "job_" + sourceRecord.recordId, statusUrl: "/v1/jobs/job_" + sourceRecord.recordId};
    }

    remote function remove(string recordId, string sourceVersion, string sourceObservedAt)
            returns core:JobAccepted|error {
        return {jobId: "job_delete", statusUrl: "/v1/jobs/job_delete"};
    }
}

// A poll connector that returns one fixed batch of three records.
class FixedBatch {
    *core:PollConnector;

    public function fetch(string cursor) returns core:FetchResult|error {
        core:SourceRecord[] records = from string id in ["a", "b", "c"]
            select {
                recordId: id,
                content: id,
                sourceVersion: "1",
                sourceObservedAt: "2026-09-28T10:00:00Z",
                pollCursor: "after-" + id
            };
        return {records, cursor: "after-c"};
    }
}

function runOnce(map<error> failures) returns [string?, string[]]|error {
    InMemoryCheckpointStore checkpoints = new;
    FakeSink sink = new (failures);
    PollJob job = new (new FixedBatch(), sink, "instance-1", checkpoints, "");
    job.execute();
    return [check checkpoints.load(POLL_POSITION), sink.delivered];
}

function rejected() returns error {
    return error http:ClientRequestError("invalid", statusCode = 400, headers = {}, body = ());
}

function unavailable() returns error {
    return error http:RemoteServerError("unavailable", statusCode = 503, headers = {}, body = ());
}

@test:Config {}
function aCleanBatchSavesTheBatchCursor() returns error? {
    [string?, string[]] [saved, delivered] = check runOnce({});
    test:assertEquals(saved, "after-c");
    test:assertEquals(delivered, ["a", "b", "c"]);
}

@test:Config {}
function aTransientFailureStopsAfterTheLastDeliveredRecord() returns error? {
    [string?, string[]] [saved, delivered] = check runOnce({"c": unavailable()});
    test:assertEquals(saved, "after-b", "the next poll resends c instead of skipping it");
    test:assertEquals(delivered, ["a", "b"]);
}

@test:Config {}
function aFailureOnTheFirstRecordSavesNothing() returns error? {
    [string?, string[]] [saved, _] = check runOnce({"a": error("connection refused")});
    test:assertEquals(saved, ());
}

@test:Config {}
function aRejectedRecordIsSkipped() returns error? {
    [string?, string[]] [saved, delivered] = check runOnce({"b": rejected()});
    test:assertEquals(saved, "after-c", "resending a rejected record cannot help");
    test:assertEquals(delivered, ["a", "c"]);
}

function config(string instanceId, string sourceId) returns ConnectorInstanceConfig {
    return {
        instanceId,
        connectorType: "salesforce",
        destination: {spaceId: "spc_1", sourceId, audience: ["src:research"], sourceAclVersion: "1"}
    };
}

@test:Config {}
function instancesNeedTheirOwnIdAndSource() {
    test:assertTrue(validateInstances([config("a", "src_1"), config("b", "src_2")]) is ());
    error? sameId = validateInstances([config("a", "src_1"), config("a", "src_2")]);
    error? sameSource = validateInstances([config("a", "src_1"), config("b", "src_1")]);
    test:assertTrue(sameId is error, "duplicate ids would share log identity");
    test:assertTrue(sameSource is error, "two instances would overwrite one source checkpoint");
}

@test:Config {}
function anUnsetConfigurationMeansNoConnectors() returns error? {
    EnvConfigProvider provider = new ("CONTEXT_ENGINE_TEST_UNSET_VARIABLE");
    test:assertEquals((check provider.provide()).length(), 0);
}

// A stand-in for the engine's checkpoint routes.
listener http:Listener mockCheckpoints = new (9591);

isolated map<string> storedCursors = {};

service /v1 on mockCheckpoints {
    resource function get sources/[string sourceId]/checkpoints() returns http:Response {
        string? cursor;
        lock {
            cursor = storedCursors[sourceId];
        }
        http:Response response = new;
        if cursor is () {
            response.statusCode = 404;
            response.setJsonPayload({code: "not_found", message: "Checkpoint not found"});
        } else {
            response.setJsonPayload({sourceId, cursor, updatedAt: "2026-09-28T10:00:00Z"});
        }
        return response;
    }

    resource function put sources/[string sourceId]/checkpoints(@http:Payload json body)
            returns json|error {
        string cursor = (check body.cursor).toString();
        lock {
            storedCursors[sourceId] = cursor;
        }
        return {sourceId, cursor, updatedAt: "2026-09-28T10:00:00Z"};
    }
}

@test:Config {}
function engineCheckpointsKeepEveryPositionAcrossRestarts() returns error? {
    core:EngineClient engine = check new ({baseUrl: "http://127.0.0.1:9591", bearerToken: "t"});
    EngineCheckpointStore first = new (engine, "src_sf");
    test:assertEquals(check first.load(POLL_POSITION), (), "nothing is stored yet");
    check first.save(POLL_POSITION, "2026-09-28T10:00:00.000Z|001000000000001AAA");
    check first.save("replay:/data/AccountChangeEvent", "42");

    // A new store for the same source, as after a restart, reads both positions back.
    EngineCheckpointStore restarted = new (engine, "src_sf");
    test:assertEquals(check restarted.load(POLL_POSITION), "2026-09-28T10:00:00.000Z|001000000000001AAA");
    test:assertEquals(check restarted.load("replay:/data/AccountChangeEvent"), "42");
}

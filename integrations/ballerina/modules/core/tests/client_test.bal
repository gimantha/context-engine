// Tests for the engine client and sink: the exact one-call request on the wire, key
// derivation, and which engine errors count as a rejection of the record itself.

import ballerina/crypto;
import ballerina/http;
import ballerina/mime;
import ballerina/test;

// A stand-in for the engine's one-call route. It answers with a job id that describes
// what it received, so each test can check the request without shared state.
listener http:Listener mockEngine = new (9590);

service /v1 on mockEngine {
    resource function post sources/[string sourceId]/ingestions(http:Request request)
            returns http:Response|error {
        http:Response response = new;
        if sourceId == "src_rejecting" {
            response.statusCode = 400;
            response.setJsonPayload({code: "invalid_request", message: "Request is invalid"});
            return response;
        }
        if sourceId == "src_down" {
            response.statusCode = 503;
            response.setJsonPayload({code: "unavailable", message: "Try later"});
            return response;
        }
        mime:Entity[] parts = check request.getBodyParts();
        string[] names = from mime:Entity part in parts
            select part.getContentDisposition().name;
        json event = check parts[0].getJson();
        string header = check request.getHeader("Idempotency-Key");
        string auth = check request.getHeader("Authorization");
        string content = parts.length() > 1 ? check parts[1].getText() : "";
        string contentType = parts.length() > 1 ? parts[1].getContentType() : "";
        response.statusCode = 202;
        response.setJsonPayload({
            jobId: string:'join("|", string:'join(",", ...names), (check event.operation).toString(),
                    content, contentType, (header == (check event.idempotencyKey).toString()).toString(),
                    auth, event.toJsonString()),
            statusUrl: "/v1/jobs/job_test"
        });
        return response;
    }
}

final EngineClient testClient = check new ({baseUrl: "http://127.0.0.1:9590", bearerToken: "connector-token"});

function destination(string sourceId) returns Destination {
    return {spaceId: "spc_test", sourceId, audience: ["src:research"], sourceAclVersion: "7"};
}

@test:Config {}
function upsertsSendTheEventFirstAndTheContentSecond() returns error? {
    RecordSink sink = new (testClient, destination("src_files"));
    JobAccepted accepted = check sink->ingest({
        recordId: "runbook.txt",
        content: "Confirm the rollback checkpoint.",
        sourceVersion: "1790000000000",
        sourceObservedAt: "2026-09-28T10:00:00Z"
    });
    string[] seen = re `\|`.split(accepted.jobId);
    test:assertEquals(seen[0], "event,content", "parts arrive event first, content second");
    test:assertEquals(seen[1], "upsert");
    test:assertEquals(seen[2], "Confirm the rollback checkpoint.");
    test:assertTrue(seen[3].startsWith("text/plain"));
    test:assertEquals(seen[4], "true", "the header key matches the event's key");
    test:assertEquals(seen[5], "Bearer connector-token");
    json event = check seen[6].fromJsonString();
    test:assertFalse((<map<json>>event).hasKey("contentRef"), "the one-call event has no contentRef");
    test:assertFalse((<map<json>>event).hasKey("title"), "the engine event has no title");
    test:assertEquals(check event.contentHash,
            "sha256:" + crypto:hashSha256("Confirm the rollback checkpoint.".toBytes()).toBase16(),
            "the event carries the content's hash for the engine to check");
    test:assertEquals(check event.sourceAclVersion, "7");
}

@test:Config {}
function deletesSendOnlyTheEvent() returns error? {
    RecordSink sink = new (testClient, destination("src_files"));
    JobAccepted accepted = check sink->remove("runbook.txt", "1790000000001", "2026-09-28T10:00:01Z");
    string[] seen = re `\|`.split(accepted.jobId);
    test:assertEquals(seen[0], "event");
    test:assertEquals(seen[1], "delete");
}

@test:Config {}
function onlyRecordLevelRefusalsCountAsRejections() {
    RecordSink rejecting = new (testClient, destination("src_rejecting"));
    RecordSink down = new (testClient, destination("src_down"));
    SourceRecord sourceRecord = {
        recordId: "r1",
        content: "x",
        sourceVersion: "1",
        sourceObservedAt: "2026-09-28T10:00:00Z"
    };
    JobAccepted|error refused = rejecting->ingest(sourceRecord);
    JobAccepted|error unavailable = down->ingest(sourceRecord);
    test:assertTrue(refused is error && isRecordRejection(refused), "400 is the record's own fault");
    test:assertTrue(unavailable is error && !isRecordRejection(unavailable),
            "503 is retried, never skipped");
    test:assertFalse(isRecordRejection(error("connection refused")), "transport errors are retried");
}

@test:Config {}
function keysAreStableShortAndDistinct() {
    string longId = "x".padEnd(500, "y");
    string key = idempotencyKey("src_0123456789abcdef0123456789abcdef", "upsert", longId, "1790000000000");
    test:assertEquals(key, idempotencyKey("src_0123456789abcdef0123456789abcdef", "upsert", longId,
            "1790000000000"));
    test:assertTrue(key.length() <= 200, "the engine caps the Idempotency-Key header at 200");
    test:assertTrue(key.startsWith("src_0123456789abcdef0123456789abcdef:upsert:"));
    test:assertNotEquals(idempotencyKey("src_a", "upsert", "ab", "c"), idempotencyKey("src_a", "upsert",
            "a", "bc"), "record id and version cannot run into each other");
    test:assertNotEquals(idempotencyKey("src_a", "upsert", "r", "1"), idempotencyKey("src_a", "delete",
            "r", "1"));
}

@test:Config {}
function recordsNeedContent() {
    byte[]|error missing = contentOf({recordId: "r", sourceVersion: "1", sourceObservedAt: "t"});
    test:assertTrue(missing is error);
    test:assertEquals(contentOf({recordId: "r", content: "é", sourceVersion: "1", sourceObservedAt: "t"}),
            "é".toBytes());
}

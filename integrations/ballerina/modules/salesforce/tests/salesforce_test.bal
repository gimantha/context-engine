// Tests for the Salesforce connector's pure logic: versions, paging queries, cursor
// validation, change-event record ids, delete ordering, and replay checkpoints.

import ballerina/test;

import ballerinax/salesforce;

import context_engine_connectors.core;

@test:Config {}
function versionsAreEpochMillisOnOneAxis() returns error? {
    test:assertEquals(check epochMillis("2026-09-25T10:00:00.123+0000"), 1790330400123);
    test:assertEquals(millisToRfc3339(1790330400123), "2026-09-25T10:00:00.123Z");
    // A delete committed after the last modification orders after it numerically.
    int modified = check epochMillis("2026-09-25T10:00:00.123+0000");
    int deleted = check epochMillis("2026-09-25T10:00:05.000+0000");
    test:assertTrue(deleted > modified);
}

@test:Config {}
function aRowMapsToTheSameRecordEveryTime() returns error? {
    record {} row = {"Id": "001000000000001AAA", "Name": "Acme", "Industry": "Energy",
        "SystemModstamp": "2026-09-25T10:00:00.123+0000", "CreatedDate": "2026-09-01T09:00:00.000+0000"};
    core:SourceRecord mapped = check mapRecord(row, ["Name", "Industry"]);
    test:assertEquals(mapped, check mapRecord(row, ["Name", "Industry"]));
    test:assertEquals(mapped.recordId, "001000000000001AAA");
    test:assertEquals(mapped.sourceVersion, "1790330400123", "numeric, so numeric ordering accepts it");
    test:assertEquals(mapped.sourceObservedAt, "2026-09-25T10:00:00.123Z");
    test:assertEquals(mapped?.content, "{\"Name\":\"Acme\", \"Industry\":\"Energy\"}");
}

SalesforceSettings settings = {
    clientId: "id",
    clientSecret: "secret",
    baseUrl: "https://example.my.salesforce.com",
    sobject: "Account",
    fields: ["Name"],
    batchSize: 200
};

@test:Config {}
function pagingIsKeysetOnCreatedDateAndId() returns error? {
    test:assertEquals(check buildSoql(settings, ""),
            "SELECT Name, Id, SystemModstamp, CreatedDate FROM Account " +
            "ORDER BY CreatedDate ASC, Id ASC LIMIT 200");
    test:assertEquals(check buildSoql(settings, "2026-09-25T10:00:00.000Z|001000000000001AAA"),
            "SELECT Name, Id, SystemModstamp, CreatedDate FROM Account WHERE " +
            "CreatedDate > 2026-09-25T10:00:00.000Z OR (CreatedDate = 2026-09-25T10:00:00.000Z " +
            "AND Id > '001000000000001AAA') ORDER BY CreatedDate ASC, Id ASC LIMIT 200");
    test:assertEquals(rowCursor({"Id": "001000000000002AAA", "CreatedDate": "2026-09-25T10:00:00.000+0000"}),
            "2026-09-25T10:00:00.000Z|001000000000002AAA");
}

@test:Config {}
function cursorsCannotChangeTheQuery() {
    test:assertTrue(buildSoql(settings, "2026-09-25T10:00:00Z OR Name != ''|001000000000001AAA") is error);
    test:assertTrue(buildSoql(settings, "2026-09-25T10:00:00.000Z|x' OR Id != '") is error);
    test:assertTrue(buildSoql(settings, "2026-09-25T10:00:00.000Z") is error, "a cursor needs both parts");
    test:assertTrue(isRecordId("001000000000001") && isRecordId("001000000000001AAA"));
    test:assertFalse(isRecordId("001' OR Id != '"));
}

@test:Config {}
function everyRecordIdInAnEventIsHandled() {
    salesforce:EventData bulk = {
        changedData: {"ChangeEventHeader": {"recordIds": ["001000000000001AAA", "001000000000002AAA",
            "not an id", "001000000000003AAA"]}},
        metadata: {recordId: "001000000000001AAA"}
    };
    test:assertEquals(changedRecordIds(bulk),
            ["001000000000001AAA", "001000000000002AAA", "001000000000003AAA"]);
    // Without a header list, the single id from the metadata is used.
    salesforce:EventData single = {changedData: {}, metadata: {recordId: "001000000000009AAA"}};
    test:assertEquals(changedRecordIds(single), ["001000000000009AAA"]);
    // With no usable id there is nothing to deliver, never a record named after the channel.
    test:assertEquals(changedRecordIds({changedData: {}}), []);
}

@test:Config {}
function deletesNeedACommitTime() {
    test:assertEquals(commitMillis({"commitTimestamp": 1790330405000}), 1790330405000);
    test:assertEquals(commitMillis({"commitTimestamp": "1790330405000"}), 1790330405000);
    test:assertEquals(commitMillis({}), (), "no fallback to 0, which would always order first");
}

isolated class MemoryStore {
    *core:CheckpointStore;
    private final map<string> values = {};

    public isolated function load(string key) returns string?|error {
        lock {
            return self.values[key];
        }
    }

    public isolated function save(string key, string value) returns error? {
        lock {
            self.values[key] = value;
        }
    }
}

@test:Config {}
function replayPositionsGoToTheCheckpointStore() returns error? {
    MemoryStore store = new;
    ReplayCheckpointCoordinator coordinator = new (store);
    test:assertEquals(check coordinator.getCheckpoint("/data/AccountChangeEvent"), ());
    check coordinator.saveCheckpoint("/data/AccountChangeEvent", 42);
    test:assertEquals(check store.load("replay:/data/AccountChangeEvent"), "42");
    // A coordinator built after a restart resumes from the stored position.
    test:assertEquals(check (new ReplayCheckpointCoordinator(store)).getCheckpoint("/data/AccountChangeEvent"), 42);
    test:assertTrue(check coordinator.attemptLeadership("group", "node", 30));
}

@test:Config {}
function channelsFollowTheObjectName() {
    test:assertEquals(deriveChangeChannel("Account"), "/data/AccountChangeEvent");
    test:assertEquals(deriveChangeChannel("Employee__c"), "/data/Employee__ChangeEvent");
}

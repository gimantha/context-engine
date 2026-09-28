// Salesforce SOQL poll connector: initial backfill and a safety net for creates.
//
// It pages through the object by (CreatedDate, Id). Paging by CreatedDate alone with
// `CreatedDate > cursor` skipped records that shared the boundary timestamp, such as a
// bulk insert of hundreds of records in one second. The Id tie-breaker makes the order
// total, so every record is seen exactly once in order. Updates and deletes are the
// change listener's job; the listener also delivers creates, which covers records that
// commit after the poll has passed their CreatedDate.

import ballerina/time;

import ballerinax/salesforce;

import context_engine_connectors.core;

// Separates the CreatedDate and the Id in a poll cursor.
const string CURSOR_SEPARATOR = "|";

# Pull connector that pages through an SObject by CreatedDate and Id.
public class SalesforceSoqlConnector {
    *core:PollConnector;

    private final SalesforceSettings settings;
    private final salesforce:Client sfClient;

    # Create a connector bound to its settings and a Salesforce client.
    #
    # + settings - the Salesforce connector settings
    # + sfClient - authenticated Salesforce REST client
    public function init(SalesforceSettings settings, salesforce:Client sfClient) {
        self.settings = settings;
        self.sfClient = sfClient;
    }

    # Fetch the next page of records after `cursor`.
    #
    # Each record carries its own position, so the poll job can resume right after the
    # last record it delivered.
    #
    # + cursor - the last saved "CreatedDate|Id" position ("" on the first poll)
    # + return - the next page plus the position after its last record
    public function fetch(string cursor) returns core:FetchResult|error {
        stream<record {}, error?> rows = check self.sfClient->query(check buildSoql(self.settings, cursor));
        core:SourceRecord[] records = [];
        string nextCursor = cursor;
        check from record {} row in rows
            do {
                core:SourceRecord sourceRecord = check mapRecord(row, self.settings.fields);
                string position = rowCursor(row);
                sourceRecord.pollCursor = position;
                records.push(sourceRecord);
                nextCursor = position;
            };
        return {records, cursor: nextCursor};
    }
}

// Build the query for the page after `cursor`: rows strictly after (CreatedDate, Id).
isolated function buildSoql(SalesforceSettings settings, string cursor) returns string|error {
    string fieldList = selectFields(settings.fields, ["Id", "SystemModstamp", "CreatedDate"]);
    string soql = string `SELECT ${fieldList} FROM ${settings.sobject}`;
    if cursor != "" {
        [string, string] [createdDate, id] = check parseCursor(cursor);
        soql += string ` WHERE CreatedDate > ${createdDate} OR (CreatedDate = ${createdDate} AND Id > '${id}')`;
    }
    return soql + string ` ORDER BY CreatedDate ASC, Id ASC LIMIT ${settings.batchSize}`;
}

// The position of one row: its CreatedDate and Id.
isolated function rowCursor(record {} row) returns string {
    return toRfc3339(row["CreatedDate"].toString()) + CURSOR_SEPARATOR + row["Id"].toString();
}

// Split a saved cursor into its CreatedDate and Id, checking both before either is
// placed in a query.
isolated function parseCursor(string cursor) returns [string, string]|error {
    int? separator = cursor.indexOf(CURSOR_SEPARATOR);
    if separator is () {
        return error(string `poll cursor '${cursor}' is not a CreatedDate|Id position`);
    }
    string createdDate = cursor.substring(0, separator);
    string id = cursor.substring(separator + 1);
    _ = check time:utcFromString(createdDate);
    if !isRecordId(id) {
        return error(string `poll cursor '${cursor}' does not end in a record id`);
    }
    return [createdDate, id];
}

// Shared Salesforce helpers used by both the SOQL poll and the change-event listener:
// client construction, single-record fetch, and the row-to-SourceRecord mapping. Kept
// module-private so the mapping is identical across the two paths: a record delivered
// by a backfill poll and by a change-triggered re-fetch must be byte-identical, so the
// second delivery is a clean replay rather than a conflict.

import ballerina/time;

import ballerinax/salesforce;

import context_engine_connectors.core;

// Build an authenticated Salesforce REST client for any of the supported auth flows.
isolated function newClient(string baseUrl, string apiVersion, SalesforceAuth auth)
        returns salesforce:Client|error {
    return new ({
        baseUrl,
        auth: clientAuthConfig(auth, baseUrl),
        apiVersion
    });
}

// Query one record by Id and map it to a SourceRecord, or () if it no longer exists.
// Produces content identical to a backfill of the same record for the same `fields`.
function fetchRecordById(salesforce:Client sfClient, string sobject, string[] fields, string id)
        returns core:SourceRecord|error? {
    // The id goes into a query, so it must have the shape of a Salesforce id.
    if !isRecordId(id) {
        return error(string `not a Salesforce record id: '${id}'`);
    }
    string fieldList = selectFields(fields, ["Id", "SystemModstamp"]);
    string soql = string `SELECT ${fieldList} FROM ${sobject} WHERE Id = '${id}' LIMIT 1`;
    stream<record {}, error?> rows = check sfClient->query(soql);
    record {|record {} value;|}|error? next = rows.next();
    check rows.close();
    if next is error {
        return next;
    }
    if next is () {
        return ();
    }
    return mapRecord(next.value, fields);
}

// Map a SOQL row to a SourceRecord. Id and SystemModstamp form the envelope; the
// content is the projection of `fields` in order, so the same record maps identically
// from a backfill poll and a by-id re-fetch.
//
// The version is SystemModstamp as epoch milliseconds. That is numeric, so the source
// is registered with numeric ordering; it only grows as the record changes; and it is
// on the same time axis as a delete's commit timestamp, so a delete always orders after
// the record's last upsert.
isolated function mapRecord(record {} row, string[] fields) returns core:SourceRecord|error {
    map<anydata> projected = {};
    foreach string f in fields {
        projected[f] = row[f];
    }
    string modstamp = row["SystemModstamp"].toString();
    return {
        recordId: row["Id"].toString(),
        content: projected.toJsonString(),
        contentType: "application/json",
        sourceVersion: (check epochMillis(modstamp)).toString(),
        // The record's own modified time, so re-delivering the same version is a replay.
        sourceObservedAt: toRfc3339(modstamp)
    };
}

// Build a SELECT field list from the business fields plus any required extras, deduped.
isolated function selectFields(string[] fields, string[] required) returns string {
    string[] selected = fields.clone();
    foreach string r in required {
        if selected.indexOf(r) is () {
            selected.push(r);
        }
    }
    return string:'join(", ", ...selected);
}

// Normalize a Salesforce REST datetime (e.g. "...+0000") to the Z form, valid both as a
// SOQL datetime literal and as an RFC 3339 `sourceObservedAt`.
isolated function toRfc3339(string datetime) returns string {
    if datetime.endsWith("+0000") {
        return datetime.substring(0, datetime.length() - 5) + "Z";
    }
    return datetime;
}

// A Salesforce datetime as milliseconds since the epoch.
isolated function epochMillis(string datetime) returns int|error {
    time:Utc utc = check time:utcFromString(toRfc3339(datetime));
    return utc[0] * 1000 + <int>(utc[1] * 1000d).floor();
}

// Epoch milliseconds as an RFC 3339 timestamp.
isolated function millisToRfc3339(int millis) returns string {
    time:Utc utc = [millis / 1000, <decimal>(millis % 1000) / 1000d];
    return time:utcToString(utc);
}

// Salesforce record ids are 15 or 18 letters and digits. Ids are checked before they are
// placed in a SOQL query, so nothing of another shape can change the query.
isolated function isRecordId(string value) returns boolean {
    return re `[A-Za-z0-9]{15}([A-Za-z0-9]{3})?`.isFullMatch(value);
}

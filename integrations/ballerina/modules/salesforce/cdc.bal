// Salesforce Change Data Capture listener: updates, undeletes, and deletes.
//
// For an update or undelete the listener re-fetches the full record by Id and delivers
// that. The result is identical to a backfill of the same record, so when the poll and
// the listener both deliver a record, the second delivery is a clean replay. An update
// that changed none of the connector's projected fields is dropped: it would only
// re-deliver identical content under a new version. Creates are left to the poll
// backfill (it pages the object by CreatedDate). A delete delivers the removal by
// identity.
//
// One change event can cover many records: a transaction that updates or deletes 50
// Accounts arrives as one event listing 50 ids. The library's `metadata.recordId` holds
// only the first, so the ids are read from the event's full change header.
//
// Replay positions are stored durably through `ReplayCheckpointCoordinator`, so after a
// restart the subscription resumes where it stopped and deletes made while the host
// was down still arrive. The library records an event's replay position once it has
// been dispatched, whether or not handling succeeded, so the handlers retry transient
// engine failures themselves before giving up on a record.

import ballerina/lang.regexp;
import ballerina/lang.runtime;
import ballerina/log;

import ballerinax/salesforce;

import context_engine_connectors.core;

// Attempts per record before a change is logged and given up on; waits 1, 2, then 4
// seconds between them.
const int DELIVERY_ATTEMPTS = 4;

// Derive the CDC channel for an SObject, e.g. "Account" -> "/data/AccountChangeEvent"
// and "Employee__c" -> "/data/Employee__ChangeEvent".
isolated function deriveChangeChannel(string sobject) returns string {
    if sobject.endsWith("__c") {
        return string `/data/${sobject.substring(0, sobject.length() - 3)}__ChangeEvent`;
    }
    return string `/data/${sobject}ChangeEvent`;
}

# Listener connector that subscribes to an SObject's change channel.
public class SalesforceCdcConnector {
    *core:ListenConnector;

    private final SalesforceSettings settings;
    private final salesforce:Client sfClient;

    # Create a connector bound to its settings and a Salesforce client.
    #
    # + settings - the Salesforce connector settings
    # + sfClient - authenticated Salesforce REST client, used to re-fetch records
    public function init(SalesforceSettings settings, salesforce:Client sfClient) {
        self.settings = settings;
        self.sfClient = sfClient;
    }

    # Subscribe to the object's change channel and deliver received events, then return.
    #
    # + sink - the delivery sink provided by the runtime
    # + checkpoints - durable storage for the channel's replay position
    # + return - an error if the listener fails to attach or start
    public function listen(core:Sink sink, core:CheckpointStore checkpoints) returns error? {
        salesforce:RestBasedListenerConfig listenerConfig = {
            auth: listenerAuthConfig(self.settings.auth, self.settings.baseUrl),
            baseUrl: self.settings.baseUrl,
            // Used only until a replay position has been stored for the channel.
            replayFrom: self.settings.replayFrom,
            coordination: {coordinator: new ReplayCheckpointCoordinator(checkpoints)}
        };
        string channel = deriveChangeChannel(self.settings.sobject);
        salesforce:Listener changeListener = check new (listenerConfig);
        ChangeEventIngestService svc = new (sink, self.sfClient, self.settings.sobject,
                self.settings.fields, channel);
        check changeListener.attach(svc, channel);
        check changeListener.'start();
        runtime:registerListener(changeListener);
        log:printInfo("subscribed to salesforce change channel", channel = channel,
                sobject = self.settings.sobject);
    }
}

# Delivers one channel's change events into the sink.
service class ChangeEventIngestService {
    *salesforce:CdcService;

    private final core:Sink sink;
    private final salesforce:Client sfClient;
    private final string sobject;
    private final string[] fields;
    private final string channel;

    function init(core:Sink sink, salesforce:Client sfClient, string sobject,
            string[] fields, string channel) {
        self.sink = sink;
        self.sfClient = sfClient;
        self.sobject = sobject;
        self.fields = fields;
        self.channel = channel;
    }

    # Created records: ignored. The poll backfill owns creates, paging the object by
    # (CreatedDate, Id), so delivering them here as well would be redundant.
    #
    # + payload - the change event
    # + return - always `()`
    remote function onCreate(salesforce:EventData payload) returns error? {
    }

    # Updated records: re-fetch each and deliver it as an upsert, unless the update
    # changed none of the connector's projected fields.
    #
    # An update to an unobserved field still bumps SystemModstamp, so re-fetching it would
    # deliver identical projected content under a new version. Those events are dropped.
    #
    # + payload - the change event
    # + return - an error if any record could not be delivered
    remote function onUpdate(salesforce:EventData payload) returns error? {
        if !self.touchesObservedField(payload) {
            log:printInfo("update changed no observed field; skipping", channel = self.channel,
                    changedFields = changedFields(payload));
            return;
        }
        return self.refetchAndIngest(payload);
    }

    # Undeleted records: re-fetch each and deliver it as an upsert.
    #
    # + payload - the change event
    # + return - an error if any record could not be delivered
    remote function onRestore(salesforce:EventData payload) returns error? {
        return self.refetchAndIngest(payload);
    }

    # Deleted records: deliver each removal by identity.
    #
    # The delete's version is its commit time in epoch milliseconds, on the same axis as
    # the upsert versions, so it orders after the record's last change. An event without
    # a commit time cannot be ordered and is not delivered.
    #
    # + payload - the change event
    # + return - an error if any removal could not be delivered
    remote function onDelete(salesforce:EventData payload) returns error? {
        int? committed = commitMillis(changeMetadata(payload));
        if committed is () {
            log:printError("delete event without a commit time; skipping", channel = self.channel);
            return error("delete event without a commit time");
        }
        string version = committed.toString();
        string observedAt = core:millisToRfc3339(committed);
        error? failure = ();
        foreach string recordId in changedRecordIds(payload) {
            core:JobAccepted|error removed = self.removeWithRetries(recordId, version, observedAt);
            if removed is error {
                log:printError("delete not delivered", 'error = removed, recordId = recordId,
                        channel = self.channel);
                failure = removed;
            }
        }
        return failure;
    }

    # Log listener transport errors; the listener manages reconnection.
    #
    # + err - the listener error
    # + return - always `()`
    remote function onError(error err) returns error? {
        log:printError("salesforce cdc listener error", 'error = err, channel = self.channel);
    }

    // Whether the event changed any of the connector's projected fields. When the header
    // lists no changed fields, deliver rather than risk dropping a real change.
    private function touchesObservedField(salesforce:EventData payload) returns boolean {
        string[] changed = changedFields(payload);
        if changed.length() == 0 {
            return true;
        }
        foreach string 'field in self.fields {
            if changed.indexOf('field) !is () {
                return true;
            }
        }
        return false;
    }

    // Re-fetch every record the event names and deliver each one.
    private function refetchAndIngest(salesforce:EventData payload) returns error? {
        string[] recordIds = changedRecordIds(payload);
        if recordIds.length() == 0 {
            log:printError("change event without record ids; skipping", channel = self.channel);
            return;
        }
        error? failure = ();
        foreach string recordId in recordIds {
            core:SourceRecord|error? sourceRecord =
                fetchRecordById(self.sfClient, self.sobject, self.fields, recordId);
            if sourceRecord is () {
                // Deleted between the change event and the re-fetch; its delete event follows.
                continue;
            }
            core:JobAccepted|error delivered = sourceRecord is error
                ? sourceRecord
                : self.ingestWithRetries(sourceRecord);
            if delivered is error {
                log:printError("change not delivered", 'error = delivered, recordId = recordId,
                        channel = self.channel);
                failure = delivered;
            }
        }
        return failure;
    }

    private function ingestWithRetries(core:SourceRecord sourceRecord) returns core:JobAccepted|error {
        core:JobAccepted|error result = self.sink->ingest(sourceRecord);
        int attempt = 1;
        while result is error && attempt < DELIVERY_ATTEMPTS && !core:isRecordRejection(result) {
            runtime:sleep(backoffSeconds(attempt));
            result = self.sink->ingest(sourceRecord);
            attempt += 1;
        }
        return result;
    }

    private function removeWithRetries(string recordId, string version, string observedAt)
            returns core:JobAccepted|error {
        core:JobAccepted|error result = self.sink->remove(recordId, version, observedAt);
        int attempt = 1;
        while result is error && attempt < DELIVERY_ATTEMPTS && !core:isRecordRejection(result) {
            runtime:sleep(backoffSeconds(attempt));
            result = self.sink->remove(recordId, version, observedAt);
            attempt += 1;
        }
        return result;
    }
}

// Seconds to wait before the next attempt: 1, 2, 4, and so on.
isolated function backoffSeconds(int attempt) returns decimal {
    return <decimal>(1 << (attempt - 1));
}

// Every record id a change event names. The full list is in the change header of the
// event's data; the library's `metadata.recordId` keeps only the first. Anything that
// is not a record id is left out, since each id is later placed in a query.
isolated function changedRecordIds(salesforce:EventData payload) returns string[] {
    string[] ids = [];
    json header = payload.changedData["ChangeEventHeader"];
    if header is map<json> {
        json listed = header["recordIds"];
        if listed is json[] {
            foreach json id in listed {
                if id is string && isRecordId(id) {
                    ids.push(id);
                }
            }
        }
    }
    if ids.length() == 0 {
        json single = changeMetadata(payload)["recordId"];
        if single is string && isRecordId(single) {
            ids.push(single);
        }
    }
    return ids;
}

// The API names of the fields a change event altered, from its change header. Empty when
// the header carries none. The library delivers `ChangeEventHeader` either as a JSON
// object or as its Ballerina record toString (e.g. "{... changedFields=[Site, Name] ...}"),
// so both forms are handled.
isolated function changedFields(salesforce:EventData payload) returns string[] {
    json header = payload.changedData["ChangeEventHeader"];
    if header is map<json> {
        json listed = header["changedFields"];
        string[] names = [];
        if listed is json[] {
            foreach json name in listed {
                if name is string {
                    names.push(name);
                }
            }
        }
        return names;
    }
    if header is string {
        return parseChangedFieldNames(header);
    }
    return [];
}

// Extract the names from the `changedFields=[A, B]` segment of the header's toString
// form. Field API names never contain "," or "]", so a bracket scan is safe.
isolated function parseChangedFieldNames(string header) returns string[] {
    string marker = "changedFields=[";
    int? at = header.indexOf(marker);
    if at is () {
        return [];
    }
    int listStart = at + marker.length();
    int? end = header.indexOf("]", listStart);
    if end is () {
        return [];
    }
    string[] names = [];
    foreach string part in regexp:split(re `,`, header.substring(listStart, end)) {
        string name = part.trim();
        if name != "" {
            names.push(name);
        }
    }
    return names;
}

// The change-event header as a plain JSON map. It is serialized rather than read field
// by field because the library types numeric header fields as `int` but delivers them
// as strings, so a typed read would fail.
isolated function changeMetadata(salesforce:EventData payload) returns map<json> {
    salesforce:ChangeEventMetadata? metadata = payload?.metadata;
    if metadata is () {
        return {};
    }
    json meta = metadata.toJson();
    return meta is map<json> ? meta : {};
}

// The commit time of a change in epoch milliseconds, or () when the header lacks it.
isolated function commitMillis(map<json> meta) returns int? {
    json commitTimestamp = meta["commitTimestamp"];
    if commitTimestamp is int {
        return commitTimestamp;
    }
    if commitTimestamp is string {
        int|error parsed = int:fromString(commitTimestamp);
        return parsed is int ? parsed : ();
    }
    return ();
}

// The checkpoint position name of a channel's replay id.
isolated function replayPosition(string channel) returns string {
    return "replay:" + channel;
}

# Stores the listener's replay positions in the instance's checkpoint store.
#
# The library resumes a subscription from the coordinator's stored checkpoint and falls
# back to `replayFrom` only when there is none. Its default coordinator keeps
# checkpoints in memory, so every restart began again at the tip and missed the changes
# made while the host was down, deletes included. Leadership stays in memory: one host
# runs each instance.
isolated class ReplayCheckpointCoordinator {
    *salesforce:ListenerCoordinator;

    private final salesforce:InMemoryCoordinator leadership = new;
    private final core:CheckpointStore checkpoints;

    isolated function init(core:CheckpointStore checkpoints) {
        self.checkpoints = checkpoints;
    }

    public isolated function attemptLeadership(string groupId, string nodeId,
            decimal livenessInterval) returns boolean|error {
        return self.leadership.attemptLeadership(groupId, nodeId, livenessInterval);
    }

    public isolated function renewLeadership(string groupId, string nodeId) returns error? {
        return self.leadership.renewLeadership(groupId, nodeId);
    }

    public isolated function saveCheckpoint(string channel, int replayId) returns error? {
        return self.checkpoints.save(replayPosition(channel), replayId.toString());
    }

    public isolated function getCheckpoint(string channel) returns int|error? {
        string? stored = check self.checkpoints.load(replayPosition(channel));
        return stored is string ? check int:fromString(stored) : ();
    }

    public isolated function relinquishLeadership(string groupId, string nodeId) returns error? {
        return self.leadership.relinquishLeadership(groupId, nodeId);
    }
}

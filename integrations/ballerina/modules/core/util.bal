// Small provider-neutral utilities shared across connectors.

import ballerina/time;

# Convert an RFC 3339 datetime to milliseconds since the epoch.
#
# Connectors version records by epoch milliseconds — a numeric, only-growing value the engine
# can order. The input must already be valid RFC 3339 (e.g. "2026-09-25T10:00:00.123Z"); a
# provider whose timestamps use another form normalizes them before calling this.
#
# + rfc3339 - an RFC 3339 datetime
# + return - milliseconds since the epoch, or an error if it cannot be parsed
public isolated function epochMillis(string rfc3339) returns int|error {
    time:Utc utc = check time:utcFromString(rfc3339);
    return utc[0] * 1000 + <int>(utc[1] * 1000d).floor();
}

# Convert milliseconds since the epoch to an RFC 3339 datetime.
#
# + millis - milliseconds since the epoch
# + return - the RFC 3339 datetime
public isolated function millisToRfc3339(int millis) returns string {
    time:Utc utc = [millis / 1000, <decimal>(millis % 1000) / 1000d];
    return time:utcToString(utc);
}

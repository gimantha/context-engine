// The Salesforce connector: one registration that runs BOTH a SOQL poll (initial
// backfill, paged by CreatedDate and Id) and a CDC listener (creates, updates,
// undeletes, and deletes).
// Its `ConnectorType` sets both a poll factory and a listen factory, so the manager
// schedules the poll and attaches the listener for a single `salesforce` config —
// the user never has to know it is two connectors underneath.

import ballerinax/salesforce;

import context_engine_connectors.core;

# Registry key for the Salesforce connector.
public const SALESFORCE_TYPE = "salesforce";

# Settings for the Salesforce connector (shared by the SOQL poll and CDC listener).
#
# `auth` selects one of the supported OAuth2 flows (see `SalesforceAuth`). For
# server-to-server sync, prefer client-credentials: it involves no refresh token,
# so mandatory refresh-token rotation does not apply.
public type SalesforceSettings record {|
    # Authentication settings; one of the supported OAuth2 flows.
    SalesforceAuth auth;
    # Salesforce instance base URL, e.g. "https://<instance>.my.salesforce.com".
    # The OAuth token endpoint is derived from it.
    string baseUrl;
    # Salesforce REST API version.
    string apiVersion = "59.0";
    # The SObject to sync, e.g. "Account". Must have Change Data Capture enabled.
    string sobject;
    # Business fields to ingest as content.
    string[] fields;
    # CDC subscription start for the first run: -1 tip (new only), -2 last 72h, or a
    # replayId. Once a replay position has been stored, restarts resume from it instead.
    int replayFrom = -1;
    # Maximum records per backfill poll.
    int batchSize = 200;
|};

# The connector type registration to hand to `ConnectorManager.register`.
#
# Sets both factories: the manager schedules the SOQL poll AND attaches the CDC
# listener for one instance, so a single `salesforce` config runs the full sync.
#
# + return - the Salesforce connector type
public function salesforceType() returns core:ConnectorType {
    return {
        name: SALESFORCE_TYPE,
        pollFactory: createPollConnector,
        listenFactory: createListenConnector
    };
}

// Poll factory: SOQL for the backfill.
function createPollConnector(json settings) returns core:PollConnector|error {
    SalesforceSettings s = check settings.cloneWithType();
    salesforce:Client sfClient = check newClient(s.baseUrl, s.apiVersion, s.auth);
    return new SalesforceSoqlConnector(s, sfClient);
}

// Listen factory: CDC for creates, updates, undeletes, and deletes.
function createListenConnector(json settings) returns core:ListenConnector|error {
    SalesforceSettings s = check settings.cloneWithType();
    salesforce:Client sfClient = check newClient(s.baseUrl, s.apiVersion, s.auth);
    return new SalesforceCdcConnector(s, sfClient);
}

// The Google Drive connector: one poll registration that backfills a folder subtree and then
// follows the Drive Changes API for creates, updates, trashes, and deletes.
//
// It is poll-only: no public webhook is needed. Its `ConnectorType` sets a poll factory only,
// so the manager schedules `fetch` on an interval and delivers each batch (upserts and
// deletes) in change order.

import context_engine_connectors.core;

# Registry key for the Google Drive connector.
public const GOOGLE_DRIVE_TYPE = "google_drive";

# Settings for the Google Drive connector.
#
# `auth` selects one of the supported flows (see `GoogleDriveAuth`); `refresh_token` is the
# recommended flow for a long-running sync.
public type GoogleDriveSettings record {|
    # Authentication settings; one of the supported flows.
    GoogleDriveAuth auth;
    # The Drive folder to sync. Its whole subtree is backfilled, and changes are scoped to
    # files under it.
    string folderId;
    # Maximum changes requested per Changes API page.
    int pageSize = 200;
    # Files delivered per backfill poll. The backfill is resumable at this granularity: each
    # poll delivers the next chunk (ordered by file id) and saves its position, so a restart
    # continues after the last delivered file instead of re-downloading everything.
    int backfillBatchSize = 100;
|};

# The connector type registration to hand to `ConnectorManager.register`.
#
# Sets a poll factory only: the manager schedules the backfill-then-changes poll for one
# instance, so a single `google_drive` config runs the full sync.
#
# + return - the Google Drive connector type
public function googleDriveType() returns core:ConnectorType {
    return {
        name: GOOGLE_DRIVE_TYPE,
        pollFactory: createPollConnector
    };
}

// Poll factory: backfill plus the Changes API.
function createPollConnector(json settings) returns core:PollConnector|error {
    GoogleDriveSettings s = check settings.cloneWithType();
    return check new GoogleDrivePollConnector(s);
}

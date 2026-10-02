// Google Drive poll connector: an initial backfill of a folder subtree, then the Drive
// Changes API for creates, updates, trashes, and deletes.
//
// The cursor carries the phase:
//   - ""                          the very first poll: bookmark the change log, then begin
//                                 backfilling.
//   - "backfill|<token>|<lastId>" backfilling: deliver the next chunk of files (ordered by
//                                 id) after <lastId>, carrying the bookmark <token> for the
//                                 eventual handoff. Resumable: a restart continues after the
//                                 last delivered file, not from the start.
//   - "changes|<token>"           steady state: list the changes since <token> and turn each
//                                 into an upsert or a delete.
//
// Deletes ride the same ordered batch as upserts (the poll job routes a record with operation
// "delete" to the sink's removal path), so removals and edits apply in change order.
//
// The bookmark <token> is captured once, before the backfill starts, and carried through
// every backfill chunk. So when the backfill finishes and hands off to the changes phase, a
// file edited during the (possibly long, multi-poll) backfill is caught right after — a clean
// idempotent replay under the same version. The changes phase then resumes from the saved
// token after a restart, so trashes and deletes made while the host was down still arrive.

import ballerina/log;
import ballerina/time;

import ballerinax/googleapis.drive as drive;

import context_engine_connectors.core;

// Cursor prefix for the backfill phase; what follows is "<token>|<lastDeliveredId>".
const string BACKFILL_PREFIX = "backfill|";

// Cursor prefix for the ongoing changes phase; what follows is the Drive page token.
const string CHANGES_PREFIX = "changes|";

# Pull connector that backfills a Drive folder subtree, then follows the Changes API.
public class GoogleDrivePollConnector {
    *core:PollConnector;

    private final GoogleDriveSettings settings;
    private final DriveAuthFlow authFlow;
    private drive:Client? cachedClient = ();
    private int rebuildAtMillis = 0;

    # Create a connector bound to its settings.
    #
    # + settings - the Google Drive connector settings
    # + return - an error if the auth flow cannot be built
    public function init(GoogleDriveSettings settings) returns error? {
        self.settings = settings;
        self.authFlow = check newAuthFlow(settings.auth);
    }

    # Fetch the next batch of changes after `cursor`.
    #
    # + cursor - "" on the first poll (backfill), then "changes|<token>"
    # + return - the batch plus the next cursor, or an error
    public function fetch(string cursor) returns core:FetchResult|error {
        drive:Client driveClient = check self.currentClient();
        if cursor == "" {
            // Bookmark the change log before the backfill, then deliver the first chunk.
            string token = check driveClient->getStartPageToken();
            return self.backfill(driveClient, token, "");
        }
        if cursor.startsWith(BACKFILL_PREFIX) {
            [string, string] [token, lastId] = parseBackfill(cursor.substring(BACKFILL_PREFIX.length()));
            return self.backfill(driveClient, token, lastId);
        }
        if cursor.startsWith(CHANGES_PREFIX) {
            return self.changes(driveClient, cursor.substring(CHANGES_PREFIX.length()));
        }
        return error(string `unrecognized google drive cursor '${cursor}'`);
    }

    // The Drive client, rebuilt when the auth flow's credential has expired. A refresh-token
    // or bearer client is built once; a service-account client is rebuilt before its minted
    // token expires.
    private function currentClient() returns drive:Client|error {
        time:Utc now = time:utcNow();
        int nowMillis = now[0] * 1000;
        drive:Client? cached = self.cachedClient;
        if cached is drive:Client && nowMillis < self.rebuildAtMillis {
            return cached;
        }
        drive:Client built = check self.authFlow.buildClient();
        self.cachedClient = built;
        self.rebuildAtMillis = nowMillis + <int>(self.authFlow.validitySeconds() * 1000);
        return built;
    }

    // Deliver the next backfill chunk: the files under the folder ordered by id, after
    // `lastId`, capped to the batch size. Each record carries a per-file poll cursor, so an
    // interruption resumes after the last delivered file; when the last chunk is reached the
    // cursor advances to the changes phase from the carried bookmark `token`.
    private function backfill(drive:Client driveClient, string token, string lastId)
            returns core:FetchResult|error {
        drive:File[] files = check listSubtreeFiles(driveClient, self.settings.folderId);
        [drive:File[], string] [chunk, nextCursor] =
            planBackfill(files, token, lastId, self.settings.backfillBatchSize);
        log:printInfo("google drive backfill poll", folderId = self.settings.folderId,
                subtreeFiles = files.length(), delivering = chunk.length(), fromId = lastId,
                nextCursor = nextCursor);
        core:SourceRecord[] records = [];
        foreach drive:File file in chunk {
            string id = file?.id ?: "";
            // The listing is a minimal projection; re-read the file by id for its full
            // metadata (modifiedTime, ...) before mapping.
            core:SourceRecord|error? mapped = mapFileById(driveClient, id);
            if mapped is error {
                log:printError("skipping unmappable drive file", 'error = mapped, fileId = id);
            } else if mapped is core:SourceRecord {
                // Save progress after this file, so a restart resumes right after it.
                mapped.pollCursor = BACKFILL_PREFIX + token + "|" + id;
                records.push(mapped);
            }
        }
        return {records, cursor: nextCursor};
    }

    // Steady state: capture the next resume token before consuming the changes, then turn each
    // change into an upsert or a delete. The next token is captured first because the Drive
    // client does not surface the changes list's own newStartPageToken; a change committed
    // after the bookmark gets a later log position, so it is caught next poll, never missed.
    private function changes(drive:Client driveClient, string token) returns core:FetchResult|error {
        string nextToken = check driveClient->getStartPageToken();
        drive:ListChangesOptional optional = {
            includeRemoved: true,
            pageSize: self.settings.pageSize
        };
        stream<drive:Change> rows = check driveClient->listChanges(token, optional);
        core:SourceRecord[] records = [];
        map<string?> parentCache = {};
        int seen = 0;
        from drive:Change change in rows
        do {
            seen += 1;
            core:SourceRecord|error? mapped = self.changeToRecord(driveClient, change, parentCache);
            if mapped is error {
                log:printError("skipping unmappable drive change", 'error = mapped,
                        fileId = change?.fileId);
            } else if mapped is core:SourceRecord {
                records.push(mapped);
            }
        };
        // Log only when there was activity, so idle polls stay quiet.
        if seen > 0 {
            log:printInfo("google drive changes poll", fromToken = token, nextToken = nextToken,
                    changesSeen = seen, delivering = records.length());
        }
        return {records, cursor: CHANGES_PREFIX + nextToken};
    }

    // Turn one change into a record, or () to skip it:
    //   - a removal (hard delete or lost access) -> delete
    //   - a trashed file -> delete
    //   - a file no longer under the folder -> skip (it is not, or no longer, ours; skipping
    //     rather than deleting avoids a flood of deletes for unrelated drive activity)
    //   - an in-scope file -> upsert (mapFile skips folders and native files)
    //
    // The change's own file projection is minimal, so the file is re-read by id for its full
    // metadata (parents for the scope check, modifiedTime for the version).
    private function changeToRecord(drive:Client driveClient, drive:Change change,
            map<string?> parentCache) returns core:SourceRecord|error? {
        string? fileId = change?.fileId;
        if fileId is () {
            return ();
        }
        if change?.removed ?: false {
            return deleteRecord(fileId, change);
        }
        drive:File file = check fetchFile(driveClient, fileId);
        if file?.trashed ?: false {
            return deleteRecord(fileId, change);
        }
        if !check isWithinFolder(driveClient, file, self.settings.folderId, parentCache) {
            return ();
        }
        return mapFile(driveClient, file);
    }
}

// Split a backfill cursor's payload "<token>|<lastId>" into its parts. The token holds no
// "|", so the first separator divides them; a missing separator means no progress yet.
isolated function parseBackfill(string payload) returns [string, string] {
    int? separator = payload.indexOf("|");
    if separator is () {
        return [payload, ""];
    }
    return [payload.substring(0, separator), payload.substring(separator + 1)];
}

// Plan the next backfill chunk from the subtree listing: the files after `lastId` ordered by
// id, capped to `batch`, plus the cursor to advance to. While files remain the cursor stays in
// the backfill phase at the chunk's last id; when the chunk empties the subtree it hands off
// to the changes phase from the bookmark `token`. Ordering by the stable, unique file id gives
// a total order the list endpoint cannot (its orderBy has no id tiebreaker), so no file is
// skipped and a restart resumes exactly after the last delivered id.
isolated function planBackfill(drive:File[] files, string token, string lastId, int batch)
        returns [drive:File[], string] {
    drive:File[] remaining = from drive:File file in files
        where (file?.id ?: "") > lastId
        order by (file?.id ?: "") ascending
        select file;
    if remaining.length() == 0 {
        return [[], CHANGES_PREFIX + token];
    }
    int end = remaining.length() < batch ? remaining.length() : batch;
    drive:File[] chunk = remaining.slice(0, end);
    if end == remaining.length() {
        return [chunk, CHANGES_PREFIX + token];
    }
    string lastChunkId = chunk[chunk.length() - 1]?.id ?: lastId;
    return [chunk, BACKFILL_PREFIX + token + "|" + lastChunkId];
}

// A delete record for a change. Its version is the change's commit time in epoch milliseconds,
// on the same axis as upsert versions, so it orders after the file's last upsert. A change
// without a time cannot be ordered and is refused.
isolated function deleteRecord(string fileId, drive:Change change) returns core:SourceRecord|error {
    string? changeTime = change?.time;
    if changeTime is () {
        return error(string `drive change for '${fileId}' has no time; cannot order the delete`);
    }
    return {
        recordId: fileId,
        operation: core:DELETE,
        sourceVersion: (check core:epochMillis(changeTime)).toString(),
        sourceObservedAt: changeTime
    };
}

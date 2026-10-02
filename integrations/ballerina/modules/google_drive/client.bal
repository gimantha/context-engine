// Shared Google Drive helpers used by both the backfill and the change handling: client
// construction, the file-to-SourceRecord mapping, folder-subtree enumeration, and the
// subtree-membership check. Kept module-private so a file maps identically whether it comes
// from a backfill listing or a change re-read, making the second delivery a clean replay.

import ballerina/log;

import ballerinax/googleapis.drive as drive;

import context_engine_connectors.core;

// The file metadata the connector needs. Drive's list and changes endpoints return only a
// minimal projection (id, name, mimeType) by default, so every file is re-read by id with
// this field mask before it is mapped or scope-checked.
const string FILE_FIELDS = "id, name, mimeType, modifiedTime, parents, trashed, webViewLink";

// Build an authenticated Drive client for the given auth settings.
function newClient(GoogleDriveAuth auth) returns drive:Client|error {
    DriveAuthFlow flow = check newAuthFlow(auth);
    return flow.buildClient();
}

// Read a file's full metadata by id. The id is checked before it is placed in the request.
function fetchFile(drive:Client driveClient, string fileId) returns drive:File|error {
    if !isFileId(fileId) {
        return error(string `not a drive id: '${fileId}'`);
    }
    return driveClient->getFile(fileId, FILE_FIELDS);
}

// Read a file's full metadata by id and map it, or () to skip it (folder or native file).
function mapFileById(drive:Client driveClient, string fileId) returns core:SourceRecord|error? {
    return mapFile(driveClient, check fetchFile(driveClient, fileId));
}

// Map a Drive file to a SourceRecord, or () to skip it (a folder or a Google-native file).
// The content comes from the file's extractor; the envelope is stamped here so a backfill and
// a change re-read of the same file produce byte-identical deliveries.
//
// The version is the file's modifiedTime as epoch milliseconds: numeric (so the source is
// registered with numeric ordering), only-growing, and on the same axis as a delete's change
// time, so a delete always orders after the file's last upsert.
function mapFile(drive:Client driveClient, drive:File file) returns core:SourceRecord|error? {
    Content? content = check extractorFor(file).extract(driveClient, file);
    if content is () {
        return ();
    }
    string? id = file?.id;
    if id is () {
        return error("drive file has no id");
    }
    string? modified = file?.modifiedTime;
    if modified is () {
        return error(string `drive file '${id}' has no modifiedTime`);
    }
    return {
        recordId: id,
        contentBytes: content.bytes,
        contentType: content.contentType,
        sourceVersion: (check core:epochMillis(modified)).toString(),
        // The file's own modified time, so re-delivering the same version is a replay.
        sourceObservedAt: modified,
        sourceUrl: file?.webViewLink ?: string `https://drive.google.com/file/d/${id}/view`
    };
}

// Every content file under a folder subtree, found by walking folders breadth-first. Folders
// are enqueued and traversed; their non-folder children are collected. Trashed items are left
// out. Native Google files are collected here but dropped later by `mapFile`.
function listSubtreeFiles(drive:Client driveClient, string folderId) returns drive:File[]|error {
    drive:File[] files = [];
    string[] queue = [folderId];
    int head = 0;
    while head < queue.length() {
        string current = queue[head];
        head += 1;
        if !isFileId(current) {
            return error(string `not a drive folder id: '${current}'`);
        }
        stream<drive:File> rows =
            check driveClient->getAllFiles(string `'${current}' in parents and trashed = false`);
        int children = 0;
        int subfolders = 0;
        from drive:File file in rows
        do {
            children += 1;
            if (file?.mimeType ?: "") == FOLDER_MIME {
                subfolders += 1;
                string? id = file?.id;
                if id is string {
                    queue.push(id);
                }
            } else {
                files.push(file);
            }
        };
        log:printInfo("listed drive folder children", folderId = current, children = children,
                subfolders = subfolders);
    }
    return files;
}

// Whether a file lives anywhere under the configured folder, walking its parents upward. Each
// visited folder's parent is memoized in `parentCache` (folder id -> its parent, or () at a
// root) so a batch of changes does not re-fetch the same ancestors.
function isWithinFolder(drive:Client driveClient, drive:File file, string folderId,
        map<string?> parentCache) returns boolean|error {
    string[] queue = (file?.parents ?: []).clone();
    map<boolean> seen = {};
    int head = 0;
    while head < queue.length() {
        string current = queue[head];
        head += 1;
        if current == folderId {
            return true;
        }
        if seen.hasKey(current) {
            continue;
        }
        seen[current] = true;
        string? parent;
        if parentCache.hasKey(current) {
            parent = parentCache[current];
        } else {
            parent = check parentOf(driveClient, current);
            parentCache[current] = parent;
        }
        if parent is string {
            queue.push(parent);
        }
    }
    return false;
}

// The first parent of a folder, or () when it has none (a root). The id is checked before it
// is placed in a request.
function parentOf(drive:Client driveClient, string folderId) returns string?|error {
    if !isFileId(folderId) {
        return error(string `not a drive id: '${folderId}'`);
    }
    drive:File folder = check driveClient->getFile(folderId, "id, parents");
    string[] parents = folder?.parents ?: [];
    return parents.length() > 0 ? parents[0] : ();
}

// Drive resource ids are URL-safe: letters, digits, hyphen, and underscore. Ids are checked
// before they go into a query so nothing of another shape can change it.
isolated function isFileId(string value) returns boolean {
    return value.length() >= 10 && re `[A-Za-z0-9_\-]+`.isFullMatch(value);
}

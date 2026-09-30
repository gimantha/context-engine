// Tests for the Google Drive connector's pure logic: versions, id validation, MIME
// classification, extractor and auth-flow selection, the delete-record mapping, and the
// no-network cases of file mapping and subtree membership. They use a bearer client, which
// makes no network call at construction, so no live Drive is needed.

import ballerina/test;

import ballerinax/googleapis.drive as drive;

import context_engine_connectors.core;

@test:Config {}
function versionsAreEpochMillisOnOneAxis() returns error? {
    test:assertEquals(check core:epochMillis("2026-09-25T10:00:00.123Z"), 1790330400123);
    // A delete committed after the last modification orders after it numerically.
    int modified = check core:epochMillis("2026-09-25T10:00:00.123Z");
    int deleted = check core:epochMillis("2026-09-25T10:00:05.000Z");
    test:assertTrue(deleted > modified);
}

@test:Config {}
function fileIdsAreValidatedBeforeAQuery() {
    test:assertTrue(isFileId("1AbCdEf_gHiJkLmNoPqRsTuVwXyZ01234"));
    test:assertTrue(isFileId("0B-abc_DEF-1234567"));
    test:assertFalse(isFileId("short"), "too short to be a drive id");
    test:assertFalse(isFileId("has space in it here"), "spaces cannot enter a query");
    test:assertFalse(isFileId("x' or trashed = false or '"), "quotes cannot change a query");
}

@test:Config {}
function nativeGoogleTypesAreDetected() {
    test:assertTrue(isNativeGoogleType("application/vnd.google-apps.document"));
    test:assertTrue(isNativeGoogleType("application/vnd.google-apps.spreadsheet"));
    test:assertFalse(isNativeGoogleType("application/pdf"));
    test:assertFalse(isNativeGoogleType("text/plain"));
}

@test:Config {}
function extractorSelectionFollowsTheMimeType() {
    drive:File folder = {id: "folder0000000001", mimeType: "application/vnd.google-apps.folder"};
    drive:File doc = {id: "native0000000001", mimeType: "application/vnd.google-apps.document"};
    drive:File sheet = {id: "native0000000002", mimeType: "application/vnd.google-apps.spreadsheet"};
    drive:File slides = {id: "native0000000003", mimeType: "application/vnd.google-apps.presentation"};
    drive:File form = {id: "native0000000004", mimeType: "application/vnd.google-apps.form"};
    drive:File pdf = {id: "binary0000000001", mimeType: "application/pdf"};
    test:assertTrue(extractorFor(folder) is SkipExtractor, "folders are skipped");
    test:assertTrue(extractorFor(doc) is ExportExtractor, "docs export to their Office format");
    test:assertTrue(extractorFor(sheet) is ExportExtractor, "sheets export");
    test:assertTrue(extractorFor(slides) is ExportExtractor, "slides export");
    test:assertTrue(extractorFor(form) is SkipExtractor, "native types with no export mapping skip");
    test:assertFalse(extractorFor(pdf) is SkipExtractor, "other files are downloaded, not skipped");
}

@test:Config {}
function exportTargetsMapNativeTypesToOfficeFormats() {
    test:assertEquals(EXPORT_TARGETS["application/vnd.google-apps.document"],
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document");
    test:assertEquals(EXPORT_TARGETS["application/vnd.google-apps.spreadsheet"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet");
    test:assertEquals(EXPORT_TARGETS["application/vnd.google-apps.presentation"],
            "application/vnd.openxmlformats-officedocument.presentationml.presentation");
    test:assertTrue(EXPORT_TARGETS["application/vnd.google-apps.form"] is (),
            "types with no export mapping are absent");
}

@test:Config {}
function authFlowFactorySelectsByType() returns error? {
    DriveAuthFlow refresh = check newAuthFlow(
        {authType: "refresh_token", clientId: "c", clientSecret: "s", refreshToken: "r"});
    DriveAuthFlow bearer = check newAuthFlow({authType: "bearer", token: "t"});
    DriveAuthFlow serviceAccount = check newAuthFlow(
        {authType: "service_account", clientEmail: "svc@example.iam.gserviceaccount.com",
            privateKeyPath: "/tmp/key.pem"});
    test:assertTrue(refresh is RefreshTokenFlow);
    test:assertTrue(bearer is BearerFlow);
    test:assertTrue(serviceAccount is ServiceAccountFlow);
    // A self-refreshing or static credential is effectively never rebuilt; a minted
    // service-account token is short-lived.
    test:assertTrue(refresh.validitySeconds() > serviceAccount.validitySeconds());
}

@test:Config {}
function deletesTakeTheirVersionFromTheChangeTime() returns error? {
    drive:Change removal = {fileId: "file00000000001", removed: true, time: "2026-09-25T10:00:05.000Z"};
    core:SourceRecord deleted = check deleteRecord("file00000000001", removal);
    test:assertEquals(deleted.operation, core:DELETE);
    test:assertEquals(deleted.recordId, "file00000000001");
    test:assertEquals(deleted.sourceVersion, "1790330405000");
    test:assertEquals(deleted.sourceObservedAt, "2026-09-25T10:00:05.000Z");
}

@test:Config {}
function aDeleteNeedsAChangeTimeToOrderIt() {
    drive:Change noTime = {fileId: "file00000000001", removed: true};
    test:assertTrue(deleteRecord("file00000000001", noTime) is error,
            "without a time the delete cannot be ordered after the last upsert");
}

@test:Config {}
function mapFileSkipsFoldersAndUnexportableNativesWithoutDownloading() returns error? {
    // A bearer client makes no network call at construction, and skipped files are never
    // fetched, so these map to () without touching Drive. (A Doc/Sheet/Slides would export,
    // which is a network call, so it is not covered here.)
    drive:Client driveClient = check newClient({authType: "bearer", token: "t"});
    drive:File folder = {id: "folder0000000001", mimeType: "application/vnd.google-apps.folder",
        modifiedTime: "2026-09-25T10:00:00.000Z"};
    drive:File form = {id: "native0000000004", mimeType: "application/vnd.google-apps.form",
        modifiedTime: "2026-09-25T10:00:00.000Z"};
    test:assertTrue(check mapFile(driveClient, folder) is (), "a folder is not content");
    test:assertTrue(check mapFile(driveClient, form) is (), "a native file with no export mapping skips");
}

@test:Config {}
function subtreeMembershipShortCircuitsOnADirectParent() returns error? {
    // A direct parent match returns true before any parent is fetched, so no network is used.
    drive:Client driveClient = check newClient({authType: "bearer", token: "t"});
    string folderId = "folder0000000001";
    drive:File child = {id: "child00000000001", parents: [folderId]};
    drive:File rootless = {id: "child00000000002", parents: []};
    map<string?> cache = {};
    test:assertTrue(check isWithinFolder(driveClient, child, folderId, cache));
    test:assertFalse(check isWithinFolder(driveClient, rootless, folderId, cache),
            "a file with no parents is not under the folder");
}

@test:Config {}
function theChangesCursorRoundTrips() {
    string cursor = CHANGES_PREFIX + "TOKEN123";
    test:assertTrue(cursor.startsWith(CHANGES_PREFIX));
    test:assertEquals(cursor.substring(CHANGES_PREFIX.length()), "TOKEN123");
}

@test:Config {}
function backfillCursorsCarryTheTokenAndLastId() {
    test:assertEquals(parseBackfill("42|fileAbc"), ["42", "fileAbc"]);
    // No progress yet: the bookmark is present but no file has been delivered.
    test:assertEquals(parseBackfill("42|"), ["42", ""]);
    // Defensive: a payload with no separator is all token.
    test:assertEquals(parseBackfill("42"), ["42", ""]);
}

@test:Config {}
function backfillChunksAdvanceByIdThenHandOffToChanges() {
    drive:File[] files = [
        {id: "file00000000003"}, {id: "file00000000001"}, {id: "file00000000002"}
    ];
    // First chunk: sorted by id, capped to the batch size, cursor stays in the backfill phase
    // at the chunk's last id.
    [drive:File[], string] [chunk1, cursor1] = planBackfill(files, "42", "", 2);
    test:assertEquals(chunk1.length(), 2);
    test:assertEquals(chunk1[0]?.id, "file00000000001");
    test:assertEquals(chunk1[1]?.id, "file00000000002");
    test:assertEquals(cursor1, "backfill|42|file00000000002");

    // Next chunk resumes after the last delivered id; it is the last, so it hands off.
    [drive:File[], string] [chunk2, cursor2] = planBackfill(files, "42", "file00000000002", 2);
    test:assertEquals(chunk2.length(), 1);
    test:assertEquals(chunk2[0]?.id, "file00000000003");
    test:assertEquals(cursor2, "changes|42", "the last chunk hands off to the changes phase");

    // Nothing left after the final id: hand off with no records.
    [drive:File[], string] [chunk3, cursor3] = planBackfill(files, "42", "file00000000003", 2);
    test:assertEquals(chunk3.length(), 0);
    test:assertEquals(cursor3, "changes|42");
}

@test:Config {}
function anEmptyFolderBackfillsToChangesImmediately() {
    [drive:File[], string] [chunk, cursor] = planBackfill([], "7", "", 100);
    test:assertEquals(chunk.length(), 0);
    test:assertEquals(cursor, "changes|7");
}

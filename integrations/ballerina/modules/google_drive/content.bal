// How a Drive file becomes deliverable content — the connector's per-file-type variation
// point.
//
// Content handling is a `ContentExtractor` interface with one implementation per kind of
// file, chosen by the `extractorFor` selector:
//   - a regular file (PDF, text, image, ...) is downloaded as-is (`RawDownloadExtractor`);
//   - a Google Workspace native file that has an export mapping (Docs, Sheets, Slides) is
//     exported to its Office format (`ExportExtractor`);
//   - anything else native (folders, forms, drawings, shortcuts) is skipped (`SkipExtractor`).
// Adding another file type is one new implementation plus one branch here, not a change to the
// poll or mapping code.

import ballerina/log;

import ballerinax/googleapis.drive as drive;

# Google Workspace native files carry this MIME-type prefix. They have no raw bytes to
# download; those with an export mapping are exported, the rest are skipped.
const string GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps.";

# The MIME type of a Drive folder. Folders are traversed for their children, never delivered.
const string FOLDER_MIME = "application/vnd.google-apps.folder";

# Export targets for Google Workspace native files: the source native MIME type mapped to the
# format it is exported to. Docs, Sheets, and Slides export to their Office formats (.docx,
# .xlsx, .pptx), which preserve the most structure for the engine to extract. Native types not
# listed here (forms, drawings, shortcuts) have no useful export and are skipped.
final readonly & map<string> EXPORT_TARGETS = {
    "application/vnd.google-apps.document":
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.google-apps.spreadsheet":
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.google-apps.presentation":
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
};

# A file's content bytes and MIME type, ready to deliver.
public type Content record {|
    # The file's bytes.
    byte[] bytes;
    # The MIME type the bytes are delivered under.
    string contentType;
|};

# Produces deliverable content for a Drive file, or `()` to skip it. Implementations vary by
# file type; `extractorFor` is the single selector.
public type ContentExtractor object {
    # Extract a file's content, or `()` to skip it.
    #
    # + driveClient - the authenticated Drive client
    # + file - the file to extract
    # + return - the content, `()` to skip, or an error
    public function extract(drive:Client driveClient, drive:File file) returns Content|error?;
};

# Downloads a file's bytes as-is and delivers them under the file's own MIME type. The engine
# accepts the types on its allowlist (text, Markdown, HTML, JSON, PDF) and refuses others,
# which the poll treats as a per-record rejection and skips.
public class RawDownloadExtractor {
    *ContentExtractor;

    public function extract(drive:Client driveClient, drive:File file) returns Content|error? {
        string? id = file?.id;
        if id is () {
            return error("drive file has no id");
        }
        drive:FileContent content = check driveClient->getFileContent(id);
        return {bytes: content.content, contentType: content.mimeType};
    }
}

# Exports a Google Workspace native file (Doc, Sheet, Slides) to a downloadable format and
# delivers those bytes. Native files have no raw content, so `exportFile` converts them. The
# engine may refuse the Office formats until it can extract them, in which case the poll skips
# the record as a rejection; no connector change is needed once the engine adds support. Drive
# caps an export at 10 MB; a larger file's export fails and that record is logged and skipped.
public class ExportExtractor {
    *ContentExtractor;

    private final string exportMimeType;

    public function init(string exportMimeType) {
        self.exportMimeType = exportMimeType;
    }

    public function extract(drive:Client driveClient, drive:File file) returns Content|error? {
        string? id = file?.id;
        if id is () {
            return error("drive file has no id");
        }
        drive:FileContent content = check driveClient->exportFile(id, self.exportMimeType);
        return {bytes: content.content, contentType: self.exportMimeType};
    }
}

# Skips a file, logging why. Used for folders and native files with no export mapping.
public class SkipExtractor {
    *ContentExtractor;

    private final string reason;

    public function init(string reason) {
        self.reason = reason;
    }

    public function extract(drive:Client driveClient, drive:File file) returns Content|error? {
        log:printInfo("skipping drive file", reason = self.reason, fileId = file?.id,
                mimeType = file?.mimeType);
        return ();
    }
}

# Select the extractor for a file by its MIME type: a native file with an export mapping is
# exported, any other native file (folders included) is skipped, and everything else is
# downloaded raw.
#
# + file - the file to classify
# + return - the extractor to use
public function extractorFor(drive:File file) returns ContentExtractor {
    string mimeType = file?.mimeType ?: "";
    if isNativeGoogleType(mimeType) {
        string? exportTarget = EXPORT_TARGETS[mimeType];
        if exportTarget is string {
            return new ExportExtractor(exportTarget);
        }
        string reason = mimeType == FOLDER_MIME ? "folder" : "native file with no export mapping";
        return new SkipExtractor(reason);
    }
    return new RawDownloadExtractor();
}

# Whether a MIME type is a Google Workspace native type (Docs, Sheets, Slides, ...).
#
# + mimeType - the MIME type to test
# + return - true for a native Google type
public function isNativeGoogleType(string mimeType) returns boolean {
    return mimeType.startsWith(GOOGLE_NATIVE_PREFIX);
}

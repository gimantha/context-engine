// File upload endpoint: a host built-in that delivers uploaded files into one source.
//
// The engine binds every source to exactly one space, so the endpoint writes to one
// configured destination. An earlier version took the space from the request path while
// keeping a fixed source, which the engine refused for every space but the source's
// own. Files are passed through unmodified; the engine extracts text per content type,
// and accepts only the types on its upload allowlist.
//
// The endpoint listens on loopback unless configured otherwise, and anywhere else it
// requires an API key, so it is never an unauthenticated way into the engine.

import ballerina/crypto;
import ballerina/http;
import ballerina/lang.runtime;
import ballerina/log;
import ballerina/mime;
import ballerina/time;

import context_engine_connectors.core;

# Where and how the upload endpoint listens, and where it delivers.
public type UploadConfig record {|
    # Address to bind. Loopback by default; any other address needs `apiKey`.
    string host = "127.0.0.1";
    # Port to bind.
    int port = 9090;
    # The space and source uploaded files are delivered to. The connector's engine
    # credential needs `ingest.write` on this source.
    core:Destination destination;
    # Key callers must send as `Authorization: Bearer <key>`. Empty means no key, which is
    # allowed only on a loopback address.
    string apiKey = "";
|};

# Start the upload endpoint. Called directly by the host, not the manager.
#
# + engineClient - the shared engine client
# + config - listening address, destination, and API key
# + return - an error if the configuration is unsafe or incomplete, or the listener fails
public function serveUploads(core:EngineClient engineClient, UploadConfig config) returns error? {
    core:Destination destination = config.destination;
    if destination.spaceId == "" || destination.sourceId == "" || destination.audience.length() == 0 {
        return error("file uploads need a space id, a source id, and an audience");
    }
    if config.apiKey == "" && !isLoopback(config.host) {
        return error(string `refusing to serve file uploads on ${config.host} without an API key`);
    }
    http:Listener uploadListener = check new (config.port, {host: config.host});
    check uploadListener.attach(new UploadService(engineClient, destination, config.apiKey), "/");
    check uploadListener.'start();
    runtime:registerListener(uploadListener);
    log:printInfo("file upload endpoint started", host = config.host, port = config.port,
            sourceId = destination.sourceId);
}

// Whether an address only accepts connections from this machine.
isolated function isLoopback(string host) returns boolean {
    return host == "127.0.0.1" || host == "::1" || host == "localhost";
}

# HTTP service that accepts file uploads and delivers each file as a record.
service class UploadService {
    *http:Service;

    private final core:RecordSink sink;
    private final byte[]? keyDigest;

    function init(core:EngineClient engineClient, core:Destination destination, string apiKey) {
        self.sink = new (engineClient, destination);
        // Only the key's digest is kept, and caller keys are compared by digest.
        self.keyDigest = apiKey == "" ? () : crypto:hashSha256(apiKey.toBytes());
    }

    # Deliver the files of a `multipart/form-data` upload.
    #
    # Each file part becomes one record named by its file name. Its version is the time
    # the upload was received, in epoch milliseconds, so a later upload of the same file
    # always orders after an earlier one; the same instant is its observed time. Parts
    # are delivered in order, and the first failure stops the request with the engine's
    # status, listing the files already accepted.
    #
    # + request - the multipart upload
    # + return - the accepted job handles, or an error response
    resource function post files(http:Request request) returns http:Response {
        if !self.authorized(request) {
            return respond(401, {message: "missing or wrong API key"});
        }
        mime:Entity[]|error parts = request.getBodyParts();
        if parts is error {
            return respond(400, {message: "send the files as multipart/form-data"});
        }
        time:Utc received = time:utcNow();
        string version = (received[0] * 1000 + <int>(received[1] * 1000d).floor()).toString();
        string observedAt = time:utcToString(received);
        json[] accepted = [];
        foreach mime:Entity part in parts {
            mime:ContentDisposition disposition = part.getContentDisposition();
            string fileName = disposition.fileName != "" ? disposition.fileName : disposition.name;
            string contentType = part.getContentType();
            if fileName == "" || contentType == "" {
                return respond(400, {
                    message: "every part needs a file name and a content type",
                    accepted
                });
            }
            byte[]|error data = part.getByteArray();
            if data is error {
                return respond(400, {message: string `could not read '${fileName}'`, accepted});
            }
            core:JobAccepted|error job = self.sink->ingest({
                recordId: fileName,
                contentBytes: data,
                contentType,
                sourceVersion: version,
                sourceObservedAt: observedAt
            });
            if job is error {
                log:printError("uploaded file not delivered", 'error = job, recordId = fileName);
                return respond(statusOf(job), {
                    message: string `'${fileName}' was not accepted: ${job.message()}`,
                    accepted
                });
            }
            log:printInfo("delivered uploaded file", recordId = fileName, jobId = job.jobId);
            accepted.push({recordId: fileName, jobId: job.jobId, statusUrl: job.statusUrl});
        }
        return respond(202, {accepted});
    }

    // Whether the request carries the configured key; always true when there is none.
    function authorized(http:Request request) returns boolean {
        byte[]? expected = self.keyDigest;
        if expected is () {
            return true;
        }
        string|http:HeaderNotFoundError header = request.getHeader("Authorization");
        if header is http:HeaderNotFoundError || !header.startsWith("Bearer ") {
            return false;
        }
        return crypto:hashSha256(header.substring(7).toBytes()) == expected;
    }
}

// The HTTP status to answer with when the engine did not accept a file: the engine's
// own status when it answered, or 502 when it could not be reached.
isolated function statusOf(error err) returns int {
    if err is http:ApplicationResponseError {
        return err.detail().statusCode;
    }
    return 502;
}

// A JSON response with a status code.
isolated function respond(int status, json body) returns http:Response {
    http:Response response = new;
    response.statusCode = status;
    response.setJsonPayload(body);
    return response;
}

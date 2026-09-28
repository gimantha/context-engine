// Tests for the upload endpoint's safety rules: where it may listen, what it needs to
// start, and how caller keys are checked.

import ballerina/http;
import ballerina/test;

import context_engine_connectors.core;

final core:EngineClient engine = check new ({baseUrl: "http://127.0.0.1:9599", bearerToken: "t"});

core:Destination uploads = {
    spaceId: "spc_1",
    sourceId: "src_files",
    audience: ["src:uploads"],
    sourceAclVersion: "1"
};

@test:Config {}
function uploadsOffLoopbackNeedAKey() {
    error? refused = serveUploads(engine, {host: "0.0.0.0", port: 9592, destination: uploads});
    test:assertTrue(refused is error, "an open address without a key is refused before binding");
    test:assertTrue(isLoopback("127.0.0.1") && isLoopback("::1") && isLoopback("localhost"));
    test:assertFalse(isLoopback("0.0.0.0"));
}

@test:Config {}
function uploadsNeedADestination() {
    error? missing = serveUploads(engine, {
        port: 9593,
        destination: {spaceId: "spc_1", sourceId: "", audience: ["src:uploads"], sourceAclVersion: "1"}
    });
    test:assertTrue(missing is error, "there is no default source to fall back on");
}

@test:Config {}
function callerKeysAreChecked() {
    UploadService keyed = new (engine, uploads, "upload-key");
    UploadService open = new (engine, uploads, "");
    http:Request good = new;
    good.setHeader("Authorization", "Bearer upload-key");
    http:Request wrong = new;
    wrong.setHeader("Authorization", "Bearer other-key");
    http:Request none = new;
    test:assertTrue(keyed.authorized(good));
    test:assertFalse(keyed.authorized(wrong));
    test:assertFalse(keyed.authorized(none));
    test:assertTrue(open.authorized(none), "without a key, loopback callers are trusted");
}

@test:Config {}
function engineStatusesPassThrough() {
    test:assertEquals(statusOf(error http:ClientRequestError("invalid", statusCode = 415, headers = {},
            body = ())), 415);
    test:assertEquals(statusOf(error("connection refused")), 502);
}

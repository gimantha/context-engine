// Connector runtime host.
//
// A single process that runs any number of configured connector instances. It builds
// one engine client, registers the connector types it enables, loads the instance
// configurations from a `ConfigProvider`, and hands them to the `ConnectorManager`,
// which schedules polls and attaches listeners. It can also serve a file-upload
// endpoint into one configured source.
//
// To enable a new connector type, import its submodule and register its
// `ConnectorType` below. To run more instances, add entries to the configuration source
// (the `CONNECTOR_CONFIGS` environment variable for `EnvConfigProvider`).

import ballerina/lang.runtime;
import ballerina/log;

import context_engine_connectors.core;
import context_engine_connectors.file_source;
import context_engine_connectors.manager;
import context_engine_connectors.salesforce;

# Base URL of the running Context Engine REST API.
configurable string engineBaseUrl = "http://127.0.0.1:8000";

# Bearer token of this host's own engine identity, which must be set. Grant it
# `ingest.write` on each configured source and nothing else: a connector credential
# authorizes delivery only. There is deliberately no default, so an administrator
# token is never used by accident.
configurable string engineToken = ?;

# Environment variable holding the JSON array of connector instance configs. Unset or
# empty means no managed connectors.
configurable string configEnvVar = "CONNECTOR_CONFIGS";

# Whether to serve the file-upload endpoint (`POST /files`). Off unless configured.
configurable boolean fileUploadEnabled = false;

# Address the upload endpoint binds. Loopback by default; any other address requires
# `fileUploadApiKey`.
configurable string fileUploadHost = "127.0.0.1";

# Port the upload endpoint binds.
configurable int fileUploadPort = 9090;

# Space and source that uploaded files are delivered to. The source must belong to the
# space, and `engineToken` needs `ingest.write` on it.
configurable string fileUploadSpaceId = "";
configurable string fileUploadSourceId = "";

# Source audience labels applied to uploaded files, mapped by the source's mapping.
configurable string[] fileUploadAudience = [];

# Source ACL version sent with uploaded files.
configurable string fileUploadSourceAclVersion = "1";

# Key upload callers send as `Authorization: Bearer <key>`. Empty means no key, which is
# allowed only on a loopback address.
configurable string fileUploadApiKey = "";

# Start the connector runtime.
#
# + return - an error if the engine client, configuration, or any connector fails to start
public function main() returns error? {
    core:EngineClient engineClient = check new ({
        baseUrl: engineBaseUrl,
        bearerToken: engineToken
    });

    manager:ConnectorManager connectorManager = new (engineClient);
    // Register every connector type this host enables.
    connectorManager.register(salesforce:salesforceType());

    manager:ConfigProvider provider = new manager:EnvConfigProvider(configEnvVar);
    manager:ConnectorInstanceConfig[] configs = check provider.provide();

    check connectorManager.'start(configs);
    log:printInfo("connector runtime started", instances = configs.length(),
            scheduledPolls = connectorManager.scheduledPollCount());

    if fileUploadEnabled {
        check file_source:serveUploads(engineClient, {
            host: fileUploadHost,
            port: fileUploadPort,
            destination: {
                spaceId: fileUploadSpaceId,
                sourceId: fileUploadSourceId,
                audience: fileUploadAudience,
                sourceAclVersion: fileUploadSourceAclVersion
            },
            apiKey: fileUploadApiKey
        });
    }

    // Registered listeners hold the runtime open; the poll scheduler also keeps its
    // worker thread alive. Park the main strand so the host stays up for a poll-only
    // deployment (no listeners) as well.
    runtime:registerListener(keepAlive);
}

// A no-op dynamic listener that keeps the process alive after main returns.
final Keepalive keepAlive = new;

isolated class Keepalive {
    *runtime:DynamicListener;
    public isolated function 'start() returns error? {
    }
    public isolated function gracefulStop() returns error? {
    }
    public isolated function immediateStop() returns error? {
    }
}

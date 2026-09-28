// Connector manager: a registry of connector types plus a scheduler that runs any
// number of configured instances.
//
// The manager holds a registry of connector types. Given a set of
// `ConnectorInstanceConfig` values it starts each one, running whichever factories
// the type provides: a poll factory is scheduled as a recurring `task:Job`, a listen
// factory has its listener attached. A type may provide both (Salesforce does), so one
// instance can do both. Every instance delivers through its own `core:RecordSink` and
// keeps its resume positions in its own `core:CheckpointStore`.

import ballerina/log;
import ballerina/task;

import context_engine_connectors.core;

# Builds the checkpoint store for one instance's destination.
public type CheckpointStoreFactory isolated function (core:Destination destination)
        returns core:CheckpointStore;

# Runs any number of configured connector instances against one engine.
public class ConnectorManager {
    private final core:EngineClient engineClient;
    private final CheckpointStoreFactory checkpointStoreFactory;
    private final map<core:ConnectorType> registry = {};
    private task:JobId[] jobs = [];

    # Create a manager bound to an engine client.
    #
    # + engineClient - the shared client every instance delivers through
    # + checkpointStoreFactory - builds each instance's checkpoint store; by default the
    #   positions live in the engine checkpoint of the instance's source, so they survive
    #   restarts
    public function init(core:EngineClient engineClient,
            CheckpointStoreFactory? checkpointStoreFactory = ()) {
        self.engineClient = engineClient;
        self.checkpointStoreFactory = checkpointStoreFactory ?:
            isolated function(core:Destination destination) returns core:CheckpointStore =>
                new EngineCheckpointStore(engineClient, destination.sourceId);
    }

    # Register a connector type so instances can reference it by name.
    #
    # + connectorType - the type registration (name + factories)
    public function register(core:ConnectorType connectorType) {
        self.registry[connectorType.name] = connectorType;
    }

    # Start every configured instance, running each factory its type provides.
    #
    # Instances are checked before any starts: instance ids must be unique, and so must
    # sources, because an instance's positions share its source's single checkpoint and
    # two instances would overwrite each other's.
    #
    # + configs - the instances to run
    # + return - an error for duplicate instances or sources, an unknown type, or a
    #   failure to start
    public function 'start(ConnectorInstanceConfig[] configs) returns error? {
        check validateInstances(configs);
        foreach ConnectorInstanceConfig config in configs {
            core:ConnectorType connectorType = check self.lookup(config.connectorType);
            core:PollConnectorFactory? pollFactory = connectorType.pollFactory;
            core:ListenConnectorFactory? listenFactory = connectorType.listenFactory;
            if pollFactory is () && listenFactory is () {
                return error(string `connector type '${connectorType.name}' has no factory`);
            }
            core:RecordSink sink = new (self.engineClient, config.destination);
            core:CheckpointStore checkpoints = self.checkpointStoreFactory(config.destination);
            if pollFactory is core:PollConnectorFactory {
                check self.schedulePoll(connectorType.name, pollFactory, config, sink, checkpoints);
            }
            if listenFactory is core:ListenConnectorFactory {
                check self.attachListener(connectorType.name, listenFactory, config, sink,
                        checkpoints);
            }
        }
    }

    # The number of scheduled poll jobs; useful for host liveness checks.
    #
    # + return - count of active poll jobs
    public function scheduledPollCount() returns int {
        return self.jobs.length();
    }

    private function schedulePoll(string typeName, core:PollConnectorFactory factory,
            ConnectorInstanceConfig config, core:Sink sink, core:CheckpointStore checkpoints)
            returns error? {
        core:PollConnector connector = check factory(config.settings);
        string cursor = check checkpoints.load(POLL_POSITION) ?: "";
        PollJob job = new (connector, sink, config.instanceId, checkpoints, cursor);
        task:JobId id = check task:scheduleJobRecurByFrequency(job, config.pollIntervalSeconds);
        self.jobs.push(id);
        log:printInfo("scheduled poll connector", instance = config.instanceId,
                'type = typeName, intervalSeconds = config.pollIntervalSeconds);
    }

    private function attachListener(string typeName, core:ListenConnectorFactory factory,
            ConnectorInstanceConfig config, core:Sink sink, core:CheckpointStore checkpoints)
            returns error? {
        core:ListenConnector connector = check factory(config.settings);
        check connector.listen(sink, checkpoints);
        log:printInfo("attached listener connector", instance = config.instanceId, 'type = typeName);
    }

    private function lookup(string name) returns core:ConnectorType|error {
        core:ConnectorType? connectorType = self.registry[name];
        if connectorType is () {
            return error(string `unknown connector type '${name}'`);
        }
        return connectorType;
    }
}

// Refuse configurations where two instances share an id or a source.
isolated function validateInstances(ConnectorInstanceConfig[] configs) returns error? {
    map<boolean> instances = {};
    map<string> sources = {};
    foreach ConnectorInstanceConfig config in configs {
        if instances.hasKey(config.instanceId) {
            return error(string `connector instance '${config.instanceId}' is configured twice`);
        }
        instances[config.instanceId] = true;
        string sourceId = config.destination.sourceId;
        string? owner = sources[sourceId];
        if owner is string {
            return error(string `source '${sourceId}' is written by both '${owner}' and ` +
                    string `'${config.instanceId}'; each source needs its own instance`);
        }
        sources[sourceId] = config.instanceId;
    }
}

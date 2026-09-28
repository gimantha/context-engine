// Checkpoint storage for connector instances.
//
// Each instance gets one `core:CheckpointStore` holding its named resume positions:
// the poll cursor under "poll", and a change-event replay id per channel. The default
// store keeps them in the engine's own per-source checkpoint, which the engine stores
// durably, so a restarted host resumes where it stopped. That is what lets a change
// listener catch up on deletes that happened while the host was down.

import context_engine_connectors.core;

# Keeps an instance's positions in memory. Positions are lost on restart, so this is
# only for tests and throwaway runs.
public isolated class InMemoryCheckpointStore {
    *core:CheckpointStore;

    private final map<string> positions = {};

    # Return the stored position, or `()` before the first save.
    #
    # + key - the position's name
    # + return - the stored position or `()`
    public isolated function load(string key) returns string?|error {
        lock {
            return self.positions[key];
        }
    }

    # Store the position in memory.
    #
    # + key - the position's name
    # + value - the position
    # + return - never fails
    public isolated function save(string key, string value) returns error? {
        lock {
            self.positions[key] = value;
        }
    }
}

# Keeps an instance's positions in the engine checkpoint of the instance's source.
#
# The engine holds one cursor per source, so all of an instance's positions are stored
# together as one small JSON object and rewritten on every save. This is why each
# source may be written by only one instance. Positions are read from the engine once
# and cached; the cache and the write happen under one lock, so a poll job and a
# change listener saving at the same time never overwrite each other's position.
public isolated class EngineCheckpointStore {
    *core:CheckpointStore;

    private final core:EngineClient engineClient;
    private final string sourceId;
    private map<string>? positions = ();

    # Create a store for one source's checkpoint.
    #
    # + engineClient - the engine client, whose credential needs `ingest.write` on the source
    # + sourceId - the source whose checkpoint holds the positions
    public isolated function init(core:EngineClient engineClient, string sourceId) {
        self.engineClient = engineClient;
        self.sourceId = sourceId;
    }

    # Return the stored position, reading the engine checkpoint on first use.
    #
    # + key - the position's name
    # + return - the stored position, `()`, or an error if the checkpoint cannot be read
    public isolated function load(string key) returns string?|error {
        lock {
            check self.ensureLoaded();
            map<string>? positions = self.positions;
            return positions is map<string> ? positions[key] : ();
        }
    }

    # Store the position and write every position back to the engine.
    #
    # + key - the position's name
    # + value - the position
    # + return - an error if the engine refuses the write
    public isolated function save(string key, string value) returns error? {
        lock {
            check self.ensureLoaded();
            map<string> positions = self.positions ?: {};
            positions[key] = value;
            self.positions = positions;
            check self.engineClient->putCheckpoint(self.sourceId, positions.toJsonString());
        }
    }

    // Read the engine checkpoint into the cache the first time it is needed.
    private isolated function ensureLoaded() returns error? {
        lock {
            if self.positions is map<string> {
                return;
            }
            string? stored = check self.engineClient->getCheckpoint(self.sourceId);
            map<string> parsed = {};
            if stored is string {
                json value = check stored.fromJsonString();
                parsed = check value.cloneWithType();
            }
            self.positions = parsed;
        }
    }
}

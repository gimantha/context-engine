# Devant Context Engine

Devant Context Engine is a standalone service for ingesting governed source records and returning authorized, source-linked context. Its public contracts use engine-owned concepts such as context spaces, sources, evidence, enrichments, and jobs. Knowledge-provider details stay behind a private Python adapter.

The repository contains the Milestone 0 architecture spike, the Milestone 1 runnable control plane, the Milestone 2 identity and grants slice, and the engine half of Milestone 3: source registration, staged uploads, connector checkpoints, and the authoritative record ledger with version ordering, deletion tombstones, and audience quarantine. The Ballerina file-source connector is developed separately; an end-to-end test plays the connector's role. Live-provider isolation and deletion verification remains an open release gate recorded in [the M0 exit decision](docs/m0/exit-decision.md).

## Repository layout

```text
.
├── contracts/                 # Provider-neutral HTTP, event, and MCP contracts
├── docs/                      # Plans, ADRs, threat model, and milestone evidence
├── engine/                    # REST API, worker, control plane, private adapter, and tests
├── local/                     # Safe example configuration for local verification
├── scripts/                   # Repository policy and boundary checks
├── tests/                     # Cross-component contract, isolation, and recovery tests
└── ui/                        # Reserved for the operator UI
```

## Prerequisites

Install the following on a clean machine:

- Git
- Python 3.12 (the supported range is `>=3.12,<3.13`)
- [uv](https://docs.astral.sh/uv/) for dependency and virtual-environment management

Live-provider verification also requires credentials for the explicitly configured language and embedding models. The default test suite does not require external credentials or network access.

## Set up from scratch

Clone the repository and install the locked development dependencies:

```bash
git clone <repository-url> context-engine
cd context-engine/engine
uv sync --group dev
uv run context-engine-migrate
```

Run the normal local verification suite:

```bash
uv run ruff format --check src tests ../tests
uv run ruff check src tests ../tests
uv run pytest -m "not live_provider"

cd ..
python3 scripts/check_provider_boundary.py
```

The expected result is a clean format and lint check, all non-live tests passing, and `Provider boundary check passed.`

## Run the local control plane

Both processes use the same durable SQLite database and the same static identity file. From `engine/`, create a local profile and a token file, then load the profile into each terminal:

```bash
cp ../local/m3.env.example .env
mkdir -p .context-engine
cp ../local/m2.static-tokens.example.json .context-engine/static-tokens.json
set -a
source .env
set +a
uv run context-engine-migrate
```

Edit `.context-engine/static-tokens.json` and replace every placeholder token with a random value of at least 16 characters. The file is ignored by Git. Each entry maps a pre-shared bearer token to a fixed identity; entries with `bootstrapActions` receive those actions on the root resource the first time the API starts, which is how the first administrator is created. Static authentication is a local development mode and the API refuses to serve it on a non-loopback address.

Start the REST API in the first terminal:

```bash
uv run context-engine-api --host 127.0.0.1 --port 8000
```

Start the worker independently in the second terminal:

```bash
uv run context-engine-worker
```

Verify the API. Health routes are open; every other route needs a bearer token from the static file:

```bash
curl http://127.0.0.1:8000/v1/health/live
curl http://127.0.0.1:8000/v1/health/ready
curl -H "Authorization: Bearer <admin token>" http://127.0.0.1:8000/v1/auth/me
curl -H "Authorization: Bearer <admin token>" \
  -H "Content-Type: application/json" \
  -d '{"name": "Incident response"}' http://127.0.0.1:8000/v1/spaces
```

Grant another identity read access to a space, using the `id` values returned by `/v1/auth/me` and the space creation call:

```bash
curl -X PUT -H "Authorization: Bearer <admin token>" \
  -H "Content-Type: application/json" \
  -d '{"principalId": "<reader principal id>", "actions": ["context.read"]}' \
  http://127.0.0.1:8000/v1/resources/<space id>/grants/reader-read
```

Deliver content the way a connector does. Register a source with an audience mapping, grant the connector identity delivery rights on that source, stage the bytes, then send the ingestion event that references the staged upload:

```bash
curl -H "Authorization: Bearer <admin token>" -H "Content-Type: application/json" \
  -d '{"name": "Runbooks", "type": "file", "audienceMapping": {"src:research": "research"}}' \
  http://127.0.0.1:8000/v1/spaces/<space id>/sources
curl -X PUT -H "Authorization: Bearer <admin token>" -H "Content-Type: application/json" \
  -d '{"principalId": "<connector principal id>", "actions": ["ingest.write"]}' \
  http://127.0.0.1:8000/v1/resources/<source id>/grants/connector
curl -H "Authorization: Bearer <connector token>" -H "Idempotency-Key: upload-000001" \
  -H "Content-Type: text/plain" --data-binary @runbook.txt \
  http://127.0.0.1:8000/v1/sources/<source id>/uploads
```

The upload response carries `uploadId` and `contentHash`. Put them into an ingestion event as `contentRef` and `contentHash`, post it to `/v1/ingestions` with the same `Idempotency-Key` as the body, and watch `/v1/jobs/<job id>` and `/v1/sources/<source id>/records/<record id>` as the worker applies it. Failed deliveries are listed at `/v1/sources/<source id>/jobs?state=failed`.

Interactive API documentation is available at `http://127.0.0.1:8000/docs`. Identity-provider integrations arrive in M6. Provider-backed execution arrives in M4.

## Run the live-provider verification

The live test is opt-in because it calls configured external models and writes local provider stores. Install the private provider extra, copy the example environment, and add your credentials:

```bash
cd engine
uv sync --group dev --extra knowledge-provider
cp ../local/m0.env.example .env
```

Edit `engine/.env` and set `CONTEXT_ENGINE_MODEL_API_KEY` and `CONTEXT_ENGINE_EMBEDDING_API_KEY`. All external configuration uses engine-owned names; the private adapter translates them to the native SDK configuration internally. Keep the model, storage, access-control, and safety settings explicit. The file is ignored by Git; never commit credentials.

Load the profile and run only the live matrix:

```bash
set -a
source .env
set +a
uv run pytest -m live_provider -v
```

The test skips unless `CONTEXT_ENGINE_RUN_LIVE_PROVIDER=true` and `CONTEXT_ENGINE_MODEL_API_KEY` are present. Record verified results in [the spike report](docs/m0/spike-report.md) and update [the exit decision](docs/m0/exit-decision.md) only when the complete isolation, lifecycle, and residue checks pass.

## Development workflow

Run Python commands from `engine/` so uv selects the repository environment and the application package is importable. Add runtime dependencies to `[project.dependencies]`, optional private-provider dependencies to `[project.optional-dependencies]`, and developer tools to `[dependency-groups].dev` in `engine/pyproject.toml`. Regenerate `engine/uv.lock` with `uv lock`; do not edit it by hand.

Before opening a change, run:

```bash
cd engine
uv run ruff format --check src tests ../tests
uv run ruff check src tests ../tests
uv run pytest -m "not live_provider"
cd ..
python3 scripts/check_provider_boundary.py
```

Changes to a public contract should update its examples and contract tests in the same change. Changes to the knowledge-backend port or adapter should also run the shared backend tests and, when external behavior is affected, the opt-in live suite.

## Architecture rules

- Client applications call the versioned REST API in `engine/src/context_engine/api/`. They never call `KnowledgeBackend` or a provider adapter directly.
- REST handlers use `ContextEngineService` for commands and queries. API schemas contain only public engine values.
- The worker owns asynchronous execution. A later provider-backed worker handler may use the provider-neutral knowledge-backend port; the API must not import it.
- Every request builds its principal from the verified bearer credential. No request body or header names the caller's own identity; a principal identifier in a body is only ever the target of a grant.
- There is no authentication mode without a verifier. `CONTEXT_ENGINE_AUTH_MODE` accepts `static` in this milestone; identity-provider modes arrive with the UI. Static mode serves loopback addresses only.
- Effective permissions come from grants on a resource and its ancestors. Grants on the root resource `engine` apply to every context space. A principal that holds no action on a resource receives not-found, never a hint that the resource exists.
- A connector's delivery right is a grant of `ingest.write` on the source. The ingestion body cannot pick a space or source the credential is not bound to, and staged content must match the event's type and hash.
- The engine never fetches a supplied URL. `sourceUrl` is display provenance; bytes arrive through staged uploads delivered by a connector, whatever the source type.
- The record ledger is authoritative. Versions compare under the source's declared ordering, an older event never replaces or resurrects a newer or deleted record, and records with an unmapped audience tag are quarantined.
- Context spaces are public resources. Internal access partitions are resolved by policy and never accepted from or returned to callers.
- Every backend data operation receives an explicit principal and explicit authorized partition scope. Missing or ambiguous scope fails closed.
- Internal provider-backed worker code depends on the port in `engine/src/context_engine/knowledge_backend/`.
- Native provider imports, identifiers, types, operations, errors, and isolation terms stay within `engine/src/context_engine/knowledge_backend/providers/` and its provider-specific tests.
- Public APIs, schemas, events, MCP tools, errors, logs, metrics, and UI copy use engine terminology. In particular, `dataset` is a private provider term and must not become a public concept.
- Provider results are translated to immutable engine types before crossing the adapter boundary.

The full rationale and accepted terminology are in [ADR 0001](docs/decisions/0001-knowledge-backend-boundary.md).

## Documentation

- [Standalone implementation plan](docs/Devant%20Context%20Engine%20Implementation%20Plan.md)
- [Milestone 0 implementation plan](docs/Milestone%200%20Implementation%20Plan.md)
- [Architecture decisions](docs/decisions/)
- [Threat model](docs/m0/threat-model.md)
- [M0 spike report](docs/m0/spike-report.md)
- [M0 exit decision](docs/m0/exit-decision.md)
- [M1 implementation report](docs/m1/implementation-report.md)
- [M2 implementation report](docs/m2/implementation-report.md)
- [M3 implementation report](docs/m3/implementation-report.md)
- [Coding-agent guide](AGENT.md)

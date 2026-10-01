# ADR 0014: Space model configuration and key handling

**Status:** Accepted

**Date:** 2026-09-29

## Context

Until M5 the engine had one language model and one embedding model, set in the environment and used by the provider for extraction. The control plane configures models per context space, with a provider, a model name, and an API key for each, and expects to read the configuration back without keys. Answers in M5 must be generated with the space's own language model (user decision, 2026-09-29), and the same models should drive extraction and embeddings.

Keys are the sensitive part. The engine runs in two deployments: under the Devant control plane, which is meant to hold keys, and standalone, with nothing but the engine and its environment. The control plane has no secret store yet, and its UI sends keys in the clear to the engine's configuration route.

## Decision

- **Models are configured per space** through `PUT` and `GET /v1/spaces/{spaceId}/configuration`, in the shape the control plane sends: an embedding model and a language model, each with provider, model name, and key. `PUT` replaces the whole configuration and needs `space.manage`; `GET` needs `source.manage` or `space.manage` and returns provider, model, and how each key is held, never the key.
- **The space's models drive every model call.** They are resolved just before a backend call and travel with it as a `ModelSelection`; the private adapter translates them to the provider's per-call settings. A space without configuration resolves to `None`, and the backend uses the engine's environment settings, so spaces indexed before configuration existed keep working.
- **The embedding model locks once the space holds indexed content.** Changing, adding, or removing it would make the stored vectors incomparable. The change is refused until M7 adds reindexing. The key may still change.
- **Keys are held by the control plane; the engine stores references.** A key reference such as `cp:<id>` is resolved at call time from the control plane's secret endpoint, `GET <base>/secrets/<id>`, called with the engine's service credential. That endpoint is the contract the control plane implements; `env:<NAME>` references serve local development and the live tests.
- **Literal keys are encrypted at rest; this is the standalone mode, and Devant's transition.** A standalone deployment has no secret store but the engine, so it sends keys and the engine encrypts each with AES-GCM under a master key from `CONTEXT_ENGINE_SECRETS_KEY`, storing only the ciphertext. A Devant deployment uses the same path until the control plane holds keys. Without a master key, literal keys are refused rather than stored in the clear. A configuration read back shows `keyKind` as `encrypted` or `reference`, so an operator can see which mode a space is in.
- **A key exists in the clear only in memory, for one call.** Model settings never appear in string form, logs, or errors; a missing key fails a query as unavailable without naming the reference.
- **Storage placement and sources in the same request are accepted and ignored.** Placement is engine-wide until the topology decision, and sources have their own route. `GET` reports the engine's actual store placement.

## Alternatives

- **Keys encrypted in the engine for good.** Rejected: it makes the engine a second secret store, which the user does not want.
- **Model calls proxied through the control plane.** Possible later; it would keep keys out of the engine entirely but puts the control plane on every indexing and answer call.
- **A space-level answer model only, with extraction on the environment model.** Rejected: the control plane configures both, and mixing sources of truth for models confuses lineage.

## Consequences

- The provider is called with per-call model settings for ingestion and retrieval, and through its context variables for enrichment, which takes none. The pinned-provider signature check covers both.
- Every model-running backend call gains an optional selection; the deterministic backend ignores it.
- A Devant deployment ends its transition when the control plane adds a secret store and the resolution endpoint and sends references. A standalone deployment keeps encrypted keys and needs master-key rotation, planned with M7 operations work.
- Stored queries (slice 2) will record which provider and model answered them.

## Validation

- `engine/tests/test_secrets.py`: the vault, both resolvers against a stand-in control-plane endpoint, and the store.
- `engine/tests/test_space_configuration.py`: keys encrypted at rest and never returned, references, the control-plane payload shape, access rules, the embedding lock, models reaching every backend call, the default for unconfigured spaces, and an unavailable key failing safely.
- `engine/tests/test_cognee_adapter.py`: translation to the provider's settings, including Azure, and the context-variable path for enrichment.
- The live lifecycle test passes explicit models on one ingest and one query.

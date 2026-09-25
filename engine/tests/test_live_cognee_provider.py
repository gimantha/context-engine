"""Opt-in live-provider isolation and record-lifecycle verification.

The engine's service identity owns every isolation unit and grants read access per principal,
which is how the worker uses the provider. Run it with real model credentials; see README.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from context_engine.config import KnowledgeBackendSettings
from context_engine.knowledge_backend import (
    AccessPartitionRef,
    BackendError,
    BackendErrorCode,
    InMemoryBackendState,
    PrincipalContext,
    QueryRequest,
    SourceRecord,
)
from context_engine.knowledge_backend.providers.cognee import (
    CogneeBackend,
    CogneeIdentityResolver,
    CogneeRuntime,
    _parse_reference,
    assert_runtime_matches_pinned_sdk,
)

pytestmark = pytest.mark.live_provider


def _live_enabled() -> bool:
    settings = KnowledgeBackendSettings.from_env()
    return settings.live_test_enabled and bool(settings.model_api_key)


@pytest.mark.skipif(not _live_enabled(), reason="live provider credentials are not configured")
@pytest.mark.asyncio
async def test_live_provider_two_audience_lifecycle(tmp_path):
    settings = replace(KnowledgeBackendSettings.from_env(), storage_path=tmp_path)
    runtime = CogneeRuntime(settings)
    runtime._module()

    from cognee.modules.engine.operations.setup import setup
    from cognee.modules.users.permissions.methods import authorized_give_permission_on_datasets

    assert_runtime_matches_pinned_sdk(runtime)
    await setup()

    state = InMemoryBackendState()
    resolver = CogneeIdentityResolver(runtime, state)
    backend = CogneeBackend(runtime, resolver, state)
    run = uuid4().hex[:8]
    service = PrincipalContext(f"{settings.service_principal_id}-{run}", f"trace-{uuid4()}")
    alpha = PrincipalContext(f"prn_alpha_{run}", f"trace-{uuid4()}")
    beta = PrincipalContext(f"prn_beta_{run}", f"trace-{uuid4()}")
    alpha_partition = AccessPartitionRef(f"prt_alpha_{run}")
    beta_partition = AccessPartitionRef(f"prt_beta_{run}")
    shared_partition = AccessPartitionRef(f"prt_shared_{run}")

    def record(content, record_id=None, version="1"):
        return SourceRecord(
            record_id=record_id or str(uuid4()),
            source_id="m0-live-source",
            version=version,
            content=content,
            content_hash="sha256:" + hashlib.sha256(content.encode()).hexdigest(),
            entities=("Gateway", "Colombo"),
        )

    alpha_canary = f"ALPHA-{uuid4()}"
    beta_canary = f"BETA-{uuid4()}"
    alpha_record = record(f"Gateway in Colombo has recovery canary {alpha_canary}.")
    beta_record = record(f"Gateway in Colombo has recovery canary {beta_canary}.")
    shared_record = record("Gateway incidents require an owner and a rollback checkpoint.")

    # The service identity writes everything, so it owns every isolation unit.
    alpha_result = await backend.ingest(alpha_record, service, alpha_partition)
    beta_result = await backend.ingest(beta_record, service, beta_partition)
    shared_result = await backend.ingest(shared_record, service, shared_partition)

    service_user = await resolver(service)
    alpha_user = await resolver(alpha)
    beta_user = await resolver(beta)

    def native_unit(result):
        return UUID(_parse_reference(result.backend_reference)[0])

    for reader, results in (
        (alpha_user, (alpha_result, shared_result)),
        (beta_user, (beta_result, shared_result)),
    ):
        await authorized_give_permission_on_datasets(
            reader.id, [native_unit(item) for item in results], "read", service_user.id
        )

    alpha_query = await backend.query(
        QueryRequest("Gateway recovery canary", limit=10),
        alpha,
        (alpha_partition, shared_partition),
    )
    beta_query = await backend.query(
        QueryRequest("Gateway recovery canary", limit=10),
        beta,
        (beta_partition, shared_partition),
    )
    assert alpha_canary in repr(alpha_query) and beta_canary not in repr(alpha_query)
    assert beta_canary in repr(beta_query) and alpha_canary not in repr(beta_query)

    # The provider refuses a partition the reader holds no key for, even if the engine asked.
    with pytest.raises(BackendError) as denied:
        await backend.query(QueryRequest(beta_canary), alpha, (beta_partition,))
    assert denied.value.code == BackendErrorCode.ACCESS_DENIED

    replacement_canary = f"ALPHA-NEW-{uuid4()}"
    replacement = record(
        f"Gateway in Colombo has replacement canary {replacement_canary}.",
        record_id=alpha_record.record_id,
        version="2",
    )
    updated = await backend.update(replacement, service, alpha_partition)
    # A replacement is a new native item, so later work must use the reference it returned.
    assert updated.backend_reference != alpha_result.backend_reference
    stale = await backend.query(QueryRequest(alpha_canary), alpha, (alpha_partition,))
    current = await backend.query(QueryRequest(replacement_canary), alpha, (alpha_partition,))
    assert alpha_canary not in repr(stale)
    assert replacement_canary in repr(current)

    assert (await backend.delete(updated.backend_reference, service, alpha_partition)).deleted
    after_delete = await backend.query(QueryRequest(replacement_canary), alpha, (alpha_partition,))
    assert replacement_canary not in repr(after_delete)

    await backend.delete(beta_result.backend_reference, service, beta_partition)
    await backend.delete(shared_result.backend_reference, service, shared_partition)

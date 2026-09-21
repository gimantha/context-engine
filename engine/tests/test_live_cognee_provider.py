"""Opt-in live-provider isolation and record-lifecycle verification."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from context_engine.config import KnowledgeBackendSettings
from context_engine.knowledge_backend import (
    AccessPartitionRef,
    BackendError,
    BackendErrorCode,
    PrincipalContext,
    QueryRequest,
    SourceRecord,
)
from context_engine.knowledge_backend.providers.cognee import (
    CogneeBackend,
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
    from cognee.modules.users.methods import create_user
    from cognee.modules.users.permissions.methods import authorized_give_permission_on_datasets

    assert_runtime_matches_pinned_sdk(runtime)
    await setup()

    users = {}

    async def resolve_user(principal):
        if principal.principal_id not in users:
            users[principal.principal_id] = await create_user(
                principal.principal_id,
                settings.live_test_password or secrets.token_urlsafe(24),
            )
        return users[principal.principal_id]

    backend = CogneeBackend(runtime, resolve_user)
    alpha = PrincipalContext(f"m0-alpha-{uuid4()}@example.invalid", f"trace-{uuid4()}")
    beta = PrincipalContext(f"m0-beta-{uuid4()}@example.invalid", f"trace-{uuid4()}")
    alpha_partition = AccessPartitionRef(f"partition-alpha-{uuid4()}")
    beta_partition = AccessPartitionRef(f"partition-beta-{uuid4()}")
    shared_partition = AccessPartitionRef(f"partition-shared-{uuid4()}")

    def record(label, content, version="1"):
        return SourceRecord(
            record_id=str(uuid4()),
            source_id="m0-live-source",
            version=version,
            content=content,
            content_hash="sha256:" + hashlib.sha256(content.encode()).hexdigest(),
            entities=("Gateway", "Colombo"),
        )

    alpha_canary = f"ALPHA-{uuid4()}"
    beta_canary = f"BETA-{uuid4()}"
    alpha_record = record("alpha", f"Gateway in Colombo has recovery canary {alpha_canary}.")
    beta_record = record("beta", f"Gateway in Colombo has recovery canary {beta_canary}.")
    shared_record = record(
        "shared", "Gateway incidents require an owner and a rollback checkpoint."
    )

    alpha_result = await backend.ingest(alpha_record, alpha, alpha_partition)
    beta_result = await backend.ingest(beta_record, beta, beta_partition)
    shared_result = await backend.ingest(shared_record, alpha, shared_partition)

    shared_native_id, _ = _parse_reference(shared_result.backend_reference)
    await authorized_give_permission_on_datasets(
        users[beta.principal_id].id,
        [UUID(shared_native_id)],
        "read",
        users[alpha.principal_id].id,
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

    with pytest.raises(BackendError) as denied:
        await backend.query(QueryRequest(beta_canary), alpha, (beta_partition,))
    assert denied.value.code == BackendErrorCode.ACCESS_DENIED

    replacement_canary = f"ALPHA-NEW-{uuid4()}"
    replacement_content = f"Gateway in Colombo has replacement canary {replacement_canary}."
    replacement = SourceRecord(
        record_id=alpha_record.record_id,
        source_id=alpha_record.source_id,
        version="2",
        content=replacement_content,
        content_hash="sha256:" + hashlib.sha256(replacement_content.encode()).hexdigest(),
        entities=alpha_record.entities,
    )
    await backend.update(replacement, alpha, alpha_partition)
    stale = await backend.query(QueryRequest(alpha_canary), alpha, (alpha_partition,))
    current = await backend.query(QueryRequest(replacement_canary), alpha, (alpha_partition,))
    assert alpha_canary not in repr(stale)
    assert replacement_canary in repr(current)

    assert (await backend.delete(alpha_result.backend_reference, alpha, alpha_partition)).deleted
    after_delete = await backend.query(QueryRequest(replacement_canary), alpha, (alpha_partition,))
    assert replacement_canary not in repr(after_delete)

    await backend.delete(beta_result.backend_reference, beta, beta_partition)
    await backend.delete(shared_result.backend_reference, alpha, shared_partition)

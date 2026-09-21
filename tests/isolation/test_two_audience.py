"""Golden two-audience isolation and revocation test."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from context_engine.knowledge_backend import (
    AccessPartitionRef,
    DummyKnowledgeBackend,
    PrincipalContext,
    QueryRequest,
    SourceRecord,
)

FIXTURE = Path(__file__).parent / "fixtures" / "two-audience.json"


def load_records():
    payload = json.loads(FIXTURE.read_text())
    return payload, {
        item["audience"]: SourceRecord(
            record_id=item["recordId"],
            source_id=item["sourceId"],
            version=item["version"],
            content=item["content"],
            content_hash="sha256:" + hashlib.sha256(item["content"].encode()).hexdigest(),
            title=item["title"],
            entities=tuple(item["entities"]),
        )
        for item in payload["records"]
    }


@pytest.mark.asyncio
async def test_two_audience_isolation_and_revocation():
    _, records = load_records()
    backend = DummyKnowledgeBackend()
    partitions = {name: AccessPartitionRef(f"partition-{name}") for name in records}
    alpha = PrincipalContext("principal-alpha", "trace-alpha")
    beta = PrincipalContext("principal-beta", "trace-beta")

    await backend.ingest(records["alpha"], alpha, partitions["alpha"])
    await backend.ingest(records["beta"], beta, partitions["beta"])
    await backend.ingest(records["shared"], alpha, partitions["shared"])

    alpha_scope = (partitions["alpha"], partitions["shared"])
    beta_scope = (partitions["beta"], partitions["shared"])
    alpha_result, beta_result = await asyncio.gather(
        backend.query(QueryRequest("Gateway recovery canary"), alpha, alpha_scope),
        backend.query(QueryRequest("Gateway recovery canary"), beta, beta_scope),
    )

    alpha_dump = repr(alpha_result)
    beta_dump = repr(beta_result)
    assert "ALPHA-7443" in alpha_dump and "BETA-9555" not in alpha_dump
    assert "BETA-9555" in beta_dump and "ALPHA-7443" not in beta_dump
    assert all("Gateway" in item.graph_path for item in alpha_result.evidence)
    assert all("Gateway" in item.graph_path for item in beta_result.evidence)

    revoked = await backend.query(QueryRequest("ALPHA-7443"), alpha, (partitions["shared"],))
    assert revoked.insufficient_evidence
    assert "ALPHA-7443" not in repr(revoked)

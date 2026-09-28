"""Opt-in M4 gate: the REST API and the worker drive the live provider end to end.

The API and the worker share one process and one event loop here. The local topology's
embedded graph store takes an exclusive per-process lock, so separate API and worker
processes cannot both open it (ADR 0002 revision). Run with real model credentials; see
README.
"""

from __future__ import annotations

import json
from dataclasses import replace
from itertools import count
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from context_engine.api import create_app
from context_engine.config import KnowledgeBackendSettings, Settings
from context_engine.domain import JobOperation
from context_engine.knowledge_backend.factory import build_knowledge_backend
from context_engine.observability import MetricsRegistry
from context_engine.persistence import (
    AuthorizationRepository,
    ControlDatabase,
    ControlPlaneRepository,
    ReadAccessRepository,
    SourceRepository,
    SqliteBackendState,
    StagingStore,
)
from context_engine.security.authorization import Authorizer
from context_engine.security.identity import build_token_verifier
from context_engine.worker.enrichment import EnrichmentJobHandler
from context_engine.worker.handler import LifecycleJobHandler, OperationDispatcher
from context_engine.worker.indexer import RecordIndexer
from context_engine.worker.indexing import IndexingCollector
from context_engine.worker.read_access import ReadAccessSynchronizer
from context_engine.worker.reauthorize import JobAuthorizer
from context_engine.worker.runtime import JobWorker

pytestmark = pytest.mark.live_provider

ROOT = Path(__file__).resolve().parents[2]
TOKENS = {
    "admin": "live-admin-token-0123456789abcdef",
    "connector": "live-connector-token-0123456789ab",
    "alpha": "live-alpha-reader-token-0123456789",
    "beta": "live-beta-reader-token-01234567890",
}


def _live_enabled() -> bool:
    settings = KnowledgeBackendSettings.from_env()
    return settings.live_test_enabled and bool(settings.model_api_key)


def _write_tokens(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "version": "1",
                "tokens": [
                    {
                        "token": TOKENS["admin"],
                        "issuer": "static://live",
                        "subject": "admin",
                        "bootstrapActions": ["access.manage", "space.manage"],
                    },
                    {
                        "token": TOKENS["connector"],
                        "issuer": "static://live",
                        "subject": "connector",
                        "kind": "service",
                    },
                    {
                        "token": TOKENS["alpha"],
                        "issuer": "static://live",
                        "subject": "alpha",
                        "groups": ["research"],
                    },
                    {
                        "token": TOKENS["beta"],
                        "issuer": "static://live",
                        "subject": "beta",
                        "groups": ["finance"],
                    },
                ],
            }
        )
    )


def _auth(name: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKENS[name]}", **extra}


class _Engine:
    """The API and the worker's components over one live backend."""

    def __init__(self, tmp_path: Path, storage: Path) -> None:
        _write_tokens(tmp_path / "tokens.json")
        self.settings = Settings(
            database_path=tmp_path / "control.db",
            migrations_path=ROOT / "engine/migrations",
            static_tokens_path=tmp_path / "tokens.json",
            staging_path=tmp_path / "staging",
            knowledge_backend="provider",
        )
        database = ControlDatabase(self.settings.database_path, self.settings.migrations_path)
        database.migrate()
        backend_settings = replace(KnowledgeBackendSettings.from_env(), storage_path=storage)
        backend = build_knowledge_backend(backend_settings, SqliteBackendState(database))
        sources = SourceRepository(database)
        authorization = AuthorizationRepository(database)
        metrics = MetricsRegistry()
        staging = StagingStore(self.settings.staging_path)
        verifier = build_token_verifier(self.settings)
        service = backend_settings.service_principal_id
        self.read_access = ReadAccessSynchronizer(
            authorization,
            sources,
            ReadAccessRepository(database),
            backend,
            verifier,
            service,
            metrics,
        )
        indexer = RecordIndexer(
            sources,
            backend,
            staging,
            service,
            metrics,
            on_partition_bound=self.read_access.sync_partition,
        )
        self.collector = IndexingCollector(sources, backend, service, metrics)
        lifecycle = LifecycleJobHandler(sources, indexer=indexer, staging=staging)
        self.worker = JobWorker(
            ControlPlaneRepository(database),
            OperationDispatcher(
                {
                    JobOperation.INGESTION: lifecycle,
                    JobOperation.UPDATE: lifecycle,
                    JobOperation.DELETION: lifecycle,
                    JobOperation.ENRICHMENT: EnrichmentJobHandler(
                        sources, backend, service, metrics
                    ),
                }
            ),
            metrics,
            JobAuthorizer(Authorizer(authorization, metrics), authorization, verifier),
            lease_seconds=600,
        )
        self.app = create_app(self.settings, knowledge_backend=backend)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://engine.local", timeout=600
        )
        self.sequence = count(1)

    async def drain(self) -> None:
        await self.read_access.sync_if_changed(f"trace_{uuid4().hex}")
        while await self.worker.run_once():
            await self.read_access.sync_if_changed(f"trace_{uuid4().hex}")

    async def collect(self) -> None:
        await self.collector.collect(f"trace_{uuid4().hex}")

    async def setup(self) -> tuple[dict, dict]:
        client = self.client
        ids = {
            name: (await client.get("/v1/auth/me", headers=_auth(name))).json()["id"]
            for name in TOKENS
        }
        space = (
            await client.post("/v1/spaces", headers=_auth("admin"), json={"name": "Live path"})
        ).json()
        source = (
            await client.post(
                f"/v1/spaces/{space['id']}/sources",
                headers=_auth("admin"),
                json={
                    "name": "Runbooks",
                    "type": "file",
                    "audienceMapping": {"src:research": "research", "src:finance": "finance"},
                },
            )
        ).json()
        grants = (
            (source["id"], "connector", ids["connector"], "ingest.write"),
            (space["id"], "alpha", ids["alpha"], "context.read"),
            (space["id"], "beta", ids["beta"], "context.read"),
            (space["id"], "enricher", ids["admin"], "context.enrich"),
        )
        for resource, name, principal, action in grants:
            response = await client.put(
                f"/v1/resources/{resource}/grants/{name}",
                headers=_auth("admin"),
                json={"principalId": principal, "actions": [action]},
            )
            assert response.status_code in (200, 201), response.text
        return space, source

    async def deliver(
        self,
        space: dict,
        source: dict,
        record_id: str,
        version: str,
        operation: str = "upsert",
        *,
        content: bytes = b"",
        audience: tuple[str, ...] = ("src:research",),
    ) -> None:
        number = next(self.sequence)
        key = f"evt-{number:06d}"
        body = {
            "schemaVersion": "1",
            "spaceId": space["id"],
            "sourceId": source["id"],
            "sourceRecordId": record_id,
            "sourceVersion": version,
            "operation": operation,
            "sourceObservedAt": "2026-09-25T10:00:00Z",
            "audience": list(audience),
            "sourceAclVersion": version,
            "idempotencyKey": key,
        }
        if operation == "upsert":
            upload = await self.client.post(
                f"/v1/sources/{source['id']}/uploads",
                headers=_auth(
                    "connector",
                    **{"Idempotency-Key": f"up-{number:06d}", "Content-Type": "text/plain"},
                ),
                content=content,
            )
            assert upload.status_code == 201, upload.text
            staged = upload.json()
            body.update(
                contentType="text/plain",
                contentRef=staged["uploadId"],
                contentHash=staged["contentHash"],
                sourceUrl=f"https://files.invalid/{record_id}",
            )
        response = await self.client.post(
            "/v1/ingestions", headers=_auth("connector", **{"Idempotency-Key": key}), json=body
        )
        assert response.status_code == 202, response.text

    async def status(self, source: dict, record_id: str) -> dict:
        response = await self.client.get(
            f"/v1/sources/{source['id']}/records/{record_id}", headers=_auth("connector")
        )
        return response.json()

    async def ask(self, reader: str, space: dict, question: str) -> tuple[int, str]:
        response = await self.client.post(
            "/v1/queries",
            headers=_auth(reader),
            json={"spaceId": space["id"], "question": question, "mode": "context"},
        )
        return response.status_code, response.text


@pytest.mark.skipif(not _live_enabled(), reason="live provider credentials are not configured")
async def test_live_provider_path_through_the_api_and_worker(tmp_path, tmp_path_factory):
    # The provider binds one storage root per process, so live tests share one per session.
    engine = _Engine(tmp_path, tmp_path_factory.getbasetemp() / "live-provider-store")
    async with engine.app.router.lifespan_context(engine.app):
        space, source = await engine.setup()
        alpha, beta = f"ALPHA-{uuid4()}", f"BETA-{uuid4()}"
        await engine.deliver(
            space, source, "runbook-a", "1", content=f"Gateway canary {alpha}.".encode()
        )
        await engine.deliver(
            space,
            source,
            "runbook-b",
            "1",
            content=f"Gateway canary {beta}.".encode(),
            audience=("src:finance",),
        )
        await engine.drain()
        await engine.collect()
        indexed = [await engine.status(source, name) for name in ("runbook-a", "runbook-b")]
        alpha_code, alpha_view = await engine.ask("alpha", space, "Gateway canary")
        _, beta_view = await engine.ask("beta", space, "Gateway canary")

        replacement = f"ALPHA-NEW-{uuid4()}"
        await engine.deliver(
            space, source, "runbook-a", "2", content=f"Gateway canary {replacement}.".encode()
        )
        await engine.drain()
        _, current = await engine.ask("alpha", space, "Gateway canary")

        moved = f"BETA-MOVED-{uuid4()}"
        await engine.deliver(
            space, source, "runbook-b", "2", content=f"Gateway canary {moved}.".encode()
        )
        await engine.drain()
        _, alpha_after_move = await engine.ask("alpha", space, "Gateway canary")
        _, beta_after_move = await engine.ask("beta", space, "Gateway canary")

        await engine.deliver(space, source, "runbook-a", "3", operation="delete")
        await engine.drain()
        deleted = await engine.status(source, "runbook-a")
        _, after_delete = await engine.ask("alpha", space, "Gateway canary")

        enrichment = await engine.client.post(
            f"/v1/spaces/{space['id']}/enrichments",
            headers=_auth("admin", **{"Idempotency-Key": "enrich-000001"}),
        )
        await engine.drain()
        enriched = (
            await engine.client.get(enrichment.json()["statusUrl"], headers=_auth("admin"))
        ).json()
        _, alpha_after_enrichment = await engine.ask("alpha", space, "Gateway canary")
        _, beta_after_enrichment = await engine.ask("beta", space, "Gateway canary")

        await engine.collect()
        progress = (
            await engine.client.get(
                f"/v1/progress/sources/{source['id']}", headers=_auth("connector")
            )
        ).json()
        moved_status = await engine.status(source, "runbook-b")
        failed = (
            await engine.client.get(
                f"/v1/sources/{source['id']}/jobs",
                params={"state": "failed"},
                headers=_auth("admin"),
            )
        ).json()

    # Indexing converges, and the scheduled cross-check agrees with the ledger.
    assert [item["indexState"] for item in indexed] == ["indexed", "indexed"]
    # Each reader sees its own audience only, with engine lineage and no provider terms.
    assert alpha_code == 200 and alpha in alpha_view and beta not in alpha_view
    assert beta in beta_view and alpha not in beta_view
    evidence = json.loads(alpha_view)["evidence"][0]
    assert (evidence["recordId"], evidence["sourceVersion"]) == ("runbook-a", "1")
    assert evidence["sourceUrl"] == "https://files.invalid/runbook-a"
    assert "prt_" not in alpha_view and "dataset" not in alpha_view.lower()
    # A replacement supersedes the old version.
    assert replacement in current and alpha not in current
    assert json.loads(current)["evidence"][0]["sourceVersion"] == "2"
    # An audience change moves the record between readers.
    assert moved in alpha_after_move
    assert moved not in beta_after_move and beta not in beta_after_move
    # A deletion converges and removes the record from every reader.
    assert deleted["state"] == "deleted" and deleted["indexState"] == "not_indexed"
    assert replacement not in after_delete
    # Enrichment runs per partition and does not cross audiences.
    assert enrichment.status_code == 202 and enriched["state"] == "succeeded"
    assert moved in alpha_after_enrichment and replacement not in alpha_after_enrichment
    assert moved not in beta_after_enrichment
    # Progress reports collected indexing, and nothing failed along the way.
    assert progress["indexing"]["state"] != "not_collected"
    assert moved_status["indexState"] == "indexed"
    assert failed == []

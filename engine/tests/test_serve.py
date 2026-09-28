"""The single-process server runs the worker loop alongside the REST API."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

from context_engine.config import Settings
from context_engine.serve import create_serve_app
from context_engine.serve import main as serve_main

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
ADMIN = "serve-admin-token-0123456789abcdef"
CONNECTOR = "serve-connector-token-0123456789a"


def _settings(tmp_path: Path) -> Settings:
    tokens = tmp_path / "tokens.json"
    tokens.write_text(
        json.dumps(
            {
                "version": "1",
                "tokens": [
                    {
                        "token": ADMIN,
                        "issuer": "static://serve",
                        "subject": "admin",
                        "bootstrapActions": ["access.manage", "space.manage"],
                    },
                    {
                        "token": CONNECTOR,
                        "issuer": "static://serve",
                        "subject": "connector",
                        "kind": "service",
                    },
                ],
            }
        )
    )
    return Settings(
        database_path=tmp_path / "control.db",
        migrations_path=MIGRATIONS,
        static_tokens_path=tokens,
        staging_path=tmp_path / "staging",
        worker_poll_seconds=0.05,
    )


def _auth(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


async def _eventually(check, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not reached in time")
        await asyncio.sleep(0.05)


async def test_background_worker_processes_deliveries_without_a_worker_process(tmp_path):
    app = create_serve_app(_settings(tmp_path))
    background = app.state.background_worker
    async with app.router.lifespan_context(app):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://engine.local"
        )
        space = (await client.post("/v1/spaces", headers=_auth(ADMIN), json={"name": "Ops"})).json()
        source = (
            await client.post(
                f"/v1/spaces/{space['id']}/sources",
                headers=_auth(ADMIN),
                json={
                    "name": "Runbooks",
                    "type": "file",
                    "audienceMapping": {"src:research": "research"},
                },
            )
        ).json()
        connector_id = (await client.get("/v1/auth/me", headers=_auth(CONNECTOR))).json()["id"]
        await client.put(
            f"/v1/resources/{source['id']}/grants/connector",
            headers=_auth(ADMIN),
            json={"principalId": connector_id, "actions": ["ingest.write"]},
        )
        upload = (
            await client.post(
                f"/v1/sources/{source['id']}/uploads",
                headers=_auth(
                    CONNECTOR, **{"Idempotency-Key": "upload-000001", "Content-Type": "text/plain"}
                ),
                content=b"Confirm the rollback checkpoint.",
            )
        ).json()
        accepted = await client.post(
            "/v1/ingestions",
            headers=_auth(CONNECTOR, **{"Idempotency-Key": "event-000001"}),
            json={
                "schemaVersion": "1",
                "spaceId": space["id"],
                "sourceId": source["id"],
                "sourceRecordId": "runbook-1",
                "sourceVersion": "1",
                "operation": "upsert",
                "sourceObservedAt": "2026-09-25T10:00:00Z",
                "audience": ["src:research"],
                "sourceAclVersion": "1",
                "idempotencyKey": "event-000001",
                "contentType": "text/plain",
                "contentRef": upload["uploadId"],
                "contentHash": upload["contentHash"],
            },
        )
        status_url = accepted.json()["statusUrl"]

        async def succeeded() -> bool:
            job = (await client.get(status_url, headers=_auth(CONNECTOR))).json()
            return job["state"] == "succeeded"

        # Nothing drains the queue here: the loop running inside the server does.
        await _eventually(succeeded)
        record = (
            await client.get(
                f"/v1/sources/{source['id']}/records/runbook-1", headers=_auth(CONNECTOR)
            )
        ).json()
        ready = await client.get("/v1/health/ready")

    assert accepted.status_code == 202
    assert record["state"] == "active" and record["currentVersion"] == "1"
    assert ready.status_code == 200
    # Shutdown stops the loop.
    assert not background.healthy()


async def test_readiness_follows_the_worker_loop(tmp_path, monkeypatch):
    app = create_serve_app(_settings(tmp_path))
    worker = app.state.background_worker.runtime.worker
    working = worker.run_once
    async with app.router.lifespan_context(app):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://engine.local"
        )

        async def status() -> int:
            return (await client.get("/v1/health/ready")).status_code

        async def ready() -> bool:
            return await status() == 200

        async def unavailable() -> bool:
            return await status() == 503

        await _eventually(ready)

        async def broken() -> bool:
            raise RuntimeError("control database unavailable")

        monkeypatch.setattr(worker, "run_once", broken)
        await _eventually(unavailable)
        # The loop restarts on its own and readiness returns once a pass completes.
        monkeypatch.setattr(worker, "run_once", working)
        await _eventually(ready)


def test_serve_startup_check_in_both_backend_modes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CONTEXT_ENGINE_DB_PATH", str(tmp_path / "control.db"))
    monkeypatch.setenv("CONTEXT_ENGINE_MIGRATIONS_PATH", str(MIGRATIONS))
    monkeypatch.setattr(sys, "argv", ["context-engine-serve", "--check"])
    serve_main()
    assert "API and worker startup check passed" in capsys.readouterr().out

    monkeypatch.setenv("CONTEXT_ENGINE_KNOWLEDGE_BACKEND", "provider")
    serve_main()
    assert "API and worker startup check passed" in capsys.readouterr().out

    monkeypatch.setenv("CONTEXT_ENGINE_KNOWLEDGE_BACKEND", "everything")
    with pytest.raises(RuntimeError):
        serve_main()


def test_serve_refuses_static_tokens_off_loopback(monkeypatch):
    monkeypatch.setenv("CONTEXT_ENGINE_AUTH_MODE", "static")
    monkeypatch.setattr(sys, "argv", ["context-engine-serve", "--host", "0.0.0.0"])
    with pytest.raises(SystemExit):
        serve_main()

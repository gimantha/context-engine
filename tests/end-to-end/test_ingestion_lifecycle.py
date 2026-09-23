"""M3 gate: a reference connector drives the REST API and the worker applies the ledger rules."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from context_engine.api import create_app
from context_engine.config import Settings
from context_engine.observability import MetricsRegistry
from context_engine.persistence import (
    AuthorizationRepository,
    ControlDatabase,
    ControlPlaneRepository,
    SourceRepository,
)
from context_engine.security.authorization import Authorizer
from context_engine.security.identity import StaticTokenVerifier
from context_engine.worker import JobAuthorizer, JobWorker, LifecycleJobHandler

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "engine/migrations"
ADMIN = "e2e-admin-token-0123456789abcdef"
CONNECTOR = "e2e-connector-token-0123456789ab"


class Connector:
    """Minimal stand-in for a source connector: stage bytes, deliver events, keep a cursor."""

    def __init__(self, client: TestClient, token: str, space_id: str, source_id: str) -> None:
        self.client = client
        self.token = token
        self.space_id = space_id
        self.source_id = source_id
        self.sequence = 0

    def _headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", **extra}

    def stage(self, data: bytes) -> dict:
        self.sequence += 1
        response = self.client.post(
            f"/v1/sources/{self.source_id}/uploads",
            headers=self._headers(
                **{"Idempotency-Key": f"upload-{self.sequence:06d}", "Content-Type": "text/plain"}
            ),
            content=data,
        )
        assert response.status_code == 201, response.text
        return response.json()

    def deliver(
        self,
        record_id: str,
        version: str,
        operation: str,
        *,
        content: bytes | None = None,
        audience: list[str] | None = None,
        acl_version: str = "1",
        key: str | None = None,
    ) -> dict:
        self.sequence += 1
        key = key or f"{self.source_id}:{record_id}:{version}:{operation}:{self.sequence}"
        body = {
            "schemaVersion": "1",
            "spaceId": self.space_id,
            "sourceId": self.source_id,
            "sourceRecordId": record_id,
            "sourceVersion": version,
            "operation": operation,
            "sourceObservedAt": "2026-09-23T10:00:00Z",
            "audience": audience if audience is not None else ["src:research"],
            "sourceAclVersion": acl_version,
            "idempotencyKey": key,
        }
        if operation == "upsert":
            assert content is not None
            upload = self.stage(content)
            body.update(
                contentType="text/plain",
                contentRef=upload["uploadId"],
                contentHash=upload["contentHash"],
                sourceUrl=f"https://files.invalid/{record_id}",
            )
        response = self.client.post(
            "/v1/ingestions", headers=self._headers(**{"Idempotency-Key": key}), json=body
        )
        assert response.status_code == 202, response.text
        self.client.put(
            f"/v1/sources/{self.source_id}/checkpoints",
            headers=self._headers(),
            json={"cursor": f"seq-{self.sequence}"},
        )
        return response.json()

    def status(self, record_id: str) -> dict:
        response = self.client.get(
            f"/v1/sources/{self.source_id}/records/{record_id}", headers=self._headers()
        )
        assert response.status_code == 200, response.text
        return response.json()


def _tokens(tmp_path: Path) -> Path:
    path = tmp_path / "tokens.json"
    path.write_text(
        json.dumps(
            {
                "version": "1",
                "tokens": [
                    {
                        "token": ADMIN,
                        "issuer": "static://e2e",
                        "subject": "admin",
                        "bootstrapActions": ["access.manage", "space.manage"],
                    },
                    {
                        "token": CONNECTOR,
                        "issuer": "static://e2e",
                        "subject": "file-connector",
                        "kind": "service",
                    },
                ],
            }
        )
    )
    return path


async def _drain(tmp_path: Path) -> int:
    database = ControlDatabase(tmp_path / "control.db", MIGRATIONS)
    repository = ControlPlaneRepository(database)
    authorization = AuthorizationRepository(database)
    metrics = MetricsRegistry()
    worker = JobWorker(
        repository,
        LifecycleJobHandler(SourceRepository(database)),
        metrics,
        JobAuthorizer(
            Authorizer(authorization, metrics),
            authorization,
            StaticTokenVerifier.from_file(tmp_path / "tokens.json"),
        ),
        lease_seconds=5,
    )
    processed = 0
    while await worker.run_once():
        processed += 1
    return processed


def _history(
    tmp_path: Path, space_id: str, source_id: str, record_id: str
) -> list[tuple[str, str, str]]:
    sources = SourceRepository(ControlDatabase(tmp_path / "control.db", MIGRATIONS))
    return [
        (row["source_version"], row["operation"], row["outcome"])
        for row in sources.list_record_versions(space_id, source_id, record_id)
    ]


async def test_sync_converges_and_never_resurrects_deleted_content(tmp_path):
    settings = Settings(
        database_path=tmp_path / "control.db",
        migrations_path=MIGRATIONS,
        static_tokens_path=_tokens(tmp_path),
        staging_path=tmp_path / "staging",
    )
    with TestClient(create_app(settings)) as client:
        admin = {"Authorization": f"Bearer {ADMIN}"}
        space = client.post("/v1/spaces", headers=admin, json={"name": "Incident response"}).json()
        source = client.post(
            f"/v1/spaces/{space['id']}/sources",
            headers=admin,
            json={
                "name": "Runbooks",
                "type": "file",
                "audienceMapping": {"src:research": "research"},
            },
        ).json()
        connector_id = client.get(
            "/v1/auth/me", headers={"Authorization": f"Bearer {CONNECTOR}"}
        ).json()["id"]
        client.put(
            f"/v1/resources/{source['id']}/grants/connector",
            headers=admin,
            json={"principalId": connector_id, "actions": ["ingest.write"]},
        )
        connector = Connector(client, CONNECTOR, space["id"], source["id"])

        # Initial scan delivers v1; a re-sync delivers the same event again under a new key.
        connector.deliver("runbook", "1", "upsert", content=b"v1 body")
        connector.deliver("runbook", "1", "upsert", content=b"v1 body")
        assert await _drain(tmp_path) == 2
        after_initial = connector.status("runbook")

        # Newer version, then an out-of-order older version, then a duplicate of the older one.
        connector.deliver("runbook", "3", "upsert", content=b"v3 body")
        connector.deliver("runbook", "2", "upsert", content=b"v2 body")
        connector.deliver("runbook", "2", "upsert", content=b"v2 body")
        assert await _drain(tmp_path) == 3
        after_updates = connector.status("runbook")

        # Delete at v4, then late upserts at v2 and v4 must not bring the record back.
        connector.deliver("runbook", "4", "delete")
        connector.deliver("runbook", "2", "upsert", content=b"v2 body")
        connector.deliver("runbook", "4", "upsert", content=b"resurrected?")
        assert await _drain(tmp_path) == 3
        after_delete = connector.status("runbook")

        # A genuinely newer version after the delete is a legitimate re-creation.
        connector.deliver("runbook", "5", "upsert", content=b"v5 body")
        assert await _drain(tmp_path) == 1
        after_recreate = connector.status("runbook")

        # Same version with different content is a conflict that fails the job.
        connector.deliver("runbook", "5", "upsert", content=b"tampered v5")
        assert await _drain(tmp_path) == 1
        failed = client.get(
            f"/v1/sources/{source['id']}/jobs", params={"state": "failed"}, headers=admin
        ).json()
        checkpoint = client.get(f"/v1/sources/{source['id']}/checkpoints", headers=admin).json()

    assert after_initial["state"] == "active" and after_initial["currentVersion"] == "1"
    assert after_updates["state"] == "active" and after_updates["currentVersion"] == "3"
    assert after_updates["contentHash"] == "sha256:" + hashlib.sha256(b"v3 body").hexdigest()
    assert after_delete["state"] == "deleted" and after_delete["currentVersion"] == "4"
    assert after_recreate["state"] == "active" and after_recreate["currentVersion"] == "5"
    assert [job["error"]["code"] for job in failed] == ["effect_conflict"]
    assert checkpoint["cursor"].startswith("seq-")
    assert _history(tmp_path, space["id"], source["id"], "runbook") == [
        ("1", "upsert", "applied"),
        ("1", "upsert", "replayed"),
        ("3", "upsert", "applied"),
        ("2", "upsert", "ignored_older"),
        ("2", "upsert", "ignored_older"),
        ("4", "delete", "applied"),
        ("2", "upsert", "ignored_older"),
        ("4", "upsert", "ignored_older"),
        ("5", "upsert", "applied"),
    ]


async def test_unmapped_audience_quarantines_until_a_mapped_acl_change(tmp_path):
    settings = Settings(
        database_path=tmp_path / "control.db",
        migrations_path=MIGRATIONS,
        static_tokens_path=_tokens(tmp_path),
        staging_path=tmp_path / "staging",
    )
    with TestClient(create_app(settings)) as client:
        admin = {"Authorization": f"Bearer {ADMIN}"}
        space = client.post("/v1/spaces", headers=admin, json={"name": "Ops"}).json()
        source = client.post(
            f"/v1/spaces/{space['id']}/sources",
            headers=admin,
            json={
                "name": "Runbooks",
                "type": "file",
                "audienceMapping": {"src:research": "research"},
            },
        ).json()
        connector_id = client.get(
            "/v1/auth/me", headers={"Authorization": f"Bearer {CONNECTOR}"}
        ).json()["id"]
        client.put(
            f"/v1/resources/{source['id']}/grants/connector",
            headers=admin,
            json={"principalId": connector_id, "actions": ["ingest.write"]},
        )
        connector = Connector(client, CONNECTOR, space["id"], source["id"])

        connector.deliver("secret", "1", "upsert", content=b"body", audience=["src:unknown-team"])
        assert await _drain(tmp_path) == 1
        quarantined = connector.status("secret")

        # An older ACL version cannot lift the quarantine; a newer mapped one does.
        connector.deliver("secret", "1", "acl_changed", audience=["src:research"], acl_version="1")
        connector.deliver("secret", "1", "acl_changed", audience=["src:research"], acl_version="2")
        assert await _drain(tmp_path) == 2
        released = connector.status("secret")

        # A later ACL change that introduces an unmapped tag quarantines again.
        connector.deliver(
            "secret", "1", "acl_changed", audience=["src:research", "src:ext"], acl_version="3"
        )
        assert await _drain(tmp_path) == 1
        again = connector.status("secret")

    assert quarantined["state"] == "quarantined"
    assert quarantined["quarantineReason"] == "unmapped_audience"
    assert released["state"] == "active" and released["sourceAclVersion"] == "2"
    assert "quarantineReason" not in released
    assert again["state"] == "quarantined" and again["sourceAclVersion"] == "3"
    assert _history(tmp_path, space["id"], source["id"], "secret") == [
        ("1", "upsert", "quarantined"),
        ("1", "acl_changed", "ignored_older"),
        ("1", "acl_changed", "applied"),
        ("1", "acl_changed", "quarantined"),
    ]

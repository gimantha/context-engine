"""REST boundary, authentication, authorization, and public-schema tests."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from context_engine.api import create_app
from context_engine.config import Settings
from context_engine.persistence import AuthorizationRepository, ControlDatabase

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "engine/migrations"

ADMIN = "admin-token-0123456789abcdef0123"
READER = "reader-token-0123456789abcdef012"
SERVICE = "service-token-0123456789abcdef01"
MEMBER = "member-token-0123456789abcdef012"


def _tokens(tmp_path: Path, member_groups: tuple[str, ...] = ("readers",)) -> Path:
    entries = [
        {
            "token": ADMIN,
            "issuer": "static://test",
            "subject": "admin",
            "kind": "user",
            "email": "admin@example.invalid",
            "bootstrapActions": ["access.manage", "space.manage"],
        },
        {
            "token": READER,
            "issuer": "static://test",
            "subject": "reader",
            "kind": "user",
            "email": "reader@example.invalid",
        },
        {"token": SERVICE, "issuer": "static://test", "subject": "connector", "kind": "service"},
        {
            "token": MEMBER,
            "issuer": "static://test",
            "subject": "member",
            "kind": "user",
            "groups": list(member_groups),
        },
    ]
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"version": "1", "tokens": entries}))
    return path


def _settings(tmp_path: Path, tokens: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "control.db",
        migrations_path=MIGRATIONS,
        static_tokens_path=tokens,
    )


def _client(tmp_path: Path, member_groups: tuple[str, ...] = ("readers",)) -> TestClient:
    return TestClient(create_app(_settings(tmp_path, _tokens(tmp_path, member_groups))))


def _auth(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


def _ingestion(space_id: str, source_id: str | None = None, upload: dict | None = None) -> dict:
    value = json.loads((ROOT / "contracts/examples/ingestion-upsert.json").read_text())
    value["spaceId"] = space_id
    if source_id:
        value["sourceId"] = source_id
    if upload:
        value["contentRef"] = upload["uploadId"]
        value["contentHash"] = upload["contentHash"]
        value["contentType"] = upload["contentType"]
    return value


def _register_source(client: TestClient, space_id: str, mapping: dict | None = None) -> dict:
    response = client.post(
        f"/v1/spaces/{space_id}/sources",
        headers=_auth(ADMIN),
        json={
            "name": "Runbooks",
            "type": "file",
            "audienceMapping": mapping
            if mapping is not None
            else {"source-group:research": "research"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _stage(client: TestClient, token: str, source_id: str, data: bytes, key: str) -> dict:
    response = client.post(
        f"/v1/sources/{source_id}/uploads",
        headers=_auth(token, **{"Idempotency-Key": key, "Content-Type": "text/plain"}),
        content=data,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _principal_id(client: TestClient, token: str) -> str:
    return client.get("/v1/auth/me", headers=_auth(token)).json()["id"]


def _grant(client, resource: str, grant_id: str, body: dict, token: str = ADMIN):
    return client.put(
        f"/v1/resources/{resource}/grants/{grant_id}", headers=_auth(token), json=body
    )


def test_requests_without_verified_credentials_are_rejected(tmp_path):
    with _client(tmp_path) as client:
        assert client.get("/v1/health/live").status_code == 200
        assert client.get("/v1/health/ready").status_code == 200
        missing = client.get("/v1/spaces", headers={"X-Trace-Id": "trace-401"})
        wrong_scheme = client.get("/v1/spaces", headers={"Authorization": f"Basic {ADMIN}"})
        unknown = client.get("/v1/spaces", headers=_auth("unknown-token-0123456789abcdef"))
        me = client.get("/v1/auth/me", headers=_auth(ADMIN))

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert missing.json() == {
        "code": "unauthenticated",
        "message": "Authentication is required",
        "traceId": "trace-401",
    }
    assert wrong_scheme.status_code == 401
    assert unknown.status_code == 401
    assert me.status_code == 200
    assert me.json()["kind"] == "user"
    assert me.json()["id"].startswith("prn_")
    assert me.json()["email"] == "admin@example.invalid"


def test_grant_read_and_revoke_closes_access(tmp_path):
    with _client(tmp_path) as client:
        created = client.post(
            "/v1/spaces", headers=_auth(ADMIN), json={"name": "Incident response"}
        )
        assert created.status_code == 201
        space = created.json()
        reader_id = _principal_id(client, READER)

        before = client.get(f"/v1/spaces/{space['id']}", headers=_auth(READER))
        listed_before = client.get("/v1/spaces", headers=_auth(READER))
        granted = _grant(
            client,
            space["id"],
            "reader-read",
            {"principalId": reader_id, "actions": ["context.read"]},
        )
        after = client.get(f"/v1/spaces/{space['id']}", headers=_auth(READER))
        listed_after = client.get("/v1/spaces", headers=_auth(READER))
        permissions = client.get(
            "/v1/auth/permissions", params={"resourceId": space["id"]}, headers=_auth(READER)
        )
        grants = client.get(f"/v1/resources/{space['id']}/grants", headers=_auth(ADMIN))
        revoked = client.delete(
            f"/v1/resources/{space['id']}/grants/reader-read", headers=_auth(ADMIN)
        )
        closed = client.get(f"/v1/spaces/{space['id']}", headers=_auth(READER))
        closed_permissions = client.get(
            "/v1/auth/permissions", params={"resourceId": space["id"]}, headers=_auth(READER)
        )

    assert before.status_code == 404
    assert listed_before.json() == []
    assert granted.status_code == 200
    assert granted.json() == {
        "id": "reader-read",
        "resourceId": space["id"],
        "actions": ["context.read"],
        "principalId": reader_id,
    }
    assert after.status_code == 200 and after.json() == space
    assert listed_after.json() == [space]
    assert permissions.json() == {"resourceId": space["id"], "actions": ["context.read"]}
    assert [item["id"] for item in grants.json()] == ["reader-read"]
    assert revoked.status_code == 204
    assert closed.status_code == 404
    assert closed_permissions.status_code == 404


def test_ingestion_requires_ingest_write_and_jobs_stay_scoped(tmp_path):
    with _client(tmp_path) as client:
        space = client.post("/v1/spaces", headers=_auth(ADMIN), json={"name": "Ops"}).json()
        source = _register_source(client, space["id"])
        service_id = _principal_id(client, SERVICE)
        reader_id = _principal_id(client, READER)
        _grant(
            client, source["id"], "svc", {"principalId": service_id, "actions": ["ingest.write"]}
        )
        _grant(client, space["id"], "rdr", {"principalId": reader_id, "actions": ["context.read"]})
        upload = _stage(
            client, SERVICE, source["id"], b"Confirm the rollback checkpoint.", "up-1234567"
        )
        body = _ingestion(space["id"], source["id"], upload)
        headers = {"Idempotency-Key": body["idempotencyKey"]}

        accepted = client.post(
            "/v1/ingestions",
            headers=_auth(SERVICE, **headers, **{"X-Trace-Id": "trace-svc"}),
            json=body,
        )
        replay = client.post("/v1/ingestions", headers=_auth(SERVICE, **headers), json=body)
        denied = client.post("/v1/ingestions", headers=_auth(READER, **headers), json=body)
        invisible = client.post("/v1/ingestions", headers=_auth(MEMBER, **headers), json=body)
        status_url = accepted.json()["statusUrl"]
        own_job = client.get(status_url, headers=_auth(SERVICE))
        admin_job = client.get(status_url, headers=_auth(ADMIN))
        reader_job = client.get(status_url, headers=_auth(READER))

    assert accepted.status_code == 202
    assert accepted.headers["location"] == status_url
    assert replay.json()["jobId"] == accepted.json()["jobId"]
    assert denied.status_code == 403
    assert denied.json()["code"] == "access_denied"
    assert denied.json()["message"] == "The requested context is not available."
    assert invisible.status_code == 404
    assert own_job.status_code == 200
    assert own_job.json()["state"] == "queued"
    assert own_job.json()["traceId"] == "trace-svc"
    assert admin_job.status_code == 200
    assert reader_job.status_code == 404


def test_forged_principal_fields_and_grant_escalation_are_rejected(tmp_path):
    with _client(tmp_path) as client:
        space = client.post("/v1/spaces", headers=_auth(ADMIN), json={"name": "Ops"}).json()
        reader_id = _principal_id(client, READER)
        _grant(client, space["id"], "rdr", {"principalId": reader_id, "actions": ["context.read"]})
        body = {**_ingestion(space["id"]), "principalId": reader_id}

        forged = client.post(
            "/v1/ingestions",
            headers=_auth(READER, **{"Idempotency-Key": body["idempotencyKey"]}),
            json=body,
        )
        escalate_self = _grant(
            client,
            space["id"],
            "self",
            {"principalId": reader_id, "actions": ["access.manage"]},
            token=READER,
        )
        escalate_root = _grant(
            client,
            "engine",
            "self",
            {"principalId": reader_id, "actions": ["space.manage"]},
            token=READER,
        )
        unknown_space = _grant(
            client, "spc_missing", "x", {"principalId": reader_id, "actions": ["context.read"]}
        )
        unknown_target = _grant(
            client, space["id"], "x", {"principalId": "prn_ghost", "actions": ["context.read"]}
        )
        both_subjects = _grant(
            client,
            space["id"],
            "x",
            {"principalId": reader_id, "group": "g", "actions": ["context.read"]},
        )
        bad_action = _grant(
            client, space["id"], "x", {"principalId": reader_id, "actions": ["dataset.read"]}
        )
        reader_list = client.get(f"/v1/resources/{space['id']}/grants", headers=_auth(READER))
        create_space = client.post("/v1/spaces", headers=_auth(READER), json={"name": "Mine"})

    assert forged.status_code == 400
    assert escalate_self.status_code == 403
    assert escalate_root.status_code == 403
    assert unknown_space.status_code == 404
    assert unknown_target.status_code == 400
    assert both_subjects.status_code == 400
    assert bad_action.status_code == 400
    assert reader_list.status_code == 403
    assert create_space.status_code == 403


def test_group_grant_and_lost_membership(tmp_path):
    with _client(tmp_path) as client:
        space = client.post("/v1/spaces", headers=_auth(ADMIN), json={"name": "Ops"}).json()
        _grant(client, space["id"], "readers", {"group": "readers", "actions": ["context.read"]})
        member = client.get(f"/v1/spaces/{space['id']}", headers=_auth(MEMBER))
        me = client.get("/v1/auth/me", headers=_auth(MEMBER))

    # The identity registry changes and the member is no longer in the group.
    with _client(tmp_path, member_groups=()) as client:
        lost = client.get(f"/v1/spaces/{space['id']}", headers=_auth(MEMBER))

    database = ControlDatabase(tmp_path / "control.db", MIGRATIONS)
    decisions = AuthorizationRepository(database)

    assert member.status_code == 200
    assert me.json()["groups"] == ["readers"]
    assert lost.status_code == 404
    assert decisions.count_decisions() > 0
    assert decisions.count_decisions("allowed") > 0


def _space_id(client: TestClient) -> str:
    return client.post("/v1/spaces", json={"name": "Incident response"}).json()["id"]


def test_ingestion_accepts_inline_content(tmp_path):
    with _client(tmp_path) as client:
        body = _ingestion(_space_id(client))
        accepted = client.post(
            "/v1/ingestions",
            headers={"Idempotency-Key": body["idempotencyKey"]},
            json=body,
        )
    assert accepted.status_code == 202
    assert accepted.json()["jobId"].startswith("job_")


def test_ingestion_accepts_staged_reference_without_inline_content(tmp_path):
    with _client(tmp_path) as client:
        body = _ingestion(_space_id(client))
        # A staged reference is the large-binary alternative to inline content.
        for field in ("content", "contentHash"):
            body.pop(field, None)
        body["contentRef"] = "staged-object-01J8M0"
        accepted = client.post(
            "/v1/ingestions",
            headers={"Idempotency-Key": body["idempotencyKey"]},
            json=body,
        )
    assert accepted.status_code == 202


def test_ingestion_requires_content_or_reference(tmp_path):
    with _client(tmp_path) as client:
        body = _ingestion(_space_id(client))
        for field in ("content", "contentHash", "contentRef"):
            body.pop(field, None)
        rejected = client.post(
            "/v1/ingestions",
            headers={"Idempotency-Key": body["idempotencyKey"]},
            json=body,
        )
    assert rejected.status_code == 400
    assert rejected.json()["code"] == "invalid_request"


def test_ingestion_rejects_content_hash_mismatch(tmp_path):
    with _client(tmp_path) as client:
        body = _ingestion(_space_id(client))
        body["contentHash"] = "sha256:" + "0" * 64
        rejected = client.post(
            "/v1/ingestions",
            headers={"Idempotency-Key": body["idempotencyKey"]},
            json=body,
        )
    assert rejected.status_code == 400
    assert rejected.json()["code"] == "invalid_request"


def test_api_returns_stable_safe_errors(tmp_path):
    with _client(tmp_path) as client:
        missing = client.get(
            "/v1/spaces/does-not-exist", headers=_auth(ADMIN, **{"X-Trace-Id": "trace-404"})
        )
        invalid = client.post("/v1/spaces", headers=_auth(ADMIN), json={"name": ""})

    assert missing.status_code == 404
    assert missing.json() == {
        "code": "not_found",
        "message": "Context space not found",
        "traceId": "trace-404",
    }
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "invalid_request"
    assert "input" not in invalid.text


def test_missing_token_file_fails_closed(tmp_path):
    settings = _settings(tmp_path, tmp_path / "absent.json")
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/health/live").status_code == 200
        assert client.get("/v1/spaces", headers=_auth(ADMIN)).status_code == 401


def test_api_routes_do_not_expose_backend_objects(tmp_path):
    with _client(tmp_path) as client:
        schema = client.get("/openapi.json").json()
    rendered = json.dumps(schema).lower()
    assert "knowledge_backend" not in rendered
    assert "backendreference" not in rendered
    assert "accesspartition" not in rendered
    assert "/v1/auth/me" in schema["paths"]

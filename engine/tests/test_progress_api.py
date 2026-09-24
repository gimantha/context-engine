"""Source progress API: reading marker, processing and indexing percentages, and access."""

from __future__ import annotations

from test_api import (
    ADMIN,
    MEMBER,
    MIGRATIONS,
    READER,
    SERVICE,
    _auth,
    _grant,
    _ingestion,
    _principal_id,
    _register_source,
    _stage,
)
from test_sources_api import _client, _drain, _setup

from context_engine.domain import IndexState
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


def _worker(tmp_path) -> JobWorker:
    database = ControlDatabase(tmp_path / "control.db", MIGRATIONS)
    authorization = AuthorizationRepository(database)
    metrics = MetricsRegistry()
    return JobWorker(
        ControlPlaneRepository(database),
        LifecycleJobHandler(SourceRepository(database)),
        metrics,
        JobAuthorizer(
            Authorizer(authorization, metrics),
            authorization,
            StaticTokenVerifier.from_file(tmp_path / "tokens.json"),
        ),
        lease_seconds=5,
    )


def _deliver(client, space: dict, source: dict, number: int) -> None:
    upload = _stage(
        client, SERVICE, source["id"], f"body {number}".encode(), f"up-prog-{number:04d}"
    )
    body = _ingestion(space["id"], source["id"], upload)
    body["sourceRecordId"] = f"doc-{number}"
    body["idempotencyKey"] = f"evt-prog-{number:04d}"
    response = client.post(
        "/v1/ingestions",
        headers=_auth(SERVICE, **{"Idempotency-Key": body["idempotencyKey"]}),
        json=body,
    )
    assert response.status_code == 202, response.text


async def test_progress_reports_reading_state_and_processing_percentage(tmp_path):
    with _client(tmp_path) as client:
        space, source = _setup(client)
        url = f"/v1/progress/sources/{source['id']}"
        runs = f"/v1/sources/{source['id']}/sync-runs"

        idle = client.get(url, headers=_auth(SERVICE)).json()
        opened = client.post(runs, headers=_auth(SERVICE))
        for number in range(4):
            _deliver(client, space, source, number)
        queued = client.get(url, headers=_auth(SERVICE)).json()
        assert await _worker(tmp_path).run_once()
        partial = client.get(url, headers=_auth(ADMIN)).json()
        run_id = opened.json()["id"]
        completed = client.post(f"{runs}/{run_id}/complete", headers=_auth(SERVICE))
        repeated = client.post(f"{runs}/{run_id}/complete", headers=_auth(SERVICE))
        await _drain(tmp_path)
        done = client.get(url, headers=_auth(SERVICE)).json()

    assert idle["reading"] == {
        "state": "idle",
        "runId": None,
        "startedAt": None,
        "completedAt": None,
    }
    assert idle["processing"]["total"] == 0 and idle["processing"]["percent"] is None
    assert idle["indexing"]["state"] == "not_collected" and idle["indexing"]["percent"] is None
    assert opened.status_code == 201 and opened.json()["state"] == "reading"
    assert queued["reading"]["state"] == "reading" and queued["reading"]["runId"] == run_id
    assert queued["processing"]["since"] == opened.json()["startedAt"]
    assert (queued["processing"]["total"], queued["processing"]["queued"]) == (4, 4)
    assert queued["processing"]["percent"] == 0.0
    assert partial["processing"]["succeeded"] == 1 and partial["processing"]["percent"] == 25.0
    assert partial["records"] == {"active": 1, "quarantined": 0, "deleted": 0}
    assert completed.status_code == 200 and completed.json()["state"] == "completed"
    assert completed.json()["completedAt"] is not None
    assert repeated.status_code == 200 and repeated.json() == completed.json()
    assert done["reading"]["state"] == "completed"
    assert done["processing"]["percent"] == 100.0 and done["records"]["active"] == 4


def test_new_sync_run_supersedes_the_old_one_and_resets_the_window(tmp_path):
    with _client(tmp_path) as client:
        space, source = _setup(client)
        runs = f"/v1/sources/{source['id']}/sync-runs"
        first = client.post(runs, headers=_auth(SERVICE)).json()
        _deliver(client, space, source, 1)
        second = client.post(runs, headers=_auth(SERVICE)).json()
        progress = client.get(f"/v1/progress/sources/{source['id']}", headers=_auth(SERVICE)).json()
        complete_first = client.post(f"{runs}/{first['id']}/complete", headers=_auth(SERVICE))
        complete_unknown = client.post(f"{runs}/run_missing/complete", headers=_auth(SERVICE))
        other_space = client.post("/v1/spaces", headers=_auth(ADMIN), json={"name": "B"}).json()
        other_source = _register_source(client, other_space["id"])
        _grant(
            client,
            other_source["id"],
            "svc",
            {"principalId": _principal_id(client, SERVICE), "actions": ["ingest.write"]},
        )
        complete_foreign = client.post(
            f"/v1/sources/{other_source['id']}/sync-runs/{second['id']}/complete",
            headers=_auth(SERVICE),
        )

    assert progress["reading"]["runId"] == second["id"]
    assert progress["processing"]["total"] == 0
    assert complete_first.status_code == 409
    assert complete_unknown.status_code == 404
    assert complete_foreign.status_code == 404


def test_progress_requires_delivery_or_management_rights(tmp_path):
    with _client(tmp_path) as client:
        space, source = _setup(client)
        url = f"/v1/progress/sources/{source['id']}"
        reader = client.get(url, headers=_auth(READER))
        member = client.get(url, headers=_auth(MEMBER))
        missing = client.get("/v1/progress/sources/src_missing", headers=_auth(ADMIN))
        unauthenticated = client.get(url)
        reader_run = client.post(f"/v1/sources/{source['id']}/sync-runs", headers=_auth(READER))
        admin_space = client.get(f"/v1/progress/spaces/{space['id']}", headers=_auth(ADMIN))
        reader_space = client.get(f"/v1/progress/spaces/{space['id']}", headers=_auth(READER))
        member_space = client.get(f"/v1/progress/spaces/{space['id']}", headers=_auth(MEMBER))
        client.patch(f"/v1/sources/{source['id']}", headers=_auth(ADMIN), json={"state": "paused"})
        paused_run = client.post(f"/v1/sources/{source['id']}/sync-runs", headers=_auth(SERVICE))

    assert reader.status_code == 403
    assert member.status_code == 404
    assert missing.status_code == 404
    assert unauthenticated.status_code == 401
    assert reader_run.status_code == 403
    assert admin_space.status_code == 200
    assert [item["sourceId"] for item in admin_space.json()["sources"]] == [source["id"]]
    assert reader_space.status_code == 200 and reader_space.json()["sources"] == []
    assert member_space.status_code == 404
    assert paused_run.status_code == 409


async def test_indexing_comes_from_the_ledger_when_the_backend_is_enabled(tmp_path):
    with _client(tmp_path, knowledge_backend="provider") as client:
        space, source = _setup(client)
        url = f"/v1/progress/sources/{source['id']}"
        for number in range(3):
            _deliver(client, space, source, number)
        await _drain(tmp_path)
        pending = client.get(url, headers=_auth(SERVICE)).json()["indexing"]
        sources = SourceRepository(ControlDatabase(tmp_path / "control.db", MIGRATIONS))
        sources.set_index_state(space["id"], source["id"], "doc-0", IndexState.INDEXED)
        sources.set_index_state(space["id"], source["id"], "doc-1", IndexState.INDEXED)
        sources.set_index_state(
            space["id"], source["id"], "doc-2", IndexState.FAILED, "extraction_unsupported"
        )
        settled = client.get(url, headers=_auth(SERVICE))
        status = client.get(
            f"/v1/sources/{source['id']}/records/doc-2", headers=_auth(SERVICE)
        ).json()

    indexing = settled.json()["indexing"]
    assert (pending["state"], pending["expected"], pending["indexing"]) == ("ok", 3, 3)
    assert pending["percent"] == 0.0
    assert indexing["state"] == "ok" and indexing["percent"] == 66.7
    assert (indexing["indexed"], indexing["failed"], indexing["missing"]) == (2, 1, 0)
    assert status["indexState"] == "failed" and status["indexError"] == "extraction_unsupported"
    assert "dataset" not in settled.text.lower()


def test_indexing_is_not_reported_while_the_backend_is_disabled(tmp_path):
    with _client(tmp_path) as client:
        _, source = _setup(client)
        indexing = client.get(
            f"/v1/progress/sources/{source['id']}", headers=_auth(SERVICE)
        ).json()["indexing"]

    assert indexing["state"] == "not_collected" and indexing["percent"] is None

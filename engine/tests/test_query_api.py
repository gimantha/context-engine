"""Context queries and enrichment through the API, with the worker indexing into a test backend."""

from __future__ import annotations

import json
from itertools import count
from pathlib import Path

from fastapi.testclient import TestClient
from test_api import (
    ADMIN,
    MEMBER,
    MIGRATIONS,
    READER,
    SERVICE,
    _auth,
    _grant,
    _principal_id,
    _stage,
    _tokens,
)

from context_engine.api import create_app
from context_engine.config import Settings
from context_engine.domain import IndexState, JobOperation
from context_engine.knowledge_backend import BackendError, BackendErrorCode, DummyKnowledgeBackend
from context_engine.observability import MetricsRegistry
from context_engine.persistence import (
    AuthorizationRepository,
    ControlDatabase,
    ControlPlaneRepository,
    ReadAccessRepository,
    SourceRepository,
    StagingStore,
)
from context_engine.security.authorization import Authorizer
from context_engine.security.identity import StaticTokenVerifier
from context_engine.worker import (
    EnrichmentJobHandler,
    JobAuthorizer,
    JobWorker,
    LifecycleJobHandler,
    OperationDispatcher,
    RecordIndexer,
)

ROOT = Path(__file__).resolve().parents[2]
SERVICE_ID = "context-engine-service"
MAPPING = {"source-group:research": "readers", "source-group:ops": "ops"}


class _Stack:
    def __init__(self, tmp_path, backend=None, *, provider=True):
        self.tmp_path = tmp_path
        self.backend = backend or DummyKnowledgeBackend()
        settings = Settings(
            database_path=tmp_path / "control.db",
            migrations_path=MIGRATIONS,
            static_tokens_path=_tokens(tmp_path),
            staging_path=tmp_path / "staging",
            knowledge_backend="provider" if provider else "none",
        )
        self.client = TestClient(
            create_app(settings, knowledge_backend=self.backend if provider else None)
        )
        self.sequence = count()

    def worker(self):
        database = ControlDatabase(self.tmp_path / "control.db", MIGRATIONS)
        sources = SourceRepository(database)
        authorization = AuthorizationRepository(database)
        metrics = MetricsRegistry()
        indexer = RecordIndexer(
            sources, self.backend, StagingStore(self.tmp_path / "staging"), SERVICE_ID, metrics
        )
        lifecycle = LifecycleJobHandler(
            sources, indexer=indexer, staging=StagingStore(self.tmp_path / "staging")
        )
        return JobWorker(
            ControlPlaneRepository(database),
            OperationDispatcher(
                {
                    JobOperation.INGESTION: lifecycle,
                    JobOperation.UPDATE: lifecycle,
                    JobOperation.DELETION: lifecycle,
                    JobOperation.ENRICHMENT: EnrichmentJobHandler(
                        sources, self.backend, SERVICE_ID, metrics
                    ),
                }
            ),
            metrics,
            JobAuthorizer(
                Authorizer(authorization, metrics),
                authorization,
                StaticTokenVerifier.from_file(self.tmp_path / "tokens.json"),
            ),
            lease_seconds=5,
        )

    async def drain(self):
        worker = self.worker()
        while await worker.run_once():
            pass

    def setup(self):
        client = self.client
        space = client.post("/v1/spaces", headers=_auth(ADMIN), json={"name": "Ops"}).json()
        source = client.post(
            f"/v1/spaces/{space['id']}/sources",
            headers=_auth(ADMIN),
            json={"name": "Runbooks", "type": "file", "audienceMapping": MAPPING},
        ).json()
        _grant(
            client,
            source["id"],
            "svc",
            {"principalId": _principal_id(client, SERVICE), "actions": ["ingest.write"]},
        )
        _grant(client, space["id"], "readers", {"group": "readers", "actions": ["context.read"]})
        _grant(
            client,
            space["id"],
            "rdr",
            {"principalId": _principal_id(client, READER), "actions": ["context.read"]},
        )
        return space, source

    def deliver(
        self,
        space,
        source,
        record_id,
        version,
        operation="upsert",
        *,
        content=b"",
        audience=("source-group:research",),
    ):
        number = next(self.sequence)
        body = json.loads((ROOT / "contracts/examples/ingestion-upsert.json").read_text())
        body.update(
            spaceId=space["id"],
            sourceId=source["id"],
            sourceRecordId=record_id,
            sourceVersion=version,
            operation=operation,
            audience=list(audience),
            idempotencyKey=f"evt-{number:06d}",
        )
        if operation == "upsert":
            upload = _stage(self.client, SERVICE, source["id"], content, f"up-{number:06d}")
            body.update(
                contentRef=upload["uploadId"],
                contentHash=upload["contentHash"],
                contentType=upload["contentType"],
            )
        else:
            for key in ("contentRef", "contentHash", "contentType", "sourceUrl"):
                body.pop(key, None)
        response = self.client.post(
            "/v1/ingestions",
            headers=_auth(SERVICE, **{"Idempotency-Key": body["idempotencyKey"]}),
            json=body,
        )
        assert response.status_code == 202, response.text

    def query(self, token, space, question, **extra):
        body = {"spaceId": space["id"], "question": question, "mode": "context", **extra}
        return self.client.post("/v1/queries", headers=_auth(token), json=body)


async def test_member_finds_indexed_passages_with_lineage(tmp_path):
    stack = _Stack(tmp_path)
    with stack.client:
        space, source = stack.setup()
        stack.deliver(
            space, source, "runbook-84", "84", content=b"Confirm the rollback checkpoint."
        )
        await stack.drain()
        response = stack.query(MEMBER, space, "rollback checkpoint")

    body = response.json()
    assert response.status_code == 200
    assert body["state"] == "completed" and body["insufficientEvidence"] is False
    [evidence] = body["evidence"]
    assert evidence["recordId"] == "runbook-84" and evidence["sourceVersion"] == "84"
    assert evidence["sourceId"] == source["id"]
    assert evidence["passage"] == "Confirm the rollback checkpoint."
    assert evidence["sourceUrl"] == "https://sources.invalid/runbooks/84"
    assert evidence["id"].startswith("evi_") and body["queryId"].startswith("qry_")
    assert "prt_" not in response.text and "dataset" not in response.text.lower()


async def test_access_rules_for_queries(tmp_path):
    stack = _Stack(tmp_path)
    with stack.client:
        space, source = stack.setup()
        other = stack.client.post(
            "/v1/spaces", headers=_auth(ADMIN), json={"name": "Hidden"}
        ).json()
        stack.deliver(
            space, source, "runbook-84", "84", content=b"Confirm the rollback checkpoint."
        )
        await stack.drain()
        outside_audience = stack.query(READER, space, "rollback checkpoint")
        no_read_action = stack.query(ADMIN, space, "rollback checkpoint")
        invisible = stack.query(MEMBER, other, "rollback checkpoint")
        answer_mode = stack.query(MEMBER, space, "rollback checkpoint", mode="answer")
        blank = stack.query(MEMBER, space, "   ")
        unauthenticated = stack.client.post("/v1/queries", json={"spaceId": space["id"]})

    # Readers outside the record's audiences learn nothing, not even that something exists.
    assert outside_audience.status_code == 200
    assert outside_audience.json()["state"] == "insufficient_evidence"
    assert outside_audience.json()["evidence"] == []
    assert no_read_action.status_code == 403
    assert invisible.status_code == 404
    assert answer_mode.status_code == 400 and blank.status_code == 400
    assert unauthenticated.status_code == 401


async def test_versions_and_deletions_are_reflected_in_queries(tmp_path):
    stack = _Stack(tmp_path)
    with stack.client:
        space, source = stack.setup()
        stack.deliver(space, source, "doc-1", "1", content=b"oldword guidance")
        await stack.drain()
        stack.deliver(space, source, "doc-1", "2", content=b"newword guidance")
        await stack.drain()
        new = stack.query(MEMBER, space, "newword").json()
        old = stack.query(MEMBER, space, "oldword").json()
        stack.deliver(space, source, "doc-1", "3", "delete")
        await stack.drain()
        deleted = stack.query(MEMBER, space, "newword").json()

    assert [(item["recordId"], item["sourceVersion"]) for item in new["evidence"]] == [
        ("doc-1", "2")
    ]
    assert old["state"] == "insufficient_evidence"
    assert deleted["state"] == "insufficient_evidence"


async def test_barrier_hides_records_the_ledger_does_not_show(tmp_path):
    stack = _Stack(tmp_path)
    with stack.client:
        space, source = stack.setup()
        stack.deliver(space, source, "doc-1", "1", content=b"gateway runbook")
        await stack.drain()
        visible = stack.query(MEMBER, space, "gateway").json()
        sources = SourceRepository(ControlDatabase(tmp_path / "control.db", MIGRATIONS))
        sources.set_index_state(space["id"], source["id"], "doc-1", IndexState.FAILED, "x")
        hidden = stack.query(MEMBER, space, "gateway").json()

    assert visible["state"] == "completed"
    # The backend still returns the passage, but the ledger no longer vouches for it.
    assert hidden["state"] == "insufficient_evidence"


class _LaggingBackend(DummyKnowledgeBackend):
    """Refuse multi-partition queries, as a backend does while one read grant is missing."""

    async def query(self, request, principal, authorized_partitions):
        if len(authorized_partitions) > 1:
            raise BackendError(BackendErrorCode.ACCESS_DENIED, "denied")
        return await super().query(request, principal, authorized_partitions)


async def test_backend_refusals_fall_back_to_one_partition_at_a_time(tmp_path):
    stack = _Stack(tmp_path, _LaggingBackend())
    with stack.client:
        space, source = stack.setup()
        stack.deliver(space, source, "doc-a", "1", content=b"gateway alpha")
        stack.deliver(
            space,
            source,
            "doc-b",
            "1",
            content=b"gateway beta",
            audience=("source-group:research", "source-group:ops"),
        )
        await stack.drain()
        applied = ReadAccessRepository(ControlDatabase(tmp_path / "control.db", MIGRATIONS))
        applied.set_synced(1, 1)
        result = stack.query(MEMBER, space, "gateway").json()

    assert sorted(item["recordId"] for item in result["evidence"]) == ["doc-a", "doc-b"]
    assert applied.last_synced() is None


async def test_queries_and_enrichment_need_the_backend(tmp_path):
    stack = _Stack(tmp_path, provider=False)
    with stack.client:
        space, _ = stack.setup()
        _grant(
            stack.client, space["id"], "enrich", {"group": "readers", "actions": ["context.enrich"]}
        )
        query = stack.query(MEMBER, space, "anything")
        enrichment = stack.client.post(
            f"/v1/spaces/{space['id']}/enrichments",
            headers=_auth(MEMBER, **{"Idempotency-Key": "enrich-000001"}),
        )

    assert query.status_code == 503 and query.json()["code"] == "unavailable"
    assert enrichment.status_code == 503


async def test_enrichment_runs_per_partition_and_is_recorded(tmp_path):
    stack = _Stack(tmp_path)
    with stack.client:
        space, source = stack.setup()
        _grant(
            stack.client, space["id"], "enrich", {"group": "readers", "actions": ["context.enrich"]}
        )
        stack.deliver(space, source, "doc-a", "1", content=b"alpha")
        stack.deliver(
            space,
            source,
            "doc-b",
            "1",
            content=b"beta",
            audience=("source-group:research", "source-group:ops"),
        )
        await stack.drain()
        url = f"/v1/spaces/{space['id']}/enrichments"
        accepted = stack.client.post(
            url, headers=_auth(MEMBER, **{"Idempotency-Key": "enrich-000001"})
        )
        replay = stack.client.post(
            url, headers=_auth(MEMBER, **{"Idempotency-Key": "enrich-000001"})
        )
        denied = stack.client.post(
            url, headers=_auth(READER, **{"Idempotency-Key": "enrich-000002"})
        )
        await stack.drain()
        status = stack.client.get(accepted.json()["statusUrl"], headers=_auth(MEMBER)).json()

    job = ControlPlaneRepository(ControlDatabase(tmp_path / "control.db", MIGRATIONS)).get_job(
        accepted.json()["jobId"]
    )
    assert (
        accepted.status_code == 202 and accepted.headers["location"] == accepted.json()["statusUrl"]
    )
    assert replay.json()["jobId"] == accepted.json()["jobId"]
    assert denied.status_code == 403
    assert status["state"] == "succeeded" and status["operation"] == "enrichment"
    assert job.result == {
        "operation": "enrichment",
        "pipelineVersion": "enrich@1",
        "partitions": 2,
        "affectedRecords": 2,
        "createdArtifacts": 0,
    }

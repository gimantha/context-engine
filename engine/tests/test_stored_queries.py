"""Stored queries and evidence reads (M5 slice 2): each query is kept with typed evidence, only
its asker reopens it, evidence by id follows current access, and every read re-runs the
visibility barrier while erasure follows the record version's content."""

from __future__ import annotations

import json
import sqlite3
from itertools import count

import yaml
from fastapi.testclient import TestClient
from jsonschema import validate
from test_api import ADMIN, MEMBER, MIGRATIONS, READER, SERVICE, _auth, _grant, _principal_id
from test_query_api import ROOT, _Stack

from context_engine.api import create_app
from context_engine.config import Settings
from context_engine.knowledge_backend import DummyKnowledgeBackend

COLLEAGUE = "colleague-token-0123456789abcdef0"
AUDITOR = "auditor-token-0123456789abcdef012"
RUNBOOK = (
    b"Runbook 84.\n\n"
    b"Confirm the rollback checkpoint before remediation.\n"
    b"Then restart the gateway.\n"
    b"Call the on-call engineer."
)


def _schema(name):
    """Return a contract schema with its local references resolved, for validation."""

    document = yaml.safe_load((ROOT / "contracts/openapi/context-engine-v1.yaml").read_text())
    schemas = document["components"]["schemas"]

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(schemas[node["$ref"].rsplit("/", 1)[-1]])
            return {key: resolve(value) for key, value in node.items()}
        if isinstance(node, list):
            return [resolve(value) for value in node]
        return node

    return resolve(schemas[name])


def _tokens(tmp_path):
    """The usual identities, plus a colleague in both audiences and an ops auditor."""

    def user(token, subject, groups):
        return {
            "token": token,
            "issuer": "static://test",
            "subject": subject,
            "kind": "user",
            "groups": groups,
        }

    entries = [
        {
            "token": ADMIN,
            "issuer": "static://test",
            "subject": "admin",
            "kind": "user",
            "bootstrapActions": ["access.manage", "space.manage"],
        },
        user(READER, "reader", []),
        {"token": SERVICE, "issuer": "static://test", "subject": "connector", "kind": "service"},
        user(MEMBER, "member", ["readers"]),
        user(COLLEAGUE, "colleague", ["readers", "ops"]),
        user(AUDITOR, "auditor", ["ops"]),
    ]
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({"version": "1", "tokens": entries}))
    return path


class _StoredStack(_Stack):
    """The query stack with a line-chunking backend and the extra identities."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.backend = DummyKnowledgeBackend(chunk_lines=1)
        settings = Settings(
            database_path=tmp_path / "control.db",
            migrations_path=MIGRATIONS,
            static_tokens_path=_tokens(tmp_path),
            staging_path=tmp_path / "staging",
            knowledge_backend="provider",
        )
        self.client = TestClient(create_app(settings, knowledge_backend=self.backend))
        self.sequence = count()

    def get(self, path, token):
        return self.client.get(path, headers=_auth(token))

    def change_audience(self, space, source, record_id, version, audience):
        """Move a record to other audiences without a new content version."""

        number = next(self.sequence)
        key = f"acl-{number:06d}"
        body = {
            "schemaVersion": "1",
            "spaceId": space["id"],
            "sourceId": source["id"],
            "sourceRecordId": record_id,
            "sourceVersion": version,
            "operation": "acl_changed",
            "sourceObservedAt": "2026-10-01T00:00:00Z",
            "audience": list(audience),
            "sourceAclVersion": str(100 + number),
            "idempotencyKey": key,
        }
        response = self.client.post(
            "/v1/ingestions", headers=_auth(SERVICE, **{"Idempotency-Key": key}), json=body
        )
        assert response.status_code == 202, response.text

    def rows(self, sql, *values):
        connection = sqlite3.connect(self.tmp_path / "control.db")
        try:
            return connection.execute(sql, values).fetchall()
        finally:
            connection.close()


def _setup(stack):
    space, source = stack.setup()
    stack.deliver(space, source, "runbook-84", "84", content=RUNBOOK)
    return space, source


async def test_a_query_is_stored_with_typed_evidence_and_reopens_the_same(tmp_path):
    stack = _StoredStack(tmp_path)
    with stack.client:
        space, _ = _setup(stack)
        await stack.drain()
        response = stack.query(MEMBER, space, "rollback checkpoint")
        assert response.status_code == 200, response.text
        body = response.json()
        query_id = body["queryId"]
        reopened = stack.get(f"/v1/queries/{query_id}", MEMBER)
        listed = stack.get(f"/v1/queries/{query_id}/evidence", MEMBER)
        [evidence] = body["evidence"]
        opened = stack.get(f"/v1/evidence/{evidence['id']}", MEMBER)
        [stored] = stack.rows(
            "SELECT question, outcome, retrieved_count, suppressed_count, partitions_json,"
            " models_json, policy_version FROM queries WHERE id = ?",
            query_id,
        )

    assert evidence["passage"] == "Confirm the rollback checkpoint before remediation."
    assert evidence["location"] == "chunk:1", "the compact form earlier clients read"
    assert evidence["locator"] == {
        "chunkIndex": 1,
        "characters": {"start": 12, "end": 63},
        "sentences": {"first": 2, "last": 2},
        "lines": {"first": 3, "last": 3},
    }
    validate(body, _schema("QueryResponse"))
    validate(opened.json(), _schema("Evidence"))
    assert reopened.status_code == 200 and reopened.json() == body
    assert listed.status_code == 200 and listed.json() == body["evidence"]
    assert opened.status_code == 200 and opened.json() == evidence
    question, outcome, retrieved, suppressed, partitions, models, policy_version = stored
    assert (question, outcome, retrieved, suppressed) == ("rollback checkpoint", "completed", 1, 0)
    assert len(json.loads(partitions)) == 1 and policy_version
    assert json.loads(models)["embedding"]["configuredBy"] == "environment"


async def test_chunk_order_places_a_repeated_passage(tmp_path):
    stack = _StoredStack(tmp_path)
    with stack.client:
        space, source = stack.setup()
        stack.deliver(
            space,
            source,
            "doc-1",
            "1",
            content=b"Restart the gateway.\nCheck logs.\nRestart the gateway.",
        )
        await stack.drain()
        evidence = stack.query(MEMBER, space, "restart gateway").json()["evidence"]

    placed = sorted((item["locator"]["chunkIndex"], item["locator"]["lines"]) for item in evidence)
    assert placed == [(0, {"first": 1, "last": 1}), (2, {"first": 3, "last": 3})]
    assert len({item["id"] for item in evidence}) == 2


async def test_only_the_asker_reopens_a_query_and_evidence_follows_access(tmp_path):
    stack = _StoredStack(tmp_path)
    with stack.client:
        space, source = _setup(stack)
        stack.deliver(
            space,
            source,
            "ops-1",
            "1",
            content=b"Ops escalation goes to the duty manager.",
            audience=("source-group:ops",),
        )
        _grant(
            stack.client,
            space["id"],
            "auditor",
            {"principalId": _principal_id(stack.client, AUDITOR), "actions": ["evidence.read"]},
        )
        await stack.drain()
        member_query = stack.query(MEMBER, space, "rollback checkpoint").json()
        ops_query = stack.query(COLLEAGUE, space, "escalation duty").json()
        [runbook] = member_query["evidence"]
        [ops] = ops_query["evidence"]

        others = [
            stack.get(f"/v1/queries/{member_query['queryId']}", token).status_code
            for token in (COLLEAGUE, READER, ADMIN, AUDITOR)
        ]
        others_evidence = stack.get(
            f"/v1/queries/{member_query['queryId']}/evidence", COLLEAGUE
        ).status_code
        colleague_opens = stack.get(f"/v1/evidence/{runbook['id']}", COLLEAGUE)
        auditor_opens_ops = stack.get(f"/v1/evidence/{ops['id']}", AUDITOR)
        auditor_cannot_ask = stack.query(AUDITOR, space, "escalation duty").status_code
        refused = [
            stack.get(f"/v1/evidence/{ops['id']}", MEMBER).status_code,
            stack.get(f"/v1/evidence/{ops['id']}", READER).status_code,
            stack.get(f"/v1/evidence/{runbook['id']}", AUDITOR).status_code,
            stack.get(f"/v1/evidence/{runbook['id']}", ADMIN).status_code,
            stack.get("/v1/evidence/evi_000000000000000000000000", MEMBER).status_code,
            stack.get("/v1/queries/qry_unknown", MEMBER).status_code,
        ]
        unauthenticated = stack.client.get(f"/v1/evidence/{runbook['id']}").status_code

    assert others == [404, 404, 404, 404] and others_evidence == 404
    assert colleague_opens.status_code == 200 and colleague_opens.json() == runbook
    assert auditor_opens_ops.status_code == 200 and auditor_opens_ops.json() == ops
    assert auditor_cannot_ask == 403, "evidence.read opens citations but does not ask questions"
    assert refused == [404, 404, 404, 404, 404, 404]
    assert unauthenticated == 401


async def test_reads_rerun_the_barrier_against_current_audiences_and_grants(tmp_path):
    stack = _StoredStack(tmp_path)
    with stack.client:
        space, source = _setup(stack)
        await stack.drain()
        query = stack.query(MEMBER, space, "rollback checkpoint").json()
        [evidence] = query["evidence"]
        query_path, evidence_path = (
            f"/v1/queries/{query['queryId']}",
            f"/v1/evidence/{evidence['id']}",
        )

        stack.change_audience(space, source, "runbook-84", "84", ("source-group:ops",))
        await stack.drain()
        moved = stack.get(query_path, MEMBER).json()
        moved_evidence = stack.get(evidence_path, MEMBER).status_code
        colleague_still = stack.get(evidence_path, COLLEAGUE).status_code
        kept = stack.rows("SELECT COUNT(*) FROM evidence WHERE id = ?", evidence["id"])

        stack.change_audience(space, source, "runbook-84", "84", ("source-group:research",))
        await stack.drain()
        restored = stack.get(query_path, MEMBER).json()
        stack.client.delete(f"/v1/resources/{space['id']}/grants/readers", headers=_auth(ADMIN))
        revoked = stack.get(query_path, MEMBER).status_code

    assert moved["evidence"] == [] and moved["insufficientEvidence"] is True
    assert moved_evidence == 404 and colleague_still == 200, "the ops colleague may read it"
    assert kept == [(1,)], "the version is unchanged, so its evidence is kept"
    assert restored["evidence"] == [evidence]
    assert revoked == 404


async def test_evidence_is_erased_with_the_content_of_its_version(tmp_path):
    stack = _StoredStack(tmp_path)
    with stack.client:
        space, source = _setup(stack)
        await stack.drain()
        first = stack.query(MEMBER, space, "rollback checkpoint").json()
        [old] = first["evidence"]

        stack.deliver(
            space,
            source,
            "runbook-84",
            "85",
            content=RUNBOOK.replace(b"rollback", b"recovery"),
        )
        await stack.drain()
        after_update = stack.get(f"/v1/queries/{first['queryId']}", MEMBER).json()
        old_opened = stack.get(f"/v1/evidence/{old['id']}", MEMBER).status_code
        erased = stack.rows("SELECT COUNT(*) FROM evidence WHERE source_version = '84'")
        second = stack.query(MEMBER, space, "recovery checkpoint").json()
        [new] = second["evidence"]

        stack.deliver(space, source, "runbook-84", "86", "delete")
        await stack.drain()
        after_delete = stack.get(f"/v1/queries/{second['queryId']}", MEMBER).json()
        remaining = stack.rows("SELECT COUNT(*) FROM evidence")
        links = stack.rows("SELECT COUNT(*) FROM query_evidence")
        queries = stack.rows("SELECT COUNT(*) FROM queries")

    assert after_update["evidence"] == [] and old_opened == 404
    assert erased == [(0,)], "released content takes its evidence with it"
    assert new["sourceVersion"] == "85" and new["locator"]["characters"]["start"] == 12
    assert after_delete["evidence"] == []
    assert remaining == [(0,)] and links == [(0,)]
    assert queries == [(2,)], "the queries themselves stay until retention removes them"


async def test_a_caller_outside_every_audience_is_stored_without_evidence(tmp_path):
    stack = _StoredStack(tmp_path)
    with stack.client:
        space, _ = _setup(stack)
        await stack.drain()
        response = stack.query(READER, space, "rollback checkpoint").json()
        reopened = stack.get(f"/v1/queries/{response['queryId']}", READER).json()
        [stored] = stack.rows(
            "SELECT outcome, partitions_json, retrieved_count, models_json FROM queries"
        )

    assert response["evidence"] == [] and reopened["evidence"] == []
    assert stored == ("insufficient_evidence", "[]", 0, "{}")

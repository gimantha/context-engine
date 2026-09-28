"""One-call ingestion: the event and its content arrive in one multipart request."""

from __future__ import annotations

import hashlib
import json

import httpx
from test_api import ADMIN, MIGRATIONS, READER, ROOT, SERVICE, _auth, _tokens
from test_sources_api import _client, _drain, _setup

from context_engine.api import create_app
from context_engine.config import Settings
from context_engine.persistence import ControlDatabase, ControlPlaneRepository

BODY = b"Confirm the rollback checkpoint."


def _event(space: dict, source: dict, key: str, **changes) -> dict:
    value = json.loads((ROOT / "contracts/examples/ingestion-direct-event.json").read_text())
    value.update(spaceId=space["id"], sourceId=source["id"], idempotencyKey=key)
    value.update(changes)
    return {name: item for name, item in value.items() if item is not None}


def _deliver(client, source, event, content=BODY, content_type="text/plain", token=SERVICE):
    files = {"event": (None, json.dumps(event), "application/json")}
    if content is not None:
        files["content"] = ("record.txt", content, content_type)
    return client.post(
        f"/v1/sources/{source['id']}/ingestions",
        headers=_auth(token, **{"Idempotency-Key": event["idempotencyKey"]}),
        files=files,
    )


async def test_one_call_upsert_is_staged_accepted_and_applied(tmp_path):
    with _client(tmp_path) as client:
        space, source = _setup(client)
        event = _event(space, source, "event-000001")
        accepted = _deliver(client, source, event)
        replayed = _deliver(client, source, event)
        await _drain(tmp_path)
        record = client.get(
            f"/v1/sources/{source['id']}/records/{event['sourceRecordId']}",
            headers=_auth(SERVICE),
        ).json()
        found = client.get(
            f"/v1/sources/{source['id']}/ingestions/event-000001", headers=_auth(SERVICE)
        )
        missing = client.get(
            f"/v1/sources/{source['id']}/ingestions/event-never-sent", headers=_auth(SERVICE)
        )
        reader = client.get(
            f"/v1/sources/{source['id']}/ingestions/event-000001", headers=_auth(READER)
        )

    job = ControlPlaneRepository(ControlDatabase(tmp_path / "control.db", MIGRATIONS)).get_job(
        accepted.json()["jobId"]
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.headers["location"] == accepted.json()["statusUrl"]
    # The same event and bytes return the original job instead of a second one.
    assert replayed.json()["jobId"] == accepted.json()["jobId"]
    # The engine computed the hash and staged the bytes itself.
    content_hash = "sha256:" + hashlib.sha256(BODY).hexdigest()
    assert record["state"] == "active" and record["contentHash"] == content_hash
    assert (
        job.payload["contentRef"].startswith("stg_") and job.payload["contentType"] == "text/plain"
    )
    # A connector that lost the reply finds the job by its key.
    assert found.status_code == 200 and found.json()["jobId"] == accepted.json()["jobId"]
    assert missing.status_code == 404
    # Only principals with delivery rights on the source can look keys up.
    assert reader.status_code == 403


def test_one_call_idempotency_covers_event_and_bytes(tmp_path):
    with _client(tmp_path) as client:
        space, source = _setup(client)
        event = _event(space, source, "event-000002")
        first = _deliver(client, source, event)
        other_bytes = _deliver(client, source, event, content=b"Tampered body.")
        other_event = _deliver(client, source, {**event, "sourceVersion": "85"})

    assert first.status_code == 202
    assert other_bytes.status_code == 409 and other_bytes.json()["code"] == "conflict"
    assert other_event.status_code == 409


def test_one_call_checks_declared_content_and_request_shape(tmp_path):
    with _client(tmp_path) as client:
        space, source = _setup(client)
        good_hash = "sha256:" + hashlib.sha256(BODY).hexdigest()
        responses = {
            "matching declared hash": _deliver(
                client, source, _event(space, source, "event-000010", contentHash=good_hash)
            ),
            "wrong declared hash": _deliver(
                client,
                source,
                _event(space, source, "event-000011", contentHash="sha256:" + "0" * 64),
            ),
            "wrong declared type": _deliver(
                client, source, _event(space, source, "event-000012", contentType="text/html")
            ),
            "content reference": _deliver(
                client, source, _event(space, source, "event-000013", contentRef="stg_other")
            ),
            "upsert without content": _deliver(
                client, source, _event(space, source, "event-000014"), content=None
            ),
            "delete with content": _deliver(
                client,
                source,
                _event(space, source, "event-000015", operation="delete", sourceVersion="90"),
            ),
            "other source in event": _deliver(
                client, source, _event(space, source, "event-000016", sourceId="src_other")
            ),
            "key header differs": client.post(
                f"/v1/sources/{source['id']}/ingestions",
                headers=_auth(SERVICE, **{"Idempotency-Key": "event-header-only"}),
                files={
                    "event": (None, json.dumps(_event(space, source, "event-000017"))),
                    "content": ("r.txt", BODY, "text/plain"),
                },
            ),
            "content before event": client.post(
                f"/v1/sources/{source['id']}/ingestions",
                headers=_auth(SERVICE, **{"Idempotency-Key": "event-000018"}),
                files={
                    "content": ("r.txt", BODY, "text/plain"),
                    "event": (None, json.dumps(_event(space, source, "event-000018"))),
                },
            ),
            "malformed event": client.post(
                f"/v1/sources/{source['id']}/ingestions",
                headers=_auth(SERVICE, **{"Idempotency-Key": "event-000019"}),
                files={"event": (None, "{not json", "application/json")},
            ),
            "not multipart": client.post(
                f"/v1/sources/{source['id']}/ingestions",
                headers=_auth(
                    SERVICE,
                    **{"Idempotency-Key": "event-000020", "Content-Type": "application/json"},
                ),
                content=json.dumps(_event(space, source, "event-000020")),
            ),
        }
        delete = _deliver(
            client,
            source,
            _event(space, source, "event-000021", operation="delete", sourceVersion="91"),
            content=None,
        )

    statuses = {name: response.status_code for name, response in responses.items()}
    assert statuses == {
        "matching declared hash": 202,
        "wrong declared hash": 400,
        "wrong declared type": 400,
        "content reference": 400,
        "upsert without content": 400,
        "delete with content": 400,
        "other source in event": 400,
        "key header differs": 400,
        "content before event": 400,
        "malformed event": 400,
        "not multipart": 415,
    }
    # Deletes and ACL changes carry only the event.
    assert delete.status_code == 202


def test_one_call_content_limits_apply_while_streaming(tmp_path):
    with _client(tmp_path, upload_max_bytes=16) as client:
        space, source = _setup(client)
        oversized = _deliver(
            client, source, _event(space, source, "event-000030"), content=b"x" * 17
        )
        wrong_type = _deliver(
            client,
            source,
            _event(space, source, "event-000031"),
            content=b"\x89PNG",
            content_type="image/png",
        )

    assert oversized.status_code == 413
    assert wrong_type.status_code == 415


async def test_delivery_rights_are_checked_before_the_body_is_read(tmp_path):
    settings = Settings(
        database_path=tmp_path / "control.db",
        migrations_path=MIGRATIONS,
        static_tokens_path=_tokens(tmp_path),
        staging_path=tmp_path / "staging",
    )
    app = create_app(settings)
    with _client(tmp_path) as setup_client:
        space, source = _setup(setup_client)
    read = []

    async def body():
        read.append(True)
        yield b"--boundary\r\n"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://engine.local"
    ) as client:
        denied = await client.post(
            f"/v1/sources/{source['id']}/ingestions",
            headers=_auth(
                READER,
                **{
                    "Idempotency-Key": "event-000040",
                    "Content-Type": "multipart/form-data; boundary=boundary",
                },
            ),
            content=body(),
        )
        foreign = await client.post(
            "/v1/sources/src_unknown/ingestions",
            headers=_auth(
                ADMIN,
                **{
                    "Idempotency-Key": "event-000041",
                    "Content-Type": "multipart/form-data; boundary=boundary",
                },
            ),
            content=body(),
        )

    # Neither caller got as far as the body: rights are checked on the path's source first.
    assert denied.status_code == 403 and foreign.status_code == 404
    assert read == []


async def test_an_invalid_event_is_rejected_before_its_content_is_read(tmp_path):
    settings = Settings(
        database_path=tmp_path / "control.db",
        migrations_path=MIGRATIONS,
        static_tokens_path=_tokens(tmp_path),
        staging_path=tmp_path / "staging",
    )
    app = create_app(settings)
    with _client(tmp_path) as setup_client:
        space, source = _setup(setup_client)
    event = json.dumps(_event(space, source, "event-000050", sourceVersion=""))
    read = []

    async def body():
        # The event part and the content part's headers arrive first; the bytes come later.
        yield (
            b"--boundary\r\n"
            b'Content-Disposition: form-data; name="event"\r\n'
            b"Content-Type: application/json\r\n\r\n" + event.encode() + b"\r\n"
            b"--boundary\r\n"
            b'Content-Disposition: form-data; name="content"; filename="r.txt"\r\n'
            b"Content-Type: text/plain\r\n\r\n"
        )
        read.append(True)
        yield BODY + b"\r\n--boundary--\r\n"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://engine.local"
    ) as client:
        rejected = await client.post(
            f"/v1/sources/{source['id']}/ingestions",
            headers=_auth(
                SERVICE,
                **{
                    "Idempotency-Key": "event-000050",
                    "Content-Type": "multipart/form-data; boundary=boundary",
                },
            ),
            content=body(),
        )

    assert rejected.status_code == 400
    assert read == []


def test_a_refused_event_stages_nothing(tmp_path):
    with _client(tmp_path) as client:
        space, source = _setup(client)
        refused = _deliver(
            client, source, _event(space, source, "event-000060", sourceVersion="not-a-number")
        )

    assert refused.status_code == 400
    assert not (tmp_path / "staging").exists() or not any((tmp_path / "staging").iterdir())

"""Self-declared labels are signed, approved and tied to actual successful claims."""

import json
import time
import uuid

import pytest
from test_cli_auth import b64, identity, link, product
from test_device_login import request_body as device_request_body
from test_progress_board import BOARD, PRIVATE, seed
from test_scope_auth import approve, claim, request_body, review

from extore.agent_identity import normalize_identity, processing_worker, record_claim
from extore.db import db, init


@pytest.mark.parametrize(
    "name,kind",
    [("Bot\n", "dots"), ("Bot", "dots\u202e"), ("Bot", ""), ("Bot", "x" * 65)],
)
def test_identity_rejects_controls_bidi_and_length(name, kind):
    with pytest.raises(ValueError):
        normalize_identity(name, kind)


def signed_scope(key, pid, kind="dots", **kwargs):
    payload = request_body(key, pid=pid, **kwargs)
    payload.pop("signature")
    payload["agent_type"] = kind
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    payload["signature"] = b64(
        key[0].sign(
            (
                "extore-cli-scope-request-v1\nhttp://localhost:8000\n" + canonical
            ).encode()
        )
    )
    return payload


def test_scope_signed_type_tamper_and_reviewed_identity_upgrade(owner, client):
    pid, key = product(owner), identity()
    body = signed_scope(key, pid, name="Dots AI")
    tampered = dict(body, agent_type="grok_bot")
    assert client.post("/api/cli/scopes/request", json=tampered).status_code == 401
    created = client.post("/api/cli/scopes/request", json=body)
    assert created.status_code == 200, created.text
    value = created.json()
    assert review(owner, value)["request"]["agent_type"] == "dots"
    approve(owner, value)
    first = claim(client, value, key)
    auth = first["authorization"]
    assert auth["agent_type"] == "dots"
    request = signed_scope(
        key, pid, "grok_bot", name="Grok AI", aid=auth["id"], revision=auth["revision"]
    )
    pending = client.post("/api/cli/scopes/request", json=request)
    assert pending.status_code == 200, pending.text
    with db() as c:
        before = c.execute(
            "SELECT client_name,agent_type,revision FROM pipeline_authorizations WHERE id=?",
            (auth["id"],),
        ).fetchone()
        assert tuple(before) == ("Dots AI", "dots", auth["revision"])
    approve(owner, pending.json())
    upgraded = claim(client, pending.json(), key)
    assert upgraded["authorization"]["agent_type"] == "grok_bot"
    assert upgraded["authorization"]["revision"] == auth["revision"] + 1
    assert upgraded["bindings"][0]["device_id"] == first["bindings"][0]["device_id"]
    assert upgraded["bindings"][0]["agent_type"] == "grok_bot"


def test_device_type_signature_and_legacy_compatibility(client):
    key = identity()
    legacy = device_request_body(key)
    assert client.post("/api/cli/device/request", json=legacy).status_code == 200
    payload = device_request_body(key, name="Typed Bot")
    base = f"extore-cli-device-request-v1\nhttp://localhost:8000\n{key[1]}\nTyped Bot\n{payload['nonce']}\n"
    payload["agent_type"] = "dots"
    payload["signature"] = b64(key[0].sign((base + "\ndots").encode()))
    assert (
        client.post(
            "/api/cli/device/request", json=dict(payload, agent_type="other")
        ).status_code
        == 401
    )
    assert client.post("/api/cli/device/request", json=payload).status_code == 200
    changed = dict(
        payload,
        agent_type="other",
        signature=b64(key[0].sign((base + "\nother").encode())),
    )
    assert client.post("/api/cli/device/request", json=changed).status_code == 401


def test_two_devices_actual_claim_snapshot_not_link_or_current_name(
    owner, client, monkeypatch
):
    pid = product(owner)
    grant, _ = link(owner, pid, max_cli_uses=2)
    with db() as c:
        sid = c.execute("SELECT shop_id FROM products WHERE id=?", (pid,)).fetchone()[0]
        devices = []
        for name, kind in [("Dots worker", "dots"), ("Grok worker", "grok_bot")]:
            did = str(uuid.uuid4())
            c.execute(
                "INSERT INTO cli_devices(id,staff_id,public_key,fingerprint,client_name,created,last_seen,agent_type) VALUES (?,?,?,?,?,?,?,?)",
                (
                    did,
                    grant["id"],
                    str(uuid.uuid4()),
                    PRIVATE,
                    name,
                    time.time(),
                    time.time(),
                    kind,
                ),
            )
            devices.append(did)
    jobs = [seed(pid, "processing", grant["id"]) for _ in devices]
    with db() as c:
        for jid, did in zip(jobs, devices):
            row = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            record_claim(c, row, grant["id"], device_id=did)
        c.execute(
            "UPDATE cli_devices SET client_name='Current changed name',agent_type='other'"
        )
    from extore import secret_store

    monkeypatch.setattr(
        secret_store,
        "open_secret",
        lambda *a, **k: pytest.fail("board decrypted a secret"),
    )
    response = owner.get(BOARD, params={"shop_id": sid})
    assert response.status_code == 200, response.text
    workers = {w["name"]: w for w in response.json()["workers"]}
    assert workers["Dots worker"]["agent_type"] == "dots"
    assert workers["Grok worker"]["agent_type"] == "grok_bot"
    assert all(w["kind"] == "cli" for w in workers.values())
    assert PRIVATE not in response.text
    assert all(did not in response.text for did in devices)
    with db() as c:
        c.execute("UPDATE jobs SET attempt=2 WHERE id=?", (jobs[0],))
        assert (
            processing_worker(
                c, c.execute("SELECT * FROM jobs WHERE id=?", (jobs[0],)).fetchone()
            )
            is None
        )


def test_schema17_additive_null_identity_and_empty_claims(owner):
    product(owner)
    init()
    with db() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 17
        assert (
            c.execute("SELECT count(*) FROM job_worker_identities").fetchone()[0] == 0
        )
        for table in (
            "cli_devices",
            "cli_device_requests",
            "cli_scope_requests",
            "pipeline_authorizations",
        ):
            columns = {r["name"]: r for r in c.execute(f"PRAGMA table_info({table})")}
            assert columns["agent_type"]["notnull"] == 0
            assert columns["agent_type"]["dflt_value"] is None


def test_pipeline_one_bot_groups_multiple_actual_device_bindings(owner, client):
    pids, key = [product(owner), product(owner)], identity()
    with db() as c:
        sid = c.execute(
            "SELECT shop_id FROM products WHERE id=?", (pids[0],)
        ).fetchone()[0]
    payload = request_body(
        key, kind="shop.pipeline", sid=sid, pids=pids, name="Pipeline Dots"
    )
    payload.pop("signature")
    payload["agent_type"] = "dots"
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    payload["signature"] = b64(
        key[0].sign(
            (
                "extore-cli-scope-request-v1\nhttp://localhost:8000\n" + canonical
            ).encode()
        )
    )
    pending = client.post("/api/cli/scopes/request", json=payload)
    assert pending.status_code == 200, pending.text
    approve(owner, pending.json())
    granted = claim(client, pending.json(), key)
    for binding in granted["bindings"]:
        jid = seed(binding["product_id"], "processing", binding["staff_id"])
        with db() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            record_claim(c, row, binding["staff_id"], device_id=binding["device_id"])
    result = owner.get(BOARD, params={"shop_id": sid})
    assert result.status_code == 200, result.text
    workers = result.json()["workers"]
    assert len(workers) == 1
    assert workers[0]["name"] == "Pipeline Dots"
    assert workers[0]["agent_type"] == "dots"
    assert workers[0]["active_jobs"] == 2
    jobs = [job for item in result.json()["products"] for job in item["jobs"]]
    assert {job["worker_id"] for job in jobs} == {workers[0]["id"]}
    with db() as c:
        rows = c.execute(
            "SELECT device_ref,worker_ref FROM job_worker_identities"
        ).fetchall()
        assert len({r["device_ref"] for r in rows}) == 2
        assert len({r["worker_ref"] for r in rows}) == 1


def test_typed_private_bind_signed_labels_cannot_relabel_existing_device(owner, client):
    pid, key = product(owner), identity()
    _, private_token = link(owner, pid)
    base = f"extore-cli-bind-v1\nhttp://localhost:8000\n{private_token}\n{key[1]}"
    payload = {
        "token": private_token,
        "public_key": key[1],
        "client_name": "Dots Private",
        "agent_type": "dots",
        "signature": b64(key[0].sign((base + "\nDots Private\ndots").encode())),
    }
    assert (
        client.post(
            "/api/cli/authorize", json=dict(payload, agent_type="other")
        ).status_code
        == 401
    )
    response = client.post("/api/cli/authorize", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["agent_type"] == "dots"
    changed = dict(
        payload,
        agent_type="other",
        signature=b64(key[0].sign((base + "\nDots Private\nother").encode())),
    )
    assert client.post("/api/cli/authorize", json=changed).status_code == 409
    with db() as c:
        assert (
            c.execute(
                "SELECT agent_type FROM cli_devices WHERE id=?",
                (response.json()["device_id"],),
            ).fetchone()[0]
            == "dots"
        )

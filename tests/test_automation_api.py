"""Real API claims and authorization races, using only isolated test cards."""

import base64
import hashlib
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_cli_auth import authorize, b64, bearer, identity, link, login

from extore import automation
from extore.app import app
from extore.db import db
from extore.pipeline_scopes import materialize, revoke_authorization
from extore.security import card_digest
from extore.service import issue_cards, submit

ORIGIN = "http://localhost:8000"
PERMISSIONS = ["queue.view", "queue.process", "queue.retry"]


@pytest.fixture(autouse=True)
def automation_tables():
    with db() as c:
        automation.init_schema(c)
        c.execute("DELETE FROM automation_requests")


def product(owner):
    response = owner.post(
        "/api/admin/products",
        json={
            "name": "Isolated automation task",
            "parameters": [{"key": "request", "label": {"zh-CN": "需求"}}],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def queue(pid, text="current input"):
    with db() as c:
        code = issue_cards(c, pid, 1)[0]
        card = c.execute(
            "SELECT * FROM cards WHERE digest=?", (card_digest(code),)
        ).fetchone()
        return dict(submit(c, card, {"request": text}))


def device(owner, pid, *, key=None, permissions=None):
    _, secret = link(owner, pid, permissions=permissions or PERMISSIONS)
    client = TestClient(app, base_url=ORIGIN)
    grant, key = authorize(client, secret, key or identity())
    token = login(client, grant, key)
    return client, grant, key, bearer(token)


def request(*devices, wait=0, limit=1, request_id=None):
    value = {
        "request_id": request_id or secrets.token_urlsafe(24),
        "issued_at": int(time.time()),
        "wait_seconds": wait,
        "limit": limit,
        "grants": [
            {"device_id": grant["device_id"], "product_id": grant["product_id"]}
            for _, grant, _, _ in devices
        ],
    }
    for item, (_, grant, key, _) in zip(value["grants"], devices, strict=True):
        item["signature"] = b64(
            key[0].sign(
                automation.next_proof(ORIGIN, value, grant["device_id"]).encode()
            )
        )
    return value


def next_(primary, body):
    client, _, _, headers = primary
    return client.post("/api/manage/next", json=body, headers=headers)


def test_next_fifo_multiple_products_and_exact_receipt(owner):
    a, b = product(owner), product(owner)
    one, two = queue(a, "first"), queue(b, "second")
    devices = device(owner, a), device(owner, b)
    body = request(*devices)
    response = next_(devices[0], body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["items"]) == 1
    assert result["items"][0]["job"]["id"] == one["id"]
    assert result["items"][0]["execution"]["params"] == {"request": "first"}
    assert "outputs" in result["items"][0]["execution"]
    assert "token" not in result and "content" not in result["items"][0]["job"]
    replay = next_(devices[0], body).json()
    assert replay["replayed"] and replay["items"][0]["job"]["id"] == one["id"]
    with db() as c:
        assert (
            c.execute("SELECT state FROM jobs WHERE id=?", (two["id"],)).fetchone()[0]
            == "queued"
        )
        assert (
            "first"
            not in c.execute("SELECT items FROM automation_requests").fetchone()[0]
        )


def test_next_same_signed_request_changed_primary_session_reuses_receipt(owner):
    a, b = product(owner), product(owner)
    first, second = queue(a), queue(b)
    devices = device(owner, a), device(owner, b)
    body = request(*devices)
    initial = next_(devices[0], body)
    assert initial.status_code == 200, initial.text
    replay = next_(devices[1], body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"]
    assert replay.json()["items"] == initial.json()["items"]
    assert replay.json()["items"][0]["job"]["id"] == first["id"]
    with db() as c:
        assert c.execute("SELECT count(*) FROM automation_requests").fetchone()[0] == 1
        assert (
            c.execute("SELECT state FROM jobs WHERE id=?", (second["id"],)).fetchone()[
                0
            ]
            == "queued"
        )


def test_next_limit_and_no_history_or_unapproved_product(owner):
    a, b = product(owner), product(owner)
    approved, hidden = queue(a), queue(b, "other merchant input")
    dev = device(owner, a)
    value = next_(dev, request(dev, limit=10)).json()
    assert [item["job"]["id"] for item in value["items"]] == [approved["id"]]
    idle = next_(dev, request(dev)).json()
    assert idle["idle"] and idle["items"] == []
    with db() as c:
        assert (
            c.execute("SELECT state FROM jobs WHERE id=?", (hidden["id"],)).fetchone()[
                0
            ]
            == "queued"
        )


def test_next_wrong_device_key_cannot_impersonate_other_grant(owner):
    a, b = product(owner), product(owner)
    queue(a)
    queue(b)
    first, second = device(owner, a), device(owner, b)
    body = request(first, second)
    body["grants"][1]["signature"] = b64(
        first[2][0].sign(
            automation.next_proof(ORIGIN, body, second[1]["device_id"]).encode()
        )
    )
    assert next_(first, body).status_code == 401
    with db() as c:
        assert (
            c.execute("SELECT count(*) FROM jobs WHERE state='queued'").fetchone()[0]
            == 2
        )


@pytest.mark.parametrize(
    "field,value",
    [("limit", 11), ("limit", True), ("wait_seconds", 26), ("wait_seconds", -1)],
)
def test_next_bounds_fail_before_claim(owner, field, value):
    pid = product(owner)
    queue(pid)
    dev = device(owner, pid)
    body = request(dev)
    body[field] = value
    assert next_(dev, body).status_code == 422


def test_next_scope_tampering_and_old_timestamp_rejected(owner):
    pid = product(owner)
    queue(pid)
    dev = device(owner, pid)
    body = request(dev)
    body["limit"] = 2
    assert next_(dev, body).status_code == 401
    body = request(dev)
    body["issued_at"] -= 121
    body["grants"][0]["signature"] = b64(
        dev[2][0].sign(
            automation.next_proof(ORIGIN, body, dev[1]["device_id"]).encode()
        )
    )
    assert next_(dev, body).status_code == 401


def test_next_parallel_ai_cannot_claim_same_task(owner):
    pid = product(owner)
    row = queue(pid)
    devices = device(owner, pid), device(owner, pid)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda dev: next_(dev, request(dev)), devices))
    assert all(reply.status_code == 200 for reply in replies)
    items = [item for reply in replies for item in reply.json()["items"]]
    assert len(items) == 1 and items[0]["job"]["id"] == row["id"]


def test_next_parallel_same_request_only_claims_once(owner):
    pid = product(owner)
    queue(pid)
    queue(pid)
    dev = device(owner, pid)
    body = request(dev)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: next_(dev, body), range(2)))
    assert all(reply.status_code == 200 for reply in replies)
    assert len({reply.json()["items"][0]["job"]["id"] for reply in replies}) == 1
    with db() as c:
        assert (
            c.execute("SELECT count(*) FROM jobs WHERE state='queued'").fetchone()[0]
            == 1
        )


def test_next_replay_after_completion_does_not_claim_another_task(owner):
    pid = product(owner)
    one, two = queue(pid), queue(pid)
    dev = device(owner, pid)
    body = request(dev)
    assert next_(dev, body).status_code == 200
    with db() as c:
        c.execute("UPDATE jobs SET state='succeeded' WHERE id=?", (one["id"],))
    reply = next_(dev, body).json()
    assert reply["items"] == [] and reply["stale"]
    with db() as c:
        assert (
            c.execute("SELECT state FROM jobs WHERE id=?", (two["id"],)).fetchone()[0]
            == "queued"
        )


def test_next_wait_rechecks_revocation_without_holding_transaction(owner, monkeypatch):
    pid = product(owner)
    dev = device(owner, pid)
    calls = []

    async def sleep(_):
        # This writer would block if the endpoint kept its transaction while waiting.
        with db() as c:
            c.execute(
                "UPDATE staff SET revoked=1 WHERE id="
                "(SELECT staff_id FROM cli_devices WHERE id=?)",
                (dev[1]["device_id"],),
            )
        queue(pid)
        calls.append(True)

    monkeypatch.setattr(automation, "asyncio", SimpleNamespace(sleep=sleep))
    reply = next_(dev, request(dev, wait=1))
    assert calls and reply.status_code == 401
    with db() as c:
        assert (
            c.execute("SELECT count(*) FROM jobs WHERE state='queued'").fetchone()[0]
            == 1
        )


def test_next_wait_claims_new_arrival_without_another_llm_request(owner, monkeypatch):
    pid = product(owner)
    dev = device(owner, pid)
    rows = []

    async def sleep(_):
        rows.append(queue(pid))

    monkeypatch.setattr(automation, "asyncio", SimpleNamespace(sleep=sleep))
    reply = next_(dev, request(dev, wait=1))
    assert reply.status_code == 200, reply.text
    assert len(rows) == 1 and reply.json()["items"][0]["job"]["id"] == rows[0]["id"]


def test_next_view_only_grant_and_browser_are_rejected(owner):
    pid = product(owner)
    dev = device(owner, pid, permissions=["queue.view"])
    assert next_(dev, request(dev)).status_code == 403
    assert owner.post("/api/manage/next", json=request(dev)).status_code == 401


def test_next_pipeline_snapshot_excludes_new_products_and_checks_revocation(owner):
    pid = product(owner)
    key = identity()
    with db() as c:
        sid = c.execute("SELECT shop_id FROM products WHERE id=?", (pid,)).fetchone()[0]
        approved = materialize(
            c,
            {
                "shop_id": sid,
                "kind": "shop.pipeline",
                "product_ids": [pid],
                "permissions": PERMISSIONS,
            },
            key[1],
            "Isolated snapshot bot",
            hashlib.sha256(base64.urlsafe_b64decode(key[1] + "=")).hexdigest(),
            "owner",
            issuer_role="root",
            issuer_shop_id=None,
        )
    client = TestClient(app, base_url=ORIGIN)
    grant = approved["bindings"][0]
    dev = client, grant, key, bearer(login(client, grant, key))
    future = product(owner)
    later = queue(future)
    assert next_(dev, request(dev)).json()["items"] == []
    tampered = request(dev)
    tampered["grants"][0]["product_id"] = future
    tampered["grants"][0]["signature"] = b64(
        key[0].sign(
            automation.next_proof(ORIGIN, tampered, grant["device_id"]).encode()
        )
    )
    assert next_(dev, tampered).status_code == 403
    with db() as c:
        revoke_authorization(c, approved["authorization"]["id"], "owner")
    assert next_(dev, request(dev)).status_code == 401
    with db() as c:
        assert (
            c.execute("SELECT state FROM jobs WHERE id=?", (later["id"],)).fetchone()[0]
            == "queued"
        )


def test_next_resumed_expired_pending_wait_does_not_claim_later_arrival(owner):
    pid = product(owner)
    dev = device(owner, pid)
    body = request(dev, wait=1)
    key = hashlib.sha256(
        (dev[1]["device_id"] + ":" + body["request_id"]).encode()
    ).hexdigest()
    with db() as c:
        now = time.time()
        c.execute(
            "INSERT INTO automation_requests VALUES (?,?,?,'pending',?,?,?,'[]')",
            (
                key,
                dev[1]["device_id"],
                hashlib.sha256(automation.next_canonical(body).encode()).hexdigest(),
                now - 2,
                now - 1,
                now + 600,
            ),
        )
    row = queue(pid)
    reply = next_(dev, body)
    assert reply.status_code == 200 and reply.json()["items"] == []
    assert reply.json()["replayed"]
    with db() as c:
        assert (
            c.execute("SELECT state FROM jobs WHERE id=?", (row["id"],)).fetchone()[0]
            == "queued"
        )


def test_next_cleanup_cannot_make_old_signed_request_claim_again(owner):
    pid = product(owner)
    dev = device(owner, pid)
    body = request(dev)
    body["issued_at"] -= 601
    body["grants"][0]["signature"] = b64(
        dev[2][0].sign(
            automation.next_proof(ORIGIN, body, dev[1]["device_id"]).encode()
        )
    )
    with db() as c:
        now = time.time()
        c.execute(
            "INSERT INTO automation_requests VALUES ('expired',?,?, 'done',?,?,?,'[]')",
            (dev[1]["device_id"], "expired-body", now - 700, now - 699, now - 1),
        )
        assert automation.cleanup(c) == 1
    row = queue(pid)
    assert next_(dev, body).status_code == 401
    with db() as c:
        assert (
            c.execute("SELECT state FROM jobs WHERE id=?", (row["id"],)).fetchone()[0]
            == "queued"
        )

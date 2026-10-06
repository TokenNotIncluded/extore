import asyncio
import json
import time

from test_redemption import redeem

from extore.db import db
from extore.sdk import verify_event
from extore.security import sign
from extore.worker import job_once, outbox_once

SECRET = "this-is-a-test-signing-secret-32-chars"


def callback(owner, pid, j, body, nonce="nonce-1", secret=SECRET, ts=None):
    raw = json.dumps(body).encode()
    ts = ts or str(int(time.time()))
    return owner.post(
        f"/api/callbacks/{pid}/{j['id']}",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Extore-Timestamp": ts,
            "X-Extore-Nonce": nonce,
            "X-Extore-Signature": sign(secret, ts, nonce, raw),
        },
    )


def test_signed_callback_replay_stale_and_terminal(owner, setup_product):
    pid, code = setup_product(
        mode="webhook", webhook_url="https://example.com/hooks", webhook_secret=SECRET
    )
    t, j = redeem(owner, code)
    data = {"state": "succeeded", "attempt": 1, "content": "secret"}
    assert callback(owner, pid, j, data, secret="wrong").status_code == 401
    assert callback(owner, pid, j, data, ts="1").status_code == 401
    assert callback(owner, pid, j, {**data, "attempt": 2}).status_code == 409
    assert callback(owner, pid, j, data).status_code == 200
    assert callback(owner, pid, j, data).status_code == 409
    assert (
        callback(
            owner, pid, j, {**data, "content": "overwrite"}, nonce="nonce-2"
        ).status_code
        == 200
    )
    assert (
        owner.post("/api/receipt/reveal", json={"token": t}).json()["content"]
        == "secret"
    )
    assert (
        callback(
            owner, pid, j, {"state": "failed", "attempt": 1}, nonce="nonce-3"
        ).status_code
        == 409
    )


def test_script_sdk_end_to_end(owner, setup_product):
    pid, code = setup_product(
        mode="script",
        script="welcome",
        parameters=[{"key": "name", "label": {"zh-CN": "昵称"}}],
    )
    t = owner.post("/api/exchange", json={"code": code}).json()["token"]
    assert (
        owner.post(
            "/api/redeem", json={"token": t, "params": {"name": "小明"}}
        ).status_code
        == 200
    )
    assert asyncio.run(job_once()) is True
    assert (
        owner.post("/api/receipt", json={"token": t}).json()["job"]["state"]
        == "succeeded"
    )
    assert (
        "小明" in owner.post("/api/receipt/reveal", json={"token": t}).json()["content"]
    )


def test_interruption_does_not_auto_retry(owner, setup_product):
    _, code = setup_product()
    t, j = redeem(owner, code)
    owner.post(
        "/api/manage/batch",
        json={"product_id": j["product_id"], "ids": [j["id"]], "action": "claim"},
    )
    with db() as c:
        c.execute("UPDATE jobs SET lease=1 WHERE id=?", (j["id"],))
    asyncio.run(job_once())
    data = owner.post("/api/receipt", json={"token": t}).json()["job"]
    assert data["state"] == "failed" and not data["can_retry"]


def test_outbox_retry_survives_and_sdk_signature(owner, setup_product, monkeypatch):
    pid, code = setup_product(
        mode="webhook", webhook_url="https://example.com/hooks", webhook_secret=SECRET
    )
    t, j = redeem(owner, code)
    from extore import worker

    async def down(row):
        raw = row["payload"].encode()
        ts = str(int(time.time()))
        nonce = "test"
        assert (
            verify_event(
                SECRET,
                raw,
                {
                    "X-Extore-Timestamp": ts,
                    "X-Extore-Nonce": nonce,
                    "X-Extore-Signature": sign(SECRET, ts, nonce, raw),
                },
            )["type"]
            == "redemption.requested"
        )
        raise ValueError("HTTP 503")

    monkeypatch.setattr(worker, "deliver_event", down)
    assert asyncio.run(outbox_once())
    with db() as c:
        row = c.execute("SELECT * FROM outbox").fetchone()
        assert row["attempts"] == 1 and row["state"] == "pending"
        assert row["error"] == "HTTP 503"


def test_outbound_private_address_denied():
    import pytest

    from extore.worker import deliver_event

    with pytest.raises(ValueError, match="公网"):
        asyncio.run(deliver_event({"url": "https://127.0.0.1/test"}))


def test_expired_attempt_event_cancelled(owner, setup_product):
    _, code = setup_product(
        mode="webhook", webhook_url="https://example.com/hooks", webhook_secret=SECRET
    )
    t, j = redeem(owner, code)
    with db() as c:
        c.execute("UPDATE jobs SET state='failed' WHERE id=?", (j["id"],))
    assert asyncio.run(outbox_once())
    with db() as c:
        assert c.execute("SELECT state FROM outbox").fetchone()[0] == "cancelled"


def test_platform_issuance_idempotency(owner, setup_product):
    from extore.db import set_setting
    from extore.security import digest

    pid, _ = setup_product()
    with db() as c:
        set_setting(c, "integration_key", digest("platform-test-key"))
    data = {"product_id": pid, "count": 2}
    headers = {
        "Authorization": "Bearer platform-test-key",
        "Idempotency-Key": "order-123",
    }
    a = owner.post("/api/integrations/cards", json=data, headers=headers)
    b = owner.post("/api/integrations/cards", json=data, headers=headers)
    assert a.status_code == 200 and a.json() == b.json()
    assert (
        owner.post(
            "/api/integrations/cards", json={**data, "count": 3}, headers=headers
        ).status_code
        == 409
    )
    with db() as c:
        assert c.execute("SELECT count(*) FROM cards").fetchone()[0] == 3
        assert (
            a.json()["codes"][0].encode()
            not in c.execute("SELECT response FROM api_requests").fetchone()[0]
        )

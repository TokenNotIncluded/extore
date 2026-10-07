"""Real signatures, browser scope isolation and atomic device-code admission."""

import json
import re
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response
from test_cli_auth import (
    authorize,
    b64,
    bearer,
    identity,
    link,
    login,
    product,
    quotas,
    staff_browser,
)

from extore import device_login
from extore.app import app
from extore.db import db, init
from extore.security import create_session, digest

ORIGIN = "http://localhost:8000"
CLI = "/api/cli/device"
BROWSER = "/api/manage/device"


@pytest.fixture
def clients():
    opened = []

    def create(*, cookie=None):
        client = TestClient(app, base_url=ORIGIN)
        if cookie:
            client.cookies.set("extore_session", cookie)
            client.headers["Origin"] = ORIGIN
        opened.append(client)
        return client

    yield create
    for client in opened:
        client.close()


def request_body(key, *, nonce=None, name="Document Bot", pid=None, origin=ORIGIN):
    nonce = nonce or b64(int(time.time()).to_bytes(8, "big") + secrets.token_bytes(24))
    proof = f"extore-cli-device-request-v1\n{origin}\n{key[1]}\n{name}\n{nonce}\n{pid or ''}"
    return {
        "public_key": key[1],
        "signature": b64(key[0].sign(proof.encode())),
        "client_name": name,
        "nonce": nonce,
        "product_id": pid,
    }


def pending(client, key=None, **kwargs):
    key = key or identity()
    payload = request_body(key, **kwargs)
    response = client.post(CLI + "/request", json=payload)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["approval_url"] == ORIGIN + "/cli/device"
    assert re.fullmatch(
        r"[A-HJ-NP-Z2-9]{4}(?:-[A-HJ-NP-Z2-9]{4}){2}", value["user_code"]
    )
    assert re.fullmatch(r"[0-9a-f]{64}", value["fingerprint"])
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", value["challenge"])
    assert 590 < value["expires_in"] <= 600
    assert value["interval"] == 5
    assert "set-cookie" not in response.headers
    return value, key, payload


def proof_body(value, key, action="status", *, origin=ORIGIN):
    proof = f"extore-cli-device-{action}-v1\n{origin}\n{value['request_id']}\n"
    if action == "claim":
        proof += value["challenge"] + "\n"
    proof += key[1]
    return {
        "request_id": value["request_id"],
        "public_key": key[1],
        "signature": b64(key[0].sign(proof.encode())),
    }


def review(owner, value, grant=None):
    response = owner.post(
        BROWSER + "/options",
        json={
            "user_code": value["user_code"],
            **({"staff_id": grant["id"]} if grant else {}),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def approve(owner, value, grant, *, context=None):
    context = context or review(owner, value, grant)
    response = owner.post(
        BROWSER + "/approve",
        json={
            "user_code": value["user_code"],
            "staff_id": grant["id"],
            "review_digest": context["review_digest"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "status": "approved"}
    return context


def claim(client, value, key):
    response = client.post(CLI + "/claim", json=proof_body(value, key, "claim"))
    assert response.status_code == 200, response.text
    assert "set-cookie" not in response.headers
    return response.json()


def _grant_state(rid):
    with db() as c:
        return dict(
            c.execute("SELECT * FROM cli_device_requests WHERE id=?", (rid,)).fetchone()
        )


def test_device_page_is_public_but_lookup_is_not(clients):
    cli = clients()
    assert cli.get("/cli/device").status_code == 200
    assert cli.head("/cli/device").status_code == 200
    value, _, _ = pending(cli)
    response = cli.post(
        BROWSER + "/options",
        json={"user_code": value["user_code"]},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 401


def test_owner_explicitly_approves_product_only_and_no_token_enters_cli(owner, clients):
    pid = product(owner)
    grant, invitation = link(
        owner, pid, permissions=["queue.view", "queue.process", "queue.retry"]
    )
    cli = clients()
    value, key, payload = pending(cli)
    choices = review(owner, value)
    assert choices["candidates"][0]["staff_id"] == grant["id"]
    assert choices["candidates"][0]["permissions"] == grant["permissions"]
    assert quotas(grant["id"]) == (0, 1, 0, 1)
    context = approve(owner, value, grant)
    assert context["selected"]["product_id"] == pid
    assert quotas(grant["id"]) == (0, 1, 0, 1)
    binding = claim(cli, value, key)
    assert binding["permissions"] == grant["permissions"]
    assert binding["product_id"] == pid
    assert binding["expires"] == grant["expires"]
    assert not binding["already_authorized"]
    assert quotas(grant["id"]) == (0, 1, 1, 1)
    session = login(cli, binding, key)
    headers = bearer(session)
    assert cli.get("/api/manage/products", headers=headers).status_code == 200
    assert cli.get("/api/admin/products", headers=headers).status_code == 401
    assert (
        cli.post(
            "/api/manage/cards", json={"product_id": pid, "count": 1}, headers=headers
        ).status_code
        == 403
    )
    assert invitation not in json.dumps([payload, value, choices, context, binding])
    assert "shop.owner" not in json.dumps([value, context, binding])
    assert "approved_session_digest" not in json.dumps(binding)


def test_staff_uses_own_link_and_browser_quota_stays_independent(owner, clients):
    pid = product(owner)
    first, secret = link(owner, pid)
    second, _ = link(owner, pid)
    browser = staff_browser(clients, secret)
    browser.headers["Origin"] = ORIGIN
    cli = clients()
    value, key, _ = pending(cli, pid=pid)
    choices = review(browser, value)
    assert [item["staff_id"] for item in choices["candidates"]] == [first["id"]]
    assert (
        browser.post(
            BROWSER + "/options",
            json={"user_code": value["user_code"], "staff_id": second["id"]},
        ).status_code
        == 403
    )
    approve(browser, value, first)
    claim(cli, value, key)
    assert quotas(first["id"]) == (1, 1, 1, 1)
    assert browser.get("/api/manage/products").status_code == 200


@pytest.mark.parametrize(
    "field", ["client_name", "nonce", "product_id", "public_key", "signature"]
)
def test_request_signature_binds_every_field_and_rejects_reflection(clients, field):
    key = identity()
    payload = request_body(key)
    payload[field] = {
        "client_name": "Other Bot",
        "nonce": request_body(key)["nonce"],
        "product_id": str(uuid.uuid4()),
        "public_key": identity()[1],
        "signature": "X" * 86,
    }[field]
    response = clients().post(CLI + "/request", json=payload)
    assert response.status_code == 401
    for name in ("signature", "nonce", "public_key"):
        assert payload[name] not in response.text
    with db() as c:
        assert c.execute("SELECT count(*) FROM cli_device_requests").fetchone()[0] == 0


def test_request_is_origin_bound_and_nonce_replay_never_renews_ttl(clients):
    cli, key = clients(), identity()
    bad = request_body(key, origin="https://evil.example")
    assert cli.post(CLI + "/request", json=bad).status_code == 401
    value, _, body = pending(cli, key)
    again = cli.post(CLI + "/request", json=body).json()
    assert again["request_id"] == value["request_id"]
    assert again["user_code"] == value["user_code"]
    assert again["expires"] == value["expires"]
    altered = request_body(key, nonce=body["nonce"], name="Rename after nonce")
    assert cli.post(CLI + "/request", json=altered).status_code == 401
    with db() as c:
        c.execute(
            "UPDATE cli_device_requests SET expires=? WHERE id=?",
            (time.time() - 1, value["request_id"]),
        )
    expired = cli.post(CLI + "/request", json=body).json()
    assert expired["expires_in"] == 0
    old_nonce = b64(
        (int(time.time()) - 1000).to_bytes(8, "big") + secrets.token_bytes(24)
    )
    old_body = request_body(key, nonce=old_nonce)
    assert cli.post(CLI + "/request", json=old_body).status_code == 401


@pytest.mark.parametrize("mixed", ["cookie", "bearer"])
@pytest.mark.parametrize("endpoint", ["request", "status", "claim"])
def test_public_handshake_rejects_mixed_browser_or_bearer_credentials(
    owner, clients, mixed, endpoint
):
    cli = clients()
    value, key, body = pending(cli)
    payload = body if endpoint == "request" else proof_body(value, key, endpoint)
    if mixed == "cookie":
        cli.cookies.set("extore_session", owner.cookies.get("extore_session"))
    else:
        cli.headers["Authorization"] = "Bearer synthetic-token"
    response = cli.post(CLI + "/" + endpoint, json=payload)
    assert response.status_code == 400


def test_status_and_claim_proofs_cannot_be_swapped_or_use_another_device(clients):
    cli = clients()
    value, key, _ = pending(cli)
    assert (
        cli.post(CLI + "/status", json=proof_body(value, identity())).status_code == 401
    )
    assert (
        cli.post(CLI + "/status", json=proof_body(value, key, "claim")).status_code
        == 401
    )
    assert (
        cli.post(CLI + "/claim", json=proof_body(value, key, "status")).status_code
        == 401
    )
    assert (
        cli.post(CLI + "/claim", json=proof_body(value, key, "claim")).status_code
        == 401
    )
    result = cli.post(CLI + "/status", json=proof_body(value, key)).json()
    assert set(result) == {"status", "expires", "interval"}
    assert result["status"] == "pending"


def test_polling_too_fast_is_persistently_slowed_down(clients):
    cli = clients()
    value, key, _ = pending(cli)
    payload = proof_body(value, key)
    assert cli.post(CLI + "/status", json=payload).json()["status"] == "pending"
    slow = cli.post(CLI + "/status", json=payload).json()
    assert slow["status"] == "slow_down"
    assert slow["interval"] == slow["retry_after"] == 10
    slow = cli.post(CLI + "/status", json=payload).json()
    assert slow["interval"] == 15
    with db() as c:
        c.execute(
            "UPDATE cli_device_requests SET poll_after=0 WHERE id=?",
            (value["request_id"],),
        )
    normal = cli.post(CLI + "/status", json=payload).json()
    assert normal["status"] == "pending" and normal["interval"] == 15


def test_reject_and_expiry_never_consume_quota(owner, clients):
    grant, _ = link(owner, product(owner))
    cli = clients()
    value, key, _ = pending(cli)
    result = owner.post(BROWSER + "/deny", json={"user_code": value["user_code"]})
    assert result.json() == {"ok": True, "status": "denied"}
    assert (
        cli.post(CLI + "/status", json=proof_body(value, key)).json()["status"]
        == "denied"
    )
    assert (
        cli.post(CLI + "/claim", json=proof_body(value, key, "claim")).status_code
        == 401
    )
    value2, key2, _ = pending(cli)
    approve(owner, value2, grant)
    with db() as c:
        c.execute(
            "UPDATE cli_device_requests SET expires=? WHERE id=?",
            (time.time() - 1, value2["request_id"]),
        )
    assert (
        cli.post(CLI + "/status", json=proof_body(value2, key2)).json()["status"]
        == "expired"
    )
    assert (
        cli.post(CLI + "/claim", json=proof_body(value2, key2, "claim")).status_code
        == 401
    )
    assert quotas(grant["id"]) == (0, 1, 0, 1)


def test_code_lookup_failures_share_bounded_session_bucket(owner):
    responses = [
        owner.post(BROWSER + "/options", json={"user_code": secrets.token_hex(6)})
        for _ in range(6)
    ]
    assert [response.status_code for response in responses] == [401] * 5 + [429]
    with db() as c:
        keys = [
            row["key"]
            for row in c.execute(
                "SELECT key FROM rate_limits WHERE key LIKE '%device-invalid-code%'"
            )
        ]
    assert len(keys) == 2
    assert all(
        "设备码" not in response.text or response.status_code == 401
        for response in responses
    )


@pytest.mark.parametrize("phase", ["approve", "claim"])
@pytest.mark.parametrize(
    "change",
    [
        "permissions",
        "expiry",
        "name",
        "archive",
        "revoke",
        "product_name",
        "shop_name",
        "product_scope",
    ],
)
def test_scope_changes_invalidate_review_and_claim(owner, clients, phase, change):
    pid = product(owner)
    grant, _ = link(owner, pid, permissions=["queue.view", "queue.process"])
    cli = clients()
    value, key, _ = pending(cli)
    context = review(owner, value, grant)
    if phase == "claim":
        approve(owner, value, grant, context=context)
    with db() as c:
        if change == "permissions":
            c.execute(
                "UPDATE staff SET permissions=? WHERE id=?",
                ('["queue.view"]', grant["id"]),
            )
        elif change == "expiry":
            c.execute("UPDATE staff SET expires=expires-10 WHERE id=?", (grant["id"],))
        elif change == "name":
            c.execute(
                "UPDATE staff SET name='Changed authorization' WHERE id=?",
                (grant["id"],),
            )
        elif change == "archive":
            c.execute("UPDATE staff SET archived=1 WHERE id=?", (grant["id"],))
        elif change == "revoke":
            c.execute("UPDATE staff SET revoked=1 WHERE id=?", (grant["id"],))
        elif change == "product_name":
            c.execute(
                "UPDATE products SET config=json_set(config,'$.name','Changed title') WHERE id=?",
                (pid,),
            )
        elif change == "shop_name":
            c.execute(
                "UPDATE shops SET name='Changed shop' WHERE id=(SELECT shop_id FROM products WHERE id=?)",
                (pid,),
            )
        elif change == "product_scope":
            other = str(uuid.uuid4())
            c.execute(
                "INSERT INTO shops(id,name,created,verified) VALUES (?,?,?,1)",
                (other, "Other shop", time.time()),
            )
            c.execute("UPDATE products SET shop_id=? WHERE id=?", (other, pid))
    if phase == "claim":
        response = cli.post(CLI + "/claim", json=proof_body(value, key, "claim"))
    else:
        response = owner.post(
            BROWSER + "/approve",
            json={
                "user_code": value["user_code"],
                "staff_id": grant["id"],
                "review_digest": context["review_digest"],
            },
        )
    assert 400 <= response.status_code < 500, response.text
    assert quotas(grant["id"])[2] == 0
    with db() as c:
        assert c.execute("SELECT count(*) FROM cli_devices").fetchone()[0] == 0


def test_parent_expiry_and_reparenting_invalidate_snapshot(owner, clients):
    pid = product(owner)
    parent, _ = link(
        owner, pid, permissions=["queue.view", "queue.process", "links.delegate"]
    )
    child, _ = link(owner, pid)
    alternate, _ = link(owner, pid, permissions=parent["permissions"])
    with db() as c:
        c.execute(
            "UPDATE staff SET parent_id=? WHERE id=?", (parent["id"], child["id"])
        )
    cli = clients()
    value, key, _ = pending(cli)
    approve(owner, value, child)
    with db() as c:
        c.execute(
            "UPDATE staff SET parent_id=? WHERE id=?", (alternate["id"], child["id"])
        )
    assert (
        cli.post(CLI + "/claim", json=proof_body(value, key, "claim")).status_code
        == 401
    )
    with db() as c:
        c.execute(
            "UPDATE staff SET parent_id=? WHERE id=?", (parent["id"], child["id"])
        )
        c.execute(
            "UPDATE staff SET expires=? WHERE id=?", (time.time() - 1, parent["id"])
        )
    assert (
        cli.post(CLI + "/claim", json=proof_body(value, key, "claim")).status_code
        == 401
    )
    assert quotas(child["id"])[2] == 0


@pytest.mark.parametrize("change", ["revoked", "expired", "disabled_shop"])
def test_first_claim_requires_approving_browser_authority_to_remain_valid(
    owner, clients, change
):
    pid = product(owner)
    grant, _ = link(owner, pid)
    cli = clients()
    value, key, _ = pending(cli)
    approve(owner, value, grant)
    with db() as c:
        if change == "revoked":
            c.execute(
                "UPDATE sessions SET revoked=1 WHERE digest=?",
                (digest(owner.cookies.get("extore_session")),),
            )
        elif change == "expired":
            c.execute(
                "UPDATE sessions SET expires=? WHERE digest=?",
                (time.time() - 1, digest(owner.cookies.get("extore_session"))),
            )
        else:
            c.execute(
                "UPDATE shops SET enabled=0 WHERE id=(SELECT shop_id FROM products WHERE id=?)",
                (pid,),
            )
    assert (
        cli.post(CLI + "/claim", json=proof_body(value, key, "claim")).status_code
        == 401
    )
    assert quotas(grant["id"])[2] == 0


def test_owner_recent_authentication_is_required_for_approval(owner, clients):
    grant, _ = link(owner, product(owner))
    value, _, _ = pending(clients())
    context = review(owner, value, grant)
    with db() as c:
        c.execute(
            "UPDATE sessions SET auth_at=? WHERE digest=?",
            (time.time() - 601, digest(owner.cookies.get("extore_session"))),
        )
    response = owner.post(
        BROWSER + "/approve",
        json={
            "user_code": value["user_code"],
            "staff_id": grant["id"],
            "review_digest": context["review_digest"],
        },
    )
    assert response.status_code == 401
    assert _grant_state(value["request_id"])["state"] == "pending"


def test_browser_logout_after_binding_preserves_cli_but_revocation_does_not(
    owner, clients
):
    grant, _ = link(owner, product(owner))
    cli = clients()
    value, key, _ = pending(cli)
    approve(owner, value, grant)
    binding = claim(cli, value, key)
    assert owner.post("/api/auth/logout").status_code == 200
    recovered = claim(cli, value, key)
    assert recovered["device_id"] == binding["device_id"]
    assert recovered["already_authorized"] is True
    session = login(cli, recovered, key)
    with db() as c:
        c.execute(
            "UPDATE cli_devices SET revoked=1 WHERE id=?", (binding["device_id"],)
        )
    assert cli.get("/api/manage/products", headers=bearer(session)).status_code == 401
    assert (
        cli.post(CLI + "/claim", json=proof_body(value, key, "claim")).status_code
        == 401
    )
    assert (
        cli.post(
            "/api/cli/challenge", json={"device_id": binding["device_id"]}
        ).status_code
        == 401
    )


def test_same_key_zero_quota_recovery_after_expired_request_never_rebinds(
    owner, clients
):
    grant, _ = link(owner, product(owner))
    cli = clients()
    value, key, _ = pending(cli)
    approve(owner, value, grant)
    original = claim(cli, value, key)
    with db() as c:
        c.execute(
            "UPDATE cli_device_requests SET expires=? WHERE id=?",
            (time.time() - 1000, value["request_id"]),
        )
    fresh, _, _ = pending(cli, key)
    choice = review(owner, fresh, grant)["selected"]
    assert choice["remaining_cli_uses"] == 0
    assert choice["already_bound"] is True
    approve(owner, fresh, grant)
    recovered = claim(cli, fresh, key)
    assert recovered["device_id"] == original["device_id"]
    assert recovered["expires"] == original["expires"]
    assert quotas(grant["id"])[2] == 1
    with db() as c:
        assert c.execute("SELECT count(*) FROM cli_devices").fetchone()[0] == 1


def test_existing_revoked_key_is_not_eligible_for_new_code_approval(owner, clients):
    grant, token = link(owner, product(owner))
    cli = clients()
    original, key = authorize(cli, token)
    with db() as c:
        c.execute(
            "UPDATE cli_devices SET revoked=1 WHERE id=?", (original["device_id"],)
        )
    value, _, _ = pending(cli, key)
    assert review(owner, value)["candidates"] == []
    assert (
        owner.post(
            BROWSER + "/options",
            json={"user_code": value["user_code"], "staff_id": grant["id"]},
        ).status_code
        == 401
    )
    assert quotas(grant["id"])[2] == 1


def test_concurrent_claim_is_idempotent_and_two_keys_compete_for_one_quota(
    owner, clients
):
    grant, _ = link(owner, product(owner))
    first, first_key, _ = pending(clients())
    second, second_key, _ = pending(clients())
    approve(owner, first, grant)
    approve(owner, second, grant)
    barrier = Barrier(2)

    def run(value, key):
        with TestClient(app, base_url=ORIGIN) as client:
            barrier.wait(timeout=10)
            return client.post(CLI + "/claim", json=proof_body(value, key, "claim"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda args: run(*args), [(first, first_key), (second, second_key)]
            )
        )
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = 0 if responses[0].status_code == 200 else 1
    value, key = [(first, first_key), (second, second_key)][winner]
    with ThreadPoolExecutor(max_workers=2) as pool:
        replay = list(pool.map(lambda _: run(value, key), range(2)))
    assert [response.status_code for response in replay] == [200, 200]
    assert len({response.json()["device_id"] for response in replay}) == 1
    assert quotas(grant["id"])[2] == 1
    with db() as c:
        assert c.execute("SELECT count(*) FROM cli_devices").fetchone()[0] == 1


def test_tenant_owner_cannot_select_other_store_and_cli_cannot_approve(owner, clients):
    grant, _ = link(owner, product(owner))
    with db() as c:
        sid = str(uuid.uuid4())
        c.execute(
            "INSERT INTO shops(id,name,created,verified) VALUES (?,?,?,1)",
            (sid, "Other shop", time.time()),
        )
        cookie = create_session(
            c, Response(), "admin", shop_id=sid, auth_method="email_password"
        )
    merchant = clients(cookie=cookie)
    cli = clients()
    value, key, _ = pending(cli)
    assert review(merchant, value)["candidates"] == []
    assert (
        merchant.post(
            BROWSER + "/options",
            json={"user_code": value["user_code"], "staff_id": grant["id"]},
        ).status_code
        == 403
    )
    approve(owner, value, grant)
    binding = claim(cli, value, key)
    session = login(cli, binding, key)
    another, _, _ = pending(clients())
    assert (
        cli.post(
            BROWSER + "/options",
            json={"user_code": another["user_code"]},
            headers=bearer(session),
        ).status_code
        == 401
    )


def test_lookup_browser_csrf_and_unknown_validation_values_never_leak(owner, clients):
    value, _, _ = pending(clients())
    assert (
        owner.post(
            BROWSER + "/options",
            json={"user_code": value["user_code"]},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    response = owner.post(
        BROWSER + "/options",
        json={"user_code": value["user_code"], "token": "sensitive-synthetic-secret"},
    )
    assert response.status_code == 400
    assert "sensitive-synthetic-secret" not in response.text


def test_audit_is_scoped_and_never_serializes_handshake_credentials(owner, clients):
    grant, secret = link(owner, product(owner))
    browser = staff_browser(clients, secret)
    browser.headers["Origin"] = ORIGIN
    cli = clients()
    value, key, _ = pending(cli)
    approve(browser, value, grant)
    claim(cli, value, key)
    response = browser.get("/api/manage/audit")
    assert response.status_code == 200, response.text
    audit = response.json()
    assert {row["action"] for row in audit} >= {
        "cli.product.approve",
        "cli.product.claim",
        "link.cli_consume",
        "cli.device.create",
    }
    text = json.dumps(audit)
    assert value["user_code"] not in text
    assert value["challenge"] not in text
    assert secret not in text
    assert digest(browser.cookies.get("extore_session")) not in text
    metadata = [row for row in audit if row["action"] == "cli.product.approve"][0]
    assert metadata["fingerprint"] == value["fingerprint"]
    assert metadata["client_name"] == "Document Bot"


def test_additive_schema_migration_preserves_all_prior_tables_and_rows(owner):
    product(owner)
    init()
    with db() as c:
        c.execute("DROP TABLE cli_device_requests")
        c.execute("PRAGMA user_version=11")
        before = {
            row["name"]: {
                "ddl": row["sql"],
                "columns": [
                    tuple(column)
                    for column in c.execute(f'PRAGMA table_info("{row["name"]}")')
                ],
                "rows": [
                    tuple(record)
                    for record in c.execute(
                        f'SELECT * FROM "{row["name"]}" ORDER BY rowid'
                    )
                ],
            }
            for row in c.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    init()
    init()
    with db() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 15
        assert c.execute("SELECT count(*) FROM cli_device_requests").fetchone()[0] == 0
        for name, old in before.items():
            assert (
                c.execute(
                    "SELECT sql FROM sqlite_master WHERE name=?", (name,)
                ).fetchone()[0]
                == old["ddl"]
            )
            assert [
                tuple(column) for column in c.execute(f'PRAGMA table_info("{name}")')
            ] == old["columns"]
            assert [
                tuple(record)
                for record in c.execute(f'SELECT * FROM "{name}" ORDER BY rowid')
            ] == old["rows"]


def test_expired_request_cleanup_is_bounded_and_live_requests_are_untouched(
    owner, clients
):
    grant, _ = link(owner, product(owner))
    cli = clients()
    value, key, body = pending(cli)
    with db() as c:
        original = dict(
            c.execute(
                "SELECT * FROM cli_device_requests WHERE id=?", (value["request_id"],)
            ).fetchone()
        )
        names = list(original)
        for index in range(device_login.CLEANUP_BATCH + 7):
            old = {
                **original,
                "id": str(uuid.uuid4()),
                "nonce": b64(secrets.token_bytes(32)),
                "user_code": str(index),
                "expires": time.time() - 1000,
            }
            c.execute(
                f"INSERT INTO cli_device_requests ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                list(old.values()),
            )
        assert device_login.cleanup_requests(c) == device_login.CLEANUP_BATCH
        assert c.execute("SELECT count(*) FROM cli_device_requests").fetchone()[0] == 8
        assert (
            c.execute(
                "SELECT user_code FROM cli_device_requests WHERE id=?",
                (value["request_id"],),
            ).fetchone()[0]
            == value["user_code"]
        )
    assert quotas(grant["id"]) == (0, 1, 0, 1)
    assert (
        cli.post(CLI + "/request", json=body).json()["request_id"]
        == value["request_id"]
    )


def test_pending_requests_are_bounded_even_with_valid_keys(owner, clients, monkeypatch):
    monkeypatch.setattr(device_login, "MAX_REQUESTS", 2)
    cli = clients()
    pending(cli)
    pending(cli)
    response = cli.post(CLI + "/request", json=request_body(identity()))
    assert response.status_code == 429
    with db() as c:
        assert c.execute("SELECT count(*) FROM cli_device_requests").fetchone()[0] == 2


def test_request_nonce_clock_bounds_and_replay_after_retirement(clients):
    cli, key = clients(), identity()
    for issued in (int(time.time()) - 600, int(time.time()) + 61):
        nonce = b64(issued.to_bytes(8, "big") + secrets.token_bytes(24))
        assert (
            cli.post(CLI + "/request", json=request_body(key, nonce=nonce)).status_code
            == 401
        )
    future_nonce = b64(
        (int(time.time()) + 60).to_bytes(8, "big") + secrets.token_bytes(24)
    )
    future = cli.post(CLI + "/request", json=request_body(key, nonce=future_nonce))
    assert future.status_code == 200
    assert future.json()["expires"] <= time.time() + 600
    value, _, _ = pending(cli, key)
    retired_nonce = b64(
        (int(time.time()) - 1000).to_bytes(8, "big") + secrets.token_bytes(24)
    )
    with db() as c:
        c.execute(
            "UPDATE cli_device_requests SET nonce=?,expires=? WHERE id=?",
            (retired_nonce, time.time() - 1000, value["request_id"]),
        )
        assert device_login.cleanup_requests(c) == 1
    replay = cli.post(CLI + "/request", json=request_body(key, nonce=retired_nonce))
    assert replay.status_code == 401


def test_review_digest_is_bound_to_the_specific_browser_session(owner, clients):
    grant, _ = link(owner, product(owner))
    value, _, _ = pending(clients())
    context = review(owner, value, grant)
    with db() as c:
        cookie = create_session(c, Response(), "admin", auth_method="passkey")
    rotated = clients(cookie=cookie)
    assert review(rotated, value, grant)["review_digest"] != context["review_digest"]
    result = rotated.post(
        BROWSER + "/approve",
        json={
            "user_code": value["user_code"],
            "staff_id": grant["id"],
            "review_digest": context["review_digest"],
        },
    )
    assert result.status_code == 409
    assert _grant_state(value["request_id"])["state"] == "pending"
    assert quotas(grant["id"])[2] == 0


def test_requested_product_filter_is_enforced_at_review_and_claim(owner, clients):
    first_pid, second_pid = product(owner), product(owner)
    first, _ = link(owner, first_pid)
    second, _ = link(owner, second_pid)
    cli = clients()
    value, key, _ = pending(cli, pid=first_pid)
    choices = review(owner, value)
    assert {scope["product_id"] for scope in choices["candidates"]} == {first_pid}
    assert (
        owner.post(
            BROWSER + "/options",
            json={"user_code": value["user_code"], "staff_id": second["id"]},
        ).status_code
        == 403
    )
    approve(owner, value, first)
    assert claim(cli, value, key)["product_id"] == first_pid
    assert quotas(second["id"])[2] == 0


def test_staff_delegation_does_not_allow_binding_a_different_link(owner, clients):
    pid = product(owner)
    parent, secret = link(
        owner, pid, permissions=["queue.view", "queue.process", "links.delegate"]
    )
    child, _ = link(owner, pid)
    with db() as c:
        c.execute(
            "UPDATE staff SET parent_id=? WHERE id=?", (parent["id"], child["id"])
        )
    browser = staff_browser(clients, secret)
    browser.headers["Origin"] = ORIGIN
    value, _, _ = pending(clients())
    assert [scope["staff_id"] for scope in review(browser, value)["candidates"]] == [
        parent["id"]
    ]
    assert (
        browser.post(
            BROWSER + "/options",
            json={"user_code": value["user_code"], "staff_id": child["id"]},
        ).status_code
        == 403
    )
    assert quotas(child["id"])[2] == 0


def test_shop_password_reset_invalidates_unclaimed_approval(clients):
    from extore.account_auth import revoke_shop_auth

    with db() as c:
        sid = str(uuid.uuid4())
        c.execute(
            "INSERT INTO shops(id,name,created,verified) VALUES (?,?,?,1)",
            (sid, "Reset shop", time.time()),
        )
        cookie = create_session(
            c, Response(), "admin", shop_id=sid, auth_method="email_password"
        )
    merchant = clients(cookie=cookie)
    grant, _ = link(merchant, product(merchant))
    cli = clients()
    value, key, _ = pending(cli)
    approve(merchant, value, grant)
    with db() as c:
        revoke_shop_auth(c, sid)
    assert (
        cli.post(CLI + "/claim", json=proof_body(value, key, "claim")).status_code
        == 401
    )
    assert quotas(grant["id"])[2] == 0


def test_same_key_cannot_recover_an_expired_link_or_parent(owner, clients):
    pid = product(owner)
    parent, _ = link(owner, pid, permissions=["queue.view", "queue.process"])
    child, secret = link(owner, pid)
    with db() as c:
        c.execute(
            "UPDATE staff SET parent_id=? WHERE id=?", (parent["id"], child["id"])
        )
    cli = clients()
    _, key = authorize(cli, secret)
    value, _, _ = pending(cli, key)
    with db() as c:
        c.execute(
            "UPDATE staff SET expires=? WHERE id=?", (time.time() - 1, parent["id"])
        )
    assert review(owner, value)["candidates"] == []
    assert (
        owner.post(
            BROWSER + "/options",
            json={"user_code": value["user_code"], "staff_id": child["id"]},
        ).status_code
        == 401
    )
    assert quotas(child["id"])[2] == 1


def test_claimed_device_sessions_remain_bound_to_ancestor_revocation(owner, clients):
    pid = product(owner)
    parent, _ = link(owner, pid, permissions=["queue.view", "queue.process"])
    child, _ = link(owner, pid)
    with db() as c:
        c.execute(
            "UPDATE staff SET parent_id=? WHERE id=?", (parent["id"], child["id"])
        )
    cli = clients()
    value, key, _ = pending(cli)
    approve(owner, value, child)
    binding = claim(cli, value, key)
    session = login(cli, binding, key)
    with db() as c:
        c.execute("UPDATE staff SET revoked=1 WHERE id=?", (parent["id"],))
    assert cli.get("/api/manage/products", headers=bearer(session)).status_code == 401
    assert (
        cli.post(
            "/api/cli/challenge", json={"device_id": binding["device_id"]}
        ).status_code
        == 401
    )
    assert quotas(child["id"])[2] == 1


def test_nonce_key_pending_cap_is_per_identity_and_not_short_code(clients, monkeypatch):
    monkeypatch.setattr(device_login, "MAX_PENDING_PER_KEY", 1)
    cli, key = clients(), identity()
    pending(cli, key)
    assert cli.post(CLI + "/request", json=request_body(key)).status_code == 429
    assert cli.post(CLI + "/request", json=request_body(identity())).status_code == 200
    with db() as c:
        assert c.execute("SELECT count(*) FROM cli_device_requests").fetchone()[0] == 2


def test_binding_audit_survives_transient_request_cleanup(owner, clients):
    grant, _ = link(owner, product(owner))
    cli = clients()
    value, key, _ = pending(cli)
    approve(owner, value, grant)
    binding = claim(cli, value, key)
    with db() as c:
        c.execute(
            "UPDATE cli_device_requests SET expires=? WHERE id=?",
            (time.time() - 1000, value["request_id"]),
        )
        assert device_login.cleanup_requests(c) == 1
    response = owner.get("/api/admin/audit")
    assert response.status_code == 200, response.text
    records = response.json()
    decision = [record for record in records if record["action"] == "cli.product.claim"]
    assert len(decision) == 1
    assert decision[0]["target"] == binding["device_id"]
    assert decision[0]["actor"] == "owner"
    assert decision[0]["fingerprint"] == value["fingerprint"]
    assert decision[0]["client_name"] == "Document Bot"
    assert value["challenge"] not in json.dumps(records)

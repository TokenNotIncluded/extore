"""Actual Passkey and device signatures stay inside the reviewed shop scope."""

import base64
import json
import secrets
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response
from test_cli_auth import authorize, link, login, product
from test_owner_cli_auth import (
    APPROVAL,
    ROOT,
    action,
    action_headers,
    b64,
    bearer,
    claim,
    claim_body,
    keypair,
    owner_session,
    payload,
    pending,
    signed,
    status_body,
)
from test_passkeys import Authenticator, register

from extore import account_auth, totp
from extore.app import app
from extore.config import ORIGIN
from extore.db import db
from extore.owner_cli_auth import revoke_owner_devices
from extore.security import create_session, digest

PASSWORD = "synthetic-tenant-cli-password-19"


@pytest.fixture
def clients():
    opened = []

    def create(*, cookie=None):
        client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        if cookie:
            client.cookies.set("extore_session", cookie)
        opened.append(client)
        return client

    yield create
    for client in opened:
        client.close()


@pytest.fixture
def stores(clients):
    result = []
    hashed = account_auth.ph.hash(PASSWORD)
    with db() as c:
        for name in ("Store A", "Store B"):
            sid = str(uuid.uuid4())
            email = sid + "@example.test"
            c.execute(
                "INSERT INTO shops(id,name,email,password_hash,created,verified) VALUES (?,?,?,?,?,1)",
                (sid, name, email, hashed, time.time()),
            )
            cookie = create_session(
                c, Response(), "admin", shop_id=sid, auth_method="email_password"
            )
            result.append({"id": sid, "name": name, "email": email, "cookie": cookie})
    for store in result:
        store["browser"] = clients(cookie=store["cookie"])
    return result


@pytest.fixture
def identities(owner, stores):
    root = {"id": None, "name": "平台管理", "browser": owner}
    for store in [root, *stores]:
        store["passkey"] = Authenticator()
        register(store["browser"], store["passkey"], store["name"])
    return [root, *stores]


def review(store, request):
    result = store["browser"].post(
        APPROVAL + "/options",
        json={
            "request_id": request["request_id"],
            "device_code": request["device_code"],
        },
    )
    assert result.status_code == 200, result.text
    result = result.json()
    assert_authority(result, store)
    return result


def assert_authority(value, store):
    assert value["role"] == "admin"
    assert value["scope"] == "shop.owner"
    assert value["shop_id"] == store["id"]
    assert value["shop_name"] == store["name"]
    assert value["superadmin"] is (store["id"] is None)


def approve(store, request, *, reviewed=None, authenticator=None):
    context = reviewed or review(store, request)
    return store["browser"].post(
        APPROVAL + "/verify",
        json={
            "request_id": request["request_id"],
            "credential": (authenticator or store["passkey"]).assertion(
                context["options"]
            ),
        },
    )


def grant(store, client, key=None):
    request, key = pending(client, key)
    result = approve(store, request)
    assert result.status_code == 200, result.text
    device = claim(client, request, key)
    session = owner_session(client, device, key)
    assert_authority(device, store)
    assert_authority(session, store)
    return request, device, session, key


def email_request(client, store, key=None):
    key = key or keypair()
    name, nonce = "Private merchant CLI", b64(secrets.token_bytes(32))
    body = {
        "public_key": key[1],
        "client_name": name,
        "nonce": nonce,
        "target_email": store["email"],
        "signature": signed(
            key,
            f"extore-cli-owner-request-v2\n{ORIGIN}\n{key[1]}\n{name}\n{nonce}\n{store['email']}",
        ),
    }
    result = client.post(ROOT + "/request", json=body)
    assert result.status_code == 200, result.text
    assert "shop_id" not in result.json()
    assert "target_email" not in result.text
    return result.json(), key, body


def fresh_password(client, request, store, **factors):
    return client.post(
        APPROVAL + "/password-options",
        json={
            "request_id": request["request_id"],
            "device_code": request["device_code"],
            "email": store["email"],
            "password": PASSWORD,
            **factors,
        },
    )


def password_approval_body(request, context):
    return {
        "request_id": request["request_id"],
        "device_code": request["device_code"],
        "approval_token": context["approval_token"],
    }


def enable_totp(store):
    secret = totp.new_secret()
    raw, hashes = totp.generate_backup_codes(2)
    with db() as c:
        c.execute(
            "UPDATE shops SET totp_secret=?,totp_backup_digests=? WHERE id=?",
            (account_auth._seal(secret, store["id"]), json.dumps(hashes), store["id"]),
        )
    code = totp._code(base64.b32decode(secret), int(time.time() // 30))
    return code, raw


def test_reviewed_passkey_scope_rejects_other_shop_and_root_credentials(
    identities, clients
):
    root, first, second = identities
    client = clients()
    request, key = pending(client)
    context = review(first, request)
    assert {x["id"] for x in context["options"]["allowCredentials"]} == {
        first["passkey"].id
    }
    for other in (root, second):
        result = approve(
            first, request, reviewed=context, authenticator=other["passkey"]
        )
        assert result.status_code == 401, result.text
    assert approve(first, request, reviewed=context).status_code == 200
    status = client.post(ROOT + "/status", json=status_body(request, key))
    assert_authority(status.json(), first)
    device = claim(client, request, key)
    session = owner_session(client, device, key)
    assert_authority(device, first)
    assert_authority(session, first)
    assert_authority(
        client.get(ROOT + "/status", headers=bearer(session)).json(), first
    )


def test_scope_cannot_change_after_review_or_approval(identities, clients):
    _, first, second = identities
    client = clients()
    request, key = pending(client)
    context = review(first, request)
    assert (
        second["browser"]
        .post(
            APPROVAL + "/options",
            json={
                "request_id": request["request_id"],
                "device_code": request["device_code"],
            },
        )
        .status_code
        == 403
    )
    with db() as c:
        c.execute(
            "UPDATE owner_cli_requests SET shop_id=? WHERE id=?",
            (second["id"], request["request_id"]),
        )
    assert approve(first, request, reviewed=context).status_code in (401, 403)
    with db() as c:
        c.execute(
            "UPDATE owner_cli_requests SET shop_id=? WHERE id=?",
            (first["id"], request["request_id"]),
        )
    assert approve(first, request, reviewed=context).status_code == 200
    with db() as c:
        c.execute(
            "UPDATE owner_cli_requests SET shop_id=NULL WHERE id=?",
            (request["request_id"],),
        )
    assert (
        client.post(ROOT + "/claim", json=claim_body(request, key)).status_code == 401
    )
    assert (
        client.post(ROOT + "/status", json=status_body(request, key)).status_code == 401
    )


@pytest.mark.parametrize("initial,other", [(0, 1), (1, 0), (1, 2)])
def test_same_device_key_cannot_change_root_or_shop_scope(
    identities, clients, initial, other
):
    client = clients()
    _, device, session, key = grant(identities[initial], client)
    request, _ = pending(client, key)
    assert approve(identities[other], request).status_code == 200
    result = client.post(ROOT + "/claim", json=claim_body(request, key))
    assert result.status_code == 403, result.text
    assert_authority(
        client.get(ROOT + "/status", headers=bearer(session)).json(),
        identities[initial],
    )
    with db() as c:
        assert (
            c.execute(
                "SELECT shop_id FROM owner_cli_devices WHERE id=?",
                (device["device_id"],),
            ).fetchone()["shop_id"]
            == identities[initial]["id"]
        )


def test_owner_bearer_cannot_relabel_its_persisted_shop(identities, clients):
    _, first, second = identities
    client = clients()
    _, _, session, _ = grant(first, client)
    for sid in (None, second["id"]):
        with db() as c:
            c.execute(
                "UPDATE sessions SET shop_id=? WHERE digest=?",
                (sid, digest(session["access_token"])),
            )
        assert client.get(ROOT + "/status", headers=bearer(session)).status_code == 401


def test_device_listing_and_revoke_only_reach_the_current_shop(identities, clients):
    root, first, second = identities
    grants = [grant(store, clients()) for store in identities]
    devices = [x[1] for x in grants]
    assert {
        x["id"] for x in first["browser"].get("/api/admin/cli-owner-devices").json()
    } == {devices[1]["device_id"]}
    assert {
        x["id"] for x in root["browser"].get("/api/admin/cli-owner-devices").json()
    } == {x["device_id"] for x in devices}
    for other in (devices[0], devices[2]):
        assert (
            first["browser"]
            .delete("/api/admin/cli-owner-devices/" + other["device_id"])
            .status_code
            == 403
        )
    assert (
        first["browser"]
        .delete("/api/admin/cli-owner-devices/" + devices[1]["device_id"])
        .status_code
        == 200
    )
    for i in (0, 2):
        assert (
            clients().get(ROOT + "/status", headers=bearer(grants[i][2])).status_code
            == 200
        )
    assert (
        root["browser"]
        .delete("/api/admin/cli-owner-devices/" + devices[2]["device_id"])
        .status_code
        == 200
    )


def test_shop_reset_clears_own_devices_and_pending_proofs_only(identities, clients):
    root, first, second = identities
    grants = [grant(store, clients()) for store in identities]
    client = clients()
    request, key, _ = email_request(client, first)
    context = fresh_password(client, request, first).json()
    with db() as c:
        account_auth.revoke_shop_auth(c, first["id"])
        assert (
            c.execute(
                "SELECT 1 FROM owner_cli_password_challenges WHERE request_id=?",
                (request["request_id"],),
            ).fetchone()
            is None
        )
    assert client.post(
        APPROVAL + "/password-approve", json=password_approval_body(request, context)
    ).status_code in (401, 409)
    assert (
        client.post(ROOT + "/claim", json=claim_body(request, key)).status_code == 401
    )
    assert client.get(ROOT + "/status", headers=bearer(grants[1][2])).status_code == 401
    for i in (0, 2):
        assert (
            client.get(ROOT + "/status", headers=bearer(grants[i][2])).status_code
            == 200
        )
        assert (
            client.post(
                ROOT + "/challenge", json={"device_id": grants[i][1]["device_id"]}
            ).status_code
            == 200
        )


def test_root_device_requires_its_root_approval_credential(identities, clients):
    root, first, _ = identities
    client = clients()
    _, root_device, root_session, _ = grant(root, client)
    _, _, merchant_session, _ = grant(first, client)
    with db() as c:
        c.execute("DELETE FROM credentials WHERE shop_id IS NULL")
    assert (
        client.post(
            ROOT + "/challenge", json={"device_id": root_device["device_id"]}
        ).status_code
        == 401
    )
    assert client.get(ROOT + "/status", headers=bearer(root_session)).status_code == 401
    assert (
        client.get(ROOT + "/status", headers=bearer(merchant_session)).status_code
        == 200
    )


def test_explicit_root_device_reset_does_not_revoke_merchant_devices(
    identities, clients
):
    client = clients()
    grants = [grant(store, client) for store in identities]
    with db() as c:
        revoke_owner_devices(c, shop_id=None)
    assert client.get(ROOT + "/status", headers=bearer(grants[0][2])).status_code == 401
    for item in grants[1:]:
        assert client.get(ROOT + "/status", headers=bearer(item[2])).status_code == 200


def test_staff_devices_and_tickets_remain_product_scoped(stores, identities, clients):
    first, second = stores
    pid = product(first["browser"])
    management, token = link(first["browser"], pid)
    client = clients()
    device, key = authorize(client, token)
    session = login(client, device, key)
    assert device["shop_id"] == session["shop_id"] == first["id"]
    assert (
        second["browser"]
        .post("/api/manage/cli-ticket", json={"staff_id": management["id"]})
        .status_code
        == 403
    )
    ticket = first["browser"].post(
        "/api/manage/cli-ticket", json={"staff_id": management["id"]}
    )
    # A consumed one-device quota stays consumed across browser and CLI channels.
    assert ticket.status_code == 409
    request, _ = pending(client, key)
    assert (
        client.post(
            APPROVAL + "/options",
            json={
                "request_id": request["request_id"],
                "device_code": request["device_code"],
            },
            headers=bearer(session),
        ).status_code
        == 401
    )
    with db() as c:
        cookie = create_session(c, Response(), "staff", management["id"])
    browser = clients(cookie=cookie)
    assert (
        browser.post(
            APPROVAL + "/options",
            json={
                "request_id": request["request_id"],
                "device_code": request["device_code"],
            },
        ).status_code
        == 401
    )
    assert client.get("/api/admin/products", headers=bearer(session)).status_code == 401
    assert client.get(ROOT + "/status", headers=bearer(session)).status_code == 401
    with db() as c:
        c.execute("UPDATE shops SET enabled=0 WHERE id=?", (first["id"],))
    assert client.get("/api/cli/status", headers=bearer(session)).status_code == 401


def test_password_without_mfa_needs_fresh_factors_then_separate_approval(
    stores, clients
):
    first, _ = stores
    client = clients()
    request, key, _ = email_request(client, first)
    body = {"request_id": request["request_id"], "device_code": request["device_code"]}
    options = first["browser"].post(APPROVAL + "/options", json=body).json()
    assert options["options"] is None
    assert options["approval_methods"] == ["password"]
    assert options["mfa_required"] is False
    assert (
        first["browser"]
        .post(
            APPROVAL + "/password-approve",
            json={**body, "approval_token": b64(bytes(32))},
        )
        .status_code
        == 401
    )
    assert (
        client.post(ROOT + "/claim", json=claim_body(request, key)).status_code == 401
    )
    context = fresh_password(client, request, first)
    assert context.status_code == 200, context.text
    context = context.json()
    assert_authority(context, first)
    assert context["approval_methods"] == ["password"]
    assert context["mfa_required"] is False
    assert len(context["approval_token"]) == 43
    assert time.time() < context["approval_expires"] <= time.time() + 301
    assert (
        client.post(ROOT + "/claim", json=claim_body(request, key)).status_code == 401
    )
    result = client.post(
        APPROVAL + "/password-approve", json=password_approval_body(request, context)
    )
    assert result.status_code == 200, result.text
    assert_authority(result.json(), first)
    assert_authority(claim(client, request, key), first)
    assert (
        client.post(
            APPROVAL + "/password-approve",
            json=password_approval_body(request, context),
        ).status_code
        == 409
    )


def test_configured_mfa_is_mandatory_and_codes_are_single_use(stores, clients):
    first, _ = stores
    code, recovery = enable_totp(first)
    client = clients()
    request, _, _ = email_request(client, first)
    assert fresh_password(client, request, first).status_code == 401
    context = fresh_password(client, request, first, code=code)
    assert context.status_code == 200, context.text
    assert context.json()["approval_methods"] == ["password_totp"]
    assert context.json()["mfa_required"] is True
    assert fresh_password(client, request, first, code=code).status_code == 401
    other_request, _, _ = email_request(client, first)
    assert (
        fresh_password(
            client, other_request, first, backup_code=recovery[0]
        ).status_code
        == 200
    )
    assert (
        fresh_password(
            client, other_request, first, backup_code=recovery[0]
        ).status_code
        == 401
    )


def test_email_target_signature_and_actor_do_not_allow_other_shop(stores, clients):
    first, second = stores
    client = clients()
    request, _, signed_body = email_request(client, first)
    assert (
        client.post(
            ROOT + "/request", json={**signed_body, "target_email": second["email"]}
        ).status_code
        == 401
    )
    assert fresh_password(client, request, second).status_code == 401
    assert fresh_password(second["browser"], request, first).status_code == 403
    result = fresh_password(client, request, first)
    assert result.status_code == 200, result.text
    assert result.json()["shop_id"] is not None


@pytest.mark.parametrize("tamper", ["scope", "snapshot", "expiry", "request"])
def test_password_approval_proof_is_bound_and_expires(stores, clients, tamper):
    first, second = stores
    client = clients()
    request, key, _ = email_request(client, first)
    context = fresh_password(client, request, first).json()
    body = password_approval_body(request, context)
    with db() as c:
        if tamper == "scope":
            c.execute(
                "UPDATE owner_cli_requests SET shop_id=? WHERE id=?",
                (second["id"], request["request_id"]),
            )
        elif tamper == "snapshot":
            c.execute(
                "UPDATE owner_cli_requests SET client_name='changed' WHERE id=?",
                (request["request_id"],),
            )
        elif tamper == "expiry":
            c.execute(
                "UPDATE owner_cli_password_challenges SET expires=0 WHERE request_id=?",
                (request["request_id"],),
            )
    if tamper == "request":
        different, _, _ = email_request(client, first)
        body = password_approval_body(different, context)
    assert client.post(APPROVAL + "/password-approve", json=body).status_code == 401
    assert (
        client.post(ROOT + "/claim", json=claim_body(request, key)).status_code == 401
    )


def test_password_approval_requires_correct_origin(stores, clients):
    first, _ = stores
    client = clients()
    request, _, _ = email_request(client, first)
    assert fresh_password(client, request, first).status_code == 200
    with TestClient(app, base_url=ORIGIN) as untrusted:
        assert fresh_password(untrusted, request, first).status_code == 403
        assert (
            untrusted.post(
                APPROVAL + "/password-approve",
                json={
                    "request_id": request["request_id"],
                    "device_code": request["device_code"],
                    "approval_token": b64(bytes(32)),
                },
                headers={"Origin": "https://other.example.test"},
            ).status_code
            == 403
        )


def test_merchant_bearer_alone_and_reused_write_proof_cannot_mutate(stores, clients):
    first, _ = stores
    client = clients()
    request, key, _ = email_request(client, first)
    context = fresh_password(client, request, first).json()
    assert (
        client.post(
            APPROVAL + "/password-approve",
            json=password_approval_body(request, context),
        ).status_code
        == 200
    )
    device = claim(client, request, key)
    session = owner_session(client, device, key)
    path = "/api/admin/products"
    raw = payload({"name": "Signed merchant product", "parameters": []})
    assert (
        client.post(
            path,
            content=raw,
            headers={**bearer(session), "Content-Type": "application/json"},
        ).status_code
        == 401
    )
    nonce = action(client, session, "POST", path, raw)
    headers = action_headers(session, key, nonce, "POST", path, raw)
    result = client.post(path, content=raw, headers=headers)
    assert result.status_code == 200, result.text
    pid = result.json()["id"]
    with db() as c:
        assert (
            c.execute("SELECT shop_id FROM products WHERE id=?", (pid,)).fetchone()[
                "shop_id"
            ]
            == first["id"]
        )
    assert client.post(path, content=raw, headers=headers).status_code == 401


@pytest.mark.parametrize("change", ["enable", "disable", "backup-codes"])
@pytest.mark.parametrize("approved", [False, True])
def test_mfa_policy_change_invalidates_pending_approvals_only(
    identities, clients, change, approved
):
    _, first, second = identities
    client = clients()
    # Existing trusted devices continue to authenticate after MFA changes.
    _, _, trusted_session, _ = grant(first, client)
    old_factors, recovery = {}, []
    if change != "enable":
        code, recovery = enable_totp(first)
        old_factors = {"code": code}
    request, key, _ = email_request(client, first)
    context = fresh_password(client, request, first, **old_factors)
    assert context.status_code == 200, context.text
    context = context.json()
    old_body = password_approval_body(request, context)
    if approved:
        assert (
            client.post(APPROVAL + "/password-approve", json=old_body).status_code
            == 200
        )
    # A pending approval in the other shop must survive this shop's change.
    other, other_key, _ = email_request(client, second)
    other_context = fresh_password(client, other, second)
    assert other_context.status_code == 200, other_context.text
    if change == "enable":
        setup = first["browser"].post(
            "/api/auth/totp/setup", json={"password": PASSWORD}
        )
        assert setup.status_code == 200, setup.text
        secret = setup.json()["secret"]
        code = totp._code(base64.b32decode(secret), int(time.time() // 30))
        result = first["browser"].post("/api/auth/totp/confirm", json={"code": code})
        assert result.status_code == 200, result.text
        new_factors = {"backup_code": result.json()["backup_codes"][0]}
    else:
        result = first["browser"].post(
            "/api/auth/totp/" + change,
            json={"password": PASSWORD, "backup_code": recovery[0]},
        )
        assert result.status_code == 200, result.text
        new_factors = (
            {}
            if change == "disable"
            else {"backup_code": result.json()["backup_codes"][0]}
        )
    assert client.post(APPROVAL + "/password-approve", json=old_body).status_code == 409
    assert (
        client.post(ROOT + "/claim", json=claim_body(request, key)).status_code == 401
    )
    with db() as c:
        assert (
            c.execute(
                "SELECT state FROM owner_cli_requests WHERE id=?",
                (request["request_id"],),
            ).fetchone()["state"]
            == "denied"
        )
        assert (
            c.execute(
                "SELECT 1 FROM owner_cli_password_challenges WHERE request_id=?",
                (request["request_id"],),
            ).fetchone()
            is None
        )
    assert (
        client.get(ROOT + "/status", headers=bearer(trusted_session)).status_code == 200
    )
    assert (
        client.post(
            APPROVAL + "/password-approve",
            json=password_approval_body(other, other_context.json()),
        ).status_code
        == 200
    )
    assert_authority(claim(client, other, other_key), second)
    fresh, fresh_key, _ = email_request(client, first)
    fresh_context = fresh_password(client, fresh, first, **new_factors)
    assert fresh_context.status_code == 200, fresh_context.text
    assert (
        client.post(
            APPROVAL + "/password-approve",
            json=password_approval_body(fresh, fresh_context.json()),
        ).status_code
        == 200
    )
    assert_authority(claim(client, fresh, fresh_key), first)
    passkey_request, passkey_key = pending(client)
    assert approve(first, passkey_request).status_code == 200
    assert_authority(claim(client, passkey_request, passkey_key), first)

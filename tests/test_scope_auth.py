"""Signed scope requests through merchant review, claim, upgrade and revocation."""

import json
import secrets
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response
from test_cli_auth import b64, bearer, identity, login, product, staff_browser

from extore import pipeline_scopes as scopes
from extore.app import app
from extore.db import db, init
from extore.models import LINK_PERMISSIONS
from extore.security import create_session

ORIGIN = "http://localhost:8000"
CLI = "/api/cli/scopes"
BROWSER = "/api/manage/device"
QUEUE = ["queue.view", "queue.process", "queue.retry"]


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


def request_body(
    key,
    *,
    pid=None,
    kind="product",
    sid=None,
    pids=None,
    permissions=None,
    aid=None,
    revision=None,
    nonce=None,
    name="Document Bot",
    reason="",
    origin=ORIGIN,
):
    payload = {
        "public_key": key[1],
        "client_name": name,
        "nonce": nonce
        or b64(int(time.time()).to_bytes(8, "big") + secrets.token_bytes(24)),
        "kind": kind,
        "shop_id": sid,
        "product_ids": pids if pids is not None else ([pid] if pid else []),
        "permissions": permissions or QUEUE,
        "authorization_id": aid,
        "expected_revision": revision,
        "reason": reason,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    payload["signature"] = b64(
        key[0].sign(f"extore-cli-scope-request-v1\n{origin}\n{canonical}".encode())
    )
    return payload


def pending(client, key=None, **kwargs):
    key = key or identity()
    payload = request_body(key, **kwargs)
    response = client.post(CLI + "/request", json=payload)
    assert response.status_code == 200, response.text
    return response.json(), key, payload


def proof_body(value, key, action="status", *, origin=ORIGIN):
    proof = f"extore-cli-scope-{action}-v1\n{origin}\n{value['request_id']}\n"
    if action == "claim":
        proof += value["challenge"] + "\n"
    proof += key[1]
    return {
        "request_id": value["request_id"],
        "public_key": key[1],
        "signature": b64(key[0].sign(proof.encode())),
    }


def review(owner, value, **selection):
    response = owner.post(
        BROWSER + "/options", json={"user_code": value["user_code"], **selection}
    )
    assert response.status_code == 200, response.text
    return response.json()


def approve(owner, value, context=None, **selection):
    context = context or review(owner, value, **selection)
    response = owner.post(
        BROWSER + "/approve",
        json={
            "user_code": value["user_code"],
            "review_digest": context["review_digest"],
            **{
                k: context["selected"][k]
                for k in ("product_ids", "permissions", "expires")
            },
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "status": "approved"}
    return context


def claim(cli, value, key):
    response = cli.post(CLI + "/claim", json=proof_body(value, key, "claim"))
    assert response.status_code == 200, response.text
    return response.json()


def granted(owner, cli, *, key=None, **kwargs):
    value, key, _ = pending(cli, key, **kwargs)
    approve(owner, value)
    return claim(cli, value, key), key


def tenant(clients, name="Other store"):
    with db() as c:
        sid = str(uuid.uuid4())
        c.execute(
            "INSERT INTO shops(id,name,email,verified,created) VALUES (?,?,?,1,?)",
            (sid, name, sid + "@example.test", time.time()),
        )
        cookie = create_session(c, Response(), "admin", shop_id=sid)
    return clients(cookie=cookie), sid


def shop(pid):
    with db() as c:
        return c.execute("SELECT shop_id FROM products WHERE id=?", (pid,)).fetchone()[
            0
        ]


def test_product_device_code_full_real_flow_and_hidden_grant(owner, clients):
    pid, cli = product(owner), clients()
    value, key, _ = pending(cli, pid=pid)
    context = review(owner, value)
    assert context["flow"] == "scope" and context["current"] is None
    assert context["request"]["requested_product_ids"] == [pid]
    assert context["selected"]["permissions"] == QUEUE
    assert "public_key" not in json.dumps(context)
    approve(owner, value, context)
    result = claim(cli, value, key)
    auth, binding = result["authorization"], result["bindings"][0]
    assert auth["kind"] == "product" and auth["product_ids"] == [pid]
    assert (
        binding["authorization_id"] == auth["id"] and not binding["already_authorized"]
    )
    session = login(cli, binding, key)
    response = cli.get("/api/manage/products", headers=bearer(session))
    assert response.status_code == 200 and response.json()[0]["id"] == pid
    assert cli.get("/api/admin/products", headers=bearer(session)).status_code == 401
    with db() as c:
        staff = c.execute(
            "SELECT * FROM staff WHERE id=?", (binding["staff_id"],)
        ).fetchone()
        assert staff["max_uses"] == 0 and staff["uses"] == 0
        assert staff["max_cli_uses"] == 1 and staff["cli_uses"] == 1
    assert owner.get("/api/admin/staff").status_code == 200
    assert owner.get("/api/admin/staff").json() == []
    assert owner.post("/api/auth/logout").status_code == 200
    assert claim(cli, value, key)["bindings"][0]["device_id"] == binding["device_id"]


def test_pipeline_request_freezes_current_products_before_review(owner, clients):
    p1, p2, cli = product(owner), product(owner), clients()
    value, key, _ = pending(cli, kind="shop.pipeline", sid=shop(p1))
    later = product(owner)
    context = review(owner, value)
    assert set(context["request"]["requested_product_ids"]) == {p1, p2}
    assert later not in [p["id"] for p in context["products"]]
    assert (
        owner.post(
            BROWSER + "/options",
            json={"user_code": value["user_code"], "product_ids": [p1, later]},
        ).status_code
        == 403
    )
    selected = review(owner, value, product_ids=[p1], permissions=["queue.view"])
    approve(owner, value, selected)
    auth = claim(cli, value, key)["authorization"]
    assert auth["product_ids"] == [p1] and auth["permissions"] == ["queue.view"]


@pytest.mark.parametrize(
    "permission",
    [
        "product.edit",
        "fulfillment.configure",
        "cards.manage",
        "events.manage",
        "links.delegate",
        "owner.admin",
    ],
)
def test_all_pipeline_permission_ceiling(owner, clients, permission):
    pid, cli, key = product(owner), clients(), identity()
    payload = request_body(
        key, kind="shop.pipeline", sid=shop(pid), permissions=QUEUE + [permission]
    )
    assert cli.post(CLI + "/request", json=payload).status_code == 400
    with db() as c:
        assert (
            c.execute("SELECT count(*) FROM pipeline_authorizations").fetchone()[0] == 0
        )


def test_single_product_can_explicitly_request_management_permissions(owner, clients):
    pid, cli = product(owner), clients()
    result, key = granted(owner, cli, pid=pid, permissions=list(LINK_PERMISSIONS))
    assert result["authorization"]["permissions"] == list(LINK_PERMISSIONS)
    session = login(cli, result["bindings"][0], key)
    response = cli.get("/api/manage/product", headers=bearer(session))
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "CLI scope test"
    assert cli.get("/api/manage/products", headers=bearer(session)).status_code == 200


def test_upgrade_is_only_applied_at_signed_claim_and_keeps_task_actor(owner, clients):
    pid, cli = product(owner), clients()
    old, key = granted(owner, cli, pid=pid, permissions=["queue.view"])
    auth, binding = old["authorization"], old["bindings"][0]
    value, _, _ = pending(
        cli, key, pid=pid, aid=auth["id"], revision=auth["revision"], permissions=QUEUE
    )
    context = review(owner, value)
    assert context["current"]["permissions"] == ["queue.view"]
    approve(owner, value, context)
    with db() as c:
        assert scopes.authorization(c, auth["id"])["permissions"] == ["queue.view"]
    upgraded = claim(cli, value, key)
    assert upgraded["authorization"]["id"] == auth["id"]
    assert upgraded["authorization"]["revision"] == auth["revision"] + 1
    assert upgraded["authorization"]["expires"] == auth["expires"]
    assert upgraded["bindings"][0]["staff_id"] == binding["staff_id"]
    assert upgraded["bindings"][0]["device_id"] == binding["device_id"]
    assert upgraded["bindings"][0]["cli_uses"] == 1


def test_pipeline_added_product_requires_new_approval(owner, clients):
    p1, cli = product(owner), clients()
    old, key = granted(owner, cli, kind="shop.pipeline", sid=shop(p1))
    auth, old_actor = old["authorization"], old["bindings"][0]
    p2 = product(owner)
    value, _, _ = pending(
        cli, key, kind="shop.pipeline", sid=shop(p1), aid=auth["id"], revision=1
    )
    context = review(owner, value)
    assert context["current"]["product_ids"] == [p1]
    assert set(context["selected"]["product_ids"]) == {p1, p2}
    with db() as c:
        assert scopes.authorization(c, auth["id"])["product_ids"] == [p1]
    approve(owner, value)
    result = claim(cli, value, key)
    assert set(result["authorization"]["product_ids"]) == {p1, p2}
    assert (
        next(b for b in result["bindings"] if b["product_id"] == p1)["staff_id"]
        == old_actor["staff_id"]
    )


def test_upgrade_cannot_change_kind_products_or_reduce_permissions(owner, clients):
    p1, p2, cli = product(owner), product(owner), clients()
    old, key = granted(owner, cli, pid=p1)
    auth = old["authorization"]
    for fields in (
        {"pids": [p1, p2]},
        {"pid": p1, "kind": "shop.pipeline", "sid": shop(p1)},
        {"pid": p1, "permissions": ["queue.view"]},
    ):
        payload = request_body(key, aid=auth["id"], revision=1, **fields)
        assert cli.post(CLI + "/request", json=payload).status_code in (400, 403)


def test_restore_after_request_cleanup_preserves_expiry_and_actor(owner, clients):
    pid, cli = product(owner), clients()
    old, key = granted(owner, cli, pid=pid)
    auth, binding = old["authorization"], old["bindings"][0]
    with db() as c:
        c.execute("DELETE FROM cli_scope_requests")
    value, _, _ = pending(cli, key, pid=pid)
    context = review(owner, value)
    assert context["request"]["authorization_id"] is None
    assert context["current"]["id"] == auth["id"]
    assert context["selected"]["expires"] == auth["expires"]
    approve(owner, value, context)
    recovered = claim(cli, value, key)
    assert recovered["authorization"]["id"] == auth["id"]
    assert recovered["authorization"]["expires"] == auth["expires"]
    assert recovered["authorization"]["revision"] == auth["revision"]
    assert recovered["bindings"][0]["device_id"] == binding["device_id"]
    assert recovered["bindings"][0]["already_authorized"]


def test_lost_upgrade_claim_stale_revision_exact_target_recovers(owner, clients):
    pid, cli = product(owner), clients()
    old, key = granted(owner, cli, pid=pid, permissions=["queue.view"])
    auth = old["authorization"]
    value, _, _ = pending(
        cli, key, pid=pid, aid=auth["id"], revision=1, permissions=QUEUE
    )
    approve(owner, value)
    actual = claim(cli, value, key)
    with db() as c:
        c.execute("DELETE FROM cli_scope_requests")
    stale = request_body(
        key,
        pid=pid,
        aid=auth["id"],
        revision=1,
        permissions=["queue.view", "queue.process"],
    )
    assert cli.post(CLI + "/request", json=stale).status_code == 409
    fresh, _, _ = pending(
        cli, key, pid=pid, aid=auth["id"], revision=1, permissions=QUEUE
    )
    context = review(owner, fresh)
    assert context["current"]["revision"] == 2
    assert context["request"]["expected_revision"] == 2
    approve(owner, fresh, context)
    recovered = claim(cli, fresh, key)
    assert recovered["authorization"] == actual["authorization"]
    assert recovered["bindings"][0]["device_id"] == actual["bindings"][0]["device_id"]


def test_owner_tenant_boundary_and_staff_cannot_approve_active_scope(owner, clients):
    pid, cli = product(owner), clients()
    other, _ = tenant(clients)
    value, _, _ = pending(cli, pid=pid)
    assert (
        other.post(
            BROWSER + "/options", json={"user_code": value["user_code"]}
        ).status_code
        == 403
    )
    from test_cli_auth import link

    grant, secret = link(owner, pid)
    staff = staff_browser(clients, secret)
    assert (
        staff.post(
            BROWSER + "/options", json={"user_code": value["user_code"]}
        ).status_code
        == 403
    )
    assert grant["id"]


def test_safe_authorization_list_and_revision_checked_revoke(owner, clients):
    pid, cli = product(owner), clients()
    old, key = granted(owner, cli, pid=pid)
    auth = old["authorization"]
    response = owner.get("/api/admin/pipeline-authorizations")
    assert response.status_code == 200, response.text
    value = response.json()[0]
    assert value["id"] == auth["id"] and value["permissions"] == QUEUE
    assert value["product_ids"] == [pid] and value["bindings_count"] == 1
    assert value["issuer_role"] == "root" and value["issuer_shop_id"] is None
    assert value["revoked"] == 0 and "public_key" not in json.dumps(value)
    endpoint = "/api/admin/pipeline-authorizations/" + auth["id"]
    assert owner.request("DELETE", endpoint, json={}).status_code == 400
    assert (
        owner.request("DELETE", endpoint, json={"expected_revision": 2}).status_code
        == 409
    )
    with db() as c:
        assert scopes.authorization(c, auth["id"])["revision"] == 1
    other, _ = tenant(clients)
    assert other.get("/api/admin/pipeline-authorizations?view=all").json() == []
    assert (
        other.request("DELETE", endpoint, json={"expected_revision": 1}).status_code
        == 403
    )
    response = owner.request("DELETE", endpoint, json={"expected_revision": 1})
    assert response.status_code == 200, response.text
    assert response.json()["revoked_bindings"] == 1
    assert owner.get("/api/admin/pipeline-authorizations").json() == []
    assert (
        owner.get("/api/admin/pipeline-authorizations?view=revoked").json()[0][
            "revoked"
        ]
        == 1
    )
    assert (
        cli.post(
            "/api/cli/challenge", json={"device_id": old["bindings"][0]["device_id"]}
        ).status_code
        == 401
    )


def test_approval_logout_before_claim_cancels_pending_device(owner, clients):
    pid, cli = product(owner), clients()
    value, key, _ = pending(cli, pid=pid)
    approve(owner, value)
    assert owner.post("/api/auth/logout").status_code == 200
    assert (
        cli.post(CLI + "/claim", json=proof_body(value, key, "claim")).status_code
        == 401
    )
    with db() as c:
        assert (
            c.execute("SELECT count(*) FROM pipeline_authorizations").fetchone()[0] == 0
        )


def test_schema13_additive_migration_preserves_old39_tables(owner):
    pid = product(owner)
    with db() as c:
        # Remove later additions to reproduce an actual schema-11 database.
        for table in (
            "proxy_routes",
            "proxy_identities",
            "automation_requests",
            "private_worker_receipts",
            "private_worker_nonces",
            "task_flow_dispatches",
            "task_flow_files",
            "task_flow_steps",
            "task_flow_runs",
            "card_task_flows",
            "text_card_payloads",
            "product_purges",
            "product_lifecycle",
            "cli_scope_requests",
            "pipeline_bindings",
            "pipeline_authorizations",
            "cli_device_requests",
        ):
            c.execute("DROP TABLE " + table)
        c.execute("PRAGMA user_version=11")
        tables = c.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        assert len(tables) == 39
        before = {
            r["name"]: (
                r["sql"],
                [tuple(x) for x in c.execute('SELECT * FROM "' + r["name"] + '"')],
            )
            for r in tables
        }
    init()
    with db() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 16
        for name, (ddl, rows) in before.items():
            assert (
                c.execute(
                    "SELECT sql FROM sqlite_master WHERE name=?", (name,)
                ).fetchone()[0]
                == ddl
            )
            assert [tuple(x) for x in c.execute('SELECT * FROM "' + name + '"')] == rows
        assert c.execute("SELECT id FROM products WHERE id=?", (pid,)).fetchone()
        for table in (
            "cli_scope_requests",
            "pipeline_bindings",
            "pipeline_authorizations",
            "cli_device_requests",
        ):
            assert c.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0


def test_scope_history_does_not_expose_current_product_after_transfer(clients):
    owner, sid = tenant(clients, "Original merchant")
    other, target_sid = tenant(clients, "New merchant")
    pid, cli = product(owner), clients()
    value, _ = granted(owner, cli, pid=pid)
    with db() as c:
        config = json.loads(
            c.execute("SELECT config FROM products WHERE id=?", (pid,)).fetchone()[0]
        )
        config["name"] = "NEW MERCHANT PRIVATE PRODUCT NAME"
        c.execute(
            "UPDATE products SET shop_id=?,config=? WHERE id=?",
            (target_sid, json.dumps(config), pid),
        )
    response = owner.get("/api/admin/pipeline-authorizations?view=all")
    assert response.status_code == 200, response.text
    assert "NEW MERCHANT PRIVATE PRODUCT NAME" not in response.text
    assert response.json()[0]["products"] == [
        {"id": pid, "name": "商品已不可用", "mode": "unavailable"}
    ]
    assert response.json()[0]["shop_id"] == sid
    assert other.get("/api/admin/pipeline-authorizations?view=all").json() == []
    assert value["authorization"]["id"] == response.json()[0]["id"]


def test_real_signed_owner_cli_can_list_and_revision_revoke_scope(owner, clients):
    from test_owner_cli_auth import (
        APPROVAL,
        owner_session,
        write,
    )
    from test_owner_cli_auth import (
        claim as owner_claim,
    )
    from test_owner_cli_auth import (
        options as owner_options,
    )
    from test_owner_cli_auth import (
        pending as owner_pending,
    )
    from test_passkeys import Authenticator, register

    pid, bot = product(owner), clients()
    value, _ = granted(owner, bot, pid=pid)
    auth = value["authorization"]
    authenticator = Authenticator()
    register(owner, authenticator, "Owner scope management test")
    cli = clients()
    request, key = owner_pending(cli)
    context = owner_options(owner, request)
    response = owner.post(
        APPROVAL + "/verify",
        json={
            "request_id": request["request_id"],
            "credential": authenticator.assertion(context["options"]),
        },
    )
    assert response.status_code == 200, response.text
    device = owner_claim(cli, request, key)
    session = owner_session(cli, device, key)
    response = cli.get("/api/admin/pipeline-authorizations", headers=bearer(session))
    assert response.status_code == 200, response.text
    assert response.json()[0]["id"] == auth["id"]
    endpoint = "/api/admin/pipeline-authorizations/" + auth["id"]
    response = cli.request(
        "DELETE", endpoint, json={"expected_revision": 1}, headers=bearer(session)
    )
    assert response.status_code == 401
    response = write(cli, session, key, "DELETE", endpoint, {"expected_revision": 2})
    assert response.status_code == 409, response.text
    response = write(cli, session, key, "DELETE", endpoint, {"expected_revision": 1})
    assert response.status_code == 200, response.text
    assert response.json()["revoked_bindings"] == 1


def test_revision_revoke_prevents_revoking_newer_permission_scope(owner, clients):
    pid, cli = product(owner), clients()
    old, key = granted(owner, cli, pid=pid, permissions=["queue.view"])
    auth = old["authorization"]
    value, _, _ = pending(cli, key, pid=pid, aid=auth["id"], revision=1)
    approve(owner, value)
    upgraded = claim(cli, value, key)
    assert upgraded["authorization"]["revision"] == 2
    response = owner.request(
        "DELETE",
        "/api/admin/pipeline-authorizations/" + auth["id"],
        json={"expected_revision": 1},
    )
    assert response.status_code == 409
    with db() as c:
        assert (
            c.execute(
                "SELECT revoked FROM pipeline_authorizations WHERE id=?", (auth["id"],)
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT revoked FROM cli_devices WHERE id=?",
                (old["bindings"][0]["device_id"],),
            ).fetchone()[0]
            == 0
        )
    assert scopes.PIPELINE_PERMISSIONS

"""Live ACL checks for derived device scopes, delegation and account recovery."""

import base64
import hashlib
import json
import time
import uuid
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response
from test_cli_auth import authorize, bearer, identity, link, login, product

from extore.account_auth import revoke_shop_auth
from extore.app import app
from extore.db import audit, db
from extore.models import LINK_PERMISSIONS
from extore.pipeline_scopes import materialize, revoke_authorization
from extore.security import create_session, digest

ORIGIN = "http://localhost:8000"
QUEUE = ["queue.view", "queue.process", "queue.retry"]


@pytest.fixture
def clients():
    with ExitStack() as stack:

        def create(*, cookie=None):
            headers = {"Origin": ORIGIN} if cookie else {}
            client = stack.enter_context(
                TestClient(app, base_url=ORIGIN, headers=headers)
            )
            if cookie:
                client.cookies.set("extore_session", cookie)
            return client

        yield create


@pytest.fixture
def stores(clients):
    result = []
    with db() as c:
        for name in ("Scope Store A", "Scope Store B"):
            sid = str(uuid.uuid4())
            c.execute(
                "INSERT INTO shops(id,name,email,created,verified) VALUES (?,?,?,?,1)",
                (sid, name, sid + "@example.test", time.time()),
            )
            cookie = create_session(c, Response(), "admin", shop_id=sid)
            result.append({"id": sid, "cookie": cookie})
    for store in result:
        store["browser"] = clients(cookie=store.pop("cookie"))
    return result


def scope(
    store, *, pids=None, permissions=None, kind="product", issuer="shop", key=None
):
    key = key or identity()
    pids = pids or [product(store["browser"])]
    raw = base64.urlsafe_b64decode(key[1] + "=")
    with db() as c:
        value = materialize(
            c,
            {
                "shop_id": store["id"],
                "kind": kind,
                "product_ids": pids,
                "permissions": permissions or QUEUE,
            },
            key[1],
            "Synthetic pipeline Bot",
            hashlib.sha256(raw).hexdigest(),
            "owner" if issuer == "root" else "shop:" + store["id"],
            issuer_role=issuer,
            issuer_shop_id=None if issuer == "root" else store["id"],
        )
    return value, key


def bound(clients, value, key, index=0):
    client = clients()
    binding = value["bindings"][index]
    authenticated = login(client, binding, key)
    client.headers.update(bearer(authenticated))
    return client, binding, authenticated


def delegate(client, pid, permissions, **kwargs):
    response = client.post(
        "/api/manage/links",
        json={
            "product_id": pid,
            "name": "Synthetic delegated operator",
            "permissions": permissions,
            **kwargs,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def browser_link(clients, value):
    client = clients()
    response = client.post(
        "/api/staff/login",
        json={"token": value["url"].split("#", 1)[1]},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200, response.text
    client.headers["Origin"] = ORIGIN
    return client


def queued(store, pid):
    response = store["browser"].post(
        "/api/admin/cards", json={"product_id": pid, "count": 1}
    )
    assert response.status_code == 200, response.text
    response = store["browser"].post(
        "/api/exchange", json={"code": response.json()["codes"][0]}
    )
    assert response.status_code == 200, response.text
    response = store["browser"].post(
        "/api/redeem", json={"token": response.json()["token"], "params": {}}
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def claim(client, binding, job_id):
    response = client.post(
        "/api/manage/batch",
        json={"product_id": binding["product_id"], "ids": [job_id], "action": "claim"},
    )
    assert response.status_code == 200, response.text


def test_scope_metadata_and_hidden_materializations_do_not_become_invitation_links(
    owner, stores, clients
):
    value, key = scope(stores[0], permissions=list(LINK_PERMISSIONS))
    cli, binding, authenticated = bound(clients, value, key)
    pid = binding["product_id"]
    status = cli.get("/api/cli/status")
    assert status.status_code == 200, status.text
    assert status.json()["authorization_id"] == value["authorization"]["id"]
    assert status.json()["authorization_revision"] == 1
    assert status.json()["scope"] == "product"
    assert authenticated["authorization_id"] == value["authorization"]["id"]
    assert authenticated["authorization_revision"] == 1
    regular, _ = link(stores[0]["browser"], pid)
    for admin in (owner, stores[0]["browser"]):
        rows = admin.get("/api/admin/staff?view=all").json()
        assert binding["staff_id"] not in {row["id"] for row in rows}
        assert regular["id"] in {row["id"] for row in rows}
    rows = (
        stores[0]["browser"]
        .get("/api/manage/links", params={"product_id": pid, "view": "all"})
        .json()
    )
    assert binding["staff_id"] not in {row["id"] for row in rows}
    assert (
        stores[0]["browser"]
        .post("/api/admin/staff/" + binding["staff_id"] + "/revoke")
        .status_code
        == 404
    )
    with db() as c:
        hidden = c.execute(
            "SELECT * FROM staff WHERE id=?", (binding["staff_id"],)
        ).fetchone()
        assert (
            hidden["max_uses"],
            hidden["uses"],
            hidden["max_cli_uses"],
            hidden["cli_uses"],
        ) == (0, 0, 1, 1)
        c.execute(
            "UPDATE staff SET digest=? WHERE id=?",
            (digest("synthetic-hidden-browser-proof"), binding["staff_id"]),
        )
    response = clients().post(
        "/api/staff/login",
        json={"token": "synthetic-hidden-browser-proof"},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 401


def test_scope_delegation_supports_child_and_grandchild_without_increasing_permissions(
    stores, clients
):
    value, key = scope(stores[0], permissions=list(LINK_PERMISSIONS))
    cli, binding, _ = bound(clients, value, key)
    pid = binding["product_id"]
    child = delegate(cli, pid, ["queue.view", "queue.process", "links.delegate"])
    child_browser = browser_link(clients, child)
    grandchild = delegate(child_browser, pid, ["queue.view", "queue.process"])
    grandchild_browser = browser_link(clients, grandchild)
    assert child["parent_id"] == binding["staff_id"]
    assert grandchild["parent_id"] == child["id"]
    assert child["max_uses"] == grandchild["max_uses"] == 1
    for invalid in (
        {"permissions": list(LINK_PERMISSIONS)},
        {"permissions": ["queue.view"], "max_uses": 2},
        {"permissions": ["queue.view"], "max_cli_uses": 2},
        {"permissions": ["queue.view"], "days": 8},
    ):
        response = cli.post(
            "/api/manage/links",
            json={"product_id": pid, "name": "Invalid expansion", **invalid},
        )
        assert response.status_code == 403, response.text
    for operator in (cli, child_browser, grandchild_browser):
        assert (
            operator.get("/api/manage/jobs", params={"product_id": pid}).status_code
            == 200
        )
    with db() as c:
        assert (
            c.execute(
                "SELECT max_uses FROM staff WHERE id=?", (binding["staff_id"],)
            ).fetchone()[0]
            == 0
        )
        revoke_authorization(c, value["authorization"]["id"], "owner")
    for operator in (cli, child_browser, grandchild_browser):
        assert (
            operator.get("/api/manage/jobs", params={"product_id": pid}).status_code
            == 401
        )
    assert (
        clients()
        .post("/api/cli/challenge", json={"device_id": binding["device_id"]})
        .status_code
        == 401
    )


@pytest.mark.parametrize(
    "change", ("move", "expire", "revoke", "permissions", "device")
)
def test_descendant_browser_sessions_recheck_each_scope_ancestor(
    stores, clients, change
):
    value, key = scope(stores[0], permissions=list(LINK_PERMISSIONS))
    cli, binding, _ = bound(clients, value, key)
    pid = binding["product_id"]
    child = delegate(cli, pid, ["queue.view", "queue.process", "links.delegate"])
    child_browser = browser_link(clients, child)
    grandchild = delegate(child_browser, pid, ["queue.view", "queue.process"])
    grandchild_browser = browser_link(clients, grandchild)
    with db() as c:
        if change == "move":
            c.execute(
                "UPDATE products SET shop_id=? WHERE id=?", (stores[1]["id"], pid)
            )
        elif change == "device":
            c.execute(
                "UPDATE cli_devices SET revoked=1 WHERE id=?", (binding["device_id"],)
            )
        else:
            column, new_value = {
                "expire": ("expires", time.time() - 1),
                "revoke": ("revoked", 1),
                "permissions": ("permissions", '["queue.view"]'),
            }[change]
            c.execute(
                f"UPDATE pipeline_authorizations SET {column}=? WHERE id=?",
                (new_value, value["authorization"]["id"]),
            )
    for operator in (cli, child_browser, grandchild_browser):
        assert operator.get(
            "/api/manage/jobs", params={"product_id": pid}
        ).status_code in (401, 403)


@pytest.mark.parametrize(
    "change", ("move", "mode", "expire", "revoke", "permissions", "key")
)
def test_stale_bearer_and_refresh_recheck_scope_boundaries(stores, clients, change):
    value, key = scope(stores[0], kind="shop.pipeline")
    cli, binding, _ = bound(clients, value, key)
    pid = binding["product_id"]
    assert cli.get("/api/manage/jobs", params={"product_id": pid}).status_code == 200
    with db() as c:
        if change == "move":
            c.execute(
                "UPDATE products SET shop_id=? WHERE id=?", (stores[1]["id"], pid)
            )
        elif change == "mode":
            c.execute(
                "UPDATE products SET config=json_set(config,'$.mode','webhook') WHERE id=?",
                (pid,),
            )
        elif change in ("expire", "revoke"):
            column, value_change = (
                ("expires", time.time() - 1) if change == "expire" else ("revoked", 1)
            )
            c.execute(
                f"UPDATE pipeline_authorizations SET {column}=? WHERE id=?",
                (value_change, value["authorization"]["id"]),
            )
        elif change == "permissions":
            c.execute(
                "UPDATE staff SET permissions=? WHERE id=?",
                (json.dumps(list(LINK_PERMISSIONS)), binding["staff_id"]),
            )
        else:
            c.execute(
                "UPDATE cli_devices SET public_key=? WHERE id=?",
                (identity()[1], binding["device_id"]),
            )
    assert cli.get("/api/manage/jobs", params={"product_id": pid}).status_code in (
        401,
        403,
    )
    assert cli.get("/api/cli/status").status_code in (401, 403)
    assert clients().post(
        "/api/cli/challenge", json={"device_id": binding["device_id"]}
    ).status_code in (401, 403)
    devices = stores[0]["browser"].get("/api/admin/cli-devices").json()
    if change != "move":
        assert (
            next(row for row in devices if row["id"] == binding["device_id"])["active"]
            is False
        )
    sessions = stores[0]["browser"].get("/api/admin/sessions").json()
    if change != "move":
        assert (
            next(row for row in sessions if row["device_id"] == binding["device_id"])[
                "active"
            ]
            is False
        )


def test_pipeline_snapshot_does_not_admit_new_products_or_other_shop_queues(
    stores, clients
):
    pids = [product(stores[0]["browser"]) for _ in range(2)]
    value, key = scope(stores[0], pids=pids, kind="shop.pipeline")
    first, first_binding, _ = bound(clients, value, key, 0)
    second, second_binding, _ = bound(clients, value, key, 1)
    new_pid, foreign_pid = product(stores[0]["browser"]), product(stores[1]["browser"])
    for operator, binding in ((first, first_binding), (second, second_binding)):
        assert (
            operator.get(
                "/api/manage/jobs", params={"product_id": binding["product_id"]}
            ).status_code
            == 200
        )
        assert (
            operator.get("/api/manage/jobs", params={"product_id": new_pid}).status_code
            == 403
        )
        assert (
            operator.get(
                "/api/manage/jobs", params={"product_id": foreign_pid}
            ).status_code
            == 403
        )
    assert (
        first.get(
            "/api/manage/jobs", params={"product_id": second_binding["product_id"]}
        ).status_code
        == 403
    )


def test_upgrade_keeps_actor_device_lease_and_expiry_while_old_bearer_refreshes_permissions(
    stores, clients
):
    value, key = scope(stores[0], permissions=["queue.view", "queue.process"])
    cli, binding, _ = bound(clients, value, key)
    job_id = queued(stores[0], binding["product_id"])
    claim(cli, binding, job_id)
    with db() as c:
        upgraded = materialize(
            c,
            {
                "shop_id": stores[0]["id"],
                "kind": "product",
                "product_ids": [binding["product_id"]],
                "permissions": QUEUE,
            },
            key[1],
            "Synthetic pipeline Bot",
            binding["fingerprint"],
            "shop:" + stores[0]["id"],
            existing_authorization_id=value["authorization"]["id"],
            expected_revision=1,
            issuer_role="shop",
            issuer_shop_id=stores[0]["id"],
        )
        job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert job["claimed_by"] == binding["staff_id"]
        assert job["state"] == "processing"
        assert job["lease"] > time.time()
    assert upgraded["bindings"][0]["staff_id"] == binding["staff_id"]
    assert upgraded["bindings"][0]["device_id"] == binding["device_id"]
    assert upgraded["authorization"]["expires"] == value["authorization"]["expires"]
    status = cli.get("/api/cli/status").json()
    assert status["authorization_revision"] == 2
    assert status["permissions"] == QUEUE


def test_revocation_releases_hidden_and_descendant_tasks_without_losing_drafts(
    stores, clients
):
    value, key = scope(stores[0], permissions=list(LINK_PERMISSIONS))
    cli, binding, _ = bound(clients, value, key)
    child = delegate(cli, binding["product_id"], ["queue.view", "queue.process"])
    child_browser = browser_link(clients, child)
    first_job, second_job = [queued(stores[0], binding["product_id"]) for _ in range(2)]
    claim(cli, binding, first_job)
    claim(child_browser, binding, second_job)
    with db() as c:
        for job_id in (first_job, second_job):
            c.execute(
                "UPDATE jobs SET progress=37,result_json=? WHERE id=?",
                ('{"draft":"keep"}', job_id),
            )
        result = revoke_authorization(c, value["authorization"]["id"], "owner")
        assert result["jobs"] == 2
        for job_id in (first_job, second_job):
            job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            assert (job["state"], job["claimed_by"], job["lease"]) == (
                "queued",
                None,
                None,
            )
            assert job["progress"] == 37
            assert job["result_json"] == '{"draft":"keep"}'
            assert json.loads(job["params"]) == {}
        assert (
            c.execute(
                "SELECT revoked FROM staff WHERE id=?", (child["id"],)
            ).fetchone()[0]
            == 1
        )


def test_shop_reset_revokes_all_shop_scopes_and_preserves_other_shop(stores, clients):
    issued = [
        (store, issuer, *scope(store, issuer=issuer))
        for store in stores
        for issuer in ("shop", "root")
    ]
    sessions = [
        (store, issuer, value, *bound(clients, value, key))
        for store, issuer, value, key in issued
    ]
    with db() as c:
        revoke_shop_auth(c, stores[0]["id"])
    for store, _, value, cli, binding, _ in sessions:
        expected = 401 if store["id"] == stores[0]["id"] else 200
        assert cli.get("/api/cli/status").status_code == expected
        with db() as c:
            assert bool(
                c.execute(
                    "SELECT revoked FROM pipeline_authorizations WHERE id=?",
                    (value["authorization"]["id"],),
                ).fetchone()[0]
            ) == (expected == 401)
        assert (
            clients()
            .post("/api/cli/challenge", json={"device_id": binding["device_id"]})
            .status_code
            == expected
        )
    assert stores[0]["browser"].get("/api/shop/account").status_code == 401
    assert stores[1]["browser"].get("/api/shop/account").status_code == 200


def test_root_reset_revokes_root_issued_scopes_only(owner, stores, clients):
    issued = [
        (store, issuer, *scope(store, issuer=issuer))
        for store in stores
        for issuer in ("shop", "root")
    ]
    sessions = [
        (issuer, *bound(clients, value, key)) for _, issuer, value, key in issued
    ]
    with db() as c:
        revoke_shop_auth(c, None)
    assert owner.get("/api/platform/shops").status_code == 401
    for issuer, cli, binding, _ in sessions:
        expected = 401 if issuer == "root" else 200
        assert cli.get("/api/cli/status").status_code == expected
        assert (
            clients()
            .post("/api/cli/challenge", json={"device_id": binding["device_id"]})
            .status_code
            == expected
        )
    for store in stores:
        assert store["browser"].get("/api/shop/account").status_code == 200


def test_scope_audit_targets_are_opaque_and_visible_only_to_the_correct_shop(
    owner, stores, clients
):
    values = [scope(store, issuer="root")[0] for store in stores]
    with db() as c:
        audit(c, "owner", "cli.scope.create", "arbitrary-private-target")
        rows = c.execute(
            "SELECT shop_id,target FROM audit WHERE action='cli.scope.create'"
        ).fetchall()
        for store, value in zip(stores, values, strict=True):
            assert (
                next(
                    row for row in rows if row["target"] == value["authorization"]["id"]
                )["shop_id"]
                == store["id"]
            )
    for store, value in zip(stores, values, strict=True):
        rows = store["browser"].get("/api/admin/audit").json()
        scope_targets = {
            row["target"] for row in rows if row["action"] == "cli.scope.create"
        }
        assert scope_targets == {value["authorization"]["id"]}
        assert all("public_key" not in row and "draft" not in row for row in rows)
    rows = owner.get("/api/admin/audit").json()
    scope_targets = {
        row["target"] for row in rows if row["action"] == "cli.scope.create"
    }
    assert scope_targets == {value["authorization"]["id"] for value in values}


def test_legacy_product_device_stays_usable_and_has_no_scope_metadata(stores, clients):
    pid = product(stores[0]["browser"])
    invitation, secret = link(stores[0]["browser"], pid)
    cli = clients()
    device, key = authorize(cli, secret)
    authenticated = login(cli, device, key)
    cli.headers.update(bearer(authenticated))
    status = cli.get("/api/cli/status")
    assert status.status_code == 200, status.text
    assert status.json()["link_id"] == invitation["id"]
    assert "authorization_id" not in status.json()
    assert "authorization_revision" not in authenticated


def test_revoking_a_mapped_device_releases_every_descendant_job_and_session(
    stores, clients
):
    value, key = scope(stores[0], permissions=list(LINK_PERMISSIONS))
    cli, binding, _ = bound(clients, value, key)
    child = delegate(cli, binding["product_id"], [*QUEUE, "links.delegate"])
    child_browser = browser_link(clients, child)
    grandchild = delegate(child_browser, binding["product_id"], QUEUE)
    grandchild_browser = browser_link(clients, grandchild)
    child_cli = clients()
    child_device, child_key = authorize(child_cli, child["url"].split("#", 1)[1])
    child_cli.headers.update(bearer(login(child_cli, child_device, child_key)))
    job_ids = [queued(stores[0], binding["product_id"]) for _ in range(3)]
    for operator, job_id in zip(
        (cli, child_cli, grandchild_browser), job_ids, strict=True
    ):
        claim(operator, binding, job_id)
    response = stores[0]["browser"].delete(
        "/api/admin/cli-devices/" + binding["device_id"]
    )
    assert response.status_code == 200, response.text
    assert response.json()["authorization_id"] == value["authorization"]["id"]
    assert response.json()["released_jobs"] == 3
    assert response.json()["revoked_sessions"] == 4
    for operator in (cli, child_cli, child_browser, grandchild_browser):
        assert (
            operator.get(
                "/api/manage/jobs", params={"product_id": binding["product_id"]}
            ).status_code
            == 401
        )
    with db() as c:
        for job_id in job_ids:
            job = c.execute(
                "SELECT state,claimed_by,lease FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            assert tuple(job) == ("queued", None, None)


def test_device_revoke_keeps_the_original_shop_boundary_after_product_move(
    stores, clients
):
    pids = [product(stores[0]["browser"]) for _ in range(2)]
    value, key = scope(stores[0], pids=pids, kind="shop.pipeline")
    first, binding, _ = bound(clients, value, key)
    second, other_binding, _ = bound(clients, value, key, 1)
    job_id = queued(stores[0], other_binding["product_id"])
    claim(second, other_binding, job_id)
    with db() as c:
        c.execute(
            "UPDATE products SET shop_id=? WHERE id=?",
            (stores[1]["id"], binding["product_id"]),
        )
    assert (
        stores[1]["browser"]
        .delete("/api/admin/cli-devices/" + binding["device_id"])
        .status_code
        == 404
    )
    assert second.get("/api/cli/status").status_code == 200
    response = stores[0]["browser"].delete(
        "/api/admin/cli-devices/" + binding["device_id"]
    )
    assert response.status_code == 200, response.text
    assert response.json()["released_jobs"] == 1
    assert first.get("/api/cli/status").status_code == 401
    assert second.get("/api/cli/status").status_code == 401
    with db() as c:
        assert (
            c.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
            == "queued"
        )


def test_product_move_does_not_expose_scope_descendant_devices_sessions_or_audit_to_new_shop(
    stores, clients
):
    value, key = scope(stores[0], permissions=list(LINK_PERMISSIONS))
    cli, binding, _ = bound(clients, value, key)
    child = delegate(cli, binding["product_id"], [*QUEUE, "links.delegate"])
    child_browser = browser_link(clients, child)
    grandchild = delegate(child_browser, binding["product_id"], QUEUE)
    browser_link(clients, grandchild)
    child_cli = clients()
    child_device, child_key = authorize(child_cli, child["url"].split("#", 1)[1])
    child_cli.headers.update(bearer(login(child_cli, child_device, child_key)))
    with db() as c:
        c.execute(
            "UPDATE products SET shop_id=?,config=json_set(config,'$.name','New private shop name') WHERE id=?",
            (stores[1]["id"], binding["product_id"]),
        )
    original = stores[0]["browser"]
    foreign = stores[1]["browser"]
    original_devices = original.get("/api/admin/cli-devices").json()
    original_sessions = original.get("/api/admin/sessions").json()
    assert {row["id"] for row in original_devices} == {
        binding["device_id"],
        child_device["device_id"],
    }
    assert {row["link_id"] for row in original_sessions if row["role"] == "staff"} == {
        binding["staff_id"],
        child["id"],
        grandchild["id"],
    }
    assert all(row["product_name"] is None for row in original_devices)
    assert all(
        row["product_name"] is None
        for row in original_sessions
        if row["role"] == "staff"
    )
    assert foreign.get("/api/admin/cli-devices").json() == []
    assert not [
        row
        for row in foreign.get("/api/admin/sessions").json()
        if row["role"] == "staff"
    ]
    target = value["authorization"]["id"]
    assert target in {row["target"] for row in original.get("/api/admin/audit").json()}
    assert target not in {
        row["target"] for row in foreign.get("/api/admin/audit").json()
    }


@pytest.mark.parametrize(
    ("original_issuer", "latest_issuer"), (("shop", "root"), ("root", "shop"))
)
def test_root_reset_uses_latest_complete_reapproval_issuer(
    stores, clients, original_issuer, latest_issuer
):
    value, key = scope(stores[0], issuer=original_issuer)
    cli, binding, _ = bound(clients, value, key)
    with db() as c:
        approved = materialize(
            c,
            {
                "shop_id": stores[0]["id"],
                "kind": "product",
                "product_ids": [binding["product_id"]],
                "permissions": QUEUE,
            },
            key[1],
            "Synthetic pipeline Bot",
            binding["fingerprint"],
            "owner" if latest_issuer == "root" else "shop:" + stores[0]["id"],
            existing_authorization_id=value["authorization"]["id"],
            expected_revision=1,
            issuer_role=latest_issuer,
            issuer_shop_id=None if latest_issuer == "root" else stores[0]["id"],
        )
        assert approved["authorization"]["id"] == value["authorization"]["id"]
        assert approved["authorization"]["revision"] == 2
        assert approved["authorization"]["issuer_role"] == latest_issuer
        assert approved["authorization"]["expires"] == value["authorization"]["expires"]
        assert approved["bindings"][0]["device_id"] == binding["device_id"]
        assert approved["bindings"][0]["staff_id"] == binding["staff_id"]
        revoke_shop_auth(c, None)
    expected = 401 if latest_issuer == "root" else 200
    assert cli.get("/api/cli/status").status_code == expected
    rows = stores[0]["browser"].get("/api/admin/audit").json()
    changes = [
        row
        for row in rows
        if row["action"] == f"cli.scope.issuer.{original_issuer}_to_{latest_issuer}"
    ]
    assert len(changes) == 1
    assert changes[0]["target"] == value["authorization"]["id"]
    assert changes[0]["actor"] == (
        "owner" if latest_issuer == "root" else "shop:" + stores[0]["id"]
    )
    assert "public_key" not in changes[0]
    assert (
        clients()
        .post("/api/cli/challenge", json={"device_id": binding["device_id"]})
        .status_code
        == expected
    )

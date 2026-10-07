"""Actual CLI requests, Passkey consent, signed actions and backend retirement."""

import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_cli_auth import link
from test_manage_cli import arguments as manage_arguments
from test_owner_cli import arguments as owner_arguments
from test_passkeys import Authenticator, register
from test_redemption import redeem

from extore import manage_client, owner_client, product_lifecycle
from extore.app import app
from extore.config import ORIGIN
from extore.db import db
from extore.manage_client import ManageError


@pytest.fixture
def actual_cli(owner, tmp_path, monkeypatch):
    records = []
    with TestClient(app, base_url=ORIGIN) as public:

        def relay(request):
            assert "cookie" not in request.headers
            response = public.request(
                request.method,
                str(request.url),
                headers=dict(request.headers),
                content=request.read(),
            )
            records.append(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "query": dict(request.url.params),
                    "status": response.status_code,
                    "signed": "x-extore-cli-signature" in request.headers,
                }
            )
            return httpx.Response(
                response.status_code, headers=response.headers, content=response.content
            )

        transport = httpx.MockTransport(relay)
        owner_profile = tmp_path / "owner-private" / "owner.json"
        manager_profile = tmp_path / "manager-private" / "manager.json"

        def admin(*args):
            return owner_client.execute(
                owner_arguments(owner_profile, *args), transport=transport
            )

        def manage(*args):
            return manage_client.execute(
                manage_arguments(*args, profile=manager_profile), transport=transport
            )

        authenticator = Authenticator()
        register(owner, authenticator, "Synthetic lifecycle CLI consent")
        assert admin("login", "--origin", ORIGIN)["status"] == "pending"
        request = json.loads(owner_profile.read_text())["owners"][0]
        context = owner.post(
            "/api/auth/cli-owner/options",
            json={
                "request_id": request["request_id"],
                "device_code": request["device_code"],
            },
        )
        assert context.status_code == 200, context.text
        approval = owner.post(
            "/api/auth/cli-owner/verify",
            json={
                "request_id": request["request_id"],
                "credential": authenticator.assertion(context.json()["options"]),
            },
        )
        assert approval.status_code == 200, approval.text
        assert admin("login-status")["status"] == "authorized"

        def bind(pid, permissions):
            value, _ = link(owner, pid, permissions=permissions)
            monkeypatch.setattr("sys.stdin", io.StringIO(value["url"]))
            assert manage("login", "--link-stdin")["ok"] is True

        yield admin, manage, bind, records


def test_actual_owner_signed_delete_restore_retains_sold_cards_tasks_and_delivery(
    owner, setup_product, actual_cli, tmp_path
):
    admin, _, _, records = actual_cli
    pid, first_code = setup_product(public=True)
    receipt, first_job = redeem(owner, first_code)
    second_code = owner.post(
        "/api/admin/cards", json={"product_id": pid, "count": 1}
    ).json()["codes"][0]
    before = len(records)
    with pytest.raises(ManageError) as error:
        admin("product", "delete", "--product", pid)
    assert error.value.code == "confirmation_required" and len(records) == before
    deleted = admin("product", "delete", "--product", pid, "--yes")
    assert deleted["product_id"] == pid and deleted["deleted"] is True
    assert owner.get("/api/products").json() == []
    assert admin("products")["products"] == []
    assert admin("products", "--view", "deleted")["products"][0]["deleted"] is True
    queue = admin("queues")["queues"]
    assert (
        queue[0]["product_id"] == pid and queue[0]["jobs"][0]["id"] == first_job["id"]
    )
    second_receipt, second_job = redeem(owner, second_code)
    assert second_job["state"] == "queued"
    for job, token in ((first_job, receipt), (second_job, second_receipt)):
        assert admin("claim", job["id"], "--product", pid)["ok"]
        content = tmp_path / (job["id"] + ".txt")
        content.write_text("synthetic-delivery")
        assert admin(
            "complete", job["id"], "--product", pid, "--content-file", str(content)
        )["ok"]
        revealed = owner.post("/api/receipt/reveal", json={"token": token})
        assert (
            revealed.status_code == 200
            and revealed.json()["content"] == "synthetic-delivery"
        )
    restored = admin("product", "restore", "--product", pid)
    assert restored == {
        "ok": True,
        "product_id": pid,
        "deleted": False,
        "deleted_at": None,
    }
    assert owner.get("/api/products").json()[0]["id"] == pid
    mutations = [
        row
        for row in records
        if row["path"]
        in ("/api/admin/products/" + pid, "/api/admin/products/" + pid + "/restore")
    ]
    assert [row["method"] for row in mutations] == ["DELETE", "POST"]
    assert all(row["status"] == 200 and row["signed"] for row in mutations)
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM cards WHERE product_id=?", (pid,)
            ).fetchone()[0]
            == 2
        )
        assert (
            c.execute(
                "SELECT count(*) FROM jobs WHERE product_id=?", (pid,)
            ).fetchone()[0]
            == 2
        )
        audit = c.execute(
            "SELECT action,actor FROM audit WHERE target=? AND action IN ('product.delete','product.restore') ORDER BY created",
            (pid,),
        ).fetchall()
        assert [(row["action"], row["actor"]) for row in audit] == [
            ("product.delete", "owner"),
            ("product.restore", "owner"),
        ]
        assert (
            c.execute(
                "SELECT count(*) FROM audit WHERE action='cli.owner.action'"
            ).fetchone()[0]
            >= 2
        )


@pytest.mark.parametrize("generic", [False, True])
def test_actual_product_grant_requires_explicit_delete_permission_and_keeps_scope(
    owner, setup_product, actual_cli, generic
):
    _, manage, bind, records = actual_cli
    pid, _ = setup_product()
    other, _ = setup_product()
    bind(
        pid,
        [
            "queue.view",
            "queue.process",
            "queue.retry",
            "product.edit",
            "fulfillment.configure",
            "cards.manage",
            "events.manage",
            "links.delegate",
        ],
    )
    before = len(records)
    with pytest.raises(ManageError) as error:
        manage("product", "delete", "--product", pid, "--yes")
    assert error.value.code == "no_scope"
    assert not any(row["method"] == "DELETE" for row in records[before:])
    bind(pid, ["product.delete"])
    before = len(records)
    with pytest.raises(ManageError) as error:
        manage("product", "delete", "--product", pid)
    assert error.value.code == "confirmation_required" and len(records) == before
    with pytest.raises(ManageError) as error:
        manage("product", "delete", "--product", other, "--yes")
    assert error.value.code == "no_scope"
    deleted = (
        manage("api", "DELETE", "/api/manage/product", "--product", pid, "--yes")[
            "result"
        ]
        if generic
        else manage("product", "delete", "--product", pid, "--yes")
    )
    assert deleted["deleted"] is True
    assert manage("products")["products"] == []
    assert manage("products", "--view", "deleted")["products"][0]["id"] == pid
    restored = (
        manage("api", "POST", "/api/manage/product/restore", "--product", pid)["result"]
        if generic
        else manage("product", "restore", "--product", pid)
    )
    assert restored["deleted"] is False
    with db() as c:
        assert not product_lifecycle.metadata(c, pid)
        assert not product_lifecycle.metadata(c, other)
    mutations = [
        row
        for row in records
        if row["path"] in ("/api/manage/product", "/api/manage/product/restore")
        and row["method"] in ("DELETE", "POST")
    ]
    assert [row["method"] for row in mutations] == ["DELETE", "POST"]
    assert all(
        row["query"]["product_id"] == pid and row["status"] == 200 for row in mutations
    )
    assert not any(
        row["path"] == "/api/manage/product" and row["method"] == "GET"
        for row in records
    )


@pytest.mark.parametrize("path", ["admin", "manage"])
def test_actual_owner_generic_lifecycle_maps_and_signs_fixed_product_target(
    setup_product, actual_cli, path
):
    admin, _, _, records = actual_cli
    pid, _ = setup_product()
    target = "/api/admin/products/" + pid if path == "admin" else "/api/manage/product"
    deleted = admin("api", "DELETE", target, "--product", pid, "--yes")["result"]
    assert deleted["deleted"] is True
    restored = admin("api", "POST", target + "/restore", "--product", pid)["result"]
    assert restored["deleted"] is False
    mutations = [
        row for row in records if row["path"].startswith("/api/admin/products/" + pid)
    ]
    assert len(mutations) == 2 and all(
        row["signed"] and row["status"] == 200 for row in mutations
    )
    with pytest.raises(ManageError) as error:
        admin("product", "delete", "--product", "no-such-product", "--yes")
    assert error.value.status == 404

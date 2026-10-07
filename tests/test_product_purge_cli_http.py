"""Real CLI signatures, shop scope and preserved sold fulfillment after purge."""

import pytest
from starlette.responses import Response
from test_processor_profiles import merchant
from test_product_lifecycle_cli_http import actual_cli as actual_cli
from test_redemption import redeem

from extore.db import db
from extore.manage_client import ManageError
from extore.models import LINK_PERMISSIONS
from extore.security import create_session


@pytest.fixture(params=("root", "shop"))
def owner(client, request):
    """Use the same real Passkey/device fixture for both account scopes."""
    if request.param == "shop":
        store, _ = merchant()
        try:
            yield store
        finally:
            store.close()
    else:
        with db() as c:
            value = create_session(c, Response(), "admin")
        client.cookies.set("extore_session", value)
        yield client


def product_shop(pid):
    with db() as c:
        return c.execute("SELECT shop_id FROM products WHERE id=?", (pid,)).fetchone()[
            0
        ]


def test_actual_signed_single_purge_keeps_old_cards_jobs_and_delivery(
    owner, setup_product, actual_cli, tmp_path
):
    admin, _, _, records = actual_cli
    pid, code = setup_product(public=True)
    token, first_job = redeem(owner, code)
    second = owner.post(
        "/api/admin/cards", json={"product_id": pid, "count": 1}
    ).json()["codes"][0]
    admin("product", "delete", "--product", pid, "--yes")
    before = len(records)
    with pytest.raises(ManageError) as error:
        admin("product", "purge", "--product", pid)
    assert error.value.code == "confirmation_required" and len(records) == before
    result = admin("product", "purge", "--product", pid, "--yes")
    assert result["product_id"] == pid and result["purged"] is True
    writes = [
        row for row in records if row["path"] == f"/api/admin/products/{pid}/purge"
    ]
    assert len(writes) == 1 and writes[0]["signed"] and writes[0]["status"] == 200
    with pytest.raises(ManageError) as error:
        admin("product", "restore", "--product", pid)
    assert error.value.status == 409
    for view in ("active", "deleted", "all"):
        assert not admin("products", "--view", view)["products"]
    history = admin("products", "--view", "history")["products"]
    assert history[0]["id"] == pid and history[0]["purged"] is True
    assert "purged_by" not in history[0]
    assert owner.get("/api/products").json() == []
    queues = admin("queues")["queues"]
    assert queues[0]["jobs"][0]["id"] == first_job["id"]

    second_token, second_job = redeem(owner, second)
    for receipt, job in ((token, first_job), (second_token, second_job)):
        assert admin("claim", job["id"], "--product", pid)["ok"]
        content = tmp_path / (job["id"] + ".txt")
        content.write_text("synthetic retained delivery")
        assert admin(
            "complete", job["id"], "--product", pid, "--content-file", str(content)
        )["ok"]
        response = owner.post("/api/receipt/reveal", json={"token": receipt})
        assert response.status_code == 200
        assert response.json()["content"] == "synthetic retained delivery"
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM cards WHERE product_id=?", (pid,)
            ).fetchone()[0]
            == 2
        )
        assert (
            c.execute(
                "SELECT count(*) FROM jobs WHERE product_id=? AND state='succeeded'",
                (pid,),
            ).fetchone()[0]
            == 2
        )
        assert (
            c.execute(
                "SELECT count(*) FROM audit WHERE action='product.purge' AND target=?",
                (pid,),
            ).fetchone()[0]
            == 1
        )


def test_actual_signed_shop_snapshot_empty_preserves_other_shop_and_noop(
    owner, setup_product, actual_cli, request
):
    admin, _, _, records = actual_cli
    pids = [setup_product()[0], setup_product()[0]]
    sid = product_shop(pids[0])
    foreign, _ = merchant()
    try:
        other = foreign.post("/api/admin/products", json={"name": "Other shop"})
        assert other.status_code == 200, other.text
        other_pid = other.json()["id"]
        assert (
            foreign.request(
                "DELETE", "/api/admin/products/" + other_pid, json={"confirmed": True}
            ).status_code
            == 200
        )
        for pid in pids:
            admin("product", "delete", "--product", pid, "--yes")
        root_scope = request.node.callspec.params["owner"] == "root"
        scope = ("--shop", sid) if root_scope else ()
        before = len(records)
        with pytest.raises(ManageError) as error:
            admin("trash", "empty", *scope)
        assert error.value.code == "confirmation_required" and len(records) == before
        if root_scope:
            with pytest.raises(ManageError) as error:
                admin("trash", "empty", "--yes")
            assert error.value.code == "no_scope" and len(records) == before
        result = admin("trash", "empty", *scope, "--yes")
        assert result == {
            "ok": True,
            "purged_product_ids": result["purged_product_ids"],
            "purged_count": 2,
            "preserved_fulfillment": True,
        }
        assert set(result["purged_product_ids"]) == set(pids)
        writes = [
            row for row in records if row["path"] == "/api/admin/products/empty-trash"
        ]
        assert len(writes) == 1 and writes[0]["signed"]
        assert writes[0]["status"] == 200 and writes[0]["query"] == {"shop_id": sid}
        with db() as c:
            assert not c.execute(
                "SELECT 1 FROM product_purges WHERE product_id=?", (other_pid,)
            ).fetchone()
            assert (
                c.execute(
                    "SELECT deleted_at FROM product_lifecycle WHERE product_id=?",
                    (other_pid,),
                ).fetchone()[0]
                is not None
            )
        before = len(records)
        noop = admin("trash", "empty", *scope, "--yes")
        assert noop["purged_count"] == 0
        assert all(row["method"] == "GET" for row in records[before:])
    finally:
        foreign.close()


@pytest.mark.parametrize("bulk", (False, True))
def test_actual_purge_only_grant_does_not_expand_old_nine_or_other_product(
    setup_product, actual_cli, bulk
):
    admin, manage, bind, records = actual_cli
    pid, _ = setup_product()
    other, _ = setup_product()
    bind(pid, list(LINK_PERMISSIONS[:9]))
    before = len(records)
    with pytest.raises(ManageError) as error:
        manage("product", "purge", "--product", pid, "--yes")
    assert error.value.code == "no_scope"
    assert not any(row["path"].endswith("/purge") for row in records[before:])
    bind(pid, ["product.purge"])
    for selected in (pid, other):
        admin("product", "delete", "--product", selected, "--yes")
    command = ("trash", "empty") if bulk else ("product", "purge")
    before = len(records)
    with pytest.raises(ManageError) as error:
        manage(*command, "--product", pid)
    assert error.value.code == "confirmation_required" and len(records) == before
    with pytest.raises(ManageError) as error:
        manage(*command, "--product", other, "--yes")
    assert error.value.code == "no_scope"
    result = manage(*command, "--product", pid, "--yes")
    assert result.get("purged") is True or result.get("purged_count") == 1
    path = "/api/manage/products/empty-trash" if bulk else "/api/manage/product/purge"
    writes = [row for row in records if row["path"] == path]
    assert len(writes) == 1 and writes[0]["status"] == 200
    assert writes[0]["query"] == {"product_id": pid}
    assert not manage("products", "--view", "all")["products"]
    assert manage("products", "--view", "history")["products"][0]["purged"] is True
    with db() as c:
        assert c.execute(
            "SELECT 1 FROM product_purges WHERE product_id=?", (pid,)
        ).fetchone()
        assert not c.execute(
            "SELECT 1 FROM product_purges WHERE product_id=?", (other,)
        ).fetchone()

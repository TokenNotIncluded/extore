"""Product retirement preserves already sold fulfillment and explicit authorization."""

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from test_official_processing import create as script_product
from test_official_processing import redeem as script_redeem
from test_processor_profiles import binding, merchant
from test_product_links import as_owner, create_link, login_link
from test_redemption import finish, redeem
from test_scope_auth import (
    CLI,
    QUEUE,
    approve,
    claim,
    granted,
    identity,
    pending,
    proof_body,
    request_body,
    review,
)

from extore import product_lifecycle, service
from extore.db import db, init
from extore.models import LINK_PERMISSIONS
from extore.security import sign, staff_authorization
from extore.worker import job_once


def remove(client, pid, **kwargs):
    return client.request(
        "DELETE", "/api/admin/products/" + pid, json={"confirmed": True}, **kwargs
    )


def test_recycle_bin_views_restore_and_idempotent_audit(owner, setup_product):
    pid, _ = setup_product(public=True)
    before = owner.get("/api/admin/products").json()[0]
    summary = owner.get("/api/manage/products?compact=true").json()[0]
    assert "deleted" not in before and "deleted" not in summary
    deleted = remove(owner, pid)
    assert deleted.status_code == 200, deleted.text
    expected = deleted.json()
    assert expected == {
        "ok": True,
        "product_id": pid,
        "deleted": True,
        "deleted_at": expected["deleted_at"],
    }
    assert remove(owner, pid).json() == expected
    for path in ("/api/admin/products", "/api/manage/products"):
        assert owner.get(path).json() == []
        assert owner.get(path + "?view=deleted").json()[0]["deleted"] is True
        assert owner.get(path + "?view=all&compact=true").json()[0] == {
            **summary,
            **({"shop_id": before["shop_id"]} if "admin" in path else {}),
            "deleted": True,
            "deleted_at": expected["deleted_at"],
        }
        assert owner.get(path + "?view=invalid").status_code == 422
    assert owner.get("/api/products").json() == []
    assert owner.get("/api/products/" + pid).status_code == 404
    detail = owner.get("/api/manage/product?product_id=" + pid).json()
    assert detail["deleted"] is True
    for _ in range(2):
        response = owner.post("/api/admin/products/" + pid + "/restore")
        assert response.status_code == 200
        assert response.json() == {
            "ok": True,
            "product_id": pid,
            "deleted": False,
            "deleted_at": None,
        }
    assert owner.get("/api/admin/products").json()[0] == before
    assert owner.get("/api/products").json()[0]["id"] == pid
    with db() as c:
        assert [
            (r["action"])
            for r in c.execute(
                "SELECT action FROM audit WHERE target=? AND action IN ('product.delete','product.restore') ORDER BY created",
                (pid,),
            )
        ] == ["product.delete", "product.restore"]


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"confirmed": False},
        {"confirmed": 1},
        {"confirmed": "true"},
        {"confirmed": True, "extra": 1},
    ],
)
def test_delete_requires_strict_confirmation(owner, setup_product, body):
    pid, _ = setup_product()
    response = owner.request("DELETE", "/api/admin/products/" + pid, json=body)
    assert response.status_code == 422
    with db() as c:
        assert not product_lifecycle.metadata(c, pid)


def test_explicit_delete_permission_and_previous_grants_not_extended(
    owner, setup_product
):
    pid, _ = setup_product()
    link = create_link(owner, pid, ["product.edit", "cards.manage"])
    login_link(owner, link)
    assert (
        owner.request(
            "DELETE", "/api/manage/product", json={"confirmed": True}
        ).status_code
        == 403
    )
    assert owner.post("/api/manage/product/restore").status_code == 403
    as_owner(owner)
    link = create_link(owner, pid, ["product.delete"])
    login_link(owner, link)
    assert (
        owner.request(
            "DELETE", "/api/manage/product", json={"confirmed": True}
        ).status_code
        == 200
    )
    # A delete-only grant can inspect safe lifecycle metadata, never needs edit.
    values = owner.get("/api/manage/products?view=all&compact=true").json()
    assert len(values) == 1 and values[0]["deleted"] is True
    assert owner.get("/api/manage/product").status_code == 403
    assert owner.post("/api/manage/product/restore").status_code == 200
    assert LINK_PERMISSIONS[:3] == ("queue.view", "queue.process", "queue.retry")
    assert LINK_PERMISSIONS[8] == "product.delete"


def test_cross_shop_delete_restore_are_forbidden(owner):
    alpha, _ = merchant()
    beta, _ = merchant()
    try:
        pid = alpha.post("/api/admin/products", json={"name": "A"}).json()["id"]
        for url in (
            "/api/admin/products/" + pid,
            "/api/manage/product?product_id=" + pid,
        ):
            assert (
                beta.request("DELETE", url, json={"confirmed": True}).status_code == 403
            )
        assert remove(alpha, pid).status_code == 200
        for url in (
            "/api/admin/products/" + pid + "/restore",
            "/api/manage/product/restore?product_id=" + pid,
        ):
            assert beta.post(url).status_code == 403
        assert remove(owner, pid).status_code == 200
    finally:
        alpha.close()
        beta.close()


def test_old_cards_queue_receipts_and_existing_links_survive(owner, setup_product):
    pid, code = setup_product(public=True)
    queued_token, queued = redeem(owner, code)
    second = owner.post(
        "/api/admin/cards", json={"product_id": pid, "count": 1}
    ).json()["codes"][0]
    link = create_link(owner, pid, ["queue.view", "queue.process"])
    assert remove(owner, pid).status_code == 200
    assert owner.get("/api/admin/cards?product_id=" + pid).status_code == 200
    # Unredeemed sold cards are still accepted and no lifecycle PII enters DTOs.
    second_token, second_job = redeem(owner, second)
    receipt = owner.post("/api/receipt", json={"token": queued_token}).json()
    assert receipt["job"]["state"] == "queued"
    assert "deleted" not in receipt["product"] and "deleted_by" not in json.dumps(
        receipt
    )
    login_link(owner, link)
    assert len(owner.get("/api/manage/jobs").json()) == 2
    assert owner.get("/api/manage/products").json() == []
    assert owner.get("/api/manage/products?view=all").json()[0]["deleted"] is True
    finish(owner, queued)
    finish(owner, second_job)
    for token in (queued_token, second_token):
        assert (
            owner.post("/api/receipt/reveal", json={"token": token}).json()["content"]
            == "secret"
        )
    assert (
        owner.post("/api/receipt/destroy", json={"token": queued_token}).status_code
        == 200
    )
    assert (
        owner.post("/api/receipt/reveal", json={"token": queued_token}).status_code
        == 409
    )


def test_deleted_blocks_all_new_writes_and_restoration_reopens_them(
    owner, setup_product
):
    pid, _ = setup_product()
    config = owner.get("/api/manage/product?product_id=" + pid).json()
    config.pop("id")
    config.pop("shop_id", None)
    assert remove(owner, pid).status_code == 200
    operations = [
        ("POST", "/api/admin/cards", {"product_id": pid, "count": 1}),
        ("POST", "/api/manage/cards", {"product_id": pid, "count": 1}),
        ("POST", "/api/admin/staff", {"product_id": pid, "name": "new"}),
        ("POST", "/api/manage/links", {"name": "new", "product_id": pid}),
        ("PUT", "/api/admin/products/" + pid, config),
        ("PUT", "/api/manage/product?product_id=" + pid, config),
        (
            "POST",
            "/api/admin/products/quick",
            {"template_id": "existing_product", "from_product_id": pid},
        ),
    ]
    with db() as c:
        before = {
            t: c.execute("SELECT count(*) FROM " + t).fetchone()[0]
            for t in ("cards", "staff", "products")
        }
    for method, url, body in operations:
        result = owner.request(method, url, json=body)
        assert result.status_code == 409, (url, result.text)
    with db() as c:
        assert before == {
            t: c.execute("SELECT count(*) FROM " + t).fetchone()[0] for t in before
        }
    owner.post("/api/admin/products/" + pid + "/restore")
    assert (
        owner.post("/api/admin/cards", json={"product_id": pid, "count": 1}).status_code
        == 200
    )


def test_text_import_cannot_bypass_deleted_product(owner):
    pid = owner.post(
        "/api/admin/products", json={"name": "stock", "mode": "stock"}
    ).json()["id"]
    assert remove(owner, pid).status_code == 200
    for prefix in ("admin", "manage"):
        result = owner.post(
            f"/api/{prefix}/cards/import-text",
            json={"product_id": pid, "text": "one\ntwo"},
        )
        assert result.status_code == 409, result.text
    with db() as c:
        assert c.execute("SELECT count(*) FROM cards").fetchone()[0] == 0


def test_processor_configuration_blocks_binding_but_sold_execution_continues(owner):
    p = script_product(owner, configuration={"template": "Hello $name"})
    receipt, _ = script_redeem(owner, p, {"name": "customer"})
    profile = binding(owner, p["id"])
    assert remove(owner, p["id"]).status_code == 200
    url = "/api/admin/processor-profiles/bindings/" + p["id"]
    assert owner.put(url, json={"profile_id": profile["id"]}).status_code == 409
    assert owner.delete(url).status_code == 409
    assert asyncio.run(job_once()) is True
    assert owner.post("/api/receipt/reveal", json={"token": receipt["token"]}).json()[
        "output"
    ] == {"content": "Hello customer"}


def test_old_webhook_callback_continues_after_delete(owner, setup_product):
    secret = "a-secure-callback-signing-secret-32chars"
    pid, code = setup_product(
        mode="webhook", webhook_url="https://example.com/work", webhook_secret=secret
    )
    receipt, j = redeem(owner, code)
    assert remove(owner, pid).status_code == 200
    body = json.dumps(
        {"state": "succeeded", "attempt": 1, "content": "callback output"}
    ).encode()
    timestamp, nonce = str(int(time.time())), "synthetic-deleted-callback"
    signature = sign(secret, timestamp, nonce, body)
    result = owner.post(
        f"/api/callbacks/{pid}/{j['id']}",
        content=body,
        headers={
            "content-type": "application/json",
            "x-extore-timestamp": timestamp,
            "x-extore-nonce": nonce,
            "x-extore-signature": signature,
        },
    )
    assert result.status_code == 200, result.text
    assert (
        owner.post("/api/receipt/reveal", json={"token": receipt}).json()["content"]
        == "callback output"
    )


def test_additive_schema15_preserves_all_old54_tables(owner, setup_product):
    setup_product()
    with db() as c:
        c.execute("DROP TABLE product_purges")
        c.execute("DROP TABLE product_lifecycle")
        c.execute("PRAGMA user_version=14")
        before = {
            r["name"]: (
                r["sql"],
                [tuple(v) for v in c.execute('SELECT * FROM "' + r["name"] + '"')],
            )
            for r in c.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert len(before) == 54
    init()
    init()
    with db() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 16
        after = {
            r["name"]: r["sql"]
            for r in c.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert set(after) - set(before) == {"product_lifecycle", "product_purges"}
        for name, (ddl, rows) in before.items():
            assert after[name] == ddl
            assert [tuple(v) for v in c.execute('SELECT * FROM "' + name + '"')] == rows
        assert not c.execute("SELECT * FROM product_lifecycle").fetchall()


def test_deletion_and_issuance_are_serialized(owner, setup_product):
    pid, _ = setup_product()
    held, finish_transaction = threading.Event(), threading.Event()

    def issue_before_delete():
        with db() as c:
            service.issue_cards(c, pid, 1)
            held.set()
            assert finish_transaction.wait(5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        issuing = pool.submit(issue_before_delete)
        assert held.wait(5)
        deleting = pool.submit(remove, owner, pid)
        assert not deleting.done()
        finish_transaction.set()
        issuing.result(5)
        assert deleting.result(5).status_code == 200
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM cards WHERE product_id=?", (pid,)
            ).fetchone()[0]
            == 2
        )
        with pytest.raises(HTTPException, match="商品已删除"):
            service.issue_cards(c, pid, 1)


def test_new_scope_excludes_deleted_but_existing_scope_upgrades_keep_it(owner):
    from fastapi.testclient import TestClient

    from extore.app import app

    first = owner.post("/api/admin/products", json={"name": "first"}).json()["id"]
    second = owner.post("/api/admin/products", json={"name": "second"}).json()["id"]
    with db() as c:
        sid = c.execute("SELECT shop_id FROM products WHERE id=?", (first,)).fetchone()[
            0
        ]
    with TestClient(app, base_url="http://localhost:8000") as cli:
        existing, key = granted(
            owner,
            cli,
            kind="shop.pipeline",
            sid=sid,
            pids=[first],
            permissions=["queue.view"],
        )
        assert remove(owner, first).status_code == 200
        with db() as c:
            assert staff_authorization(c, existing["bindings"][0]["staff_id"])
        new, other, _ = pending(cli, kind="shop.pipeline", sid=sid)
        assert review(owner, new)["request"]["requested_product_ids"] == [second]
        request = cli.post(CLI + "/request", json=request_body(identity(), pid=first))
        assert request.status_code == 409
        upgraded, _, _ = pending(
            cli,
            key,
            kind="shop.pipeline",
            sid=sid,
            pids=[first, second],
            permissions=QUEUE,
            aid=existing["authorization"]["id"],
            revision=existing["authorization"]["revision"],
        )
        approve(owner, upgraded)
        result = claim(cli, upgraded, key)
        assert set(result["authorization"]["product_ids"]) == {first, second}
        # Deletion between review and claim stops adding a new product only.
        third = owner.post("/api/admin/products", json={"name": "third"}).json()["id"]
        pending_new, key_new, _ = pending(cli, pid=third)
        approve(owner, pending_new)
        assert remove(owner, third).status_code == 200
        response = cli.post(
            CLI + "/claim", json=proof_body(pending_new, key_new, "claim")
        )
        assert response.status_code == 409

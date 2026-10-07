"""Permanent trash markers do not erase or revoke already sold fulfillment."""

import math

import pytest
from test_processor_profiles import merchant
from test_product_lifecycle import remove
from test_product_links import as_owner, create_link, login_link
from test_redemption import finish, redeem

from extore import product_lifecycle
from extore.db import db, init
from extore.models import LINK_PERMISSIONS


def purge(client, pid, *, body=None):
    return client.post(
        "/api/admin/products/" + pid + "/purge",
        json={"confirmed": True} if body is None else body,
    )


def empty(client, pids, *, path="/api/admin/products/empty-trash"):
    return client.post(path, json={"confirmed": True, "product_ids": pids})


def markers():
    with db() as c:
        return [
            tuple(row)
            for row in c.execute("SELECT * FROM product_purges ORDER BY product_id")
        ]


def test_purge_preserves_old_code_queue_receipt_and_cannot_restore(
    owner, setup_product
):
    pid, code = setup_product(public=True)
    second = owner.post(
        "/api/admin/cards", json={"product_id": pid, "count": 1}
    ).json()["codes"][0]
    receipt, j = redeem(owner, code)
    link = create_link(owner, pid, ["queue.view", "queue.process"])
    assert remove(owner, pid).status_code == 200
    response = purge(owner, pid)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result == {
        "ok": True,
        "product_id": pid,
        "deleted": True,
        "purged": True,
        "purged_at": result["purged_at"],
    }
    assert math.isfinite(result["purged_at"])
    assert purge(owner, pid).json() == result
    assert owner.post("/api/admin/products/" + pid + "/restore").status_code == 409
    assert (
        owner.post("/api/manage/product/restore?product_id=" + pid).status_code == 409
    )
    for base in ("/api/admin/products", "/api/manage/products"):
        for view in ("active", "deleted", "all"):
            assert owner.get(base + "?view=" + view).json() == []
        history = owner.get(base + "?view=history&compact=true").json()
        assert len(history) == 1 and history[0]["purged"] is True
        assert history[0]["deleted"] is True
        assert "purged_by" not in history[0] and "deleted_by" not in history[0]
    assert owner.get("/api/products").json() == []
    second_receipt, second_job = redeem(owner, second)
    assert owner.get("/api/admin/cards?product_id=" + pid).status_code == 200
    assert (
        owner.post("/api/admin/cards", json={"product_id": pid, "count": 1}).status_code
        == 409
    )
    assert (
        owner.post(
            "/api/admin/staff", json={"product_id": pid, "name": "new"}
        ).status_code
        == 409
    )
    login_link(owner, link)
    assert owner.get("/api/manage/products?view=all").json() == []
    assert owner.get("/api/manage/products?view=history").json()[0]["purged"] is True
    assert len(owner.get("/api/manage/jobs").json()) == 2
    finish(owner, j)
    finish(owner, second_job)
    for token in (receipt, second_receipt):
        view = owner.post("/api/receipt", json={"token": token}).json()
        assert "purged" not in view["product"] and "deleted_by" not in str(view)
        assert (
            owner.post("/api/receipt/reveal", json={"token": token}).json()["content"]
            == "secret"
        )
    as_owner(owner)
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM audit WHERE action='product.purge' AND target=?",
                (pid,),
            ).fetchone()[0]
            == 1
        )
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


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"confirmed": False},
        {"confirmed": 1},
        {"confirmed": "true"},
        {"confirmed": True, "extra": True},
    ],
)
def test_single_purge_requires_strict_confirmation(owner, setup_product, body):
    pid, _ = setup_product()
    remove(owner, pid)
    response = owner.post("/api/admin/products/" + pid + "/purge", json=body)
    assert response.status_code == 422
    assert markers() == []


@pytest.mark.parametrize(
    "ids", [[], [""], ["x" * 101], ["same", "same"], [1], ["x"] * 501]
)
def test_bulk_requires_bounded_explicit_unique_snapshot(owner, ids):
    response = empty(owner, ids)
    assert response.status_code == 422
    assert markers() == []


@pytest.mark.parametrize(
    "body",
    [
        {"product_ids": ["x"]},
        {"confirmed": False, "product_ids": ["x"]},
        {"confirmed": True, "product_ids": ["x"], "all": True},
    ],
)
def test_bulk_strict_confirmation(owner, body):
    response = owner.post("/api/admin/products/empty-trash", json=body)
    assert response.status_code == 422
    assert markers() == []


def test_bulk_explicit_ids_does_not_purge_later_trash_and_is_idempotent(
    owner, setup_product
):
    first, _ = setup_product()
    second, _ = setup_product()
    later, _ = setup_product()
    remove(owner, first)
    remove(owner, second)
    snapshot = [second, first]
    remove(owner, later)
    expected = {
        "ok": True,
        "purged_product_ids": snapshot,
        "purged_count": 2,
        "preserved_fulfillment": True,
    }
    assert empty(owner, snapshot).json() == expected
    assert empty(owner, snapshot).json() == expected
    assert [p["id"] for p in owner.get("/api/admin/products?view=deleted").json()] == [
        later
    ]
    assert set(
        p["id"] for p in owner.get("/api/admin/products?view=history").json()
    ) == {first, second, later}
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM audit WHERE action='product.purge'"
            ).fetchone()[0]
            == 2
        )


@pytest.mark.parametrize("bad", ["active", "missing"])
def test_bulk_prevalidation_rejects_everything_if_one_id_invalid(
    owner, setup_product, bad
):
    pid, _ = setup_product()
    other, _ = setup_product()
    remove(owner, pid)
    invalid = other if bad == "active" else "does-not-exist"
    before = markers()
    response = empty(owner, [pid, invalid])
    assert response.status_code == (409 if bad == "active" else 404), response.text
    assert markers() == before
    with db() as c:
        assert not c.execute(
            "SELECT 1 FROM audit WHERE action='product.purge'"
        ).fetchone()


def test_bulk_rejects_restored_snapshot_and_purged_replay_stays_atomic(
    owner, setup_product
):
    first, _ = setup_product()
    second, _ = setup_product()
    remove(owner, first)
    remove(owner, second)
    purge(owner, first)
    owner.post("/api/admin/products/" + second + "/restore")
    before = markers()
    assert empty(owner, [first, second]).status_code == 409
    assert markers() == before


def test_owner_and_root_bulk_cannot_cross_shop(owner):
    alpha, sid_a = merchant()
    beta, sid_b = merchant()
    try:
        a = alpha.post("/api/admin/products", json={"name": "A"}).json()["id"]
        b = beta.post("/api/admin/products", json={"name": "B"}).json()["id"]
        remove(alpha, a)
        remove(beta, b)
        assert purge(beta, a).status_code == 403
        assert empty(alpha, [a, b]).status_code == 403
        assert empty(owner, [a, b]).status_code == 403
        assert (
            empty(
                owner, [a], path="/api/admin/products/empty-trash?shop_id=" + sid_b
            ).status_code
            == 403
        )
        assert markers() == []
        assert (
            empty(
                owner, [a], path="/api/admin/products/empty-trash?shop_id=" + sid_a
            ).status_code
            == 200
        )
        assert purge(owner, b).status_code == 200
    finally:
        alpha.close()
        beta.close()


def test_old_permissions_do_not_gain_purge_and_purge_only_grant_is_sufficient(
    owner, setup_product
):
    first, _ = setup_product()
    other, _ = setup_product()
    old = create_link(owner, first, list(LINK_PERMISSIONS[:9]))
    pure = create_link(owner, first, ["product.purge"])
    remove(owner, first)
    remove(owner, other)
    login_link(owner, old)
    assert (
        owner.post("/api/manage/product/purge", json={"confirmed": True}).status_code
        == 403
    )
    assert (
        empty(owner, [first], path="/api/manage/products/empty-trash").status_code
        == 403
    )
    login_link(owner, pure)
    assert (
        owner.get("/api/manage/products?view=deleted&compact=true").json()[0]["id"]
        == first
    )
    assert (
        empty(
            owner, [first, other], path="/api/manage/products/empty-trash"
        ).status_code
        == 403
    )
    assert markers() == []
    assert (
        empty(
            owner, [first], path="/api/manage/products/empty-trash?product_id=" + other
        ).status_code
        == 403
    )
    assert (
        empty(
            owner, [first], path="/api/manage/products/empty-trash?product_id=" + first
        ).status_code
        == 200
    )
    assert (
        owner.post("/api/manage/product/purge", json={"confirmed": True}).status_code
        == 200
    )
    assert owner.get("/api/manage/products?view=all").json() == []
    assert (
        owner.get("/api/manage/products?view=history&compact=true").json()[0]["purged"]
        is True
    )
    assert owner.post("/api/manage/product/restore").status_code == 403
    assert LINK_PERMISSIONS[:3] == ("queue.view", "queue.process", "queue.retry")
    assert LINK_PERMISSIONS[8:10] == ("product.delete", "product.purge")


def test_schema16_only_adds_empty_purge_table_and_preserves_old55(owner, setup_product):
    pid, _ = setup_product()
    remove(owner, pid)
    init()
    with db() as c:
        c.execute("DROP TABLE product_purges")
        c.execute("PRAGMA user_version=15")
        before = {
            r["name"]: (
                r["sql"],
                [tuple(v) for v in c.execute('SELECT * FROM "' + r["name"] + '"')],
            )
            for r in c.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert len(before) == 57
    init()
    init()
    with db() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 20
        names = {
            r["name"]
            for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert names - set(before) == {"product_purges"}
        for name, (ddl, rows) in before.items():
            assert (
                c.execute(
                    "SELECT sql FROM sqlite_master WHERE name=?", (name,)
                ).fetchone()[0]
                == ddl
            )
            assert [tuple(v) for v in c.execute('SELECT * FROM "' + name + '"')] == rows
        assert not c.execute("SELECT * FROM product_purges").fetchall()
        assert product_lifecycle.metadata(c, pid)["deleted"] is True


@pytest.mark.parametrize("scenario", ["script", "webhook", "scope"])
def test_purged_automatic_execution_and_existing_scoped_grants_remain_live(
    owner, setup_product, monkeypatch, scenario
):
    import test_product_lifecycle as checks

    original = checks.remove

    def retire_and_purge(client, pid, **kwargs):
        response = original(client, pid, **kwargs)
        if response.status_code == 200:
            assert purge(client, pid).status_code == 200
        return response

    monkeypatch.setattr(checks, "remove", retire_and_purge)
    if scenario == "script":
        checks.test_processor_configuration_blocks_binding_but_sold_execution_continues(
            owner
        )
    elif scenario == "webhook":
        checks.test_old_webhook_callback_continues_after_delete(owner, setup_product)
    else:
        checks.test_new_scope_excludes_deleted_but_existing_scope_upgrades_keep_it(
            owner
        )

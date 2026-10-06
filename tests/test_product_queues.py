import json

import pytest
from test_redemption import redeem

from extore.db import db


def login_staff(owner, product_id):
    response = owner.post(
        "/api/admin/staff", json={"product_id": product_id, "name": "商品处理员"}
    )
    assert response.status_code == 200, response.text
    value = response.json()["url"].split("#")[1]
    response = owner.post("/api/staff/login", json={"token": value})
    assert response.status_code == 200, response.text


def snapshot():
    with db() as c:
        return {
            table: [dict(row) for row in c.execute(f"SELECT * FROM {table}")]
            for table in ("jobs", "events", "audit")
        }


def test_queue_product_list_is_minimal_and_requires_login(owner, setup_product):
    pid, _ = setup_product(name="人工服务", delivery="service")
    pid2, _ = setup_product(
        name="自动交付",
        mode="webhook",
        webhook_url="https://example.com/secret-hook",
        webhook_secret="a-private-signing-secret-32-characters",
    )
    response = owner.get("/api/manage/products")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert [
        {
            key: value
            for key, value in row.items()
            if key not in ("parameters", "outputs")
        }
        for row in rows
    ] == [
        {
            "id": pid,
            "name": "人工服务",
            "mode": "manual",
            "delivery": "service",
            "view_policy": "repeat",
        },
        {
            "id": pid2,
            "name": "自动交付",
            "mode": "webhook",
            "delivery": "content",
            "view_policy": "repeat",
        },
    ]
    assert all(
        set(row)
        == {"id", "name", "mode", "delivery", "view_policy", "parameters", "outputs"}
        for row in rows
    )
    assert all(row["parameters"][0]["key"] == "email" for row in rows)
    assert rows[0]["outputs"] == []
    assert rows[1]["outputs"][0]["key"] == "content"
    assert "secret-hook" not in response.text and "signing-secret" not in response.text
    owner.cookies.clear()
    assert owner.get("/api/manage/products").status_code == 401


def test_admin_must_select_existing_product_even_for_empty_queue(owner, setup_product):
    pid, _ = setup_product()
    for query in ({}, {"product_id": ""}):
        assert owner.get("/api/manage/jobs", params=query).status_code == 400
    assert (
        owner.get("/api/manage/jobs", params={"product_id": "missing"}).status_code
        == 404
    )
    response = owner.get("/api/manage/jobs", params={"product_id": pid})
    assert response.status_code == 200 and response.json() == []


def test_queue_order_position_state_and_limit_are_product_scoped(owner, setup_product):
    pid, code = setup_product()
    _, first = redeem(owner, code)
    pid2, code2 = setup_product()
    _, other = redeem(owner, code2)
    code3 = owner.post("/api/admin/cards", json={"product_id": pid, "count": 1}).json()[
        "codes"
    ][0]
    _, second = redeem(owner, code3)
    rows = owner.get("/api/manage/jobs", params={"product_id": pid}).json()
    assert [row["id"] for row in rows] == [first["id"], second["id"]]
    assert [row["queue_ahead"] for row in rows] == [0, 1]
    rows = owner.get("/api/manage/jobs", params={"product_id": pid2}).json()
    assert [(row["id"], row["queue_ahead"]) for row in rows] == [(other["id"], 0)]
    assert (
        owner.post(
            "/api/manage/batch",
            json={"product_id": pid, "ids": [first["id"]], "action": "claim"},
        ).status_code
        == 200
    )
    rows = owner.get(
        "/api/manage/jobs", params={"product_id": pid, "state": "queued", "limit": 1}
    ).json()
    assert [(row["id"], row["queue_ahead"]) for row in rows] == [(second["id"], 0)]
    rows = owner.get(
        "/api/manage/jobs", params={"product_id": pid, "state": "processing"}
    ).json()
    assert [row["id"] for row in rows] == [first["id"]]


def test_batch_requires_existing_product_and_rejects_cross_product_atomically(
    owner, setup_product
):
    pid, code = setup_product()
    _, first = redeem(owner, code)
    _, other_code = setup_product()
    _, other = redeem(owner, other_code)
    before = snapshot()
    for body, status in (
        ({"ids": [first["id"]], "action": "claim"}, 422),
        ({"product_id": "", "ids": [first["id"]], "action": "claim"}, 422),
        ({"product_id": "missing", "ids": [first["id"]], "action": "claim"}, 404),
        (
            {"product_id": pid, "ids": [first["id"], other["id"]], "action": "claim"},
            403,
        ),
    ):
        response = owner.post("/api/manage/batch", json=body)
        assert response.status_code == status, response.text
        assert snapshot() == before


def test_same_product_batch_handles_duplicates_once(owner, setup_product):
    pid, code = setup_product(delivery="service")
    _, first = redeem(owner, code)
    code2 = owner.post("/api/admin/cards", json={"product_id": pid, "count": 1}).json()[
        "codes"
    ][0]
    _, second = redeem(owner, code2)
    body = {"product_id": pid, "ids": [first["id"], second["id"], first["id"]]}
    assert (
        owner.post("/api/manage/batch", json={**body, "action": "claim"}).status_code
        == 200
    )
    assert (
        owner.post(
            "/api/manage/batch", json={**body, "action": "succeed", "message": "已完成"}
        ).status_code
        == 200
    )
    rows = owner.get("/api/manage/jobs", params={"product_id": pid}).json()
    assert len(rows) == 2 and all(row["state"] == "succeeded" for row in rows)
    with db() as c:
        assert (
            c.execute("SELECT count(*) FROM audit WHERE action='job.claim'").fetchone()[
                0
            ]
            == 2
        )
        assert (
            c.execute(
                "SELECT count(*) FROM audit WHERE action='job.succeed'"
            ).fetchone()[0]
            == 2
        )


def test_same_product_batch_rolls_back_on_later_state_conflict(owner, setup_product):
    pid, code = setup_product()
    _, first = redeem(owner, code)
    code2 = owner.post("/api/admin/cards", json={"product_id": pid, "count": 1}).json()[
        "codes"
    ][0]
    _, second = redeem(owner, code2)
    assert (
        owner.post(
            "/api/manage/batch",
            json={"product_id": pid, "ids": [second["id"]], "action": "claim"},
        ).status_code
        == 200
    )
    before = snapshot()
    response = owner.post(
        "/api/manage/batch",
        json={"product_id": pid, "ids": [first["id"], second["id"]], "action": "claim"},
    )
    assert response.status_code == 409
    assert snapshot() == before


def test_staff_only_sees_own_product_and_rejects_foreign_scope(owner, setup_product):
    pid, code = setup_product(name="员工的商品")
    _, own = redeem(owner, code)
    pid2, code2 = setup_product(name="其他商品")
    _, other = redeem(owner, code2)
    login_staff(owner, pid)
    products = owner.get("/api/manage/products").json()
    assert len(products) == 1 and products[0]["id"] == pid
    for query in ({}, {"product_id": pid}):
        rows = owner.get("/api/manage/jobs", params=query).json()
        assert [row["id"] for row in rows] == [own["id"]]
    before = snapshot()
    for foreign_id in (pid2, "nonexistent"):
        assert (
            owner.get("/api/manage/jobs", params={"product_id": foreign_id}).status_code
            == 403
        )
        assert (
            owner.post(
                "/api/manage/batch",
                json={
                    "product_id": foreign_id,
                    "ids": [other["id"]],
                    "action": "claim",
                },
            ).status_code
            == 403
        )
    assert (
        owner.post(
            "/api/manage/batch",
            json={
                "product_id": pid,
                "ids": [own["id"], other["id"]],
                "action": "claim",
            },
        ).status_code
        == 403
    )
    assert snapshot() == before
    assert (
        owner.post(
            "/api/manage/batch",
            json={"product_id": pid, "ids": [own["id"]], "action": "claim"},
        ).status_code
        == 200
    )


@pytest.mark.parametrize("operation", ("products", "jobs", "batch"))
def test_revocation_before_queue_transaction_prevents_access(
    owner, setup_product, monkeypatch, operation
):
    import extore.app as app_module

    pid, code = setup_product()
    _, own = redeem(owner, code)
    login_staff(owner, pid)
    real_session = app_module.session

    def revoked_after_authentication(*args, **kwargs):
        result = real_session(*args, **kwargs)
        with db() as c:
            c.execute("UPDATE staff SET revoked=1 WHERE id=?", (result["staff_id"],))
        return result

    monkeypatch.setattr(app_module, "session", revoked_after_authentication)
    before = snapshot()
    if operation == "batch":
        response = owner.post(
            "/api/manage/batch",
            json={"product_id": pid, "ids": [own["id"]], "action": "claim"},
        )
    else:
        response = owner.get(f"/api/manage/{operation}", params={"product_id": pid})
    assert response.status_code == 401
    assert snapshot() == before


def test_ancestor_permission_change_before_batch_transaction_is_enforced(
    owner, setup_product, monkeypatch
):
    import extore.app as app_module

    pid, code = setup_product()
    _, own = redeem(owner, code)
    parent = owner.post(
        "/api/admin/staff",
        json={
            "product_id": pid,
            "name": "店长",
            "permissions": ["queue.view", "queue.process", "links.delegate"],
        },
    ).json()
    owner.post("/api/staff/login", json={"token": parent["url"].split("#")[1]})
    child = owner.post(
        "/api/manage/links",
        json={"name": "处理员", "permissions": ["queue.view", "queue.process"]},
    ).json()
    owner.post("/api/staff/login", json={"token": child["url"].split("#")[1]})
    real_session = app_module.session

    def reduced_after_authentication(*args, **kwargs):
        result = real_session(*args, **kwargs)
        with db() as c:
            c.execute(
                "UPDATE staff SET permissions=? WHERE id=?",
                (json.dumps(["queue.view", "links.delegate"]), parent["id"]),
            )
        return result

    monkeypatch.setattr(app_module, "session", reduced_after_authentication)
    before = snapshot()
    response = owner.post(
        "/api/manage/batch",
        json={"product_id": pid, "ids": [own["id"]], "action": "claim"},
    )
    assert response.status_code == 403
    assert snapshot() == before

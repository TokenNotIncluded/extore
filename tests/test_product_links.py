import json
import time

import pytest
from starlette.responses import Response
from test_redemption import redeem

from extore.db import db
from extore.security import create_session, digest, sign

ALL_PERMISSIONS = {
    "queue.view",
    "queue.process",
    "queue.retry",
    "product.edit",
    "fulfillment.configure",
    "cards.manage",
    "events.manage",
    "links.delegate",
}
LEGACY_PERMISSIONS = {"queue.view", "queue.process"}
SECRET = "this-is-a-private-webhook-secret-32-chars"
PROCESSOR_CONFIGS = (
    (
        "resource_link",
        {
            "resource_url": "https://example.com/private-resource",
            "message": "私有资源领取说明",
        },
    ),
    (
        "personalized_text",
        {
            "template": "私有交付模板：你好，$name，你的独享内容是 EXTORE-PRIVATE-TEMPLATE。"
        },
    ),
)


def as_owner(client):
    with db() as c:
        value = create_session(c, Response(), "admin")
    use_session(client, value)


def use_session(client, value):
    client.cookies.clear()
    client.cookies.set("extore_session", value)


def login_link(client, link):
    client.cookies.clear()
    response = client.post(
        "/api/staff/login", json={"token": link["url"].split("#", 1)[1]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "staff"
    return client.cookies.get("extore_session")


def create_link(client, product_id, permissions=None, **kwargs):
    body = {"product_id": product_id, "name": "商品管理人员", "max_uses": 100, **kwargs}
    if permissions is not None:
        body["permissions"] = permissions
    response = client.post("/api/admin/staff", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def delegate(client, permissions, **kwargs):
    response = client.post(
        "/api/manage/links",
        json={
            "name": "下级商品管理人员",
            "days": 1,
            "max_uses": 100,
            "permissions": permissions,
            **kwargs,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def snapshot(*tables):
    with db() as c:
        return {
            table: [dict(row) for row in c.execute(f"SELECT * FROM {table}")]
            for table in tables
        }


def assert_safe_link(link, product_id, permissions):
    assert link["product_id"] == product_id
    assert set(link["permissions"]) == set(permissions)
    assert len(link["permissions"]) == len(set(link["permissions"]))
    assert link["name"] and link["expires"] > time.time()
    assert not {"digest", "token", "secret"}.intersection(link)


def test_legacy_defaults_and_safe_staff_session(owner, setup_product):
    pid, _ = setup_product()
    link = create_link(owner, pid)
    assert_safe_link(link, pid, LEGACY_PERMISSIONS)
    private_token = link["url"].split("#", 1)[1]
    with db() as c:
        row = c.execute("SELECT * FROM staff WHERE id=?", (link["id"],)).fetchone()
        assert row["digest"] == digest(private_token)
        assert private_token not in str(dict(row))
    listed = owner.get("/api/admin/staff").json()
    assert len(listed) == 1
    assert_safe_link(listed[0], pid, LEGACY_PERMISSIONS)
    assert "url" not in listed[0] and private_token not in str(listed)
    login_link(owner, link)
    status = owner.get("/api/auth/status").json()
    assert status["role"] == "staff"
    assert status["product_id"] == pid
    assert status["link_name"] == link["name"]
    assert set(status["permissions"]) == LEGACY_PERMISSIONS
    assert status["link_expires"] == link["expires"]
    assert status["link_id"] == link["id"]
    assert status["parent_id"] is None
    assert not {"digest", "token", "url"}.intersection(status)
    for endpoint in ("products", "cards", "events", "staff"):
        assert owner.get(f"/api/admin/{endpoint}").status_code == 401
    assert owner.get("/api/auth/passkeys").status_code == 401


@pytest.mark.parametrize(
    "permissions",
    (
        [],
        ["unknown.permission"],
        ["queue.view", "unknown.permission"],
        ["queue.process"],
        ["queue.retry"],
        ["queue.process", "queue.retry"],
        ["fulfillment.configure"],
        ["queue.view", "fulfillment.configure"],
    ),
)
def test_invalid_permissions_do_not_create_links(owner, setup_product, permissions):
    pid, _ = setup_product()
    before = snapshot("staff", "audit")
    response = owner.post(
        "/api/admin/staff",
        json={"product_id": pid, "name": "权限无效", "permissions": permissions},
    )
    assert response.status_code == 422, response.text
    assert snapshot("staff", "audit") == before


def test_permissions_are_normalized_and_work_on_automated_products(
    owner, setup_product
):
    pid, _ = setup_product(
        mode="webhook", webhook_url="https://example.com/hooks", webhook_secret=SECRET
    )
    link = create_link(owner, pid, ["events.manage", "events.manage"])
    assert_safe_link(link, pid, {"events.manage"})
    login_link(owner, link)
    response = owner.get("/api/manage/products")
    assert response.status_code == 200, response.text
    rows = response.json()
    assert [
        {
            key: value
            for key, value in row.items()
            if key
            not in (
                "parameters",
                "outputs",
                "variants",
                "progress_steps",
                "support_email",
            )
        }
        for row in rows
    ] == [
        {
            "id": pid,
            "name": "测试商品",
            "mode": "webhook",
            "delivery": "content",
            "view_policy": "repeat",
        }
    ]
    assert all(
        isinstance(row["parameters"], list) and isinstance(row["outputs"], list)
        for row in rows
    )
    assert SECRET not in response.text and "webhook_url" not in response.text
    assert owner.get("/api/manage/jobs").status_code == 403


def test_queue_view_cannot_mutate_any_job_action(owner, setup_product):
    pid, code = setup_product()
    _, task = redeem(owner, code)
    link = create_link(owner, pid, ["queue.view"])
    login_link(owner, link)
    response = owner.get("/api/manage/jobs")
    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [task["id"]]
    before = snapshot("jobs", "events", "audit")
    for action in ("claim", "progress", "succeed", "fail", "retry"):
        response = owner.post(
            "/api/manage/batch",
            json={"product_id": pid, "ids": [task["id"]], "action": action},
        )
        assert response.status_code == 403, response.text
        assert snapshot("jobs", "events", "audit") == before


def test_queue_process_and_retry_are_independent_permissions(owner, setup_product):
    pid, code = setup_product(delivery="service")
    _, failed = redeem(owner, code)
    code2 = owner.post("/api/admin/cards", json={"product_id": pid, "count": 1}).json()[
        "codes"
    ][0]
    _, successful = redeem(owner, code2)
    processor = create_link(owner, pid, ["queue.view", "queue.process"])
    retrier = create_link(owner, pid, ["queue.view", "queue.retry"])
    login_link(owner, processor)
    body = {"product_id": pid, "ids": [failed["id"], successful["id"]]}
    assert (
        owner.post("/api/manage/batch", json={**body, "action": "claim"}).status_code
        == 200
    )
    assert (
        owner.post(
            "/api/manage/batch", json={**body, "action": "progress", "progress": 40}
        ).status_code
        == 200
    )
    assert (
        owner.post(
            "/api/manage/batch",
            json={"product_id": pid, "ids": [failed["id"]], "action": "fail"},
        ).status_code
        == 200
    )
    assert (
        owner.post(
            "/api/manage/batch",
            json={"product_id": pid, "ids": [successful["id"]], "action": "succeed"},
        ).status_code
        == 200
    )
    retry = {"product_id": pid, "ids": [failed["id"]], "action": "retry"}
    assert owner.post("/api/manage/batch", json=retry).status_code == 403
    login_link(owner, retrier)
    assert owner.post("/api/manage/batch", json=retry).status_code == 200
    assert (
        owner.post("/api/manage/batch", json={**retry, "action": "claim"}).status_code
        == 403
    )
    with db() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (failed["id"],)).fetchone()
        assert row["state"] == "failed" and row["retryable"] == 1
        row = c.execute("SELECT * FROM jobs WHERE id=?", (successful["id"],)).fetchone()
        assert row["state"] == "succeeded"


@pytest.mark.parametrize(
    "method,path,body",
    (
        ("GET", "/api/manage/product", None),
        ("PUT", "/api/manage/product", {"name": "无权修改"}),
        ("GET", "/api/manage/cards", None),
        ("POST", "/api/manage/cards", {"count": 1}),
        ("GET", "/api/manage/events", None),
        ("GET", "/api/manage/links", None),
        (
            "POST",
            "/api/manage/links",
            {"name": "无权转授", "days": 1, "permissions": ["queue.view"]},
        ),
    ),
)
def test_queue_permission_does_not_grant_other_product_management(
    owner, setup_product, method, path, body
):
    pid, _ = setup_product()
    link = create_link(owner, pid, ["queue.view"])
    login_link(owner, link)
    if path == "/api/manage/cards" and method == "POST":
        body = {**body, "product_id": pid}
    before = snapshot("products", "cards", "staff", "audit")
    response = owner.request(method, path, json=body)
    assert response.status_code == 403, response.text
    assert snapshot("products", "cards", "staff", "audit") == before


def test_product_editor_is_scoped_and_keeps_delivery_restrictions(owner, setup_product):
    pid, _ = setup_product(name="可编辑商品")
    pid2, _ = setup_product(name="其他商品")
    link = create_link(owner, pid, ["product.edit"])
    login_link(owner, link)
    response = owner.get("/api/manage/product")
    assert response.status_code == 200, response.text
    product = response.json()
    assert product["id"] == pid
    response = owner.put(
        "/api/manage/product",
        json={**product, "name": "新的名称", "description": "说明"},
    )
    assert response.status_code == 200, response.text
    assert owner.get("/api/manage/product").json()["name"] == "新的名称"
    assert (
        owner.put(
            "/api/manage/product", json={**product, "view_policy": "once"}
        ).status_code
        == 403
    )
    for method in ("GET", "PUT"):
        response = owner.request(
            method,
            "/api/manage/product",
            params={"product_id": pid2},
            json=product if method == "PUT" else None,
        )
        assert response.status_code == 403, response.text
    with db() as c:
        assert (
            '"其他商品"'
            in c.execute("SELECT config FROM products WHERE id=?", (pid2,)).fetchone()[
                0
            ]
        )
    assert owner.get("/api/manage/jobs").status_code == 403


def test_card_management_cannot_cross_product_or_expose_digests(owner, setup_product):
    pid, _ = setup_product()
    pid2, _ = setup_product()
    own_id = owner.get("/api/admin/cards", params={"product_id": pid}).json()[0]["id"]
    other_id = owner.get("/api/admin/cards", params={"product_id": pid2}).json()[0][
        "id"
    ]
    link = create_link(owner, pid, ["cards.manage"])
    login_link(owner, link)
    assert owner.post("/api/manage/cards", json={"count": 1}).status_code == 422
    rows = owner.get("/api/manage/cards").json()
    assert [row["id"] for row in rows] == [own_id]
    assert all(set(row) == {"id", "product_id", "state", "created"} for row in rows)
    response = owner.post("/api/manage/cards", json={"product_id": pid, "count": 2})
    assert response.status_code == 200, response.text
    assert len(response.json()["codes"]) == 2
    before = snapshot("cards", "audit")
    assert (
        owner.get("/api/manage/cards", params={"product_id": pid2}).status_code == 403
    )
    assert (
        owner.post(
            "/api/manage/cards", json={"product_id": pid2, "count": 1}
        ).status_code
        == 403
    )
    assert (
        owner.post(f"/api/manage/cards/{other_id}/revoke", json={}).status_code == 403
    )
    assert snapshot("cards", "audit") == before
    assert owner.post(f"/api/manage/cards/{own_id}/revoke", json={}).status_code == 200
    with db() as c:
        assert (
            c.execute("SELECT state FROM cards WHERE id=?", (own_id,)).fetchone()[0]
            == "revoked"
        )
        assert (
            c.execute("SELECT state FROM cards WHERE id=?", (other_id,)).fetchone()[0]
            == "ready"
        )


def test_event_management_is_scoped_and_never_returns_webhook_secrets(
    owner, setup_product
):
    pid, code = setup_product(
        webhook_url="https://example.com/private-hook", webhook_secret=SECRET
    )
    redeem(owner, code)
    pid2, code2 = setup_product(
        webhook_url="https://example.com/other-hook", webhook_secret=SECRET
    )
    redeem(owner, code2)
    with db() as c:
        own_id = c.execute(
            "SELECT id FROM events WHERE product_id=?", (pid,)
        ).fetchone()[0]
        other_id = c.execute(
            "SELECT id FROM events WHERE product_id=?", (pid2,)
        ).fetchone()[0]
        c.execute("UPDATE outbox SET state='dead',attempts=8,error='HTTP 503'")
    link = create_link(owner, pid, ["events.manage"])
    login_link(owner, link)
    response = owner.get("/api/manage/events")
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()] == [own_id]
    assert SECRET not in response.text and "example.com" not in response.text
    assert all(
        not {"payload", "url", "secret"}.intersection(row) for row in response.json()
    )
    before = snapshot("outbox", "audit")
    assert (
        owner.get("/api/manage/events", params={"product_id": pid2}).status_code == 403
    )
    assert (
        owner.post(f"/api/manage/events/{other_id}/retry", json={}).status_code == 403
    )
    assert snapshot("outbox", "audit") == before
    assert owner.post(f"/api/manage/events/{own_id}/retry", json={}).status_code == 200
    with db() as c:
        own = c.execute("SELECT * FROM outbox WHERE id=?", (own_id,)).fetchone()
        other = c.execute("SELECT * FROM outbox WHERE id=?", (other_id,)).fetchone()
        assert own["state"] == "pending" and own["attempts"] == 0
        assert other["state"] == "dead" and other["attempts"] == 8


def test_delegation_requires_strictly_fewer_permissions_and_shorter_expiry(
    owner, setup_product
):
    pid, _ = setup_product()
    pid2, _ = setup_product()
    permissions = ["queue.view", "queue.process", "links.delegate"]
    parent = create_link(owner, pid, permissions, days=2)
    login_link(owner, parent)
    before = snapshot("staff", "sessions", "audit")
    for body in (
        {"permissions": permissions},
        {"permissions": [*permissions, "cards.manage"]},
        {"permissions": ["queue.view"], "days": 3},
        {"permissions": ["queue.view"], "product_id": pid2},
    ):
        response = owner.post(
            "/api/manage/links", json={"name": "越界授权", "days": 1, **body}
        )
        assert response.status_code == 403, response.text
        assert snapshot("staff", "sessions", "audit") == before
    child = delegate(owner, ["queue.view"], days=0.5, product_id=pid)
    assert_safe_link(child, pid, {"queue.view"})
    assert child["expires"] <= parent["expires"]
    assert child["expires"] < time.time() + 0.6 * 86400
    login_link(owner, child)
    assert owner.get("/api/manage/jobs").status_code == 200
    assert owner.get("/api/manage/links").status_code == 403


def test_links_only_list_and_revoke_own_descendants(owner, setup_product):
    pid, _ = setup_product()
    parent = create_link(owner, pid, sorted(ALL_PERMISSIONS), days=3)
    sibling = create_link(owner, pid, ["queue.view", "links.delegate"], days=3)
    parent_session = login_link(owner, parent)
    child = delegate(owner, ["queue.view", "queue.process", "links.delegate"])
    child_session = login_link(owner, child)
    grandchild = delegate(owner, ["queue.view"], days=0.5)
    rows = owner.get("/api/manage/links").json()
    assert {row["id"] for row in rows} == {grandchild["id"]}
    assert all("url" not in row and "digest" not in row for row in rows)
    before = snapshot("staff", "sessions", "audit")
    for outsider in (parent, sibling, child):
        response = owner.post(f"/api/manage/links/{outsider['id']}/revoke", json={})
        assert response.status_code in (403, 404), response.text
        assert snapshot("staff", "sessions", "audit") == before
    use_session(owner, parent_session)
    rows = owner.get("/api/manage/links").json()
    assert {row["id"] for row in rows} == {child["id"], grandchild["id"]}
    assert sibling["id"] not in str(rows)
    assert (
        owner.post(f"/api/manage/links/{child['id']}/revoke", json={}).status_code
        == 200
    )
    use_session(owner, child_session)
    assert owner.get("/api/manage/products").status_code == 401
    owner.cookies.clear()
    assert (
        owner.post(
            "/api/staff/login", json={"token": grandchild["url"].split("#", 1)[1]}
        ).status_code
        == 401
    )


def test_admin_revocation_invalidates_descendant_sessions_and_requeues_claims(
    owner, setup_product
):
    pid, code = setup_product()
    _, task = redeem(owner, code)
    parent = create_link(owner, pid, sorted(ALL_PERMISSIONS), days=3)
    parent_session = login_link(owner, parent)
    child = delegate(owner, ["queue.view", "queue.process", "links.delegate"])
    child_session = login_link(owner, child)
    grandchild = delegate(owner, ["queue.view", "queue.process"], days=0.5)
    grandchild_session = login_link(owner, grandchild)
    assert (
        owner.post(
            "/api/manage/batch",
            json={"product_id": pid, "ids": [task["id"]], "action": "claim"},
        ).status_code
        == 200
    )
    as_owner(owner)
    assert (
        owner.post(f"/api/admin/staff/{parent['id']}/revoke", json={}).status_code
        == 200
    )
    with db() as c:
        assert not c.execute(
            "SELECT 1 FROM sessions WHERE revoked=0 AND staff_id IN (?,?,?)",
            (parent["id"], child["id"], grandchild["id"]),
        ).fetchone()
        task_row = c.execute("SELECT * FROM jobs WHERE id=?", (task["id"],)).fetchone()
        assert task_row["state"] == "queued"
        assert task_row["claimed_by"] is None and task_row["lease"] is None
    for value in (parent_session, child_session, grandchild_session):
        use_session(owner, value)
        assert owner.get("/api/manage/products").status_code == 401
    owner.cookies.clear()
    assert (
        owner.post(
            "/api/staff/login", json={"token": grandchild["url"].split("#", 1)[1]}
        ).status_code
        == 401
    )


@pytest.mark.parametrize("invalidates", ("expires", "revoked"))
def test_ancestor_changes_invalidate_live_descendant_sessions(
    owner, setup_product, invalidates
):
    pid, _ = setup_product()
    parent = create_link(
        owner, pid, ["queue.view", "cards.manage", "links.delegate"], days=3
    )
    login_link(owner, parent)
    child = delegate(owner, ["cards.manage", "links.delegate"])
    login_link(owner, child)
    grandchild = delegate(owner, ["cards.manage"], days=0.5)
    login_link(owner, grandchild)
    effective_expiry = time.time() + 600
    with db() as c:
        c.execute(
            "UPDATE staff SET expires=? WHERE id=?", (effective_expiry, parent["id"])
        )
    status = owner.get("/api/auth/status").json()
    assert status["role"] == "staff" and status["link_expires"] == effective_expiry
    with db() as c:
        if invalidates == "expires":
            c.execute(
                "UPDATE staff SET expires=? WHERE id=?", (time.time() - 1, parent["id"])
            )
        else:
            c.execute("UPDATE staff SET revoked=1 WHERE id=?", (parent["id"],))
    assert owner.get("/api/manage/products").status_code == 401
    assert owner.get("/api/manage/cards").status_code == 401
    assert owner.get("/api/auth/status").json()["role"] is None
    owner.cookies.clear()
    assert (
        owner.post(
            "/api/staff/login", json={"token": grandchild["url"].split("#", 1)[1]}
        ).status_code
        == 401
    )


def test_queue_link_cannot_revoke_cards_links_or_retry_events(owner, setup_product):
    pid, code = setup_product(
        webhook_url="https://example.com/hooks", webhook_secret=SECRET
    )
    redeem(owner, code)
    owner.post("/api/admin/cards", json={"product_id": pid, "count": 1})
    with db() as c:
        cid = c.execute("SELECT id FROM cards WHERE state='ready'").fetchone()[0]
        eid = c.execute("SELECT id FROM outbox").fetchone()[0]
        c.execute("UPDATE outbox SET state='dead'")
    target = create_link(owner, pid, ["queue.view"])
    viewer = create_link(owner, pid, ["queue.view"])
    login_link(owner, viewer)
    before = snapshot("cards", "staff", "outbox", "sessions", "audit")
    for path in (
        f"/api/manage/cards/{cid}/revoke",
        f"/api/manage/events/{eid}/retry",
        f"/api/manage/links/{target['id']}/revoke",
    ):
        response = owner.post(path, json={})
        assert response.status_code == 403, response.text
        assert snapshot("cards", "staff", "outbox", "sessions", "audit") == before


@pytest.mark.parametrize("days", (0, -1, 90.01))
def test_invalid_link_lifetimes_do_not_create_links(owner, setup_product, days):
    pid, _ = setup_product()
    parent = create_link(owner, pid, ["queue.view", "links.delegate"])
    before = snapshot("staff", "audit")
    response = owner.post(
        "/api/admin/staff",
        json={
            "product_id": pid,
            "name": "错误期限",
            "permissions": ["queue.view"],
            "days": days,
        },
    )
    assert response.status_code == 422, response.text
    assert snapshot("staff", "audit") == before
    login_link(owner, parent)
    before = snapshot("staff", "audit")
    response = owner.post(
        "/api/manage/links",
        json={"name": "错误期限", "permissions": ["queue.view"], "days": days},
    )
    assert response.status_code == 422, response.text
    assert snapshot("staff", "audit") == before


@pytest.mark.parametrize("days", (0.25, 10))
def test_default_child_expiry_uses_seven_days_or_parent_remaining(
    owner, setup_product, days
):
    pid, _ = setup_product()
    parent = create_link(owner, pid, ["queue.view", "links.delegate"], days=days)
    login_link(owner, parent)
    started = time.time()
    response = owner.post(
        "/api/manage/links",
        json={"name": "使用默认期限", "permissions": ["queue.view"]},
    )
    assert response.status_code == 200, response.text
    child = response.json()
    assert_safe_link(child, pid, {"queue.view"})
    assert child["expires"] <= parent["expires"]
    if days < 7:
        assert child["expires"] == parent["expires"]
    else:
        assert started + 7 * 86400 <= child["expires"] <= time.time() + 7 * 86400


def test_revocation_leaves_independent_same_and_other_product_links_active(
    owner, setup_product
):
    pid, code = setup_product()
    _, target_job = redeem(owner, code)
    another_code = owner.post(
        "/api/admin/cards", json={"product_id": pid, "count": 1}
    ).json()["codes"][0]
    _, sibling_job = redeem(owner, another_code)
    pid2, code2 = setup_product()
    _, foreign_job = redeem(owner, code2)
    parent = create_link(owner, pid, sorted(ALL_PERMISSIONS), days=3)
    sibling = create_link(owner, pid)
    foreign = create_link(owner, pid2)
    login_link(owner, parent)
    child = delegate(owner, ["queue.view", "queue.process"])
    child_session = login_link(owner, child)
    for link, task in (
        (child, target_job),
        (sibling, sibling_job),
        (foreign, foreign_job),
    ):
        value = login_link(owner, link)
        link["session"] = value
        assert (
            owner.post(
                "/api/manage/batch",
                json={
                    "product_id": task["product_id"],
                    "ids": [task["id"]],
                    "action": "claim",
                },
            ).status_code
            == 200
        )
    as_owner(owner)
    assert (
        owner.post(f"/api/admin/staff/{parent['id']}/revoke", json={}).status_code
        == 200
    )
    use_session(owner, child_session)
    assert owner.get("/api/manage/products").status_code == 401
    for link, task in ((sibling, sibling_job), (foreign, foreign_job)):
        use_session(owner, link["session"])
        response = owner.get("/api/manage/jobs")
        assert response.status_code == 200, response.text
        own_task = next(row for row in response.json() if row["id"] == task["id"])
        assert own_task["state"] == "processing"
        with db() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (task["id"],)).fetchone()
            assert row["claimed_by"] == link["id"] and row["lease"] is not None


@pytest.mark.parametrize(
    "operation",
    (
        "products",
        "product",
        "cards",
        "card.revoke",
        "event.retry",
        "links",
        "link.revoke",
    ),
)
def test_ancestor_revocation_before_management_transaction_prevents_access(
    owner, setup_product, monkeypatch, operation
):
    import extore.app as app_module

    pid, code = setup_product(
        webhook_url="https://example.com/hooks", webhook_secret=SECRET
    )
    redeem(owner, code)
    owner.post("/api/admin/cards", json={"product_id": pid, "count": 1})
    with db() as c:
        cid = c.execute("SELECT id FROM cards WHERE state='ready'").fetchone()[0]
        eid = c.execute("SELECT id FROM outbox").fetchone()[0]
        c.execute("UPDATE outbox SET state='dead'")
    parent = create_link(owner, pid, sorted(ALL_PERMISSIONS), days=3)
    login_link(owner, parent)
    child = delegate(
        owner,
        [
            "queue.view",
            "product.edit",
            "cards.manage",
            "events.manage",
            "links.delegate",
        ],
    )
    child_session = login_link(owner, child)
    grandchild = delegate(owner, ["queue.view"], days=0.5)
    use_session(owner, child_session)
    real_session = app_module.session

    def revoked_after_authentication(*args, **kwargs):
        result = real_session(*args, **kwargs)
        with db() as c:
            c.execute("UPDATE staff SET revoked=1 WHERE id=?", (parent["id"],))
        return result

    monkeypatch.setattr(app_module, "session", revoked_after_authentication)
    before = snapshot("products", "cards", "outbox", "events", "sessions", "audit")
    if operation == "product":
        response = owner.put("/api/manage/product", json={"name": "不应写入"})
    elif operation == "cards":
        response = owner.post("/api/manage/cards", json={"product_id": pid, "count": 2})
    elif operation == "card.revoke":
        response = owner.post(f"/api/manage/cards/{cid}/revoke", json={})
    elif operation == "event.retry":
        response = owner.post(f"/api/manage/events/{eid}/retry", json={})
    elif operation == "links":
        response = owner.post(
            "/api/manage/links",
            json={"name": "不应转授", "days": 0.1, "permissions": ["queue.view"]},
        )
    elif operation == "link.revoke":
        response = owner.post(f"/api/manage/links/{grandchild['id']}/revoke", json={})
    else:
        response = owner.get("/api/manage/products")
    assert response.status_code == 401, response.text
    assert (
        snapshot("products", "cards", "outbox", "events", "sessions", "audit") == before
    )


def test_product_editor_preserves_hidden_secret_and_cannot_sign_callbacks(
    owner, setup_product
):
    pid, code = setup_product(
        mode="webhook",
        webhook_url="https://example.com/private-hook",
        webhook_secret=SECRET,
    )
    _, task = redeem(owner, code)
    editor = create_link(owner, pid, ["product.edit"])
    login_link(owner, editor)
    response = owner.get("/api/manage/product")
    assert response.status_code == 200, response.text
    config = response.json()
    assert config["webhook_secret"] == "" and SECRET not in response.text
    updates = {
        **config,
        "name": "修改后的商品",
        "description": "安全更新商品说明",
        "parameters": [
            {"key": "email", "label": {"zh-CN": "收货邮箱"}, "type": "email"}
        ],
    }
    response = owner.put("/api/manage/product", json=updates)
    assert response.status_code == 200, response.text
    assert response.json()["webhook_secret"] == "" and SECRET not in response.text
    response = owner.get("/api/manage/product")
    assert response.json()["webhook_secret"] == "" and SECRET not in response.text
    with db() as c:
        stored = json.loads(
            c.execute("SELECT config FROM products WHERE id=?", (pid,)).fetchone()[0]
        )
    assert stored["webhook_secret"] == SECRET
    assert (
        stored["name"] == updates["name"]
        and stored["description"] == updates["description"]
    )
    assert stored["allow_retry"] == config["allow_retry"]
    assert stored["max_attempts"] == config["max_attempts"]
    assert stored["parameters"][0]["label"] == {"zh-CN": "收货邮箱"}

    body = json.dumps(
        {"state": "succeeded", "attempt": 1, "content": "伪造交付"}
    ).encode()
    timestamp, nonce = str(int(time.time())), "editor-forged-callback"
    before = snapshot("jobs", "events", "audit")
    response = owner.post(
        f"/api/callbacks/{pid}/{task['id']}",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Extore-Timestamp": timestamp,
            "X-Extore-Nonce": nonce,
            "X-Extore-Signature": sign(
                config["webhook_secret"], timestamp, nonce, body
            ),
        },
    )
    assert response.status_code == 401, response.text
    assert snapshot("jobs", "events", "audit") == before


@pytest.mark.parametrize(
    "changes",
    (
        {"mode": "manual"},
        {"delivery": "service", "outputs": []},
        {"view_policy": "once"},
        {"webhook_url": "https://example.com/replaced-hook"},
        {"webhook_secret": "a-new-private-webhook-secret-32-characters"},
        {"mode": "manual", "webhook_url": ""},
        {"allow_retry": False},
        {"max_attempts": 5},
    ),
)
def test_product_editor_cannot_change_fulfillment_configuration(
    owner, setup_product, changes
):
    pid, _ = setup_product(
        mode="webhook",
        webhook_url="https://example.com/private-hook",
        webhook_secret=SECRET,
    )
    editor = create_link(owner, pid, ["product.edit"])
    login_link(owner, editor)
    config = owner.get("/api/manage/product").json()
    before = snapshot("products", "audit")
    response = owner.put("/api/manage/product", json={**config, **changes})
    assert response.status_code == 403, response.text
    assert snapshot("products", "audit") == before


def test_full_product_manager_can_configure_fulfillment_before_cards_are_issued(owner):
    response = owner.post("/api/admin/products", json={"name": "未发行的商品"})
    assert response.status_code == 200, response.text
    pid = response.json()["id"]
    manager = create_link(owner, pid, sorted(ALL_PERMISSIONS))
    assert_safe_link(manager, pid, ALL_PERMISSIONS)
    login_link(owner, manager)
    initial = owner.get("/api/manage/product").json()
    response = owner.put(
        "/api/manage/product",
        json={
            **initial,
            "mode": "webhook",
            "view_policy": "once",
            "webhook_url": "https://example.com/approved-hook",
            "webhook_secret": SECRET,
            "allow_retry": False,
            "max_attempts": 5,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "webhook"
    assert response.json()["delivery"] == "content"
    assert response.json()["view_policy"] == "once"
    assert response.json()["script"] == ""
    assert response.json()["webhook_secret"] == SECRET
    assert response.json()["allow_retry"] is False
    assert response.json()["max_attempts"] == 5
    configured = owner.get("/api/manage/product").json()
    response = owner.put(
        "/api/manage/product", json={**configured, "delivery": "service", "outputs": []}
    )
    assert response.status_code == 200, response.text
    assert response.json()["delivery"] == "service" and response.json()["outputs"] == []
    response = owner.put(
        "/api/manage/product",
        json={**response.json(), "delivery": "content", "outputs": initial["outputs"]},
    )
    assert response.status_code == 200, response.text
    configured = response.json()
    assert configured["webhook_secret"] == SECRET
    assert (
        owner.post(
            "/api/manage/cards", json={"product_id": pid, "count": 1}
        ).status_code
        == 200
    )
    before = snapshot("products", "audit")
    for changes in (
        {"mode": "manual"},
        {"delivery": "service", "outputs": []},
        {"view_policy": "repeat"},
        {"webhook_secret": "another-private-webhook-secret-32-characters"},
        {"parameters": [{"key": "name", "label": {"zh-CN": "姓名"}}]},
        {
            "outputs": [
                {"key": "content", "label": {"zh-CN": "交付内容"}, "type": "text"}
            ]
        },
    ):
        response = owner.put("/api/manage/product", json={**configured, **changes})
        assert response.status_code == 409, response.text
        assert snapshot("products", "audit") == before


def test_fulfillment_configuration_permission_does_not_require_queue_permissions(owner):
    response = owner.post(
        "/api/admin/products",
        json={
            "name": "专职自动化配置",
            "mode": "webhook",
            "webhook_url": "https://example.com/original-hook",
            "webhook_secret": SECRET,
        },
    )
    assert response.status_code == 200, response.text
    pid = response.json()["id"]
    manager = create_link(owner, pid, ["product.edit", "fulfillment.configure"])
    login_link(owner, manager)
    response = owner.get("/api/manage/product")
    assert response.status_code == 200, response.text
    config = response.json()
    assert config["webhook_secret"] == SECRET
    response = owner.put(
        "/api/manage/product",
        json={**config, "webhook_url": "https://example.com/replacement-hook"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["webhook_url"] == "https://example.com/replacement-hook"
    assert response.json()["webhook_secret"] == SECRET
    response = owner.put(
        "/api/manage/product",
        json={
            **response.json(),
            "webhook_secret": "",
            "description": "保留现有签名密钥",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["webhook_secret"] == SECRET
    with db() as c:
        config = json.loads(
            c.execute("SELECT config FROM products WHERE id=?", (pid,)).fetchone()[0]
        )
        assert config["webhook_secret"] == SECRET
    assert owner.get("/api/manage/jobs").status_code == 403
    assert (
        owner.post(
            "/api/manage/cards", json={"product_id": pid, "count": 1}
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "changes",
    (
        {"mode": "webhook", "webhook_secret": SECRET},
        {
            "mode": "webhook",
            "webhook_url": "http://example.com/hooks",
            "webhook_secret": SECRET,
        },
        {
            "mode": "webhook",
            "webhook_url": "https://example.com/hooks",
            "webhook_secret": "short",
        },
        {"mode": "script", "script": ""},
    ),
)
def test_fulfillment_configuration_is_fully_validated_before_writing(owner, changes):
    response = owner.post("/api/admin/products", json={"name": "配置校验商品"})
    assert response.status_code == 200, response.text
    pid = response.json()["id"]
    manager = create_link(owner, pid, ["product.edit", "fulfillment.configure"])
    login_link(owner, manager)
    config = owner.get("/api/manage/product").json()
    before = snapshot("products", "audit")
    response = owner.put("/api/manage/product", json={**config, **changes})
    assert response.status_code == 422, response.text
    assert snapshot("products", "audit") == before


@pytest.mark.parametrize("processor_id,private_config", PROCESSOR_CONFIGS)
def test_product_editor_preserves_encrypted_processor_configuration(
    owner, processor_id, private_config
):
    response = owner.post(
        "/api/admin/products",
        json={
            "name": "商品处理器商品",
            "mode": "script",
            "processor_id": processor_id,
            "processor_config": private_config,
        },
    )
    assert response.status_code == 200, response.text
    pid = response.json()["id"]
    assert (
        owner.post("/api/admin/cards", json={"product_id": pid, "count": 1}).status_code
        == 200
    )
    editor = create_link(owner, pid, ["product.edit"])
    manager = create_link(owner, pid, ["product.edit", "fulfillment.configure"])
    login_link(owner, editor)
    response = owner.get("/api/manage/product")
    assert response.status_code == 200, response.text
    config = response.json()
    assert config["processor_config"] == {}
    assert all(value not in response.text for value in private_config.values())
    response = owner.put(
        "/api/manage/product",
        json={**config, "name": "仅修改商品名称", "description": "公开的商品说明"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "仅修改商品名称"
    assert response.json()["processor_config"] == {}
    assert all(value not in response.text for value in private_config.values())
    with db() as c:
        stored = json.loads(
            c.execute("SELECT config FROM products WHERE id=?", (pid,)).fetchone()[0]
        )
        bound = dict(
            c.execute(
                "SELECT * FROM processor_product_bindings WHERE product_id=?", (pid,)
            ).fetchone()
        )
        revision = c.execute(
            "SELECT ciphertext FROM processor_profile_revisions WHERE profile_id=? AND revision=?",
            (bound["profile_id"], bound["revision"]),
        ).fetchone()[0]
        card = c.execute(
            "SELECT id FROM cards WHERE product_id=? LIMIT 1", (pid,)
        ).fetchone()[0]
        from extore.processor_profiles import runtime_configuration

        configuration, _ = runtime_configuration(
            c, {"product_id": pid, "card_id": card}, processor_id
        )
        assert configuration == private_config
    assert stored["processor_config"] == {}
    assert all(value not in revision for value in private_config.values())
    assert stored["processor_id"] == processor_id
    changed_config = (
        {
            "resource_url": "https://example.com/replaced-private-resource",
            "message": "新的私有说明",
        }
        if processor_id == "resource_link"
        else {"template": "未经授权的配置变更：$name"}
    )
    before = snapshot("products", "audit")
    response = owner.put(
        "/api/manage/product", json={**config, "processor_config": changed_config}
    )
    assert response.status_code == 403, response.text
    assert snapshot("products", "audit") == before
    specifications = {
        spec["id"]: spec for spec in owner.get("/api/manage/processors").json()
    }
    other_id, other_config = next(
        item for item in PROCESSOR_CONFIGS if item[0] != processor_id
    )
    response = owner.put(
        "/api/manage/product",
        json={
            **config,
            "processor_id": other_id,
            "processor_config": other_config,
            "parameters": specifications[other_id]["parameters"],
            "outputs": specifications[other_id]["outputs"],
        },
    )
    assert response.status_code == 403, response.text
    assert snapshot("products", "audit") == before
    login_link(owner, manager)
    response = owner.get("/api/manage/product")
    assert response.status_code == 200, response.text
    assert response.json()["processor_config"] == {}
    assert all(value not in response.text for value in private_config.values())
    before = snapshot(
        "products", "audit", "processor_product_bindings", "processor_profile_revisions"
    )
    response = owner.put(
        "/api/manage/product",
        json={**response.json(), "processor_config": changed_config},
    )
    assert response.status_code == 403, response.text
    assert (
        snapshot(
            "products",
            "audit",
            "processor_product_bindings",
            "processor_profile_revisions",
        )
        == before
    )


@pytest.mark.parametrize("processor_id,private_config", PROCESSOR_CONFIGS)
def test_full_manager_cannot_select_processor_accounts_and_owner_freezes_issued_processor(
    owner, processor_id, private_config
):
    response = owner.post("/api/admin/products", json={"name": "待选择商品处理器"})
    assert response.status_code == 200, response.text
    pid = response.json()["id"]
    manager = create_link(owner, pid, sorted(ALL_PERMISSIONS))
    login_link(owner, manager)
    specifications = {
        spec["id"]: spec for spec in owner.get("/api/manage/processors").json()
    }
    config = owner.get("/api/manage/product").json()
    config = {
        key: value
        for key, value in config.items()
        if key
        not in ("parameters", "outputs", "variants", "progress_steps", "support_email")
    }
    processor_config = {
        **config,
        "mode": "script",
        "processor_id": processor_id,
        "processor_config": private_config,
    }
    before = snapshot("products", "audit", "processor_product_bindings")
    response = owner.put(
        "/api/manage/product",
        json=processor_config,
    )
    assert response.status_code == 403, response.text
    assert snapshot("products", "audit", "processor_product_bindings") == before
    as_owner(owner)
    response = owner.put("/api/admin/products/" + pid, json=processor_config)
    assert response.status_code == 200, response.text
    config = response.json()
    assert config["script"] == ""
    assert config["processor_id"] == processor_id
    assert config["processor_config"] == {
        key: value
        for key, value in private_config.items()
        if key == ("message" if processor_id == "resource_link" else "template")
    }
    if processor_id == "resource_link":
        assert private_config["resource_url"] not in response.text
    assert config["parameters"] == specifications[processor_id]["parameters"]
    assert config["outputs"] == specifications[processor_id]["outputs"]
    assert (
        owner.post("/api/admin/cards", json={"product_id": pid, "count": 1}).status_code
        == 200
    )
    other_id, other_config = next(
        item for item in PROCESSOR_CONFIGS if item[0] != processor_id
    )
    before = snapshot("products", "audit")
    response = owner.put(
        "/api/admin/products/" + pid,
        json={
            **config,
            "processor_id": other_id,
            "processor_config": other_config,
            "parameters": specifications[other_id]["parameters"],
            "outputs": specifications[other_id]["outputs"],
        },
    )
    assert response.status_code == 409, response.text
    assert snapshot("products", "audit") == before
    login_link(owner, manager)
    response = owner.get("/api/manage/product")
    assert response.status_code == 200, response.text
    assert response.json()["processor_config"] == {}
    response = owner.put(
        "/api/manage/product",
        json={**response.json(), "processor_config": private_config},
    )
    assert response.status_code == 403, response.text


@pytest.mark.parametrize(
    "permissions", (["product.edit"], ["product.edit", "fulfillment.configure"])
)
def test_arbitrary_processor_filenames_are_rejected_even_with_configuration_permission(
    owner, setup_product, permissions
):
    pid, _ = setup_product()
    link = create_link(owner, pid, permissions)
    login_link(owner, link)
    config = owner.get("/api/manage/product").json()
    before = snapshot("products", "audit")
    response = owner.put("/api/manage/product", json={**config, "script": "welcome"})
    assert response.status_code == 422, response.text
    assert snapshot("products", "audit") == before


def test_official_processor_catalog_requires_product_edit_permission(
    owner, setup_product
):
    pid, _ = setup_product()
    viewer = create_link(owner, pid, ["queue.view"])
    editor = create_link(owner, pid, ["product.edit"])
    login_link(owner, viewer)
    assert owner.get("/api/manage/processors").status_code == 403
    login_link(owner, editor)
    response = owner.get("/api/manage/processors")
    assert response.status_code == 200, response.text
    assert {spec["id"] for spec in response.json()} == {
        "resource_link",
        "personalized_text",
    }
    assert owner.get("/api/admin/processors").status_code == 401
    owner.cookies.clear()
    assert owner.get("/api/manage/processors").status_code == 401


@pytest.mark.parametrize("field", ("parameters", "outputs"))
@pytest.mark.parametrize(
    "attribute,value", (("key", "replacement"), ("type", "text"), ("required", False))
)
def test_issued_queue_can_change_future_schema_without_changing_existing_task(
    owner, setup_product, field, attribute, value
):
    pid, code = setup_product()
    _, task = redeem(owner, code)
    manager = create_link(owner, pid, ["product.edit", "fulfillment.configure"])
    login_link(owner, manager)
    config = owner.get("/api/manage/product").json()
    changed = [{**item, attribute: value} for item in config[field]]
    before = snapshot("products", "audit")
    response = owner.put("/api/manage/product", json={**config, field: changed})
    assert response.status_code == 200, response.text
    assert response.json()[field] == changed
    with db() as c:
        raw = c.execute(
            "SELECT schema_snapshot FROM jobs WHERE id=?", (task["id"],)
        ).fetchone()[0]
    assert json.loads(raw)[field] == config[field]
    assert snapshot("products", "audit") != before


def test_issued_manual_product_field_labels_and_tutorials_remain_editable(
    owner, setup_product
):
    pid, code = setup_product()
    _, task = redeem(owner, code)
    editor = create_link(owner, pid, ["product.edit"])
    worker = create_link(owner, pid)
    login_link(owner, editor)
    config = owner.get("/api/manage/product").json()
    changes = {
        field: [
            {
                **item,
                "label": {"zh-CN": "更新后的显示名称"},
                "description": {"zh-CN": "**更新后的教程**"},
                "collapsed": False,
            }
            for item in config[field]
        ]
        for field in ("parameters", "outputs")
    }
    response = owner.put("/api/manage/product", json={**config, **changes})
    assert response.status_code == 200, response.text
    for field in changes:
        assert response.json()[field] == changes[field]
    assert owner.get("/api/manage/jobs").status_code == 403
    login_link(owner, worker)
    response = owner.get("/api/manage/jobs")
    assert response.status_code == 200, response.text
    queued = next(row for row in response.json() if row["id"] == task["id"])
    assert queued["params"] == {"email": "user@example.com"}
    assert (
        owner.post(
            "/api/manage/batch",
            json={"product_id": pid, "ids": [task["id"]], "action": "claim"},
        ).status_code
        == 200
    )
    assert (
        owner.post(
            "/api/manage/batch",
            json={
                "product_id": pid,
                "ids": [task["id"]],
                "action": "succeed",
                "content": "按新的说明完成交付",
            },
        ).status_code
        == 200
    )

"""Merchant credentials are bounded by server-side product ownership."""

import json
import time
import uuid
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response

from extore.app import app
from extore.db import db, set_setting
from extore.security import card_digest, create_session


@pytest.fixture
def stores(owner):
    result = {"root": owner}
    with ExitStack() as stack:
        for name in ("alpha", "beta"):
            sid = str(uuid.uuid4())
            with db() as c:
                c.execute(
                    "INSERT INTO shops(id,name,email,created,verified) VALUES (?,?,?,?,1)",
                    (sid, name, sid + "@example.com", time.time()),
                )
                value = create_session(c, Response(), "admin", shop_id=sid)
            merchant = stack.enter_context(
                TestClient(
                    app,
                    base_url="http://localhost:8000",
                    headers={"Origin": "http://localhost:8000"},
                )
            )
            merchant.cookies.set("extore_session", value)
            response = merchant.post(
                "/api/admin/products",
                json={"name": name + " 商品", "public": True},
            )
            assert response.status_code == 200, response.text
            pid = response.json()["id"]
            codes = merchant.post(
                "/api/admin/cards", json={"product_id": pid, "count": 2}
            ).json()["codes"]
            receipt = merchant.post("/api/exchange", json={"code": codes[0]}).json()
            submitted = merchant.post(
                "/api/redeem", json={"token": receipt["token"], "params": {}}
            )
            assert submitted.status_code == 200, submitted.text
            link = merchant.post(
                "/api/admin/staff", json={"product_id": pid, "name": name + " 管理"}
            )
            assert link.status_code == 200, link.text
            with db() as c:
                cid = c.execute(
                    "SELECT id FROM cards WHERE digest=?", (card_digest(codes[1]),)
                ).fetchone()["id"]
                event = c.execute(
                    "SELECT id FROM events WHERE product_id=? ORDER BY created LIMIT 1",
                    (pid,),
                ).fetchone()["id"]
                c.execute(
                    "INSERT OR REPLACE INTO outbox(id,url,secret,due,state) VALUES (?,?,?,?,'dead')",
                    (event, "https://example.com/hook", "s" * 32, time.time()),
                )
            result[name] = {
                "client": merchant,
                "shop": sid,
                "product": pid,
                "codes": codes,
                "receipt": receipt["token"],
                "job": submitted.json()["id"],
                "card": cid,
                "link": link.json(),
                "event": event,
            }
        yield result


@pytest.mark.parametrize(
    ("path", "id_field", "object_field"),
    (
        ("/api/admin/products", "id", "product"),
        ("/api/manage/products", "id", "product"),
        ("/api/admin/cards", "product_id", "product"),
        ("/api/admin/staff", "product_id", "product"),
        ("/api/admin/events", "id", "event"),
    ),
)
def test_merchant_lists_never_include_another_shop(
    stores, path, id_field, object_field
):
    alpha, beta = stores["alpha"], stores["beta"]
    rows = alpha["client"].get(path).json()
    assert rows and all(row[id_field] != beta[object_field] for row in rows)
    if object_field == "product":
        assert all(row[id_field] == alpha["product"] for row in rows)
    if "products" in path:
        compact = alpha["client"].get(path + "?compact=true").json()
        assert [row["id"] for row in compact] == [alpha["product"]]
    root_rows = stores["root"].get(path).json()
    assert any(row[id_field] == beta[object_field] for row in root_rows)
    assert stores["root"].get(path + "?shop_id=" + alpha["shop"]).json() == rows
    assert alpha["client"].get(path + "?shop_id=" + beta["shop"]).status_code == 403


@pytest.mark.parametrize(
    ("method", "path", "body"),
    (
        ("GET", "/api/admin/cards?product_id={product}", None),
        ("GET", "/api/manage/product?product_id={product}", None),
        ("GET", "/api/manage/jobs?product_id={product}", None),
        ("GET", "/api/manage/cards?product_id={product}", None),
        ("GET", "/api/manage/events?product_id={product}", None),
        ("GET", "/api/manage/links?product_id={product}", None),
        ("PUT", "/api/admin/products/{product}", {"name": "越权修改"}),
        ("POST", "/api/admin/cards", {"product_id": "{product}", "count": 1}),
        ("POST", "/api/admin/cards/{card}/revoke", {}),
        ("POST", "/api/admin/staff", {"product_id": "{product}", "name": "越权链接"}),
        ("POST", "/api/admin/staff/{link}/revoke", {}),
        ("POST", "/api/admin/events/{event}/retry", {}),
        ("POST", "/api/manage/cards", {"product_id": "{product}", "count": 1}),
        (
            "POST",
            "/api/manage/batch",
            {"product_id": "{product}", "ids": ["{job}"], "action": "claim"},
        ),
        ("POST", "/api/manage/links", {"product_id": "{product}", "name": "越权委派"}),
        (
            "POST",
            "/api/admin/products/quick",
            {"template_id": "existing_product", "from_product_id": "{product}"},
        ),
    ),
)
def test_foreign_resource_ids_do_not_grant_access(stores, method, path, body):
    beta = {**stores["beta"], "link": stores["beta"]["link"]["id"]}
    url = path.format(**beta)
    payload = (
        json.loads(
            json.dumps(body)
            .replace("{product}", beta["product"])
            .replace("{job}", beta["job"])
        )
        if body is not None
        else None
    )
    response = stores["alpha"]["client"].request(method, url, json=payload)
    assert response.status_code == 403, response.text
    with db() as c:
        assert (
            json.loads(
                c.execute(
                    "SELECT config FROM products WHERE id=?", (beta["product"],)
                ).fetchone()[0]
            )["name"]
            == "beta 商品"
        )
        assert (
            c.execute("SELECT state FROM cards WHERE id=?", (beta["card"],)).fetchone()[
                0
            ]
            == "ready"
        )
        assert (
            c.execute("SELECT state FROM jobs WHERE id=?", (beta["job"],)).fetchone()[0]
            == "queued"
        )
        assert (
            c.execute(
                "SELECT revoked FROM staff WHERE id=?", (beta["link"],)
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT state FROM outbox WHERE id=?", (beta["event"],)
            ).fetchone()[0]
            == "dead"
        )


def test_merchant_cannot_select_foreign_shop_on_creation(stores):
    alpha, beta = stores["alpha"], stores["beta"]
    for path, payload in (
        ("/api/admin/products", {"name": "越权创建"}),
        ("/api/admin/products/quick", {"template_id": "manual_service"}),
    ):
        response = alpha["client"].post(path + "?shop_id=" + beta["shop"], json=payload)
        assert response.status_code == 403, response.text
        response = stores["root"].post(path + "?shop_id=" + beta["shop"], json=payload)
        assert response.status_code == 200, response.text
        item = response.json().get("product", response.json())
        assert item["shop_id"] == beta["shop"]
    assert (
        stores["root"]
        .post(
            "/api/admin/products?shop_id=" + str(uuid.uuid4()), json={"name": "missing"}
        )
        .status_code
        == 401
    )


def test_quick_clone_is_same_shop_and_resets_secret_authority(stores):
    alpha, beta = stores["alpha"], stores["beta"]
    config = {
        "name": "hook",
        "mode": "webhook",
        "webhook_url": "https://example.com/hook",
        "webhook_secret": "x" * 32,
    }
    source = alpha["client"].post("/api/admin/products", json=config).json()
    cloned = alpha["client"].post(
        "/api/admin/products/quick",
        json={"template_id": "existing_product", "from_product_id": source["id"]},
    )
    assert cloned.status_code == 200, cloned.text
    copy = cloned.json()["product"]
    assert copy["shop_id"] == alpha["shop"] and copy["public"] is False
    assert copy["webhook_secret"] != config["webhook_secret"]
    assert copy["processor_config"] == {}
    cross = stores["root"].post(
        "/api/admin/products/quick?shop_id=" + beta["shop"],
        json={"template_id": "existing_product", "from_product_id": source["id"]},
    )
    assert cross.status_code == 403


def test_public_shop_filter_never_exposes_credentials(stores):
    alpha, beta = stores["alpha"], stores["beta"]
    response = alpha["client"].get("/api/products?shop_id=" + alpha["shop"])
    assert [p["id"] for p in response.json()] == [alpha["product"]]
    assert beta["product"] not in json.dumps(response.json())
    for product in response.json():
        assert (
            not {
                "shop_id",
                "processor_config",
                "processor_profile_id",
                "webhook_secret",
            }
            & product.keys()
        )
    assert (
        alpha["client"].get("/api/products?shop_id=" + str(uuid.uuid4())).status_code
        == 404
    )


def test_owner_queue_claim_audit_identifies_shop_and_cannot_mix_jobs(stores):
    alpha, beta = stores["alpha"], stores["beta"]
    assert (
        stores["root"]
        .get(
            "/api/manage/jobs?product_id="
            + beta["product"]
            + "&shop_id="
            + alpha["shop"]
        )
        .status_code
        == 403
    )
    response = alpha["client"].post(
        "/api/manage/batch",
        json={
            "product_id": alpha["product"],
            "ids": [alpha["job"], beta["job"]],
            "action": "claim",
        },
    )
    assert response.status_code == 403
    response = alpha["client"].post(
        "/api/manage/batch",
        json={"product_id": alpha["product"], "ids": [alpha["job"]], "action": "claim"},
    )
    assert response.status_code == 200, response.text
    with db() as c:
        assert (
            c.execute(
                "SELECT claimed_by FROM jobs WHERE id=?", (alpha["job"],)
            ).fetchone()[0]
            == "shop:" + alpha["shop"]
        )
        assert (
            c.execute(
                "SELECT actor FROM audit WHERE action='job.claim' AND target=?",
                (alpha["job"],),
            ).fetchone()[0]
            == "shop:" + alpha["shop"]
        )


def test_disabling_shop_blocks_new_work_but_preserves_existing_receipt(stores):
    alpha, beta = stores["alpha"], stores["beta"]
    with db() as c:
        c.execute(
            "UPDATE jobs SET state='succeeded',content='旧交付',result_json=? WHERE id=?",
            (json.dumps({"content": "旧交付"}), alpha["job"]),
        )
        c.execute("UPDATE shops SET enabled=0 WHERE id=?", (alpha["shop"],))
    browser = beta["client"]
    assert (
        browser.post("/api/exchange", json={"code": alpha["codes"][1]}).status_code
        == 404
    )
    assert (
        browser.post(
            "/api/redeem", json={"token": alpha["receipt"], "params": {}}
        ).status_code
        == 404
    )
    assert browser.get("/api/products?shop_id=" + alpha["shop"]).status_code == 404
    assert all(p["id"] != alpha["product"] for p in browser.get("/api/products").json())
    assert (
        browser.post("/api/receipt", json={"token": alpha["receipt"]}).status_code
        == 200
    )
    assert (
        browser.post("/api/receipt/reveal", json={"token": alpha["receipt"]}).json()[
            "content"
        ]
        == "旧交付"
    )
    assert (
        browser.post(
            "/api/receipt/destroy", json={"token": alpha["receipt"]}
        ).status_code
        == 200
    )
    assert alpha["client"].get("/api/admin/products").status_code == 401


def test_legacy_integration_key_cannot_issue_another_shops_cards(stores):
    from extore.security import digest

    with db() as c:
        set_setting(c, "integration_key", digest("legacy-platform-secret"))
        before = c.execute(
            "SELECT count(*) FROM cards WHERE product_id=?",
            (stores["beta"]["product"],),
        ).fetchone()[0]
    response = stores["root"].post(
        "/api/integrations/cards",
        headers={
            "Authorization": "Bearer legacy-platform-secret",
            "Idempotency-Key": "cross-store-nope",
        },
        json={"product_id": stores["beta"]["product"], "count": 1},
    )
    assert response.status_code == 403, response.text
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM cards WHERE product_id=?",
                (stores["beta"]["product"],),
            ).fetchone()[0]
            == before
        )


def test_store_integration_keys_and_idempotency_are_independent(stores):
    alpha, beta = stores["alpha"], stores["beta"]
    keys = {}
    for name in ("alpha", "beta"):
        row = stores[name]
        response = row["client"].post("/api/shop/integration-key", json={})
        assert response.status_code == 200, response.text
        keys[name] = response.json()["key"]
        assert len(keys[name]) >= 40
        status = row["client"].get("/api/shop/integration-key").json()
        assert status["configured"] and "key" not in status
    assert keys["alpha"] != keys["beta"]
    assert (
        alpha["client"]
        .post("/api/shop/integration-key?shop_id=" + beta["shop"], json={})
        .status_code
        == 403
    )
    assert (
        alpha["client"]
        .delete("/api/shop/integration-key?shop_id=" + beta["shop"])
        .status_code
        == 403
    )
    issued = {}
    for name in ("alpha", "beta"):
        row = stores[name]
        headers = {
            "Authorization": "Bearer " + keys[name],
            "Idempotency-Key": "same-platform-order-number",
        }
        data = {"product_id": row["product"], "count": 1}
        first = row["client"].post(
            "/api/integrations/cards", headers=headers, json=data
        )
        assert first.status_code == 200, first.text
        issued[name] = first.json()["codes"]
        second = row["client"].post(
            "/api/integrations/cards", headers=headers, json=data
        )
        assert second.json() == first.json()
    assert issued["alpha"] != issued["beta"]
    foreign = alpha["client"].post(
        "/api/integrations/cards",
        headers={
            "Authorization": "Bearer " + keys["alpha"],
            "Idempotency-Key": "other-request-key",
        },
        json={"product_id": beta["product"], "count": 1},
    )
    assert foreign.status_code == 403
    with db() as c:
        assert c.execute("SELECT count(*) FROM api_requests").fetchone()[0] == 2
        for row in c.execute("SELECT key_digest FROM shop_integration_keys"):
            assert row[0] not in keys.values()
        assert not any(
            secret in json.dumps([dict(r) for r in c.execute("SELECT * FROM audit")])
            for secret in keys.values()
        )
    rotated = alpha["client"].post("/api/shop/integration-key", json={}).json()["key"]
    assert rotated != keys["alpha"]
    assert (
        alpha["client"]
        .post(
            "/api/integrations/cards",
            headers={
                "Authorization": "Bearer " + keys["alpha"],
                "Idempotency-Key": "retired-key-request",
            },
            json={"product_id": alpha["product"], "count": 1},
        )
        .status_code
        == 401
    )
    assert alpha["client"].delete("/api/shop/integration-key").status_code == 200
    assert (
        alpha["client"].get("/api/shop/integration-key").json()["configured"] is False
    )


def test_rotating_integration_key_requires_recent_browser_authentication(stores):
    alpha = stores["alpha"]
    with db() as c:
        c.execute("UPDATE sessions SET auth_at=0 WHERE shop_id=?", (alpha["shop"],))
    assert alpha["client"].post("/api/shop/integration-key", json={}).status_code == 401
    assert alpha["client"].delete("/api/shop/integration-key").status_code == 401
    assert alpha["client"].get("/api/shop/integration-key").status_code == 200


def test_merchant_storage_view_excludes_other_shop_and_disk_information(stores):
    alpha, beta = stores["alpha"], stores["beta"]
    with db() as c:
        for item, data in ((alpha, b"alpha-data"), (beta, b"beta-data-is-longer")):
            card = c.execute(
                "SELECT card_id FROM jobs WHERE id=?", (item["job"],)
            ).fetchone()[0]
            c.execute(
                "INSERT INTO job_files(id,card_id,product_id,job_id,field_key,kind,filename,content_type,size,content,created) VALUES (?,?,?,?,?,'input',?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    card,
                    item["product"],
                    item["job"],
                    "file",
                    "example.txt",
                    "text/plain",
                    len(data),
                    data,
                    time.time(),
                ),
            )
    own = alpha["client"].get("/api/admin/storage").json()
    assert own["stored_bytes"] == len(b"alpha-data")
    assert not {"disk_free_bytes", "disk_reserve_bytes"} & own.keys()
    root = stores["root"].get("/api/admin/storage").json()
    assert root["stored_bytes"] == len(b"alpha-data") + len(b"beta-data-is-longer")
    assert "disk_free_bytes" in root
    assert (
        stores["root"].get("/api/admin/storage?shop_id=" + alpha["shop"]).json() == own
    )
    assert (
        alpha["client"].get("/api/admin/storage?shop_id=" + beta["shop"]).status_code
        == 403
    )


def test_retry_reuses_original_frozen_file_and_discards_output_draft(owner):
    from test_files import batch, output_file, prepare

    product, receipt, original, source = prepare(
        owner, allow_retry=False, max_attempts=1
    )
    assert batch(owner, original, "claim", progress=75).status_code == 200
    draft = output_file(owner, original).json()
    response = batch(
        owner,
        original,
        "request_retry",
        message="外部服务暂不可用，请按原资料重试。",
        reason_type="external",
        retry_mode="reuse",
    )
    assert response.status_code == 200, response.text
    view = owner.post("/api/receipt", json={"token": receipt}).json()["job"]
    assert view["state"] == "needs_input" and view["retry_mode"] == "reuse"
    assert view["retry_reason_type"] == "external" and view["can_retry"]
    with db() as c:
        saved = c.execute("SELECT * FROM jobs WHERE id=?", (original["id"],)).fetchone()
        original_schema = saved["schema_snapshot"]
        original_plan = saved["progress_plan"]
        assert (
            c.execute(
                "SELECT state FROM cards WHERE id=?", (saved["card_id"],)
            ).fetchone()[0]
            == "ready"
        )
        assert (
            c.execute("SELECT id FROM job_files WHERE id=?", (draft["id"],)).fetchone()
            is None
        )
    owner.put(
        "/api/admin/products/" + product["id"],
        json={
            **product,
            "parameters": [{"key": "replacement", "label": {"en": "Replacement"}}],
        },
    )
    response = owner.post(
        "/api/retry", json={"token": receipt, "params": {"source": "attacker-file-id"}}
    )
    assert response.status_code == 200, response.text
    retried = response.json()
    assert retried["id"] == original["id"] and retried["attempt"] == 2
    assert retried["state"] == "queued" and retried["progress"] == 0
    with db() as c:
        saved = c.execute("SELECT * FROM jobs WHERE id=?", (original["id"],)).fetchone()
        assert json.loads(saved["params"]) == {"source": source["id"]}
        assert (
            saved["schema_snapshot"] == original_schema
            and saved["progress_plan"] == original_plan
        )
        stored_file = c.execute(
            "SELECT * FROM job_files WHERE id=?", (source["id"],)
        ).fetchone()
        assert (
            stored_file["content"] == b"customer-file"
            and stored_file["bound"] == 1
            and stored_file["attempt"] == 2
        )
    assert (
        owner.get("/api/manage/files/" + source["id"] + "/download").content
        == b"customer-file"
    )
    assert owner.post("/api/retry", json={"token": receipt}).status_code == 409


def test_retry_revise_requires_fresh_inputs_and_rejects_reuse_endpoint(owner):
    from test_task_outcomes import batch, create, launch

    product = create(owner)
    _, receipt, original = launch(owner, product)
    assert batch(owner, original, "claim").status_code == 200
    response = batch(
        owner,
        original,
        "request_retry",
        message="需求需要更明确",
        reason_type="customer_input",
    )
    assert response.status_code == 200, response.text
    waiting = owner.post("/api/receipt", json={"token": receipt}).json()["job"]
    assert (
        waiting["retry_mode"] == "revise"
        and waiting["retry_reason_type"] == "customer_input"
    )
    assert owner.post("/api/retry", json={"token": receipt}).status_code == 409
    response = owner.post(
        "/api/redeem",
        json={"token": receipt, "params": {"email": "revised@example.com"}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["id"] == original["id"] and response.json()["attempt"] == 2


def test_link_views_include_invalid_ancestor_in_history_and_hide_archived(stores):
    alpha, beta = stores["alpha"], stores["beta"]
    parent = (
        alpha["client"]
        .post(
            "/api/admin/staff",
            json={
                "product_id": alpha["product"],
                "name": "委派父链接",
                "permissions": ["queue.view", "queue.process", "links.delegate"],
            },
        )
        .json()
    )
    with db() as c:
        value = create_session(c, Response(), "staff", parent["id"])
    with TestClient(
        app,
        base_url="http://localhost:8000",
        headers={"Origin": "http://localhost:8000"},
    ) as manager:
        manager.cookies.set("extore_session", value)
        response = manager.post(
            "/api/manage/links",
            json={
                "product_id": alpha["product"],
                "name": "下级链接",
                "permissions": ["queue.view"],
            },
        )
        assert response.status_code == 200, response.text
        child = response.json()
    with db() as c:
        c.execute(
            "UPDATE staff SET expires=? WHERE id=?", (time.time() - 1, parent["id"])
        )
    for path in (
        "/api/admin/staff",
        "/api/manage/links?product_id=" + alpha["product"],
    ):
        separator = "&" if "?" in path else "?"
        active = alpha["client"].get(path).json()
        history = alpha["client"].get(path + separator + "view=history").json()
        assert child["id"] not in {item["id"] for item in active}
        assert {parent["id"], child["id"]} <= {item["id"] for item in history}
        assert beta["link"]["id"] not in {item["id"] for item in history}
    with db() as c:
        c.execute("UPDATE staff SET archived=1,revoked=1 WHERE id=?", (parent["id"],))
    assert parent["id"] not in {
        item["id"]
        for item in alpha["client"].get("/api/admin/staff?view=history").json()
    }
    assert parent["id"] in {
        item["id"] for item in alpha["client"].get("/api/admin/staff?view=all").json()
    }

import json
import re
import time
import uuid

import pytest
from extore_processors import get_spec
from fastapi import HTTPException
from starlette.responses import Response
from test_redemption import redeem
from test_webhook_worker import callback

from extore.db import db
from extore.models import Product
from extore.security import create_session, digest

CONFIG_PERMISSIONS = ["product.edit", "fulfillment.configure"]
RELATED_TABLES = (
    "cards",
    "card_batches",
    "card_meta",
    "jobs",
    "grants",
    "events",
    "outbox",
)
SECRET = "legitimate-source-webhook-signing-secret-32-chars"


def snapshot(*tables):
    with db() as c:
        return {
            table: [dict(row) for row in c.execute(f"SELECT * FROM {table}")]
            for table in tables
        }


def quick_create(owner, **body):
    response = owner.post("/api/admin/products/quick", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def login_link(client, link):
    client.cookies.clear()
    response = client.post(
        "/api/staff/login", json={"token": link["url"].split("#", 1)[1]}
    )
    assert response.status_code == 200, response.text


def redeem_params(client, code, params):
    response = client.post("/api/exchange", json={"code": code})
    assert response.status_code == 200, response.text
    response = client.post(
        "/api/redeem", json={"token": response.json()["token"], "params": params}
    )
    assert response.status_code == 200, response.text
    return response.json()


def assert_config_link(result, started, finished):
    assert set(result) == {"product", "management_link"}
    product = result["product"]
    link = result["management_link"]
    assert set(link) == {
        "max_cli_uses",
        "cli_uses",
        "remaining_cli_uses",
        "max_uses",
        "uses",
        "remaining_uses",
        "id",
        "url",
        "permissions",
        "expires",
        "name",
        "product_id",
        "parent_id",
        "created",
        "revoked",
    }
    assert uuid.UUID(product["id"]).version == 4
    assert uuid.UUID(link["id"]).version == 4
    assert (link["max_uses"], link["uses"], link["remaining_uses"]) == (1, 0, 1)
    assert link["product_id"] == product["id"]
    assert link["permissions"] == CONFIG_PERMISSIONS
    assert link["name"] == ("AI 配置 · " + product["name"])[:100]
    assert link["parent_id"] is None and link["revoked"] == 0
    assert started <= link["created"] <= finished
    assert started + 7 * 86400 <= link["expires"] <= finished + 7 * 86400
    assert link["url"].startswith("http://localhost:8000/staff#")
    private_token = link["url"].split("#", 1)[1]
    assert len(private_token) >= 40
    with db() as c:
        row = dict(
            c.execute("SELECT * FROM staff WHERE id=?", (link["id"],)).fetchone()
        )
        assert row["digest"] == digest(private_token)
        assert private_token not in str(row)
        assert json.loads(row["permissions"]) == CONFIG_PERMISSIONS
    return product, link


def test_builtin_templates_are_safe_descriptors(owner):
    before = snapshot("products", "staff", "audit")
    response = owner.get("/api/admin/product-templates")
    assert response.status_code == 200, response.text
    templates = response.json()
    assert {item["id"] for item in templates} == {
        "manual_content",
        "manual_service",
    }
    assert len(templates) == 2
    for item in templates:
        assert set(item) == {"id", "name", "description", "mode", "delivery"}
        assert item["name"].strip() and item["description"].strip()
        assert item["mode"] == "manual"
        assert item["delivery"] == item["id"].removeprefix("manual_")
    assert snapshot("products", "staff", "audit") == before


@pytest.mark.parametrize("template_id", ("manual_content", "manual_service"))
def test_builtin_quick_product_is_private_and_creates_only_config_link(
    owner, template_id
):
    before = snapshot(*RELATED_TABLES)
    started = time.time()
    result = quick_create(owner, template_id=template_id)
    product, link = assert_config_link(result, started, time.time())
    assert set(product) == {"id", *Product.model_fields}
    assert re.fullmatch(r"未命名商品 · [A-Z0-9]{6}", product["name"])
    assert product["public"] is False
    assert product["mode"] == "manual"
    assert product["delivery"] == template_id.removeprefix("manual_")
    assert product["description"] == ""
    assert (
        product["webhook_url"] == product["webhook_secret"] == product["script"] == ""
    )
    assert snapshot(*RELATED_TABLES) == before
    assert owner.get("/api/products").json() == []
    assert owner.get("/api/products/" + product["id"]).status_code == 404
    assert owner.get("/api/admin/products").json() == [product]
    listed = owner.get("/api/admin/staff").json()
    assert listed == [{key: value for key, value in link.items() if key != "url"}]
    assert link["url"].split("#", 1)[1] not in str(listed)


def test_default_quick_products_have_distinct_ids_names_and_links(owner):
    results = [quick_create(owner, template_id="manual_content") for _ in range(3)]
    for field in ("id", "name"):
        assert len({result["product"][field] for result in results}) == len(results)
    for field in ("id", "url"):
        assert len({result["management_link"][field] for result in results}) == len(
            results
        )


@pytest.mark.parametrize("name", ("  专属商品  ", "商" * 120, "  " + "商" * 120 + "  "))
def test_quick_product_accepts_custom_name_and_maximum_length(owner, name):
    started = time.time()
    result = quick_create(owner, template_id="manual_service", name=name)
    product, _ = assert_config_link(result, started, time.time())
    assert product["name"] == name.strip()
    assert product["public"] is False


def test_builtin_null_source_is_equivalent_to_no_source(owner):
    result = quick_create(owner, template_id="manual_content", from_product_id=None)
    assert result["product"]["mode"] == "manual"
    assert result["product"]["public"] is False


@pytest.mark.parametrize("mode", ("webhook", "script"))
def test_existing_product_template_copies_valid_config_without_orders_or_cards(
    owner, setup_product, mode
):
    automation = {
        "delivery": "service",
        "parameters": [
            {
                "key": "email",
                "label": {"zh-CN": "邮箱", "en": "Email"},
                "type": "email",
                "description": {"zh-CN": "**核对邮箱**", "en": "Check email"},
                "collapsed": False,
            },
            {
                "key": "note",
                "label": {"zh-CN": "备注", "en": "Note"},
                "type": "textarea",
                "required": False,
                "description": {"zh-CN": "可选备注"},
                "collapsed": True,
            },
        ],
    }
    private_template = "source private delivery $name - secret 732849"
    if mode == "script":
        spec = get_spec("personalized_text")
        automation = {
            "delivery": spec["delivery"],
            "parameters": spec["parameters"],
            "outputs": spec["outputs"],
            "processor_id": spec["id"],
            "processor_config": {"template": private_template},
        }
    source_id, code = setup_product(
        name="已经发行的自动化商品",
        description="## 来源说明\n完整保留商品说明。",
        logo="https://example.com/logo.png",
        image="https://example.com/image.png",
        public=True,
        mode=mode,
        view_policy="once",
        allow_retry=False,
        max_attempts=7,
        webhook_url="https://example.com/real-hook",
        webhook_secret=SECRET,
        **automation,
    )
    source_job = redeem_params(
        owner,
        code,
        {"name": "原始顾客"} if mode == "script" else {"email": "user@example.com"},
    )
    source_link = owner.post(
        "/api/admin/staff", json={"product_id": source_id, "name": "来源处理员"}
    ).json()
    source = owner.get("/api/admin/products").json()[0]
    before = snapshot(*RELATED_TABLES)
    started = time.time()
    result = quick_create(
        owner, template_id="existing_product", from_product_id=source_id
    )
    product, link = assert_config_link(result, started, time.time())
    assert product["id"] != source_id and product["name"] != source["name"]
    assert product["public"] is False
    copied_fields = set(Product.model_fields) - {
        "name",
        "public",
        "webhook_secret",
        "processor_config",
    }
    assert {key: product[key] for key in copied_fields} == {
        key: source[key] for key in copied_fields
    }
    assert len(product["webhook_secret"]) >= 32
    assert product["webhook_secret"] != SECRET
    if mode == "script":
        default_template = get_spec("personalized_text")["configuration"][0]["default"]
        assert product["processor_config"] == {"template": default_template}
        assert private_template not in json.dumps(result)
        assert source["processor_config"] == {"template": private_template}
    else:
        assert product["processor_config"] == source["processor_config"] == {}
    assert snapshot(*RELATED_TABLES) == before
    assert owner.get("/api/admin/products").json()[0] == source
    assert source_job["product_id"] == source_id
    with db() as c:
        assert c.execute("SELECT count(*) FROM staff").fetchone()[0] == 2
        assert (
            c.execute(
                "SELECT product_id FROM staff WHERE id=?", (source_link["id"],)
            ).fetchone()[0]
            == source_id
        )
        assert (
            c.execute(
                "SELECT count(*) FROM cards WHERE product_id=?", (product["id"],)
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT count(*) FROM jobs WHERE product_id=?", (product["id"],)
            ).fetchone()[0]
            == 0
        )
    assert link["product_id"] == product["id"]


def test_resource_clone_clears_delivery_secrets_until_new_resource_is_configured(
    owner, setup_product
):
    spec = get_spec("resource_link")
    private_url = "https://example.com/source-private-download-837294"
    private_message = "source private instructions 837294"
    source_id, source_code = setup_product(
        name="私密资源来源商品",
        public=True,
        mode="script",
        delivery=spec["delivery"],
        parameters=spec["parameters"],
        outputs=spec["outputs"],
        processor_id=spec["id"],
        processor_config={"resource_url": private_url, "message": private_message},
    )
    source = owner.get("/api/admin/products").json()[0]
    before = snapshot(*RELATED_TABLES)
    started = time.time()
    result = quick_create(
        owner, template_id="existing_product", from_product_id=source_id
    )
    clone, link = assert_config_link(result, started, time.time())
    assert clone["processor_id"] == "resource_link"
    assert clone["processor_config"] == {"resource_url": "", "message": ""}
    assert clone["parameters"] == source["parameters"]
    assert clone["outputs"] == source["outputs"]
    assert clone["public"] is False
    assert snapshot(*RELATED_TABLES) == before
    public_responses = (
        owner.get("/api/products"),
        owner.post("/api/exchange", json={"code": source_code}),
        owner.get("/api/products/" + clone["id"]),
    )
    for value in (private_url, private_message):
        assert value not in json.dumps(result)
        assert all(value not in response.text for response in public_responses)
        with db() as c:
            stored = c.execute(
                "SELECT config FROM products WHERE id=?", (clone["id"],)
            ).fetchone()[0]
            assert value not in stored
    assert public_responses[0].status_code == public_responses[1].status_code == 200
    assert public_responses[2].status_code == 404
    before = snapshot("products", "staff", "audit", *RELATED_TABLES)
    response = owner.post(
        "/api/admin/cards", json={"product_id": clone["id"], "count": 1}
    )
    assert response.status_code == 400, response.text
    assert "配置" in response.json()["detail"]
    assert snapshot("products", "staff", "audit", *RELATED_TABLES) == before
    login_link(owner, link)
    response = owner.get("/api/manage/product")
    assert response.status_code == 200, response.text
    assert response.json()["processor_config"] == clone["processor_config"]
    assert private_url not in response.text and private_message not in response.text
    configured = {
        **response.json(),
        "processor_config": {
            "resource_url": "https://example.com/new-merchant-resource-837294",
            "message": "新资源使用说明",
        },
    }
    response = owner.put("/api/manage/product", json=configured)
    assert response.status_code == 200, response.text
    with db() as c:
        value = create_session(c, Response(), "admin")
    owner.cookies.clear()
    owner.cookies.set("extore_session", value)
    response = owner.post(
        "/api/admin/cards", json={"product_id": clone["id"], "count": 1}
    )
    assert response.status_code == 200 and len(response.json()["codes"]) == 1
    assert owner.get("/api/admin/products").json()[0] == source


def test_cloned_webhook_keys_cannot_sign_each_others_callbacks(owner, setup_product):
    source_id, source_code = setup_product(
        mode="webhook",
        webhook_url="https://example.com/real-hook",
        webhook_secret=SECRET,
    )
    _, source_job = redeem(owner, source_code)
    result = quick_create(
        owner, template_id="existing_product", from_product_id=source_id
    )
    clone = result["product"]
    response = owner.post(
        "/api/admin/cards", json={"product_id": clone["id"], "count": 1}
    )
    assert response.status_code == 200, response.text
    _, clone_job = redeem(owner, response.json()["codes"][0])
    login_link(owner, result["management_link"])
    response = owner.get("/api/manage/product")
    assert response.status_code == 200, response.text
    assert response.json()["webhook_secret"] == clone["webhook_secret"]
    assert SECRET not in response.text
    update = {"state": "succeeded", "attempt": 1, "content": "交付内容"}
    before = snapshot("jobs", "events", "outbox", "audit", "callback_nonces")
    assert (
        callback(owner, clone["id"], clone_job, update, secret=SECRET).status_code
        == 401
    )
    assert (
        callback(
            owner, source_id, source_job, update, secret=clone["webhook_secret"]
        ).status_code
        == 401
    )
    assert snapshot("jobs", "events", "outbox", "audit", "callback_nonces") == before
    assert (
        callback(owner, source_id, source_job, update, secret=SECRET).status_code == 200
    )
    assert (
        callback(
            owner, clone["id"], clone_job, update, secret=clone["webhook_secret"]
        ).status_code
        == 200
    )


@pytest.mark.parametrize(
    "body,status",
    (
        ({"template_id": "unknown"}, 422),
        ({"template_id": "existing_product"}, 422),
        ({"template_id": "existing_product", "from_product_id": ""}, 422),
        ({"template_id": "existing_product", "from_product_id": None}, 422),
        ({"template_id": "existing_product", "from_product_id": "missing"}, 404),
        ({"template_id": "manual_content", "from_product_id": "missing"}, 422),
        ({"template_id": "manual_service", "from_product_id": "missing"}, 422),
        ({"template_id": "manual_content", "from_product_id": ""}, 422),
        ({"template_id": "manual_content", "name": ""}, 422),
        ({"template_id": "manual_content", "name": " \n\t "}, 422),
        ({"template_id": "manual_content", "name": "商" * 121}, 422),
        ({"template_id": "manual_content", "public": True}, 422),
        ({"template_id": "manual_content", "permissions": ["cards.manage"]}, 422),
        ({}, 422),
    ),
)
def test_invalid_quick_create_does_not_leave_partial_records(owner, body, status):
    before = snapshot("products", "staff", "audit", *RELATED_TABLES)
    response = owner.post("/api/admin/products/quick", json=body)
    assert response.status_code == status, response.text
    assert snapshot("products", "staff", "audit", *RELATED_TABLES) == before


@pytest.mark.parametrize("role", ("unauthenticated", "bootstrap", "staff"))
def test_quick_create_and_templates_require_owner(owner, setup_product, role):
    if role == "staff":
        product_id, _ = setup_product()
        response = owner.post(
            "/api/admin/staff", json={"product_id": product_id, "name": "处理员"}
        )
        assert response.status_code == 200, response.text
        login_link(owner, response.json())
    else:
        owner.cookies.clear()
        if role == "bootstrap":
            with db() as c:
                value = create_session(c, Response(), "bootstrap")
            owner.cookies.set("extore_session", value)
    before = snapshot("products", "staff", "audit", *RELATED_TABLES)
    assert owner.get("/api/admin/product-templates").status_code == 401
    response = owner.post(
        "/api/admin/products/quick", json={"template_id": "manual_content"}
    )
    assert response.status_code == 401, response.text
    assert snapshot("products", "staff", "audit", *RELATED_TABLES) == before


def test_quick_config_link_edits_only_its_product_and_fulfillment(owner, setup_product):
    other_id, _ = setup_product()
    result = quick_create(owner, template_id="manual_content")
    product = result["product"]
    login_link(owner, result["management_link"])
    status = owner.get("/api/auth/status").json()
    assert status["role"] == "staff" and status["product_id"] == product["id"]
    assert status["permissions"] == CONFIG_PERMISSIONS
    response = owner.get("/api/manage/product")
    assert response.status_code == 200 and response.json() == product
    updated = {
        **product,
        "name": "由 AI 配置",
        "description": "详细兑换说明",
        "mode": "webhook",
        "webhook_url": "https://example.com/fulfillment",
        "webhook_secret": SECRET,
        "parameters": [{"key": "email", "label": {"zh-CN": "邮箱"}, "type": "email"}],
    }
    response = owner.put("/api/manage/product", json=updated)
    assert response.status_code == 200, response.text
    assert response.json()["name"] == updated["name"]
    assert response.json()["webhook_secret"] == SECRET
    for method in ("get", "put"):
        response = owner.request(
            method,
            "/api/manage/product",
            params={"product_id": other_id},
            json=updated if method == "put" else None,
        )
        assert response.status_code == 403, response.text
    before = snapshot("products", "staff", "audit", *RELATED_TABLES)
    for method, path, body in (
        ("GET", "/api/manage/jobs", None),
        (
            "POST",
            "/api/manage/batch",
            {"product_id": product["id"], "ids": ["missing"], "action": "claim"},
        ),
        ("GET", "/api/manage/cards", None),
        ("POST", "/api/manage/cards", {"product_id": product["id"], "count": 1}),
        ("GET", "/api/manage/events", None),
        ("GET", "/api/manage/links", None),
        (
            "POST",
            "/api/manage/links",
            {"name": "无权委派", "permissions": ["product.edit"]},
        ),
    ):
        response = owner.request(method, path, json=body)
        assert response.status_code == 403, response.text
    for path in ("products", "cards", "events", "staff", "product-templates"):
        assert owner.get("/api/admin/" + path).status_code == 401
    assert owner.get("/api/auth/passkeys").status_code == 401
    assert (
        owner.post(
            "/api/admin/products/quick", json={"template_id": "manual_service"}
        ).status_code
        == 401
    )
    assert snapshot("products", "staff", "audit", *RELATED_TABLES) == before


@pytest.mark.parametrize("http_error", (False, True))
def test_late_link_failure_rolls_back_product_link_and_audits(
    owner, monkeypatch, http_error
):
    import extore.app as app_module

    real_create_link = app_module.create_product_link

    def create_then_fail(c, body, session):
        real_create_link(c, body, session)
        if http_error:
            raise HTTPException(400, "配置链接创建失败")
        raise RuntimeError("forced late link failure")

    monkeypatch.setattr(app_module, "create_product_link", create_then_fail)
    before = snapshot("products", "staff", "audit", *RELATED_TABLES)
    if http_error:
        response = owner.post(
            "/api/admin/products/quick", json={"template_id": "manual_content"}
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "配置链接创建失败"
    else:
        with pytest.raises(RuntimeError, match="forced late link failure"):
            owner.post(
                "/api/admin/products/quick", json={"template_id": "manual_content"}
            )
    assert snapshot("products", "staff", "audit", *RELATED_TABLES) == before

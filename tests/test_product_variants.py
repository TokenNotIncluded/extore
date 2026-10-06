import asyncio
import io
import json
import time
import uuid

import pytest
from pydantic import ValidationError
from test_product_links import create_link, login_link
from test_redemption import finish

from extore.db import db, set_setting
from extore.models import Product
from extore.security import card_digest, digest

DEFAULT_VARIANT = {
    "id": "default",
    "name": "默认规格",
    "description": "",
    "price": None,
    "currency": "CNY",
    "attributes": {},
    "enabled": True,
}


def variant(variant_id="basic", **values):
    return {
        "id": variant_id,
        "name": "基础规格",
        "description": "规格说明",
        "price": "12.34",
        "currency": "CNY",
        "attributes": {"plan": variant_id, "quota": 100},
        "enabled": True,
        **values,
    }


def create(owner, **values):
    response = owner.post("/api/admin/products", json={"name": "多规格商品", **values})
    assert response.status_code == 200, response.text
    return response.json()


def issue(owner, product_id, variant_id="default", prefix="admin", **values):
    response = owner.post(
        f"/api/{prefix}/cards",
        json={
            "product_id": product_id,
            "count": 1,
            "variant_id": variant_id,
            **values,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["codes"][0]


def exchange(owner, code, **values):
    response = owner.post("/api/exchange", json={"code": code, **values})
    assert response.status_code == 200, response.text
    return response.json()


def redeem(owner, exchanged, params=None, **values):
    response = owner.post(
        "/api/redeem",
        json={"token": exchanged["token"], "params": params or {}, **values},
    )
    assert response.status_code == 200, response.text
    return response.json()


def update(owner, product, **values):
    response = owner.put(
        f"/api/admin/products/{product['id']}", json={**product, **values}
    )
    assert response.status_code == 200, response.text
    return response.json()


def card_meta(code):
    with db() as c:
        return dict(
            c.execute(
                "SELECT card_meta.* FROM card_meta JOIN cards "
                "ON cards.id=card_meta.card_id WHERE cards.digest=?",
                (card_digest(code),),
            ).fetchone()
        )


def event_data(job_id):
    with db() as c:
        return [
            json.loads(row["payload"])["data"]
            for row in c.execute(
                "SELECT payload FROM events WHERE job_id=? ORDER BY created",
                (job_id,),
            )
        ]


def issuance_snapshot():
    with db() as c:
        return {
            table: [dict(row) for row in c.execute(f"SELECT * FROM {table}")]
            for table in ("cards", "card_meta", "card_batches", "audit")
        }


def platform_headers(key="variant-order-0001"):
    with db() as c:
        set_setting(c, "integration_key", digest("variant-platform-key"))
    return {
        "Authorization": "Bearer variant-platform-key",
        "Idempotency-Key": key,
    }


def test_omitted_variants_create_one_compatible_default(owner):
    product = create(owner)
    assert product["variants"] == [DEFAULT_VARIANT]
    response = owner.post(
        "/api/admin/cards", json={"product_id": product["id"], "count": 1}
    )
    assert response.status_code == 200, response.text
    code = response.json()["codes"][0]
    assert exchange(owner, code)["variant"] == DEFAULT_VARIANT
    metadata = card_meta(code)
    assert metadata["variant_id"] == "default"
    assert json.loads(metadata["variant_snapshot"]) == DEFAULT_VARIANT


def test_variant_defaults_and_decimal_price_are_canonical(owner):
    product = create(
        owner,
        variants=[
            {"id": "basic", "name": "标准规格", "price": "0012.340000"},
            {"id": "free", "name": "免费规格", "price": "0.000000"},
            {"id": "quoted", "name": "联系报价"},
        ],
    )
    first, free, quoted = product["variants"]
    assert first == {
        **DEFAULT_VARIANT,
        "id": "basic",
        "name": "标准规格",
        "price": "12.34",
    }
    assert free["price"] == "0"
    assert quoted["price"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"id": "UPPER"},
        {"id": "contains space"},
        {"id": "../escape"},
        {"id": "_leading"},
        {"id": "x" * 41},
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 121},
        {"description": "x" * 10001},
        {"price": "-0.01"},
        {"price": "1e2"},
        {"price": "NaN"},
        {"price": "Infinity"},
        {"price": ""},
        {"price": "+1"},
        {"price": "1000000000000"},
        {"price": "0.1234567"},
        {"price": 1.5},
        {"price": True},
        {"currency": "CN"},
        {"currency": "ABCDEF"},
        {"currency": "CNY1"},
        {"currency": "¥"},
        {"attributes": {"nested": {"plan": "premium"}}},
        {"attributes": {"list": [1, 2]}},
        {"attributes": {"x" * 101: "value"}},
        {"attributes": {"text": "x" * 1001}},
        {"attributes": {str(index): index for index in range(21)}},
    ],
)
def test_invalid_variant_schema_is_rejected_before_product_creation(owner, changes):
    response = owner.post(
        "/api/admin/products",
        json={"name": "非法规格", "variants": [variant(**changes)]},
    )
    assert response.status_code == 422, response.text
    with db() as c:
        assert c.execute("SELECT count(*) FROM products").fetchone()[0] == 0


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_nonfinite_variant_attributes_are_rejected(value):
    with pytest.raises(ValidationError):
        Product.model_validate(
            {"name": "非法属性", "variants": [variant(attributes={"quota": value})]}
        )


@pytest.mark.parametrize(
    "value",
    (
        9007199254740992,
        -9007199254740992,
        float(9007199254740992),
        float(-9007199254740992),
    ),
)
def test_integer_variant_attributes_outside_browser_precision_are_rejected(value):
    with pytest.raises(ValidationError):
        Product.model_validate(
            {"name": "超出安全整数", "variants": [variant(attributes={"quota": value})]}
        )


@pytest.mark.parametrize(
    "value",
    (
        9007199254740991,
        -9007199254740991,
        float(9007199254740991),
        float(-9007199254740991),
    ),
)
def test_integer_variant_attributes_accept_browser_precision_boundaries(value):
    product = Product.model_validate(
        {"name": "安全整数边界", "variants": [variant(attributes={"quota": value})]}
    )
    actual = product.variants[0].attributes["quota"]
    assert actual == value and type(actual) is type(value)


@pytest.mark.parametrize(
    "variants",
    (
        [],
        [variant(), variant()],
        [variant(f"sku-{index}") for index in range(101)],
    ),
)
def test_empty_duplicate_or_excessive_variants_are_rejected(owner, variants):
    response = owner.post(
        "/api/admin/products", json={"name": "非法规格列表", "variants": variants}
    )
    assert response.status_code == 422, response.text


def test_variant_schema_accepts_limits_and_preserves_scalar_types(owner):
    attributes = {
        "boolean": True,
        "integer": 500,
        "fraction": 0.5,
        "null": None,
        "x" * 100: "x" * 1000,
        **{f"key-{index}": index for index in range(15)},
    }
    selected = variant(
        "x" * 40,
        name="x" * 120,
        description="x" * 10000,
        price="999999999999.999999",
        currency="ABCDE",
        attributes=attributes,
    )
    product = create(
        owner,
        variants=[selected, *[variant(f"sku-{index}") for index in range(99)]],
    )
    assert len(product["variants"]) == 100
    assert product["variants"][0] == selected
    stored = product["variants"][0]["attributes"]
    assert stored["boolean"] is True
    assert type(stored["integer"]) is int
    assert type(stored["fraction"]) is float
    assert stored["null"] is None


@pytest.mark.parametrize("prefix", ("admin", "manage", "integrations"))
@pytest.mark.parametrize("variant_id", ("missing", "disabled", "default"))
def test_issuance_requires_a_selected_existing_enabled_variant(
    owner, prefix, variant_id
):
    product = create(owner, variants=[variant(), variant("disabled", enabled=False)])
    headers = platform_headers() if prefix == "integrations" else {}
    before = issuance_snapshot()
    response = owner.post(
        f"/api/{prefix}/cards",
        json={
            "product_id": product["id"],
            "count": 3,
            "variant_id": variant_id,
        },
        headers=headers,
    )
    assert response.status_code == (409 if variant_id == "disabled" else 400), (
        response.text
    )
    assert issuance_snapshot() == before


@pytest.mark.parametrize("prefix", ("admin", "manage", "integrations"))
def test_each_issuer_binds_the_requested_variant(owner, prefix):
    selected = variant("premium", price="199.9", attributes={"quota": 500})
    product = create(owner, variants=[variant(), selected])
    headers = platform_headers() if prefix == "integrations" else {}
    response = owner.post(
        f"/api/{prefix}/cards",
        json={"product_id": product["id"], "count": 2, "variant_id": "premium"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    for code in response.json()["codes"]:
        metadata = card_meta(code)
        assert metadata["variant_id"] == "premium"
        assert json.loads(metadata["variant_snapshot"]) == selected
        assert exchange(owner, code)["variant"] == selected


def test_card_snapshot_survives_price_attributes_labels_and_availability_changes(owner):
    selected = variant("premium", price="99.5", attributes={"quota": 500})
    product = create(owner, variants=[selected])
    code = issue(owner, product["id"], "premium")
    original_metadata = card_meta(code)
    changed = variant(
        "premium",
        name="新名称",
        description="新版说明",
        price="129",
        currency="USD",
        attributes={"quota": 999},
        enabled=False,
    )
    product = update(owner, product, variants=[changed, variant("new")])
    assert product["variants"][0] == changed
    assert card_meta(code) == original_metadata
    exchanged = exchange(owner, code)
    assert exchanged["variant"] == selected
    task = redeem(owner, exchanged)
    assert task["variant"] == selected
    finish(owner, task)
    receipt = owner.post("/api/receipt", json={"token": exchanged["token"]})
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["variant"] == selected
    assert receipt.json()["job"]["variant"] == selected
    assert all(data["variant"] == selected for data in event_data(task["id"]))
    new_code = issue(owner, product["id"], "new")
    assert exchange(owner, new_code)["variant"] == variant("new")


@pytest.mark.parametrize("metadata", ("missing", "empty"))
def test_legacy_cards_bind_empty_server_default_without_current_attributes(
    owner, metadata
):
    product = create(owner)
    code, card_id = "LEGACY-VARIANT-CARD", str(uuid.uuid4())
    with db() as c:
        legacy_config = {key: value for key, value in product.items() if key != "id"}
        legacy_config.pop("variants", None)
        c.execute(
            "UPDATE products SET config=? WHERE id=?",
            (json.dumps(legacy_config), product["id"]),
        )
        c.execute(
            "INSERT INTO cards(id,digest,product_id,created) VALUES (?,?,?,?)",
            (card_id, card_digest(code), product["id"], time.time()),
        )
        if metadata == "empty":
            c.execute(
                "INSERT INTO card_meta(card_id,variant_id,variant_snapshot) "
                "VALUES (?,'default','{}')",
                (card_id,),
            )
    update(
        owner,
        product,
        variants=[variant("default", attributes={"quota": 999999}, enabled=False)],
    )
    exchanged = exchange(owner, code)
    assert exchanged["variant"] == DEFAULT_VARIANT
    task = redeem(owner, exchanged)
    assert task["variant"] == DEFAULT_VARIANT
    receipt = owner.post("/api/receipt", json={"token": exchanged["token"]}).json()
    assert receipt["variant"] == DEFAULT_VARIANT
    assert event_data(task["id"])[0]["variant"] == DEFAULT_VARIANT


@pytest.mark.parametrize("revoke", (False, True))
def test_issued_variant_cannot_be_removed_but_unissued_variant_can(owner, revoke):
    product = create(owner, variants=[variant("issued"), variant("unused")])
    code = issue(owner, product["id"], "issued")
    if revoke:
        response = owner.post(f"/api/admin/cards/{card_meta(code)['card_id']}/revoke")
        assert response.status_code == 200, response.text
    product = update(owner, product, variants=[variant("issued")])
    response = owner.put(
        f"/api/admin/products/{product['id']}",
        json={**product, "variants": [variant("replacement")]},
    )
    assert response.status_code == 409, response.text
    products = owner.get("/api/admin/products").json()
    assert products[0]["variants"] == [variant("issued")]


def test_default_id_cannot_be_removed_while_legacy_cards_exist(owner):
    product = create(owner)
    with db() as c:
        c.execute(
            "INSERT INTO cards(id,digest,product_id,created) VALUES (?,?,?,?)",
            (
                str(uuid.uuid4()),
                card_digest("LEGACY-DEFAULT-LOCK"),
                product["id"],
                time.time(),
            ),
        )
    response = owner.put(
        f"/api/admin/products/{product['id']}",
        json={**product, "variants": [variant("replacement")]},
    )
    assert response.status_code == 409, response.text


def test_customer_variant_parameters_and_top_level_values_cannot_spoof_binding(owner):
    selected = variant("premium")
    product = create(
        owner,
        variants=[selected, variant("basic")],
        parameters=[
            {"key": "variant", "label": {"zh-CN": "客户备注"}, "required": False}
        ],
    )
    code = issue(owner, product["id"], "premium")
    exchanged = exchange(owner, code, variant_id="basic", variant={"id": "basic"})
    assert exchanged["variant"] == selected
    task = redeem(
        owner,
        exchanged,
        {"variant": '{"id":"basic","attributes":{"quota":999999}}'},
        variant_id="basic",
        variant={"id": "basic"},
    )
    assert task["variant"] == selected
    data = event_data(task["id"])[0]
    assert data["variant"] == selected
    assert data["params"]["variant"].startswith('{"id":"basic"')
    assert card_meta(code)["variant_id"] == "premium"


def test_undeclared_variant_parameters_fail_before_reserving_a_card(owner):
    product = create(owner, variants=[variant()])
    code = issue(owner, product["id"], "basic")
    exchanged = exchange(owner, code)
    response = owner.post(
        "/api/redeem",
        json={"token": exchanged["token"], "params": {"variant_id": "premium"}},
    )
    assert response.status_code == 400, response.text
    with db() as c:
        assert c.execute("SELECT state FROM cards").fetchone()[0] == "ready"
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_same_product_variants_share_one_product_queue_and_keep_per_job_snapshots(
    owner,
):
    first_variant, second_variant = variant(), variant("premium")
    product = create(owner, variants=[first_variant, second_variant])
    first = redeem(owner, exchange(owner, issue(owner, product["id"], "basic")))
    second = redeem(owner, exchange(owner, issue(owner, product["id"], "premium")))
    other = create(owner, name="其他商品", variants=[variant("premium")])
    foreign = redeem(owner, exchange(owner, issue(owner, other["id"], "premium")))
    response = owner.get("/api/manage/jobs", params={"product_id": product["id"]})
    assert response.status_code == 200, response.text
    rows = response.json()
    assert [row["id"] for row in rows] == [first["id"], second["id"]]
    assert [row["queue_ahead"] for row in rows] == [0, 1]
    assert [row["variant"] for row in rows] == [first_variant, second_variant]
    assert foreign["queue_ahead"] == 0
    response = owner.post(
        "/api/manage/batch",
        json={
            "product_id": product["id"],
            "ids": [first["id"], second["id"]],
            "action": "claim",
        },
    )
    assert response.status_code == 200, response.text
    assert all(data["variant"] == first_variant for data in event_data(first["id"]))
    assert all(data["variant"] == second_variant for data in event_data(second["id"]))


def test_retry_preserves_issued_variant_after_product_configuration_changes(owner):
    selected = variant()
    product = create(owner, variants=[selected])
    exchanged = exchange(owner, issue(owner, product["id"], "basic"))
    task = redeem(owner, exchanged)
    for action in ("claim", "fail"):
        response = owner.post(
            "/api/manage/batch",
            json={
                "product_id": product["id"],
                "ids": [task["id"]],
                "action": action,
                "retryable": True,
            },
        )
        assert response.status_code == 200, response.text
    update(owner, product, variants=[variant(attributes={"quota": 999}, enabled=False)])
    retried = redeem(owner, exchanged)
    assert retried["id"] == task["id"] and retried["attempt"] == 2
    assert retried["variant"] == selected
    assert all(data["variant"] == selected for data in event_data(task["id"]))


def test_card_manager_can_issue_own_variant_and_foreign_products_fail_atomically(owner):
    selected = variant("premium")
    product = create(owner, variants=[selected])
    other = create(owner, name="无权商品", variants=[selected])
    link = create_link(owner, product["id"], ["cards.manage"])
    login_link(owner, link)
    code = issue(owner, product["id"], "premium", prefix="manage")
    assert exchange(owner, code)["variant"] == selected
    before = issuance_snapshot()
    for foreign in (other["id"], "missing"):
        response = owner.post(
            "/api/manage/cards",
            json={"product_id": foreign, "count": 3, "variant_id": "premium"},
        )
        assert response.status_code == 403, response.text
        assert issuance_snapshot() == before


def test_platform_idempotency_fingerprint_distinguishes_variant_selection(owner):
    product = create(owner, variants=[DEFAULT_VARIANT, variant(), variant("premium")])
    headers = platform_headers()
    data = {"product_id": product["id"], "count": 1, "variant_id": "basic"}
    first = owner.post("/api/integrations/cards", json=data, headers=headers)
    assert first.status_code == 200, first.text
    assert (
        owner.post("/api/integrations/cards", json=data, headers=headers).json()
        == first.json()
    )
    before = issuance_snapshot()
    for variant_id in ("premium", "default"):
        response = owner.post(
            "/api/integrations/cards",
            json={**data, "variant_id": variant_id},
            headers=headers,
        )
        assert response.status_code == 409, response.text
        assert issuance_snapshot() == before


def test_platform_idempotent_replay_survives_disabling_the_issued_variant(owner):
    product = create(owner, variants=[variant()])
    headers = platform_headers()
    body = {"product_id": product["id"], "count": 2, "variant_id": "basic"}
    response = owner.post("/api/integrations/cards", json=body, headers=headers)
    assert response.status_code == 200, response.text
    original = response.json()
    update(owner, product, variants=[variant(enabled=False)])
    before = issuance_snapshot()
    response = owner.post("/api/integrations/cards", json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == original
    assert issuance_snapshot() == before
    new_order_headers = {**headers, "Idempotency-Key": "variant-order-0002"}
    response = owner.post(
        "/api/integrations/cards", json=body, headers=new_order_headers
    )
    assert response.status_code == 409, response.text
    assert issuance_snapshot() == before


def test_repeated_issue_tracking_cannot_replace_an_issued_variant_snapshot(owner):
    from extore.card_tracking import record_issue

    selected = variant("premium")
    product = create(owner, variants=[selected, variant()])
    code = issue(owner, product["id"], "premium", label="初始批次")
    before = issuance_snapshot()
    with db() as c:
        result = record_issue(
            c,
            product["id"],
            [code],
            label="替换批次",
            expires=time.time() + 3600,
            variant_id="basic",
            variant_snapshot=variant(attributes={"quota": 999999}),
        )
        assert result is None
    assert issuance_snapshot() == before
    assert exchange(owner, code)["variant"] == selected


def test_callback_cannot_change_card_or_job_variant_metadata(owner):
    from test_webhook_worker import SECRET, callback

    selected = variant("premium")
    product = create(
        owner,
        mode="webhook",
        webhook_url="https://example.com/hooks",
        webhook_secret=SECRET,
        variants=[selected, variant()],
    )
    code = issue(owner, product["id"], "premium")
    exchanged = exchange(owner, code)
    task = redeem(owner, exchanged)
    original = card_meta(code)
    response = callback(
        owner,
        product["id"],
        task,
        {
            "state": "succeeded",
            "attempt": 1,
            "content": "交付内容",
            "variant_id": "basic",
            "variant": variant(attributes={"quota": 999999}),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["variant"] == selected
    assert card_meta(code) == original
    assert all(data["variant"] == selected for data in event_data(task["id"]))


@pytest.mark.parametrize("permission", ("queue.view", "product.edit"))
def test_other_product_permissions_do_not_grant_variant_issuance(owner, permission):
    product = create(owner, variants=[variant()])
    login_link(owner, create_link(owner, product["id"], [permission]))
    before = issuance_snapshot()
    response = owner.post(
        "/api/manage/cards",
        json={"product_id": product["id"], "count": 3, "variant_id": "basic"},
    )
    assert response.status_code == 403, response.text
    assert issuance_snapshot() == before


@pytest.mark.parametrize("include_metadata", (False, True))
def test_platform_default_variant_preserves_pre_variant_idempotency_fingerprint(
    owner, include_metadata
):
    from cryptography.fernet import Fernet

    from extore.config import DATA

    product = create(owner)
    code = issue(owner, product["id"])
    key = "pre-variant-order-0001"
    headers = platform_headers(key)
    data = {"product_id": product["id"], "count": 1}
    if include_metadata:
        data.update(label="原订单", expires=time.time() + 3600)
    prior_response = {"codes": [code]}
    cipher = Fernet((DATA / "issuance.key").read_bytes())
    with db() as c:
        c.execute(
            "INSERT INTO api_requests(key,fingerprint,response,created) VALUES (?,?,?,?)",
            (
                digest(key),
                digest(json.dumps(data, ensure_ascii=False, separators=(",", ":"))),
                cipher.encrypt(json.dumps(prior_response).encode()),
                time.time(),
            ),
        )
    before = issuance_snapshot()
    for body in (data, {**data, "variant_id": "default"}):
        response = owner.post("/api/integrations/cards", json=body, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json() == prior_response
        assert issuance_snapshot() == before


def test_sdk_task_parses_variant_as_separate_metadata_and_keeps_legacy_tasks():
    from extore.sdk.script import Task

    selected = variant("premium")
    payload = {
        "id": "task-1",
        "product_id": "product-1",
        "attempt": 2,
        "params": {"variant": "customer text", "name": "小明"},
        "configuration": {"template": "你好"},
        "variant": selected,
    }
    task = Task(**json.loads(json.dumps(payload)))
    assert task.variant == selected
    assert task.params["variant"] == "customer text"
    assert task.idempotency_key == "task-1"
    legacy = Task(id="old-task", product_id="product-1", attempt=1, params={})
    assert isinstance(legacy.variant, dict)
    assert legacy.idempotency_key == "old-task"


def test_sdk_run_hands_handler_the_bound_variant(monkeypatch, capsys):
    import extore.sdk.script as script

    selected = variant("premium")
    payload = {
        "id": "task-1",
        "product_id": "product-1",
        "attempt": 1,
        "params": {"variant": "customer text"},
        "variant": selected,
    }
    monkeypatch.setattr(script.sys, "stdin", io.StringIO(json.dumps(payload)))

    def handle(task):
        assert task.variant == selected
        assert task.params["variant"] == "customer text"
        return script.Result.success("处理完成")

    script.run(handle)
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "succeeded" and result["content"] == "处理完成"


def test_worker_passes_frozen_variant_to_processor_envelope(owner, monkeypatch):
    from extore import worker

    selected = variant("premium", attributes={"quota": 500})
    product = create(
        owner,
        mode="script",
        processor_id="personalized_text",
        processor_config={"template": "你好，$name"},
        variants=[selected],
    )
    exchanged = exchange(owner, issue(owner, product["id"], "premium"))
    task = redeem(owner, exchanged, {"name": "小明"})
    update(owner, product, variants=[variant("premium", attributes={"quota": 999})])
    captured = []

    class Input:
        def write(self, payload):
            captured.append(json.loads(payload))

        async def drain(self):
            pass

        def close(self):
            pass

    class Output:
        def __init__(self):
            self.lines = iter(
                [
                    b'{"kind":"result","state":"succeeded","output":{"content":"ok"}}\n',
                    b"",
                ]
            )

        async def readline(self):
            return next(self.lines)

    class Process:
        stdin = Input()
        stdout = Output()
        returncode = 0

        async def wait(self):
            return 0

    async def start(*args, **kwargs):
        return Process()

    monkeypatch.setattr(worker.asyncio, "create_subprocess_exec", start)
    assert asyncio.run(worker.job_once()) is True
    assert len(captured) == 1
    assert captured[0]["variant"] == selected
    assert captured[0]["params"] == {"name": "小明"}
    assert captured[0]["configuration"] == {"template": "你好，$name"}
    receipt = owner.post("/api/receipt", json={"token": exchanged["token"]}).json()
    assert receipt["job"]["id"] == task["id"]
    assert receipt["job"]["state"] == "succeeded"
    assert receipt["job"]["variant"] == selected


def test_real_official_processor_accepts_variant_envelope(owner):
    from extore.worker import job_once

    selected = variant("premium")
    product = create(
        owner,
        mode="script",
        processor_id="personalized_text",
        processor_config={"template": "你好，$name"},
        variants=[selected],
    )
    exchanged = exchange(owner, issue(owner, product["id"], "premium"))
    redeem(owner, exchanged, {"name": "小明"})
    assert asyncio.run(job_once()) is True
    receipt = owner.post("/api/receipt", json={"token": exchanged["token"]}).json()
    assert receipt["job"]["state"] == "succeeded"
    assert receipt["job"]["variant"] == selected
    revealed = owner.post("/api/receipt/reveal", json={"token": exchanged["token"]})
    assert revealed.status_code == 200, revealed.text
    assert revealed.json()["output"] == {"content": "你好，小明"}

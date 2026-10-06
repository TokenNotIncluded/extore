import json
import sqlite3
import time
import uuid

import pytest
from fastapi import HTTPException
from test_card_tracking import STATES, batch, card_id, expire, inventory, stats
from test_product_links import create_link, login_link
from test_redemption import finish, redeem

from extore.card_tracking import init_schema, record_issue
from extore.db import db
from extore.security import card_digest

SUMMARY_COUNTS = (
    "total",
    "remaining",
    "available",
    "used",
    "verified",
    "viewed",
    "in_progress",
    "completed",
    "failed",
)


def variant(vid, name, **kwargs):
    return {
        "id": vid,
        "name": name,
        "description": f"{name}说明",
        "price": "10.50",
        "currency": "CNY",
        "attributes": {"tier": vid, "days": 30, "lifetime": False},
        "enabled": True,
        **kwargs,
    }


def create_product(owner, variants=None, **kwargs):
    body = {
        "name": "多规格商品",
        "parameters": [{"key": "email", "label": {"zh-CN": "邮箱"}, "type": "email"}],
        **kwargs,
    }
    if variants is not None:
        body["variants"] = variants
    response = owner.post("/api/admin/products", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def issue_variant(owner, pid, vid="default", count=1, **kwargs):
    response = owner.post(
        "/api/admin/cards",
        json={"product_id": pid, "variant_id": vid, "count": count, **kwargs},
    )
    assert response.status_code == 200, response.text
    return response.json()["codes"]


def assert_variants_sum_to_product(product):
    variants = product["variants"]
    assert len({v["variant_id"] for v in variants}) == len(variants)
    for key in SUMMARY_COUNTS:
        assert sum(v["summary"][key] for v in variants) == product[key]
    for state in STATES:
        assert (
            sum(v["summary"]["states"][state] for v in variants)
            == product["states"][state]
        )
    assert sum(v["summary"]["total"] for v in variants) == sum(
        product["states"].values()
    )


def edit_variants(owner, product, variants):
    response = owner.put(
        f"/api/admin/products/{product['id']}", json={**product, "variants": variants}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_old_metadata_schema_migrates_without_changing_issuance_or_verification():
    now = time.time()
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(
        """
        CREATE TABLE products(id TEXT PRIMARY KEY,config TEXT NOT NULL,created REAL NOT NULL);
        CREATE TABLE cards(id TEXT PRIMARY KEY,digest TEXT UNIQUE NOT NULL,product_id TEXT NOT NULL REFERENCES products(id),state TEXT NOT NULL DEFAULT 'ready',created REAL NOT NULL);
        CREATE TABLE events(id TEXT PRIMARY KEY,type TEXT NOT NULL,job_id TEXT,product_id TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL);
        CREATE TABLE card_batches(id TEXT PRIMARY KEY,product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,label TEXT NOT NULL DEFAULT '',created REAL NOT NULL,count INTEGER NOT NULL);
        CREATE TABLE card_meta(card_id TEXT PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,batch_id TEXT REFERENCES card_batches(id) ON DELETE SET NULL,code_suffix TEXT,expires REAL,first_verified REAL);
        """
    )
    c.execute("INSERT INTO products VALUES (?,?,?)", ("legacy-product", "{}", now))
    c.execute(
        "INSERT INTO cards VALUES (?,?,?,?,?)",
        ("legacy-card", "legacy-hash", "legacy-product", "ready", now),
    )
    c.execute(
        "INSERT INTO card_batches VALUES (?,?,?,?,?)",
        ("legacy-batch", "legacy-product", "历史批次", now, 1),
    )
    c.execute(
        "INSERT INTO card_meta VALUES (?,?,?,?,?)",
        ("legacy-card", "legacy-batch", "ABCDEF", now + 86400, now + 1),
    )
    before = {
        table: dict(c.execute(f"SELECT * FROM {table}").fetchone())
        for table in ("card_meta", "card_batches")
    }
    init_schema(c)
    init_schema(c)
    for table in ("card_meta", "card_batches"):
        after = dict(c.execute(f"SELECT * FROM {table}").fetchone())
        assert {key: after[key] for key in before[table]} == before[table]
        assert after["variant_id"] == "default"
        assert c.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1
    meta = c.execute("SELECT * FROM card_meta").fetchone()
    assert json.loads(meta["variant_snapshot"]) == {}
    assert not c.execute("PRAGMA foreign_key_check").fetchall()
    c.close()


def test_two_variants_have_exact_counts_and_scoped_inventory_filters(owner):
    product = create_product(
        owner, [variant("basic", "基础版"), variant("premium", "高级版", price="20.00")]
    )
    pid = product["id"]
    basic = issue_variant(owner, pid, "basic", 4, label="基础批次")
    premium = issue_variant(owner, pid, "premium", 3, label="高级批次")
    unused, revoked, retryable, succeeded = basic
    expired, queued, processing = premium
    assert (
        owner.post(f"/api/admin/cards/{card_id(revoked)}/revoke", json={}).status_code
        == 200
    )
    _, task = redeem(owner, retryable)
    batch(owner, task, "claim")
    batch(owner, task, "fail", retryable=True)
    token, task = redeem(owner, succeeded)
    finish(owner, task, "PRIVATE DELIVERY")
    assert owner.post("/api/receipt/reveal", json={"token": token}).status_code == 200
    expire(expired)
    redeem(owner, queued)
    _, task = redeem(owner, processing)
    batch(owner, task, "claim")
    result = stats(owner, pid)
    assert result["variants"] == result["products"][0]["variants"]
    scoped_product = result["products"][0]
    assert_variants_sum_to_product(scoped_product)
    buckets = {v["variant_id"]: v for v in result["variants"]}
    assert set(buckets) == {"basic", "premium"}
    assert buckets["basic"]["summary"]["total"] == 4
    assert buckets["basic"]["summary"]["remaining"] == 1
    assert buckets["basic"]["summary"]["available"] == 2
    assert buckets["basic"]["summary"]["states"]["revoked"] == 1
    assert buckets["basic"]["summary"]["states"]["failed_retryable"] == 1
    assert buckets["premium"]["summary"]["states"]["expired"] == 1
    assert buckets["premium"]["summary"]["in_progress"] == 2
    assert buckets["premium"]["summary"]["available"] == 0
    assert scoped_product["total"] == 7 and scoped_product["used"] == 4
    assert scoped_product["viewed"] == 1 and scoped_product["completed"] == 1
    global_stats = stats(owner)
    assert global_stats["variants"] == []
    assert_variants_sum_to_product(global_stats["products"][0])
    filtered = inventory(owner, pid, variant_id="basic", limit=1, offset=1)
    assert filtered["total"] == 4 and len(filtered["items"]) == 1
    assert filtered["items"][0]["variant_id"] == "basic"
    assert filtered["items"][0]["variant_name"] == "基础版"
    assert filtered["summary"] == result["summary"]
    filtered = inventory(owner, pid, variant_id="basic", status="unused")
    assert [row["id"] for row in filtered["items"]] == [card_id(unused)]
    assert filtered["total"] == 1 and filtered["summary"]["total"] == 7
    assert inventory(owner, pid, variant_id="missing")["total"] == 0


def test_metadata_and_batch_freeze_variant_snapshot_without_inventory_leak(owner):
    chosen = variant(
        "basic", "原规格", attributes={"activation_tier": "PRIVATE ATTRIBUTE VALUE"}
    )
    product = create_product(owner, [chosen])
    pid = product["id"]
    code = issue_variant(owner, pid, "basic")[0]
    with db() as c:
        meta = dict(
            c.execute(
                "SELECT * FROM card_meta WHERE card_id=?",
                (card_id_from_connection(c, code),),
            ).fetchone()
        )
        snapshot = json.loads(meta["variant_snapshot"])
        assert meta["variant_id"] == "basic"
        assert snapshot["id"] == "basic" and snapshot["name"] == "原规格"
        assert snapshot["price"] == product["variants"][0]["price"]
        assert snapshot["currency"] == "CNY"
        assert snapshot["attributes"] == chosen["attributes"]
        batch_row = c.execute(
            "SELECT * FROM card_batches WHERE id=?", (meta["batch_id"],)
        ).fetchone()
        assert batch_row["variant_id"] == "basic"
        record_issue(
            c,
            pid,
            [code],
            variant_id="basic",
            variant_snapshot={**snapshot, "name": "不能覆写"},
        )
        assert (
            dict(
                c.execute(
                    "SELECT * FROM card_meta WHERE card_id=?", (meta["card_id"],)
                ).fetchone()
            )
            == meta
        )
    updated = edit_variants(
        owner, product, [{**chosen, "name": "新名称", "price": "99.00"}]
    )
    assert updated["variants"][0]["name"] == "新名称"
    row = inventory(owner, pid)["items"][0]
    assert row["variant_id"] == "basic" and row["variant_name"] == "原规格"
    assert "variant_snapshot" not in row and "attributes" not in row
    response = owner.get(f"/api/admin/cards/{card_id(code)}/history")
    assert response.status_code == 200, response.text
    assert response.json()["card"]["variant_name"] == "原规格"
    for safe_text in (json.dumps(row), response.text, json.dumps(stats(owner, pid))):
        assert "PRIVATE ATTRIBUTE VALUE" not in safe_text
        assert code not in safe_text


def card_id_from_connection(c, code):
    return c.execute(
        "SELECT id FROM cards WHERE digest=?", (card_digest(code),)
    ).fetchone()[0]


def test_disabled_and_removed_variants_keep_historical_inventory(owner):
    basic = variant("basic", "旧基础版")
    premium = variant("premium", "高级版", price="20.00")
    product = create_product(owner, [basic, premium])
    pid = product["id"]
    basic_code = issue_variant(owner, pid, "basic")[0]
    issue_variant(owner, pid, "premium", 2)
    product = edit_variants(owner, product, [{**basic, "enabled": False}, premium])
    result = stats(owner, pid)
    buckets = {v["variant_id"]: v for v in result["variants"]}
    assert not buckets["basic"]["enabled"] and buckets["basic"]["summary"]["total"] == 1
    assert_variants_sum_to_product(result["products"][0])
    response = owner.post(
        "/api/admin/cards", json={"product_id": pid, "variant_id": "basic", "count": 1}
    )
    assert response.status_code == 409, response.text
    response = owner.put(
        f"/api/admin/products/{pid}", json={**product, "variants": [premium]}
    )
    assert response.status_code == 409, response.text
    assert "basic" in {v["variant_id"] for v in stats(owner, pid)["variants"]}
    # Imported/older configurations can omit an already issued SKU even though
    # normal API edits protect its ID. Reporting must still retain that stock.
    with db() as c:
        config = json.loads(
            c.execute("SELECT config FROM products WHERE id=?", (pid,)).fetchone()[0]
        )
        config["variants"] = [premium]
        c.execute("UPDATE products SET config=? WHERE id=?", (json.dumps(config), pid))
    result = stats(owner, pid)
    buckets = {v["variant_id"]: v for v in result["variants"]}
    assert set(buckets) == {"basic", "premium"}
    assert buckets["basic"]["name"] == "旧基础版" and not buckets["basic"]["enabled"]
    assert buckets["basic"]["summary"]["remaining"] == 1
    assert buckets["premium"]["summary"]["total"] == 2
    assert_variants_sum_to_product(result["products"][0])
    rows = inventory(owner, pid, variant_id="basic")["items"]
    assert [r["id"] for r in rows] == [card_id(basic_code)]
    assert rows[0]["variant_name"] == "旧基础版"
    assert owner.post("/api/exchange", json={"code": basic_code}).status_code == 200


def test_legacy_cards_without_metadata_have_default_variant_and_empty_attributes(owner):
    product = create_product(owner)
    pid = product["id"]
    code = "LEGACY-SECRET-CARD"
    cid = str(uuid.uuid4())
    with db() as c:
        c.execute(
            "INSERT INTO cards(id,digest,product_id,created) VALUES (?,?,?,?)",
            (cid, card_digest(code), pid, time.time()),
        )
    result = stats(owner, pid)
    assert [v["variant_id"] for v in result["variants"]] == ["default"]
    assert result["variants"][0]["summary"]["total"] == 1
    assert_variants_sum_to_product(result["products"][0])
    row = inventory(owner, pid, variant_id="default")["items"][0]
    assert row["id"] == cid and row["variant_id"] == "default"
    assert row["code_suffix"] is None and row["batch_id"] is None
    assert "variant_snapshot" not in row and "attributes" not in row
    assert owner.post("/api/exchange", json={"code": code}).status_code == 200
    with db() as c:
        meta = c.execute("SELECT * FROM card_meta WHERE card_id=?", (cid,)).fetchone()
        assert (
            meta["variant_id"] == "default"
            and json.loads(meta["variant_snapshot"]) == {}
        )


def test_new_default_issuance_and_zero_card_variants_are_visible(owner):
    product = create_product(owner)
    pid = product["id"]
    code = issue_variant(owner, pid)[0]
    row = inventory(owner, pid)["items"][0]
    assert row["id"] == card_id(code) and row["variant_id"] == "default"
    other = create_product(
        owner, [variant("basic", "基础版"), variant("premium", "高级版", enabled=False)]
    )
    result = stats(owner, other["id"])
    assert {v["variant_id"] for v in result["variants"]} == {"basic", "premium"}
    assert all(v["summary"]["total"] == 0 for v in result["variants"])
    assert_variants_sum_to_product(result["products"][0])


@pytest.mark.parametrize("variant_id,status", (("missing", 400), ("disabled", 409)))
def test_unknown_or_disabled_variant_cannot_issue_cards(owner, variant_id, status):
    product = create_product(
        owner,
        [variant("basic", "基础版"), variant("disabled", "停用版", enabled=False)],
    )
    response = owner.post(
        "/api/admin/cards",
        json={"product_id": product["id"], "variant_id": variant_id, "count": 3},
    )
    assert response.status_code == status, response.text
    result = stats(owner, product["id"])
    assert result["summary"]["total"] == 0
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM card_batches WHERE product_id=?", (product["id"],)
            ).fetchone()[0]
            == 0
        )


def test_variant_stats_and_filters_respect_management_product_scope(owner):
    product = create_product(owner, [variant("basic", "授权基础版")])
    pid = product["id"]
    code = issue_variant(owner, pid, "basic")[0]
    other = create_product(owner, [variant("foreign", "未授权规格")])
    issue_variant(owner, other["id"], "foreign", 2)
    link = create_link(owner, pid, ["cards.manage"])
    login_link(owner, link)
    for endpoint in ("card-stats", "card-inventory"):
        response = owner.get(f"/api/manage/{endpoint}", params={"variant_id": "basic"})
        assert response.status_code == 200, response.text
        assert response.json()["summary"]["total"] == 1
        assert other["id"] not in response.text and "未授权规格" not in response.text
        assert (
            owner.get(
                f"/api/manage/{endpoint}",
                params={"product_id": other["id"], "variant_id": "foreign"},
            ).status_code
            == 403
        )
    response = owner.get("/api/manage/card-inventory", params={"variant_id": "foreign"})
    assert response.status_code == 200 and response.json()["items"] == []
    assert response.json()["summary"]["total"] == 1
    assert (
        owner.get(f"/api/manage/cards/{card_id(code)}/history").json()["card"][
            "variant_id"
        ]
        == "basic"
    )


def test_equal_variant_ids_in_distinct_products_are_not_merged(owner):
    first = create_product(owner, [variant("basic", "商品 A 基础版")])
    second = create_product(owner, [variant("basic", "商品 B 基础版")])
    issue_variant(owner, first["id"], "basic", 1)
    issue_variant(owner, second["id"], "basic", 3)
    result = stats(owner)
    assert result["summary"]["total"] == 4 and result["variants"] == []
    products = {p["product_id"]: p for p in result["products"]}
    assert products[first["id"]]["variants"][0]["summary"]["total"] == 1
    assert products[second["id"]]["variants"][0]["summary"]["total"] == 3
    for product in result["products"]:
        assert_variants_sum_to_product(product)


@pytest.mark.parametrize("variant_id", ("30-days", "a" * 40))
def test_valid_variant_slug_is_preserved_by_real_issuance(owner, variant_id):
    product = create_product(owner, [variant(variant_id, "有效规格")])
    code = issue_variant(owner, product["id"], variant_id)[0]
    row = inventory(owner, product["id"], variant_id=variant_id)["items"][0]
    assert row["id"] == card_id(code) and row["variant_id"] == variant_id
    response = owner.post("/api/exchange", json={"code": code})
    assert response.status_code == 200, response.text
    assert response.json()["variant"]["id"] == variant_id


def test_legacy_card_does_not_inherit_current_custom_variant_attributes(owner):
    product = create_product(
        owner,
        [variant("premium", "新高级版", attributes={"access": "current premium"})],
    )
    pid = product["id"]
    code = "LEGACY-WITHOUT-SNAPSHOT"
    cid = str(uuid.uuid4())
    with db() as c:
        c.execute(
            "INSERT INTO cards(id,digest,product_id,created) VALUES (?,?,?,?)",
            (cid, card_digest(code), pid, time.time()),
        )
    result = stats(owner, pid)
    buckets = {v["variant_id"]: v for v in result["variants"]}
    assert set(buckets) == {"default", "premium"}
    assert buckets["default"]["summary"]["total"] == 1
    assert not buckets["default"]["enabled"]
    assert buckets["premium"]["summary"]["total"] == 0
    assert_variants_sum_to_product(result["products"][0])
    response = owner.post("/api/exchange", json={"code": code})
    assert response.status_code == 200, response.text
    assert response.json()["variant"]["id"] == "default"
    assert response.json()["variant"]["attributes"] == {}


@pytest.mark.parametrize(
    "snapshot",
    (
        None,
        variant("different", "不匹配规格"),
        variant("basic", "价格无效", price="NaN"),
        variant("basic", "嵌套属性", attributes={"nested": {"secret": "value"}}),
    ),
)
def test_nondefault_snapshot_is_required_and_validated_before_recording(
    owner, snapshot
):
    product = create_product(owner, [variant("basic", "基础版")])
    pid = product["id"]
    cid = str(uuid.uuid4())
    code = "UNRECORDED-CARD-" + cid
    with db() as c:
        c.execute(
            "INSERT INTO cards(id,digest,product_id,created) VALUES (?,?,?,?)",
            (cid, card_digest(code), pid, time.time()),
        )
        with pytest.raises(HTTPException) as error:
            record_issue(c, pid, [code], variant_id="basic", variant_snapshot=snapshot)
        assert error.value.status_code == 400
        assert (
            c.execute(
                "SELECT count(*) FROM card_meta WHERE card_id=?", (cid,)
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT count(*) FROM card_batches WHERE product_id=?", (pid,)
            ).fetchone()[0]
            == 0
        )

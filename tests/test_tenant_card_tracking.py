"""Card identifiers, filters and aggregate counts cannot cross shop boundaries."""

import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response
from test_card_tracking import card_id
from test_product_links import create_link, login_link
from test_redemption import redeem

from extore.app import app
from extore.db import db
from extore.models import Product
from extore.security import create_session
from extore.service import issue_cards


@pytest.fixture
def tenant_cards(owner):
    shops = [str(uuid.uuid4()), str(uuid.uuid4())]
    a_pid, b_pid = str(uuid.uuid4()), str(uuid.uuid4())
    with db() as c:
        c.executemany(
            "INSERT INTO shops(id,name,created,verified) VALUES (?,?,?,1)",
            [
                (sid, f"独立商店 {index}", time.time())
                for index, sid in enumerate(shops)
            ],
        )
        for pid, sid, name in (
            (a_pid, shops[0], "甲店商品"),
            (b_pid, shops[1], "乙店私有商品"),
        ):
            config = Product(
                name=name,
                parameters=[
                    {"key": "email", "label": {"zh-CN": "邮箱"}, "type": "email"}
                ],
            ).model_dump()
            c.execute(
                "INSERT INTO products(id,config,created,shop_id) VALUES (?,?,?,?)",
                (pid, json.dumps(config), time.time(), sid),
            )
        a_codes = issue_cards(c, a_pid, 3, label="甲店库存")
        b_codes = issue_cards(c, b_pid, 2, label="乙店私有批次")
        a_value = create_session(c, Response(), "admin", shop_id=shops[0])
        b_value = create_session(c, Response(), "admin", shop_id=shops[1])
        # These are already-authenticated fixtures. Authentication ceremonies
        # are covered separately; assertions here exercise actual HTTP queries.
        b_batch = c.execute(
            "SELECT id FROM card_batches WHERE product_id=?", (b_pid,)
        ).fetchone()[0]
    _, b_job = redeem(owner, b_codes[0])
    with (
        TestClient(
            app,
            base_url="http://localhost:8000",
            headers={"Origin": "http://localhost:8000"},
        ) as a_client,
        TestClient(
            app,
            base_url="http://localhost:8000",
            headers={"Origin": "http://localhost:8000"},
        ) as b_client,
    ):
        a_client.cookies.set("extore_session", a_value)
        b_client.cookies.set("extore_session", b_value)
        yield {
            "a": a_client,
            "b": b_client,
            "a_shop": shops[0],
            "b_shop": shops[1],
            "a_pid": a_pid,
            "b_pid": b_pid,
            "a_codes": a_codes,
            "b_codes": b_codes,
            "b_batch": b_batch,
            "b_job": b_job,
        }


@pytest.mark.parametrize("prefix", ("admin", "manage"))
def test_unfiltered_stats_only_include_current_shop(tenant_cards, prefix):
    a = tenant_cards["a"].get(f"/api/{prefix}/card-stats")
    b = tenant_cards["b"].get(f"/api/{prefix}/card-stats")
    assert a.status_code == b.status_code == 200
    assert a.json()["summary"]["total"] == 3
    assert a.json()["summary"]["remaining"] == 3
    assert b.json()["summary"]["total"] == 2
    assert b.json()["summary"]["in_progress"] == 1
    assert [p["product_id"] for p in a.json()["products"]] == [tenant_cards["a_pid"]]
    assert [p["product_id"] for p in b.json()["products"]] == [tenant_cards["b_pid"]]
    assert "乙店私有" not in json.dumps(a.json(), ensure_ascii=False)


@pytest.mark.parametrize("prefix", ("admin", "manage"))
def test_inventory_filters_cannot_disclose_foreign_card_or_batch(tenant_cards, prefix):
    client = tenant_cards["a"]
    url = f"/api/{prefix}/card-inventory"
    result = client.get(url).json()
    assert result["total"] == result["summary"]["total"] == 3
    assert {item["product_id"] for item in result["items"]} == {tenant_cards["a_pid"]}
    foreign_id = card_id(tenant_cards["b_codes"][1])
    foreign_suffix = tenant_cards["b_codes"][1].replace("-", "")[-6:]
    for params in (
        {"batch_id": tenant_cards["b_batch"]},
        {"search": foreign_id},
        {"search": foreign_suffix},
        {"status": "queued"},
    ):
        response = client.get(url, params=params)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["items"] == []
        assert result["total"] == 0
        assert result["summary"]["total"] == 3


@pytest.mark.parametrize("prefix", ("admin", "manage"))
@pytest.mark.parametrize("endpoint", ("card-stats", "card-inventory"))
def test_explicit_foreign_product_is_denied(tenant_cards, prefix, endpoint):
    response = tenant_cards["a"].get(
        f"/api/{prefix}/{endpoint}",
        params={"product_id": tenant_cards["b_pid"]},
    )
    assert response.status_code == 403
    assert "乙店" not in response.text


@pytest.mark.parametrize("prefix", ("admin", "manage"))
def test_guessed_card_history_is_denied_without_product_filter(tenant_cards, prefix):
    cid = card_id(tenant_cards["b_codes"][0])
    url = f"/api/{prefix}/cards/{cid}/history"
    for params in ({}, {"product_id": tenant_cards["b_pid"]}):
        response = tenant_cards["a"].get(url, params=params)
        assert response.status_code == 403
        assert "乙店" not in response.text
        assert cid not in response.text
        assert tenant_cards["b_job"]["id"] not in response.text
    response = tenant_cards["b"].get(url)
    assert response.status_code == 200
    result = response.json()
    assert result["card"]["product_id"] == tenant_cards["b_pid"]
    assert result["card"]["job_id"] == tenant_cards["b_job"]["id"]
    assert "params" not in result["card"]
    assert "digest" not in result["card"]
    assert "user@example.com" not in response.text


def test_platform_owner_keeps_explicit_global_view(owner, tenant_cards):
    for prefix in ("admin", "manage"):
        response = owner.get(f"/api/{prefix}/card-stats")
        assert response.status_code == 200, response.text
        assert response.json()["summary"]["total"] == 5
        assert {p["product_id"] for p in response.json()["products"]} == {
            tenant_cards["a_pid"],
            tenant_cards["b_pid"],
        }


def test_staff_card_permission_does_not_expand_to_shop_wide(owner, tenant_cards):
    link = create_link(owner, tenant_cards["a_pid"], ["cards.manage"])
    login_link(owner, link)
    for endpoint in ("card-stats", "card-inventory"):
        response = owner.get(f"/api/manage/{endpoint}")
        assert response.status_code == 200, response.text
        assert response.json()["summary"]["total"] == 3
        assert (
            owner.get(
                f"/api/manage/{endpoint}",
                params={"product_id": tenant_cards["b_pid"]},
            ).status_code
            == 403
        )
    assert (
        owner.get(
            f"/api/manage/cards/{card_id(tenant_cards['b_codes'][0])}/history"
        ).status_code
        == 403
    )


@pytest.mark.parametrize("prefix", ("admin", "manage"))
def test_disabled_shop_session_cannot_read_inventory(tenant_cards, prefix):
    with db() as c:
        c.execute("UPDATE shops SET enabled=0 WHERE id=?", (tenant_cards["a_shop"],))
    for endpoint in ("card-stats", "card-inventory"):
        response = tenant_cards["a"].get(f"/api/{prefix}/{endpoint}")
        assert response.status_code == 401

"""Factory/workshop guidance is scoped configuration, never customer authority."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from test_automation_api import device, next_, queue, request
from test_processor_profiles import merchant
from test_product_links import create_link, login_link
from test_progress_board import PRIVATE, seed
from test_shop_migration import legacy_connection

from extore import service, shops, worker
from extore.app import app
from extore.db import db
from extore.models import JobUpdate
from extore.work_instructions import instructions

FACTORY = "先核验来源，再交付。\nKeep the result editable."
WORKSHOP = "为本车间制作可编辑的文件，不编造引用。"


def create(client, **extra):
    response = client.post("/api/admin/products", json={"name": "电子车间", **extra})
    assert response.status_code == 200, response.text
    return response.json()


def factory(client, sid, value=FACTORY):
    response = client.put(
        "/api/admin/factory",
        params={"shop_id": sid},
        json={"factory_slogan": value},
    )
    assert response.status_code == 200, response.text
    return response.json()


def staff_client(owner, product_id, permissions):
    link = create_link(owner, product_id, permissions)
    client = TestClient(
        app,
        base_url="http://localhost:8000",
        headers={"Origin": "http://localhost:8000"},
    )
    login_link(client, link)
    return client, link


def test_factory_is_shop_local_and_root_selection_is_explicit(owner):
    a, aid = merchant()
    b, bid = merchant()
    assert a.get("/api/admin/factory").json()["factory_slogan"] == ""
    assert factory(a, aid)["factory_slogan"] == FACTORY
    assert b.get("/api/admin/factory").json()["factory_slogan"] == ""
    assert a.get("/api/admin/factory", params={"shop_id": bid}).status_code == 403
    assert (
        a.put(
            "/api/admin/factory", params={"shop_id": bid}, json={"factory_slogan": "x"}
        ).status_code
        == 403
    )
    assert owner.get("/api/admin/factory").status_code == 400
    assert (
        owner.put("/api/admin/factory", json={"factory_slogan": "x"}).status_code == 400
    )
    assert factory(owner, bid, "B 工厂")["shop_id"] == bid
    assert b.get("/api/admin/factory").json()["factory_slogan"] == "B 工厂"
    assert a.get("/api/admin/factory").json()["factory_slogan"] == FACTORY
    with db() as c:
        assert all(
            FACTORY not in row["target"] and WORKSHOP not in row["target"]
            for row in c.execute("SELECT target FROM audit")
        )


@pytest.mark.parametrize(
    "value",
    [
        "x" * 4001,
        "bad\x00text",
        "bad\x7ftext",
        "bad\x85text",
        "\ud800",
        123,
        True,
        None,
    ],
)
def test_invalid_guidance_never_changes_configuration(owner, value):
    p = create(owner, workshop_slogan=WORKSHOP)
    sid = p["shop_id"]
    factory(owner, sid)
    assert (
        owner.put(
            "/api/admin/factory",
            params={"shop_id": sid},
            content=json.dumps({"factory_slogan": value}),
            headers={"Content-Type": "application/json"},
        ).status_code
        == 422
    )
    assert (
        owner.put(
            "/api/manage/workshop",
            params={"product_id": p["id"]},
            content=json.dumps({"workshop_slogan": value}),
            headers={"Content-Type": "application/json"},
        ).status_code
        == 422
    )
    with db() as c:
        value = instructions(c, p["id"])
    assert value["factory_slogan"] == FACTORY
    assert value["workshop_slogan"] == WORKSHOP


@pytest.mark.parametrize("field", ("factory_slogan", "workshop_slogan"))
@pytest.mark.parametrize("invalid", ("value", "mapping-key", "extra-key"))
def test_validation_rejects_surrogates_without_echoing_private_input(
    owner, field, invalid
):
    private = "PRIVATE_VALIDATION_INPUT_DO_NOT_ECHO_782431"
    p = create(owner, workshop_slogan=WORKSHOP)
    factory(owner, p["shop_id"])
    body = {field: "safe text"}
    if invalid == "value":
        body[field] = "\ud800" + private
    elif invalid == "mapping-key":
        body[field] = {"\ud800": private}
    else:
        body["\ud800"] = private
    endpoint = (
        "/api/admin/factory" if field == "factory_slogan" else "/api/manage/workshop"
    )
    params = (
        {"shop_id": p["shop_id"]}
        if field == "factory_slogan"
        else {"product_id": p["id"]}
    )
    response = owner.put(
        endpoint,
        params=params,
        content=json.dumps(body),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert private not in response.text
    errors = response.json()["detail"]
    assert errors and all(set(error) == {"type", "loc", "msg"} for error in errors)
    with db() as c:
        current = instructions(c, p["id"])
    assert current["factory_slogan"] == FACTORY
    assert current["workshop_slogan"] == WORKSHOP


def test_markdown_unicode_and_exact_empty_values_are_supported(owner):
    text = "# 标语 💡\r\n\t只使用已有权限。\n" + "字" * 3979
    assert len(text) <= 4000
    p = create(owner, workshop_slogan=text)
    assert p["workshop_slogan"] == text
    assert factory(owner, p["shop_id"], text)["factory_slogan"] == text
    cleared = owner.put(
        "/api/manage/workshop",
        params={"product_id": p["id"]},
        json={"workshop_slogan": ""},
    )
    assert cleared.status_code == 200 and cleared.json()["workshop_slogan"] == ""
    assert factory(owner, p["shop_id"], "  \n\t")["factory_slogan"] == ""


def test_workshop_partial_write_keeps_delivery_fields_and_legacy_puts_preserve(owner):
    p = create(owner, workshop_slogan=WORKSHOP)
    with db() as c:
        before = json.loads(
            c.execute("SELECT config FROM products WHERE id=?", (p["id"],)).fetchone()[
                0
            ]
        )
    edit, _ = staff_client(owner, p["id"], ["product.edit"])
    updated = edit.put("/api/manage/workshop", json={"workshop_slogan": "新的车间标语"})
    assert updated.status_code == 200, updated.text
    assert updated.json() == {"product_id": p["id"], "workshop_slogan": "新的车间标语"}
    with db() as c:
        after = json.loads(
            c.execute("SELECT config FROM products WHERE id=?", (p["id"],)).fetchone()[
                0
            ]
        )
    assert {k: v for k, v in before.items() if k != "workshop_slogan"} == {
        k: v for k, v in after.items() if k != "workshop_slogan"
    }
    assert (
        edit.put(
            "/api/admin/factory", json={"factory_slogan": "staff escalation"}
        ).status_code
        == 403
    )
    old_client_body = dict(before)
    old_client_body.pop("workshop_slogan")
    response = edit.put("/api/manage/product", json=old_client_body)
    assert response.status_code == 200, response.text
    assert response.json()["workshop_slogan"] == "新的车间标语"
    response = owner.put("/api/admin/products/" + p["id"], json=old_client_body)
    assert response.status_code == 200, response.text
    assert response.json()["workshop_slogan"] == "新的车间标语"


def test_monitor_sees_slogans_without_task_payload_or_write_authority(
    owner, monkeypatch
):
    p = create(owner, workshop_slogan=WORKSHOP, public=True)
    factory(owner, p["shop_id"])
    seed(p["id"])
    monitor, _ = staff_client(owner, p["id"], ["queue.monitor"])

    def forbidden(*args, **kwargs):
        raise AssertionError("Slogan projection read task/configuration contents")

    monkeypatch.setattr(service, "product", forbidden)
    monkeypatch.setattr(service, "job_view", forbidden)
    board = monitor.get("/api/manage/progress-board")
    assert board.status_code == 200, board.text
    assert board.json()["shop"]["factory_slogan"] == FACTORY
    assert board.json()["products"][0]["workshop_slogan"] == WORKSHOP
    assert PRIVATE not in board.text
    read = monitor.get("/api/manage/instructions")
    assert read.status_code == 200 and read.json()["workshop_slogan"] == WORKSHOP
    assert PRIVATE not in read.text
    assert monitor.get("/api/manage/jobs").status_code == 403
    assert (
        monitor.put(
            "/api/manage/workshop", json={"workshop_slogan": "forged"}
        ).status_code
        == 403
    )
    assert (
        monitor.put("/api/admin/factory", json={"factory_slogan": "forged"}).status_code
        == 403
    )


def test_reads_are_scoped_fresh_and_revision_changes_only_with_config(owner):
    a, aid = merchant()
    b, _ = merchant()
    pa = create(a, workshop_slogan=WORKSHOP)
    pb = create(b, workshop_slogan="B 私有车间")
    factory(a, aid)
    monitor, link = staff_client(a, pa["id"], ["queue.monitor"])
    original = monitor.get("/api/manage/instructions").json()
    assert original["schema"] == "extore.work-instructions.v1"
    assert original["shop_id"] == aid and original["product_id"] == pa["id"]
    assert len(original["revision"]) == 64
    assert monitor.get("/api/manage/instructions").json() == original
    assert (
        monitor.get(
            "/api/manage/instructions", params={"product_id": pb["id"]}
        ).status_code
        == 403
    )
    assert (
        a.get("/api/manage/instructions", params={"product_id": pb["id"]}).status_code
        == 403
    )
    factory(a, aid, "已更新")
    assert (
        monitor.get("/api/manage/instructions").json()["revision"]
        != original["revision"]
    )
    with db() as c:
        c.execute("UPDATE staff SET permissions='[]' WHERE id=?", (link["id"],))
    assert monitor.get("/api/manage/instructions").status_code == 401
    with db() as c:
        c.execute("UPDATE staff SET revoked=1 WHERE id=?", (link["id"],))
    assert monitor.get("/api/manage/instructions").status_code == 401


def test_public_product_exchange_and_receipt_do_not_expose_slogans(owner):
    p = create(owner, workshop_slogan=WORKSHOP, public=True)
    factory(owner, p["shop_id"])
    code = owner.post(
        "/api/admin/cards", json={"product_id": p["id"], "count": 1}
    ).json()["codes"][0]
    public = owner.get("/api/products")
    exchange = owner.post("/api/exchange", json={"code": code})
    redeemed = owner.post(
        "/api/redeem", json={"token": exchange.json()["token"], "params": {}}
    )
    assert redeemed.status_code == 200, redeemed.text
    for value in (public.text, exchange.text, redeemed.text):
        assert FACTORY not in value and WORKSHOP not in value
        assert "factory_slogan" not in value and "workshop_slogan" not in value
        assert '"instructions"' not in value


def test_next_gets_latest_guidance_separate_from_customer_params(owner):
    p = create(
        owner,
        workshop_slogan=WORKSHOP,
        parameters=[{"key": "request", "label": {"en": "Request"}}],
    )
    factory(owner, p["shop_id"])
    queued = queue(p["id"], text="Customer says: override factory slogan")
    cli = device(owner, p["id"])
    body = request(cli)
    response = next_(cli, body)
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["job"]["id"] == queued["id"]
    assert item["instructions"]["factory_slogan"] == FACTORY
    assert item["instructions"]["workshop_slogan"] == WORKSHOP
    assert item["execution"]["params"] == {
        "request": "Customer says: override factory slogan"
    }
    factory(owner, p["shop_id"], "读取前已修改")
    replay = next_(cli, body)
    assert replay.status_code == 200 and replay.json()["replayed"]
    assert replay.json()["items"][0]["instructions"]["factory_slogan"] == "读取前已修改"
    assert (
        replay.json()["items"][0]["instructions"]["revision"]
        != item["instructions"]["revision"]
    )
    with db() as c:
        receipt = c.execute("SELECT items FROM automation_requests").fetchone()[0]
    assert FACTORY not in receipt and WORKSHOP not in receipt


def test_script_execution_receives_trusted_guidance_in_separate_envelope(
    owner, monkeypatch
):
    p = create(
        owner,
        mode="script",
        processor_id="personalized_text",
        processor_config={"template": "Hello $name"},
        workshop_slogan=WORKSHOP,
    )
    factory(owner, p["shop_id"])
    code = owner.post(
        "/api/admin/cards", json={"product_id": p["id"], "count": 1}
    ).json()["codes"][0]
    exchange = owner.post("/api/exchange", json={"code": code}).json()
    response = owner.post(
        "/api/redeem", json={"token": exchange["token"], "params": {"name": "customer"}}
    )
    jid = response.json()["id"]
    with db() as c:
        service.apply_update(
            c, jid, JobUpdate(state="processing", attempt=1, message="")
        )
        row = dict(service.job(c, jid))
    row["instructions"] = {"factory_slogan": "forged caller"}
    seen = []

    async def capture(row, product, package):
        seen.append(row["instructions"])
        assert row["params"] == '{"name": "customer"}'
        return JobUpdate(
            state="succeeded", attempt=1, output={"content": "Hello customer"}
        )

    monkeypatch.setattr(worker, "_execute_processor", capture)
    asyncio.run(worker.execute_script(row, p))
    assert (
        seen[0]["factory_slogan"] == FACTORY and seen[0]["workshop_slogan"] == WORKSHOP
    )


def test_factory_migration_is_additive_and_does_not_rewrite_historical_config():
    c = legacy_connection()
    c.execute(
        "CREATE TABLE shops(id TEXT PRIMARY KEY,name TEXT NOT NULL,email TEXT UNIQUE,password_hash TEXT,enabled INTEGER NOT NULL DEFAULT 1,created REAL NOT NULL,verified INTEGER NOT NULL DEFAULT 0,legacy INTEGER NOT NULL DEFAULT 0,totp_secret TEXT)"
    )
    c.execute(
        "INSERT INTO shops(id,name,email,password_hash,created,verified,legacy,totp_secret) VALUES ('legacy','旧店铺','private@example.test','private-hash',1,1,1,'ciphertext')"
    )
    config = '{"name":"旧商品","mode":"manual","private":"preserve exact bytes"}'
    c.execute("INSERT INTO products(id,config,created) VALUES ('p',?,2)", (config,))
    before = tuple(c.execute("SELECT * FROM shops").fetchone())
    columns = [r["name"] for r in c.execute("PRAGMA table_info(shops)")]
    shops.init_schema(c)
    shops.init_schema(c)
    assert (
        tuple(c.execute("SELECT " + ",".join(columns) + " FROM shops").fetchone())
        == before
    )
    assert (
        c.execute("SELECT factory_slogan FROM shops WHERE id='legacy'").fetchone()[0]
        == ""
    )
    assert c.execute("SELECT config FROM products WHERE id='p'").fetchone()[0] == config
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    c.close()

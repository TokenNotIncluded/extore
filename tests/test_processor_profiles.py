import asyncio
import json
import time
import uuid

from fastapi.testclient import TestClient
from starlette.responses import Response

from extore.app import app
from extore.db import db, init
from extore.security import create_session
from extore.worker import job_once


def merchant():
    sid = str(uuid.uuid4())
    client = TestClient(
        app,
        base_url="http://localhost:8000",
        headers={"Origin": "http://localhost:8000"},
    )
    with db() as c:
        c.execute(
            "INSERT INTO shops(id,name,created,verified) VALUES (?,?,?,1)",
            (sid, "配置隔离店铺", time.time()),
        )
        cookie = create_session(c, Response(), "admin", shop_id=sid)
    client.cookies.set("extore_session", cookie)
    return client, sid


def create_product(
    client, template="Hello $name", processor_id="personalized_text", configuration=None
):
    response = client.post(
        "/api/admin/products",
        json={
            "name": "隔离处理器商品",
            "mode": "script",
            "processor_id": processor_id,
            "processor_config": configuration
            if configuration is not None
            else {"template": template},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def binding(client, pid):
    response = client.get(f"/api/admin/processor-profiles/bindings/{pid}")
    assert response.status_code == 200, response.text
    return response.json()["profile"]


def issue(client, pid):
    response = client.post("/api/admin/cards", json={"product_id": pid, "count": 1})
    assert response.status_code == 200, response.text
    return response.json()["codes"][0]


def submit(client, code, params=None):
    exchanged = client.post("/api/exchange", json={"code": code}).json()
    response = client.post(
        "/api/redeem",
        json={
            "token": exchanged["token"],
            "params": params if params is not None else {"name": "Ada"},
        },
    )
    assert response.status_code == 200, response.text
    return exchanged["token"], response.json()


def test_profile_values_are_write_only_and_ciphertext_not_in_metadata(owner):
    product = create_product(owner, "PRIVATE-TEMPLATE $name")
    assert product["processor_config"] == {}
    profile = binding(owner, product["id"])
    for response in (
        owner.get("/api/admin/products"),
        owner.get("/api/admin/processor-profiles"),
        owner.get(f"/api/admin/processor-profiles/{profile['id']}"),
    ):
        assert response.status_code == 200
        assert (
            "PRIVATE-TEMPLATE" not in response.text
            and "ciphertext" not in response.text
        )
    with db() as c:
        raw = c.execute(
            "SELECT config FROM products WHERE id=?", (product["id"],)
        ).fetchone()[0]
        secret = c.execute(
            "SELECT ciphertext FROM processor_profile_revisions WHERE profile_id=?",
            (profile["id"],),
        ).fetchone()[0]
        assert "PRIVATE-TEMPLATE" not in raw + secret
        assert json.loads(raw)["processor_config"] == {}
        assert secret.startswith("v1.")


def test_same_processor_isolated_between_shops_and_cross_bind_denied(owner):
    a, sid_a = merchant()
    b, sid_b = merchant()
    pa = create_product(a, "Shop A $name")
    pb = create_product(b, "Shop B $name")
    profile_a = binding(a, pa["id"])
    profile_b = binding(b, pb["id"])
    assert profile_a["shop_id"] == sid_a and profile_b["shop_id"] == sid_b
    assert b.get(f"/api/admin/processor-profiles/{profile_a['id']}").status_code == 404
    assert (
        b.put(
            f"/api/admin/processor-profiles/{profile_a['id']}",
            json={"configuration": {"template": "EVIL $name"}},
        ).status_code
        == 404
    )
    assert (
        b.put(
            f"/api/admin/processor-profiles/bindings/{pb['id']}",
            json={"profile_id": profile_a["id"]},
        ).status_code
        == 404
    )
    assert (
        a.get("/api/admin/processor-profiles", params={"shop_id": sid_b}).status_code
        == 403
    )
    ra, _ = submit(a, issue(a, pa["id"]))
    rb, _ = submit(b, issue(b, pb["id"]))
    assert asyncio.run(job_once()) and asyncio.run(job_once())
    assert (
        a.post("/api/receipt/reveal", json={"token": ra}).json()["output"]["content"]
        == "Shop A Ada"
    )
    assert (
        b.post("/api/receipt/reveal", json={"token": rb}).json()["output"]["content"]
        == "Shop B Ada"
    )


def test_card_configuration_is_frozen_until_explicit_future_issuance_binding(owner):
    product = create_product(owner, "OLD $name")
    old_code = issue(owner, product["id"])
    profile = binding(owner, product["id"])
    response = owner.put(
        f"/api/admin/processor-profiles/{profile['id']}",
        json={"configuration": {"template": "NEW $name"}},
    )
    assert response.status_code == 200 and response.json()["revision"] == 2
    assert binding(owner, product["id"])["bound_revision"] == 1
    response = owner.put(
        f"/api/admin/processor-profiles/bindings/{product['id']}",
        json={"profile_id": profile["id"]},
    )
    assert response.status_code == 200
    new_code = issue(owner, product["id"])
    old_receipt, _ = submit(owner, old_code)
    new_receipt, _ = submit(owner, new_code)
    assert asyncio.run(job_once()) and asyncio.run(job_once())
    assert (
        owner.post("/api/receipt/reveal", json={"token": old_receipt}).json()["output"][
            "content"
        ]
        == "OLD Ada"
    )
    assert (
        owner.post("/api/receipt/reveal", json={"token": new_receipt}).json()["output"][
            "content"
        ]
        == "NEW Ada"
    )


def test_masked_product_edit_preserves_profile_and_configuration(owner):
    product = create_product(owner, "PRIVATE $name")
    profile = binding(owner, product["id"])
    response = owner.put(
        f"/api/admin/products/{product['id']}",
        json={**product, "name": "改标题", "processor_config": {}},
    )
    assert response.status_code == 200, response.text
    assert binding(owner, product["id"])["id"] == profile["id"]
    receipt, _ = submit(owner, issue(owner, product["id"]))
    assert asyncio.run(job_once())
    assert (
        owner.post("/api/receipt/reveal", json={"token": receipt}).json()["output"][
            "content"
        ]
        == "PRIVATE Ada"
    )


def test_profile_patch_preserves_omitted_secret_fields(owner):
    product = create_product(
        owner,
        processor_id="resource_link",
        configuration={"resource_url": "https://example.com/PRIVATE", "message": "old"},
    )
    profile = binding(owner, product["id"])
    response = owner.put(
        f"/api/admin/processor-profiles/{profile['id']}",
        json={"configuration": {"message": "new"}},
    )
    assert response.status_code == 200, response.text
    assert "PRIVATE" not in response.text
    assert (
        owner.put(
            f"/api/admin/processor-profiles/bindings/{product['id']}",
            json={"profile_id": profile["id"]},
        ).status_code
        == 200
    )
    code = issue(owner, product["id"])
    receipt, _ = submit(owner, code, {})
    assert asyncio.run(job_once())
    assert owner.post("/api/receipt/reveal", json={"token": receipt}).json()[
        "output"
    ] == {"resource_url": "https://example.com/PRIVATE", "message": "new"}


def test_incomplete_profile_can_be_completed_without_reading_secrets(owner):
    product = create_product(owner, processor_id="resource_link", configuration={})
    profile = binding(owner, product["id"])
    response = owner.put(
        f"/api/admin/processor-profiles/{profile['id']}",
        json={"configuration": {"resource_url": "https://example.com/PRIVATE"}},
    )
    assert response.status_code == 200 and response.json()["revision"] == 2
    assert "PRIVATE" not in response.text
    assert (
        owner.put(
            f"/api/admin/processor-profiles/bindings/{product['id']}",
            json={"profile_id": profile["id"]},
        ).status_code
        == 200
    )
    assert issue(owner, product["id"])


def test_fulfillment_management_link_cannot_read_or_switch_tenant_configuration(owner):
    product = create_product(owner, "ACCOUNT-SECRET $name")
    profile = binding(owner, product["id"])
    response = owner.post(
        "/api/admin/staff",
        json={
            "product_id": product["id"],
            "name": "配置经理",
            "permissions": ["product.edit", "fulfillment.configure"],
        },
    )
    assert response.status_code == 200, response.text
    staff = TestClient(
        app,
        base_url="http://localhost:8000",
        headers={"Origin": "http://localhost:8000"},
    )
    assert (
        staff.post(
            "/api/staff/login", json={"token": response.json()["url"].split("#")[1]}
        ).status_code
        == 200
    )
    assert (
        staff.get(f"/api/admin/processor-profiles/{profile['id']}").status_code == 401
    )
    assert (
        staff.put(
            f"/api/admin/processor-profiles/bindings/{product['id']}",
            json={"profile_id": profile["id"]},
        ).status_code
        == 401
    )
    config = staff.get("/api/manage/product").json()
    assert config["processor_config"] == {} and "ACCOUNT-SECRET" not in json.dumps(
        config
    )
    assert (
        staff.put(
            "/api/manage/product",
            json={**config, "processor_config": {"template": "WRONG-ACCOUNT $name"}},
        ).status_code
        == 403
    )
    response = staff.put("/api/manage/product", json={**config, "name": "只改展示"})
    assert response.status_code == 200, response.text
    assert binding(owner, product["id"])["id"] == profile["id"]


def test_revoked_profile_blocks_existing_job_and_new_issuance(owner):
    product = create_product(owner)
    receipt, task = submit(owner, issue(owner, product["id"]))
    profile = binding(owner, product["id"])
    assert (
        owner.delete(f"/api/admin/processor-profiles/{profile['id']}").status_code
        == 200
    )
    assert (
        owner.post(
            "/api/admin/cards", json={"product_id": product["id"], "count": 1}
        ).status_code
        == 409
    )
    assert (
        owner.put(
            f"/api/admin/processor-profiles/{profile['id']}", json={"name": "不能恢复"}
        ).status_code
        == 409
    )
    assert asyncio.run(job_once())
    assert (
        owner.post("/api/receipt", json={"token": receipt}).json()["job"]["state"]
        == "failed"
    )
    with db() as c:
        row = c.execute(
            "SELECT content,result_json FROM jobs WHERE id=?", (task["id"],)
        ).fetchone()
        assert row["content"] is None and row["result_json"] is None


def test_disabled_shop_does_not_start_automatic_processing(owner):
    client, sid = merchant()
    product = create_product(client)
    _, task = submit(client, issue(client, product["id"]))
    with db() as c:
        c.execute("UPDATE shops SET enabled=0 WHERE id=?", (sid,))
    assert asyncio.run(job_once()) is False
    with db() as c:
        assert (
            c.execute("SELECT state FROM jobs WHERE id=?", (task["id"],)).fetchone()[0]
            == "queued"
        )


def test_legacy_configuration_migration_encrypts_and_freezes_old_card(owner):
    product = create_product(owner)
    code = issue(owner, product["id"])
    with db() as c:
        values = json.loads(
            c.execute(
                "SELECT config FROM products WHERE id=?", (product["id"],)
            ).fetchone()[0]
        )
        values["processor_config"] = {"template": "LEGACY-PRIVATE $name"}
        c.execute("DELETE FROM processor_card_bindings")
        c.execute("DELETE FROM processor_product_bindings")
        c.execute(
            "UPDATE products SET config=? WHERE id=?",
            (json.dumps(values), product["id"]),
        )
    init()
    with db() as c:
        raw = c.execute(
            "SELECT config FROM products WHERE id=?", (product["id"],)
        ).fetchone()[0]
        assert "LEGACY-PRIVATE" not in raw
        assert (
            c.execute("SELECT count(*) FROM processor_card_bindings").fetchone()[0] == 1
        )
    receipt, _ = submit(owner, code)
    assert asyncio.run(job_once())
    assert (
        owner.post("/api/receipt/reveal", json={"token": receipt}).json()["output"][
            "content"
        ]
        == "LEGACY-PRIVATE Ada"
    )


def test_processor_defines_real_steps_and_worker_streams_completion(owner, monkeypatch):
    from extore import worker

    product = create_product(owner)
    receipt, task = submit(owner, issue(owner, product["id"]))
    original = worker.apply_update
    stages = []

    def capture(c, jid, update):
        result = original(c, jid, update)
        row = c.execute(
            "SELECT progress_plan,completed_steps,progress,message FROM jobs WHERE id=?",
            (jid,),
        ).fetchone()
        stages.append(
            {
                **dict(row),
                "completed_steps": json.loads(row["completed_steps"]),
                "plan": json.loads(row["progress_plan"]),
            }
        )
        return result

    monkeypatch.setattr(worker, "apply_update", capture)
    assert asyncio.run(job_once())
    with db() as c:
        row = c.execute(
            "SELECT progress_plan,completed_steps FROM jobs WHERE id=?", (task["id"],)
        ).fetchone()
        steps = json.loads(row["progress_plan"])
        assert [step["id"] for step in steps] == ["validate_input", "prepare_delivery"]
        assert json.loads(row["completed_steps"]) == [
            "validate_input",
            "prepare_delivery",
        ]
    assert any(
        s["completed_steps"] == ["validate_input"]
        and s["progress"] == 50
        and s["message"]
        for s in stages
    )
    assert (
        owner.post("/api/receipt", json={"token": receipt}).json()["job"]["steps"][0][
            "done"
        ]
        is True
    )

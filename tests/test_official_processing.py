import asyncio
import json

import pytest
from extore_processors import get_spec

from extore.db import db
from extore.models import JobUpdate
from extore.worker import job_once


def create(owner, processor_id="personalized_text", configuration=None, **values):
    response = owner.post(
        "/api/admin/products",
        json={
            "name": "商品处理器商品",
            "mode": "script",
            "processor_id": processor_id,
            "processor_config": configuration or {},
            **values,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def redeem(owner, product, params):
    response = owner.post(
        "/api/admin/cards", json={"product_id": product["id"], "count": 1}
    )
    assert response.status_code == 200, response.text
    exchanged = owner.post(
        "/api/exchange", json={"code": response.json()["codes"][0]}
    ).json()
    response = owner.post(
        "/api/redeem", json={"token": exchanged["token"], "params": params}
    )
    assert response.status_code == 200, response.text
    return exchanged, response.json()


@pytest.mark.parametrize(
    "processor_id,configuration,params,output",
    (
        (
            "personalized_text",
            {"template": "你好，$name！$$ 只进行文本替换。"},
            {"name": "小明"},
            {"content": "你好，小明！$ 只进行文本替换。"},
        ),
        (
            "resource_link",
            {
                "resource_url": "https://example.com/private?token=secret-resource",
                "message": "下载后保存好你的副本。",
            },
            {},
            {
                "resource_url": "https://example.com/private?token=secret-resource",
                "message": "下载后保存好你的副本。",
            },
        ),
    ),
)
def test_approved_processors_deliver_code_defined_results(
    owner, processor_id, configuration, params, output
):
    product = create(owner, processor_id, configuration)
    spec = get_spec(processor_id)
    assert product["parameters"] == spec["parameters"]
    assert product["outputs"] == spec["outputs"]
    exchanged, task = redeem(owner, product, params)
    assert "processor_config" not in exchanged["product"]
    assert asyncio.run(job_once()) is True
    receipt = owner.post("/api/receipt", json={"token": exchanged["token"]}).json()
    assert receipt["job"]["state"] == "succeeded"
    assert "output" not in receipt["job"] and "content" not in receipt["job"]
    assert "processor_config" not in receipt["product"]
    revealed = owner.post(
        "/api/receipt/reveal", json={"token": exchanged["token"]}
    ).json()
    assert revealed["output"] == output
    with db() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (task["id"],)).fetchone()
        assert json.loads(row["result_json"]) == output
        completion = c.execute(
            "SELECT payload FROM events WHERE type='fulfillment.succeeded'"
        ).fetchone()[0]
        assert all(value not in completion for value in output.values())


@pytest.mark.parametrize(
    "values",
    (
        {"script": "welcome"},
        {"script": "../../malicious.py"},
        {"processor_id": "../../malicious.py"},
        {"processor_id": "unapproved_processor"},
        {"parameters": [{"key": "extra", "label": {"zh-CN": "代码未定义的参数"}}]},
        {"outputs": [{"key": "extra", "label": {"zh-CN": "未知输出"}}]},
        {"processor_config": {"unknown_secret": "cannot-add-unapproved-config"}},
        {"processor_config": {"template": "$unsupported"}},
    ),
)
def test_merchant_cannot_execute_unapproved_code_or_replace_contract(owner, values):
    response = owner.post(
        "/api/admin/products",
        json={
            "name": "非法处理器商品",
            "mode": "script",
            "processor_id": "personalized_text",
            **values,
        },
    )
    assert response.status_code == 422, response.text
    with db() as c:
        assert c.execute("SELECT count(*) FROM products").fetchone()[0] == 0


def test_incomplete_processor_can_be_saved_but_not_issued(owner):
    product = create(owner, "resource_link")
    assert product["processor_config"] == {"message": ""}
    profile = owner.get(
        f"/api/admin/processor-profiles/bindings/{product['id']}"
    ).json()["profile"]
    assert profile["processor_id"] == "resource_link"
    response = owner.post(
        "/api/admin/cards", json={"product_id": product["id"], "count": 1}
    )
    assert response.status_code == 400, response.text
    with db() as c:
        assert c.execute("SELECT count(*) FROM cards").fetchone()[0] == 0
        stored = c.execute("SELECT config FROM products").fetchone()[0]
        assert json.loads(stored)["processor_config"] == {}
        assert (
            c.execute("SELECT ciphertext FROM processor_profile_revisions")
            .fetchone()[0]
            .startswith("v1.")
        )


def test_processor_input_limits_reject_before_reserving_a_card(owner):
    product = create(owner)
    code = owner.post(
        "/api/admin/cards", json={"product_id": product["id"], "count": 1}
    ).json()["codes"][0]
    token = owner.post("/api/exchange", json={"code": code}).json()["token"]
    response = owner.post(
        "/api/redeem", json={"token": token, "params": {"name": "x" * 201}}
    )
    assert response.status_code == 400, response.text
    with db() as c:
        assert c.execute("SELECT state FROM cards").fetchone()[0] == "ready"
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_invalid_processor_result_fails_immediately_without_storing_secrets(
    owner, monkeypatch
):
    from extore import worker

    product = create(
        owner,
        "resource_link",
        {"resource_url": "https://example.com/private"},
    )
    exchanged, task = redeem(owner, product, {})

    async def invalid_output(row, config):
        return JobUpdate(
            state="succeeded",
            attempt=row["attempt"],
            output={"resource_url": "javascript:secret-malformed-result"},
        )

    monkeypatch.setattr(worker, "execute_script", invalid_output)
    assert asyncio.run(job_once())
    receipt = owner.post("/api/receipt", json={"token": exchanged["token"]}).json()
    assert receipt["job"]["state"] == "failed"
    assert not receipt["job"]["can_retry"]
    assert "secret-malformed-result" not in json.dumps(receipt)
    with db() as c:
        stored = c.execute("SELECT * FROM jobs WHERE id=?", (task["id"],)).fetchone()
        assert stored["content"] is None and stored["result_json"] is None
        assert stored["lease"] is None

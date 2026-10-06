import io
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_redemption import redeem
from test_webhook_worker import SECRET, callback

from extore.db import db
from extore.sdk import Client
from extore.sdk.client import signature
from extore.sdk.script import Result, run

OUTPUT_FIELDS = [
    {"key": "access_code", "label": {"zh-CN": "访问码", "en": "Access code"}},
    {
        "key": "download",
        "label": {"zh-CN": "下载地址"},
        "type": "url",
        "description": {"zh-CN": "打开地址领取资源。"},
    },
    {
        "key": "contact",
        "label": {"zh-CN": "联系邮箱"},
        "type": "email",
        "required": False,
    },
    {
        "key": "remaining",
        "label": {"zh-CN": "剩余次数"},
        "type": "number",
        "required": False,
    },
    {
        "key": "instructions",
        "label": {"zh-CN": "使用说明"},
        "type": "textarea",
        "required": False,
    },
]
OUTPUT = {
    "access_code": "secret-access-482910",
    "download": "https://example.com/private-download-482910",
    "contact": "private-support-482910@example.com",
    "remaining": "12.5",
    "instructions": "secret instructions 482910\n第二行",
}
INVALID_URLS = (
    "https://example.com:bad/download",
    "https://example.com:99999/download",
    "https://user:password@example.com/download",
    "https://user@example.com/download",
    "https://@example.com/download",
    "https://example.com\\private",
    "https://example.com/a\x00b",
    "https://example.com/a\x1fb",
    "https://example.com/a\x7fb",
    "https://example.com/a\nb",
)
INVALID_URL_IDS = (
    "non-numeric-port",
    "out-of-range-port",
    "username-and-password",
    "username-only",
    "empty-username",
    "backslash",
    "nul",
    "control-character",
    "delete-character",
    "embedded-newline",
)


def batch(owner, job, action, **values):
    return owner.post(
        "/api/manage/batch",
        json={
            "product_id": job["product_id"],
            "ids": [job["id"]],
            "action": action,
            **values,
        },
    )


def claim(owner, job):
    response = batch(owner, job, "claim")
    assert response.status_code == 200, response.text


def succeed(owner, job, **values):
    response = batch(owner, job, "succeed", **values)
    assert response.status_code == 200, response.text


def stored_job(job_id):
    with db() as c:
        return dict(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def snapshot():
    with db() as c:
        return {
            table: [dict(row) for row in c.execute(f"SELECT * FROM {table}")]
            for table in ("jobs", "cards", "events", "audit", "callback_nonces")
        }


def test_multiple_outputs_are_stored_and_only_revealed_to_customer(
    owner, setup_product
):
    product_id, code = setup_product(outputs=OUTPUT_FIELDS)
    token, job = redeem(owner, code)
    claim(owner, job)
    succeed(owner, job, output=OUTPUT)

    row = stored_job(job["id"])
    assert row["state"] == "succeeded"
    assert json.loads(row["result_json"]) == OUTPUT
    public_status = owner.post("/api/receipt", json={"token": token}).json()
    managed_jobs = owner.get(
        "/api/manage/jobs", params={"product_id": product_id}
    ).json()
    with db() as c:
        events = [dict(r) for r in c.execute("SELECT * FROM events")]
    for response in (public_status, managed_jobs, events):
        serialized = json.dumps(response)
        assert "result_json" not in serialized
        assert all(value not in serialized for value in OUTPUT.values())
    assert "output" not in public_status["job"]
    assert "content" not in public_status["job"]
    assert "output" not in managed_jobs[0]
    assert "content" not in managed_jobs[0]

    for _ in range(2):
        response = owner.post("/api/receipt/reveal", json={"token": token})
        assert response.status_code == 200, response.text
        assert response.json()["output"] == OUTPUT
        assert all(value in response.json()["content"] for value in OUTPUT.values())


@pytest.mark.parametrize(
    "output",
    (
        {},
        {**OUTPUT, "access_code": " "},
        {key: value for key, value in OUTPUT.items() if key != "download"},
        {**OUTPUT, "undeclared": "secret"},
        {**OUTPUT, "contact": "invalid-email"},
        {**OUTPUT, "download": "invalid-url"},
        {**OUTPUT, "download": "javascript:alert(1)"},
        {**OUTPUT, "remaining": "not-a-number"},
        {**OUTPUT, "remaining": "NaN"},
        {**OUTPUT, "remaining": "Infinity"},
        {**OUTPUT, "remaining": "-Infinity"},
        {**OUTPUT, "instructions": "x" * 100001},
    ),
    ids=(
        "empty",
        "blank-required",
        "missing-required",
        "unknown-field",
        "invalid-email",
        "invalid-url",
        "unsafe-url-scheme",
        "invalid-number",
        "nan",
        "positive-infinity",
        "negative-infinity",
        "oversized",
    ),
)
def test_invalid_output_does_not_complete_or_mutate_job(owner, setup_product, output):
    _, code = setup_product(outputs=OUTPUT_FIELDS)
    _, job = redeem(owner, code)
    claim(owner, job)
    before = snapshot()
    response = batch(owner, job, "succeed", output=output)
    assert response.status_code == 400, response.text
    assert snapshot() == before


@pytest.mark.parametrize("url", INVALID_URLS, ids=INVALID_URL_IDS)
def test_output_url_boundaries_are_rejected_atomically(owner, setup_product, url):
    _, code = setup_product(outputs=OUTPUT_FIELDS)
    _, job = redeem(owner, code)
    claim(owner, job)
    before = snapshot()
    response = batch(owner, job, "succeed", output={**OUTPUT, "download": url})
    assert response.status_code == 400, response.text
    assert snapshot() == before


@pytest.mark.parametrize("url", INVALID_URLS, ids=INVALID_URL_IDS)
def test_invalid_customer_url_preserves_unused_card(owner, setup_product, url):
    product_id, code = setup_product(
        parameters=[
            {"key": "source_url", "label": {"zh-CN": "资源地址"}, "type": "url"}
        ]
    )
    response = owner.post("/api/exchange", json={"code": code})
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    before = snapshot()
    response = owner.post(
        "/api/redeem", json={"token": token, "params": {"source_url": url}}
    )
    assert response.status_code == 400, response.text
    assert snapshot() == before
    with db() as c:
        card = c.execute(
            "SELECT * FROM cards WHERE product_id=?", (product_id,)
        ).fetchone()
        assert card["state"] == "ready"
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize(
    "url",
    (
        "https://example.com/resource?version=2#download",
        "http://example.com/resource",
        "https://[2001:db8::1]:8443/resource",
    ),
    ids=("https", "http", "ipv6-with-port"),
)
def test_valid_customer_url_is_accepted(owner, setup_product, url):
    product_id, code = setup_product(
        parameters=[
            {"key": "source_url", "label": {"zh-CN": "资源地址"}, "type": "url"}
        ]
    )
    response = owner.post("/api/exchange", json={"code": code})
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    response = owner.post(
        "/api/redeem", json={"token": token, "params": {"source_url": url}}
    )
    assert response.status_code == 200, response.text
    row = stored_job(response.json()["id"])
    assert json.loads(row["params"]) == {"source_url": url}
    with db() as c:
        assert (
            c.execute(
                "SELECT state FROM cards WHERE product_id=?", (product_id,)
            ).fetchone()[0]
            == "reserved"
        )


def test_optional_fields_can_be_omitted_and_text_output_whitespace_is_preserved(
    owner, setup_product
):
    _, code = setup_product(outputs=OUTPUT_FIELDS)
    token, job = redeem(owner, code)
    claim(owner, job)
    succeed(
        owner,
        job,
        output={
            "access_code": "  actual-code  ",
            "download": " https://example.com/a ",
        },
    )
    response = owner.post("/api/receipt/reveal", json={"token": token}).json()
    assert response["output"]["access_code"] == "  actual-code  "
    assert response["output"]["download"] == "https://example.com/a"
    assert all(not response["output"].get(key) for key in ("contact", "remaining"))


def test_total_output_limit_applies_across_fields(owner, setup_product):
    fields = [
        {"key": "first", "label": {"zh-CN": "第一段"}, "type": "textarea"},
        {"key": "second", "label": {"zh-CN": "第二段"}, "type": "textarea"},
    ]
    _, code = setup_product(outputs=fields)
    token, job = redeem(owner, code)
    claim(owner, job)
    before = snapshot()
    response = batch(
        owner, job, "succeed", output={"first": "x" * 50001, "second": "y" * 50000}
    )
    assert response.status_code == 400, response.text
    assert snapshot() == before
    succeed(owner, job, output={"first": "x" * 50000, "second": "y" * 50000})
    result = owner.post("/api/receipt/reveal", json={"token": token}).json()["output"]
    assert len(result["first"]) + len(result["second"]) == 100000


def test_legacy_content_remains_compatible_for_default_output(owner, setup_product):
    _, code = setup_product()
    token, job = redeem(owner, code)
    claim(owner, job)
    succeed(owner, job, content="legacy private delivery")
    result = owner.post("/api/receipt/reveal", json={"token": token}).json()
    assert result == {
        "content": "legacy private delivery",
        "output": {"content": "legacy private delivery"},
    }


def test_pre_migration_content_can_still_be_revealed(owner, setup_product):
    _, code = setup_product()
    token, job = redeem(owner, code)
    with db() as c:
        c.execute(
            "UPDATE jobs SET state='succeeded',content=?,result_json=NULL WHERE id=?",
            ("pre-migration delivery", job["id"]),
        )
    response = owner.post("/api/receipt/reveal", json={"token": token})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "content": "pre-migration delivery",
        "output": {"content": "pre-migration delivery"},
    }


@pytest.mark.parametrize("value", (123, None, ["secret"], {"nested": "secret"}))
def test_output_values_are_strictly_strings(owner, setup_product, value):
    _, code = setup_product(outputs=OUTPUT_FIELDS)
    _, job = redeem(owner, code)
    claim(owner, job)
    before = snapshot()
    response = batch(owner, job, "succeed", output={**OUTPUT, "access_code": value})
    assert response.status_code == 422, response.text
    assert snapshot() == before


@pytest.mark.parametrize("action", ("progress", "fail"))
def test_incomplete_states_cannot_store_output(owner, setup_product, action):
    _, code = setup_product(outputs=OUTPUT_FIELDS)
    _, job = redeem(owner, code)
    claim(owner, job)
    before = snapshot()
    response = batch(owner, job, action, output=OUTPUT)
    assert response.status_code == 400, response.text
    assert snapshot() == before


def test_conflicting_legacy_and_structured_content_is_rejected(owner, setup_product):
    _, code = setup_product()
    _, job = redeem(owner, code)
    claim(owner, job)
    before = snapshot()
    response = batch(
        owner, job, "succeed", content="first", output={"content": "second"}
    )
    assert response.status_code == 400, response.text
    assert snapshot() == before


def test_legacy_content_cannot_bypass_custom_output_schema(owner, setup_product):
    _, code = setup_product(outputs=OUTPUT_FIELDS)
    _, job = redeem(owner, code)
    claim(owner, job)
    before = snapshot()
    response = batch(owner, job, "succeed", content="legacy delivery")
    assert response.status_code == 400, response.text
    assert snapshot() == before


def test_content_alias_does_not_bypass_content_field_type(owner, setup_product):
    _, code = setup_product(
        outputs=[{"key": "content", "label": {"zh-CN": "数字"}, "type": "number"}]
    )
    _, job = redeem(owner, code)
    claim(owner, job)
    before = snapshot()
    response = batch(owner, job, "succeed", content="not a number")
    assert response.status_code == 400, response.text
    assert snapshot() == before


def test_batch_completion_is_atomic_with_structured_output(owner, setup_product):
    product_id, code = setup_product(outputs=OUTPUT_FIELDS)
    _, first = redeem(owner, code)
    code2 = owner.post(
        "/api/admin/cards", json={"product_id": product_id, "count": 1}
    ).json()["codes"][0]
    _, second = redeem(owner, code2)
    for job in (first, second):
        claim(owner, job)
    assert batch(owner, second, "fail").status_code == 200
    before = snapshot()
    response = owner.post(
        "/api/manage/batch",
        json={
            "product_id": product_id,
            "ids": [first["id"], second["id"]],
            "action": "succeed",
            "output": OUTPUT,
        },
    )
    assert response.status_code == 409, response.text
    assert snapshot() == before
    assert stored_job(first["id"])["result_json"] is None


def test_webhook_validates_output_and_does_not_consume_failed_nonce(
    owner, setup_product
):
    product_id, code = setup_product(
        outputs=OUTPUT_FIELDS,
        mode="webhook",
        webhook_url="https://example.com/hooks",
        webhook_secret=SECRET,
    )
    token, job = redeem(owner, code)
    before = snapshot()
    invalid = callback(
        owner,
        product_id,
        job,
        {"attempt": 1, "state": "succeeded", "output": {**OUTPUT, "remaining": "NaN"}},
        nonce="structured-callback",
    )
    assert invalid.status_code == 400, invalid.text
    assert snapshot() == before
    response = callback(
        owner,
        product_id,
        job,
        {"attempt": 1, "state": "succeeded", "output": OUTPUT},
        nonce="structured-callback",
    )
    assert response.status_code == 200, response.text
    assert (
        owner.post("/api/receipt/reveal", json={"token": token}).json()["output"]
        == OUTPUT
    )


def test_webhook_repeat_completion_cannot_overwrite_structured_delivery(
    owner, setup_product
):
    product_id, code = setup_product(
        outputs=OUTPUT_FIELDS,
        mode="webhook",
        webhook_url="https://example.com/hooks",
        webhook_secret=SECRET,
    )
    token, job = redeem(owner, code)
    for index, output in enumerate((OUTPUT, {**OUTPUT, "access_code": "overwritten"})):
        response = callback(
            owner,
            product_id,
            job,
            {"attempt": 1, "state": "succeeded", "output": output},
            nonce=f"structured-completion-{index}",
        )
        assert response.status_code == 200, response.text
    assert (
        owner.post("/api/receipt/reveal", json={"token": token}).json()["output"]
        == OUTPUT
    )


def test_once_reveal_is_atomic_and_erases_all_output_storage(owner, setup_product):
    _, code = setup_product(outputs=OUTPUT_FIELDS, view_policy="once")
    token, job = redeem(owner, code)
    claim(owner, job)
    succeed(owner, job, output=OUTPUT)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(
                lambda _: owner.post("/api/receipt/reveal", json={"token": token}),
                range(4),
            )
        )
    successes = [response for response in responses if response.status_code == 200]
    assert len(successes) == 1 and successes[0].json()["output"] == OUTPUT
    assert sum(response.status_code == 410 for response in responses) == 3
    row = stored_job(job["id"])
    assert row["content"] is None and row["result_json"] is None


def test_destroy_erases_structured_output_and_customer_parameters(owner, setup_product):
    _, code = setup_product(outputs=OUTPUT_FIELDS)
    token, job = redeem(owner, code)
    claim(owner, job)
    succeed(owner, job, output=OUTPUT)
    response = owner.post("/api/receipt/destroy", json={"token": token})
    assert response.status_code == 200, response.text
    row = stored_job(job["id"])
    assert row["state"] == "destroyed"
    assert row["content"] is None and row["result_json"] is None
    assert json.loads(row["params"]) == {}
    assert owner.post("/api/receipt/reveal", json={"token": token}).status_code == 409
    assert owner.post("/api/exchange", json={"code": code}).status_code == 410


def test_retry_clears_all_preceding_output_storage(owner, setup_product):
    _, code = setup_product(outputs=OUTPUT_FIELDS)
    token, job = redeem(owner, code)
    claim(owner, job)
    assert batch(owner, job, "fail", retryable=True).status_code == 200
    # A partial result from an older worker or migrated database must never carry
    # over into the next attempt, even if that older worker did not scrub it.
    with db() as c:
        c.execute(
            "UPDATE jobs SET content=?,result_json=? WHERE id=?",
            ("stale delivery", json.dumps(OUTPUT), job["id"]),
        )
    response = owner.post(
        "/api/redeem", json={"token": token, "params": {"email": "user@example.com"}}
    )
    assert response.status_code == 200, response.text
    assert response.json()["attempt"] == 2
    row = stored_job(job["id"])
    assert row["state"] == "queued"
    assert row["content"] is None and row["result_json"] is None


@pytest.mark.parametrize("output", (None, {}), ids=("legacy-content", "empty-output"))
def test_service_goods_keep_status_only(owner, setup_product, output):
    _, code = setup_product(delivery="service")
    token, job = redeem(owner, code)
    claim(owner, job)
    succeed(owner, job, content="legacy ignored delivery", output=output)
    row = stored_job(job["id"])
    assert row["content"] is None and row["result_json"] is None
    assert owner.post("/api/receipt/reveal", json={"token": token}).json() == {
        "content": None,
        "output": {},
    }


def test_service_goods_reject_undeclared_output(owner, setup_product):
    _, code = setup_product(delivery="service")
    _, job = redeem(owner, code)
    claim(owner, job)
    before = snapshot()
    response = batch(owner, job, "succeed", output={"content": "unexpected secret"})
    assert response.status_code == 400, response.text
    assert snapshot() == before


@pytest.mark.parametrize("change", ("key", "type", "required", "remove", "add"))
def test_issued_webhook_cards_freeze_output_schema(owner, setup_product, change):
    product_id, _ = setup_product(
        outputs=OUTPUT_FIELDS,
        mode="webhook",
        webhook_url="https://example.com/events",
        webhook_secret="a" * 32,
    )
    config = next(
        product
        for product in owner.get("/api/admin/products").json()
        if product["id"] == product_id
    )
    config.pop("id")
    if change == "key":
        config["outputs"][0]["key"] = "new_access_code"
    elif change == "type":
        config["outputs"][0]["type"] = "textarea"
    elif change == "required":
        config["outputs"][0]["required"] = False
    elif change == "remove":
        config["outputs"].pop()
    else:
        config["outputs"].append({"key": "new_field", "label": {"zh-CN": "新字段"}})
    response = owner.put("/api/admin/products/" + product_id, json=config)
    assert response.status_code == 409, response.text


def test_issued_goods_allow_output_labels_and_tutorials_to_improve(
    owner, setup_product
):
    product_id, _ = setup_product(outputs=OUTPUT_FIELDS)
    config = next(
        product
        for product in owner.get("/api/admin/products").json()
        if product["id"] == product_id
    )
    config.pop("id")
    config["outputs"][0].update(
        label={"zh-CN": "已改进的领取码", "en": "Improved claim code"},
        description={"zh-CN": "**按这里的教程操作。**"},
        collapsed=False,
    )
    response = owner.put("/api/admin/products/" + product_id, json=config)
    assert response.status_code == 200, response.text
    assert response.json()["outputs"][0] == config["outputs"][0]


def test_script_sdk_emits_structured_success(monkeypatch, capsys):
    task = {"id": "task-1", "product_id": "product-1", "attempt": 1, "params": {}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(task)))
    run(lambda task: Result.success(output=OUTPUT, message="已完成"))
    result = json.loads(capsys.readouterr().out)
    assert result["kind"] == "result" and result["state"] == "succeeded"
    assert result["output"] == OUTPUT
    assert result["message"] == "已完成"


def test_webhook_sdk_signs_exact_structured_payload(monkeypatch):
    captured = {}

    def urlopen(request, timeout):
        captured.update(request=request, timeout=timeout)
        return io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    result = Client("https://extore.example/", SECRET).update(
        "product-1", "task-1", 1, state="succeeded", output=OUTPUT
    )
    assert result == {"ok": True}
    request = captured["request"]
    assert request.full_url == "https://extore.example/api/callbacks/product-1/task-1"
    assert captured["timeout"] == 30
    assert json.loads(request.data)["output"] == OUTPUT
    headers = {key.lower(): value for key, value in request.header_items()}
    assert headers["x-extore-signature"] == signature(
        SECRET, headers["x-extore-timestamp"], headers["x-extore-nonce"], request.data
    )

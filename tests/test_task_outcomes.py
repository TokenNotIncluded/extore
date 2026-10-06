import json

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from test_files import input_file, output_file, prepare
from test_product_links import as_owner, create_link, login_link

from extore.db import db
from extore.models import BatchUpdate, JobUpdate, ProductLinkInput
from extore.service import apply_update


def create(owner, **config):
    response = owner.post(
        "/api/admin/products",
        json={
            "name": "资料审核",
            "delivery": "service",
            "parameters": [
                {"key": "email", "label": {"zh-CN": "邮箱"}, "type": "email"}
            ],
            "progress_steps": [{"id": "verify", "label": {"zh-CN": "核实资料"}}],
            **config,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def launch(owner, product, email="original@example.com"):
    response = owner.post("/api/admin/cards", json={"product_id": product["id"]})
    assert response.status_code == 200, response.text
    code = response.json()["codes"][0]
    response = owner.post("/api/exchange", json={"code": code})
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    response = owner.post(
        "/api/redeem", json={"token": token, "params": {"email": email}}
    )
    assert response.status_code == 200, response.text
    return code, token, response.json()


def batch(owner, row, action, **values):
    return owner.post(
        "/api/manage/batch",
        json={
            "product_id": row["product_id"],
            "ids": [row["id"]],
            "action": action,
            **values,
        },
    )


def receipt(owner, token):
    response = owner.post("/api/receipt", json={"token": token})
    assert response.status_code == 200, response.text
    return response.json()


def saved_job(jid):
    with db() as c:
        return dict(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())


def snapshot():
    with db() as c:
        return {
            table: [dict(row) for row in c.execute(f"SELECT * FROM {table}")]
            for table in ("jobs", "cards", "job_files", "events", "audit")
        }


@pytest.mark.parametrize("action", ["request_changes", "reject"])
@pytest.mark.parametrize("reason", ["", " \n ", "x" * 1001])
def test_review_requires_reason(action, reason):
    with pytest.raises(ValidationError):
        BatchUpdate(product_id="product", ids=["job"], action=action, message=reason)


@pytest.mark.parametrize("action", ["request_changes", "reject"])
@pytest.mark.parametrize(
    "result",
    [
        {"content": "delivery"},
        {"output": {"result": "delivery"}},
        {"completed_steps": []},
    ],
)
def test_review_does_not_include_delivery_or_change_steps(action, result):
    with pytest.raises(ValidationError):
        BatchUpdate(
            product_id="product", ids=["job"], action=action, message="reason", **result
        )


@pytest.mark.parametrize("value", [0, 1001, True, 1.5, "2"])
def test_cli_link_quota_is_a_bounded_strict_integer(value):
    with pytest.raises(ValidationError):
        ProductLinkInput(name="manager", max_cli_uses=value)


def test_cli_link_quota_defaults_to_one():
    assert ProductLinkInput(name="manager").max_cli_uses == 1
    assert ProductLinkInput(name="manager", max_cli_uses=1000).max_cli_uses == 1000


def test_return_for_changes_keeps_task_and_allows_correction_without_failure_retry_policy(
    owner,
):
    product = create(owner, allow_retry=False, max_attempts=1)
    _, token, row = launch(owner, product)
    original = saved_job(row["id"])
    assert batch(owner, row, "claim", completed_steps=["verify"]).status_code == 200
    response = batch(
        owner, row, "request_changes", message="  请改正邮箱后重新提交。  "
    )
    assert response.status_code == 200, response.text
    waiting = receipt(owner, token)["job"]
    assert waiting["state"] == "needs_input" and waiting["can_retry"] is True
    assert waiting["message"] == "请改正邮箱后重新提交。"
    assert waiting["params"] == {"email": "original@example.com"}
    with db() as c:
        assert (
            c.execute(
                "SELECT state FROM cards WHERE id=?", (original["card_id"],)
            ).fetchone()[0]
            == "ready"
        )
    for attempt in (2, 3):
        response = owner.post(
            "/api/redeem",
            json={
                "token": token,
                "params": {"email": f"corrected{attempt}@example.com"},
            },
        )
        assert response.status_code == 200, response.text
        updated = response.json()
        assert updated["id"] == row["id"] and updated["attempt"] == attempt
        assert updated["state"] == "queued" and updated["progress"] == 0
        assert updated["completed_steps"] == []
        saved = saved_job(row["id"])
        assert saved["schema_snapshot"] == original["schema_snapshot"]
        assert saved["progress_plan"] == original["progress_plan"]
        assert saved["claimed_by"] is None and saved["lease"] is None
        if attempt == 2:
            assert batch(owner, row, "claim").status_code == 200
            assert (
                batch(owner, row, "request_changes", message="还需补充。").status_code
                == 200
            )
    assert batch(owner, row, "claim").status_code == 200
    assert batch(owner, row, "succeed").status_code == 200
    assert receipt(owner, token)["job"]["state"] == "succeeded"


def test_returned_task_uses_frozen_form_after_product_changes(owner):
    product = create(owner, allow_retry=False, max_attempts=1)
    _, token, row = launch(owner, product)
    assert batch(owner, row, "claim").status_code == 200
    assert (
        batch(owner, row, "request_changes", message="请填写正确的邮箱。").status_code
        == 200
    )
    response = owner.put(
        f"/api/admin/products/{product['id']}",
        json={
            **product,
            "parameters": [{"key": "phone", "label": {"en": "Phone"}}],
            "progress_steps": [{"id": "new", "label": {"en": "New plan"}}],
        },
    )
    assert response.status_code == 200, response.text
    visible = receipt(owner, token)
    assert visible["product"]["parameters"][0]["key"] == "email"
    response = owner.post(
        "/api/redeem", json={"token": token, "params": {"email": "fixed@example.com"}}
    )
    assert response.status_code == 200, response.text
    assert response.json()["steps"][0]["id"] == "verify"


def test_invalid_resubmission_keeps_returned_state_and_original_params(owner):
    product = create(owner)
    _, token, row = launch(owner, product)
    assert batch(owner, row, "claim").status_code == 200
    assert batch(owner, row, "request_changes", message="修改邮箱。").status_code == 200
    before = snapshot()
    response = owner.post(
        "/api/redeem", json={"token": token, "params": {"email": "bad"}}
    )
    assert response.status_code == 400, response.text
    assert snapshot() == before


def test_rejected_card_is_terminal_but_reason_remains_visible(owner):
    product = create(owner)
    code, token, row = launch(owner, product)
    assert batch(owner, row, "claim").status_code == 200
    assert (
        batch(owner, row, "reject", message="此申请不符合商品要求。").status_code == 200
    )
    result = receipt(owner, token)["job"]
    assert result["state"] == "rejected" and result["can_retry"] is False
    assert result["message"] == "此申请不符合商品要求。"
    assert "params" not in result
    assert owner.post("/api/exchange", json={"code": code}).status_code == 410
    assert (
        owner.post(
            "/api/redeem",
            json={"token": token, "params": {"email": "fixed@example.com"}},
        ).status_code
        == 410
    )
    assert owner.post("/api/receipt/reveal", json={"token": token}).status_code == 409
    assert batch(owner, row, "retry").status_code == 409
    card_id = saved_job(row["id"])["card_id"]
    with db() as c:
        assert (
            c.execute("SELECT state FROM cards WHERE id=?", (card_id,)).fetchone()[0]
            == "rejected"
        )


@pytest.mark.parametrize("action", ["request_changes", "reject"])
def test_review_requires_current_owned_processing_task(owner, action):
    product = create(owner)
    _, _, row = launch(owner, product)
    before = snapshot()
    assert batch(owner, row, action, message="reason").status_code == 409
    assert snapshot() == before
    assert batch(owner, row, "claim").status_code == 200
    link = create_link(
        owner, product["id"], permissions=["queue.view", "queue.process"]
    )
    login_link(owner, link)
    before = snapshot()
    assert batch(owner, row, action, message="reason").status_code == 409
    assert snapshot() == before


@pytest.mark.parametrize("action", ["request_changes", "reject"])
@pytest.mark.parametrize("permissions", [["queue.view"], ["queue.view", "queue.retry"]])
def test_review_requires_process_permission(owner, action, permissions):
    product = create(owner)
    _, _, row = launch(owner, product)
    assert batch(owner, row, "claim").status_code == 200
    link = create_link(owner, product["id"], permissions=permissions)
    login_link(owner, link)
    assert batch(owner, row, action, message="reason").status_code == 403


@pytest.mark.parametrize("action", ["request_changes", "reject"])
def test_mixed_product_review_is_atomic(owner, action):
    first_product = create(owner)
    second_product = create(owner)
    _, _, first = launch(owner, first_product)
    _, _, second = launch(owner, second_product)
    assert batch(owner, first, "claim").status_code == 200
    assert batch(owner, second, "claim").status_code == 200
    before = snapshot()
    response = owner.post(
        "/api/manage/batch",
        json={
            "product_id": first_product["id"],
            "ids": [first["id"], second["id"]],
            "action": action,
            "message": "reason",
        },
    )
    assert response.status_code == 403, response.text
    assert snapshot() == before


def test_batch_rolls_back_first_review_when_second_is_not_claimed(owner):
    product = create(owner)
    _, _, first = launch(owner, product)
    _, _, second = launch(owner, product)
    assert batch(owner, first, "claim").status_code == 200
    before = snapshot()
    response = owner.post(
        "/api/manage/batch",
        json={
            "product_id": product["id"],
            "ids": [first["id"], second["id"]],
            "action": "reject",
            "message": "reason",
        },
    )
    assert response.status_code == 409, response.text
    assert snapshot() == before


def test_other_product_link_cannot_review(owner):
    product = create(owner)
    other = create(owner)
    _, _, row = launch(owner, product)
    assert batch(owner, row, "claim").status_code == 200
    link = create_link(owner, other["id"], permissions=["queue.view", "queue.process"])
    login_link(owner, link)
    assert batch(owner, row, "reject", message="reason").status_code == 403


@pytest.mark.parametrize(
    "action,state", [("request_changes", "needs_input"), ("reject", "rejected")]
)
def test_review_event_contains_reason_and_safe_snapshots_without_params(
    owner, action, state
):
    product = create(owner)
    _, _, row = launch(owner, product)
    assert batch(owner, row, "claim").status_code == 200
    assert batch(owner, row, action, message="reason").status_code == 200
    with db() as c:
        event = json.loads(
            c.execute(
                "SELECT payload FROM events WHERE type=?", ("fulfillment." + state,)
            ).fetchone()[0]
        )
    assert event["data"]["state"] == state and event["data"]["message"] == "reason"
    assert event["data"]["variant"] == row["variant"]
    assert event["data"]["steps"][0]["id"] == "verify"
    assert "params" not in event["data"] and "output" not in event["data"]


@pytest.mark.parametrize("action", ["request_changes", "reject"])
def test_review_purges_output_drafts_and_keeps_input_files(owner, action):
    _, token, row, source = prepare(owner, allow_retry=False, max_attempts=1)
    assert batch(owner, row, "claim").status_code == 200
    draft = output_file(owner, row)
    assert draft.status_code == 200, draft.text
    assert batch(owner, row, action, message="reason").status_code == 200
    with db() as c:
        assert (
            c.execute(
                "SELECT id FROM job_files WHERE id=?", (draft.json()["id"],)
            ).fetchone()
            is None
        )
        assert (
            c.execute("SELECT id FROM job_files WHERE id=?", (source["id"],)).fetchone()
            is not None
        )
    if action == "request_changes":
        response = owner.post(
            "/api/redeem", json={"token": token, "params": {"source": source["id"]}}
        )
        assert response.status_code == 200, response.text
        assert response.json()["attempt"] == 2
        with db() as c:
            assert (
                c.execute(
                    "SELECT attempt FROM job_files WHERE id=?", (source["id"],)
                ).fetchone()[0]
                == 2
            )


def test_returned_customer_can_upload_replacement_and_binding_cleans_unused_input(
    owner,
):
    _, token, row, source = prepare(owner, allow_retry=False, max_attempts=1)
    assert batch(owner, row, "claim").status_code == 200
    assert (
        batch(owner, row, "request_changes", message="请换一个文件。").status_code
        == 200
    )
    replacement = input_file(owner, token, content=b"corrected")
    assert replacement.status_code == 200, replacement.text
    response = owner.post(
        "/api/redeem",
        json={"token": token, "params": {"source": replacement.json()["id"]}},
    )
    assert response.status_code == 200, response.text
    with db() as c:
        assert (
            c.execute("SELECT id FROM job_files WHERE id=?", (source["id"],)).fetchone()
            is None
        )


def test_genuine_failure_retry_policy_is_preserved(owner):
    product = create(owner, allow_retry=False, max_attempts=1)
    _, token, row = launch(owner, product)
    assert batch(owner, row, "claim").status_code == 200
    assert (
        batch(
            owner, row, "fail", message="provider failure", retryable=True
        ).status_code
        == 200
    )
    assert receipt(owner, token)["job"]["can_retry"] is False
    assert (
        owner.post(
            "/api/redeem",
            json={"token": token, "params": {"email": "fixed@example.com"}},
        ).status_code
        == 409
    )


def test_completed_jobs_reject_review_even_if_actor_is_owner(owner):
    product = create(owner)
    _, _, row = launch(owner, product)
    assert batch(owner, row, "claim").status_code == 200
    assert batch(owner, row, "succeed").status_code == 200
    for action in ("request_changes", "reject"):
        assert batch(owner, row, action, message="reason").status_code == 409


@pytest.mark.parametrize("action", ["request_changes", "reject"])
def test_review_never_restores_a_revoked_card(owner, action):
    product = create(owner)
    _, _, row = launch(owner, product)
    assert batch(owner, row, "claim").status_code == 200
    with db() as c:
        c.execute(
            "UPDATE cards SET state='revoked' WHERE id=(SELECT card_id FROM jobs WHERE id=?)",
            (row["id"],),
        )
    before = snapshot()
    assert batch(owner, row, action, message="reason").status_code == 410
    assert snapshot() == before


@pytest.mark.parametrize("action", ["request_changes", "reject"])
def test_completion_cannot_overwrite_review_outcome(owner, action):
    product = create(owner)
    _, _, row = launch(owner, product)
    assert batch(owner, row, "claim").status_code == 200
    assert batch(owner, row, action, message="reason").status_code == 200
    before = snapshot()
    with pytest.raises(HTTPException) as caught, db() as c:
        apply_update(c, row["id"], JobUpdate(state="succeeded", attempt=1))
    assert caught.value.status_code == 409
    assert snapshot() == before


def test_previous_attempt_cannot_finish_a_resubmitted_task(owner):
    product = create(owner)
    _, token, row = launch(owner, product)
    assert batch(owner, row, "claim").status_code == 200
    assert batch(owner, row, "request_changes", message="reason").status_code == 200
    assert (
        owner.post(
            "/api/redeem",
            json={"token": token, "params": {"email": "fixed@example.com"}},
        ).status_code
        == 200
    )
    before = snapshot()
    with pytest.raises(HTTPException) as caught, db() as c:
        apply_update(c, row["id"], JobUpdate(state="succeeded", attempt=1))
    assert caught.value.status_code == 409
    assert snapshot() == before


def test_automation_cannot_be_overridden_by_queue_review(owner):
    product = create(
        owner,
        mode="webhook",
        webhook_url="https://example.com/hook",
        webhook_secret="a-private-webhook-signing-secret-32chars",
    )
    _, _, row = launch(owner, product)
    with db() as c:
        c.execute(
            "UPDATE jobs SET state='processing',claimed_by='owner' WHERE id=?",
            (row["id"],),
        )
    assert batch(owner, row, "request_changes", message="reason").status_code == 409


def test_default_queue_views_include_returned_and_processed_rejected(owner):
    product = create(owner)
    _, _, returned = launch(owner, product)
    _, _, rejected = launch(owner, product)
    for row, action in ((returned, "request_changes"), (rejected, "reject")):
        assert batch(owner, row, "claim").status_code == 200
        assert batch(owner, row, action, message="reason").status_code == 200
    active = owner.get("/api/manage/jobs", params={"product_id": product["id"]}).json()
    processed = owner.get(
        "/api/manage/jobs", params={"product_id": product["id"], "view": "processed"}
    ).json()
    assert [row["id"] for row in active] == [returned["id"]]
    assert [row["id"] for row in processed] == [rejected["id"]]


def test_assigned_product_processor_can_request_changes_and_reclaim_after_resubmit(
    owner,
):
    product = create(owner)
    _, token, row = launch(owner, product)
    link = create_link(
        owner, product["id"], permissions=["queue.view", "queue.process"]
    )
    login_link(owner, link)
    assert batch(owner, row, "claim").status_code == 200
    assert batch(owner, row, "request_changes", message="reason").status_code == 200
    response = owner.post(
        "/api/redeem", json={"token": token, "params": {"email": "fixed@example.com"}}
    )
    assert response.status_code == 200, response.text
    assert batch(owner, row, "claim").status_code == 200
    assert batch(owner, row, "succeed").status_code == 200
    as_owner(owner)
    assert receipt(owner, token)["job"]["state"] == "succeeded"

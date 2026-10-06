"""Read-only retry preserves originals, receipts, and card accounting."""

import json
import time

import pytest
from test_files import input_file, output_file, prepare
from test_task_outcomes import batch, create, launch, receipt, saved_job, snapshot

from extore.db import db


def returned(owner, row, *, mode="reuse", reason="external"):
    assert batch(owner, row, "claim").status_code == 200
    response = batch(
        owner,
        row,
        "request_retry",
        retry_mode=mode,
        reason_type=reason,
        message="上游暂时不可用，请稍后使用原需求重试。",
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("reason", ["external", "processor"])
def test_readonly_retry_keeps_original_file_ids_and_card_unused_until_requeued(
    owner, reason
):
    product, token, row, source = prepare(owner, allow_retry=False, max_attempts=1)
    original = saved_job(row["id"])
    assert batch(owner, row, "claim").status_code == 200
    draft = output_file(owner, row, content=b"obsolete output").json()["id"]
    response = batch(
        owner,
        row,
        "request_retry",
        retry_mode="reuse",
        reason_type=reason,
        message="暂时无法处理，原需求与附件可以保留重试。",
    )
    assert response.status_code == 200, response.text
    waiting = receipt(owner, token)["job"]
    assert waiting["state"] == "needs_input" and waiting["can_retry"]
    assert waiting["retry_mode"] == "reuse" and waiting["retry_reason_type"] == reason
    assert waiting["params"] == {"source": source["id"]}
    with db() as c:
        payload = json.loads(
            c.execute(
                "SELECT payload FROM events WHERE job_id=? AND type='fulfillment.needs_input'",
                (row["id"],),
            ).fetchone()[0]
        )
    assert payload["data"]["retry_mode"] == "reuse"
    assert payload["data"]["retry_reason_type"] == reason
    assert "params" not in payload["data"]
    assert "source" not in payload["data"]
    counts = owner.get(
        "/api/admin/card-stats", params={"product_id": product["id"]}
    ).json()["summary"]
    assert counts["remaining"] == 1 and counts["used"] == 0
    assert counts["states"]["needs_input"] == 1
    with db() as c:
        assert (
            c.execute(
                "SELECT state FROM cards WHERE id=?", (original["card_id"],)
            ).fetchone()[0]
            == "ready"
        )
        assert (
            c.execute("SELECT id FROM job_files WHERE id=?", (draft,)).fetchone()
            is None
        )
        original_file = dict(
            c.execute("SELECT * FROM job_files WHERE id=?", (source["id"],)).fetchone()
        )
    response = owner.post("/api/retry", json={"token": token})
    assert response.status_code == 200, response.text
    assert response.json()["id"] == row["id"]
    assert response.json()["state"] == "queued" and response.json()["attempt"] == 2
    updated = saved_job(row["id"])
    assert updated["params"] == original["params"]
    assert updated["schema_snapshot"] == original["schema_snapshot"]
    assert updated["progress_plan"] == original["progress_plan"]
    assert updated["completed_steps"] == "[]" and updated["progress"] == 0
    assert updated["claimed_by"] is None and updated["lease"] is None
    with db() as c:
        kept = dict(
            c.execute("SELECT * FROM job_files WHERE id=?", (source["id"],)).fetchone()
        )
        assert kept["content"] == original_file["content"]
        assert kept["id"] == original_file["id"] and kept["bound"] == 1
        assert kept["attempt"] == 2
        assert c.execute("SELECT COUNT(*) FROM job_files").fetchone()[0] == 1
    before = snapshot()
    assert owner.post("/api/retry", json={"token": token}).status_code == 409
    assert snapshot() == before


def test_readonly_retry_uses_frozen_parameters_after_product_form_changes(owner):
    product = create(owner, allow_retry=False, max_attempts=1)
    _, token, row = launch(owner, product)
    original = saved_job(row["id"])
    returned(owner, row, reason="processor")
    response = owner.put(
        f"/api/admin/products/{product['id']}",
        json={**product, "parameters": [{"key": "phone", "label": {"en": "Phone"}}]},
    )
    assert response.status_code == 200, response.text
    response = owner.post("/api/retry", json={"token": token})
    assert response.status_code == 200, response.text
    assert saved_job(row["id"])["params"] == original["params"]
    assert saved_job(row["id"])["schema_snapshot"] == original["schema_snapshot"]


def test_reuse_mode_rejects_changed_parameters_before_job_or_card_mutation(owner):
    product = create(owner)
    _, token, row = launch(owner, product)
    returned(owner, row)
    before = snapshot()
    response = owner.post(
        "/api/redeem",
        json={"token": token, "params": {"email": "different@example.com"}},
    )
    assert response.status_code == 409
    assert snapshot() == before


def test_reuse_mode_rejects_replacing_an_input_file(owner):
    _, token, row, source = prepare(owner)
    returned(owner, row)
    replacement = input_file(owner, token, content=b"replacement input")
    assert replacement.status_code == 200, replacement.text
    before = snapshot()
    response = owner.post(
        "/api/redeem",
        json={"token": token, "params": {"source": replacement.json()["id"]}},
    )
    assert response.status_code == 409
    assert snapshot() == before
    assert json.loads(saved_job(row["id"])["params"]) == {"source": source["id"]}


def test_revise_mode_cannot_use_the_readonly_retry_endpoint(owner):
    product = create(owner)
    _, token, row = launch(owner, product)
    returned(owner, row, mode="revise", reason="customer_input")
    before = snapshot()
    assert owner.post("/api/retry", json={"token": token}).status_code == 409
    assert snapshot() == before
    assert receipt(owner, token)["job"]["retry_mode"] == "revise"
    response = owner.post(
        "/api/redeem",
        json={"token": token, "params": {"email": "corrected@example.com"}},
    )
    assert response.status_code == 200, response.text
    assert json.loads(saved_job(row["id"])["params"]) == {
        "email": "corrected@example.com"
    }


@pytest.mark.parametrize("blocked", ["shop_disabled", "revoked", "expired"])
def test_readonly_retry_does_not_restore_disabled_or_unusable_cards(owner, blocked):
    product = create(owner)
    _, token, row = launch(owner, product)
    returned(owner, row)
    with db() as c:
        if blocked == "shop_disabled":
            c.execute(
                "UPDATE shops SET enabled=0 WHERE id=(SELECT shop_id FROM products WHERE id=?)",
                (product["id"],),
            )
        elif blocked == "revoked":
            c.execute(
                "UPDATE cards SET state='revoked' WHERE id=(SELECT card_id FROM jobs WHERE id=?)",
                (row["id"],),
            )
        else:
            c.execute(
                "UPDATE card_meta SET expires=? WHERE card_id=(SELECT card_id FROM jobs WHERE id=?)",
                (time.time() - 1, row["id"]),
            )
    before = snapshot()
    response = owner.post("/api/retry", json={"token": token})
    assert response.status_code in (404, 410), response.text
    assert snapshot() == before


def test_single_receipt_cannot_retry_a_foreign_card(owner):
    product = create(owner)
    _, token_a, a = launch(owner, product)
    _, _, b = launch(owner, product)
    returned(owner, a)
    returned(owner, b)
    before = snapshot()
    response = owner.post(
        "/api/retry", json={"token": token_a, "card_id": saved_job(b["id"])["card_id"]}
    )
    assert response.status_code == 404
    assert snapshot() == before


def test_batch_readonly_retry_requires_selection_and_only_requeues_selected_card(owner):
    product = create(owner, allow_retry=False, max_attempts=1)
    response = owner.post(
        "/api/admin/cards", json={"product_id": product["id"], "count": 2}
    )
    assert response.status_code == 200
    response = owner.post(
        "/api/exchange", json={"code": "\n".join(response.json()["codes"])}
    )
    assert response.status_code == 200, response.text
    initial = response.json()
    token = initial["token"]
    cards = [item["card_id"] for item in initial["items"]]
    response = owner.post(
        "/api/redeem",
        json={
            "token": token,
            "items": [
                {"card_id": cards[0], "params": {"email": "first@example.com"}},
                {"card_id": cards[1], "params": {"email": "second@example.com"}},
            ],
        },
    )
    assert response.status_code == 200, response.text
    jobs = [item["job"] for item in response.json()["items"]]
    for row in jobs:
        returned(owner, row)
    before = snapshot()
    assert owner.post("/api/retry", json={"token": token}).status_code == 400
    assert snapshot() == before
    response = owner.post("/api/retry", json={"token": token, "card_id": cards[1]})
    assert response.status_code == 200, response.text
    assert response.json()["id"] == jobs[1]["id"] and response.json()["attempt"] == 2
    after = receipt(owner, token)
    states = {item["card_id"]: item["job"] for item in after["items"]}
    assert (
        states[cards[0]]["state"] == "needs_input" and states[cards[0]]["attempt"] == 1
    )
    assert states[cards[1]]["state"] == "queued" and states[cards[1]]["attempt"] == 2
    _, _, foreign = launch(owner, product)
    returned(owner, foreign)
    before = snapshot()
    assert (
        owner.post(
            "/api/retry",
            json={"token": token, "card_id": saved_job(foreign["id"])["card_id"]},
        ).status_code
        == 404
    )
    assert snapshot() == before

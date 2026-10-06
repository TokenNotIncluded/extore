"""Inventory effects of returning input to the customer and rejecting a request."""

import pytest
from test_card_tracking import batch, card_id, expire, inventory, stats
from test_card_variants import create_product, issue_variant, variant
from test_product_links import create_link, login_link
from test_redemption import finish, redeem

from extore.db import db

OUTCOME_STATES = {
    "unused",
    "needs_input",
    "queued",
    "processing",
    "succeeded",
    "failed_retryable",
    "failed_terminal",
    "destroyed",
    "revoked",
    "expired",
    "rejected",
}
COUNTS = (
    "total",
    "remaining",
    "available",
    "used",
    "verified",
    "viewed",
    "in_progress",
    "completed",
    "failed",
    "rejected",
)


def outcome(owner, task, action, message):
    batch(owner, task, "claim")
    response = owner.post(
        "/api/manage/batch",
        json={
            "product_id": task["product_id"],
            "ids": [task["id"]],
            "action": action,
            "message": message,
        },
    )
    assert response.status_code == 200, response.text


def resubmit(owner, token, email="corrected@example.com"):
    response = owner.post(
        "/api/redeem", json={"token": token, "params": {"email": email}}
    )
    assert response.status_code == 200, response.text
    return response.json()


def assert_summary_partition(result):
    summary = result["summary"]
    assert set(summary["states"]) == OUTCOME_STATES
    assert sum(summary["states"].values()) == summary["total"]
    assert summary["remaining"] == (
        summary["states"]["unused"] + summary["states"]["needs_input"]
    )
    assert summary["available"] == (
        summary["remaining"] + summary["states"]["failed_retryable"]
    )
    assert summary["rejected"] == summary["states"]["rejected"]
    for product in result["products"]:
        for key in COUNTS:
            assert sum(v["summary"][key] for v in product["variants"]) == product[key]
        for state in OUTCOME_STATES:
            assert (
                sum(v["summary"]["states"][state] for v in product["variants"])
                == (product["states"][state])
            )
        assert sum(product["states"].values()) == product["total"]
    for key in COUNTS:
        assert sum(product[key] for product in result["products"]) == summary[key]


def test_return_resubmit_return_success_restores_inventory_without_consuming_twice(
    owner, setup_product
):
    pid, code = setup_product(allow_retry=False, max_attempts=1)
    token, task = redeem(owner, code)
    jid = task["id"]
    initial_used_at = inventory(owner, pid)["items"][0]["used_at"]
    assert stats(owner, pid)["summary"]["used"] == 1

    for attempt in (1, 2):
        assert task["attempt"] == attempt
        outcome(
            owner, task, "request_changes", f"Please correct input, round {attempt}"
        )
        row = inventory(owner, pid)["items"][0]
        assert row["status"] == row["job_state"] == "needs_input"
        assert row["state"] == "ready" and row["id"] == card_id(code)
        assert row["used_at"] == initial_used_at
        summary = stats(owner, pid)["summary"]
        assert summary["total"] == summary["remaining"] == summary["available"] == 1
        assert summary["used"] == summary["failed"] == summary["rejected"] == 0
        assert summary["verified"] == 1 and summary["states"]["needs_input"] == 1
        assert summary["states"]["unused"] == 0
        assert_summary_partition(stats(owner, pid))
        assert owner.post("/api/exchange", json={"code": code}).status_code == 200
        receipt = owner.post("/api/receipt", json={"token": token}).json()["job"]
        assert receipt["can_retry"] is True
        assert receipt["message"] == f"Please correct input, round {attempt}"
        task = resubmit(owner, token, email=f"round-{attempt}@example.com")
        assert task["id"] == jid and task["attempt"] == attempt + 1
        summary = stats(owner, pid)["summary"]
        assert (
            summary["used"] == 1 and summary["remaining"] == summary["available"] == 0
        )
        assert (
            summary["states"]["needs_input"] == 0 and summary["states"]["queued"] == 1
        )
        assert_summary_partition(stats(owner, pid))

    finish(owner, task, "FINAL PRIVATE DELIVERY")
    summary = stats(owner, pid)["summary"]
    assert summary["used"] == summary["completed"] == summary["total"] == 1
    assert summary["remaining"] == summary["available"] == summary["rejected"] == 0
    assert summary["states"]["succeeded"] == 1
    assert_summary_partition(stats(owner, pid))
    with db() as c:
        assert c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1


def test_rejection_is_terminal_and_is_not_failed_or_remaining(owner, setup_product):
    pid, code = setup_product()
    token, task = redeem(owner, code)
    outcome(owner, task, "reject", "The supplied account is not eligible")
    row = inventory(owner, pid, status="rejected")["items"][0]
    assert row["status"] == row["job_state"] == row["state"] == "rejected"
    summary = stats(owner, pid)["summary"]
    assert summary["used"] == summary["rejected"] == 1
    assert summary["remaining"] == summary["available"] == summary["failed"] == 0
    assert summary["completed"] == 0 and summary["states"]["rejected"] == 1
    assert_summary_partition(stats(owner, pid))
    assert owner.post("/api/exchange", json={"code": code}).status_code == 410
    response = owner.post(
        "/api/redeem", json={"token": token, "params": {"email": "another@example.com"}}
    )
    assert response.status_code == 410, response.text
    receipt = owner.post("/api/receipt", json={"token": token})
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["job"]["message"] == "The supplied account is not eligible"
    assert receipt.json()["job"]["can_retry"] is False
    assert (
        owner.post(f"/api/admin/cards/{card_id(code)}/revoke", json={}).status_code
        == 409
    )


def test_expired_return_is_unavailable_and_revocation_takes_precedence(
    owner, setup_product
):
    pid, code = setup_product()
    token, task = redeem(owner, code)
    outcome(owner, task, "request_changes", "Correct the email")
    expire(code)
    row = inventory(owner, pid)["items"][0]
    assert row["job_state"] == "needs_input" and row["status"] == "expired"
    summary = stats(owner, pid)["summary"]
    assert summary["states"]["expired"] == 1 and summary["states"]["needs_input"] == 0
    assert summary["remaining"] == summary["available"] == summary["used"] == 0
    assert_summary_partition(stats(owner, pid))
    assert owner.post("/api/exchange", json={"code": code}).status_code == 410
    response = owner.post(
        "/api/redeem", json={"token": token, "params": {"email": "fixed@example.com"}}
    )
    assert response.status_code == 410, response.text
    assert (
        owner.post("/api/receipt", json={"token": token}).json()["job"]["can_retry"]
        is False
    )
    assert (
        owner.post(f"/api/admin/cards/{card_id(code)}/revoke", json={}).status_code
        == 200
    )
    summary = stats(owner, pid)["summary"]
    assert summary["states"]["revoked"] == 1 and summary["states"]["expired"] == 0
    assert summary["remaining"] == summary["available"] == summary["used"] == 0
    assert_summary_partition(stats(owner, pid))


def test_rejected_order_keeps_its_terminal_status_after_expiry(owner, setup_product):
    pid, code = setup_product()
    token, task = redeem(owner, code)
    outcome(owner, task, "reject", "Permanent business rejection")
    expire(code)
    row = inventory(owner, pid)["items"][0]
    assert row["status"] == "rejected"
    summary = stats(owner, pid)["summary"]
    assert summary["rejected"] == summary["used"] == 1
    assert summary["states"]["expired"] == summary["remaining"] == 0
    assert owner.post("/api/exchange", json={"code": code}).status_code == 410
    assert_summary_partition(stats(owner, pid))


def test_all_outcomes_partition_each_variant_and_product_totals(owner):
    product = create_product(
        owner, [variant("basic", "基础版"), variant("pro", "高级版")]
    )
    pid = product["id"]
    unused, returned, retry_failed, queued = issue_variant(owner, pid, "basic", 4)
    rejected, succeeded, expired_return, revoked_return = issue_variant(
        owner, pid, "pro", 4
    )
    for code in (returned, expired_return, revoked_return):
        _, task = redeem(owner, code)
        outcome(owner, task, "request_changes", "Please supply valid information")
    _, task = redeem(owner, retry_failed)
    batch(owner, task, "claim")
    batch(owner, task, "fail", retryable=True)
    redeem(owner, queued)
    _, task = redeem(owner, rejected)
    outcome(owner, task, "reject", "Business rules reject this request")
    _, task = redeem(owner, succeeded)
    finish(owner, task)
    expire(expired_return)
    assert (
        owner.post(
            f"/api/admin/cards/{card_id(revoked_return)}/revoke", json={}
        ).status_code
        == 200
    )
    result = stats(owner, pid)
    assert_summary_partition(result)
    summary = result["summary"]
    assert summary["total"] == 8
    assert summary["remaining"] == 2 and summary["available"] == 3
    assert summary["used"] == 4 and summary["rejected"] == 1
    assert summary["failed"] == summary["completed"] == summary["in_progress"] == 1
    assert summary["verified"] == 7
    for state in (
        "unused",
        "needs_input",
        "failed_retryable",
        "queued",
        "rejected",
        "succeeded",
        "expired",
        "revoked",
    ):
        assert summary["states"][state] == 1
    for vid, expected in (("basic", 2), ("pro", 0)):
        bucket = next(v for v in result["variants"] if v["variant_id"] == vid)
        assert bucket["summary"]["remaining"] == expected
    for state, vid in (("needs_input", "basic"), ("rejected", "pro")):
        scoped = inventory(owner, pid, variant_id=vid, status=state)
        assert scoped["total"] == 1 and scoped["items"][0]["status"] == state
        assert scoped["summary"] == summary
    assert inventory(owner, pid, status="needs_input")["items"][0]["id"] == card_id(
        returned
    )
    assert inventory(owner, pid, status="unused")["items"][0]["id"] == card_id(unused)
    assert_summary_partition(stats(owner))


@pytest.mark.parametrize(
    "action,state,event_type",
    (
        ("request_changes", "needs_input", "fulfillment.needs_input"),
        ("reject", "rejected", "fulfillment.rejected"),
    ),
)
def test_history_records_outcome_without_customer_input_or_reasons(
    owner, setup_product, action, state, event_type
):
    pid, code = setup_product()
    token, task = redeem(owner, code)
    outcome(owner, task, action, "PRIVATE CUSTOMER SPECIFIC REASON")
    response = owner.get(f"/api/admin/cards/{card_id(code)}/history")
    assert response.status_code == 200, response.text
    details = response.json()
    matching = [event for event in details["timeline"] if event["type"] == event_type]
    assert matching and matching[-1]["state"] == state
    assert all(
        set(event) <= {"id", "type", "created", "attempt", "state", "progress"}
        for event in details["timeline"]
    )
    assert all(
        secret not in response.text
        for secret in (
            code,
            token,
            "user@example.com",
            "PRIVATE CUSTOMER SPECIFIC REASON",
        )
    )
    receipt = owner.post("/api/receipt", json={"token": token})
    assert (
        receipt.status_code == 200
        and receipt.json()["job"]["message"] == "PRIVATE CUSTOMER SPECIFIC REASON"
    )


@pytest.mark.parametrize(
    "action,state", (("request_changes", "needs_input"), ("reject", "rejected"))
)
def test_outcome_inventory_stays_within_management_product_scope(
    owner, setup_product, action, state
):
    pid, code = setup_product(name="Authorized product")
    other_pid, other_code = setup_product(name="Other private product")
    _, task = redeem(owner, code)
    outcome(owner, task, action, "PRIVATE REASON")
    _, task = redeem(owner, other_code)
    outcome(owner, task, action, "OTHER PRIVATE REASON")
    link = create_link(owner, pid, ["cards.manage"])
    login_link(owner, link)
    response = owner.get("/api/manage/card-inventory", params={"status": state})
    assert response.status_code == 200, response.text
    assert (
        response.json()["total"] == 1
        and response.json()["items"][0]["product_id"] == pid
    )
    assert (
        other_pid not in response.text and "Other private product" not in response.text
    )
    summary = owner.get("/api/manage/card-stats").json()
    assert summary["summary"]["total"] == 1
    assert_summary_partition(summary)
    assert (
        owner.get(
            "/api/manage/card-inventory",
            params={"product_id": other_pid, "status": state},
        ).status_code
        == 403
    )
    assert (
        owner.get(f"/api/manage/cards/{card_id(other_code)}/history").status_code == 403
    )

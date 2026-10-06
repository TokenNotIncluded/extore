import json
import time
import uuid

import pytest
from fastapi import HTTPException
from test_product_links import create_link, login_link
from test_redemption import finish, redeem

from extore.db import db
from extore.security import card_digest

STATES = {
    "unused",
    "queued",
    "processing",
    "succeeded",
    "failed_retryable",
    "failed_terminal",
    "destroyed",
    "revoked",
    "expired",
}
SAFE_CARD_FIELDS = {
    "id",
    "product_id",
    "product_name",
    "state",
    "status",
    "created",
    "code_suffix",
    "batch_id",
    "batch_label",
    "expires",
    "first_verified",
    "job_id",
    "job_state",
    "attempt",
    "retryable",
    "revealed",
    "used_at",
    "updated",
}


def card_id(code):
    with db() as c:
        return c.execute(
            "SELECT id FROM cards WHERE digest=?", (card_digest(code),)
        ).fetchone()[0]


def issue(owner, pid, count=1, **kwargs):
    response = owner.post(
        "/api/admin/cards", json={"product_id": pid, "count": count, **kwargs}
    )
    assert response.status_code == 200, response.text
    return response.json()["codes"]


def batch(owner, task, action, **kwargs):
    response = owner.post(
        "/api/manage/batch",
        json={
            "product_id": task["product_id"],
            "ids": [task["id"]],
            "action": action,
            **kwargs,
        },
    )
    assert response.status_code == 200, response.text


def expire(code):
    with db() as c:
        c.execute(
            "UPDATE card_meta SET expires=? WHERE card_id=?",
            (time.time() - 1, card_id_from_connection(c, code)),
        )


def card_id_from_connection(c, code):
    return c.execute(
        "SELECT id FROM cards WHERE digest=?", (card_digest(code),)
    ).fetchone()[0]


def inventory(owner, pid="", **kwargs):
    response = owner.get(
        "/api/admin/card-inventory", params={"product_id": pid, **kwargs}
    )
    assert response.status_code == 200, response.text
    return response.json()


def stats(owner, pid=""):
    response = owner.get("/api/admin/card-stats", params={"product_id": pid})
    assert response.status_code == 200, response.text
    return response.json()


def assert_safe_card(item):
    assert set(item) <= SAFE_CARD_FIELDS
    assert item["status"] in STATES
    assert item["id"] and item["product_id"]


def test_empty_store_returns_zero_stats(owner):
    result = stats(owner)
    assert result["products"] == []
    assert all(
        result["summary"][key] == 0
        for key in (
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
    )
    assert result["summary"]["states"] == dict.fromkeys(STATES, 0)


def test_empty_inventory_and_zero_card_product_are_visible(owner):
    response = owner.post("/api/admin/products", json={"name": "尚未发行"})
    assert response.status_code == 200, response.text
    pid = response.json()["id"]
    result = stats(owner)
    assert result["summary"]["total"] == 0
    assert result["summary"]["remaining"] == 0
    assert result["summary"]["available"] == 0
    assert result["summary"]["states"] == dict.fromkeys(STATES, 0)
    assert [
        (p["product_id"], p["product_name"], p["total"]) for p in result["products"]
    ] == [(pid, "尚未发行", 0)]
    result = inventory(owner)
    assert result["items"] == [] and result["total"] == 0
    assert result["offset"] == 0 and result["limit"] == 100


def test_product_inventory_and_totals_stay_scoped(owner, setup_product):
    pid, code = setup_product(name="商品 A")
    issue(owner, pid, 2)
    other_pid, other_code = setup_product(name="商品 B")
    redeem(owner, other_code)
    result = stats(owner)
    assert result["summary"]["total"] == 4
    assert result["summary"]["remaining"] == 3
    assert result["summary"]["used"] == 1
    products = {p["product_id"]: p for p in result["products"]}
    assert products[pid]["total"] == 3 and products[pid]["used"] == 0
    assert products[other_pid]["total"] == 1 and products[other_pid]["used"] == 1
    scoped = stats(owner, pid)
    assert scoped["summary"]["total"] == 3
    assert [p["product_id"] for p in scoped["products"]] == [pid]
    rows = inventory(owner, pid)["items"]
    assert {row["product_id"] for row in rows} == {pid}
    assert card_id(code) in {row["id"] for row in rows}
    for row in rows:
        assert_safe_card(row)


def test_verified_is_distinct_from_used_and_repeated_checks_do_not_inflate_it(
    owner, setup_product
):
    pid, code = setup_product()
    cid = card_id(code)
    first = owner.post("/api/exchange", json={"code": code})
    assert first.status_code == 200, first.text
    first_verified = inventory(owner, pid)["items"][0]["first_verified"]
    assert first_verified is not None
    for _ in range(2):
        assert owner.post("/api/exchange", json={"code": code}).status_code == 200
    summary = stats(owner, pid)["summary"]
    assert summary["verified"] == 1 and summary["used"] == 0
    assert summary["remaining"] == 1 and summary["available"] == 1
    row = inventory(owner, pid)["items"][0]
    assert row["id"] == cid and row["first_verified"] == first_verified
    token = first.json()["token"]
    for _ in range(3):
        response = owner.post(
            "/api/redeem",
            json={"token": token, "params": {"email": "user@example.com"}},
        )
        assert response.status_code == 200, response.text
    summary = stats(owner, pid)["summary"]
    assert summary["used"] == 1 and summary["verified"] == 1
    assert summary["remaining"] == 0 and summary["available"] == 0
    row = inventory(owner, pid)["items"][0]
    assert row["used_at"] is not None and row["attempt"] == 1


def test_every_inventory_state_is_counted_once(owner, setup_product):
    pid, first_code = setup_product()
    codes = [first_code, *issue(owner, pid, 8)]
    (
        unused,
        queued,
        processing,
        succeeded,
        retryable,
        terminal,
        destroyed,
        revoked,
        expired,
    ) = codes
    redeem(owner, queued)
    _, task = redeem(owner, processing)
    batch(owner, task, "claim")
    _, task = redeem(owner, succeeded)
    finish(owner, task, "success payload")
    for code, may_retry in ((retryable, True), (terminal, False)):
        _, task = redeem(owner, code)
        batch(owner, task, "claim")
        batch(owner, task, "fail", retryable=may_retry)
    token, task = redeem(owner, destroyed)
    finish(owner, task, "destroyed payload")
    assert owner.post("/api/receipt/reveal", json={"token": token}).status_code == 200
    assert owner.post("/api/receipt/destroy", json={"token": token}).status_code == 200
    assert (
        owner.post(f"/api/admin/cards/{card_id(revoked)}/revoke", json={}).status_code
        == 200
    )
    expire(expired)
    result = stats(owner, pid)
    summary = result["summary"]
    assert summary["states"] == dict.fromkeys(STATES, 1)
    assert sum(summary["states"].values()) == summary["total"] == 9
    assert summary["remaining"] == 1 and summary["available"] == 2
    assert summary["used"] == 6 and summary["verified"] == 6
    assert summary["viewed"] == 1 and summary["in_progress"] == 2
    assert summary["completed"] == 2
    assert summary["failed"] == 2
    expected = dict(
        zip(
            map(card_id, codes),
            (
                "unused",
                "queued",
                "processing",
                "succeeded",
                "failed_retryable",
                "failed_terminal",
                "destroyed",
                "revoked",
                "expired",
            ),
            strict=True,
        )
    )
    assert {r["id"]: r["status"] for r in inventory(owner, pid)["items"]} == expected
    assert card_id(unused) in expected
    for state in STATES:
        filtered = inventory(owner, pid, status=state)
        assert filtered["total"] == 1 and len(filtered["items"]) == 1
        assert filtered["items"][0]["status"] == state
        assert filtered["summary"] == summary


def test_retry_exhaustion_and_disabled_retry_are_unavailable(owner, setup_product):
    for kwargs in ({"allow_retry": False}, {"max_attempts": 1}):
        pid, code = setup_product(**kwargs)
        _, task = redeem(owner, code)
        batch(owner, task, "claim")
        batch(owner, task, "fail", retryable=True)
        result = stats(owner, pid)["summary"]
        assert result["states"]["failed_terminal"] == 1
        assert result["states"]["failed_retryable"] == 0
        assert result["available"] == 0 and result["used"] == 1


def test_multiple_attempts_count_as_one_used_card_and_keep_initial_use_time(
    owner, setup_product
):
    pid, code = setup_product()
    token, task = redeem(owner, code)
    initial_used_at = inventory(owner, pid)["items"][0]["used_at"]
    batch(owner, task, "claim")
    batch(owner, task, "fail", retryable=True)
    response = owner.post(
        "/api/redeem", json={"token": token, "params": {"email": "second@example.com"}}
    )
    assert response.status_code == 200, response.text
    assert response.json()["attempt"] == 2
    row = inventory(owner, pid)["items"][0]
    assert row["attempt"] == 2 and row["used_at"] == initial_used_at
    summary = stats(owner, pid)["summary"]
    assert summary["used"] == 1 and summary["verified"] == 1
    assert summary["available"] == 0 and summary["states"]["queued"] == 1
    history = owner.get(f"/api/admin/cards/{card_id(code)}/history").json()
    assert {
        r["attempt"]
        for r in history["timeline"]
        if r["type"] in ("redemption.requested", "redemption.retried")
    } == {1, 2}
    assert "second@example.com" not in json.dumps(history)


def test_service_completion_counts_as_used_without_delivery_view(owner, setup_product):
    pid, code = setup_product(delivery="service")
    token, task = redeem(owner, code)
    finish(owner, task)
    response = owner.post("/api/receipt/reveal", json={"token": token})
    assert response.status_code == 200 and response.json()["content"] is None
    summary = stats(owner, pid)["summary"]
    assert summary["used"] == 1 and summary["completed"] == 1
    assert summary["viewed"] == 0 and summary["states"]["succeeded"] == 1


def test_batches_suffix_search_and_pagination_keep_unfiltered_summary(
    owner, setup_product
):
    pid, initial = setup_product()
    future = time.time() + 86400
    codes = issue(owner, pid, 3, label="秋季批次", expires=future)
    rows = inventory(owner, pid)["items"]
    batch_rows = [r for r in rows if r["id"] != card_id(initial)]
    assert len({r["batch_id"] for r in batch_rows}) == 1
    batch_id = batch_rows[0]["batch_id"]
    assert batch_id and all(r["batch_label"] == "秋季批次" for r in batch_rows)
    assert all(r["expires"] == future for r in batch_rows)
    assert {r["code_suffix"] for r in batch_rows} == {
        code.replace("-", "")[-6:] for code in codes
    }
    filtered = inventory(owner, pid, batch_id=batch_id, offset=1, limit=1)
    assert filtered["total"] == 3 and len(filtered["items"]) == 1
    assert filtered["offset"] == 1 and filtered["limit"] == 1
    assert filtered["summary"]["total"] == 4
    assert inventory(owner, pid, batch_id=batch_id, offset=3)["items"] == []
    row = batch_rows[0]
    for query in (row["id"], row["code_suffix"]):
        searched = inventory(owner, pid, search=query)
        assert row["id"] in {r["id"] for r in searched["items"]}
        assert searched["summary"]["total"] == 4
    assert inventory(owner, pid, search="%' OR 1=1 --")["total"] == 0


@pytest.mark.parametrize(
    "query",
    (
        {"offset": -1},
        {"offset": "bad"},
        {"limit": 0},
        {"limit": 501},
        {"limit": "bad"},
        {"status": "ready"},
        {"status": "invalid"},
    ),
)
def test_inventory_invalid_filters_are_rejected(owner, setup_product, query):
    pid, _ = setup_product()
    response = owner.get(
        "/api/admin/card-inventory", params={"product_id": pid, **query}
    )
    assert response.status_code == 422, response.text


def test_history_is_chronological_and_does_not_expose_delivery_or_customer_data(
    owner, setup_product
):
    secret = "a-private-hook-signing-secret-12345678"
    pid, code = setup_product(
        webhook_url="https://example.com/private-hook", webhook_secret=secret
    )
    token, task = redeem(owner, code)
    batch(owner, task, "claim")
    batch(owner, task, "progress", progress=45, message="PRIVATE STAFF NOTE")
    batch(
        owner,
        task,
        "succeed",
        content="PRIVATE DELIVERY CONTENT",
        message="PRIVATE STAFF NOTE",
    )
    assert owner.post("/api/receipt/reveal", json={"token": token}).status_code == 200
    response = owner.get(
        f"/api/admin/cards/{card_id(code)}/history", params={"product_id": pid}
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert_safe_card(result["card"])
    assert result["card"]["status"] == "succeeded" and result["card"]["revealed"]
    timeline = result["timeline"]
    assert timeline and [r["created"] for r in timeline] == sorted(
        r["created"] for r in timeline
    )
    assert {
        "redemption.requested",
        "fulfillment.progress",
        "fulfillment.succeeded",
        "delivery.viewed",
    } <= {r["type"] for r in timeline}
    for entry in timeline:
        assert set(entry) <= {"id", "type", "created", "attempt", "state", "progress"}
        assert entry["id"] and entry["type"]
    inventory_response = owner.get(
        "/api/admin/card-inventory", params={"product_id": pid}
    )
    for output in (
        response.text,
        inventory_response.text,
        json.dumps(stats(owner, pid)),
    ):
        assert all(
            private not in output
            for private in (
                code,
                token,
                "user@example.com",
                secret,
                "private-hook",
                "PRIVATE DELIVERY CONTENT",
                "PRIVATE STAFF NOTE",
            )
        )


def test_legacy_cards_without_metadata_remain_visible(owner):
    response = owner.post("/api/admin/products", json={"name": "旧商品"})
    assert response.status_code == 200, response.text
    pid = response.json()["id"]
    cid = str(uuid.uuid4())
    with db() as c:
        c.execute(
            "INSERT INTO cards(id,digest,product_id,created) VALUES (?,?,?,?)",
            (cid, card_digest("LEGACY-PRIVATE-CODE"), pid, time.time()),
        )
    rows = inventory(owner, pid)["items"]
    assert len(rows) == 1 and rows[0]["id"] == cid
    for field in (
        "code_suffix",
        "batch_id",
        "batch_label",
        "expires",
        "first_verified",
    ):
        assert rows[0][field] is None
    assert rows[0]["status"] == "unused"
    assert stats(owner, pid)["summary"]["remaining"] == 1


@pytest.mark.parametrize("endpoint", ("card-stats", "card-inventory"))
def test_tracking_requires_login_and_known_owner_product(
    owner, setup_product, endpoint
):
    pid, _ = setup_product()
    assert (
        owner.get(
            f"/api/admin/{endpoint}", params={"product_id": "missing"}
        ).status_code
        == 404
    )
    owner.cookies.clear()
    for prefix in ("admin", "manage"):
        assert (
            owner.get(
                f"/api/{prefix}/{endpoint}", params={"product_id": pid}
            ).status_code
            == 401
        )


def test_history_requires_known_card_and_matching_product(owner, setup_product):
    pid, code = setup_product()
    other_pid, _ = setup_product()
    assert owner.get("/api/admin/cards/missing/history").status_code == 404
    response = owner.get(
        f"/api/admin/cards/{card_id(code)}/history", params={"product_id": other_pid}
    )
    assert response.status_code in (403, 404)
    assert code not in response.text


@pytest.mark.parametrize("permission", ("queue.view", "product.edit", "events.manage"))
def test_other_permissions_do_not_grant_card_tracking(owner, setup_product, permission):
    pid, code = setup_product()
    link = create_link(owner, pid, [permission])
    login_link(owner, link)
    for path in ("card-stats", "card-inventory", f"cards/{card_id(code)}/history"):
        assert owner.get(f"/api/manage/{path}").status_code == 403
        assert owner.get(f"/api/admin/{path}").status_code == 401


def test_card_manager_sees_only_its_product_and_no_foreign_card_history(
    owner, setup_product
):
    pid, code = setup_product(name="授权商品")
    issue(owner, pid, 2)
    other_pid, other_code = setup_product(name="无权商品")
    link = create_link(owner, pid, ["cards.manage"])
    login_link(owner, link)
    for endpoint in ("card-stats", "card-inventory"):
        for query in ({}, {"product_id": pid}):
            response = owner.get(f"/api/manage/{endpoint}", params=query)
            assert response.status_code == 200, response.text
            assert response.json()["summary"]["total"] == 3
            assert "无权商品" not in response.text and other_pid not in response.text
        for foreign in (other_pid, "missing"):
            assert (
                owner.get(
                    f"/api/manage/{endpoint}", params={"product_id": foreign}
                ).status_code
                == 403
            )
    assert owner.get(f"/api/manage/cards/{card_id(code)}/history").status_code == 200
    assert (
        owner.get(f"/api/manage/cards/{card_id(other_code)}/history").status_code == 403
    )


@pytest.mark.parametrize("endpoint", ("card-stats", "card-inventory", "history"))
def test_revoked_link_is_rechecked_inside_tracking_transaction(
    owner, setup_product, monkeypatch, endpoint
):
    import extore.card_tracking as tracking

    pid, code = setup_product()
    link = create_link(owner, pid, ["cards.manage"])
    login_link(owner, link)
    original_session = tracking.session

    def revoke_after_login_check(*args, **kwargs):
        value = original_session(*args, **kwargs)
        with db() as c:
            c.execute("UPDATE staff SET revoked=1 WHERE id=?", (link["id"],))
        return value

    monkeypatch.setattr(tracking, "session", revoke_after_login_check)
    path = f"cards/{card_id(code)}/history" if endpoint == "history" else endpoint
    response = owner.get(f"/api/manage/{path}")
    assert response.status_code == 401, response.text
    assert code not in response.text and pid not in response.text


def test_expired_unused_card_cannot_be_verified_or_submitted_with_older_grant(
    owner, setup_product
):
    pid, code = setup_product()
    token = owner.post("/api/exchange", json={"code": code}).json()["token"]
    expire(code)
    assert owner.post("/api/exchange", json={"code": code}).status_code == 410
    response = owner.post(
        "/api/redeem", json={"token": token, "params": {"email": "user@example.com"}}
    )
    assert response.status_code == 410, response.text
    assert stats(owner, pid)["summary"]["states"]["expired"] == 1
    assert stats(owner, pid)["summary"]["used"] == 0
    with db() as c:
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_expired_failed_card_cannot_start_another_attempt(owner, setup_product):
    pid, code = setup_product()
    token, task = redeem(owner, code)
    batch(owner, task, "claim")
    batch(owner, task, "fail", retryable=True)
    expire(code)
    assert owner.post("/api/exchange", json={"code": code}).status_code == 410
    response = owner.post(
        "/api/redeem", json={"token": token, "params": {"email": "user@example.com"}}
    )
    assert response.status_code == 410, response.text
    summary = stats(owner, pid)["summary"]
    assert summary["available"] == 0 and summary["used"] == 1
    assert summary["states"]["failed_terminal"] == 1
    assert summary["states"]["expired"] == 0
    with db() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (task["id"],)).fetchone()
        assert row["state"] == "failed" and row["attempt"] == 1


def test_expiration_preserves_in_progress_delivery_and_completed_repeat_view(
    owner, setup_product
):
    pid, code = setup_product()
    token, task = redeem(owner, code)
    expire(code)
    assert stats(owner, pid)["summary"]["states"]["queued"] == 1
    batch(owner, task, "claim")
    batch(owner, task, "succeed", content="KEEP THIS DELIVERY")
    assert owner.post("/api/exchange", json={"code": code}).status_code == 200
    for _ in range(2):
        response = owner.post("/api/receipt/reveal", json={"token": token})
        assert response.status_code == 200, response.text
        assert response.json()["content"] == "KEEP THIS DELIVERY"
    summary = stats(owner, pid)["summary"]
    assert summary["states"]["succeeded"] == 1
    assert summary["viewed"] == 1 and summary["used"] == 1


def test_issue_tracking_hook_is_idempotent_and_does_not_store_full_code(
    owner, setup_product
):
    from extore.card_tracking import record_issue

    pid, code = setup_product()
    with db() as c:
        before = dict(
            c.execute(
                "SELECT * FROM card_meta WHERE card_id=?",
                (card_id_from_connection(c, code),),
            ).fetchone()
        )
        batches_before = c.execute("SELECT count(*) FROM card_batches").fetchone()[0]
        record_issue(
            c, pid, [code], label="不能覆盖原批次", expires=time.time() + 90000
        )
        after = dict(
            c.execute(
                "SELECT * FROM card_meta WHERE card_id=?",
                (card_id_from_connection(c, code),),
            ).fetchone()
        )
        assert before == after
        assert (
            c.execute("SELECT count(*) FROM card_batches").fetchone()[0]
            == batches_before
        )
        for table in ("cards", "card_meta", "card_batches"):
            assert code not in str(
                [dict(r) for r in c.execute(f"SELECT * FROM {table}")]
            )


@pytest.mark.parametrize("expiry", (-1, 0, float("nan"), float("inf")))
def test_issue_tracking_hook_rejects_invalid_expiration(owner, setup_product, expiry):
    from extore.card_tracking import record_issue

    pid, code = setup_product()
    with db() as c:
        with pytest.raises(HTTPException) as error:
            record_issue(c, pid, [code], expires=expiry)
        assert error.value.status_code == 400

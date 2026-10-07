"""Real customer/worker boundaries for immutable, consumable card properties."""

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from extore.card_entitlements import allocated_bytes, validate_capacity
from extore.db import db
from extore.models import Product, RevisionRequest


def create(owner, credits=3, key="edits", **values):
    response = owner.post(
        "/api/admin/products",
        json={
            "name": "Editable delivery",
            "parameters": [
                {
                    "key": "requirements",
                    "label": {"en": "Requirements"},
                    "type": "textarea",
                }
            ],
            "revision_policy": {
                "attribute_key": key,
                "label": {"en": "Included revisions"},
            },
            "variants": [
                {
                    "id": "default",
                    "name": "Document",
                    "attributes": {key: credits, "format": "DOCX"},
                }
            ],
            **values,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def issue(owner, product, **values):
    response = owner.post(
        "/api/admin/cards", json={"product_id": product["id"], **values}
    )
    assert response.status_code == 200, response.text
    return response.json()["codes"][0]


def exchange(owner, code):
    response = owner.post("/api/exchange", json={"code": code})
    assert response.status_code == 200, response.text
    return response.json()


def launch(owner, product, **issuance):
    code = issue(owner, product, **issuance)
    view = exchange(owner, code)
    response = owner.post(
        "/api/redeem",
        json={
            "token": view["token"],
            "params": {"requirements": "Original requirements"},
        },
    )
    assert response.status_code == 200, response.text
    return code, view["token"], response.json()


def batch(owner, row, action, **values):
    return owner.post(
        "/api/manage/batch",
        json={
            "product_id": row["product_id"],
            "ids": [row["id"]],
            "attempt": row["attempt"],
            "action": action,
            **values,
        },
    )


def complete(owner, row, content="First delivery"):
    response = batch(owner, row, "claim")
    assert response.status_code == 200, response.text
    response = batch(owner, row, "succeed", content=content)
    assert response.status_code == 200, response.text


def receipt(owner, token):
    response = owner.post("/api/receipt", json={"token": token})
    assert response.status_code == 200, response.text
    return response.json()


def revision(owner, token, current=0, message="Please improve the chart.", **values):
    return owner.post(
        "/api/receipt/revisions",
        json={
            "token": token,
            "request_id": str(uuid.uuid4()),
            "expected_revision": current,
            "message": message,
            **values,
        },
    )


@pytest.mark.parametrize("invalid", [True, 1.0, "1", None, -1, 1001])
def test_capacity_is_a_strict_bounded_integer(invalid):
    with pytest.raises(ValueError):
        validate_capacity({"custom": invalid}, {"attribute_key": "custom"})
    with pytest.raises(ValidationError):
        Product(
            name="No",
            revision_policy={"attribute_key": "custom", "label": {"en": "Rounds"}},
            variants=[
                {"id": "default", "name": "One", "attributes": {"custom": invalid}}
            ],
        )


@pytest.mark.parametrize(
    "values",
    [
        {"view_policy": "once"},
        {"delivery": "service"},
        {"mode": "stock", "parameters": []},
    ],
)
def test_revision_policy_rejects_incompatible_delivery(values):
    with pytest.raises(ValidationError):
        Product(
            name="No",
            revision_policy={"attribute_key": "custom", "label": {"en": "Rounds"}},
            **values,
        )


def test_absent_attribute_is_zero_and_issue_override_is_validated(owner):
    product = create(
        owner, variants=[{"id": "default", "name": "No edits", "attributes": {}}]
    )
    view = exchange(owner, issue(owner, product))
    assert view["entitlements"]["total"] == 0
    view = exchange(
        owner, issue(owner, product, attributes={"edits": 2, "customer_tag": "custom"})
    )
    assert view["card_attributes"] == {"edits": 2, "customer_tag": "custom"}
    assert view["entitlements"]["total"] == 2
    for invalid in (True, 1.0, "1", None, -1, 1001):
        response = owner.post(
            "/api/admin/cards",
            json={"product_id": product["id"], "attributes": {"edits": invalid}},
        )
        assert response.status_code == 400, response.text


def test_issued_attributes_and_policy_never_inherit_sku_edits(owner):
    product = create(owner, credits=1)
    code = issue(owner, product, attributes={"edits": 2, "customer_tag": "special"})
    old = exchange(owner, code)
    changed = {
        **product,
        "revision_policy": {"attribute_key": "new_key", "label": {"en": "New policy"}},
        "variants": [
            {
                "id": "default",
                "name": "Changed",
                "attributes": {"new_key": 9, "format": "PPTX"},
            }
        ],
    }
    assert (
        owner.put(f"/api/admin/products/{product['id']}", json=changed).status_code
        == 200
    )
    retained = receipt(owner, old["token"])
    assert retained["card_attributes"] == {
        "edits": 2,
        "format": "DOCX",
        "customer_tag": "special",
    }
    assert retained["entitlements"]["attribute_key"] == "edits"
    assert retained["entitlements"]["total"] == 2
    fresh = exchange(owner, issue(owner, changed))
    assert fresh["entitlements"]["attribute_key"] == "new_key"
    assert fresh["entitlements"]["total"] == 9


@pytest.mark.parametrize("remove_record", [False, True])
def test_old_or_explicit_null_policy_cards_cannot_acquire_future_rights(
    owner, remove_record
):
    product = create(owner, revision_policy=None)
    code = issue(owner, product)
    if remove_record:
        with db() as c:
            c.execute("DELETE FROM card_entitlements")
    changed = {
        **product,
        "revision_policy": {"attribute_key": "edits", "label": {"en": "Revisions"}},
    }
    assert (
        owner.put(f"/api/admin/products/{product['id']}", json=changed).status_code
        == 200
    )
    view = exchange(owner, code)
    assert view["entitlements"] is None
    assert view["product"]["revision_policy"] is None


def test_one_revision_request_consumes_one_round_and_retains_original_input(owner):
    product = create(owner, credits=1)
    _, token, row = launch(owner, product)
    complete(owner, row)
    initial = receipt(owner, token)
    assert initial["entitlements"]["can_request"] is True
    response = revision(owner, token)
    assert response.status_code == 200, response.text
    changed = response.json()
    assert changed["id"] == row["id"] and changed["attempt"] == row["attempt"] + 1
    assert changed["revision"] == {
        "current": 1,
        "message": "Please improve the chart.",
        "is_revision": True,
    }
    assert changed["entitlements"]["remaining"] == 0
    with db() as c:
        saved = c.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
        assert json.loads(saved["params"]) == {"requirements": "Original requirements"}
        assert (
            c.execute(
                "SELECT state FROM cards WHERE id=?", (saved["card_id"],)
            ).fetchone()[0]
            == "used"
        )
        event = json.loads(
            c.execute(
                "SELECT payload FROM events WHERE type='revision.requested'"
            ).fetchone()[0]
        )
        assert event["data"]["revision"]["current"] == 1
        assert event["data"]["params"] == {"requirements": "Original requirements"}
        assert event["data"]["card_attributes"]["edits"] == 1
    old = owner.post("/api/receipt/reveal", json={"token": token})
    assert old.status_code == 200 and old.json()["content"] == "First delivery"
    assert old.json()["revision"] == 0
    complete(owner, changed, content="Second delivery")
    assert revision(owner, token, current=1).status_code == 409
    assert (
        owner.post("/api/receipt/reveal", json={"token": token}).json()["content"]
        == "Second delivery"
    )
    assert (
        owner.post("/api/receipt/reveal", json={"token": token, "revision": 0}).json()[
            "content"
        ]
        == "First delivery"
    )


def test_same_request_is_idempotent_and_changed_content_conflicts(owner):
    product = create(owner)
    _, token, row = launch(owner, product)
    complete(owner, row)
    request_id = str(uuid.uuid4())
    for _ in range(3):
        response = revision(owner, token, request_id=request_id)
        assert response.status_code == 200, response.text
        assert response.json()["entitlements"]["used"] == 1
    assert (
        revision(owner, token, message="Different", request_id=request_id).status_code
        == 409
    )
    assert revision(owner, token, current=1, request_id=request_id).status_code == 409
    with db() as c:
        assert (
            c.execute("SELECT COUNT(*) FROM job_revision_requests").fetchone()[0] == 1
        )
        assert (
            c.execute("SELECT COUNT(*) FROM job_delivery_versions").fetchone()[0] == 1
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM events WHERE type='revision.requested'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("same_id", [False, True])
def test_concurrent_same_round_never_spends_twice(owner, same_id):
    product = create(owner)
    _, token, row = launch(owner, product)
    complete(owner, row)
    shared = str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(
            pool.map(
                lambda _: revision(
                    owner, token, request_id=shared if same_id else str(uuid.uuid4())
                ),
                range(6),
            )
        )
    assert sum(response.status_code == 200 for response in responses) == (
        6 if same_id else 1
    )
    assert receipt(owner, token)["entitlements"]["used"] == 1
    with db() as c:
        assert (
            c.execute("SELECT COUNT(*) FROM job_revision_requests").fetchone()[0] == 1
        )


def test_each_round_has_its_own_technical_retry_budget_without_extra_credit_use(owner):
    product = create(owner, credits=2, max_attempts=2)
    _, token, row = launch(owner, product)
    complete(owner, row)
    for current in (0, 1):
        response = revision(owner, token, current=current)
        assert response.status_code == 200, response.text
        working = response.json()
        assert batch(owner, working, "claim").status_code == 200
        assert batch(owner, working, "fail", retryable=True).status_code == 200
        failed = receipt(owner, token)["job"]
        assert failed["can_retry"] is True
        response = owner.post(
            "/api/redeem",
            json={"token": token, "params": {"requirements": "Original requirements"}},
        )
        assert response.status_code == 200, response.text
        retry = response.json()
        assert retry["attempt"] == working["attempt"] + 1
        assert retry["entitlements"]["used"] == current + 1
        complete(owner, retry, content=f"Revision {current + 1}")
    assert receipt(owner, token)["job"]["attempt"] == 5


def test_retry_after_revision_does_not_return_card_to_stock(owner):
    product = create(owner)
    _, token, row = launch(owner, product)
    complete(owner, row)
    row = revision(owner, token).json()
    assert batch(owner, row, "claim").status_code == 200
    assert (
        batch(
            owner,
            row,
            "request_retry",
            message="Please confirm your chart.",
            retry_mode="reuse",
            reason_type="external",
        ).status_code
        == 200
    )
    inventory = owner.get(
        "/api/manage/card-stats", params={"product_id": product["id"]}
    )
    assert inventory.status_code == 200, inventory.text
    summary = inventory.json()["summary"]
    assert (
        summary["used"] == 1 and summary["remaining"] == 0 and summary["available"] == 0
    )
    folders = owner.get(
        "/api/manage/card-batches", params={"product_id": product["id"]}
    )
    assert folders.status_code == 200, folders.text
    assert (
        folders.json()["items"][0]["used"] == 1
        and folders.json()["items"][0]["remaining"] == 0
    )
    assert owner.post("/api/retry", json={"token": token}).status_code == 200
    assert receipt(owner, token)["entitlements"]["used"] == 1


def test_old_completion_and_missing_attempt_cannot_write_a_revision(owner):
    product = create(owner)
    _, token, first = launch(owner, product)
    complete(owner, first)
    working = revision(owner, token).json()
    payload = {"product_id": product["id"], "ids": [first["id"]], "action": "claim"}
    assert owner.post("/api/manage/batch", json=payload).status_code == 409
    assert batch(owner, working, "claim").status_code == 200
    assert batch(owner, first, "succeed", content="stale").status_code == 409
    payload["action"] = "succeed"
    payload["content"] = "stale"
    assert owner.post("/api/manage/batch", json=payload).status_code == 409
    assert receipt(owner, token)["job"]["state"] == "processing"


def test_expiry_disabled_shop_rejection_and_destroy_prevent_new_work(owner):
    product = create(owner)
    _, token, row = launch(owner, product)
    complete(owner, row)
    with db() as c:
        c.execute("UPDATE card_meta SET expires=?", (time.time() - 1,))
    assert receipt(owner, token)["entitlements"]["reason"] == "expired"
    assert revision(owner, token).status_code == 409
    assert owner.post("/api/receipt/reveal", json={"token": token}).status_code == 200
    with db() as c:
        c.execute("UPDATE card_meta SET expires=NULL")
        c.execute("UPDATE shops SET enabled=0")
    assert revision(owner, token).status_code == 404
    with db() as c:
        c.execute("UPDATE shops SET enabled=1")
    row = revision(owner, token).json()
    assert batch(owner, row, "claim").status_code == 200
    assert (
        batch(owner, row, "reject", message="Outside the purchased scope").status_code
        == 200
    )
    assert revision(owner, token, current=1).status_code == 410
    assert (
        owner.post("/api/receipt/reveal", json={"token": token}).json()["content"]
        == "First delivery"
    )
    assert owner.post("/api/receipt/destroy", json={"token": token}).status_code == 200
    assert owner.post("/api/receipt/reveal", json={"token": token}).status_code == 409


def test_destroy_during_revision_scrubs_all_versions_and_invalidates_worker(owner):
    product = create(owner)
    _, token, first = launch(owner, product)
    complete(owner, first, content="Private initial result")
    row = revision(owner, token, message="Private correction suggestion").json()
    assert batch(owner, row, "claim").status_code == 200
    with db() as c:
        assert allocated_bytes(c) > 0
    assert owner.post("/api/receipt/destroy", json={"token": token}).status_code == 200
    assert batch(owner, row, "succeed", content="Late output").status_code == 409
    assert revision(owner, token, current=1).status_code == 410
    with db() as c:
        assert allocated_bytes(c) == 0
        archives = [
            dict(item) for item in c.execute("SELECT * FROM job_delivery_versions")
        ]
        assert archives[0]["content"] is None and archives[0]["result_json"] is None
        requests = [
            dict(item) for item in c.execute("SELECT * FROM job_revision_requests")
        ]
        assert len(requests) == 1 and requests[0]["message"] == ""
        assert "Private correction suggestion" not in json.dumps(
            [dict(item) for item in c.execute("SELECT * FROM events")]
        )


def test_revision_parameters_and_request_ids_cannot_spoof_authority(owner):
    product = create(owner)
    _, token, row = launch(owner, product)
    complete(owner, row)
    assert revision(owner, token, card_attributes={"edits": 999}).status_code == 422
    assert revision(owner, token, request_id="x" * 36).status_code == 422
    assert revision(owner, token, message="  ").status_code == 422
    with pytest.raises(ValidationError):
        RevisionRequest(
            token="token",
            request_id=str(uuid.uuid4()),
            expected_revision=True,
            message="Note",
        )


def test_revision_text_uses_quota_and_archiving_transfers_without_double_charge(
    owner, monkeypatch
):
    from extore import files

    product = create(owner, credits=2)
    _, token, first = launch(owner, product)
    complete(owner, first, content="Original")
    row = revision(owner, token, message="x").json()
    assert batch(owner, row, "claim").status_code == 200
    with db() as c:
        before = allocated_bytes(c)
    monkeypatch.setattr(files, "MAX_CARD_BYTES", before + 20)
    assert batch(owner, row, "succeed", content="x" * 30).status_code == 413
    assert receipt(owner, token)["job"]["state"] == "processing"
    assert (
        owner.post("/api/receipt/reveal", json={"token": token}).json()["content"]
        == "Original"
    )
    content = "Changed"
    extra = len(content.encode()) + len(json.dumps({"content": content}).encode())
    monkeypatch.setattr(files, "MAX_CARD_BYTES", before + extra)
    assert batch(owner, row, "succeed", content=content).status_code == 200
    with db() as c:
        assert allocated_bytes(c) == before + extra
    monkeypatch.setattr(files, "MAX_CARD_BYTES", before + extra + 1)
    response = revision(owner, token, current=1, message="y")
    assert response.status_code == 200, response.text
    with db() as c:
        assert allocated_bytes(c) == before + extra + 1


def test_progress_events_do_not_duplicate_full_revision_input_or_history(owner):
    product = create(owner)
    _, token, first = launch(owner, product)
    complete(owner, first)
    row = revision(owner, token, message="PRIVATE INPUT " * 500).json()
    assert batch(owner, row, "claim").status_code == 200
    assert (
        batch(
            owner, row, "progress", progress=25, message="Preparing draft"
        ).status_code
        == 200
    )
    with db() as c:
        events = [
            json.loads(item[0])
            for item in c.execute(
                "SELECT payload FROM events WHERE job_id=?", (row["id"],)
            )
        ]
    for event in events:
        data = event["data"]
        assert "deliveries" not in data
        if event["type"] == "revision.requested":
            assert data["card_attributes"]["edits"] == 3
            assert data["revision"]["message"].startswith("PRIVATE INPUT")
            assert data["last_delivery"]["revision"] == 0
        elif event["type"] != "redemption.requested":
            assert "card_attributes" not in data
            assert "message" not in data["revision"]


def test_display_editor_cannot_change_policy_or_revision_allowance(owner):
    from test_product_links import create_link, login_link

    product = create(owner)
    link = create_link(owner, product["id"], ["product.edit"])
    login_link(owner, link)
    current = owner.get("/api/manage/product", params={"product_id": product["id"]})
    assert current.status_code == 200, current.text
    payload = current.json()
    response = owner.put(
        "/api/manage/product",
        params={"product_id": product["id"]},
        json={**payload, "revision_policy": None},
    )
    assert response.status_code == 403, response.text
    variants = [
        {**variant, "attributes": {**variant["attributes"], "edits": 4}}
        for variant in payload["variants"]
    ]
    response = owner.put(
        "/api/manage/product",
        params={"product_id": product["id"]},
        json={**payload, "variants": variants},
    )
    assert response.status_code == 403, response.text
    variants[0]["attributes"]["edits"] = 3
    variants[0]["attributes"]["format"] = "PPTX"
    response = owner.put(
        "/api/manage/product",
        params={"product_id": product["id"]},
        json={**payload, "name": "New display title", "variants": variants},
    )
    assert response.status_code == 200, response.text


def test_schema21_upgrade_preserves_legacy_job_without_granting_new_entitlements(owner):
    from extore.db import init

    product = create(owner)
    _, token, row = launch(owner, product)
    complete(owner, row)
    with db() as c:
        for table in (
            "job_delivery_versions",
            "job_revision_requests",
            "card_entitlements",
        ):
            c.execute("DROP TABLE " + table)
        for column in ("revision_round", "revision_start_attempt", "revision_message"):
            c.execute("ALTER TABLE jobs DROP COLUMN " + column)
        c.execute("PRAGMA user_version=20")
        previous = dict(
            c.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
        )
    init()
    init()
    with db() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 21
        after = dict(
            c.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
        )
        assert {key: after[key] for key in previous} == previous
        assert after["revision_round"] == 0 and after["revision_start_attempt"] == 1
        assert after["revision_message"] == ""
        assert c.execute("SELECT COUNT(*) FROM card_entitlements").fetchone()[0] == 0
        assert not c.execute("PRAGMA foreign_key_check").fetchall()
    assert receipt(owner, token)["entitlements"] is None
    assert revision(owner, token).status_code == 409
    assert (
        owner.post("/api/receipt/reveal", json={"token": token}).json()["content"]
        == "First delivery"
    )

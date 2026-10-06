"""Exercise partial redemption through HTTP, using synthetic issuance only."""

import json
import time
import uuid

import pytest

from extore import service
from extore.db import db
from extore.models import JobUpdate
from extore.security import card_digest, digest


def issue(owner, pid, count=1):
    response = owner.post("/api/admin/cards", json={"product_id": pid, "count": count})
    assert response.status_code == 200, response.text
    return response.json()["codes"]


def create(owner, **config):
    response = owner.post(
        "/api/admin/products", json={"name": "独立参数商品", **config}
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def exchange(owner, *codes):
    response = owner.post("/api/batch/exchange", json={"code": "\n".join(codes)})
    assert response.status_code == 200, response.text
    return response.json()


def redeem(owner, receipt, *items):
    response = owner.post(
        "/api/batch/redeem", json={"token": receipt["token"], "items": list(items)}
    )
    assert response.status_code == 200, response.text
    return response.json()


def card_id(code):
    with db() as c:
        return c.execute(
            "SELECT id FROM cards WHERE digest=?", (card_digest(code),)
        ).fetchone()[0]


def inspect():
    with db() as c:
        return {
            table: [dict(row) for row in c.execute("SELECT * FROM " + table)]
            for table in ("cards", "jobs", "job_files", "events", "outbox")
        }


def test_exchange_keeps_valid_cards_with_bad_expired_revoked_and_duplicate_codes(
    owner, setup_product, caplog
):
    pid, first = setup_product()
    second, expired, revoked = issue(owner, pid, 3)
    expired_id, revoked_id = card_id(expired), card_id(revoked)
    with db() as c:
        c.execute(
            "UPDATE card_meta SET expires=? WHERE card_id=?",
            (time.time() - 1, expired_id),
        )
        c.execute("UPDATE cards SET state='revoked' WHERE id=?", (revoked_id,))
    result = exchange(
        owner, first, "NOT-A-CODE", first.lower(), expired, revoked, second
    )
    assert result["batch"] is True and result["partial"] is True
    assert [item["status"] for item in result["items"]] == [
        "valid",
        "invalid",
        "duplicate",
        "invalid",
        "invalid",
        "valid",
    ]
    assert result["items"][2]["duplicate_of"] == 0
    assert result["summary"] == {
        "total": 6,
        "accepted": 2,
        "valid": 2,
        "used": 0,
        "needs_retry": 0,
        "invalid": 3,
        "duplicate": 1,
    }
    for index in (1, 2, 3, 4):
        assert "card_id" not in result["items"][index]
        assert "product" not in result["items"][index]
    public = json.dumps(result)
    for code in (first, second, expired, revoked):
        assert code not in public and code not in caplog.text
    with db() as c:
        assert c.execute("SELECT count(*) FROM grants").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM receipt_batch_cards").fetchone()[0] == 2
        assert (
            c.execute(
                "SELECT count(*) FROM card_meta WHERE first_verified IS NOT NULL"
            ).fetchone()[0]
            == 2
        )
    saved = owner.post("/api/batch/receipt", json={"token": result["token"]})
    assert saved.status_code == 200
    assert [item["index"] for item in saved.json()["items"]] == [0, 5]
    assert "token" not in saved.json()


def test_all_bad_codes_create_no_authority_and_shape_errors_never_echo_input(owner):
    result = exchange(owner, "NO-SUCH-CODE", "OTHER-INVALID-CODE")
    assert result["token"] is None and result["summary"]["accepted"] == 0
    with db() as c:
        assert c.execute("SELECT count(*) FROM receipt_batches").fetchone()[0] == 0
    secret = "SYNTHETIC-SECRET-CODE"
    response = owner.post("/api/batch/exchange", json={"code": {"secret": secret}})
    assert response.status_code == 400 and secret not in response.text
    response = owner.post("/api/batch/exchange", json={"code": secret, "extra": secret})
    assert response.status_code == 400 and secret not in response.text
    response = owner.post("/api/batch/exchange", content=b'{"code": "broken')
    assert response.status_code == 400


def test_exchange_is_bounded_before_deduplication(owner, setup_product):
    _, code = setup_product()
    response = owner.post("/api/batch/exchange", json={"code": "\n".join([code] * 31)})
    assert response.status_code == 400 and code not in response.text
    with db() as c:
        assert c.execute("SELECT count(*) FROM receipt_batches").fetchone()[0] == 0


def test_invalid_unicode_is_rejected_without_blocking_other_cards(owner, setup_product):
    _, code = setup_product()
    response = owner.post(
        "/api/batch/exchange",
        content=json.dumps({"code": "invalid-\ud800\n" + code}).encode(),
    )
    assert response.status_code == 200
    result = response.json()
    assert [item["accepted"] for item in result["items"]] == [False, True]
    assert result["items"][0]["http_status"] == 400
    response = owner.post("/api/batch/receipt", content=b'{"token": "\\ud800"}')
    assert response.status_code == 400


def test_multiple_products_and_shops_keep_their_own_parameters(owner, setup_product):
    _, first = setup_product()
    other = create(owner, parameters=[{"key": "topic", "label": {"zh-CN": "主题"}}])
    second = issue(owner, other)[0]
    sid = str(uuid.uuid4())
    with db() as c:
        c.execute(
            "INSERT INTO shops(id,name,email,created,verified) VALUES (?,?,?,?,1)",
            (sid, "另外一家店", "other@example.test", time.time()),
        )
        c.execute("UPDATE products SET shop_id=? WHERE id=?", (sid, other))
    receipt = exchange(owner, first, second)
    a, b = receipt["items"]
    assert a["product"]["parameters"][0]["key"] == "email"
    assert b["product"]["parameters"][0]["key"] == "topic"
    result = redeem(
        owner,
        receipt,
        {"card_id": a["card_id"], "params": {"email": "first@example.test"}},
        {"card_id": b["card_id"], "params": {"topic": "independent"}},
    )
    assert result["submission_summary"]["succeeded"] == 2
    with db() as c:
        params = {
            row["card_id"]: json.loads(row["params"])
            for row in c.execute("SELECT * FROM jobs")
        }
    assert params[a["card_id"]] == {"email": "first@example.test"}
    assert params[b["card_id"]] == {"topic": "independent"}


def test_existing_repeat_delivery_is_accepted_and_can_still_be_revealed(
    owner, setup_product
):
    pid, code = setup_product()
    legacy = owner.post("/api/exchange", json={"code": code}).json()
    launched = owner.post(
        "/api/redeem",
        json={"token": legacy["token"], "params": {"email": "x@example.test"}},
    ).json()
    with db() as c:
        service.apply_update(
            c,
            launched["id"],
            JobUpdate(state="succeeded", attempt=1, content="PRIVATE-DELIVERY"),
        )
    receipt = exchange(owner, code, issue(owner, pid)[0])
    item = receipt["items"][0]
    assert item["status"] == "used" and item["accepted"] is True
    assert "PRIVATE-DELIVERY" not in json.dumps(receipt)
    reveal = owner.post(
        "/api/receipt/reveal",
        json={"token": receipt["token"], "card_id": item["card_id"]},
    )
    assert reveal.status_code == 200 and reveal.json()["content"] == "PRIVATE-DELIVERY"


def test_used_once_does_not_gain_new_receipt_authority(owner, setup_product):
    _, used = setup_product(view_policy="once")
    legacy = owner.post("/api/exchange", json={"code": used}).json()
    row = owner.post(
        "/api/redeem",
        json={"token": legacy["token"], "params": {"email": "x@example.test"}},
    ).json()
    with db() as c:
        service.apply_update(
            c,
            row["id"],
            JobUpdate(state="succeeded", attempt=1, content="ONE-TIME-CONTENT"),
        )
    _, ready = setup_product()
    receipt = exchange(owner, used, ready)
    assert receipt["items"][0]["status"] == "used"
    assert receipt["items"][0]["accepted"] is False
    assert "card_id" not in receipt["items"][0]
    assert receipt["summary"]["accepted"] == 1


def test_original_batch_receipt_can_reveal_its_once_delivery(owner, setup_product):
    _, code = setup_product(view_policy="once")
    receipt = exchange(owner, code)
    cid = receipt["items"][0]["card_id"]
    launched = redeem(
        owner, receipt, {"card_id": cid, "params": {"email": "x@example.test"}}
    )
    row = launched["results"][0]["job"]
    with db() as c:
        service.apply_update(
            c,
            row["id"],
            JobUpdate(state="succeeded", attempt=1, content="ORIGINAL-ONCE-DELIVERY"),
        )
    saved = owner.post("/api/batch/receipt", json={"token": receipt["token"]})
    assert saved.status_code == 200
    item = saved.json()["items"][0]
    assert item["accepted"] is True and item["status"] == "used"
    assert item["job"]["state"] == "succeeded"
    assert "ORIGINAL-ONCE-DELIVERY" not in saved.text
    reveal = {"token": receipt["token"], "card_id": cid}
    assert (
        owner.post("/api/receipt/reveal", json=reveal).json()["content"]
        == "ORIGINAL-ONCE-DELIVERY"
    )
    assert owner.post("/api/receipt/reveal", json=reveal).status_code == 410


def test_new_read_and_partial_submission_accept_legacy_batch_token(
    owner, setup_product
):
    pid, code = setup_product()
    legacy = owner.post(
        "/api/exchange", json={"code": code + "\n" + issue(owner, pid)[0]}
    ).json()
    response = owner.post("/api/batch/receipt", json={"token": legacy["token"]})
    assert response.status_code == 200 and response.json()["summary"]["accepted"] == 2
    result = redeem(
        owner,
        legacy,
        {
            "card_id": legacy["items"][0]["card_id"],
            "params": {"email": "x@example.test"},
        },
    )
    assert result["results"][0]["status"] == "submitted"


def test_needs_retry_is_visible_and_reuses_the_same_task(owner, setup_product):
    _, code = setup_product()
    receipt = exchange(owner, code)
    item = receipt["items"][0]
    initial = redeem(
        owner,
        receipt,
        {"card_id": item["card_id"], "params": {"email": "old@example.test"}},
    )
    row = initial["results"][0]["job"]
    claimed = owner.post(
        "/api/manage/batch",
        json={"product_id": row["product_id"], "ids": [row["id"]], "action": "claim"},
    )
    assert claimed.status_code == 200, claimed.text
    changed = owner.post(
        "/api/manage/batch",
        json={
            "product_id": row["product_id"],
            "ids": [row["id"]],
            "action": "request_changes",
            "message": "需要重试",
        },
    )
    assert changed.status_code == 200, changed.text
    retried_receipt = exchange(owner, code)
    assert retried_receipt["items"][0]["status"] == "needs_retry"
    result = redeem(
        owner,
        retried_receipt,
        {"card_id": item["card_id"], "params": {"email": "new@example.test"}},
    )
    assert result["results"][0]["status"] == "submitted"
    assert result["results"][0]["job"]["id"] == row["id"]
    assert result["results"][0]["job"]["attempt"] == 2


def test_invalid_parameters_do_not_undo_good_submission(owner, setup_product):
    pid, first = setup_product()
    receipt = exchange(owner, first, issue(owner, pid)[0])
    a, b = receipt["items"]
    result = redeem(
        owner,
        receipt,
        {"card_id": a["card_id"], "params": {"email": "valid@example.test"}},
        {"card_id": b["card_id"], "params": {"email": "bad-email"}},
    )
    assert [item["status"] for item in result["results"]] == ["submitted", "error"]
    assert result["submission_summary"] == {"total": 2, "succeeded": 1, "failed": 1}
    state = inspect()
    assert len(state["jobs"]) == len(state["events"]) == 1
    assert {row["id"]: row["state"] for row in state["cards"]} == {
        a["card_id"]: "reserved",
        b["card_id"]: "ready",
    }
    retry = redeem(
        owner,
        receipt,
        {"card_id": b["card_id"], "params": {"email": "fixed@example.test"}},
    )
    assert retry["submission_summary"]["succeeded"] == 1
    again = redeem(
        owner,
        receipt,
        {"card_id": a["card_id"], "params": {"email": "valid@example.test"}},
    )
    assert again["results"][0]["status"] == "unchanged"
    assert len(inspect()["events"]) == 2


@pytest.mark.parametrize(
    "malformed",
    [{}, {"params": {}}, {"params": []}, {"params": {"email": 12}}, {"params": None}],
)
def test_each_card_requires_its_own_text_parameters(owner, setup_product, malformed):
    pid, first = setup_product()
    receipt = exchange(owner, first, issue(owner, pid)[0])
    a, b = receipt["items"]
    bad = {"card_id": b["card_id"], **malformed}
    result = redeem(
        owner,
        receipt,
        {"card_id": a["card_id"], "params": {"email": "valid@example.test"}},
        bad,
    )
    assert [item["status"] for item in result["results"]] == ["submitted", "error"]
    assert result["results"][1]["http_status"] == 400
    assert len(inspect()["jobs"]) == 1


def test_duplicate_and_foreign_card_cannot_gain_batch_authority(owner, setup_product):
    pid, code = setup_product()
    foreign = card_id(issue(owner, pid)[0])
    receipt = exchange(owner, code)
    owned = receipt["items"][0]["card_id"]
    item = {"card_id": owned, "params": {"email": "valid@example.test"}}
    result = redeem(
        owner,
        receipt,
        item,
        item,
        {"card_id": foreign, "params": {"email": "x@example.test"}},
    )
    assert [entry["status"] for entry in result["results"]] == [
        "submitted",
        "duplicate",
        "error",
    ]
    assert result["results"][2]["http_status"] == 404
    assert "card_id" not in result["results"][2]
    assert len(inspect()["jobs"]) == len(inspect()["events"]) == 1


def test_card_file_ownership_is_enforced_per_item(owner):
    pid = create(
        owner,
        parameters=[{"key": "source", "label": {"zh-CN": "文件"}, "type": "file"}],
    )
    receipt = exchange(owner, *issue(owner, pid, 2))
    a, b = receipt["items"]
    upload = owner.post(
        "/api/files/upload",
        data={
            "token": receipt["token"],
            "card_id": a["card_id"],
            "field_key": "source",
        },
        files={"file": ("brief.txt", b"test input", "text/plain")},
    )
    assert upload.status_code == 200, upload.text
    file_id = upload.json()["id"]
    result = redeem(
        owner,
        receipt,
        {"card_id": a["card_id"], "params": {"source": file_id}},
        {"card_id": b["card_id"], "params": {"source": file_id}},
    )
    assert [item["status"] for item in result["results"]] == ["submitted", "error"]
    state = inspect()
    assert len(state["jobs"]) == 1 and state["job_files"][0]["bound"] == 1
    assert state["job_files"][0]["card_id"] == a["card_id"]


def test_exception_after_mutation_rolls_back_only_that_card_without_secret_logs(
    owner, setup_product, monkeypatch, caplog
):
    pid, code = setup_product()
    receipt = exchange(owner, code, issue(owner, pid)[0])
    a, b = receipt["items"]
    original = service.submit
    leaked = "SYNTHETIC-PRIVATE-CODE-AND-CREDENTIAL"

    def injected(c, card, params):
        row = original(c, card, params)
        if card["id"] == a["card_id"]:
            raise RuntimeError(leaked)
        return row

    monkeypatch.setattr(service, "submit", injected)
    result = redeem(
        owner,
        receipt,
        {"card_id": a["card_id"], "params": {"email": "first@example.test"}},
        {"card_id": b["card_id"], "params": {"email": "second@example.test"}},
    )
    assert [item["status"] for item in result["results"]] == ["error", "submitted"]
    assert result["results"][0]["http_status"] == 500
    assert leaked not in json.dumps(result) and leaked not in caplog.text
    state = inspect()
    assert len(state["jobs"]) == len(state["events"]) == 1
    assert state["jobs"][0]["card_id"] == b["card_id"]
    assert {row["id"]: row["state"] for row in state["cards"]}[a["card_id"]] == "ready"


def test_file_binding_is_rolled_back_with_failed_card_submission(owner, monkeypatch):
    pid = create(
        owner,
        parameters=[{"key": "source", "label": {"zh-CN": "文件"}, "type": "file"}],
    )
    receipt = exchange(owner, *issue(owner, pid, 2))
    a, b = receipt["items"]
    files = []
    for item in (a, b):
        response = owner.post(
            "/api/files/upload",
            data={
                "token": receipt["token"],
                "card_id": item["card_id"],
                "field_key": "source",
            },
            files={"file": ("source.txt", b"synthetic file", "text/plain")},
        )
        assert response.status_code == 200
        files.append(response.json()["id"])
    original = service.submit

    def injected(c, card, params):
        row = original(c, card, params)
        if card["id"] == a["card_id"]:
            raise ValueError("failure after binding input file")
        return row

    monkeypatch.setattr(service, "submit", injected)
    result = redeem(
        owner,
        receipt,
        {"card_id": a["card_id"], "params": {"source": files[0]}},
        {"card_id": b["card_id"], "params": {"source": files[1]}},
    )
    assert result["submission_summary"] == {"total": 2, "succeeded": 1, "failed": 1}
    state = inspect()
    by_file = {row["id"]: row for row in state["job_files"]}
    assert by_file[files[0]]["bound"] == 0 and by_file[files[0]]["job_id"] is None
    assert (
        by_file[files[1]]["bound"] == 1
        and by_file[files[1]]["job_id"] == state["jobs"][0]["id"]
    )


def test_imported_suffix_metadata_cannot_return_a_full_code(owner, setup_product):
    _, code = setup_product()
    cid = card_id(code)
    with db() as c:
        c.execute("UPDATE card_meta SET code_suffix=? WHERE card_id=?", (code, cid))
    receipt = exchange(owner, code)
    assert receipt["items"][0]["suffix"] == code.replace("-", "")[-6:]
    assert code not in json.dumps(receipt)


def test_receipt_isolates_revoked_expired_and_render_errors(
    owner, setup_product, monkeypatch
):
    pid, first = setup_product()
    receipt = exchange(owner, first, *issue(owner, pid, 3))
    a, b, broken, good = receipt["items"]
    with db() as c:
        c.execute("UPDATE cards SET state='revoked' WHERE id=?", (a["card_id"],))
        c.execute(
            "UPDATE card_meta SET expires=? WHERE card_id=?",
            (time.time() - 1, b["card_id"]),
        )
    # A per-card SKU render failure is contained independently from the shared
    # product: this models corrupted historical metadata, not a whole service outage.
    from extore import batch_redemption

    original_variant = batch_redemption.card_variant

    def variant(c, card):
        if card["id"] == broken["card_id"]:
            raise ValueError("PRIVATE-CONFIGURATION")
        return original_variant(c, card)

    monkeypatch.setattr(batch_redemption, "card_variant", variant)
    response = owner.post("/api/batch/receipt", json={"token": receipt["token"]})
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["accepted"] for item in items] == [False, False, False, True]
    assert items[3]["card_id"] == good["card_id"]
    assert "PRIVATE-CONFIGURATION" not in response.text
    assert "token" not in response.json()


def test_expired_unknown_and_single_grants_are_not_batch_receipts(owner, setup_product):
    _, code = setup_product()
    legacy = owner.post("/api/exchange", json={"code": code}).json()
    assert (
        owner.post("/api/batch/receipt", json={"token": legacy["token"]}).status_code
        == 404
    )
    assert (
        owner.post("/api/receipt", json={"token": legacy["token"]}).status_code == 200
    )
    receipt = exchange(owner, code)
    with db() as c:
        c.execute(
            "UPDATE receipt_batches SET expires=? WHERE digest=?",
            (time.time() - 1, digest(receipt["token"])),
        )
    for value in (receipt["token"], "unknown-token"):
        assert (
            owner.post("/api/batch/receipt", json={"token": value}).status_code == 404
        )
        assert (
            owner.post(
                "/api/batch/redeem",
                json={
                    "token": value,
                    "items": [{"card_id": card_id(code), "params": {}}],
                },
            ).status_code
            == 404
        )
    fresh = exchange(owner, code)
    assert fresh["token"]
    with db() as c:
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_shop_disabled_after_exchange_is_a_per_card_failure(owner, setup_product):
    _, first = setup_product()
    pid = create(owner)
    other = issue(owner, pid)[0]
    sid = str(uuid.uuid4())
    with db() as c:
        c.execute(
            "INSERT INTO shops(id,name,email,created,verified) VALUES (?,?,?,?,1)",
            (sid, "另一店", "disabled@example.test", time.time()),
        )
        c.execute("UPDATE products SET shop_id=? WHERE id=?", (sid, pid))
    receipt = exchange(owner, first, other)
    a, b = receipt["items"]
    with db() as c:
        c.execute("UPDATE shops SET enabled=0 WHERE id=?", (sid,))
    result = redeem(
        owner,
        receipt,
        {"card_id": a["card_id"], "params": {"email": "valid@example.test"}},
        {"card_id": b["card_id"], "params": {}},
    )
    assert [item["status"] for item in result["results"]] == ["submitted", "error"]
    assert result["items"][1]["accepted"] is False
    assert len(inspect()["jobs"]) == 1


def test_disabling_shop_preserves_accepted_task_read_only(owner, setup_product):
    pid, code = setup_product()
    receipt = exchange(owner, code)
    cid = receipt["items"][0]["card_id"]
    params = {"email": "original@example.test"}
    launched = redeem(owner, receipt, {"card_id": cid, "params": params})
    jid = launched["results"][0]["job"]["id"]
    with db() as c:
        c.execute(
            "UPDATE shops SET enabled=0 WHERE id=(SELECT shop_id FROM products WHERE id=?)",
            (pid,),
        )
    response = owner.post("/api/batch/receipt", json={"token": receipt["token"]})
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["accepted"] is True and item["job"]["id"] == jid
    result = redeem(owner, receipt, {"card_id": cid, "params": params})
    assert result["results"][0]["status"] == "error"
    assert result["results"][0]["http_status"] == 404
    assert result["items"][0]["job"]["id"] == jid
    assert len(inspect()["jobs"]) == len(inspect()["events"]) == 1

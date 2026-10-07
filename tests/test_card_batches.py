"""Folders are tenant-scoped; clearing stock cannot remove sold fulfillment."""

import json
import time
import uuid

import pytest
import test_tenant_card_tracking
from test_card_outcomes import outcome
from test_card_tracking import card_id
from test_product_links import create_link, login_link
from test_redemption import finish, redeem
from test_text_cards import stock_product

from extore.db import db

tenant_cards_fixture = test_tenant_card_tracking.tenant_cards


def folders(owner, pid, prefix="admin", **params):
    response = owner.get(
        f"/api/{prefix}/card-batches", params={"product_id": pid, **params}
    )
    assert response.status_code == 200, response.text
    return response.json()


def issued_batch(owner, pid, count=1):
    response = owner.post(
        "/api/admin/cards",
        json={"product_id": pid, "count": count, "label": "秋日补货"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def preview(owner, pid, bid, action="delete", prefix="admin"):
    response = owner.post(
        f"/api/{prefix}/card-batches/{bid}/{action}-preview", params={"product_id": pid}
    )
    assert response.status_code == 200, response.text
    return response.json()


def change(owner, pid, bid, action="delete", revision=None, prefix="admin"):
    revision = revision or preview(owner, pid, bid, action, prefix)["revision"]
    return owner.request(
        "DELETE" if action == "delete" else "POST",
        f"/api/{prefix}/card-batches/{bid}" + ("" if action == "delete" else "/purge"),
        params={"product_id": pid},
        json={"confirmed": True, "revision": revision},
    )


def test_folder_grouping_counts_search_pagination_and_no_plaintext(
    owner, setup_product
):
    pid, first = setup_product()
    result = issued_batch(owner, pid, 3)
    listing = folders(owner, pid)
    assert listing["total"] == 2
    batch = next(item for item in listing["items"] if item["id"] == result["batch_id"])
    assert batch["total"] == batch["remaining"] == batch["available"] == 3
    assert batch["used"] == batch["in_progress"] == 0
    assert batch["states"]["unused"] == 3
    assert batch["deleted"] is False
    assert folders(owner, pid, search="秋日")["total"] == 1
    cid = card_id(result["codes"][0])
    assert folders(owner, pid, search=cid)["items"][0]["id"] == result["batch_id"]
    suffix = result["codes"][0].replace("-", "")[-6:]
    assert folders(owner, pid, search=suffix)["items"][0]["id"] == result["batch_id"]
    assert len(folders(owner, pid, limit=1, offset=1)["items"]) == 1
    assert folders(owner, pid, search="%' OR 1=1--")["items"] == []
    serialized = json.dumps(listing)
    for code in (first, *result["codes"]):
        assert code not in serialized and code.replace("-", "") not in serialized
    assert "digest" not in serialized and "content" not in serialized


def test_legacy_cards_use_stable_folder_and_can_archive_restore(owner, setup_product):
    pid, code = setup_product()
    cid = card_id(code)
    with db() as c:
        c.execute("DELETE FROM card_meta WHERE card_id=?", (cid,))
    assert folders(owner, pid)["items"][0]["id"] == "legacy"
    assert folders(owner, pid)["items"][0]["id"] == "legacy"
    inventory = owner.get(
        "/api/admin/card-inventory", params={"product_id": pid, "batch_id": "legacy"}
    ).json()
    assert [row["id"] for row in inventory["items"]] == [card_id(code)]
    deleted = change(owner, pid, "legacy")
    assert deleted.status_code == 200, deleted.text
    bid = deleted.json()["batch_id"]
    assert bid != "legacy"
    assert folders(owner, pid)["items"] == []
    assert folders(owner, pid, view="deleted")["items"][0]["id"] == bid
    restored = owner.post(
        f"/api/admin/card-batches/{bid}/restore", params={"product_id": pid}
    )
    assert restored.status_code == 200
    assert folders(owner, pid)["items"][0]["id"] == bid
    assert owner.post("/api/exchange", json={"code": code}).status_code == 200


def test_archive_stops_unstarted_retry_but_keeps_accepted_tasks_and_delivery(
    owner, setup_product
):
    pid, _ = setup_product()
    result = issued_batch(owner, pid, 4)
    queued_token, queued = redeem(owner, result["codes"][0])
    delivered_token, delivered = redeem(owner, result["codes"][1])
    finish(owner, delivered, "ACCEPTED PRIVATE DELIVERY")
    retry_token, retry = redeem(owner, result["codes"][2])
    outcome(owner, retry, "request_changes", "Please retry")
    plan = preview(owner, pid, result["batch_id"])
    assert plan["revocable_count"] == 2 and plan["in_progress"] == 1
    assert plan["delete_count"] == 0 and plan["retain_count"] == 4
    response = change(owner, pid, result["batch_id"], revision=plan["revision"])
    assert response.status_code == 200, response.text
    assert response.json()["revoked"] == 2
    assert all(
        item["id"] != result["batch_id"] for item in folders(owner, pid)["items"]
    )
    assert (
        owner.post("/api/exchange", json={"code": result["codes"][3]}).status_code
        == 404
    )
    assert (
        owner.post(
            "/api/redeem",
            json={"token": retry_token, "params": {"email": "x@example.com"}},
        ).status_code
        == 410
    )
    assert (
        owner.post("/api/receipt", json={"token": queued_token}).json()["job"]["id"]
        == queued["id"]
    )
    assert (
        owner.post("/api/receipt/reveal", json={"token": delivered_token}).json()[
            "content"
        ]
        == "ACCEPTED PRIVATE DELIVERY"
    )
    restored = owner.post(
        f"/api/admin/card-batches/{result['batch_id']}/restore",
        params={"product_id": pid},
    )
    assert restored.status_code == 200
    assert (
        owner.post("/api/exchange", json={"code": result["codes"][3]}).status_code
        == 200
    )


def test_purge_releases_no_job_files_and_keeps_delivered_card_hidden(
    owner, setup_product
):
    pid, _ = setup_product()
    result = issued_batch(owner, pid, 2)
    delivered_token, delivered = redeem(owner, result["codes"][0])
    finish(owner, delivered, "KEEP THIS DELIVERY")
    unused_id, delivered_id = (card_id(code) for code in result["codes"][::-1])
    with db() as c:
        c.execute(
            "INSERT INTO job_files(id,card_id,product_id,field_key,kind,filename,content_type,size,content,created) VALUES (?,?,?,'source','input','draft.txt','text/plain',5,?,?)",
            (str(uuid.uuid4()), unused_id, pid, b"draft", time.time()),
        )
    assert change(owner, pid, result["batch_id"]).status_code == 200
    plan = preview(owner, pid, result["batch_id"], "purge")
    assert plan["delete_count"] == plan["retain_count"] == 1
    assert (
        change(owner, pid, result["batch_id"], "purge", plan["revision"]).status_code
        == 200
    )
    with db() as c:
        assert (
            c.execute("SELECT 1 FROM cards WHERE id=?", (unused_id,)).fetchone() is None
        )
        assert (
            c.execute(
                "SELECT 1 FROM job_files WHERE card_id=?", (unused_id,)
            ).fetchone()
            is None
        )
        assert c.execute("SELECT 1 FROM cards WHERE id=?", (delivered_id,)).fetchone()
        meta = c.execute(
            "SELECT batch_id,batch_deleted_at FROM card_meta WHERE card_id=?",
            (delivered_id,),
        ).fetchone()
        assert meta["batch_id"] is None and meta["batch_deleted_at"] is not None
        assert c.execute("SELECT 1 FROM jobs WHERE id=?", (delivered["id"],)).fetchone()
        actions = {
            row[0]
            for row in c.execute(
                "SELECT action FROM audit WHERE target=?", (result["batch_id"],)
            )
        }
        assert actions == {"cards.batch.delete", "cards.batch.purge"}
    assert folders(owner, pid, view="deleted")["items"] == []
    assert all(item["id"] != "legacy" for item in folders(owner, pid)["items"])
    assert (
        owner.post("/api/receipt/reveal", json={"token": delivered_token}).json()[
            "content"
        ]
        == "KEEP THIS DELIVERY"
    )


def test_purge_releases_unsold_text_stock_and_grants(owner):
    pid = stock_product(owner)
    result = owner.post(
        "/api/admin/cards/import-text",
        json={"product_id": pid, "text": "PRIVATE STOCK A\nPRIVATE STOCK B"},
    ).json()
    assert (
        owner.post("/api/exchange", json={"code": result["codes"][0]}).status_code
        == 200
    )
    assert change(owner, pid, result["batch_id"]).status_code == 200
    assert change(owner, pid, result["batch_id"], "purge").status_code == 200
    with db() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM text_card_payloads WHERE product_id=?", (pid,)
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT COUNT(*) FROM cards WHERE product_id=?", (pid,)
            ).fetchone()[0]
            == 0
        )
        assert c.execute("SELECT COUNT(*) FROM grants").fetchone()[0] == 0


def test_preview_revision_detects_concurrent_redemption_and_confirmation_is_strict(
    owner, setup_product
):
    pid, _ = setup_product()
    result = issued_batch(owner, pid)
    plan = preview(owner, pid, result["batch_id"])
    redeem(owner, result["codes"][0])
    assert (
        change(owner, pid, result["batch_id"], revision=plan["revision"]).status_code
        == 409
    )
    for confirmation in (False, 1, "true", None):
        response = owner.request(
            "DELETE",
            f"/api/admin/card-batches/{result['batch_id']}",
            params={"product_id": pid},
            json={"revision": plan["revision"], "confirmed": confirmation},
        )
        assert response.status_code == 422
    assert preview(owner, pid, result["batch_id"])["in_progress"] == 1
    assert (
        owner.post(
            f"/api/admin/card-batches/{result['batch_id']}/purge-preview",
            params={"product_id": pid},
        ).status_code
        == 409
    )


@pytest.mark.parametrize("prefix", ("admin", "manage"))
def test_batches_cannot_cross_shop_or_selected_product(tenant_cards_fixture, prefix):
    tenant_cards = tenant_cards_fixture
    a, pid, foreign = tenant_cards["a"], tenant_cards["a_pid"], tenant_cards["b_batch"]
    listing = folders(a, pid, prefix)
    assert sum(item["total"] for item in listing["items"]) == 3
    response = a.post(
        f"/api/{prefix}/card-batches/{foreign}/delete-preview",
        params={"product_id": pid},
    )
    assert response.status_code == 404
    response = a.post(
        f"/api/{prefix}/card-batches/{foreign}/delete-preview",
        params={"product_id": tenant_cards["b_pid"]},
    )
    assert response.status_code == 403
    assert "乙店" not in json.dumps(listing, ensure_ascii=False)
    with db() as c:
        assert (
            c.execute(
                "SELECT deleted_at FROM card_batches WHERE id=?", (foreign,)
            ).fetchone()[0]
            is None
        )


def test_staff_requires_cards_permission_and_cannot_use_admin_route(
    owner, setup_product
):
    pid, _ = setup_product()
    link = create_link(owner, pid, ["queue.view"])
    login_link(owner, link)
    assert owner.get("/api/manage/card-batches").status_code == 403
    assert (
        owner.get("/api/admin/card-batches", params={"product_id": pid}).status_code
        == 401
    )


def test_staff_batch_delete_stays_in_authorized_product(owner, setup_product):
    pid, _ = setup_product()
    other_pid, other_code = setup_product()
    result = issued_batch(owner, pid)
    link = create_link(owner, pid, ["cards.manage"])
    login_link(owner, link)
    assert folders(owner, pid, "manage")["total"] == 2
    assert change(owner, pid, result["batch_id"], prefix="manage").status_code == 200
    assert (
        owner.get(
            "/api/manage/card-batches", params={"product_id": other_pid}
        ).status_code
        == 403
    )
    assert owner.post("/api/exchange", json={"code": other_code}).status_code == 200


def test_purge_revision_rejects_new_draft_attachment(owner, setup_product):
    pid, code = setup_product()
    cid = card_id(code)
    bid = folders(owner, pid)["items"][0]["id"]
    assert change(owner, pid, bid).status_code == 200
    plan = preview(owner, pid, bid, "purge")
    with db() as c:
        c.execute(
            "INSERT INTO job_files(id,card_id,product_id,field_key,kind,filename,content_type,size,content,created) VALUES (?,?,?,'source','input','late.txt','text/plain',4,?,?)",
            (str(uuid.uuid4()), cid, pid, b"late", time.time()),
        )
    assert change(owner, pid, bid, "purge", plan["revision"]).status_code == 409
    with db() as c:
        assert (
            c.execute(
                "SELECT content FROM job_files WHERE card_id=?", (cid,)
            ).fetchone()[0]
            == b"late"
        )


def test_restore_does_not_restore_preexisting_manual_revocation(owner, setup_product):
    pid, _ = setup_product()
    result = issued_batch(owner, pid, 2)
    cid = card_id(result["codes"][0])
    assert owner.post(f"/api/admin/cards/{cid}/revoke").status_code == 200
    assert change(owner, pid, result["batch_id"]).status_code == 200
    assert (
        owner.post(
            f"/api/admin/card-batches/{result['batch_id']}/restore",
            params={"product_id": pid},
        ).status_code
        == 200
    )
    assert (
        owner.post("/api/exchange", json={"code": result["codes"][0]}).status_code
        == 404
    )
    assert (
        owner.post("/api/exchange", json={"code": result["codes"][1]}).status_code
        == 200
    )


def test_inconsistent_foreign_card_link_blocks_batch_mutation(owner, setup_product):
    pid, _ = setup_product()
    _, foreign = setup_product()
    bid = folders(owner, pid)["items"][0]["id"]
    foreign_id = card_id(foreign)
    with db() as c:
        c.execute("UPDATE card_meta SET batch_id=? WHERE card_id=?", (bid, foreign_id))
    response = owner.post(
        f"/api/admin/card-batches/{bid}/delete-preview", params={"product_id": pid}
    )
    assert response.status_code == 409
    with db() as c:
        assert (
            c.execute("SELECT state FROM cards WHERE id=?", (foreign_id,)).fetchone()[0]
            == "ready"
        )

"""Retained editions keep exact file authority without escaping storage limits."""

import json
import time
import uuid
from contextlib import ExitStack

import pytest
from test_card_entitlements import batch, receipt, revision
from test_files import download, prepare
from test_tenant_files import merchant_client

from extore import files, storage, worker
from extore.card_entitlements import allocated_bytes
from extore.db import db


def launch(owner, credits=3, **values):
    return prepare(
        owner,
        revision_policy={
            "attribute_key": "custom_edits",
            "label": {"en": "Edit credits"},
        },
        variants=[
            {
                "id": "default",
                "name": "Editable document",
                "attributes": {"custom_edits": credits},
            }
        ],
        **values,
    )


def upload(owner, row, content=b"edition", **data):
    return owner.post(
        "/api/manage/files/upload",
        data={
            "job_id": row["id"],
            "field_key": "result",
            "attempt": str(row["attempt"]),
            **data,
        },
        files={"file": ("paper.docx", content, "application/octet-stream")},
    )


def deliver(owner, row, content=b"edition"):
    assert batch(owner, row, "claim").status_code == 200
    response = upload(owner, row, content)
    assert response.status_code == 200, response.text
    output = response.json()
    response = batch(owner, row, "succeed", output={"result": output["id"]})
    assert response.status_code == 200, response.text
    return output


@pytest.mark.parametrize("already_revealed", [False, True])
def test_old_file_remains_readable_during_and_after_revision(owner, already_revealed):
    _, token, first, _ = launch(owner)
    old_file = deliver(owner, first, b"original-docx-bytes")
    if already_revealed:
        assert (
            owner.post("/api/receipt/reveal", json={"token": token}).status_code == 200
        )
    second = revision(owner, token).json()
    inventory = owner.get(
        "/api/manage/card-stats", params={"product_id": first["product_id"]}
    ).json()["summary"]
    assert inventory["viewed"] == int(already_revealed)
    assert inventory["used"] == 1 and inventory["remaining"] == 0
    if not already_revealed:
        assert download(owner, token, old_file["id"]).status_code == 409
        assert (
            owner.post("/api/receipt/reveal", json={"token": token}).json()["revision"]
            == 0
        )
    assert download(owner, token, old_file["id"]).content == b"original-docx-bytes"
    inventory = owner.get(
        "/api/manage/card-stats", params={"product_id": first["product_id"]}
    ).json()["summary"]
    assert inventory["viewed"] == 1
    new_file = deliver(owner, second, b"revised-docx-bytes")
    assert download(owner, token, new_file["id"]).status_code == 409
    assert (
        owner.post("/api/receipt/reveal", json={"token": token}).json()["revision"] == 1
    )
    assert download(owner, token, new_file["id"]).content == b"revised-docx-bytes"
    assert download(owner, token, old_file["id"]).content == b"original-docx-bytes"
    assert (
        owner.post(
            "/api/files/download",
            json={"token": token, "file_id": old_file["id"], "revision": 1},
        ).status_code
        == 409
    )
    historic = owner.post("/api/receipt/reveal", json={"token": token, "revision": 0})
    assert historic.json()["output"]["result"] == old_file["id"]
    assert [
        item["revision"] for item in receipt(owner, token)["job"]["deliveries"]
    ] == [0, 1]


def test_failed_or_returned_revision_keeps_old_output_but_discards_its_draft(owner):
    _, token, first, _ = launch(owner, max_attempts=2)
    old_file = deliver(owner, first, b"old-good-document")
    assert owner.post("/api/receipt/reveal", json={"token": token}).status_code == 200
    row = revision(owner, token).json()
    assert batch(owner, row, "claim").status_code == 200
    draft = upload(owner, row, b"not-a-delivery").json()
    assert batch(owner, row, "fail", retryable=True).status_code == 200
    # Supply the real, unchanged server input instead of a customer-provided
    # allowance or invented file identifier.
    with db() as c:
        params = json.loads(
            c.execute("SELECT params FROM jobs WHERE id=?", (row["id"],)).fetchone()[0]
        )
    response = owner.post("/api/redeem", json={"token": token, "params": params})
    assert response.status_code == 200, response.text
    retried = response.json()
    assert download(owner, token, old_file["id"]).content == b"old-good-document"
    assert download(owner, token, draft["id"]).status_code == 404
    assert batch(owner, retried, "claim").status_code == 200
    another = upload(owner, retried, b"also-not-a-delivery").json()
    assert (
        batch(
            owner,
            retried,
            "request_retry",
            message="External dependency is temporarily unavailable.",
            reason_type="external",
            retry_mode="reuse",
        ).status_code
        == 200
    )
    assert download(owner, token, old_file["id"]).content == b"old-good-document"
    assert download(owner, token, another["id"]).status_code == 404
    assert receipt(owner, token)["entitlements"]["used"] == 1


def test_rejected_revision_retains_successful_file_but_never_permits_more_work(owner):
    _, token, first, _ = launch(owner)
    old_file = deliver(owner, first, b"legitimately-delivered")
    second = revision(owner, token).json()
    assert batch(owner, second, "claim").status_code == 200
    assert (
        batch(
            owner,
            second,
            "reject",
            message="The revision is outside the allowed scope.",
        ).status_code
        == 200
    )
    assert owner.post("/api/receipt/reveal", json={"token": token}).status_code == 200
    assert download(owner, token, old_file["id"]).content == b"legitimately-delivered"
    assert revision(owner, token, current=1).status_code == 410


def test_historical_file_cannot_cross_card_product_or_shop(owner):
    _, token, first, _ = launch(owner)
    output = deliver(owner, first, b"tenant-private-output")
    revision(owner, token)
    assert owner.post("/api/receipt/reveal", json={"token": token}).status_code == 200
    _, other_token, _, _ = launch(owner)
    assert download(owner, other_token, output["id"]).status_code == 403
    assert (
        owner.post(
            "/api/receipt/revisions",
            json={
                "token": other_token,
                "card_id": "wrong-card",
                "request_id": str(uuid.uuid4()),
                "expected_revision": 0,
                "message": "Change it",
            },
        ).status_code
        == 404
    )
    with db() as c:
        sid = str(uuid.uuid4())
        c.execute(
            "INSERT INTO shops(id,name,email,verified,created) VALUES (?,?,?,1,?)",
            (sid, "Other merchant", sid + "@example.test", time.time()),
        )
    with ExitStack() as stack:
        other = merchant_client(stack, sid)
        assert (
            other.get(f"/api/manage/files/{output['id']}/download").status_code == 403
        )
        assert (
            other.get(
                "/api/manage/jobs", params={"product_id": first["product_id"]}
            ).status_code
            == 403
        )


def test_upload_attempt_cannot_be_omitted_or_upgraded_to_the_current_revision(owner):
    _, token, first, _ = launch(owner)
    deliver(owner, first)
    row = revision(owner, token).json()
    assert batch(owner, row, "claim").status_code == 200
    before = owner.get("/api/manage/files", params={"job_id": row["id"]}).json()
    data = {"job_id": row["id"], "field_key": "result"}
    for extra in ({}, {"attempt": str(first["attempt"])}):
        response = owner.post(
            "/api/manage/files/upload",
            data={**data, **extra},
            files={"file": ("stale.docx", b"stale", "application/octet-stream")},
        )
        assert response.status_code == 409, response.text
    after = owner.get("/api/manage/files", params={"job_id": row["id"]}).json()
    assert after == before
    assert upload(owner, row).status_code == 200


def test_historical_files_and_text_share_card_shop_and_global_quotas(
    owner, monkeypatch
):
    _, token, first, _ = launch(owner)
    old = deliver(owner, first, b"x" * 20)
    row = revision(owner, token, message="An edit").json()
    assert batch(owner, row, "claim").status_code == 200
    with db() as c:
        text_bytes = allocated_bytes(c)
        file_bytes = c.execute("SELECT SUM(size) FROM job_files").fetchone()[0]
        usage = storage.storage_usage(c)
    assert text_bytes > 0 and usage["stored_bytes"] == text_bytes + file_bytes
    monkeypatch.setattr(files, "MAX_CARD_BYTES", text_bytes + file_bytes + 3)
    assert upload(owner, row, b"four").status_code == 413
    monkeypatch.setattr(files, "MAX_CARD_BYTES", 1000000)
    monkeypatch.setattr(storage, "UPLOAD_TOTAL_BYTES", text_bytes + file_bytes + 3)
    assert upload(owner, row, b"four").status_code == 507
    assert (
        owner.post(
            "/api/receipt/reveal", json={"token": token, "revision": 0}
        ).status_code
        == 200
    )
    assert download(owner, token, old["id"]).content == b"x" * 20


def test_destroy_mid_revision_clears_every_file_body_and_suggestion(owner):
    _, token, first, _ = launch(owner)
    output = deliver(owner, first)
    row = revision(owner, token, message="Private change request").json()
    assert batch(owner, row, "claim").status_code == 200
    upload(owner, row, b"draft")
    assert owner.post("/api/receipt/destroy", json={"token": token}).status_code == 200
    with db() as c:
        assert c.execute("SELECT COUNT(*) FROM job_files").fetchone()[0] == 0
        assert allocated_bytes(c) == 0
    assert download(owner, token, output["id"]).status_code == 404
    assert upload(owner, row).status_code == 409


def test_destroyed_revision_cannot_be_dispatched_after_dns_resolution(owner):
    _, token, first, _ = launch(owner)
    deliver(owner, first)
    row = revision(owner, token).json()
    with db() as c:
        event = c.execute(
            "SELECT id,payload FROM events WHERE type='revision.requested'"
        ).fetchone()
    dispatch = {
        "id": event["id"],
        "payload": event["payload"],
        "url": "https://worker.example.test/hook",
        "secret": "s" * 32,
    }
    worker._check_event_authority(dispatch)
    assert owner.post("/api/receipt/destroy", json={"token": token}).status_code == 200
    with pytest.raises(ValueError, match="失效"):
        worker._check_event_authority(dispatch)
    assert receipt(owner, token)["job"]["attempt"] > row["attempt"]

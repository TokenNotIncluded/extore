"""File IDs and the shared owner claimant must never cross shop boundaries."""

import time
import uuid
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response

from extore import files, storage
from extore.app import app
from extore.config import ORIGIN
from extore.db import db
from extore.models import Product
from extore.security import create_session, digest, token


def identifier():
    return str(uuid.uuid4())


def domain(c, name):
    now = time.time()
    sid, pid, cid, jid = (identifier() for _ in range(4))
    receipt = token()
    c.execute(
        "INSERT INTO shops(id,name,email,verified,created) VALUES (?,?,?,1,?)",
        (sid, name, f"{sid}@example.com", now),
    )
    config = Product(
        name=name,
        parameters=[{"key": "source", "label": {"zh-CN": "需求"}, "type": "file"}],
        outputs=[{"key": "result", "label": {"zh-CN": "交付"}, "type": "file"}],
    )
    c.execute(
        "INSERT INTO products(id,config,created,shop_id) VALUES (?,?,?,?)",
        (pid, config.model_dump_json(), now, sid),
    )
    c.execute(
        "INSERT INTO cards(id,digest,product_id,state,created) VALUES (?,?,?,'reserved',?)",
        (cid, digest(token()), pid, now),
    )
    c.execute(
        "INSERT INTO grants(digest,card_id,expires) VALUES (?,?,?)",
        (digest(receipt), cid, now + 3600),
    )
    c.execute(
        "INSERT INTO jobs(id,card_id,product_id,state,params,claimed_by,created,updated) "
        "VALUES (?,?,?,'processing','{}',?,?,?)",
        (jid, cid, pid, f"shop:{sid}", now, now),
    )
    stored = []
    for kind, key in (("input", "source"), ("output", "result")):
        fid = identifier()
        content = f"{name}-{kind}-private".encode()
        c.execute(
            "INSERT INTO job_files(id,card_id,product_id,job_id,field_key,kind,filename,"
            "content_type,size,content,bound,created) VALUES (?,?,?,?,?,?,?,?,?,?,1,?)",
            (
                fid,
                cid,
                pid,
                jid,
                key,
                kind,
                key + ".txt",
                "text/plain",
                len(content),
                content,
                now,
            ),
        )
        stored.append(fid)
    return {
        "shop_id": sid,
        "product_id": pid,
        "card_id": cid,
        "job_id": jid,
        "receipt": receipt,
        "source_id": stored[0],
        "output_id": stored[1],
    }


def merchant_client(stack, shop_id):
    client = stack.enter_context(
        TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
    )
    with db() as c:
        value = create_session(
            c, Response(), "admin", shop_id=shop_id, auth_method="passkey"
        )
    client.cookies.set("extore_session", value)
    return client


@pytest.fixture
def tenants():
    with db() as c:
        a, b = domain(c, "店铺甲"), domain(c, "店铺乙")
    with ExitStack() as stack:
        yield (
            a,
            b,
            merchant_client(stack, a["shop_id"]),
            merchant_client(stack, b["shop_id"]),
        )


def watch_blob_reads(monkeypatch):
    reads = []
    original = files._file

    def observed(c, fid, *, content=False):
        if content:
            reads.append(fid)
        return original(c, fid, content=content)

    monkeypatch.setattr(files, "_file", observed)
    return reads


def test_shop_owners_list_and_download_only_their_files(tenants, monkeypatch):
    a, b, client_a, client_b = tenants
    reads = watch_blob_reads(monkeypatch)
    for own, foreign, client in ((a, b, client_a), (b, a, client_b)):
        listed = client.get("/api/manage/files", params={"job_id": own["job_id"]})
        assert listed.status_code == 200, listed.text
        assert {item["id"] for item in listed.json()} == {
            own["source_id"],
            own["output_id"],
        }
        denied = client.get("/api/manage/files", params={"job_id": foreign["job_id"]})
        assert denied.status_code == 403, denied.text
        before = list(reads)
        response = client.get(f"/api/manage/files/{foreign['output_id']}/download")
        assert response.status_code == 403, response.text
        assert reads == before
        response = client.get(f"/api/manage/files/{own['output_id']}/download")
        assert response.status_code == 200, response.text
        assert b"private" in response.content
    assert reads == [a["output_id"], b["output_id"]]


def test_shared_owner_claimant_does_not_allow_cross_shop_output_upload(tenants):
    a, b, client_a, client_b = tenants
    with db() as c:
        before = c.execute("SELECT COUNT(*) FROM job_files").fetchone()[0]
        # Even an overlapping or stale claimant cannot expand the shop scope.
        c.execute(
            "UPDATE jobs SET claimed_by=? WHERE id=?",
            (f"shop:{a['shop_id']}", b["job_id"]),
        )
    denied = client_a.post(
        "/api/manage/files/upload",
        data={"job_id": b["job_id"], "field_key": "result"},
        files={"file": ("foreign.txt", b"should-not-exist", "text/plain")},
    )
    assert denied.status_code == 403, denied.text
    with db() as c:
        assert c.execute("SELECT COUNT(*) FROM job_files").fetchone()[0] == before
        assert c.execute("SELECT COUNT(*) FROM upload_reservations").fetchone()[0] == 0
        c.execute(
            "UPDATE jobs SET claimed_by=? WHERE id=?",
            (f"shop:{b['shop_id']}", b["job_id"]),
        )
    for own, client in ((a, client_a), (b, client_b)):
        uploaded = client.post(
            "/api/manage/files/upload",
            data={"job_id": own["job_id"], "field_key": "result"},
            files={"file": ("allowed.txt", b"shop-owned", "text/plain")},
        )
        assert uploaded.status_code == 200, uploaded.text
        assert uploaded.json()["job_id"] == own["job_id"]


def test_cross_shop_upload_is_denied_before_payload_reservation(tenants, monkeypatch):
    _, b, client_a, _ = tenants
    growth = []
    original = storage.UploadReservation.grow

    def observed(self, size):
        growth.append(size)
        return original(self, size)

    monkeypatch.setattr(storage.UploadReservation, "grow", observed)
    denied = client_a.post(
        "/api/manage/files/upload",
        data={"job_id": b["job_id"], "field_key": "result"},
        files={"file": ("foreign.bin", b"x" * (files.CHUNK_BYTES * 3))},
    )
    assert denied.status_code == 403, denied.text
    assert growth == []


def test_file_first_upload_cannot_bypass_shop_reservation(tenants, client, monkeypatch):
    a, _, _, _ = tenants
    growth = []
    original = storage.UploadReservation.grow

    def observed(self, size):
        growth.append(size)
        return original(self, size)

    monkeypatch.setattr(storage.UploadReservation, "grow", observed)
    boundary = "file-before-shop-identity"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="wrong.bin"\r\n\r\n'
        + "x" * (files.CHUNK_BYTES * 3)
        + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="token"\r\n\r\n{a["receipt"]}\r\n'
        + f'--{boundary}\r\nContent-Disposition: form-data; name="field_key"\r\n\r\nsource\r\n'
        + f"--{boundary}--\r\n"
    ).encode()
    denied = client.post(
        "/api/files/upload",
        content=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert denied.status_code == 400, denied.text
    assert "文件之前" in denied.json()["detail"]
    assert growth == []
    with db() as c:
        assert c.execute("SELECT COUNT(*) FROM upload_reservations").fetchone()[0] == 0


def test_superadmin_can_inspect_files_from_both_shops(tenants, owner):
    a, b, _, _ = tenants
    for record in (a, b):
        assert (
            owner.get(
                "/api/manage/files", params={"job_id": record["job_id"]}
            ).status_code
            == 200
        )
        response = owner.get(f"/api/manage/files/{record['source_id']}/download")
        assert response.status_code == 200, response.text


def test_inconsistent_file_ownership_is_rejected_before_reading_blob(
    tenants, monkeypatch
):
    a, b, client_a, _ = tenants
    with db() as c:
        c.execute(
            "UPDATE job_files SET product_id=? WHERE id=?",
            (b["product_id"], a["output_id"]),
        )
    reads = watch_blob_reads(monkeypatch)
    response = client_a.get(f"/api/manage/files/{a['output_id']}/download")
    assert response.status_code == 403, response.text
    assert reads == []
    listed = client_a.get("/api/manage/files", params={"job_id": a["job_id"]})
    assert listed.status_code == 200, listed.text
    assert {item["id"] for item in listed.json()} == {a["source_id"]}


@pytest.mark.parametrize("same_shop", (False, True))
def test_product_management_links_keep_product_scope_within_and_across_shops(
    tenants, same_shop
):
    a, b, client_a, _ = tenants
    with db() as c:
        if same_shop:
            c.execute(
                "UPDATE products SET shop_id=? WHERE id=?",
                (a["shop_id"], b["product_id"]),
            )
        staff_id = identifier()
        c.execute(
            "INSERT INTO staff(id,digest,product_id,name,expires) VALUES (?,?,?,?,?)",
            (
                staff_id,
                digest(token()),
                a["product_id"],
                "文件处理",
                time.time() + 3600,
            ),
        )
        credential = create_session(c, Response(), "staff", staff_id)
    client_a.cookies.set("extore_session", credential)
    allowed = client_a.get("/api/manage/files", params={"job_id": a["job_id"]})
    assert allowed.status_code == 200, allowed.text
    denied = client_a.get("/api/manage/files", params={"job_id": b["job_id"]})
    assert denied.status_code == 403, denied.text
    denied = client_a.get(f"/api/manage/files/{b['source_id']}/download")
    assert denied.status_code == 403, denied.text


def ready_for_download(record):
    with db() as c:
        c.execute(
            "UPDATE jobs SET state='succeeded',revealed=1 WHERE id=?",
            (record["job_id"],),
        )
        c.execute("UPDATE job_files SET released=1 WHERE id=?", (record["output_id"],))


def test_customer_receipt_cannot_download_another_shop_output(
    tenants, client, monkeypatch
):
    a, b, _, _ = tenants
    ready_for_download(a)
    ready_for_download(b)
    reads = watch_blob_reads(monkeypatch)
    denied = client.post(
        "/api/files/download",
        json={"token": a["receipt"], "file_id": b["output_id"]},
    )
    assert denied.status_code == 403, denied.text
    assert reads == []
    allowed = client.post(
        "/api/files/download",
        json={"token": a["receipt"], "file_id": a["output_id"]},
    )
    assert allowed.status_code == 200, allowed.text
    assert reads == [a["output_id"]]


def test_batch_receipt_cannot_select_another_shops_card(tenants, client, monkeypatch):
    a, b, _, _ = tenants
    receipt = token()
    with db() as c:
        c.execute(
            "INSERT INTO receipt_batches(digest,expires,created) VALUES (?,?,?)",
            (digest(receipt), time.time() + 3600, time.time()),
        )
        c.execute(
            "INSERT INTO receipt_batch_cards(digest,card_id,position) VALUES (?,?,0)",
            (digest(receipt), a["card_id"]),
        )
    reads = watch_blob_reads(monkeypatch)
    denied = client.post(
        "/api/files/download",
        json={"token": receipt, "card_id": b["card_id"], "file_id": b["output_id"]},
    )
    assert denied.status_code == 404, denied.text
    assert reads == []
    denied = client.post(
        "/api/files/upload",
        data={"token": receipt, "card_id": b["card_id"], "field_key": "source"},
        files={"file": ("foreign.txt", b"another-store-card", "text/plain")},
    )
    assert denied.status_code == 404, denied.text
    with db() as c:
        assert c.execute("SELECT COUNT(*) FROM upload_reservations").fetchone()[0] == 0


def test_disabled_shop_blocks_new_uploads_but_keeps_paid_deliveries_readable(
    tenants, client
):
    a, _, client_a, _ = tenants
    ready_for_download(a)
    new_receipt = token()
    with db() as c:
        cid = identifier()
        c.execute(
            "INSERT INTO cards(id,digest,product_id,created) VALUES (?,?,?,?)",
            (cid, digest(token()), a["product_id"], time.time()),
        )
        c.execute(
            "INSERT INTO grants(digest,card_id,expires) VALUES (?,?,?)",
            (digest(new_receipt), cid, time.time() + 3600),
        )
        c.execute("UPDATE shops SET enabled=0 WHERE id=?", (a["shop_id"],))
    upload = client.post(
        "/api/files/upload",
        data={"token": new_receipt, "field_key": "source"},
        files={"file": ("blocked.txt", b"disabled-store", "text/plain")},
    )
    assert upload.status_code == 404, upload.text
    assert (
        client_a.get("/api/manage/files", params={"job_id": a["job_id"]}).status_code
        == 401
    )
    download = client.post(
        "/api/files/download",
        json={"token": a["receipt"], "file_id": a["output_id"]},
    )
    assert download.status_code == 200, download.text
    with db() as c:
        assert c.execute("SELECT COUNT(*) FROM upload_reservations").fetchone()[0] == 0
        assert (
            c.execute(
                "SELECT COUNT(*) FROM job_files WHERE card_id=?", (cid,)
            ).fetchone()[0]
            == 0
        )

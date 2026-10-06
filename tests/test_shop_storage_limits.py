"""Per-shop capacity stays atomic with global reservations and BLOB commits."""

import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.responses import Response

from extore import storage
from extore.app import app
from extore.config import DATA, ORIGIN, UPLOAD_SHOP_BYTES
from extore.db import db
from extore.security import create_session


def tenant(owner, name, *, limit=10):
    sid = str(uuid.uuid4())
    with db() as c:
        c.execute(
            "INSERT INTO shops(id,name,email,created,verified,storage_limit_bytes) "
            "VALUES (?,?,?,?,1,?)",
            (sid, name, sid + "@example.com", time.time(), limit),
        )
    response = owner.post(
        "/api/admin/products",
        params={"shop_id": sid},
        json={
            "name": name,
            "parameters": [
                {"key": "source", "label": {"zh-CN": "文件"}, "type": "file"}
            ],
        },
    )
    assert response.status_code == 200, response.text
    return sid, response.json()["id"]


def receipt(owner, pid):
    response = owner.post("/api/admin/cards", json={"product_id": pid, "count": 1})
    assert response.status_code == 200, response.text
    response = owner.post("/api/exchange", json={"code": response.json()["codes"][0]})
    assert response.status_code == 200, response.text
    return response.json()["token"]


def upload(owner, token, content):
    return owner.post(
        "/api/files/upload",
        data={"token": token, "field_key": "source"},
        files={"file": ("source.bin", content, "application/octet-stream")},
    )


def usage(shop=None):
    with db() as c:
        return storage.storage_usage(c, shop_id=shop)


def test_new_shop_default_is_one_gib_without_rewriting_existing_files(owner):
    sid = str(uuid.uuid4())
    with db() as c:
        c.execute(
            "INSERT INTO shops(id,name,created) VALUES (?,?,?)",
            (sid, "默认额度", time.time()),
        )
        row = c.execute(
            "SELECT storage_limit_bytes FROM shops WHERE id=?", (sid,)
        ).fetchone()
    assert row[0] == UPLOAD_SHOP_BYTES == 1024 * 1024 * 1024


def test_shop_at_capacity_cannot_take_another_shops_space(owner):
    shop_a, product_a = tenant(owner, "店铺甲", limit=6)
    shop_b, product_b = tenant(owner, "店铺乙", limit=10)
    token_a, token_b = receipt(owner, product_a), receipt(owner, product_b)
    assert upload(owner, token_a, b"123456").status_code == 200
    denied = upload(owner, token_a, b"x")
    assert denied.status_code == 507 and "店铺" in denied.json()["detail"]
    assert upload(owner, token_b, b"12345").status_code == 200
    assert usage(shop_a)["stored_bytes"] == 6
    assert usage(shop_b)["stored_bytes"] == 5
    assert usage()["stored_bytes"] == 11
    assert usage()["uploading_bytes"] == 0


def test_shop_quotas_do_not_bypass_shared_global_capacity(owner, monkeypatch):
    monkeypatch.setattr(storage, "UPLOAD_TOTAL_BYTES", 10)
    shop_a, product_a = tenant(owner, "店铺甲", limit=10)
    shop_b, product_b = tenant(owner, "店铺乙", limit=10)
    assert upload(owner, receipt(owner, product_a), b"123456").status_code == 200
    token_b = receipt(owner, product_b)
    assert upload(owner, token_b, b"12345").status_code == 507
    assert upload(owner, token_b, b"1234").status_code == 200
    assert usage(shop_a)["stored_bytes"] == 6
    assert usage(shop_b)["stored_bytes"] == 4
    assert usage()["stored_bytes"] == 10


def test_parallel_same_shop_streams_cannot_overshoot_its_capacity(owner, monkeypatch):
    monkeypatch.setattr(storage, "UPLOAD_CONCURRENCY", 8)
    shop, product = tenant(owner, "并发店铺", limit=10)
    started = threading.Barrier(6)
    attempted = threading.Barrier(6)

    def receive():
        with storage.receiving_upload() as reservation:
            with db() as c:
                reservation.bind_product(c, product)
            started.wait(timeout=10)
            try:
                reservation.grow(4)
                result = 200
            except HTTPException as exc:
                result = exc.status_code
            attempted.wait(timeout=10)
            assert usage(shop)["uploading_bytes"] <= 10
            return result

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: receive(), range(6)))
    assert results.count(200) == 2
    assert results.count(507) == 4
    assert usage(shop)["uploading_bytes"] == 0
    assert usage()["active_uploads"] == 0


def test_parallel_tenants_still_serialize_shared_global_reservations(
    owner, monkeypatch
):
    monkeypatch.setattr(storage, "UPLOAD_TOTAL_BYTES", 10)
    monkeypatch.setattr(storage, "UPLOAD_CONCURRENCY", 8)
    shop_a, product_a = tenant(owner, "并发甲", limit=10)
    shop_b, product_b = tenant(owner, "并发乙", limit=10)
    started = threading.Barrier(4)
    attempted = threading.Barrier(4)

    def receive(product):
        with storage.receiving_upload() as reservation:
            with db() as c:
                reservation.bind_product(c, product)
            started.wait(timeout=10)
            try:
                reservation.grow(4)
                result = 200
            except HTTPException as exc:
                result = exc.status_code
            attempted.wait(timeout=10)
            assert usage()["uploading_bytes"] <= 10
            assert (
                usage(shop_a)["uploading_bytes"] + usage(shop_b)["uploading_bytes"]
                <= 10
            )
            return result

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(receive, [product_a, product_a, product_b, product_b]))
    assert results.count(200) == 2
    assert results.count(507) == 2
    assert usage()["uploading_bytes"] == 0


def test_binding_and_final_check_do_not_double_charge_reserved_bytes(owner):
    shop, product = tenant(owner, "精确额度", limit=9)
    assert upload(owner, receipt(owner, product), b"123456").status_code == 200
    with storage.receiving_upload() as reservation:
        with db() as c:
            reservation.bind_product(c, product)
        reservation.grow(3)
        with db() as c:
            reservation.bind_product(c, product)
            storage.check_storage_quota(
                c, 3, reservation_id=reservation.id, product_id=product
            )
            assert storage.storage_usage(c, shop)["stored_bytes"] == 6
            assert storage.storage_usage(c, shop)["uploading_bytes"] == 3
        with pytest.raises(HTTPException) as exc:
            reservation.grow(1)
        assert exc.value.status_code == 507
        assert usage(shop)["uploading_bytes"] == 3
    assert usage(shop)["stored_bytes"] == 6 and usage(shop)["uploading_bytes"] == 0


def test_preexisting_unbound_lease_can_bind_without_miscounting_or_data_loss(owner):
    shop, product = tenant(owner, "旧版上传", limit=8)
    with storage.receiving_upload() as reservation:
        reservation.grow(4)
        with db() as c:
            storage.init_schema(c)
            assert storage.storage_usage(c)["uploading_bytes"] == 4
            assert storage.storage_usage(c, shop)["uploading_bytes"] == 0
            assert (
                storage.prune_uploads(c, now=time.time() + 30 * 86400)[
                    "abandoned_uploads"
                ]
                == 0
            )
            reservation.bind_product(c, product)
            assert storage.storage_usage(c, shop)["uploading_bytes"] == 4
        reservation.grow(4)
        with pytest.raises(HTTPException) as exc:
            reservation.grow(1)
        assert exc.value.status_code == 507
    assert usage(shop)["uploading_bytes"] == 0
    assert not list((DATA / "upload-tmp").glob("*.lock"))


def test_legacy_reservation_schema_migration_preserves_rows_and_nullable_owner():
    with sqlite3.connect(":memory:") as c:
        c.row_factory = sqlite3.Row
        c.execute(
            "CREATE TABLE upload_reservations(id TEXT PRIMARY KEY,size INTEGER NOT NULL,created REAL NOT NULL)"
        )
        identifier = str(uuid.uuid4())
        c.execute(
            "INSERT INTO upload_reservations VALUES (?,?,?)", (identifier, 123, 100)
        )
        storage.init_schema(c)
        storage.init_schema(c)
        row = c.execute(
            "SELECT * FROM upload_reservations WHERE id=?", (identifier,)
        ).fetchone()
        assert dict(row) == {
            "id": identifier,
            "size": 123,
            "created": 100,
            "shop_id": None,
            "product_id": None,
        }


def test_reservation_cannot_rebind_another_product_or_changed_shop(owner):
    shop_a, product_a = tenant(owner, "绑定甲", limit=10)
    shop_b, product_b = tenant(owner, "绑定乙", limit=10)
    with storage.receiving_upload() as reservation:
        with db() as c:
            reservation.bind_product(c, product_a)
        reservation.grow(3)
        with pytest.raises(HTTPException) as exc, db() as c:
            reservation.bind_product(c, product_b)
        assert exc.value.status_code == 409
        with db() as c:
            c.execute("UPDATE products SET shop_id=? WHERE id=?", (shop_b, product_a))
        with pytest.raises(HTTPException) as exc:
            reservation.grow(1)
        assert exc.value.status_code == 409
        assert usage(shop_a)["uploading_bytes"] == 3
        assert usage(shop_b)["uploading_bytes"] == 0
    assert usage()["uploading_bytes"] == 0


def test_scoped_usage_never_discloses_other_shops_or_physical_disk(owner):
    shop_a, product_a = tenant(owner, "私有容量甲", limit=10)
    shop_b, product_b = tenant(owner, "私有容量乙", limit=20)
    assert upload(owner, receipt(owner, product_a), b"123").status_code == 200
    assert upload(owner, receipt(owner, product_b), b"123456").status_code == 200
    with storage.receiving_upload() as reservation:
        with db() as c:
            reservation.bind_product(c, product_b)
        reservation.grow(2)
        own = usage(shop_a)
        assert own == {
            "stored_bytes": 3,
            "uploading_bytes": 0,
            "limit_bytes": 10,
            "active_uploads": 0,
            "upload_concurrency": storage.UPLOAD_CONCURRENCY,
        }
        assert usage(shop_b)["stored_bytes"] == 6
        assert usage(shop_b)["uploading_bytes"] == 2
        assert usage()["uploading_bytes"] == 2
        assert "disk_free_bytes" in usage()


def test_lowering_quota_cannot_remove_existing_or_reserved_files(owner):
    shop, product = tenant(owner, "调整额度", limit=10)
    assert upload(owner, receipt(owner, product), b"123456").status_code == 200
    with storage.receiving_upload() as reservation:
        with db() as c:
            reservation.bind_product(c, product)
        reservation.grow(3)
        with pytest.raises(HTTPException) as exc, db() as c:
            storage.set_shop_storage_limit(c, shop, 8)
        assert exc.value.status_code == 409
        with db() as c:
            assert storage.set_shop_storage_limit(c, shop, 9)["limit_bytes"] == 9
        assert usage(shop)["stored_bytes"] == 6
        assert usage(shop)["uploading_bytes"] == 3
    with db() as c:
        storage.set_shop_storage_limit(c, shop, 6)
    assert usage(shop)["stored_bytes"] == 6
    assert upload(owner, receipt(owner, product), b"x").status_code == 507


def test_existing_files_over_quota_are_preserved_and_only_new_uploads_fail(owner):
    shop, product = tenant(owner, "迁移旧附件", limit=10)
    receipt_token = receipt(owner, product)
    descriptor = upload(owner, receipt_token, b"123456").json()
    with db() as c:
        c.execute("UPDATE shops SET storage_limit_bytes=1 WHERE id=?", (shop,))
        storage.init_schema(c)
        assert (
            c.execute(
                "SELECT content FROM job_files WHERE id=?", (descriptor["id"],)
            ).fetchone()[0]
            == b"123456"
        )
    assert usage(shop)["stored_bytes"] == 6
    assert upload(owner, receipt_token, b"x").status_code == 507
    with db() as c:
        assert (
            c.execute(
                "SELECT content FROM job_files WHERE id=?", (descriptor["id"],)
            ).fetchone()[0]
            == b"123456"
        )


def test_shop_storage_endpoint_exposes_only_own_logical_usage(owner):
    shop_a, product_a = tenant(owner, "接口隔离甲", limit=10)
    _, product_b = tenant(owner, "接口隔离乙", limit=20)
    assert upload(owner, receipt(owner, product_a), b"123").status_code == 200
    assert upload(owner, receipt(owner, product_b), b"123456").status_code == 200
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as merchant:
        with db() as c:
            cookie = create_session(c, Response(), "admin", shop_id=shop_a)
        merchant.cookies.set("extore_session", cookie)
        response = merchant.get("/api/admin/storage")
        assert response.status_code == 200, response.text
        assert response.json() == {
            "stored_bytes": 3,
            "uploading_bytes": 0,
            "limit_bytes": 10,
            "active_uploads": 0,
            "upload_concurrency": storage.UPLOAD_CONCURRENCY,
        }
    response = owner.get("/api/admin/storage")
    assert response.status_code == 200
    assert response.json()["stored_bytes"] == 9
    assert "disk_free_bytes" in response.json()


def test_shop_cannot_adjust_quota_but_recent_superadmin_can(owner):
    shop, product = tenant(owner, "平台配置", limit=10)
    assert upload(owner, receipt(owner, product), b"123456").status_code == 200
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as merchant:
        with db() as c:
            cookie = create_session(c, Response(), "admin", shop_id=shop)
        merchant.cookies.set("extore_session", cookie)
        assert (
            merchant.patch(
                f"/api/platform/shops/{shop}", json={"storage_limit_bytes": 20}
            ).status_code
            == 403
        )
    assert usage(shop)["limit_bytes"] == 10
    response = owner.patch(
        f"/api/platform/shops/{shop}", json={"storage_limit_bytes": 20}
    )
    assert response.status_code == 200, response.text
    assert usage(shop)["limit_bytes"] == 20
    response = owner.patch(
        f"/api/platform/shops/{shop}", json={"storage_limit_bytes": 5}
    )
    assert response.status_code == 409
    assert usage(shop)["limit_bytes"] == 20
    assert usage(shop)["stored_bytes"] == 6


def test_invalid_combined_shop_settings_roll_back_quota_disable_and_sessions(owner):
    shop, product = tenant(owner, "原子配置", limit=10)
    assert upload(owner, receipt(owner, product), b"123456").status_code == 200
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as merchant:
        with db() as c:
            cookie = create_session(c, Response(), "admin", shop_id=shop)
        merchant.cookies.set("extore_session", cookie)
        response = owner.patch(
            f"/api/platform/shops/{shop}",
            json={"enabled": False, "storage_limit_bytes": 5},
        )
        assert response.status_code == 409, response.text
        assert merchant.get("/api/admin/storage").status_code == 200
        with db() as c:
            row = c.execute(
                "SELECT enabled,storage_limit_bytes FROM shops WHERE id=?", (shop,)
            ).fetchone()
        assert dict(row) == {"enabled": 1, "storage_limit_bytes": 10}

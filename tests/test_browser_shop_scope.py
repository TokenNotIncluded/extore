"""A stale page must not send its operations into another browser identity."""

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response
from test_cli_auth import authorize, bearer, link, login

from extore.app import app
from extore.db import db
from extore.security import create_session, digest

ORIGIN = "http://localhost:8000"


@pytest.fixture
def shops():
    opened = []
    stores = []
    with db() as c:
        for name in ("Captured A", "Actual B"):
            sid = str(uuid.uuid4())
            c.execute(
                "INSERT INTO shops(id,name,email,created,verified) VALUES (?,?,?,?,1)",
                (sid, name, sid + "@example.test", time.time()),
            )
            cookie = create_session(
                c, Response(), "admin", shop_id=sid, auth_method="passkey"
            )
            session_id = c.execute(
                "SELECT id FROM sessions WHERE digest=?", (digest(cookie),)
            ).fetchone()[0]
            stores.append(
                {"id": sid, "name": name, "cookie": cookie, "session_id": session_id}
            )
    for store in stores:
        client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        client.cookies.set("extore_session", store["cookie"])
        opened.append(client)
        store["client"] = client
    yield stores
    for client in opened:
        client.close()


def headers(store, *, session_id=None):
    return {
        "X-Extore-Shop-Scope": store["id"] or "platform",
        "X-Extore-Session-ID": session_id or store["session_id"],
    }


def product_count():
    with db() as c:
        return c.execute("SELECT count(*) FROM products").fetchone()[0]


def current_name(sid):
    with db() as c:
        return c.execute("SELECT name FROM shops WHERE id=?", (sid,)).fetchone()[0]


def test_captured_other_shop_header_rejects_create_without_mutation(shops):
    captured, actual = shops
    before = product_count()
    result = actual["client"].post(
        "/api/admin/products",
        json={"name": "Must never reach B", "parameters": []},
        headers=headers(captured),
    )
    assert result.status_code == 409, result.text
    assert product_count() == before
    assert (
        actual["client"]
        .get("/api/admin/products", headers=headers(captured))
        .status_code
        == 409
    )


def test_captured_other_shop_cannot_rename_current_store(shops):
    captured, actual = shops
    result = actual["client"].patch(
        "/api/shop/account", json={"name": "Old A form"}, headers=headers(captured)
    )
    assert result.status_code == 409, result.text
    assert current_name(captured["id"]) == captured["name"]
    assert current_name(actual["id"]) == actual["name"]


def test_root_capture_does_not_become_merchant_create(shops, owner):
    root = owner.get("/api/auth/status").json()
    actual = shops[0]
    before = product_count()
    result = actual["client"].post(
        "/api/admin/products",
        json={"name": "Wrong platform operation", "parameters": []},
        headers={
            "X-Extore-Shop-Scope": "platform",
            "X-Extore-Session-ID": root["session_id"],
        },
    )
    assert result.status_code == 409, result.text
    assert product_count() == before


def test_merchant_capture_does_not_become_root_create(shops, owner):
    before = product_count()
    result = owner.post(
        "/api/admin/products",
        json={"name": "Wrong root operation", "parameters": []},
        headers=headers(shops[0]),
    )
    assert result.status_code == 409, result.text
    assert product_count() == before


def test_same_shop_changed_session_is_stale_even_when_role_and_shop_match(shops):
    store = shops[0]
    with db() as c:
        new_cookie = create_session(
            c, Response(), "admin", shop_id=store["id"], auth_method="passkey"
        )
    store["client"].cookies.set("extore_session", new_cookie)
    result = store["client"].patch(
        "/api/shop/account",
        json={"name": "Stale same-shop form"},
        headers=headers(store),
    )
    assert result.status_code == 409, result.text
    assert current_name(store["id"]) == store["name"]
    status = store["client"].get("/api/auth/status").json()
    assert status["session_id"] != store["session_id"]
    assert status["shop_id"] == store["id"] and status["role"] == "admin"


def test_matching_captured_headers_allow_exact_store_operation(shops):
    store = shops[0]
    result = store["client"].post(
        "/api/admin/products",
        json={"name": "Right store operation", "parameters": []},
        headers=headers(store),
    )
    assert result.status_code == 200, result.text
    with db() as c:
        assert (
            c.execute(
                "SELECT shop_id FROM products WHERE id=?", (result.json()["id"],)
            ).fetchone()[0]
            == store["id"]
        )
    assert (
        store["client"]
        .patch(
            "/api/shop/account", json={"name": "Right rename"}, headers=headers(store)
        )
        .status_code
        == 200
    )
    assert current_name(store["id"]) == "Right rename"


def test_anonymous_status_remains_available_without_stale_scope_headers(client):
    status = client.get("/api/auth/status").json()
    assert (
        status["role"] is None
        and status["session_id"] is None
        and status["shop_id"] is None
    )
    assert "registration_enabled" in status


def test_safe_status_can_discover_current_identity_without_old_scope(shops):
    _, actual = shops
    status = actual["client"].get("/api/auth/status").json()
    assert (
        status["shop_id"] == actual["id"]
        and status["session_id"] == actual["session_id"]
    )
    assert status["superadmin"] is False
    assert all(
        key not in status for key in ("digest", "cookie", "token", "password_hash")
    )


@pytest.mark.parametrize("legacy_null", [False, True])
def test_staff_scope_is_derived_from_product_including_legacy_sessions(
    shops, legacy_null
):
    store = shops[0]
    result = store["client"].post(
        "/api/admin/products", json={"name": "Staff scope", "parameters": []}
    )
    assert result.status_code == 200, result.text
    pid = result.json()["id"]
    invitation, raw = link(
        store["client"], pid, permissions=["queue.view", "product.edit"]
    )
    with db() as c:
        cookie = create_session(c, Response(), "staff", invitation["id"])
        sid = c.execute(
            "SELECT id FROM sessions WHERE digest=?", (digest(cookie),)
        ).fetchone()[0]
        if legacy_null:
            c.execute(
                "UPDATE sessions SET shop_id=NULL WHERE digest=?", (digest(cookie),)
            )
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as staff:
        staff.cookies.set("extore_session", cookie)
        accepted = staff.get(
            "/api/manage/product",
            headers={"X-Extore-Shop-Scope": store["id"], "X-Extore-Session-ID": sid},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["id"] == pid
        assert (
            staff.get(
                "/api/manage/product",
                headers={"X-Extore-Shop-Scope": "platform", "X-Extore-Session-ID": sid},
            ).status_code
            == 409
        )
        assert (
            staff.get(
                "/api/manage/product",
                headers={
                    "X-Extore-Shop-Scope": shops[1]["id"],
                    "X-Extore-Session-ID": sid,
                },
            ).status_code
            == 409
        )


def test_cli_bound_to_shop_remains_usable_without_browser_scope_headers(shops):
    store = shops[0]
    result = store["client"].post(
        "/api/admin/products", json={"name": "CLI scope", "parameters": []}
    )
    assert result.status_code == 200
    pid = result.json()["id"]
    _, raw = link(store["client"], pid, permissions=["queue.view"])
    with TestClient(app, base_url=ORIGIN) as client:
        device, key = authorize(client, raw)
        session = login(client, device, key)
        result = client.get(
            "/api/manage/jobs", params={"product_id": pid}, headers=bearer(session)
        )
        assert result.status_code == 200, result.text
        assert result.json() == []
        assert (
            client.get(
                "/api/manage/jobs",
                params={"product_id": pid},
                headers={**bearer(session), "X-Extore-Shop-Scope": shops[1]["id"]},
            ).status_code
            == 409
        )


def test_matching_scope_cannot_override_foreign_product_ownership(shops):
    first, second = shops
    created = first["client"].post(
        "/api/admin/products", json={"name": "A only", "parameters": []}
    )
    assert created.status_code == 200
    pid = created.json()["id"]
    assert (
        second["client"]
        .get("/api/manage/product", params={"product_id": pid}, headers=headers(second))
        .status_code
        == 403
    )
    assert (
        second["client"]
        .get("/api/manage/product", params={"product_id": pid}, headers=headers(first))
        .status_code
        == 409
    )

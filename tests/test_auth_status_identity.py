"""Shop display identity is private metadata, never an authorization selector."""

import time
import uuid
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response
from test_cli_auth import authorize, bearer, link, login, product, staff_browser
from test_passkeys import Authenticator, register
from test_tenant_owner_cli_auth import grant

from extore import auth
from extore.app import app
from extore.db import db
from extore.security import create_session, digest

ORIGIN = "http://localhost:8000"
DISPLAY_KEYS = {"shop_name", "shop_email"}


@pytest.fixture
def clients():
    with ExitStack() as stack:

        def create(*, cookie=None):
            client = stack.enter_context(TestClient(app, base_url=ORIGIN))
            if cookie:
                client.cookies.set("extore_session", cookie)
                client.headers["Origin"] = ORIGIN
            return client

        yield create


@pytest.fixture
def stores(clients):
    result = []
    with db() as c:
        for index in range(2):
            sid = str(uuid.uuid4())
            name, email = f"Private Store {index}", f"private-{sid}@example.test"
            c.execute(
                "INSERT INTO shops(id,name,email,created,verified) VALUES (?,?,?,?,1)",
                (sid, name, email, time.time()),
            )
            cookie = create_session(c, Response(), "admin", shop_id=sid)
            result.append({"id": sid, "name": name, "email": email, "cookie": cookie})
    for store in result:
        store["browser"] = clients(cookie=store["cookie"])
    return result


def assert_identity(value, store):
    assert value["role"] == "admin"
    assert value["shop_id"] == store["id"]
    assert value["shop_name"] == store["name"]
    assert value["shop_email"] == store["email"]
    assert value["superadmin"] is False


def assert_private(value, stores):
    assert DISPLAY_KEYS.isdisjoint(value)
    assert all(store["email"] not in str(value) for store in stores)


def test_authenticated_shop_owners_receive_only_their_own_display_identity(stores):
    for store in stores:
        response = store["browser"].get("/api/auth/status")
        assert response.status_code == 200, response.text
        assert_identity(response.json(), store)
        other = next(
            candidate for candidate in stores if candidate["id"] != store["id"]
        )
        assert other["email"] not in response.text


def test_query_cannot_select_another_shops_identity(stores):
    own, other = stores
    response = own["browser"].get("/api/auth/status", params={"shop_id": other["id"]})
    assert response.status_code == 200, response.text
    assert_identity(response.json(), own)
    assert other["email"] not in response.text


def test_stale_shop_context_does_not_return_owner_metadata(stores):
    own, other = stores
    response = own["browser"].get(
        "/api/auth/status", headers={"X-Extore-Shop-Scope": other["id"]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] is None
    assert response.json()["shop_id"] is None
    assert_private(response.json(), stores)


def test_identity_uses_current_shop_metadata_without_changing_authorization(stores):
    store = stores[0]
    before = store["browser"].get("/api/auth/status").json()
    with db() as c:
        c.execute(
            "UPDATE shops SET name=?,email=? WHERE id=?",
            ("Renamed Shop", "renamed@example.test", store["id"]),
        )
    after = store["browser"].get("/api/auth/status").json()
    assert after["shop_name"] == "Renamed Shop"
    assert after["shop_email"] == "renamed@example.test"
    assert after["session_id"] == before["session_id"]
    assert after["shop_id"] == before["shop_id"]


def test_anonymous_invalid_cookie_and_root_do_not_receive_shop_identity(
    owner, stores, clients
):
    for candidate in (clients(), clients(cookie="invalid-private-session"), owner):
        response = candidate.get("/api/auth/status")
        assert response.status_code == 200, response.text
        assert_private(response.json(), stores)
    assert owner.get("/api/auth/status").json()["superadmin"] is True


def test_product_link_browser_and_cli_do_not_receive_shop_owner_email(stores, clients):
    store = stores[0]
    pid = product(store["browser"])
    _, invitation = link(store["browser"], pid)
    browser = staff_browser(clients, invitation)
    response = browser.get("/api/auth/status")
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "staff"
    assert_private(response.json(), stores)
    cli = clients()
    device, key = authorize(cli, invitation)
    authenticated = login(cli, device, key)
    response = cli.get("/api/auth/status", headers=bearer(authenticated))
    assert response.status_code == 401, response.text
    assert_private(response.json(), stores)


def test_real_passkey_approved_owner_cli_receives_its_own_display_identity(
    stores, clients
):
    store = stores[0]
    store["passkey"] = Authenticator()
    register(store["browser"], store["passkey"], "Private Shop CLI")
    cli = clients()
    _, _, authenticated, _ = grant(store, cli)
    response = cli.get("/api/auth/status", headers=bearer(authenticated))
    assert response.status_code == 200, response.text
    assert_identity(response.json(), store)
    assert response.json()["channel"] == "cli"
    assert stores[1]["email"] not in response.text


@pytest.mark.parametrize("change", ("expired", "revoked", "disabled"))
def test_invalidated_owner_session_does_not_return_metadata(stores, change):
    store = stores[0]
    with db() as c:
        if change == "disabled":
            c.execute("UPDATE shops SET enabled=0 WHERE id=?", (store["id"],))
        else:
            column, value = (
                ("expires", time.time() - 1) if change == "expired" else ("revoked", 1)
            )
            c.execute(
                f"UPDATE sessions SET {column}=? WHERE digest=?",
                (value, digest(store["cookie"])),
            )
    response = store["browser"].get("/api/auth/status")
    assert response.status_code == 200, response.text
    assert response.json()["role"] is None
    assert response.json()["session_id"] is None
    assert response.json()["shop_id"] is None
    assert_private(response.json(), stores)


def test_logout_race_between_session_validation_and_metadata_read_stays_private(
    stores, monkeypatch
):
    original = auth.session

    def invalidate_after_validation(request, roles):
        current = original(request, roles)
        with db() as c:
            c.execute(
                "UPDATE sessions SET revoked=1 WHERE digest=?", (current["digest"],)
            )
        return current

    monkeypatch.setattr(auth, "session", invalidate_after_validation)
    response = stores[0]["browser"].get("/api/auth/status")
    assert response.status_code == 200, response.text
    assert response.json()["role"] is None
    assert response.json()["session_id"] is None
    assert response.json()["shop_id"] is None
    assert_private(response.json(), stores)

"""Confirmation must prove the current identity without replacing its session."""

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response
from test_passkeys import Authenticator, register
from webauthn.helpers import bytes_to_base64url

from extore.app import app
from extore.config import ORIGIN
from extore.db import db
from extore.security import create_session, digest

OPTIONS = "/api/auth/reauth/passkey/options"
VERIFY = "/api/auth/reauth/passkey/verify"


@pytest.fixture
def accounts(owner):
    opened = []
    result = []
    for index in range(3):
        shop_id = str(uuid.uuid4()) if index else None
        if shop_id:
            with db() as c:
                c.execute(
                    "INSERT INTO shops(id,name,email,created,verified) VALUES (?,?,?,?,1)",
                    (
                        shop_id,
                        f"Test shop {index}",
                        f"{shop_id}@example.test",
                        time.time(),
                    ),
                )
                cookie = create_session(c, Response(), "admin", shop_id=shop_id)
            client = TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
            client.cookies.set("extore_session", cookie)
            opened.append(client)
        else:
            client = owner
        key = Authenticator()
        register(client, key)
        status = client.get("/api/auth/status").json()
        cookie = client.cookies.get("extore_session")
        with db() as c:
            c.execute(
                "UPDATE sessions SET auth_at=? WHERE digest=?", (1, digest(cookie))
            )
        result.append(
            dict(client=client, key=key, shop_id=shop_id, status=status, cookie=cookie)
        )
    yield result
    for client in opened:
        client.close()


def options(account):
    response = account["client"].post(OPTIONS, json={})
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["userVerification"] == "required"
    assert [item["id"] for item in value["allowCredentials"]] == [account["key"].id]
    return value


def assertion(account, challenge, **kwargs):
    credential = account["key"].assertion(challenge, **kwargs)
    handle = (
        ("extore-shop:" + account["shop_id"]).encode()
        if account["shop_id"]
        else b"extore-owner"
    )
    credential["response"]["userHandle"] = bytes_to_base64url(handle)
    return credential


def session_row(account):
    with db() as c:
        return dict(
            c.execute(
                "SELECT * FROM sessions WHERE digest=?", (digest(account["cookie"]),)
            ).fetchone()
        )


def assert_identity(account):
    client = account["client"]
    assert client.cookies.get("extore_session") == account["cookie"]
    assert client.get("/api/auth/status").json() == account["status"]


@pytest.mark.parametrize("index", [0, 1, 2])
def test_correct_passkey_refreshes_only_current_session(accounts, index):
    account = accounts[index]
    before = session_row(account)
    credential = assertion(account, options(account))
    response = account["client"].post(VERIFY, json={"credential": credential})
    assert response.status_code == 200, response.text
    assert not any(
        value.startswith("extore_session=")
        for value in response.headers.get_list("set-cookie")
    )
    after = session_row(account)
    assert after["auth_at"] > before["auth_at"]
    assert after["auth_method"] == "passkey"
    for key in before.keys() - {"auth_at", "auth_method"}:
        assert after[key] == before[key], key
    assert_identity(account)
    assert (
        account["client"].post(VERIFY, json={"credential": credential}).status_code
        == 400
    )


@pytest.mark.parametrize("current,wrong", [(0, 1), (1, 0), (1, 2), (2, 1)])
def test_wrong_account_passkey_fails_without_login_or_session_changes(
    accounts, current, wrong
):
    account = accounts[current]
    before = [session_row(item) for item in accounts]
    credential = assertion(accounts[wrong], options(account))
    response = account["client"].post(VERIFY, json={"credential": credential})
    assert response.status_code == 401, response.text
    assert "不属于当前账号" in response.text
    assert [session_row(item) for item in accounts] == before
    assert_identity(account)
    # A failed signed response cannot be reused, even with the correct key.
    assert (
        account["client"].post(VERIFY, json={"credential": credential}).status_code
        == 400
    )
    # The next explicit attempt can still confirm the original identity.
    credential = assertion(account, options(account))
    assert (
        account["client"].post(VERIFY, json={"credential": credential}).status_code
        == 200
    )
    assert_identity(account)


@pytest.mark.parametrize(
    "failure",
    [
        "signature",
        "user-verification",
        "user-handle",
        "malformed-handle",
        "origin",
        "challenge",
    ],
)
def test_invalid_assertions_cannot_refresh_and_are_consumed(accounts, failure):
    account = accounts[0]
    challenge = options(account)
    before = session_row(account)
    kwargs = {}
    if failure == "signature":
        kwargs["signer"] = accounts[1]["key"]
    elif failure == "user-verification":
        kwargs["verified"] = False
    elif failure == "challenge":
        challenge = {**challenge, "challenge": "b3RoZXItY2hhbGxlbmdl"}
    credential = assertion(account, challenge, **kwargs)
    if failure in {"user-handle", "malformed-handle"}:
        credential["response"]["userHandle"] = (
            "@@@" if failure == "malformed-handle" else "b3RoZXI"
        )
    elif failure == "origin":
        import base64

        value = credential["response"]["clientDataJSON"]
        data = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        credential["response"]["clientDataJSON"] = bytes_to_base64url(
            data.replace(ORIGIN.encode(), b"https://different.example.test")
        )
    response = account["client"].post(VERIFY, json={"credential": credential})
    assert response.status_code == 401, response.text
    assert session_row(account) == before
    assert_identity(account)
    assert (
        account["client"].post(VERIFY, json={"credential": credential}).status_code
        == 400
    )


def test_allow_list_assertion_without_user_handle_is_valid(accounts):
    account = accounts[1]
    credential = assertion(account, options(account))
    credential["response"]["userHandle"] = None
    response = account["client"].post(VERIFY, json={"credential": credential})
    assert response.status_code == 200, response.text
    assert_identity(account)


@pytest.mark.parametrize(
    "replacement", ["session", "expired", "revoked", "disabled-shop"]
)
def test_confirmation_requires_the_original_live_session(accounts, replacement):
    account = accounts[1]
    credential = assertion(account, options(account))
    with db() as c:
        if replacement == "session":
            cookie = create_session(c, Response(), "admin", shop_id=account["shop_id"])
            account["client"].cookies.set("extore_session", cookie)
        elif replacement == "expired":
            c.execute("UPDATE challenges SET expires=0")
        elif replacement == "revoked":
            c.execute(
                "UPDATE sessions SET revoked=1 WHERE digest=?",
                (digest(account["cookie"]),),
            )
        else:
            c.execute("UPDATE shops SET enabled=0 WHERE id=?", (account["shop_id"],))
    response = account["client"].post(VERIFY, json={"credential": credential})
    assert response.status_code in {400, 401}, response.text
    assert session_row(account)["auth_at"] == 1


def test_login_and_confirmation_challenges_cannot_be_interchanged(accounts):
    account = accounts[0]
    credential = assertion(account, options(account))
    response = account["client"].post(
        "/api/auth/login/verify", json={"credential": credential}
    )
    assert response.status_code == 400
    assert_identity(account)
    challenge = account["client"].post("/api/auth/login/options", json={}).json()
    assert not challenge.get("allowCredentials")
    credential = assertion(account, challenge)
    assert (
        account["client"].post(VERIFY, json={"credential": credential}).status_code
        == 400
    )
    assert_identity(account)
    # Choosing an account remains intentional and supported on the login route.
    credential = assertion(accounts[1], challenge)
    response = account["client"].post(
        "/api/auth/login/verify", json={"credential": credential}
    )
    assert response.status_code == 200, response.text
    assert response.json()["shop_id"] == accounts[1]["shop_id"]


def test_no_passkeys_does_not_offer_an_unrestricted_confirmation(owner):
    before = owner.cookies.get("extore_session")
    response = owner.post(OPTIONS, json={})
    assert response.status_code == 409, response.text
    assert owner.cookies.get("extore_session") == before
    with db() as c:
        assert c.execute("SELECT count(*) FROM challenges").fetchone()[0] == 0


def test_confirmation_requires_authentication_and_captured_browser_scope(accounts):
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as anonymous:
        assert anonymous.post(OPTIONS, json={}).status_code == 401
        assert anonymous.post(VERIFY, json={"credential": {}}).status_code == 401
    account = accounts[0]
    for headers in [
        {"X-Extore-Shop-Scope": accounts[1]["shop_id"]},
        {"X-Extore-Session-ID": "stale-browser-session"},
    ]:
        assert (
            account["client"].post(OPTIONS, json={}, headers=headers).status_code == 409
        )
        credential = assertion(account, options(account))
        assert (
            account["client"]
            .post(VERIFY, json={"credential": credential}, headers=headers)
            .status_code
            == 409
        )
        assert session_row(account)["auth_at"] == 1
        assert_identity(account)

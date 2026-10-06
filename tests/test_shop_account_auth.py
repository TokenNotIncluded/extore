"""Account ceremonies cannot cross tenant boundaries or bypass a second factor."""

import base64
import json
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response

from extore import account_auth, auth, mail, totp
from extore.app import app
from extore.db import db, set_setting, setting
from extore.secret_store import open_secret, store_secret
from extore.security import create_session, digest

PASSWORD = "synthetic-password-13"
NEW_PASSWORD = "changed-synthetic-password-13"
SMTP = {
    "enabled": True,
    "host": "smtp.example.test",
    "port": 587,
    "mode": "starttls",
    "sender": "extore@example.test",
    "username": "mailer",
    "password": "synthetic-mail-password",
}


def _client():
    return TestClient(
        app,
        base_url="http://localhost:8000",
        headers={"Origin": "http://localhost:8000"},
    )


@pytest.fixture
def merchant():
    sid = str(uuid.uuid4())
    email = f"merchant-{sid}@example.test"
    with db() as c:
        c.execute(
            "INSERT INTO shops(id,name,email,password_hash,created,verified) VALUES (?,?,?,?,?,1)",
            (sid, "Synthetic Shop", email, account_auth.ph.hash(PASSWORD), time.time()),
        )
        cookie = create_session(
            c, Response(), "admin", shop_id=sid, auth_method="email_password"
        )
    client = _client()
    client.cookies.set("extore_session", cookie)
    yield {"id": sid, "email": email, "client": client, "cookie": cookie}
    client.close()


def _configure_smtp():
    with db() as c:
        mail.configure(c, SMTP)


def _mailed_token(kind):
    with db() as c:
        message = c.execute(
            "SELECT * FROM mail_outbox ORDER BY created DESC LIMIT 1"
        ).fetchone()
        values = mail._open(
            message["payload"], "mail", message["id"], message["tenant_id"]
        )
        assert f"/account/{kind}#" in values["body"]
        return values["body"].split(f"/account/{kind}#", 1)[1].split("\n", 1)[0]


def _enable_totp(sid):
    secret = totp.new_secret()
    raw, hashes = totp.generate_backup_codes()
    with db() as c:
        c.execute(
            "UPDATE shops SET totp_secret=?,totp_backup_digests=? WHERE id=?",
            (
                store_secret(
                    secret, tenant_id=sid, resource_type="totp", resource_id=sid
                ),
                json.dumps(hashes),
                sid,
            ),
        )
    return secret, raw


def _code(secret):
    return totp._code(base64.b32decode(secret), int(time.time() // 30))


def test_registration_default_off_and_merchant_cannot_enable_it(merchant, client):
    assert client.get("/api/auth/status").json()["registration_enabled"] is False
    assert (
        client.post(
            "/api/auth/register/email/request",
            json={"email": "new@example.test", "name": "New", "password": PASSWORD},
        ).status_code
        == 403
    )
    assert (
        merchant["client"]
        .put("/api/platform/settings", json={"registration_enabled": True})
        .status_code
        == 403
    )
    assert merchant["client"].get("/api/platform/shops").status_code == 403


def test_superadmin_settings_are_redacted_and_require_recent_login(owner):
    assert (
        owner.put(
            "/api/platform/settings", json={"registration_enabled": True}
        ).status_code
        == 409
    )
    result = owner.put(
        "/api/platform/settings", json={"smtp": SMTP, "registration_enabled": True}
    )
    assert result.status_code == 200, result.text
    assert result.json()["registration_enabled"] is True
    assert "password" not in result.json()["smtp"]
    assert "username" not in result.json()["smtp"]
    with db() as c:
        c.execute("UPDATE sessions SET auth_at=?", (time.time() - 601,))
    assert (
        owner.put(
            "/api/platform/settings", json={"registration_enabled": False}
        ).status_code
        == 401
    )


def test_disabling_smtp_disables_public_registration(owner):
    assert (
        owner.put(
            "/api/platform/settings", json={"smtp": SMTP, "registration_enabled": True}
        ).status_code
        == 200
    )
    result = owner.put("/api/platform/settings", json={"smtp": {"enabled": False}})
    assert result.status_code == 200
    assert result.json()["registration_enabled"] is False


def test_invite_is_atomic_if_mail_not_configured(owner):
    email = f"unconfigured-{uuid.uuid4()}@example.test"
    response = owner.post(
        "/api/platform/shops", json={"email": email, "name": "Invited"}
    )
    assert response.status_code == 503
    with db() as c:
        assert (
            c.execute("SELECT 1 FROM shops WHERE email=?", (email,)).fetchone() is None
        )


def test_invite_email_verified_password_claim_is_one_use(owner):
    _configure_smtp()
    email = f"invited-{uuid.uuid4()}@example.test"
    response = owner.post(
        "/api/platform/shops", json={"email": email.upper(), "name": "Invited"}
    )
    assert response.status_code == 200, response.text
    sid = response.json()["shop"]["id"]
    assert response.json()["shop"]["verified"] is False
    assert "token" not in response.text
    raw = _mailed_token("invite")
    customer = _client()
    assert (
        customer.post(
            "/api/auth/invite/claim", json={"token": raw, "password": PASSWORD}
        ).status_code
        == 200
    )
    status = customer.get("/api/auth/status").json()
    assert status["shop_id"] == sid and status["superadmin"] is False
    assert (
        customer.post(
            "/api/auth/invite/claim", json={"token": raw, "password": PASSWORD}
        ).status_code
        == 400
    )
    with db() as c:
        row = c.execute("SELECT * FROM shops WHERE id=?", (sid,)).fetchone()
        assert row["email"] == email and row["verified"] == 1
        assert PASSWORD not in row["password_hash"]
        assert (
            raw
            not in c.execute("SELECT payload FROM mail_outbox LIMIT 1").fetchone()[
                "payload"
            ]
        )
    customer.close()


def test_email_login_keeps_password_enabled_with_passkeys(merchant, client):
    with db() as c:
        c.execute(
            "INSERT INTO credentials(id,public_key,sign_count,name,created,shop_id) VALUES (?,?,?,?,?,?)",
            (
                "synthetic-merchant-passkey",
                b"key",
                0,
                "Device",
                time.time(),
                merchant["id"],
            ),
        )
    response = client.post(
        "/api/auth/email/login",
        json={"email": merchant["email"].upper(), "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    assert response.json()["shop_id"] == merchant["id"]
    assert client.get("/api/shop/account").json()["passkeys"] == 1


def test_unknown_account_and_wrong_password_respond_identically(merchant, client):
    results = []
    for email in (merchant["email"], "unknown@example.test"):
        results.append(
            client.post(
                "/api/auth/email/login", json={"email": email, "password": "wrong"}
            )
        )
    assert [r.status_code for r in results] == [401, 401]
    assert results[0].json() == results[1].json()


def test_disabled_or_unverified_shop_cannot_login(merchant, client):
    with db() as c:
        c.execute("UPDATE shops SET verified=0 WHERE id=?", (merchant["id"],))
    body = {"email": merchant["email"], "password": PASSWORD}
    assert client.post("/api/auth/email/login", json=body).status_code == 401
    with db() as c:
        c.execute("UPDATE shops SET verified=1,enabled=0 WHERE id=?", (merchant["id"],))
    assert client.post("/api/auth/email/login", json=body).status_code == 401
    assert merchant["client"].get("/api/shop/account").status_code == 401


def test_password_login_requires_totp_and_consumes_code_once(merchant, client):
    secret, _ = _enable_totp(merchant["id"])
    body = {"email": merchant["email"], "password": PASSWORD}
    assert client.post("/api/auth/email/login", json=body).status_code == 401
    code = _code(secret)
    assert (
        client.post("/api/auth/email/login", json={**body, "code": code}).status_code
        == 200
    )
    client.cookies.clear()
    assert (
        client.post("/api/auth/email/login", json={**body, "code": code}).status_code
        == 401
    )


def test_backup_code_is_account_scoped_and_consumed_once(merchant, client):
    _, backups = _enable_totp(merchant["id"])
    body = {"email": merchant["email"], "password": PASSWORD, "backup_code": backups[0]}
    assert client.post("/api/auth/email/login", json=body).status_code == 200
    client.cookies.clear()
    assert client.post("/api/auth/email/login", json=body).status_code == 401
    with db() as c:
        stored = c.execute(
            "SELECT totp_backup_digests FROM shops WHERE id=?", (merchant["id"],)
        ).fetchone()[0]
        assert backups[0] not in stored and len(json.loads(stored)) == 9


def test_totp_setup_does_not_enable_until_confirm_and_is_encrypted(merchant):
    response = merchant["client"].post(
        "/api/auth/totp/setup", json={"password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    secret = response.json()["secret"]
    with db() as c:
        row = c.execute("SELECT * FROM shops WHERE id=?", (merchant["id"],)).fetchone()
        assert row["totp_secret"] is None and secret not in row["pending_totp_secret"]
    confirm = merchant["client"].post(
        "/api/auth/totp/confirm", json={"code": _code(secret)}
    )
    assert confirm.status_code == 200, confirm.text
    assert len(confirm.json()["backup_codes"]) == 10
    assert merchant["client"].get("/api/shop/account").json()["totp_enabled"] is True
    assert "backup_codes" not in merchant["client"].get("/api/shop/account").text


def test_mfa_security_write_uses_fresh_body_without_double_consuming_code(merchant):
    secret, _ = _enable_totp(merchant["id"])
    with db() as c:
        c.execute(
            "UPDATE sessions SET auth_at=1 WHERE digest=?",
            (digest(merchant["cookie"]),),
        )
    code = _code(secret)
    response = merchant["client"].post(
        "/api/auth/totp/backup-codes",
        json={"password": PASSWORD, "code": code},
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["backup_codes"]) == 10
    assert (
        merchant["client"]
        .post("/api/auth/totp/disable", json={"password": PASSWORD, "code": code})
        .status_code
        == 401
    )


def test_email_password_reset_cannot_bypass_totp_and_invalidates_old_sessions(
    merchant, client
):
    _configure_smtp()
    secret, _ = _enable_totp(merchant["id"])
    assert (
        client.post(
            "/api/auth/password/reset/request", json={"email": merchant["email"]}
        ).status_code
        == 200
    )
    raw = _mailed_token("reset")
    body = {"token": raw, "password": NEW_PASSWORD}
    assert client.post("/api/auth/password/reset/confirm", json=body).status_code == 401
    assert (
        client.post(
            "/api/auth/password/reset/confirm", json={**body, "code": _code(secret)}
        ).status_code
        == 200
    )
    assert merchant["client"].get("/api/shop/account").status_code == 401
    assert client.post("/api/auth/password/reset/confirm", json=body).status_code == 400
    with db() as c:
        hashed = c.execute(
            "SELECT password_hash FROM shops WHERE id=?", (merchant["id"],)
        ).fetchone()[0]
        assert account_auth.ph.verify(hashed, NEW_PASSWORD)


def test_reset_request_does_not_enumerate_when_smtp_disabled(merchant, client):
    known = client.post(
        "/api/auth/password/reset/request", json={"email": merchant["email"]}
    )
    missing = client.post(
        "/api/auth/password/reset/request", json={"email": "missing@example.test"}
    )
    assert known.status_code == missing.status_code == 200
    assert known.json() == missing.json()


def test_registration_requires_mailed_verification_and_one_use(owner):
    _configure_smtp()
    with db() as c:
        set_setting(c, "registration_enabled", "true")
    email = f"registered-{uuid.uuid4()}@example.test"
    customer = _client()
    assert (
        customer.post(
            "/api/auth/register/email/request",
            json={"email": email, "name": "New Shop", "password": PASSWORD},
        ).status_code
        == 200
    )
    with db() as c:
        assert (
            c.execute("SELECT 1 FROM shops WHERE email=?", (email,)).fetchone() is None
        )
    raw = _mailed_token("register")
    response = customer.post("/api/auth/register/email/confirm", json={"token": raw})
    assert response.status_code == 200, response.text
    assert response.json()["shop_id"] is not None
    assert (
        customer.post(
            "/api/auth/register/email/confirm", json={"token": raw}
        ).status_code
        == 400
    )
    customer.close()


def test_registration_confirmation_respects_current_switch(owner):
    _configure_smtp()
    with db() as c:
        set_setting(c, "registration_enabled", "true")
    customer = _client()
    assert (
        customer.post(
            "/api/auth/register/email/request",
            json={
                "email": f"pending-{uuid.uuid4()}@example.test",
                "name": "Pending",
                "password": PASSWORD,
            },
        ).status_code
        == 200
    )
    raw = _mailed_token("register")
    with db() as c:
        set_setting(c, "registration_enabled", "false")
    assert (
        customer.post(
            "/api/auth/register/email/confirm", json={"token": raw}
        ).status_code
        == 403
    )
    customer.close()


def test_multiple_passkeys_are_scoped_and_last_merchant_key_removable(merchant, owner):
    with db() as c:
        for cid, sid in (
            ("root-key", None),
            ("merchant-key-1", merchant["id"]),
            ("merchant-key-2", merchant["id"]),
        ):
            c.execute(
                "INSERT INTO credentials(id,public_key,sign_count,name,created,shop_id) VALUES (?,?,?,?,?,?)",
                (cid, b"key", 0, cid, time.time(), sid),
            )
    assert {x["id"] for x in owner.get("/api/auth/passkeys").json()} == {"root-key"}
    assert {x["id"] for x in merchant["client"].get("/api/auth/passkeys").json()} == {
        "merchant-key-1",
        "merchant-key-2",
    }
    assert merchant["client"].delete("/api/auth/passkeys/root-key").status_code == 404
    assert (
        merchant["client"].delete("/api/auth/passkeys/merchant-key-1").status_code
        == 200
    )
    assert (
        merchant["client"].delete("/api/auth/passkeys/merchant-key-2").status_code
        == 200
    )
    assert owner.delete("/api/auth/passkeys/root-key").status_code == 409


def test_merchant_binding_passkey_does_not_disable_root_bootstrap(
    merchant, monkeypatch
):
    class Verified:
        credential_id = b"new-merchant-key"
        credential_public_key = b"fake-public-key-for-scoping"
        sign_count = 0

    monkeypatch.setattr(
        auth, "verify_registration_response", lambda **kwargs: Verified()
    )
    with db() as c:
        set_setting(c, "bootstrap_password", "root-password-hash-sentinel")
    assert (
        merchant["client"].post("/api/auth/register/options", json={}).status_code
        == 200
    )
    assert (
        merchant["client"]
        .post("/api/auth/register/verify", json={"credential": {}, "name": "Laptop"})
        .status_code
        == 200
    )
    with db() as c:
        assert setting(c, "bootstrap_password") == "root-password-hash-sentinel"
        row = c.execute("SELECT * FROM credentials").fetchone()
        assert row["shop_id"] == merchant["id"]
        assert c.execute(
            "SELECT password_hash FROM shops WHERE id=?", (merchant["id"],)
        ).fetchone()[0]


def test_totp_ciphertext_cannot_be_moved_between_shops(merchant):
    secret, _ = _enable_totp(merchant["id"])
    with db() as c:
        ciphertext = c.execute(
            "SELECT totp_secret FROM shops WHERE id=?", (merchant["id"],)
        ).fetchone()[0]
    with pytest.raises(RuntimeError):
        open_secret(
            ciphertext,
            tenant_id=str(uuid.uuid4()),
            resource_type="totp",
            resource_id=merchant["id"],
        )
    assert secret not in ciphertext


def test_root_auth_reset_preserves_merchant_sessions_and_passkeys(merchant, owner):
    with db() as c:
        account_auth.revoke_shop_auth(c, None)
        assert (
            c.execute(
                "SELECT revoked FROM sessions WHERE digest=?",
                (digest(merchant["cookie"]),),
            ).fetchone()[0]
            == 0
        )
    assert merchant["client"].get("/api/shop/account").status_code == 200
    assert owner.get("/api/platform/shops").status_code == 401


def test_invite_expired_token_cannot_claim_account(owner):
    _configure_smtp()
    assert (
        owner.post(
            "/api/platform/shops",
            json={"email": f"expired-{uuid.uuid4()}@example.test", "name": "Expired"},
        ).status_code
        == 200
    )
    raw = _mailed_token("invite")
    with db() as c:
        c.execute(
            "UPDATE account_tokens SET expires=? WHERE digest=?",
            (time.time() - 1, digest(raw)),
        )
    customer = _client()
    assert (
        customer.post(
            "/api/auth/invite/claim", json={"token": raw, "password": PASSWORD}
        ).status_code
        == 400
    )
    customer.close()


def test_password_reset_old_token_is_invalid_after_password_change(merchant, client):
    _configure_smtp()
    assert (
        client.post(
            "/api/auth/password/reset/request", json={"email": merchant["email"]}
        ).status_code
        == 200
    )
    raw = _mailed_token("reset")
    response = merchant["client"].post(
        "/api/auth/password/change",
        json={"password": PASSWORD, "new_password": NEW_PASSWORD},
    )
    assert response.status_code == 200, response.text
    assert merchant["client"].get("/api/shop/account").status_code == 200
    assert (
        client.post(
            "/api/auth/password/reset/confirm",
            json={"token": raw, "password": PASSWORD},
        ).status_code
        == 400
    )


def test_password_and_totp_tokens_are_not_written_to_audit(merchant, client):
    secret, backups = _enable_totp(merchant["id"])
    assert (
        client.post(
            "/api/auth/email/login",
            json={
                "email": merchant["email"],
                "password": PASSWORD,
                "backup_code": backups[0],
            },
        ).status_code
        == 200
    )
    with db() as c:
        logged = json.dumps([dict(r) for r in c.execute("SELECT * FROM audit")])
    for private in (PASSWORD, secret, backups[0], merchant["email"]):
        assert private not in logged


def test_account_rate_limit_survives_ip_rotation(merchant):
    from fastapi import HTTPException
    from starlette.requests import Request

    for i in range(5):
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/auth/email/login",
                "headers": [],
                "client": (f"192.0.2.{i + 1}", 1),
            }
        )
        account_auth._throttle(request, "email-login", merchant["email"])
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/email/login",
            "headers": [],
            "client": ("192.0.2.99", 1),
        }
    )
    with pytest.raises(HTTPException) as caught:
        account_auth._throttle(request, "email-login", merchant["email"])
    assert caught.value.status_code == 429


def test_passkey_login_derives_only_credential_shop_scope(
    merchant, client, monkeypatch
):
    class Verified:
        new_sign_count = 2

    monkeypatch.setattr(
        auth, "verify_authentication_response", lambda **kwargs: Verified()
    )
    with db() as c:
        c.execute(
            "INSERT INTO credentials(id,public_key,sign_count,name,created,shop_id) VALUES (?,?,?,?,?,?)",
            (
                "tenant-login-credential",
                b"credential",
                1,
                "Laptop",
                time.time(),
                merchant["id"],
            ),
        )
    assert client.post("/api/auth/login/options", json={}).status_code == 200
    response = client.post(
        "/api/auth/login/verify", json={"credential": {"id": "tenant-login-credential"}}
    )
    assert response.status_code == 200, response.text
    assert (
        response.json()["shop_id"] == merchant["id"]
        and response.json()["superadmin"] is False
    )
    assert client.get("/api/platform/shops").status_code == 403


def test_old_passkey_challenge_cannot_be_used_after_session_switch(
    merchant, owner, monkeypatch
):
    class Verified:
        credential_id = b"cross-session-credential"
        credential_public_key = b"credential"
        sign_count = 0

    monkeypatch.setattr(
        auth, "verify_registration_response", lambda **kwargs: Verified()
    )
    assert (
        merchant["client"].post("/api/auth/register/options", json={}).status_code
        == 200
    )
    challenge_cookie = merchant["client"].cookies.get("extore_challenge")
    owner.cookies.set("extore_challenge", challenge_cookie, path="/api/auth")
    assert (
        owner.post("/api/auth/register/verify", json={"credential": {}}).status_code
        == 400
    )
    with db() as c:
        assert not c.execute(
            "SELECT 1 FROM credentials WHERE id=?",
            ("Y3Jvc3Mtc2Vzc2lvbi1jcmVkZW50aWFs",),
        ).fetchone()


def test_superadmin_disable_shop_revokes_merchant_but_not_root(owner, merchant):
    response = owner.patch(
        f"/api/platform/shops/{merchant['id']}", json={"enabled": False}
    )
    assert response.status_code == 200, response.text
    assert merchant["client"].get("/api/shop/account").status_code == 401
    assert owner.get("/api/platform/shops").status_code == 200


def test_concurrent_login_same_otp_creates_only_one_session(merchant):
    from concurrent.futures import ThreadPoolExecutor

    secret, _ = _enable_totp(merchant["id"])
    body = {"email": merchant["email"], "password": PASSWORD, "code": _code(secret)}

    def login_once(_):
        with _client() as client:
            return client.post("/api/auth/email/login", json=body).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(login_once, range(4)))
    assert statuses.count(200) == 1
    assert statuses.count(401) == 3


def test_concurrent_invite_claim_consumes_token_once(owner):
    from concurrent.futures import ThreadPoolExecutor

    _configure_smtp()
    assert (
        owner.post(
            "/api/platform/shops",
            json={
                "email": f"concurrent-{uuid.uuid4()}@example.test",
                "name": "Concurrent",
            },
        ).status_code
        == 200
    )
    raw = _mailed_token("invite")

    def claim_once(_):
        with _client() as client:
            return client.post(
                "/api/auth/invite/claim", json={"token": raw, "password": PASSWORD}
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(claim_once, range(2)))
    assert sorted(statuses) == [200, 400]

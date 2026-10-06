import time

from argon2 import PasswordHasher
from test_redemption import redeem

from extore.db import db, set_setting


def test_password_bootstrap_cannot_manage(client):
    with db() as c:
        set_setting(
            c, "bootstrap_password", PasswordHasher().hash("testing-password-123")
        )
    assert (
        client.post(
            "/api/auth/password", json={"password": "testing-password-123"}
        ).status_code
        == 200
    )
    assert client.get("/api/auth/status").json()["role"] == "bootstrap"
    assert client.get("/api/admin/products").status_code == 401
    assert client.post("/api/auth/register/options", json={}).status_code == 200
    assert (
        client.post(
            "/api/auth/register/verify", json={"credential": {"id": "fake"}}
        ).status_code
        == 400
    )
    assert client.get("/api/admin/products").status_code == 401


def test_password_disabled_if_key_exists(client):
    with db() as c:
        set_setting(
            c, "bootstrap_password", PasswordHasher().hash("testing-password-123")
        )
        c.execute(
            "INSERT INTO credentials VALUES (?,?,?,?,?)",
            ("fake", b"fake", 0, "test", time.time()),
        )
    assert (
        client.post(
            "/api/auth/password", json={"password": "testing-password-123"}
        ).status_code
        == 403
    )


def test_staff_scoping_claim_and_revocation(owner, setup_product):
    pid, code = setup_product()
    _, j = redeem(owner, code)
    pid2, code2 = setup_product()
    _, j2 = redeem(owner, code2)
    staff = owner.post(
        "/api/admin/staff", json={"product_id": pid, "name": "员工 A"}
    ).json()
    admin_cookie = owner.cookies.get("extore_session")
    value = staff["url"].split("#")[1]
    assert owner.post("/api/staff/login", json={"token": value}).status_code == 200
    assert owner.get("/api/admin/products").status_code == 401
    assert [j["id"] for j in owner.get("/api/manage/jobs").json()] == [j["id"]]
    assert (
        owner.post(
            "/api/manage/batch", json={"ids": [j2["id"]], "action": "claim"}
        ).status_code
        == 403
    )
    assert (
        owner.post(
            "/api/manage/batch",
            json={"ids": [j["id"]], "action": "succeed", "content": "x"},
        ).status_code
        == 409
    )
    assert (
        owner.post(
            "/api/manage/batch", json={"ids": [j["id"]], "action": "claim"}
        ).status_code
        == 200
    )
    # Login as another staff member, verify it cannot finish A's task.
    owner.cookies.clear()
    owner.cookies.set("extore_session", admin_cookie)
    # staff login rotates the previous session; create a new owner session for this test.
    from starlette.responses import Response

    from extore.security import create_session

    with db() as c:
        admin_cookie = create_session(c, Response(), "admin")
    owner.cookies.set("extore_session", admin_cookie)
    other = owner.post(
        "/api/admin/staff", json={"product_id": pid, "name": "员工 B"}
    ).json()
    owner.post("/api/staff/login", json={"token": other["url"].split("#")[1]})
    assert (
        owner.post(
            "/api/manage/batch",
            json={"ids": [j["id"]], "action": "succeed", "content": "x"},
        ).status_code
        == 409
    )
    with db() as c:
        admin_cookie = create_session(c, Response(), "admin")
    staff_cookie = owner.cookies.get("extore_session", domain="localhost.local")
    owner.cookies.clear()
    owner.cookies.set("extore_session", admin_cookie)
    assert (
        owner.post("/api/admin/staff/" + other["id"] + "/revoke", json={}).status_code
        == 200
    )
    owner.cookies.clear()
    owner.cookies.set("extore_session", staff_cookie)
    assert owner.get("/api/manage/jobs").status_code == 401


def test_csrf_and_body_limit(owner):
    assert (
        owner.post(
            "/api/auth/logout", json={}, headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert owner.post("/api/auth/logout", content=b"x" * 256001).status_code == 413


def test_card_revocation(owner, setup_product):
    _, code = setup_product()
    cid = owner.get("/api/admin/cards").json()[0]["id"]
    assert owner.post("/api/admin/cards/" + cid + "/revoke", json={}).status_code == 200
    assert owner.post("/api/exchange", json={"code": code}).status_code == 404


def test_cli_bootstrap_is_private_and_idempotent(monkeypatch, capsys):
    import sys

    import pytest

    from extore.cli import main
    from extore.config import DATA
    from extore.db import setting

    path = DATA / "bootstrap-password.txt"
    path.unlink(missing_ok=True)
    monkeypatch.setattr(sys, "argv", ["extore-admin", "bootstrap"])
    main()
    password = path.read_text().strip()
    assert len(password) >= 40 and path.stat().st_mode & 0o777 == 0o600
    assert password not in capsys.readouterr().out
    with db() as c:
        assert PasswordHasher().verify(setting(c, "bootstrap_password"), password)
    with pytest.raises(SystemExit, match="Already initialized"):
        main()
    path.unlink()


def test_head_probes(client):
    assert client.head("/").status_code == 200
    assert client.head("/health").status_code == 200
    assert client.head("/health").content == b""

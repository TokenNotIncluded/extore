"""Management admission quotas and authorization of retained session records."""

import json
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.responses import Response

from extore.app import app
from extore.db import audit, db
from extore.link_access import consume_link, init_schema, revoke_session
from extore.security import create_session, digest, session


def product(owner, **kwargs):
    response = owner.post(
        "/api/admin/products",
        json={"name": "会话测试商品", "parameters": [], **kwargs},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def link(owner, pid, permissions=None, max_uses=1):
    body = {"product_id": pid, "name": "处理链接", "max_uses": max_uses}
    if permissions is not None:
        body["permissions"] = permissions
    response = owner.post("/api/admin/staff", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    return result, result["url"].split("#", 1)[1]


def staff_client(token):
    client = TestClient(
        app,
        base_url="http://localhost:8000",
        headers={"Origin": "http://localhost:8000"},
    )
    response = client.post("/api/staff/login", json={"token": token})
    assert response.status_code == 200, response.text
    return client


def test_default_single_use_login_reuse_does_not_mint_another_session(owner):
    pid = product(owner)
    grant, token = link(owner, pid)
    assert (grant["max_uses"], grant["uses"], grant["remaining_uses"]) == (1, 0, 1)
    handler = staff_client(token)
    cookie = handler.cookies.get("extore_session")
    response = handler.post("/api/staff/login", json={"token": token})
    assert response.status_code == 200
    assert handler.cookies.get("extore_session") == cookie
    status = handler.get("/api/auth/status").json()
    assert status["role"] == "staff"
    assert (status["max_uses"], status["uses"], status["remaining_uses"]) == (1, 1, 0)
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM sessions WHERE staff_id=?", (grant["id"],)
            ).fetchone()[0]
            == 1
        )
    fresh = TestClient(
        app,
        base_url="http://localhost:8000",
        headers={"Origin": "http://localhost:8000"},
    )
    assert fresh.post("/api/staff/login", json={"token": token}).status_code == 409
    assert handler.get("/api/manage/products").status_code == 200


def test_quota_is_atomic_and_existing_session_survives_exhaustion(owner):
    grant, _ = link(owner, product(owner))

    def attempt(_):
        try:
            with db() as c:
                row = c.execute(
                    "SELECT * FROM staff WHERE id=?", (grant["id"],)
                ).fetchone()
                consume_link(c, row)
            return 200
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(attempt, range(6)))
    assert results.count(200) == 1
    assert results.count(409) == 5
    with db() as c:
        row = c.execute("SELECT * FROM staff WHERE id=?", (grant["id"],)).fetchone()
        assert row["uses"] == 1
        cookie = create_session(c, Response(), "staff", grant["id"])
    handler = TestClient(app, base_url="http://localhost:8000")
    handler.cookies.set("extore_session", cookie)
    assert handler.get("/api/manage/products").status_code == 200


def test_concurrent_new_browser_logins_create_exactly_one_session(owner):
    grant, token = link(owner, product(owner))
    clients = [
        TestClient(
            app,
            base_url="http://localhost:8000",
            headers={"Origin": "http://localhost:8000"},
        )
        for _ in range(6)
    ]
    ready = Barrier(len(clients))

    def attempt(client):
        assert client.cookies.get("extore_session") is None
        ready.wait(timeout=10)
        response = client.post("/api/staff/login", json={"token": token})
        return response.status_code, client.cookies.get("extore_session")

    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        results = list(pool.map(attempt, clients))
    assert sum(status == 200 for status, _ in results) == 1
    assert sum(status == 409 for status, _ in results) == 5
    assert sum(cookie is not None for _, cookie in results) == 1
    with db() as c:
        assert (
            c.execute("SELECT uses FROM staff WHERE id=?", (grant["id"],)).fetchone()[0]
            == 1
        )
        assert (
            c.execute(
                "SELECT count(*) FROM sessions WHERE staff_id=?", (grant["id"],)
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute(
                "SELECT count(*) FROM audit WHERE action='link.consume' AND target=?",
                (grant["id"],),
            ).fetchone()[0]
            == 1
        )


def test_failed_admission_rolls_back_quota_and_audit(owner):
    grant, _ = link(owner, product(owner))
    with pytest.raises(RuntimeError):
        with db() as c:
            row = c.execute("SELECT * FROM staff WHERE id=?", (grant["id"],)).fetchone()
            consume_link(c, row)
            raise RuntimeError("session creation failed")
    with db() as c:
        assert (
            c.execute("SELECT uses FROM staff WHERE id=?", (grant["id"],)).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT count(*) FROM audit WHERE action='link.consume' AND target=?",
                (grant["id"],),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("invalid", [0, -1, 1001, True, "2"])
def test_new_link_quota_rejects_invalid_values(owner, invalid):
    response = owner.post(
        "/api/admin/staff",
        json={"product_id": product(owner), "name": "非法额度", "max_uses": invalid},
    )
    assert response.status_code == 422


def test_legacy_migration_counts_active_sessions_without_invalidating_them():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        "CREATE TABLE staff(id TEXT PRIMARY KEY);"
        "CREATE TABLE sessions(digest TEXT PRIMARY KEY,role TEXT,staff_id TEXT,expires REAL,created REAL);"
    )
    now = time.time()
    for sid in ("unused", "one", "many"):
        c.execute("INSERT INTO staff(id) VALUES (?)", (sid,))
    for key, sid, expires in (
        ("one-live", "one", now + 3600),
        ("one-expired", "one", now - 1),
        ("many-a", "many", now + 3600),
        ("many-b", "many", now + 3600),
    ):
        c.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?)", (key, "staff", sid, expires, now)
        )
    init_schema(c)
    initial_ids = {r["digest"]: r["id"] for r in c.execute("SELECT * FROM sessions")}
    init_schema(c)
    assert len(set(initial_ids.values())) == 4
    assert all(str(uuid.UUID(value)) == value for value in initial_ids.values())
    assert initial_ids == {
        r["digest"]: r["id"] for r in c.execute("SELECT * FROM sessions")
    }
    limits = {
        r["id"]: (r["max_uses"], r["uses"]) for r in c.execute("SELECT * FROM staff")
    }
    assert limits == {"unused": (1, 0), "one": (1, 1), "many": (2, 2)}
    assert all(
        r["revoked"] == 0 and r["last_seen"] == now
        for r in c.execute("SELECT * FROM sessions")
    )
    c.close()


def test_owner_session_list_hides_cookie_digests_and_bounds_metadata(owner):
    grant, token = link(owner, product(owner))
    handler = staff_client(token)
    cookie = handler.cookies.get("extore_session")
    response = owner.get("/api/admin/sessions")
    assert response.status_code == 200
    rows = response.json()
    assert sum(row["current"] for row in rows) == 1
    other = next(row for row in rows if row["link_id"] == grant["id"])
    assert other["active"] and not other["revoked"]
    assert len(other["ua"]) <= 300 and len(other["ip"]) <= 100
    assert str(uuid.UUID(other["id"])) == other["id"]
    for secret in (cookie, digest(cookie), token, digest(token)):
        assert secret not in response.text
    assert all("digest" not in row for row in rows)


def test_staff_session_scope_excludes_parents_peers_and_other_products(owner):
    pid = product(owner)
    permissions = ["queue.view", "queue.process", "links.delegate"]
    manager, manager_token = link(owner, pid, permissions)
    peer, peer_token = link(owner, pid)
    elsewhere, elsewhere_token = link(owner, product(owner))
    boss = staff_client(manager_token)
    child_response = boss.post(
        "/api/manage/links",
        json={"name": "下级", "permissions": ["queue.view"], "max_uses": 1},
    )
    assert child_response.status_code == 200, child_response.text
    child = child_response.json()
    worker = staff_client(child["url"].split("#", 1)[1])
    staff_client(peer_token)
    staff_client(elsewhere_token)
    visible = boss.get("/api/manage/sessions").json()
    assert {row["link_id"] for row in visible} == {manager["id"], child["id"]}
    own = worker.get("/api/manage/sessions").json()
    assert {row["link_id"] for row in own} == {child["id"]}
    owner_rows = owner.get("/api/admin/sessions").json()
    for forbidden in owner_rows:
        if forbidden["link_id"] != child["id"]:
            response = worker.delete(f"/api/manage/sessions/{forbidden['id']}")
            assert response.status_code == 404
    assert worker.get("/api/admin/sessions").status_code == 401
    assert {peer["id"], elsewhere["id"]}.isdisjoint({row["link_id"] for row in visible})


def test_delegate_can_revoke_child_but_child_cannot_revoke_parent(owner):
    manager, manager_token = link(
        owner, product(owner), ["queue.view", "links.delegate"]
    )
    boss = staff_client(manager_token)
    child = boss.post(
        "/api/manage/links", json={"name": "下级", "permissions": ["queue.view"]}
    ).json()
    worker = staff_client(child["url"].split("#", 1)[1])
    rows = boss.get("/api/manage/sessions").json()
    parent_session = next(row for row in rows if row["link_id"] == manager["id"])
    child_session = next(row for row in rows if row["link_id"] == child["id"])
    assert (
        worker.delete(f"/api/manage/sessions/{parent_session['id']}").status_code == 404
    )
    response = boss.delete(f"/api/manage/sessions/{child_session['id']}")
    assert response.json() == {"ok": True, "id": child_session["id"], "current": False}
    assert worker.get("/api/manage/sessions").status_code == 401


def test_current_session_revocation_returns_success_then_rejects_requests(owner):
    grant, token = link(owner, product(owner))
    handler = staff_client(token)
    current = handler.get("/api/manage/sessions").json()[0]
    response = handler.delete(f"/api/manage/sessions/{current['id']}")
    assert response.json() == {"ok": True, "id": current["id"], "current": True}
    assert handler.get("/api/manage/sessions").status_code == 401
    with db() as c:
        row = c.execute(
            "SELECT * FROM sessions WHERE id=?", (current["id"],)
        ).fetchone()
        assert row["revoked"] == 1 and row["staff_id"] == grant["id"]
        assert (
            c.execute(
                "SELECT count(*) FROM audit WHERE action='session.revoke' AND target=?",
                (current["id"],),
            ).fetchone()[0]
            == 1
        )


def test_logout_retains_revoked_session_and_audit(owner):
    before = next(
        row for row in owner.get("/api/admin/sessions").json() if row["current"]
    )
    assert owner.post("/api/auth/logout", json={}).status_code == 200
    assert owner.get("/api/admin/sessions").status_code == 401
    with db() as c:
        assert (
            c.execute(
                "SELECT revoked FROM sessions WHERE id=?", (before["id"],)
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute(
                "SELECT count(*) FROM audit WHERE action='session.logout' AND target=?",
                (before["id"],),
            ).fetchone()[0]
            == 1
        )


def test_session_seen_throttles_updates_without_audit_noise(owner):
    value = owner.cookies.get("extore_session")
    with db() as c:
        c.execute(
            "UPDATE sessions SET last_seen=? WHERE digest=?",
            (time.time() - 31, digest(value)),
        )
    assert owner.get("/api/admin/sessions").status_code == 200
    with db() as c:
        first = c.execute(
            "SELECT last_seen FROM sessions WHERE digest=?", (digest(value),)
        ).fetchone()[0]
        events = c.execute("SELECT count(*) FROM audit").fetchone()[0]
    assert owner.get("/api/admin/sessions").status_code == 200
    with db() as c:
        assert (
            c.execute(
                "SELECT last_seen FROM sessions WHERE digest=?", (digest(value),)
            ).fetchone()[0]
            == first
        )
        assert c.execute("SELECT count(*) FROM audit").fetchone()[0] == events


def test_cleanup_keeps_recent_expired_sessions_for_ninety_days(owner):
    with db() as c:
        recent = create_session(c, Response(), "admin")
        old = create_session(c, Response(), "admin")
        c.execute(
            "UPDATE sessions SET expires=? WHERE digest=?",
            (time.time() - 89 * 86400, digest(recent)),
        )
        c.execute(
            "UPDATE sessions SET expires=? WHERE digest=?",
            (time.time() - 91 * 86400, digest(old)),
        )
        create_session(c, Response(), "admin")
        assert c.execute(
            "SELECT 1 FROM sessions WHERE digest=?", (digest(recent),)
        ).fetchone()
        assert not c.execute(
            "SELECT 1 FROM sessions WHERE digest=?", (digest(old),)
        ).fetchone()


def test_audit_scope_is_safe_and_excludes_other_links_and_business_payloads(owner):
    first, first_token = link(owner, product(owner))
    second, second_token = link(owner, product(owner))
    handler = staff_client(first_token)
    staff_client(second_token)
    with db() as c:
        audit(c, "owner", "product.update", "DO-NOT-EXPOSE-PAYLOAD")
        audit(c, "owner", "session.revoke", "DO-NOT-EXPOSE-COOKIE")
    response = handler.get("/api/manage/audit")
    assert response.status_code == 200
    assert response.json()
    assert second["id"] not in response.text
    for secret in (
        first_token,
        second_token,
        "DO-NOT-EXPOSE-PAYLOAD",
        "DO-NOT-EXPOSE-COOKIE",
    ):
        assert secret not in response.text
    assert any(
        row["action"] == "link.consume" and row["target"] == first["id"]
        for row in response.json()
    )
    assert owner.get("/api/admin/audit?limit=201").status_code == 422


def test_revocation_is_rechecked_inside_management_transaction(owner):
    from starlette.requests import Request

    value = owner.cookies.get("extore_session")
    request = Request(
        {"type": "http", "headers": [(b"cookie", f"extore_session={value}".encode())]}
    )
    authenticated = session(request)
    with db() as c:
        revoke_session(c, digest(value))
    from extore.security import authorize_management

    with db() as c, pytest.raises(HTTPException) as error:
        authorize_management(c, authenticated)
    assert error.value.status_code == 401


def test_last_staff_session_release_preserves_queue_drafts_and_progress(
    owner, setup_product
):
    pid, code = setup_product()
    grant, _ = link(owner, pid, max_uses=2)
    exchange = owner.post("/api/exchange", json={"code": code}).json()
    submitted = owner.post(
        "/api/redeem",
        json={"token": exchange["token"], "params": {"email": "customer@example.test"}},
    )
    assert submitted.status_code == 200, submitted.text
    jid = submitted.json()["id"]
    with db() as c:
        first = create_session(c, Response(), "staff", grant["id"])
        second = create_session(c, Response(), "staff", grant["id"])
        c.execute(
            "UPDATE jobs SET state='processing',claimed_by=?,lease=?,progress=50,"
            "progress_plan=?,completed_steps=?,result_json=? WHERE id=?",
            (
                grant["id"],
                time.time() + 300,
                '[{"id":"first"}]',
                '["first"]',
                json.dumps({"draft": "retain"}),
                jid,
            ),
        )
        revoke_session(c, digest(first))
        assert (
            c.execute("SELECT state FROM jobs WHERE id=?", (jid,)).fetchone()[0]
            == "processing"
        )
        revoke_session(c, digest(second))
        job = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        assert (
            job["state"] == "queued"
            and job["claimed_by"] is None
            and job["lease"] is None
        )
        assert job["progress"] == 50
        assert job["completed_steps"] == '["first"]'
        assert job["result_json"] == '{"draft": "retain"}'
        assert json.loads(job["params"]) == {"email": "customer@example.test"}

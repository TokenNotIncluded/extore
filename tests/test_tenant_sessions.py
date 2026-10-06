"""Merchant sessions cannot inspect, revoke, or expand another shop's authority."""

import json
import time
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from extore.app import app
from extore.db import audit, db
from extore.link_access import revoke_session
from extore.security import authorize_management, create_session, digest, session


def new_id():
    return str(uuid.uuid4())


def request_for(cookie, path="/api/admin/sessions"):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [(b"cookie", f"extore_session={cookie}".encode())],
            "query_string": b"",
            "server": ("localhost", 8000),
            "scheme": "http",
        }
    )


@pytest.fixture
def tenants():
    now = time.time()
    records = []
    clients = []
    with db() as c:
        for name in ("A", "B"):
            shop, product, link, device = (new_id() for _ in range(4))
            c.execute(
                "INSERT INTO shops(id,name,email,created,verified) VALUES (?,?,?,?,1)",
                (shop, name, f"{name.lower()}-{shop}@example.test", now),
            )
            c.execute(
                "INSERT INTO products(id,config,created,shop_id) VALUES (?,?,?,?)",
                (
                    product,
                    json.dumps({"name": name, "mode": "manual", "parameters": []}),
                    now,
                    shop,
                ),
            )
            c.execute(
                "INSERT INTO staff(id,digest,product_id,name,expires,created,permissions) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    link,
                    digest(new_id()),
                    product,
                    name,
                    now + 3600,
                    now,
                    '["queue.view","queue.process"]',
                ),
            )
            c.execute(
                "INSERT INTO cli_devices(id,staff_id,public_key,fingerprint,client_name,created,last_seen) "
                "VALUES (?,?,?,?,?,?,?)",
                (device, link, new_id(), new_id(), name, now, now),
            )
            owner_cookie = create_session(c, Response(), "admin", shop_id=shop)
            staff_cookie = create_session(c, Response(), "staff", link)
            owner_session = c.execute(
                "SELECT id FROM sessions WHERE digest=?", (digest(owner_cookie),)
            ).fetchone()["id"]
            staff_session = c.execute(
                "SELECT id FROM sessions WHERE digest=?", (digest(staff_cookie),)
            ).fetchone()["id"]
            audit(c, link, "cli.device.create", device)
            records.append(
                {
                    "shop": shop,
                    "product": product,
                    "link": link,
                    "device": device,
                    "owner_cookie": owner_cookie,
                    "staff_cookie": staff_cookie,
                    "owner_session": owner_session,
                    "staff_session": staff_session,
                }
            )
    for record in records:
        client = TestClient(
            app,
            base_url="http://localhost:8000",
            headers={"Origin": "http://localhost:8000"},
        )
        client.cookies.set("extore_session", record["owner_cookie"])
        record["client"] = client
        clients.append(client)
    yield records
    for client in clients:
        client.close()


def test_owner_session_list_includes_only_own_owner_and_staff(tenants, owner):
    first, second = tenants
    response = first["client"].get("/api/admin/sessions")
    assert response.status_code == 200, response.text
    assert {row["id"] for row in response.json()} == {
        first["owner_session"],
        first["staff_session"],
    }
    assert all(row["shop_id"] == first["shop"] for row in response.json())
    root_rows = owner.get("/api/admin/sessions").json()
    assert {first["owner_session"], second["owner_session"]} <= {
        row["id"] for row in root_rows
    }


@pytest.mark.parametrize("kind", ("owner_session", "staff_session"))
def test_owner_cannot_revoke_another_shop_session(tenants, kind):
    first, second = tenants
    response = first["client"].delete(f"/api/admin/sessions/{second[kind]}")
    assert response.status_code == 404, response.text
    with db() as c:
        assert (
            c.execute(
                "SELECT revoked FROM sessions WHERE id=?", (second[kind],)
            ).fetchone()[0]
            == 0
        )
    assert second["client"].get("/api/admin/sessions").status_code == 200


def test_owner_can_revoke_own_staff_session_only(tenants):
    first, second = tenants
    response = first["client"].delete(f"/api/admin/sessions/{first['staff_session']}")
    assert response.status_code == 200, response.text
    with db() as c:
        assert (
            c.execute(
                "SELECT revoked FROM sessions WHERE id=?", (first["staff_session"],)
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute(
                "SELECT revoked FROM sessions WHERE id=?", (second["staff_session"],)
            ).fetchone()[0]
            == 0
        )


def test_device_list_and_revoke_do_not_cross_shop_boundary(tenants):
    first, second = tenants
    response = first["client"].get("/api/admin/cli-devices")
    assert response.status_code == 200, response.text
    assert {row["id"] for row in response.json()} == {first["device"]}
    response = first["client"].delete(f"/api/admin/cli-devices/{second['device']}")
    assert response.status_code == 404, response.text
    with db() as c:
        assert (
            c.execute(
                "SELECT revoked FROM cli_devices WHERE id=?", (second["device"],)
            ).fetchone()[0]
            == 0
        )


def test_merchant_audit_contains_only_own_retained_targets(tenants):
    first, second = tenants
    first["client"].delete(f"/api/admin/sessions/{first['staff_session']}")
    response = first["client"].get("/api/admin/audit")
    assert response.status_code == 200, response.text
    targets = {row["target"] for row in response.json()}
    assert first["owner_session"] in targets
    assert first["staff_session"] in targets
    assert first["device"] in targets
    assert not targets & {
        second["owner_session"],
        second["staff_session"],
        second["device"],
        second["link"],
    }
    assert any(row["actor"] == f"shop:{first['shop']}" for row in response.json())


@pytest.mark.parametrize("role", ("admin", "staff"))
def test_disabling_shop_invalidates_existing_browser_sessions(tenants, role):
    first, second = tenants
    cookie = first[f"{'owner' if role == 'admin' else 'staff'}_cookie"]
    with db() as c:
        c.execute("UPDATE shops SET enabled=0 WHERE id=?", (first["shop"],))
    with pytest.raises(HTTPException) as error:
        session(request_for(cookie), (role,))
    assert error.value.status_code == 401
    assert second["client"].get("/api/admin/sessions").status_code == 200


def test_saved_session_dictionary_cannot_change_its_shop_or_survive_shop_reassignment(
    tenants,
):
    first, second = tenants
    authenticated = session(request_for(first["owner_cookie"]))
    with db() as c:
        forged = {**authenticated, "shop_id": second["shop"]}
        with pytest.raises(HTTPException) as error:
            authorize_management(c, forged)
        assert error.value.status_code == 401
        c.execute(
            "UPDATE sessions SET shop_id=? WHERE digest=?",
            (second["shop"], authenticated["digest"]),
        )
        with pytest.raises(HTTPException) as error:
            authorize_management(c, authenticated)
        assert error.value.status_code == 401


def test_staff_session_cannot_point_to_another_shop(tenants):
    first, second = tenants
    with db() as c:
        c.execute(
            "UPDATE sessions SET shop_id=? WHERE digest=?",
            (second["shop"], digest(first["staff_cookie"])),
        )
    with pytest.raises(HTTPException) as error:
        session(request_for(first["staff_cookie"]), ("staff",))
    assert error.value.status_code == 401


def test_shop_session_exposes_stable_account_actor_without_email(tenants):
    first, _ = tenants
    authenticated = session(request_for(first["owner_cookie"]))
    assert authenticated["shop_id"] == first["shop"]
    assert authenticated["account_id"] == f"shop:{first['shop']}"
    assert "@" not in authenticated["account_id"]


def test_last_shop_session_release_preserves_other_shop_task(tenants):
    first, second = tenants
    now = time.time()
    job_ids = []
    with db() as c:
        remaining_cookie = create_session(c, Response(), "admin", shop_id=first["shop"])
        for record in tenants:
            card_id, job_id = new_id(), new_id()
            c.execute(
                "INSERT INTO cards(id,digest,product_id,created) VALUES (?,?,?,?)",
                (card_id, digest(new_id()), record["product"], now),
            )
            c.execute(
                "INSERT INTO jobs(id,card_id,product_id,state,params,claimed_by,lease,progress,result_json,created,updated) VALUES (?,?,?,'processing',?,?,?,?,?,?,?)",
                (
                    job_id,
                    card_id,
                    record["product"],
                    '{"request":"keep"}',
                    f"shop:{record['shop']}",
                    now + 300,
                    75,
                    '{"draft":"retain"}',
                    now,
                    now,
                ),
            )
            job_ids.append(job_id)
        revoke_session(c, digest(first["owner_cookie"]))
        assert (
            c.execute("SELECT state FROM jobs WHERE id=?", (job_ids[0],)).fetchone()[0]
            == "processing"
        )
        revoke_session(c, digest(remaining_cookie))
        own_job = c.execute("SELECT * FROM jobs WHERE id=?", (job_ids[0],)).fetchone()
        assert own_job["state"] == "queued" and own_job["claimed_by"] is None
        assert (
            own_job["progress"] == 75 and own_job["result_json"] == '{"draft":"retain"}'
        )
        assert own_job["params"] == '{"request":"keep"}'
        other = c.execute("SELECT * FROM jobs WHERE id=?", (job_ids[1],)).fetchone()
        assert (
            other["state"] == "processing"
            and other["claimed_by"] == f"shop:{second['shop']}"
        )

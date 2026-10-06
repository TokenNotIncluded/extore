"""Record retention cannot erase live work or cross a shop's authority."""

import json
import sqlite3
import time
import uuid
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response
from test_tenant_files import domain

from extore.app import app
from extore.config import ORIGIN
from extore.db import audit, db
from extore.security import create_session, digest, token

DAY = 86400
POLICY = {
    "enabled": True,
    "event_retention_days": 30,
    "dead_letter_retention_days": 90,
    "audit_retention_days": 180,
    "link_retention_days": 90,
}


def identifier():
    return str(uuid.uuid4())


def event(c, product_id, *, created, state=None, finished=None, job_id=None):
    eid = identifier()
    c.execute(
        "INSERT INTO events(id,type,job_id,product_id,payload,created) VALUES (?,?,?,?,?,?)",
        (
            eid,
            "redemption.requested",
            job_id,
            product_id,
            json.dumps({"id": eid}),
            created,
        ),
    )
    if state is not None:
        c.execute(
            "INSERT INTO outbox(id,url,secret,due,state,finished_at) VALUES (?,?,?,?,?,?)",
            (
                eid,
                "https://example.com/callback",
                "unrelated-secret",
                created,
                state,
                finished,
            ),
        )
    return eid


def audit_row(c, shop_id, *, created, target="not-a-known-resource"):
    return c.execute(
        "INSERT INTO audit(actor,action,target,created,shop_id) VALUES (?,?,?,?,?)",
        (
            f"shop:{shop_id}" if shop_id else "legacy",
            "test.retention",
            target,
            created,
            shop_id,
        ),
    ).lastrowid


def records(c, table):
    return tuple(
        tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid")
    )


def work_fingerprint(c):
    return {
        table: records(c, table)
        for table in ("products", "cards", "jobs", "job_files", "grants")
    }


def login(stack, *, shop_id=None, auth_at=None, role="admin", staff_id=None):
    client = stack.enter_context(
        TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
    )
    with db() as c:
        credential = create_session(
            c,
            Response(),
            role,
            staff_id,
            shop_id=shop_id,
            auth_at=auth_at,
            auth_method="passkey",
        )
    client.cookies.set("extore_session", credential)
    return client, credential


@pytest.fixture
def tenants():
    with db() as c:
        a, b = domain(c, "保留策略甲"), domain(c, "保留策略乙")
    with ExitStack() as stack:
        root, _ = login(stack)
        client_a, credential_a = login(stack, shop_id=a["shop_id"])
        client_b, _ = login(stack, shop_id=b["shop_id"])
        yield a, b, root, client_a, client_b, credential_a


def cleanup(c, **kwargs):
    from extore.maintenance import cleanup_records

    return cleanup_records(c, policy=POLICY, **kwargs)


def test_event_age_uses_terminal_finish_and_never_removes_pending_or_unknown(tenants):
    a, _, _, _, _, _ = tenants
    now = time.time()
    with db() as c:
        removed = {
            event(c, a["product_id"], created=now - 40 * DAY),
            event(
                c,
                a["product_id"],
                created=now - 400 * DAY,
                state="delivered",
                finished=now - 40 * DAY,
            ),
            event(
                c,
                a["product_id"],
                created=now - 400 * DAY,
                state="cancelled",
                finished=now - 40 * DAY,
            ),
            event(
                c,
                a["product_id"],
                created=now - 400 * DAY,
                state="dead",
                finished=now - 100 * DAY,
            ),
        }
        retained = {
            event(c, a["product_id"], created=now - 2 * DAY),
            event(
                c,
                a["product_id"],
                created=now - 400 * DAY,
                state="delivered",
                finished=now - 2 * DAY,
            ),
            event(
                c,
                a["product_id"],
                created=now - 400 * DAY,
                state="dead",
                finished=now - 40 * DAY,
            ),
            event(
                c,
                a["product_id"],
                created=now - 400 * DAY,
                state="pending",
                finished=now - 400 * DAY,
            ),
            event(
                c,
                a["product_id"],
                created=now - 400 * DAY,
                state="unknown-new-state",
                finished=now - 400 * DAY,
            ),
            event(
                c,
                a["product_id"],
                created=now - 400 * DAY,
                state="delivered",
                finished=None,
            ),
        }
        before = work_fingerprint(c)
        result = cleanup(
            c, shop_id=a["shop_id"], areas=("events",), dry_run=False, now=now
        )
        assert result["changed"]["events"] == len(removed)
        assert {row[0] for row in c.execute("SELECT id FROM events")} == retained
        assert not {row[0] for row in c.execute("SELECT id FROM outbox")} & removed
        assert work_fingerprint(c) == before


def test_dry_run_preserves_events_outbox_audit_and_work(tenants):
    a, _, _, _, _, _ = tenants
    now = time.time()
    with db() as c:
        event(
            c,
            a["product_id"],
            created=now - 400 * DAY,
            state="delivered",
            finished=now - 40 * DAY,
        )
        audit_row(c, a["shop_id"], created=now - 200 * DAY, target=a["product_id"])
        before = {
            table: records(c, table)
            for table in ("events", "outbox", "audit", "settings")
        }
        work = work_fingerprint(c)
        result = cleanup(c, shop_id=a["shop_id"], areas=("events", "audit"), now=now)
        assert result["dry_run"] is True
        assert result["eligible"]["events"] == 1
        assert result["eligible"]["audit"] == 1
        assert result["changed"]["events"] == result["changed"]["audit"] == 0
        assert {table: records(c, table) for table in before} == before
        assert work_fingerprint(c) == work


def test_applied_cleanup_is_bounded_and_reports_more(tenants):
    a, _, _, _, _, _ = tenants
    now = time.time()
    with db() as c:
        for _ in range(205):
            event(c, a["product_id"], created=now - 40 * DAY)
        for expected, remaining, more in (
            (100, 105, True),
            (100, 5, True),
            (5, 0, False),
        ):
            result = cleanup(
                c,
                shop_id=a["shop_id"],
                areas=("events",),
                dry_run=False,
                limit=100,
                now=now,
            )
            assert result["changed"]["events"] == expected
            assert result["has_more"]["events"] is more
            assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == remaining


def test_shop_cleanup_cannot_touch_other_shop_or_unknown_legacy_audit(tenants):
    a, b, _, client_a, _, _ = tenants
    now = time.time()
    with db() as c:
        ea = event(c, a["product_id"], created=now - 40 * DAY, job_id=a["job_id"])
        eb = event(c, b["product_id"], created=now - 40 * DAY, job_id=b["job_id"])
        aa = audit_row(c, a["shop_id"], created=now - 200 * DAY, target=a["product_id"])
        ab = audit_row(c, b["shop_id"], created=now - 200 * DAY, target=b["product_id"])
        unknown = audit_row(c, None, created=now - 200 * DAY)
        work = work_fingerprint(c)
    preview = client_a.post(
        "/api/admin/maintenance/cleanup", json={"areas": ["events", "audit"]}
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["dry_run"] is True
    assert (
        preview.json()["eligible"]["events"] == preview.json()["eligible"]["audit"] == 1
    )
    response = client_a.post(
        "/api/admin/maintenance/cleanup",
        json={"areas": ["events", "audit"], "dry_run": False},
    )
    assert response.status_code == 200, response.text
    assert (
        response.json()["changed"]["events"] == response.json()["changed"]["audit"] == 1
    )
    with db() as c:
        assert c.execute("SELECT 1 FROM events WHERE id=?", (ea,)).fetchone() is None
        assert c.execute("SELECT 1 FROM events WHERE id=?", (eb,)).fetchone()
        assert c.execute("SELECT 1 FROM audit WHERE id=?", (aa,)).fetchone() is None
        assert c.execute("SELECT 1 FROM audit WHERE id=?", (ab,)).fetchone()
        assert c.execute("SELECT 1 FROM audit WHERE id=?", (unknown,)).fetchone()
        assert work_fingerprint(c) == work


def test_product_cleanup_keeps_other_products_and_unattributed_audit(tenants):
    a, _, _, client_a, _, _ = tenants
    now = time.time()
    with db() as c:
        other = domain(c, "甲店另一个商品")
        c.execute(
            "UPDATE products SET shop_id=? WHERE id=?",
            (a["shop_id"], other["product_id"]),
        )
        selected = event(c, a["product_id"], created=now - 40 * DAY)
        untouched = event(c, other["product_id"], created=now - 40 * DAY)
        removable_audit = {
            audit_row(c, a["shop_id"], created=now - 200 * DAY, target=a[key])
            for key in ("product_id", "job_id", "card_id")
        }
        untouched_audit = audit_row(
            c, a["shop_id"], created=now - 200 * DAY, target=other["product_id"]
        )
        unknown = audit_row(c, a["shop_id"], created=now - 200 * DAY)
    response = client_a.post(
        "/api/admin/maintenance/cleanup",
        json={
            "areas": ["events", "audit"],
            "product_id": a["product_id"],
            "dry_run": False,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["changed"]["events"] == 1
    assert response.json()["changed"]["audit"] == len(removable_audit)
    with db() as c:
        assert (
            c.execute("SELECT 1 FROM events WHERE id=?", (selected,)).fetchone() is None
        )
        assert c.execute("SELECT 1 FROM events WHERE id=?", (untouched,)).fetchone()
        assert (
            not {row[0] for row in c.execute("SELECT id FROM audit")} & removable_audit
        )
        assert c.execute(
            "SELECT 1 FROM audit WHERE id=?", (untouched_audit,)
        ).fetchone()
        assert c.execute("SELECT 1 FROM audit WHERE id=?", (unknown,)).fetchone()


def test_maintenance_counts_reveal_only_selected_shop(tenants):
    a, b, root, client_a, client_b, _ = tenants
    now = time.time()
    with db() as c:
        for index in range(3):
            event(
                c, a["product_id"], created=now, state="pending" if index == 0 else None
            )
        for index in range(5):
            event(
                c, b["product_id"], created=now, state="pending" if index < 2 else None
            )
        for _ in range(2):
            audit_row(c, a["shop_id"], created=now)
        audit_row(c, b["shop_id"], created=now)
        audit_row(c, None, created=now)
    for client, expected_events, expected_pending, expected_audit in (
        (client_a, 3, 1, 3),
        (client_b, 5, 2, 2),
        (root, 8, 3, 7),
    ):
        response = client.get("/api/admin/maintenance")
        assert response.status_code == 200, response.text
        counts = response.json()["counts"]
        assert counts["events_total"] == expected_events
        assert counts["events_pending"] == expected_pending
        assert counts["audit_total"] == expected_audit
        assert "unrelated-secret" not in response.text
    root_selected = root.get("/api/admin/maintenance", params={"shop_id": a["shop_id"]})
    assert root_selected.status_code == 200, root_selected.text
    assert (
        root_selected.json()["counts"]
        == client_a.get("/api/admin/maintenance").json()["counts"]
    )


@pytest.mark.parametrize(
    "method,path",
    (
        ("GET", "/api/admin/maintenance"),
        ("POST", "/api/admin/maintenance/cleanup"),
        ("PUT", "/api/admin/maintenance/policy"),
    ),
)
def test_merchant_cannot_select_foreign_shop(tenants, method, path):
    _, b, _, client_a, _, _ = tenants
    kwargs = (
        {"params": {"shop_id": b["shop_id"]}}
        if method == "GET"
        else {
            "json": {
                "shop_id": b["shop_id"],
                **(
                    POLICY
                    if method == "PUT"
                    else {"areas": ["events"], "dry_run": False}
                ),
            }
        }
    )
    response = client_a.request(method, path, **kwargs)
    assert response.status_code == 403, response.text


def test_cleanup_needs_recent_auth_only_for_actual_writes(tenants):
    a, _, _, client_a, _, credential = tenants
    with db() as c:
        eid = event(c, a["product_id"], created=time.time() - 40 * DAY)
        c.execute(
            "UPDATE sessions SET auth_at=? WHERE digest=?",
            (time.time() - 601, digest(credential)),
        )
    preview = client_a.post(
        "/api/admin/maintenance/cleanup", json={"areas": ["events"], "dry_run": True}
    )
    assert preview.status_code == 200, preview.text
    denied = client_a.post(
        "/api/admin/maintenance/cleanup", json={"areas": ["events"], "dry_run": False}
    )
    assert denied.status_code == 401, denied.text
    denied = client_a.put("/api/admin/maintenance/policy", json=POLICY)
    assert denied.status_code == 401, denied.text
    with db() as c:
        assert c.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone()


@pytest.mark.parametrize(
    "payload",
    (
        {"areas": ["events"], "limit": 0},
        {"areas": ["events"], "limit": 501},
        {"areas": ["events"], "limit": "100"},
        {"areas": ["unrecognized"]},
    ),
)
def test_invalid_cleanup_batch_cannot_change_data(tenants, payload):
    a, _, _, client_a, _, _ = tenants
    with db() as c:
        eid = event(c, a["product_id"], created=time.time() - 40 * DAY)
    denied = client_a.post(
        "/api/admin/maintenance/cleanup", json={"dry_run": False, **payload}
    )
    assert denied.status_code == 422, denied.text
    with db() as c:
        assert c.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone()


@pytest.mark.parametrize("value", ("false", 0))
def test_cleanup_flags_cannot_coerce_preview_into_destructive_apply(tenants, value):
    a, _, _, client_a, _, _ = tenants
    with db() as c:
        eid = event(c, a["product_id"], created=time.time() - 40 * DAY)
    denied = client_a.post(
        "/api/admin/maintenance/cleanup",
        json={"areas": ["events"], "dry_run": value},
    )
    assert denied.status_code == 422, denied.text
    with db() as c:
        assert c.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone()


@pytest.mark.parametrize("value", ("false", 0))
def test_policy_enabled_requires_an_explicit_boolean(tenants, value):
    _, _, root, _, _, _ = tenants
    denied = root.put(
        "/api/admin/maintenance/policy", json={**POLICY, "enabled": value}
    )
    assert denied.status_code == 422, denied.text


def test_maintenance_endpoints_reject_staff_and_anonymous(tenants):
    a, _, _, _, _, _ = tenants
    with db() as c:
        sid = identifier()
        c.execute(
            "INSERT INTO staff(id,digest,product_id,name,expires) VALUES (?,?,?,?,?)",
            (sid, digest(token()), a["product_id"], "仅商品管理", time.time() + DAY),
        )
    with ExitStack() as stack:
        staff, _ = login(stack, role="staff", staff_id=sid)
        anonymous = stack.enter_context(
            TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})
        )
        for client in (staff, anonymous):
            assert client.get("/api/admin/maintenance").status_code == 401
            assert (
                client.post(
                    "/api/admin/maintenance/cleanup",
                    json={"areas": ["events"], "dry_run": False},
                ).status_code
                == 401
            )
            assert (
                client.put("/api/admin/maintenance/policy", json=POLICY).status_code
                == 401
            )


@pytest.mark.parametrize(
    "field,value",
    (
        ("event_retention_days", 0),
        ("dead_letter_retention_days", 0),
        ("link_retention_days", 0),
        ("audit_retention_days", 89),
        ("event_retention_days", 3651),
        ("dead_letter_retention_days", 3651),
        ("link_retention_days", 3651),
        ("audit_retention_days", 3651),
    ),
)
def test_policy_rejects_unsafe_or_unbounded_retention(tenants, field, value):
    _, _, root, _, _, _ = tenants
    response = root.put("/api/admin/maintenance/policy", json={**POLICY, field: value})
    assert response.status_code == 422, response.text


def test_root_policy_is_inherited_until_shop_sets_its_own(tenants):
    _, _, root, client_a, client_b, _ = tenants
    inherited = {
        **POLICY,
        "event_retention_days": 10,
        "dead_letter_retention_days": 20,
        "audit_retention_days": 120,
        "link_retention_days": 60,
    }
    response = root.put("/api/admin/maintenance/policy", json=inherited)
    assert response.status_code == 200, response.text
    for client in (root, client_a, client_b):
        response = client.get("/api/admin/maintenance")
        assert response.status_code == 200, response.text
        assert response.json()["policy"] == inherited
    response = client_a.put("/api/admin/maintenance/policy", json=POLICY)
    assert response.status_code == 200, response.text
    assert client_a.get("/api/admin/maintenance").json()["policy"] == POLICY
    assert client_b.get("/api/admin/maintenance").json()["policy"] == inherited
    assert root.get("/api/admin/maintenance").json()["policy"] == inherited


def test_root_product_cleanup_honors_the_shops_retention_override(tenants):
    a, _, root, client_a, _, _ = tenants
    assert (
        root.put(
            "/api/admin/maintenance/policy", json={**POLICY, "event_retention_days": 10}
        ).status_code
        == 200
    )
    assert client_a.put("/api/admin/maintenance/policy", json=POLICY).status_code == 200
    with db() as c:
        eid = event(c, a["product_id"], created=time.time() - 20 * DAY)
    response = root.post(
        "/api/admin/maintenance/cleanup",
        json={"areas": ["events"], "product_id": a["product_id"], "dry_run": False},
    )
    assert response.status_code == 200, response.text
    assert response.json()["shop_id"] == a["shop_id"]
    assert response.json()["policy"]["event_retention_days"] == 30
    assert response.json()["changed"]["events"] == 0
    with db() as c:
        assert c.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone()


def test_root_can_clean_unknown_legacy_audit(tenants):
    _, _, root, _, _, _ = tenants
    with db() as c:
        unknown = audit_row(c, None, created=time.time() - 200 * DAY)
    response = root.post(
        "/api/admin/maintenance/cleanup", json={"areas": ["audit"], "dry_run": False}
    )
    assert response.status_code == 200, response.text
    assert response.json()["changed"]["audit"] == 1
    with db() as c:
        assert (
            c.execute("SELECT 1 FROM audit WHERE id=?", (unknown,)).fetchone() is None
        )


def test_new_audit_ids_remain_monotonic_after_deleting_highest_record(tenants):
    _, _, root, _, _, _ = tenants
    with db() as c:
        deleted = audit_row(c, None, created=time.time() - 200 * DAY)
    response = root.post(
        "/api/admin/maintenance/cleanup", json={"areas": ["audit"], "dry_run": False}
    )
    assert response.status_code == 200, response.text
    with db() as c:
        cleanup_id = c.execute(
            "SELECT id FROM audit WHERE action='maintenance.cleanup' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        assert cleanup_id > deleted
        audit(c, "owner", "test.after-cleanup", "safe")
        next_id = c.execute(
            "SELECT id FROM audit WHERE action='test.after-cleanup'"
        ).fetchone()[0]
        assert next_id > cleanup_id


def test_legacy_terminal_rows_receive_conservative_migration_age_once():
    from extore.maintenance import init_schema

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    try:
        c.executescript(
            "CREATE TABLE outbox(id TEXT PRIMARY KEY,state TEXT NOT NULL);"
            "CREATE TABLE audit(id INTEGER PRIMARY KEY,actor TEXT,action TEXT,target TEXT,created REAL);"
            "CREATE TABLE staff(id TEXT PRIMARY KEY,product_id TEXT,created REAL);"
            "CREATE TABLE events(id TEXT PRIMARY KEY,created REAL,product_id TEXT);"
        )
        c.executemany(
            "INSERT INTO outbox(id,state) VALUES (?,?)",
            (
                ("delivered", "delivered"),
                ("cancelled", "cancelled"),
                ("dead", "dead"),
                ("pending", "pending"),
            ),
        )
        before = time.time()
        init_schema(c)
        first = {
            row["id"]: row["finished_at"] for row in c.execute("SELECT * FROM outbox")
        }
        assert all(
            before <= first[key] <= time.time()
            for key in ("delivered", "cancelled", "dead")
        )
        assert first["pending"] is None
        c.execute(
            "INSERT INTO outbox(id,state,finished_at) VALUES ('missing-finish','delivered',NULL)"
        )
        init_schema(c)
        assert {
            row["id"]: row["finished_at"] for row in c.execute("SELECT * FROM outbox")
        } == {**first, "missing-finish": None}
        assert "shop_id" in {
            row["name"] for row in c.execute("PRAGMA table_info(audit)")
        }
    finally:
        c.close()

"""Automatic retention stays bounded, honors policy, and preserves live work."""

import json
import time

import pytest
from fastapi import HTTPException
from test_link_cleanup import claimed_task, link
from test_maintenance_events import DAY, POLICY, audit_row, event, work_fingerprint
from test_tenant_files import domain

from extore.db import db, set_setting, setting
from extore.link_cleanup import cleanup_links
from extore.maintenance import maintenance_once
from extore.security import staff_authorization


def test_automatic_cleanup_honors_shop_disable_and_keeps_pending_and_live_work():
    now = time.time()
    with db() as c:
        first, second = domain(c, "自动保留甲"), domain(c, "自动保留乙")
        set_setting(
            c,
            f"maintenance_policy:{first['shop_id']}",
            json.dumps({**POLICY, "enabled": False}),
        )
        first_event = event(c, first["product_id"], created=now - 40 * DAY)
        second_event = event(c, second["product_id"], created=now - 40 * DAY)
        pending = event(
            c, second["product_id"], created=now - 400 * DAY, state="pending"
        )
        first_audit = audit_row(c, first["shop_id"], created=now - 200 * DAY)
        second_audit = audit_row(c, second["shop_id"], created=now - 200 * DAY)
        unknown_audit = audit_row(c, None, created=now - 200 * DAY)
        first_link = link(c, first["product_id"], expires=now - 100 * DAY)
        second_link = link(c, second["product_id"], expires=now - 100 * DAY)
        recent_link = link(c, second["product_id"], expires=now - 5 * DAY)
        work = work_fingerprint(c)
    result = maintenance_once()
    assert result == {"links": 1, "events": 1, "audit": 2}
    with db() as c:
        assert {row["id"] for row in c.execute("SELECT id FROM events")} == {
            first_event,
            pending,
        }
        assert (
            c.execute("SELECT 1 FROM events WHERE id=?", (second_event,)).fetchone()
            is None
        )
        assert c.execute("SELECT 1 FROM audit WHERE id=?", (first_audit,)).fetchone()
        assert (
            c.execute(
                "SELECT 1 FROM audit WHERE id IN (?,?)", (second_audit, unknown_audit)
            ).fetchone()
            is None
        )
        assert (
            c.execute(
                "SELECT archived FROM staff WHERE id=?", (second_link,)
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute(
                "SELECT archived FROM staff WHERE id=?", (first_link,)
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT archived FROM staff WHERE id=?", (recent_link,)
            ).fetchone()[0]
            == 0
        )
        assert work_fingerprint(c) == work


def test_platform_disabling_automatic_retention_cannot_be_overridden_by_shop():
    now = time.time()
    with db() as c:
        shop = domain(c, "平台暂停清理")
        set_setting(
            c, "maintenance_policy:platform", json.dumps({**POLICY, "enabled": False})
        )
        set_setting(c, f"maintenance_policy:{shop['shop_id']}", json.dumps(POLICY))
        eid = event(c, shop["product_id"], created=now - 40 * DAY)
        aid = audit_row(c, None, created=now - 200 * DAY)
        sid = link(c, shop["product_id"], expires=now - 100 * DAY)
    assert maintenance_once() == {"links": 0, "events": 0, "audit": 0}
    with db() as c:
        assert c.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone()
        assert c.execute("SELECT 1 FROM audit WHERE id=?", (aid,)).fetchone()
        assert (
            c.execute("SELECT archived FROM staff WHERE id=?", (sid,)).fetchone()[0]
            == 0
        )


def test_worker_cursor_reaches_shops_after_the_first_twenty():
    now = time.time()
    with db() as c:
        events_by_shop = {}
        for index in range(25):
            shop = domain(c, f"公平清理 {index}")
            events_by_shop[shop["shop_id"]] = event(
                c, shop["product_id"], created=now - 40 * DAY
            )
        shops = [row["id"] for row in c.execute("SELECT id FROM shops ORDER BY id")]
        expected_first = len(set(shops[:20]) & events_by_shop.keys())
    assert maintenance_once()["events"] == expected_first
    with db() as c:
        assert setting(c, "maintenance_cursor") == shops[19]
        remaining = {row["id"] for row in c.execute("SELECT id FROM events")}
        assert remaining == {
            eid for sid, eid in events_by_shop.items() if sid not in shops[:20]
        }
    assert maintenance_once()["events"] == 25 - expected_first
    with db() as c:
        assert c.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert setting(c, "maintenance_cursor") == ""


def test_each_worker_tick_deletes_at_most_one_hundred_events_per_shop():
    now = time.time()
    with db() as c:
        shop = domain(c, "大批事件清理")
        for _ in range(205):
            event(c, shop["product_id"], created=now - 40 * DAY)
        work = work_fingerprint(c)
    assert maintenance_once()["events"] == 100
    assert maintenance_once()["events"] == 100
    assert maintenance_once()["events"] == 5
    with db() as c:
        assert c.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert work_fingerprint(c) == work


def test_archived_ancestor_cannot_revive_when_revoked_flag_is_accidentally_reset():
    now = time.time()
    with db() as c:
        shop = domain(c, "墓碑保持失效")
        parent = link(c, shop["product_id"], expires=now - DAY)
        child = link(c, shop["product_id"], parent=parent)
        cleanup_links(c, [parent], "owner")
        c.execute(
            "UPDATE staff SET revoked=0,expires=? WHERE id=?", (now + DAY, parent)
        )
        for sid in (parent, child):
            with pytest.raises(HTTPException) as error:
                staff_authorization(c, sid)
            assert error.value.status_code == 401


def test_cleanup_does_not_release_foreign_product_claim_with_corrupt_actor_relation():
    now = time.time()
    with db() as c:
        first, second = domain(c, "清理边界甲"), domain(c, "清理边界乙")
        sid = link(c, first["product_id"])
        own_job, _ = claimed_task(c, first["product_id"], sid)
        foreign_job, _ = claimed_task(c, second["product_id"], sid)
        c.execute("UPDATE staff SET expires=? WHERE id=?", (now - DAY, sid))
        cleanup_links(c, [sid], "owner")
        own = c.execute("SELECT * FROM jobs WHERE id=?", (own_job,)).fetchone()
        foreign = c.execute("SELECT * FROM jobs WHERE id=?", (foreign_job,)).fetchone()
        assert own["state"] == "queued" and own["claimed_by"] is None
        assert foreign["state"] == "processing" and foreign["claimed_by"] == sid
        assert foreign["progress"] == 75

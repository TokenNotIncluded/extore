import json
import sqlite3
import time
import uuid

import pytest
from fastapi import HTTPException
from starlette.responses import Response

from extore.db import db
from extore.link_cleanup import (
    cleanup_links,
    eligible_links,
    init_schema,
    link_state,
)
from extore.models import Product
from extore.security import (
    card_digest,
    create_session,
    digest,
    staff_authorization,
    token,
)
from extore.service import issue_cards


@pytest.fixture
def store(owner):
    now = time.time()
    shops = [str(uuid.uuid4()), str(uuid.uuid4())]
    products = [str(uuid.uuid4()), str(uuid.uuid4())]
    with db() as c:
        init_schema(c)
        for index, (shop, product) in enumerate(zip(shops, products, strict=True)):
            c.execute(
                "INSERT INTO shops(id,name,created,verified) VALUES (?,?,?,1)",
                (shop, f"商店 {index}", now),
            )
            c.execute(
                "INSERT INTO products(id,shop_id,config,created) VALUES (?,?,?,?)",
                (
                    product,
                    shop,
                    json.dumps(Product(name=f"商品 {index}").model_dump()),
                    now,
                ),
            )
    return {"shops": shops, "products": products, "now": now}


def link(c, product_id, *, expires=None, created=None, parent=None, permissions=None):
    sid = str(uuid.uuid4())
    now = time.time()
    c.execute(
        "INSERT INTO staff(id,digest,product_id,name,expires,permissions,parent_id,created,"
        "max_uses,uses,max_cli_uses,cli_uses) VALUES (?,?,?,?,?,?,?,?,3,1,2,0)",
        (
            sid,
            digest(token()),
            product_id,
            "私有管理链接名称",
            now + 3600 if expires is None else expires,
            json.dumps(
                ["queue.view", "queue.process", "links.delegate"]
                if permissions is None
                else permissions
            ),
            parent,
            now if created is None else created,
        ),
    )
    return sid


def row(c, sid):
    return c.execute("SELECT * FROM staff WHERE id=?", (sid,)).fetchone()


def snapshot(c, *tables):
    return {
        table: [
            dict(item) for item in c.execute(f"SELECT * FROM {table} ORDER BY rowid")
        ]
        for table in tables
    }


def device_and_session(c, sid):
    device = str(uuid.uuid4())
    c.execute(
        "INSERT INTO cli_devices(id,staff_id,public_key,fingerprint,client_name,created,last_seen) "
        "VALUES (?,?,?,?,?,?,?)",
        (device, sid, token(), token(), "本地测试客户端", time.time(), time.time()),
    )
    value = create_session(c, Response(), "staff", sid)
    c.execute(
        "UPDATE sessions SET channel='cli',device_id=? WHERE digest=?",
        (device, digest(value)),
    )
    create_session(c, Response(), "staff", sid)
    return device


def claimed_task(c, product_id, sid):
    code = issue_cards(c, product_id, 1)[0]
    cid = c.execute(
        "SELECT id FROM cards WHERE digest=?", (card_digest(code),)
    ).fetchone()[0]
    jid, fid = str(uuid.uuid4()), str(uuid.uuid4())
    c.execute(
        "INSERT INTO jobs(id,card_id,product_id,state,params,progress,claimed_by,lease,created,updated) "
        "VALUES (?,?,?,'processing',?,75,?,?,?,?)",
        (
            jid,
            cid,
            product_id,
            '{"requirement":"保留原始需求"}',
            sid,
            time.time() + 300,
            time.time(),
            time.time(),
        ),
    )
    c.execute("UPDATE cards SET state='reserved' WHERE id=?", (cid,))
    c.execute(
        "INSERT INTO job_files(id,card_id,product_id,job_id,field_key,kind,filename,content_type,size,content,bound,created) "
        "VALUES (?,?,?,?,?,'input',?,?,?, ?,1,?)",
        (
            fid,
            cid,
            product_id,
            jid,
            "document",
            "测试附件.txt",
            "text/plain",
            4,
            b"keep",
            time.time(),
        ),
    )
    return jid, fid


def test_schema_migration_is_idempotent_and_retains_live_link(store):
    with db() as c:
        sid = link(c, store["products"][0])
        before = dict(row(c, sid))
        init_schema(c)
        init_schema(c)
        assert dict(row(c, sid)) == before
        assert link_state(c, row(c, sid)) == "active"
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE staff SET archived=2 WHERE id=?", (sid,))


def test_child_of_expired_ancestor_is_history_and_uses_ancestor_retention(store):
    with db() as c:
        now = store["now"]
        parent = link(c, store["products"][0], expires=now - 1000)
        child = link(c, store["products"][0], expires=now + 10000, parent=parent)
        assert link_state(c, row(c, child), now) == "history"
        assert set(eligible_links(c, before=now - 500, now=now)) == {parent, child}
        assert eligible_links(c, before=now - 1500, now=now) == []


def test_future_clock_honors_ancestor_expiry_without_revoking_live_links(store):
    with db() as c:
        now = store["now"]
        parent = link(c, store["products"][0], expires=now + 300)
        child = link(c, store["products"][0], expires=now + 900, parent=parent)
        assert link_state(c, row(c, child), now) == "active"
        assert link_state(c, row(c, child), now + 301) == "history"
        assert set(eligible_links(c, now=now + 301)) == {parent, child}
        with pytest.raises(HTTPException) as error:
            cleanup_links(c, [child], "owner", dry_run=True)
        assert error.value.status_code == 409
        assert row(c, child)["revoked"] == 0


def test_revocation_retention_uses_audit_and_unknown_times_are_kept(store):
    with db() as c:
        now = store["now"]
        parent = link(c, store["products"][0])
        child = link(c, store["products"][0], parent=parent)
        c.execute("UPDATE staff SET revoked=1 WHERE id=?", (parent,))
        assert set(eligible_links(c)) == {parent, child}
        assert eligible_links(c, before=now - 100) == []
        c.execute(
            "INSERT INTO audit(actor,action,target,created) VALUES ('owner','staff.revoke',?,?)",
            (parent, now - 500),
        )
        assert set(eligible_links(c, before=now - 100)) == {parent, child}


def test_permissions_intersection_and_disabled_shop_are_not_active(store):
    with db() as c:
        parent = link(c, store["products"][0], permissions=["queue.view"])
        child = link(
            c, store["products"][0], parent=parent, permissions=["cards.manage"]
        )
        assert link_state(c, row(c, child)) == "history"
        assert child in eligible_links(c)
        c.execute("UPDATE shops SET enabled=0 WHERE id=?", (store["shops"][0],))
        assert link_state(c, row(c, parent)) == "history"
        assert eligible_links(c, before=store["now"] - 100) == []


def test_active_rows_before_expired_rows_do_not_starve_selection(store):
    with db() as c:
        for index in range(10):
            link(c, store["products"][0], created=store["now"] - 100 + index)
        first = link(
            c, store["products"][0], created=store["now"], expires=store["now"] - 10
        )
        second = link(
            c, store["products"][0], created=store["now"] + 1, expires=store["now"] - 10
        )
        assert eligible_links(c, limit=1) == [first]
        assert cleanup_links(c, [first], "system") == 1
        assert eligible_links(c, limit=1) == [second]


def test_staff_scope_is_applied_before_limit_and_cannot_clean_self(store):
    with db() as c:
        unrelated = link(c, store["products"][0], expires=store["now"] - 100, created=0)
        other = link(c, store["products"][1], expires=store["now"] - 100, created=0)
        parent = link(c, store["products"][0])
        child = link(c, store["products"][0], parent=parent, expires=store["now"] - 10)
        assert eligible_links(c, staff_id=parent, limit=1) == [child]
        with pytest.raises(HTTPException) as error:
            cleanup_links(c, [parent], parent)
        assert error.value.status_code == 403
        for sid in (unrelated, other):
            with pytest.raises(HTTPException) as error:
                cleanup_links(c, [sid], parent)
            assert error.value.status_code == 404
        assert cleanup_links(c, [child], parent) == 1


def test_archiving_keeps_parent_tombstone_and_permanently_invalidates_child(store):
    with db() as c:
        parent = link(c, store["products"][0], expires=store["now"] - 10)
        child = link(c, store["products"][0], parent=parent)
        before = dict(row(c, parent))
        assert cleanup_links(c, [parent], "owner") == 1
        after = row(c, parent)
        assert after["archived"] == after["revoked"] == 1
        assert after["name"] == "已清理链接"
        assert json.loads(after["permissions"]) == ["queue.view"]
        assert after["digest"] != before["digest"]
        assert after["uses"] == after["max_uses"]
        assert after["cli_uses"] == after["max_cli_uses"]
        for key in ("id", "product_id", "parent_id", "created", "expires"):
            assert after[key] == before[key]
        assert row(c, child)["parent_id"] == parent
        assert row(c, child)["archived"] == row(c, child)["revoked"] == 0
        assert link_state(c, after) == "archived"
        assert link_state(c, row(c, child)) == "history"
        with pytest.raises(HTTPException) as error:
            staff_authorization(c, child)
        assert error.value.status_code == 401
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_cleanup_releases_only_its_product_tasks_and_preserves_files(store):
    with db() as c:
        parent = link(c, store["products"][0])
        child = link(c, store["products"][0], parent=parent)
        other = link(c, store["products"][1])
        child_device = device_and_session(c, child)
        other_device = device_and_session(c, other)
        jid, fid = claimed_task(c, store["products"][0], child)
        other_jid, _ = claimed_task(c, store["products"][1], other)
        before = snapshot(c, "cards", "job_files")
        c.execute("UPDATE staff SET expires=? WHERE id=?", (store["now"] - 10, parent))
        assert cleanup_links(c, [parent], f"shop:{store['shops'][0]}") == 1
        task = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        assert task["state"] == "queued" and task["claimed_by"] is None
        assert task["lease"] is None and task["progress"] == 75
        assert json.loads(task["params"])["requirement"] == "保留原始需求"
        assert (
            c.execute("SELECT content FROM job_files WHERE id=?", (fid,)).fetchone()[0]
            == b"keep"
        )
        assert snapshot(c, "cards", "job_files") == before
        assert (
            c.execute(
                "SELECT revoked FROM cli_devices WHERE id=?", (child_device,)
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute(
                "SELECT 1 FROM sessions WHERE staff_id=? AND revoked=0", (child,)
            ).fetchone()
            is None
        )
        assert (
            c.execute(
                "SELECT revoked FROM cli_devices WHERE id=?", (other_device,)
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute("SELECT state FROM jobs WHERE id=?", (other_jid,)).fetchone()[0]
            == "processing"
        )
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_cleanup_releases_orphaned_claim_with_no_session_rows(store):
    with db() as c:
        sid = link(c, store["products"][0])
        jid, _ = claimed_task(c, store["products"][0], sid)
        c.execute("UPDATE staff SET expires=? WHERE id=?", (store["now"] - 10, sid))
        assert cleanup_links(c, [sid], "owner") == 1
        result = c.execute(
            "SELECT state,progress FROM jobs WHERE id=?", (jid,)
        ).fetchone()
        assert tuple(result) == ("queued", 75)


def test_cross_shop_batch_is_rejected_before_any_change(store):
    with db() as c:
        a = link(c, store["products"][0], expires=store["now"] - 10)
        b = link(c, store["products"][1], expires=store["now"] - 10)
        assert eligible_links(c, shop_id=store["shops"][0]) == [a]
        assert eligible_links(c, product_id=store["products"][1]) == [b]
        before = snapshot(c, "staff", "audit", "sessions", "cli_devices")
        with pytest.raises(HTTPException) as error:
            cleanup_links(c, [a, b], f"shop:{store['shops'][0]}")
        assert error.value.status_code == 404
        assert snapshot(c, "staff", "audit", "sessions", "cli_devices") == before
        assert set(eligible_links(c)) == {a, b}
        assert cleanup_links(c, [a, b], "owner") == 2


def test_active_link_in_batch_rejects_whole_batch_and_stale_row_is_refreshed(store):
    with db() as c:
        expired = link(c, store["products"][0], expires=store["now"] - 10)
        active = link(c, store["products"][0])
        old = row(c, expired)
        c.execute(
            "UPDATE staff SET expires=? WHERE id=?", (store["now"] + 3600, expired)
        )
        assert link_state(c, old) == "active"
        c.execute("UPDATE staff SET expires=? WHERE id=?", (store["now"] - 10, expired))
        before = snapshot(c, "staff", "audit")
        with pytest.raises(HTTPException) as error:
            cleanup_links(c, [expired, active], "owner")
        assert error.value.status_code == 409
        assert snapshot(c, "staff", "audit") == before


def test_cleanup_is_idempotent_and_dry_run_does_not_change_state(store):
    with db() as c:
        sid = link(c, store["products"][0], expires=store["now"] - 10)
        before = snapshot(c, "staff", "audit", "sessions", "cli_devices")
        assert cleanup_links(c, [sid, sid], "owner", dry_run=True) == 1
        assert snapshot(c, "staff", "audit", "sessions", "cli_devices") == before
        assert cleanup_links(c, [sid, sid], "owner") == 1
        after = snapshot(c, "staff", "audit", "sessions", "cli_devices")
        assert cleanup_links(c, [sid], "owner") == 0
        assert snapshot(c, "staff", "audit", "sessions", "cli_devices") == after
        assert eligible_links(c) == []
        audits = c.execute(
            "SELECT actor,action,target FROM audit WHERE action='staff.archive'"
        ).fetchall()
        assert [tuple(entry) for entry in audits] == [("owner", "staff.archive", sid)]


def test_failed_cleanup_rolls_back_even_if_caller_catches_exception(store, monkeypatch):
    from extore import link_access

    with db() as c:
        links = [
            link(c, store["products"][0], expires=store["now"] - 10) for _ in range(2)
        ]
        before = snapshot(c, "staff", "audit")
        real_revoke = link_access.revoke_staff_sessions
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("合成故障")
            return real_revoke(*args, **kwargs)

        monkeypatch.setattr(link_access, "revoke_staff_sessions", fail_second)
        with pytest.raises(RuntimeError, match="合成故障"):
            cleanup_links(c, links, "owner")
        assert snapshot(c, "staff", "audit") == before


@pytest.mark.parametrize("limit", (0, 501, True, 1.5))
def test_selection_limit_is_bounded(store, limit):
    with db() as c, pytest.raises(ValueError):
        eligible_links(c, limit=limit)


def test_cleanup_refuses_overlarge_batches_and_unknown_actors(store):
    with db() as c:
        sid = link(c, store["products"][0], expires=store["now"] - 10)
        with pytest.raises(HTTPException) as error:
            cleanup_links(c, [sid] * 501, "owner")
        assert error.value.status_code == 400
        with pytest.raises(HTTPException):
            cleanup_links(c, [sid], "unknown-manager")
        assert row(c, sid)["archived"] == 0


def test_malformed_ancestry_stays_invalid_after_archiving(store):
    with db() as c:
        parent = link(c, store["products"][0])
        child = link(c, store["products"][0], parent=parent)
        c.execute("UPDATE staff SET parent_id=? WHERE id=?", (child, parent))
        assert link_state(c, row(c, child)) == "history"
        assert eligible_links(c, before=store["now"] - 100) == []
        assert cleanup_links(c, [parent], "owner") == 1
        assert row(c, parent)["parent_id"] == child
        assert row(c, child)["parent_id"] == parent
        with pytest.raises(HTTPException) as error:
            staff_authorization(c, child)
        assert error.value.status_code == 401

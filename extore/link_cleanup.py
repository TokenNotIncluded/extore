"""Archive inactive links while preserving the authorization ancestry tombstone."""

import math
import time
import uuid

from fastapi import HTTPException

from .db import audit
from .security import digest, fail, staff_authorization, token

MAX_LINKS = 500


def init_schema(c):
    columns = {row["name"] for row in c.execute("PRAGMA table_info(staff)")}
    if "archived" not in columns:
        c.execute(
            "ALTER TABLE staff ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 "
            "CHECK(archived IN (0,1))"
        )
    c.execute(
        "CREATE INDEX IF NOT EXISTS staff_cleanup "
        "ON staff(product_id,archived,created,id)"
    )


def _clock(value):
    value = time.time() if value is None else value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Invalid cleanup timestamp")
    if not math.isfinite(value):
        raise ValueError("Invalid cleanup timestamp")
    return value


def _chain(c, link_id):
    """Stop at a missing parent, cycle or cross-product edge; never invent a root."""
    rows = []
    seen = set()
    product_id = None
    while link_id is not None:
        if link_id in seen:
            return rows, False
        seen.add(link_id)
        row = c.execute("SELECT * FROM staff WHERE id=?", (link_id,)).fetchone()
        if row is None or (product_id is not None and row["product_id"] != product_id):
            return rows, False
        product_id = row["product_id"]
        rows.append(row)
        link_id = row["parent_id"]
    return rows, bool(rows)


def link_state(c, row, now=None):
    """Classify current authorization, including ancestor expiry and permissions.

    ``now`` can advance the retention clock; it never makes authorization that
    is invalid at the actual current time valid again.
    """
    now = _clock(now)
    current = c.execute("SELECT * FROM staff WHERE id=?", (row["id"],)).fetchone()
    if current is None:
        return "history"
    if current["archived"]:
        return "archived"
    chain, complete = _chain(c, current["id"])
    if not complete or any(node["archived"] for node in chain):
        return "history"
    try:
        authorized = staff_authorization(c, current["id"])
    except HTTPException:
        return "history"
    return "active" if authorized["expires"] > now else "history"


def _inactive_since(c, row, now):
    """Use only an observed expiry or auditable revocation for automatic cleanup."""
    timestamps = []
    chain, _ = _chain(c, row["id"])
    for node in chain:
        try:
            expiry = float(node["expires"])
        except (TypeError, ValueError, OverflowError):
            expiry = None
        if expiry is not None and math.isfinite(expiry) and expiry <= now:
            timestamps.append(expiry)
        if node["revoked"] or node["archived"]:
            recorded = c.execute(
                "SELECT MIN(created) FROM audit "
                "WHERE target=? AND action IN ('staff.revoke','staff.archive') "
                "AND created<=?",
                (node["id"], now),
            ).fetchone()[0]
            if recorded is not None:
                timestamps.append(recorded)
    return min(timestamps) if timestamps else None


def _delegate(c, staff_id):
    row = staff_authorization(c, staff_id)
    if "links.delegate" not in row["permissions"]:
        fail("没有管理下级链接的权限", 403)
    return row


def eligible_links(
    c,
    shop_id=None,
    product_id=None,
    before=None,
    limit=100,
    now=None,
    *,
    staff_id=None,
):
    """Return only opaque IDs; scope descendants before selecting a bounded batch."""
    if type(limit) is not int or not 1 <= limit <= MAX_LINKS:
        raise ValueError("Cleanup limit must be between 1 and 500")
    now = _clock(now)
    if before is not None:
        before = _clock(before)
    params = []
    sql = ""
    if staff_id is not None:
        actor = _delegate(c, staff_id)
        if shop_id is not None and shop_id != actor["shop_id"]:
            fail("没有此店铺的管理权限", 403)
        if product_id is not None and product_id != actor["product_id"]:
            fail("没有此商品的管理权限", 403)
        shop_id, product_id = actor["shop_id"], actor["product_id"]
        sql = (
            "WITH RECURSIVE descendants(id) AS ("
            "SELECT id FROM staff WHERE parent_id=? AND product_id=? "
            "UNION SELECT child.id FROM staff child JOIN descendants "
            "ON child.parent_id=descendants.id WHERE child.product_id=?) "
        )
        params.extend((staff_id, product_id, product_id))
    sql += (
        "SELECT staff.* FROM staff JOIN products ON products.id=staff.product_id "
        "WHERE staff.archived=0"
    )
    if shop_id is not None:
        sql += " AND products.shop_id=?"
        params.append(shop_id)
    if product_id is not None:
        sql += " AND staff.product_id=?"
        params.append(product_id)
    if staff_id is not None:
        sql += " AND staff.id IN (SELECT id FROM descendants) AND staff.id!=?"
        params.append(staff_id)
    sql += " ORDER BY staff.created,staff.id"
    result = []
    # Apply the limit after classifying, so earlier active links cannot keep
    # an eligible expired link out of every maintenance batch.
    for row in c.execute(sql, params):
        if link_state(c, row, now) != "history":
            continue
        if before is not None:
            inactive_since = _inactive_since(c, row, now)
            if inactive_since is None or inactive_since > before:
                continue
        result.append(row["id"])
        if len(result) == limit:
            break
    return result


def _actor_scope(c, actor):
    if actor in ("owner", "system"):
        return None, None
    if isinstance(actor, str) and actor.startswith("shop:"):
        from .shops import shop_row

        shop_id = actor.removeprefix("shop:")
        shop_row(c, shop_id)
        return shop_id, None
    if not isinstance(actor, str) or not actor:
        fail("无效的管理者", 403)
    authorized = _delegate(c, actor)
    return authorized["shop_id"], authorized


def _in_scope(c, row, actor, shop_id, delegate):
    product = c.execute(
        "SELECT shop_id FROM products WHERE id=?", (row["product_id"],)
    ).fetchone()
    if product is None or (shop_id is not None and product["shop_id"] != shop_id):
        return False
    if delegate is None:
        return True
    if row["id"] == actor:
        fail("不能清理自己的管理链接", 403)
    if row["product_id"] != delegate["product_id"]:
        return False
    chain, _ = _chain(c, row["id"])
    return any(node["id"] == actor for node in chain[1:])


def _revoke_descendants(c, row, actor):
    from .link_access import _release_last_staff_session, revoke_staff_sessions

    descendants = c.execute(
        "WITH RECURSIVE descendants(id) AS (SELECT id FROM staff WHERE id=? "
        "UNION SELECT child.id FROM staff child JOIN descendants "
        "ON child.parent_id=descendants.id WHERE child.product_id=?) "
        "SELECT id FROM descendants ORDER BY id",
        (row["id"], row["product_id"]),
    ).fetchall()
    for descendant in descendants:
        revoke_staff_sessions(c, descendant["id"], actor)
        _release_last_staff_session(c, descendant["id"])


def cleanup_links(c, ids, actor, *, dry_run=False):
    """Recheck and archive atomically; callers must supply a server-derived actor.

    Rows and parent edges remain as tombstones. ``system`` is reserved for the
    internal maintenance worker and must never be accepted from request input.
    """
    if not isinstance(ids, (list, tuple)) or len(ids) > MAX_LINKS:
        fail("一次最多清理 500 个链接", 400)
    if any(not isinstance(sid, str) or not sid or len(sid) > 100 for sid in ids):
        fail("管理链接 ID 无效", 400)
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be a boolean")
    savepoint = f"link_cleanup_{uuid.uuid4().hex}"
    c.execute(f"SAVEPOINT {savepoint}")
    try:
        shop_id, delegate = _actor_scope(c, actor)
        rows = []
        for sid in dict.fromkeys(ids):
            row = c.execute("SELECT * FROM staff WHERE id=?", (sid,)).fetchone()
            if row is None or not _in_scope(c, row, actor, shop_id, delegate):
                fail("管理链接不存在", 404)
            state = link_state(c, row)
            if state == "active":
                fail("仍然有效的链接不能清理", 409)
            if state != "archived":
                rows.append(row)
        if not dry_run:
            for row in rows:
                c.execute(
                    "UPDATE staff SET archived=1,revoked=1,digest=?,name='已清理链接',"
                    "permissions='[\"queue.view\"]',uses=max_uses,cli_uses=max_cli_uses "
                    "WHERE id=? AND archived=0",
                    (digest(token()), row["id"]),
                )
                _revoke_descendants(c, row, actor)
                audit(c, actor, "staff.archive", row["id"])
        c.execute(f"RELEASE SAVEPOINT {savepoint}")
        return len(rows)
    except BaseException:
        c.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        c.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise

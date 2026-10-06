"""Bounded management-link use and scoped, auditable login sessions."""

import hmac
import json
import re
import time
import uuid

from fastapi import APIRouter, Query, Request

from .db import audit, db
from .security import (
    authorize_management,
    fail,
    link_descendant_ids,
    session,
    session_credential_digest,
    staff_authorization,
)

router = APIRouter(prefix="/api")
RETENTION_SECONDS = 90 * 86400
AUDIT_ACTIONS = (
    "session.create",
    "session.replace",
    "session.logout",
    "session.revoke",
    "session.link_revoke",
    "session.bootstrap_complete",
    "session.auth_reset",
    "link.consume",
    "link.cli_consume",
    "cli.device.create",
    "cli.device.revoke",
    "cli.ticket.create",
    "cli.product.request",
    "cli.product.approve",
    "cli.product.deny",
    "cli.product.claim",
    "cli.scope.request",
    "cli.scope.approve",
    "cli.scope.deny",
    "cli.scope.claim",
    "cli.scope.create",
    "cli.scope.upgrade",
    "cli.scope.recover",
    "cli.scope.revoke",
    "cli.scope.issuer.root_to_shop",
    "cli.scope.issuer.shop_to_root",
    "cli.owner.request",
    "cli.owner.approve",
    "cli.owner.device.create",
    "cli.owner.device.revoke",
    "cli.owner.action",
    "staff.create",
    "staff.revoke",
    "staff.archive",
)


def init_schema(c):
    """Migrate without changing cookies, link tokens, or active authorization."""
    session_columns = {r["name"] for r in c.execute("PRAGMA table_info(sessions)")}
    for name, definition in (
        ("id", "TEXT"),
        ("last_seen", "REAL NOT NULL DEFAULT 0"),
        ("ip", "TEXT NOT NULL DEFAULT ''"),
        ("ua", "TEXT NOT NULL DEFAULT ''"),
        ("revoked", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in session_columns:
            c.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")
    for row in c.execute("SELECT digest FROM sessions WHERE id IS NULL OR id=''"):
        c.execute(
            "UPDATE sessions SET id=? WHERE digest=?",
            (str(uuid.uuid4()), row["digest"]),
        )
    c.execute("UPDATE sessions SET last_seen=created WHERE last_seen=0")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS sessions_id ON sessions(id)")
    c.execute(
        "CREATE INDEX IF NOT EXISTS sessions_link ON sessions(staff_id,revoked,expires)"
    )
    staff_columns = {r["name"] for r in c.execute("PRAGMA table_info(staff)")}
    migrating_links = "max_uses" not in staff_columns or "uses" not in staff_columns
    if "max_uses" not in staff_columns:
        c.execute("ALTER TABLE staff ADD COLUMN max_uses INTEGER NOT NULL DEFAULT 1")
    if "uses" not in staff_columns:
        c.execute("ALTER TABLE staff ADD COLUMN uses INTEGER NOT NULL DEFAULT 0")
    if migrating_links:
        # Existing active logins already consumed their links. Grant no new
        # admission simply because an older version did not count logins.
        now = time.time()
        c.execute(
            "UPDATE staff SET uses=(SELECT count(*) FROM sessions WHERE "
            "sessions.staff_id=staff.id AND sessions.role='staff' "
            "AND sessions.revoked=0 AND sessions.expires>?)",
            (now,),
        )
        c.execute("UPDATE staff SET max_uses=max(1,uses)")
    from .cli_auth import init_schema as init_cli_schema

    init_cli_schema(c)
    from .owner_cli_auth import init_schema as init_owner_cli_schema

    init_owner_cli_schema(c)


def augment_link_view(row):
    row = dict(row)
    maximum = row.get("max_uses", 1)
    used = row.get("uses", 0)
    return {
        "max_uses": maximum,
        "uses": used,
        "remaining_uses": max(0, maximum - used),
        "max_cli_uses": row.get("max_cli_uses", 1),
        "cli_uses": row.get("cli_uses", 0),
        "remaining_cli_uses": max(
            0, row.get("max_cli_uses", 1) - row.get("cli_uses", 0)
        ),
    }


def consume_link(c, row, request=None, channel="browser"):
    """Consume one successful new login inside the caller's IMMEDIATE transaction."""
    # Resolve the current row under the write transaction. A stale row passed
    # by a caller must not reset a quota or bypass ancestor revocation.
    current = staff_authorization(c, row["id"])
    if channel not in ("browser", "cli"):
        raise ValueError("Unknown link admission channel")
    maximum_column, used_column = (
        ("max_uses", "uses") if channel == "browser" else ("max_cli_uses", "cli_uses")
    )
    maximum, used = current[maximum_column], current[used_column]
    if (
        not isinstance(maximum, int)
        or maximum < 1
        or not isinstance(used, int)
        or used < 0
    ):
        fail("商品管理链接的使用额度无效", 401)
    if used >= maximum:
        fail("此商品管理链接已达到使用次数上限，请申请新的链接", 409)
    result = c.execute(
        f"UPDATE staff SET {used_column}={used_column}+1 WHERE id=? AND {used_column}=? AND {used_column}<{maximum_column} "
        "AND revoked=0 AND expires>?",
        (current["id"], used, time.time()),
    )
    if result.rowcount != 1:
        fail("此商品管理链接已达到使用次数上限，请申请新的链接", 409)
    audit(
        c,
        current["id"],
        "link.consume" if channel == "browser" else "link.cli_consume",
        current["id"],
    )
    current[used_column] = used + 1
    return current


def session_actor(row):
    if row["role"] == "staff":
        return row["staff_id"]
    if row["role"] == "admin":
        shop_id = row["shop_id"]
        return f"shop:{shop_id}" if shop_id is not None else "owner"
    return "bootstrap"


def revoke_session(c, session_digest, actor=None, action="session.revoke"):
    """Retain the row and record a single transition; never log cookie digests."""
    if action not in AUDIT_ACTIONS or not action.startswith("session."):
        raise ValueError("Unknown session audit action")
    row = c.execute(
        "SELECT id,role,staff_id,revoked,shop_id FROM sessions WHERE digest=?",
        (session_digest,),
    ).fetchone()
    if row is None or row["revoked"]:
        return None
    c.execute("UPDATE sessions SET revoked=1 WHERE digest=?", (session_digest,))
    audit(c, actor or session_actor(row), action, row["id"])
    if row["role"] == "staff":
        _release_last_staff_session(c, row["staff_id"])
    elif row["role"] == "admin" and row["shop_id"] is not None:
        _release_last_shop_session(c, row["shop_id"])
    return row["id"]


def _release_last_staff_session(c, staff_id):
    if c.execute(
        "SELECT 1 FROM sessions WHERE role='staff' AND staff_id=? AND revoked=0 "
        "AND expires>? LIMIT 1",
        (staff_id, time.time()),
    ).fetchone():
        return
    jobs = c.execute(
        "SELECT jobs.id FROM jobs JOIN products ON products.id=jobs.product_id "
        "WHERE jobs.claimed_by=? AND jobs.state='processing' "
        "AND jobs.product_id=(SELECT product_id FROM staff WHERE id=?) "
        "AND COALESCE(json_extract(products.config,'$.mode'),'manual')='manual'",
        (staff_id, staff_id),
    ).fetchall()
    for job in jobs:
        c.execute(
            "UPDATE jobs SET state='queued',claimed_by=NULL,lease=NULL,updated=? "
            "WHERE id=? AND state='processing' AND claimed_by=?",
            (time.time(), job["id"], staff_id),
        )
        # Preserve params, result drafts, files and progress so a replacement
        # processor can continue rather than repeat completed work.
        audit(c, staff_id, "job.release", job["id"])


def _release_last_shop_session(c, shop_id):
    if c.execute(
        "SELECT 1 FROM sessions WHERE role='admin' AND shop_id=? AND revoked=0 "
        "AND expires>? LIMIT 1",
        (shop_id, time.time()),
    ).fetchone():
        return
    actor = f"shop:{shop_id}"
    jobs = c.execute(
        "SELECT jobs.id FROM jobs JOIN products ON products.id=jobs.product_id "
        "WHERE products.shop_id=? AND jobs.claimed_by=? AND jobs.state='processing' "
        "AND COALESCE(json_extract(products.config,'$.mode'),'manual')='manual'",
        (shop_id, actor),
    ).fetchall()
    for job in jobs:
        c.execute(
            "UPDATE jobs SET state='queued',claimed_by=NULL,lease=NULL,updated=? "
            "WHERE id=? AND state='processing' AND claimed_by=?",
            (time.time(), job["id"], actor),
        )
        audit(c, actor, "job.release", job["id"])


def revoke_staff_sessions(c, staff_id, actor="owner"):
    from .cli_auth import revoke_devices

    revoke_devices(c, staff_id, actor)
    rows = c.execute(
        "SELECT digest FROM sessions WHERE staff_id=? AND revoked=0", (staff_id,)
    ).fetchall()
    for row in rows:
        revoke_session(c, row["digest"], actor, "session.link_revoke")
    return len(rows)


def revoke_all_sessions(c, actor="owner", action="session.auth_reset"):
    from .cli_auth import revoke_devices
    from .owner_cli_auth import revoke_owner_devices

    revoke_devices(c, actor=actor)
    revoke_owner_devices(c, actor=actor)
    rows = c.execute("SELECT digest FROM sessions WHERE revoked=0").fetchall()
    for row in rows:
        revoke_session(c, row["digest"], actor, action)
    return len(rows)


def cleanup_sessions(c, now=None):
    now = time.time() if now is None else now
    # An expired session remains available for ninety days, including its
    # opaque ID and metadata needed to interpret revocation audit entries.
    c.execute("DELETE FROM sessions WHERE expires<?", (now - RETENTION_SECONDS,))


def request_metadata(request):
    if request is None:
        return "", ""
    ip = request.client.host if request.client else ""
    ua = request.headers.get("user-agent", "")

    # Forwarded headers are untrusted. Suppress controls so metadata remains
    # plain one-line text when a manager exports or displays the session list.
    def clean(value, limit):
        return re.sub(r"[\x00-\x1f\x7f]", " ", value)[:limit]

    return clean(ip, 100), clean(ua, 300)


def _link_scope(c, s):
    authorize_management(c, s)
    if s["role"] == "admin":
        if s.get("shop_id") is None:
            return None
        return {
            row["id"]
            for row in c.execute(
                "WITH RECURSIVE scoped_links(id,shop_id) AS ("
                "SELECT pipeline_bindings.staff_id,pipeline_authorizations.shop_id "
                "FROM pipeline_bindings JOIN pipeline_authorizations "
                "ON pipeline_authorizations.id=pipeline_bindings.authorization_id "
                "UNION SELECT child.id,scoped_links.shop_id FROM staff child "
                "JOIN scoped_links ON child.parent_id=scoped_links.id) "
                "SELECT staff.id FROM staff JOIN products ON products.id=staff.product_id "
                "WHERE (products.shop_id=? AND NOT EXISTS "
                "(SELECT 1 FROM scoped_links WHERE id=staff.id)) OR "
                "(EXISTS (SELECT 1 FROM scoped_links WHERE id=staff.id AND shop_id=?) "
                "AND NOT EXISTS (SELECT 1 FROM scoped_links WHERE id=staff.id AND shop_id!=?))",
                (s["shop_id"], s["shop_id"], s["shop_id"]),
            )
        }
    ids = [s["staff_id"]]
    if "links.delegate" in s["permissions"]:
        ids = link_descendant_ids(c, s["staff_id"])
    placeholders = ",".join("?" for _ in ids)
    return {
        row["id"]
        for row in c.execute(
            f"SELECT id FROM staff WHERE product_id=? AND id IN ({placeholders})",
            (s["product_id"], *ids),
        )
    }


def _session_rows(c, scope, shop_id=None):
    sql = (
        "SELECT sessions.*,staff.name AS link_name,staff.product_id AS product_id,"
        "products.config AS product_config,products.shop_id AS product_shop_id,"
        "COALESCE(cli_devices.fingerprint,owner_cli_devices.fingerprint) AS device_fingerprint,"
        "COALESCE(cli_devices.revoked,owner_cli_devices.revoked) AS device_revoked,owner_cli_devices.expires AS owner_grant_expires FROM sessions "
        "LEFT JOIN staff ON staff.id=sessions.staff_id "
        "LEFT JOIN products ON products.id=staff.product_id"
        " LEFT JOIN cli_devices ON cli_devices.id=sessions.device_id AND cli_devices.staff_id=sessions.staff_id"
        " LEFT JOIN owner_cli_devices ON owner_cli_devices.id=sessions.owner_device_id AND owner_cli_devices.shop_id IS sessions.shop_id"
    )
    values = ()
    if scope is not None:
        if not scope and shop_id is None:
            return []
        sql += " WHERE (sessions.role='staff' AND sessions.staff_id IN ("
        sql += ",".join("?" for _ in scope) + "))"
        values = tuple(sorted(scope))
        if shop_id is not None:
            sql += " OR (sessions.role='admin' AND sessions.shop_id=?)"
            values += (shop_id,)
    return c.execute(
        sql + " ORDER BY sessions.created DESC,sessions.id", values
    ).fetchall()


def _session_view(c, row, current_digest):
    active = not row["revoked"] and row["expires"] > time.time()
    if row["channel"] == "cli" and (
        row["device_revoked"] is None or row["device_revoked"]
    ):
        active = False
    if row["owner_device_id"] and (
        row["owner_grant_expires"] is None or row["owner_grant_expires"] <= time.time()
    ):
        active = False
    if active and row["owner_device_id"]:
        from fastapi import HTTPException

        from .owner_cli_auth import owner_device

        try:
            device = owner_device(c, row["owner_device_id"])
            if device["shop_id"] != row["shop_id"]:
                active = False
        except HTTPException:
            active = False
    if active and row["role"] == "staff":
        from fastapi import HTTPException

        try:
            if row["channel"] == "cli":
                from .cli_auth import _device

                _device(c, row["device_id"])
            else:
                staff_authorization(c, row["staff_id"])
        except HTTPException:
            active = False
    elif active and row["shop_id"] is not None:
        from fastapi import HTTPException

        from .shops import shop_row

        try:
            shop_row(c, row["shop_id"])
        except HTTPException:
            active = False
    name = None
    if row["product_config"] and row["shop_id"] in (None, row["product_shop_id"]):
        try:
            name = json.loads(row["product_config"]).get("name")
        except (TypeError, ValueError, AttributeError):
            pass
    return {
        "id": row["id"],
        "role": row["role"],
        "shop_id": row["shop_id"],
        "link_id": row["staff_id"],
        "link_name": row["link_name"],
        "product_id": row["product_id"],
        "product_name": name,
        "created": row["created"],
        "last_seen": row["last_seen"],
        "expires": row["expires"],
        "revoked": bool(row["revoked"]),
        "current": hmac.compare_digest(row["digest"], current_digest),
        "active": bool(active),
        "ip": row["ip"][:100],
        "ua": row["ua"][:300],
        "channel": row["channel"],
        "client_name": row["client_name"],
        "device_id": row["device_id"] or row["owner_device_id"],
        "owner_device_id": row["owner_device_id"],
        "fingerprint": row["device_fingerprint"],
    }


def _list_sessions(request, roles):
    s = session(request, roles)
    with db() as c:
        scope = _link_scope(c, s)
        current_digest, _ = session_credential_digest(request)
        return [
            _session_view(c, row, current_digest)
            for row in _session_rows(
                c, scope, s.get("shop_id") if s["role"] == "admin" else None
            )
        ]


def _revoke_session_by_id(session_id, request, roles):
    s = session(request, roles)
    with db() as c:
        scope = _link_scope(c, s)
        row = c.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None or (
            scope is not None
            and not (
                (row["role"] == "staff" and row["staff_id"] in scope)
                or (
                    s["role"] == "admin"
                    and row["role"] == "admin"
                    and s.get("shop_id") is not None
                    and row["shop_id"] == s["shop_id"]
                )
            )
        ):
            fail("登录会话不存在", 404)
        current = hmac.compare_digest(
            row["digest"], session_credential_digest(request)[0]
        )
        revoke_session(c, row["digest"], session_actor(s))
        return {"ok": True, "id": row["id"], "current": current}


def _list_audit(request, roles, limit):
    s = session(request, roles)
    with db() as c:
        scope = _link_scope(c, s)
        # Whitelist only session/link events whose targets are safe opaque IDs.
        # Existing business audit targets can contain arbitrary identifiers;
        # this session UI does not serialize those unrelated event types.
        placeholders = ",".join("?" for _ in AUDIT_ACTIONS)
        sql = f"SELECT * FROM audit WHERE action IN ({placeholders})"
        values = list(AUDIT_ACTIONS)
        if scope is not None:
            shop_id = s.get("shop_id") if s["role"] == "admin" else None
            session_ids = {r["id"] for r in _session_rows(c, scope, shop_id)}
            device_ids = {
                r["id"]
                for r in c.execute("SELECT id,staff_id FROM cli_devices")
                if r["staff_id"] in scope
            }
            targets = scope | session_ids | device_ids
            if shop_id is not None:
                for table in ("owner_cli_devices", "owner_cli_requests"):
                    targets |= {
                        row["id"]
                        for row in c.execute(
                            f"SELECT id FROM {table} WHERE shop_id=?", (shop_id,)
                        )
                    }
                targets |= {
                    row["id"]
                    for row in c.execute(
                        "SELECT id FROM pipeline_authorizations WHERE shop_id=?",
                        (shop_id,),
                    )
                }
                targets |= {
                    row["id"]
                    for row in c.execute(
                        "SELECT id FROM cli_scope_requests WHERE approved_shop_id=? "
                        "OR authorization_id IN (SELECT id FROM pipeline_authorizations "
                        "WHERE shop_id=?)",
                        (shop_id, shop_id),
                    )
                }
            targets |= {
                row["id"]
                for row in c.execute(
                    "SELECT id,approved_staff_id,approved_shop_id FROM cli_device_requests"
                )
                if row["approved_staff_id"] in scope
                or (shop_id is not None and row["approved_shop_id"] == shop_id)
            }
            if not targets:
                return []
            sql += " AND target IN (" + ",".join("?" for _ in targets) + ")"
            values.extend(sorted(targets))
        sql += " ORDER BY created DESC,id DESC LIMIT ?"
        values.append(limit)
        rows = c.execute(sql, values).fetchall()
        # Every emitted target must refer to a retained link/session, not an
        # arbitrary string inserted into the common audit table by other code.
        safe_targets = {r["id"] for r in c.execute("SELECT id FROM sessions")}
        safe_targets |= {r["id"] for r in c.execute("SELECT id FROM staff")}
        safe_targets |= {r["id"] for r in c.execute("SELECT id FROM cli_devices")}
        safe_targets |= {r["id"] for r in c.execute("SELECT id FROM owner_cli_devices")}
        safe_targets |= {
            r["id"] for r in c.execute("SELECT id FROM owner_cli_requests")
        }
        safe_targets |= {
            r["id"] for r in c.execute("SELECT id FROM cli_device_requests")
        }
        safe_targets |= {
            r["id"] for r in c.execute("SELECT id FROM pipeline_authorizations")
        }
        safe_targets |= {
            r["id"] for r in c.execute("SELECT id FROM cli_scope_requests")
        }
        return [
            {
                **{k: row[k] for k in ("id", "actor", "action", "target", "created")},
                **_audit_metadata(c, row),
            }
            for row in rows
            if row["target"] in safe_targets
            and (
                row["actor"] in ("owner", "bootstrap", "ssh", "pending", "system")
                or (
                    row["actor"].startswith("shop:")
                    and c.execute(
                        "SELECT 1 FROM shops WHERE id=?", (row["actor"][5:],)
                    ).fetchone()
                )
                or c.execute(
                    "SELECT 1 FROM staff WHERE id=?", (row["actor"],)
                ).fetchone()
            )
        ]


def _audit_metadata(c, row):
    # Reconstruct safe metadata through opaque targets; never serialize the
    # credential or free-form contents of the common audit table.
    target = c.execute(
        "SELECT client_name,fingerprint FROM cli_scope_requests WHERE id=?",
        (row["target"],),
    ).fetchone()
    if target is not None:
        return {"channel": "cli", **dict(target)}
    pending = c.execute(
        "SELECT client_name,fingerprint FROM cli_device_requests WHERE id=?",
        (row["target"],),
    ).fetchone()
    if pending is not None:
        return {"channel": "cli", **dict(pending)}
    target = c.execute(
        "SELECT client_name,fingerprint FROM pipeline_authorizations WHERE id=?",
        (row["target"],),
    ).fetchone()
    if target is not None:
        return {"channel": "cli", **dict(target)}
    target = c.execute(
        "SELECT sessions.channel,sessions.client_name,COALESCE(cli_devices.fingerprint,owner_cli_devices.fingerprint) AS fingerprint "
        "FROM sessions LEFT JOIN cli_devices ON cli_devices.id=sessions.device_id AND cli_devices.staff_id=sessions.staff_id "
        "LEFT JOIN owner_cli_devices ON owner_cli_devices.id=sessions.owner_device_id AND owner_cli_devices.shop_id IS sessions.shop_id "
        "WHERE sessions.id=?",
        (row["target"],),
    ).fetchone()
    if target is not None:
        return dict(target)
    target = c.execute(
        "SELECT client_name,fingerprint FROM cli_devices WHERE id=?", (row["target"],)
    ).fetchone()
    if target is not None:
        return {"channel": "cli", **dict(target)}
    for table in ("owner_cli_devices", "owner_cli_requests"):
        target = c.execute(
            f"SELECT client_name,fingerprint FROM {table} WHERE id=?", (row["target"],)
        ).fetchone()
        if target is not None:
            return {"channel": "cli", **dict(target)}
    return {
        "channel": "cli"
        if row["action"] in ("link.cli_consume", "cli.ticket.create")
        else "browser",
        "client_name": None,
        "fingerprint": None,
    }


@router.get("/admin/sessions")
def admin_sessions(request: Request):
    return _list_sessions(request, ("admin",))


@router.delete("/admin/sessions/{session_id}")
def admin_revoke_session(session_id: str, request: Request):
    return _revoke_session_by_id(session_id, request, ("admin",))


@router.get("/manage/sessions")
def management_sessions(request: Request):
    return _list_sessions(request, ("staff",))


@router.delete("/manage/sessions/{session_id}")
def management_revoke_session(session_id: str, request: Request):
    return _revoke_session_by_id(session_id, request, ("staff",))


@router.get("/admin/audit")
def admin_audit(request: Request, limit: int = Query(default=200, ge=1, le=200)):
    return _list_audit(request, ("admin",), limit)


@router.get("/manage/audit")
def management_audit(request: Request, limit: int = Query(default=200, ge=1, le=200)):
    return _list_audit(request, ("staff",), limit)

"""Signed CLI device grants, separate from browser link admissions."""

import base64
import hashlib
import json
import re
import time
import unicodedata
import uuid
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import ORIGIN
from .db import audit, db
from .security import (
    authorize_management,
    digest,
    fail,
    rate_limit,
    require_cli_bearer,
    session,
    staff_authorization,
    token,
)

router = APIRouter(prefix="/api")
TTL = 300
SESSION_TTL = 28800


def init_schema(c):
    columns = {r["name"] for r in c.execute("PRAGMA table_info(staff)")}
    for name, default in (("max_cli_uses", 1), ("cli_uses", 0)):
        if name not in columns:
            c.execute(
                f"ALTER TABLE staff ADD COLUMN {name} INTEGER NOT NULL DEFAULT {default}"
            )
    c.execute(
        "CREATE TABLE IF NOT EXISTS cli_devices ("
        "id TEXT PRIMARY KEY,staff_id TEXT NOT NULL REFERENCES staff(id),"
        "public_key TEXT NOT NULL,fingerprint TEXT NOT NULL,client_name TEXT NOT NULL,"
        "created REAL NOT NULL,last_seen REAL NOT NULL,"
        "revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),"
        "UNIQUE(staff_id,public_key))"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS cli_challenges ("
        "id TEXT PRIMARY KEY,device_id TEXT NOT NULL REFERENCES cli_devices(id),"
        "challenge TEXT NOT NULL,expires REAL NOT NULL,created REAL NOT NULL)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS cli_bind_tickets ("
        "digest TEXT PRIMARY KEY,staff_id TEXT NOT NULL REFERENCES staff(id),"
        "expires REAL NOT NULL,created REAL NOT NULL,"
        "consumed INTEGER NOT NULL DEFAULT 0 CHECK(consumed IN (0,1)),"
        "bound_device_id TEXT REFERENCES cli_devices(id))"
    )
    columns = {r["name"] for r in c.execute("PRAGMA table_info(sessions)")}
    for name, definition in (
        (
            "channel",
            "TEXT NOT NULL DEFAULT 'browser' CHECK(channel IN ('browser','cli'))",
        ),
        ("device_id", "TEXT REFERENCES cli_devices(id)"),
        ("client_name", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in columns:
            c.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")
    c.execute("CREATE INDEX IF NOT EXISTS cli_device_link ON cli_devices(staff_id)")
    c.execute("CREATE INDEX IF NOT EXISTS cli_session_device ON sessions(device_id)")


class BindInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=2048)
    public_key: str = Field(min_length=43, max_length=43)
    client_name: str = Field(min_length=1, max_length=100)
    signature: str = Field(min_length=86, max_length=86)


class ChallengeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str = Field(min_length=1, max_length=100)


class SessionInput(ChallengeInput):
    challenge_id: str = Field(min_length=1, max_length=100)
    signature: str = Field(min_length=86, max_length=86)


class TicketInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    staff_id: str | None = Field(default=None, min_length=1, max_length=100)


async def _body(request, cls):
    # FastAPI's default validation response includes submitted input values.
    # Credential-bearing payloads must never be reflected into a 422 response.
    try:
        raw = await request.json()
        return cls.model_validate(raw)
    except (ValueError, TypeError, ValidationError):
        fail("授权参数无效", 400)


def _handshake(request):
    if request.headers.get("authorization") is not None or request.cookies.get(
        "extore_session"
    ):
        fail("CLI 授权不能混用浏览器登录凭证", 400)


def _decode(value, size):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        fail("设备签名无效", 401)
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError:
        fail("设备签名无效", 401)
    if len(raw) != size or base64.urlsafe_b64encode(raw).decode().rstrip("=") != value:
        fail("设备签名无效", 401)
    return raw


def _verify(public_key, signature, proof):
    raw = _decode(public_key, 32)
    try:
        Ed25519PublicKey.from_public_bytes(raw).verify(
            _decode(signature, 64), proof.encode()
        )
    except (InvalidSignature, ValueError):
        fail("设备签名无效", 401)
    return raw


def _link_token(value):
    ticket_only = False
    if value.startswith("#"):
        value = value[1:]
    elif "://" in value:
        try:
            u = urlsplit(value)
            base = urlsplit(ORIGIN)
            if (
                u.scheme != base.scheme
                or u.netloc != base.netloc
                or u.path not in ("/staff", "/cli")
                or u.query
                or u.username
                or u.password
            ):
                fail("商品管理链接无效", 401)
            value = u.fragment
            ticket_only = u.path == "/cli"
        except ValueError:
            fail("商品管理链接无效", 401)
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        fail("商品管理链接无效", 401)
    return value, ticket_only


def _device(c, device_id):
    from .pipeline_scopes import check_device_scope

    row = c.execute("SELECT * FROM cli_devices WHERE id=?", (device_id,)).fetchone()
    if row is None or row["revoked"]:
        fail("CLI 设备授权已失效", 401)
    staff = staff_authorization(c, row["staff_id"])
    pipeline_scope = check_device_scope(c, row, staff)
    if pipeline_scope is not None:
        staff.update(
            authorization_id=pipeline_scope["id"],
            authorization_revision=pipeline_scope["revision"],
            scope=pipeline_scope["kind"],
        )
    return row, staff


def _scope_metadata(staff):
    return {
        key: staff[key]
        for key in ("authorization_id", "authorization_revision", "scope")
        if key in staff
    }


def _bind_view(device, staff, already):
    return {
        **_scope_metadata(staff),
        "device_id": device["id"],
        "product_id": staff["product_id"],
        "shop_id": staff["shop_id"],
        "client_name": device["client_name"],
        "fingerprint": device["fingerprint"],
        "already_authorized": already,
        "expires": staff["expires"],
        "permissions": staff["permissions"],
        "max_cli_uses": staff["max_cli_uses"],
        "cli_uses": staff["cli_uses"],
        "remaining_cli_uses": max(0, staff["max_cli_uses"] - staff["cli_uses"]),
    }


@router.post("/cli/authorize")
async def authorize_cli(request: Request):
    from .link_access import consume_link

    _handshake(request)
    rate_limit(request, "cli-bind", 20, 60)
    body = await _body(request, BindInput)
    name = body.client_name.strip()
    if not name or any(unicodedata.category(x).startswith("C") for x in name):
        fail("设备名称无效", 400)
    public_raw = _verify(
        body.public_key,
        body.signature,
        f"extore-cli-bind-v1\n{ORIGIN}\n{body.token}\n{body.public_key}",
    )
    value, ticket_only = _link_token(body.token)
    with db() as c:
        ticket = c.execute(
            "SELECT * FROM cli_bind_tickets WHERE digest=?", (digest(value),)
        ).fetchone()
        if ticket:
            if ticket["expires"] <= time.time():
                fail("CLI 授权口令已失效", 401)
            staff = staff_authorization(c, ticket["staff_id"])
        else:
            if ticket_only:
                fail("CLI 授权口令已失效", 401)
            row = c.execute(
                "SELECT * FROM staff WHERE digest=?", (digest(value),)
            ).fetchone()
            if row is None:
                fail("商品管理链接无效", 401)
            staff = staff_authorization(c, row["id"])
        existing = c.execute(
            "SELECT * FROM cli_devices WHERE staff_id=? AND public_key=?",
            (staff["id"], body.public_key),
        ).fetchone()
        if (
            ticket
            and ticket["consumed"]
            and (existing is None or existing["id"] != ticket["bound_device_id"])
        ):
            fail("CLI 授权口令已使用", 401)
        if existing is not None:
            if existing["revoked"]:
                fail("CLI 设备授权已撤销，请申请新的管理链接", 401)
            existing, staff = _device(c, existing["id"])
            if ticket and not ticket["consumed"]:
                c.execute(
                    "UPDATE cli_bind_tickets SET consumed=1,bound_device_id=? WHERE digest=?",
                    (existing["id"], ticket["digest"]),
                )
            return _bind_view(existing, staff, True)
        staff = consume_link(c, staff, request, channel="cli")
        did, now = str(uuid.uuid4()), time.time()
        fingerprint = hashlib.sha256(public_raw).hexdigest()
        c.execute(
            "INSERT INTO cli_devices(id,staff_id,public_key,fingerprint,client_name,created,last_seen) "
            "VALUES (?,?,?,?,?,?,?)",
            (did, staff["id"], body.public_key, fingerprint, name, now, now),
        )
        if ticket:
            c.execute(
                "UPDATE cli_bind_tickets SET consumed=1,bound_device_id=? WHERE digest=?",
                (did, ticket["digest"]),
            )
        audit(c, staff["id"], "cli.device.create", did)
        device = c.execute("SELECT * FROM cli_devices WHERE id=?", (did,)).fetchone()
        return _bind_view(device, staff, False)


@router.post("/cli/challenge")
async def cli_challenge(request: Request):
    _handshake(request)
    rate_limit(request, "cli-challenge", 60, 60)
    body = await _body(request, ChallengeInput)
    with db() as c:
        _, staff = _device(c, body.device_id)
        now = time.time()
        c.execute("DELETE FROM cli_challenges WHERE expires<=?", (now,))
        pending = c.execute(
            "SELECT count(*) FROM cli_challenges WHERE device_id=?", (body.device_id,)
        ).fetchone()[0]
        if pending >= 20:
            fail("设备请求过于频繁，请稍后重试", 429)
        cid, challenge = str(uuid.uuid4()), token()
        expires = min(now + TTL, staff["expires"])
        c.execute(
            "INSERT INTO cli_challenges VALUES (?,?,?,?,?)",
            (cid, body.device_id, challenge, expires, now),
        )
        return {
            "challenge_id": cid,
            "challenge": challenge,
            "expires": expires,
            "expires_in": max(0, int(expires - now)),
        }


@router.post("/cli/session")
async def cli_session(request: Request):
    from .link_access import cleanup_sessions, request_metadata

    _handshake(request)
    rate_limit(request, "cli-session", 60, 60)
    body = await _body(request, SessionInput)
    with db() as c:
        device, staff = _device(c, body.device_id)
        challenge = c.execute(
            "SELECT * FROM cli_challenges WHERE id=? AND device_id=? AND expires>?",
            (body.challenge_id, body.device_id, time.time()),
        ).fetchone()
        if challenge is None:
            fail("设备登录验证已失效", 401)
        _verify(
            device["public_key"],
            body.signature,
            f"extore-cli-session-v1\n{ORIGIN}\n{body.device_id}\n{body.challenge_id}\n{challenge['challenge']}",
        )
        now, sid, access = time.time(), str(uuid.uuid4()), token()
        expires = min(now + SESSION_TTL, staff["expires"])
        cleanup_sessions(c, now)
        c.execute("DELETE FROM cli_challenges WHERE id=?", (body.challenge_id,))
        ip, ua = request_metadata(request)
        c.execute(
            "INSERT INTO sessions(digest,role,staff_id,expires,created,id,last_seen,ip,ua,"
            "revoked,channel,device_id,client_name,shop_id,auth_at,auth_method) VALUES (?,'staff',?,?,?,?,?,?,?,0,'cli',?,?,?,?,?)",
            (
                digest(access),
                staff["id"],
                expires,
                now,
                sid,
                now,
                ip,
                ua,
                device["id"],
                device["client_name"],
                staff["shop_id"],
                now,
                "device_key",
            ),
        )
        c.execute("UPDATE cli_devices SET last_seen=? WHERE id=?", (now, device["id"]))
        audit(c, staff["id"], "session.create", sid)
        return {
            **_scope_metadata(staff),
            "access_token": access,
            "token_type": "Bearer",
            "expires": expires,
            "expires_in": max(0, int(expires - now)),
            "device_id": device["id"],
            "session_id": sid,
            "product_id": staff["product_id"],
            "shop_id": staff["shop_id"],
            "permissions": staff["permissions"],
        }


@router.get("/cli/status")
def cli_status(request: Request):
    s = require_cli_bearer(request)
    with db() as c:
        authorize_management(c, s)
        row = c.execute(
            "SELECT config FROM products WHERE id=?", (s["product_id"],)
        ).fetchone()
        result = {
            **_scope_metadata(s),
            "role": "staff",
            "channel": "cli",
            "origin": ORIGIN,
            "product_id": s["product_id"],
            "shop_id": s["shop_id"],
            "product_name": json.loads(row["config"]).get("name") if row else None,
            "permissions": s["permissions"],
            "link_id": s["staff_id"],
            "link_name": s["name"],
            "link_expires": s["link_expires"],
            "device_id": s["device_id"],
            "client_name": s["client_name"],
            "session_id": s["id"],
            "expires": s["expires"],
        }
        for key in (
            "max_uses",
            "uses",
            "remaining_uses",
            "max_cli_uses",
            "cli_uses",
            "remaining_cli_uses",
        ):
            result[key] = s[key]
        return result


@router.delete("/cli/session")
def cli_logout(request: Request):
    from .link_access import revoke_session

    s = require_cli_bearer(request)
    with db() as c:
        authorize_management(c, s)
        revoke_session(c, s["digest"], s["staff_id"], "session.logout")
    return {"ok": True, "id": s["id"], "current": True}


@router.post("/manage/cli-ticket")
async def create_cli_ticket(request: Request):
    s = session(request, ("admin", "staff"))
    if s["channel"] != "browser":
        fail("请在浏览器登录后创建 CLI 授权口令", 403)
    body = await _body(request, TicketInput)
    rate_limit(request, "cli-ticket", 20, 60)
    with db() as c:
        authorize_management(c, s)
        staff_id = body.staff_id if s["role"] == "admin" else s["staff_id"]
        if staff_id is None:
            fail("请选择商品管理链接", 400)
        if s["role"] == "staff" and body.staff_id not in (None, s["staff_id"]):
            fail("不能为其它商品管理链接创建 CLI 授权口令", 403)
        staff = staff_authorization(c, staff_id)
        from .shops import authorize_product

        authorize_product(c, s, staff["product_id"])
        if staff["cli_uses"] >= staff["max_cli_uses"]:
            fail("此商品管理链接已达到 CLI 设备授权次数上限", 409)
        now, value = time.time(), "cli1_" + token()
        expires = min(now + TTL, staff["expires"])
        c.execute("DELETE FROM cli_bind_tickets WHERE expires<?", (now - 86400,))
        c.execute(
            "INSERT INTO cli_bind_tickets(digest,staff_id,expires,created) VALUES (?,?,?,?)",
            (digest(value), staff["id"], expires, now),
        )
        audit(
            c,
            s["staff_id"] if s["role"] == "staff" else s.get("account_id", "owner"),
            "cli.ticket.create",
            staff["id"],
        )
        return {
            "token": value,
            "origin": ORIGIN,
            "expires": expires,
            "expires_in": max(0, int(expires - now)),
            "staff_id": staff["id"],
            "product_id": staff["product_id"],
            "shop_id": staff["shop_id"],
        }


def revoke_devices(c, staff_id=None, actor="owner"):
    """Disable grants as well as sessions so a retained key cannot mint another."""
    sql, args = "SELECT id FROM cli_devices WHERE revoked=0", ()
    if staff_id is not None:
        sql += " AND staff_id=?"
        args = (staff_id,)
    rows = c.execute(sql, args).fetchall()
    for row in rows:
        c.execute("UPDATE cli_devices SET revoked=1 WHERE id=?", (row["id"],))
        c.execute("DELETE FROM cli_challenges WHERE device_id=?", (row["id"],))
        audit(c, actor, "cli.device.revoke", row["id"])
    if staff_id is None:
        c.execute("DELETE FROM cli_bind_tickets")
    else:
        c.execute("DELETE FROM cli_bind_tickets WHERE staff_id=?", (staff_id,))
    return len(rows)


def _devices(request, roles):
    from .link_access import _link_scope

    s = session(request, roles)
    with db() as c:
        scope = _link_scope(c, s)
        sql = "SELECT cli_devices.*,staff.name AS link_name,staff.product_id,products.config,products.shop_id AS product_shop_id FROM cli_devices JOIN staff ON staff.id=cli_devices.staff_id JOIN products ON products.id=staff.product_id"
        args = ()
        if scope is not None:
            sql += (
                " WHERE cli_devices.staff_id IN (" + ",".join("?" for _ in scope) + ")"
            )
            args = tuple(sorted(scope))
        result = []
        for row in c.execute(
            sql + " ORDER BY cli_devices.created DESC", args
        ).fetchall():
            mapped = c.execute(
                "SELECT pipeline_authorizations.id,pipeline_authorizations.revision,"
                "pipeline_authorizations.kind,pipeline_authorizations.shop_id "
                "FROM pipeline_bindings JOIN pipeline_authorizations "
                "ON pipeline_authorizations.id=pipeline_bindings.authorization_id "
                "WHERE pipeline_bindings.device_id=?",
                (row["id"],),
            ).fetchone()
            active = not row["revoked"]
            if active:
                try:
                    _device(c, row["id"])
                except HTTPException:
                    active = False
            result.append(
                {
                    **(
                        {
                            "authorization_id": mapped["id"],
                            "authorization_revision": mapped["revision"],
                            "scope": mapped["kind"],
                        }
                        if mapped is not None
                        else {}
                    ),
                    "id": row["id"],
                    "link_id": row["staff_id"],
                    "link_name": row["link_name"],
                    "product_id": row["product_id"],
                    "product_name": (
                        json.loads(row["config"]).get("name")
                        if (
                            (
                                mapped is None
                                or mapped["shop_id"] == row["product_shop_id"]
                            )
                            and (
                                s["role"] != "admin"
                                or s.get("shop_id") in (None, row["product_shop_id"])
                            )
                        )
                        else None
                    ),
                    "client_name": row["client_name"],
                    "fingerprint": row["fingerprint"],
                    "created": row["created"],
                    "last_seen": row["last_seen"],
                    "revoked": bool(row["revoked"]),
                    "active": bool(active),
                }
            )
        return result


def _revoke_device(device_id, request, roles):
    from .link_access import _link_scope, revoke_session, session_actor

    s = session(request, roles)
    with db() as c:
        scope = _link_scope(c, s)
        row = c.execute("SELECT * FROM cli_devices WHERE id=?", (device_id,)).fetchone()
        if row is None:
            fail("CLI 设备不存在", 404)
        actor = session_actor(s)
        mapped = c.execute(
            "SELECT pipeline_authorizations.id,pipeline_authorizations.shop_id "
            "FROM pipeline_bindings JOIN pipeline_authorizations "
            "ON pipeline_authorizations.id=pipeline_bindings.authorization_id "
            "WHERE pipeline_bindings.device_id=?",
            (device_id,),
        ).fetchone()
        if mapped is not None:
            # The original shop remains the revocation owner even when the
            # product has moved. A new product owner cannot revoke its other
            # previously approved products through the device compatibility API.
            if (
                s["role"] == "admin"
                and s.get("shop_id") not in (None, mapped["shop_id"])
            ) or (s["role"] == "staff" and row["staff_id"] not in scope):
                fail("CLI 设备不存在", 404)
            from .pipeline_scopes import revoke_authorization

            before = c.execute(
                "SELECT count(*) FROM sessions WHERE revoked=0"
            ).fetchone()[0]
            changed = revoke_authorization(c, mapped["id"], actor)
            after = c.execute(
                "SELECT count(*) FROM sessions WHERE revoked=0"
            ).fetchone()[0]
            return {
                "ok": True,
                "id": device_id,
                "authorization_id": mapped["id"],
                "revoked_sessions": before - after,
                "released_jobs": changed["jobs"],
            }
        if scope is not None and row["staff_id"] not in scope:
            fail("CLI 设备不存在", 404)
        if not row["revoked"]:
            c.execute("UPDATE cli_devices SET revoked=1 WHERE id=?", (device_id,))
            audit(c, actor, "cli.device.revoke", device_id)
        c.execute("DELETE FROM cli_challenges WHERE device_id=?", (device_id,))
        rows = c.execute(
            "SELECT digest FROM sessions WHERE device_id=? AND revoked=0", (device_id,)
        ).fetchall()
        for current in rows:
            revoke_session(c, current["digest"], actor)
        return {"ok": True, "id": device_id, "revoked_sessions": len(rows)}


@router.get("/admin/cli-devices")
def admin_devices(request: Request):
    return _devices(request, ("admin",))


@router.get("/manage/cli-devices")
def management_devices(request: Request):
    return _devices(request, ("staff",))


@router.delete("/admin/cli-devices/{device_id}")
def admin_revoke_device(device_id: str, request: Request):
    return _revoke_device(device_id, request, ("admin",))


@router.delete("/manage/cli-devices/{device_id}")
def management_revoke_device(device_id: str, request: Request):
    return _revoke_device(device_id, request, ("staff",))

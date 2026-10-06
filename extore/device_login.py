"""Browser-approved, proof-of-key CLI admission for one existing product grant.

The displayed user code can locate a request, but cannot claim it. Only the
device's Ed25519 key can claim the explicitly reviewed product authorization.
"""

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
import unicodedata
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .cli_auth import _bind_view, _body, _decode, _handshake, _verify
from .config import ORIGIN
from .db import audit, db
from .security import (
    authorize_management,
    fail,
    rate_limit,
    session,
    staff_authorization,
    token,
)
from .shops import authorize_product

router = APIRouter(prefix="/api")
REQUEST_TTL = 600
POLL_INTERVAL = 5
MAX_POLL_INTERVAL = 60
MAX_REQUESTS = 10000
MAX_PENDING_PER_KEY = 10
CLEANUP_BATCH = 200
CLEANUP_GRACE = 300
MAX_CANDIDATES = 200
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def init_schema(c):
    # One additive table; existing grants, devices, sessions and their DDL stay
    # intact. SET NULL permits their established retention and reset flows.
    c.execute(
        "CREATE TABLE IF NOT EXISTS cli_device_requests ("
        "id TEXT PRIMARY KEY,public_key TEXT NOT NULL,client_name TEXT NOT NULL,"
        "fingerprint TEXT NOT NULL,nonce TEXT NOT NULL,user_code TEXT UNIQUE NOT NULL,"
        "challenge TEXT NOT NULL,requested_product_id TEXT,expires REAL NOT NULL,"
        "created REAL NOT NULL,state TEXT NOT NULL DEFAULT 'pending' "
        "CHECK(state IN ('pending','approved','claimed','denied')),"
        "approved_staff_id TEXT REFERENCES staff(id) ON DELETE SET NULL,"
        "approved_snapshot TEXT,approved_session_digest TEXT "
        "REFERENCES sessions(digest) ON DELETE SET NULL,approved_role TEXT,"
        "approved_shop_id TEXT,device_id TEXT REFERENCES cli_devices(id) "
        "ON DELETE SET NULL,poll_after REAL NOT NULL DEFAULT 0,"
        "poll_interval INTEGER NOT NULL DEFAULT 5,UNIQUE(public_key,nonce))"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS cli_device_request_expiry "
        "ON cli_device_requests(expires)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS cli_device_request_staff "
        "ON cli_device_requests(approved_staff_id)"
    )


class DeviceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    public_key: str = Field(min_length=43, max_length=43)
    signature: str = Field(min_length=86, max_length=86)
    client_name: str = Field(min_length=1, max_length=100)
    nonce: str = Field(min_length=43, max_length=43)
    product_id: str | None = Field(default=None, min_length=1, max_length=100)


class DeviceProof(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=100)
    public_key: str = Field(min_length=43, max_length=43)
    signature: str = Field(min_length=86, max_length=86)


class CodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_code: str = Field(min_length=1, max_length=64)


class OptionsInput(CodeInput):
    staff_id: str | None = Field(default=None, min_length=1, max_length=100)


class ApprovalInput(CodeInput):
    staff_id: str = Field(min_length=1, max_length=100)
    review_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class _InvalidCode(Exception):
    pass


def cleanup_requests(c, now=None):
    """Bounded retirement of expired handshakes, never of active devices."""
    now = time.time() if now is None else now
    result = c.execute(
        "DELETE FROM cli_device_requests WHERE id IN ("
        "SELECT id FROM cli_device_requests WHERE expires<? "
        "ORDER BY expires,id LIMIT ?)",
        (now - CLEANUP_GRACE, CLEANUP_BATCH),
    )
    return result.rowcount


def _request(c, request_id):
    row = c.execute(
        "SELECT * FROM cli_device_requests WHERE id=?", (request_id,)
    ).fetchone()
    if row is None:
        fail("设备授权请求无效", 401)
    return row


def _pending_code(c, value):
    code = value.strip().upper().replace("-", "")
    if not re.fullmatch(f"[{CODE_ALPHABET}]{{12}}", code):
        raise _InvalidCode
    code = "-".join(code[i : i + 4] for i in range(0, 12, 4))
    row = c.execute(
        "SELECT * FROM cli_device_requests WHERE user_code=?", (code,)
    ).fetchone()
    if row is None or row["expires"] <= time.time() or row["state"] != "pending":
        raise _InvalidCode
    return row


def _bad_code(request, actor):
    from .account_auth import _throttle

    # Throttle failures outside a rolled-back transaction. Buckets identify a
    # session, never attacker-controlled codes that would grow the rate table.
    _throttle(request, "device-invalid-code", actor["id"], limit=5, window=60)
    fail("设备码无效、已过期或已处理", 401)


def _browser(request, purpose):
    if request.headers.get("authorization") is not None:
        fail("请使用已登录的浏览器批准设备", 401)
    actor = session(request, ("admin", "staff"))
    if actor["channel"] != "browser":
        fail("请使用已登录的浏览器批准设备", 401)
    from .account_auth import _throttle

    _throttle(request, "device-" + purpose, actor["id"], limit=20, window=60)
    return actor


def _actor(actor):
    if actor["role"] == "staff":
        return actor["staff_id"]
    return "shop:" + actor["shop_id"] if actor["shop_id"] else "owner"


def _scope(c, staff, public_key=None):
    product = c.execute(
        "SELECT config,shop_id FROM products WHERE id=?", (staff["product_id"],)
    ).fetchone()
    shop = c.execute(
        "SELECT name FROM shops WHERE id=?", (staff["shop_id"],)
    ).fetchone()
    already_bound = (
        public_key is not None
        and c.execute(
            "SELECT 1 FROM cli_devices WHERE staff_id=? AND public_key=? AND revoked=0",
            (staff["id"], public_key),
        ).fetchone()
        is not None
    )
    return {
        "staff_id": staff["id"],
        "link_name": staff["name"],
        "product_id": staff["product_id"],
        "product_name": json.loads(product["config"])["name"],
        "shop_id": staff["shop_id"],
        "shop_name": shop["name"],
        "permissions": staff["permissions"],
        "expires": staff["expires"],
        "remaining_cli_uses": max(0, staff["max_cli_uses"] - staff["cli_uses"]),
        "already_bound": already_bound,
    }


def _select(c, actor, row, staff_id, *, require_capacity=True):
    authorize_management(c, actor)
    if actor["role"] == "staff" and staff_id != actor["staff_id"]:
        fail("只能批准当前商品授权的 CLI 设备", 403)
    staff = staff_authorization(c, staff_id)
    authorize_product(c, actor, staff["product_id"])
    if (
        row["requested_product_id"] is not None
        and row["requested_product_id"] != staff["product_id"]
    ):
        fail("设备申请与所选商品不匹配", 403)
    existing = c.execute(
        "SELECT revoked FROM cli_devices WHERE staff_id=? AND public_key=?",
        (staff_id, row["public_key"]),
    ).fetchone()
    if existing is not None and existing["revoked"]:
        fail("CLI 设备授权已撤销，请使用新的设备密钥", 401)
    if (
        require_capacity
        and existing is None
        and staff["cli_uses"] >= staff["max_cli_uses"]
    ):
        fail("此商品授权已达到 CLI 设备绑定次数上限", 409)
    return staff


def _snapshot(c, row, staff):
    scope = _scope(c, staff)
    scope.pop("remaining_cli_uses")
    scope.pop("already_bound")
    # Persist the complete chain as well as its current intersection. Changing
    # a parent or its permissions must invalidate a previously reviewed scope.
    chain, current = [], staff["id"]
    while current is not None:
        link = c.execute("SELECT * FROM staff WHERE id=?", (current,)).fetchone()
        chain.append(
            {
                name: link[name]
                for name in (
                    "id",
                    "product_id",
                    "name",
                    "parent_id",
                    "permissions",
                    "expires",
                    "revoked",
                    "archived",
                    "max_cli_uses",
                )
            }
        )
        current = link["parent_id"]
    return json.dumps(
        {
            "request": {
                name: row[name]
                for name in (
                    "id",
                    "public_key",
                    "client_name",
                    "fingerprint",
                    "nonce",
                    "challenge",
                    "requested_product_id",
                    "expires",
                )
            },
            "scope": scope,
            "chain": chain,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _public_request(row):
    return {
        "request_id": row["id"],
        "user_code": row["user_code"],
        "client_name": row["client_name"],
        "fingerprint": row["fingerprint"],
        "product_id": row["requested_product_id"],
        "expires": row["expires"],
    }


def _review_digest(snapshot, actor):
    reviewed = json.dumps(
        {
            "scope_snapshot": snapshot,
            "browser": {
                name: actor.get(name)
                for name in ("digest", "role", "shop_id", "staff_id")
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(reviewed.encode()).hexdigest()


def _request_view(row):
    return {
        **_public_request(row),
        "approval_url": ORIGIN + "/cli/device",
        "challenge": row["challenge"],
        "expires_in": max(0, int(row["expires"] - time.time() + 0.999)),
        "interval": row["poll_interval"],
    }


@router.post("/cli/device/request")
async def create_request(request: Request):
    _handshake(request)
    rate_limit(request, "device-request", 10, 60)
    body = await _body(request, DeviceRequest)
    if body.client_name != body.client_name.strip() or any(
        unicodedata.category(char).startswith("C") for char in body.client_name
    ):
        fail("设备名称无效", 400)
    nonce_issued = int.from_bytes(_decode(body.nonce, 32)[:8], "big")
    raw = _verify(
        body.public_key,
        body.signature,
        f"extore-cli-device-request-v1\n{ORIGIN}\n{body.public_key}\n"
        f"{body.client_name}\n{body.nonce}\n{body.product_id or ''}",
    )
    with db() as c:
        row = c.execute(
            "SELECT * FROM cli_device_requests WHERE public_key=? AND nonce=?",
            (body.public_key, body.nonce),
        ).fetchone()
        if row is not None:
            if (
                row["client_name"] != body.client_name
                or row["requested_product_id"] != body.product_id
            ):
                fail("设备授权申请不匹配", 401)
            # Retry a lost response without regenerating codes or extending TTL.
            return _request_view(row)
        now = time.time()
        if nonce_issued > now + 60 or nonce_issued + REQUEST_TTL <= now:
            fail("设备申请签名已过期，请生成新的设备码", 401)
        cleanup_requests(c, now)
        if (
            c.execute("SELECT count(*) FROM cli_device_requests").fetchone()[0]
            >= MAX_REQUESTS
            or c.execute(
                "SELECT count(*) FROM cli_device_requests WHERE public_key=? "
                "AND expires>? AND state IN ('pending','approved')",
                (body.public_key, now),
            ).fetchone()[0]
            >= MAX_PENDING_PER_KEY
        ):
            fail("设备授权申请过多，请稍后重试", 429)
        rid = str(uuid.uuid4())
        for _ in range(8):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
            code = "-".join(code[i : i + 4] for i in range(0, 12, 4))
            if c.execute(
                "SELECT 1 FROM cli_scope_requests WHERE user_code=?", (code,)
            ).fetchone():
                continue
            try:
                c.execute(
                    "INSERT INTO cli_device_requests(id,public_key,client_name,"
                    "fingerprint,nonce,user_code,challenge,requested_product_id,"
                    "expires,created) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        rid,
                        body.public_key,
                        body.client_name,
                        hashlib.sha256(raw).hexdigest(),
                        body.nonce,
                        code,
                        token(),
                        body.product_id,
                        min(now + REQUEST_TTL, nonce_issued + REQUEST_TTL),
                        now,
                    ),
                )
                break
            except sqlite3.IntegrityError:
                # Only the random short code can collide inside this transaction.
                continue
        else:
            fail("暂时无法生成设备码，请稍后重试", 503)
        audit(c, "pending", "cli.product.request", rid)
        return _request_view(_request(c, rid))


@router.post("/cli/device/status")
async def request_status(request: Request):
    _handshake(request)
    rate_limit(request, "device-poll", 120, 60)
    body = await _body(request, DeviceProof)
    _verify(
        body.public_key,
        body.signature,
        f"extore-cli-device-status-v1\n{ORIGIN}\n{body.request_id}\n{body.public_key}",
    )
    with db() as c:
        row = _request(c, body.request_id)
        if not hmac.compare_digest(row["public_key"], body.public_key):
            fail("设备授权申请不匹配", 401)
        now = time.time()
        state = "expired" if row["expires"] <= now else row["state"]
        result = {
            "status": state,
            "expires": row["expires"],
            "interval": row["poll_interval"],
        }
        if state not in ("pending", "approved"):
            return result
        if row["poll_after"] > now:
            interval = min(MAX_POLL_INTERVAL, row["poll_interval"] + POLL_INTERVAL)
            c.execute(
                "UPDATE cli_device_requests SET poll_interval=?,poll_after=? WHERE id=?",
                (interval, now + interval, row["id"]),
            )
            return {
                "status": "slow_down",
                "expires": row["expires"],
                "interval": interval,
                "retry_after": interval,
            }
        c.execute(
            "UPDATE cli_device_requests SET poll_after=? WHERE id=?",
            (now + row["poll_interval"], row["id"]),
        )
        # Before approval no tenant, product, link or credential data is returned.
        return result


@router.post("/manage/device/options")
async def approval_options(request: Request):
    from . import scope_auth

    if await scope_auth.handles_code(request):
        return await scope_auth.approval_options(request)
    actor = _browser(request, "options")
    body = await _body(request, OptionsInput)
    try:
        with db() as c:
            row = _pending_code(c, body.user_code)
            authorize_management(c, actor)
            selected = None
            if body.staff_id is not None:
                selected = _select(c, actor, row, body.staff_id)
            sql = (
                "SELECT staff.id FROM staff JOIN products ON products.id=staff.product_id "
                "WHERE staff.revoked=0 AND staff.archived=0 AND staff.expires>? "
                "AND NOT EXISTS (SELECT 1 FROM pipeline_bindings WHERE pipeline_bindings.staff_id=staff.id) "
                "AND (staff.cli_uses<staff.max_cli_uses OR EXISTS ("
                "SELECT 1 FROM cli_devices WHERE cli_devices.staff_id=staff.id "
                "AND cli_devices.public_key=? AND cli_devices.revoked=0))"
            )
            values = [time.time(), row["public_key"]]
            if actor["role"] == "staff":
                sql += " AND staff.id=?"
                values.append(actor["staff_id"])
            elif actor["shop_id"] is not None:
                sql += " AND products.shop_id=?"
                values.append(actor["shop_id"])
            if row["requested_product_id"] is not None:
                sql += " AND staff.product_id=?"
                values.append(row["requested_product_id"])
            sql += " ORDER BY staff.created DESC,staff.id LIMIT ?"
            values.append(MAX_CANDIDATES + 1)
            ids = c.execute(sql, values).fetchall()
            candidates = []
            for item in ids[:MAX_CANDIDATES]:
                try:
                    staff = _select(c, actor, row, item["id"])
                except HTTPException:
                    continue
                candidates.append(_scope(c, staff, row["public_key"]))
            result = {
                "request": _public_request(row),
                "candidates": candidates,
                "candidates_truncated": len(ids) > MAX_CANDIDATES,
            }
            if selected is not None:
                result.update(
                    selected=_scope(c, selected, row["public_key"]),
                    snapshot_digest=hashlib.sha256(
                        _snapshot(c, row, selected).encode()
                    ).hexdigest(),
                    review_digest=_review_digest(_snapshot(c, row, selected), actor),
                )
            return result
    except _InvalidCode:
        _bad_code(request, actor)


@router.post("/manage/device/approve")
async def approve_request(request: Request):
    from . import scope_auth

    if await scope_auth.handles_code(request):
        return await scope_auth.approve_request(request)
    actor = _browser(request, "approve")
    body = await _body(request, ApprovalInput)
    try:
        with db() as c:
            row = _pending_code(c, body.user_code)
            staff = _select(c, actor, row, body.staff_id)
            if actor["role"] == "admin":
                from .account_auth import require_recent

                require_recent(actor)
            snapshot = _snapshot(c, row, staff)
            if not hmac.compare_digest(
                _review_digest(snapshot, actor), body.review_digest
            ):
                fail("商品授权范围已变化，请重新核对设备与权限", 409)
            c.execute(
                "UPDATE cli_device_requests SET state='approved',approved_staff_id=?,"
                "approved_snapshot=?,approved_session_digest=?,approved_role=?,"
                "approved_shop_id=? WHERE id=? AND state='pending'",
                (
                    staff["id"],
                    snapshot,
                    actor["digest"],
                    actor["role"],
                    actor["shop_id"],
                    row["id"],
                ),
            )
            audit(c, _actor(actor), "cli.product.approve", row["id"])
            return {"ok": True, "status": "approved"}
    except _InvalidCode:
        _bad_code(request, actor)


@router.post("/manage/device/deny")
async def deny_request(request: Request):
    from . import scope_auth

    if await scope_auth.handles_code(request):
        return await scope_auth.deny_request(request)
    actor = _browser(request, "deny")
    body = await _body(request, CodeInput)
    try:
        with db() as c:
            row = _pending_code(c, body.user_code)
            authorize_management(c, actor)
            if actor["role"] == "staff" and row["requested_product_id"] not in (
                None,
                actor["product_id"],
            ):
                fail("设备申请与当前商品不匹配", 403)
            if actor["role"] == "admin" and row["requested_product_id"] is not None:
                authorize_product(c, actor, row["requested_product_id"])
            c.execute(
                "UPDATE cli_device_requests SET state='denied',approved_role=?,"
                "approved_shop_id=?,approved_staff_id=?,approved_session_digest=? "
                "WHERE id=? AND state='pending'",
                (
                    actor["role"],
                    actor["shop_id"],
                    actor.get("staff_id"),
                    actor["digest"],
                    row["id"],
                ),
            )
            audit(c, _actor(actor), "cli.product.deny", row["id"])
            return {"ok": True, "status": "denied"}
    except _InvalidCode:
        _bad_code(request, actor)


def _approval_actor(c, row):
    current = c.execute(
        "SELECT * FROM sessions WHERE digest=? AND revoked=0 AND expires>? "
        "AND channel='browser'",
        (row["approved_session_digest"], time.time()),
    ).fetchone()
    if (
        current is None
        or current["role"] != row["approved_role"]
        or current["shop_id"] != row["approved_shop_id"]
        or current["role"] not in ("admin", "staff")
    ):
        fail("浏览器设备审批已失效，请重新申请", 401)
    actor = dict(current)
    authorize_management(c, actor)
    return actor


@router.post("/cli/device/claim")
async def claim_request(request: Request):
    from .link_access import consume_link

    _handshake(request)
    rate_limit(request, "device-claim", 30, 60)
    body = await _body(request, DeviceProof)
    with db() as c:
        row = _request(c, body.request_id)
        _verify(
            body.public_key,
            body.signature,
            f"extore-cli-device-claim-v1\n{ORIGIN}\n{row['id']}\n"
            f"{row['challenge']}\n{body.public_key}",
        )
        if (
            not hmac.compare_digest(row["public_key"], body.public_key)
            or row["expires"] <= time.time()
            or row["state"] not in ("approved", "claimed")
            or row["approved_staff_id"] is None
        ):
            fail("设备授权尚未批准或已失效", 401)
        staff = staff_authorization(c, row["approved_staff_id"])
        if row["approved_snapshot"] != _snapshot(c, row, staff):
            fail("已批准的商品授权范围发生变化，请重新申请", 401)
        existing = c.execute(
            "SELECT * FROM cli_devices WHERE staff_id=? AND public_key=?",
            (staff["id"], body.public_key),
        ).fetchone()
        if existing is not None and existing["revoked"]:
            fail("CLI 设备授权已撤销，请使用新的设备密钥", 401)
        already = row["state"] == "claimed"
        already_bound = existing is not None
        if already:
            # Binding survives a subsequent normal browser logout. A lost claim
            # response is recoverable only with the same key and recorded device.
            if existing is None or existing["id"] != row["device_id"]:
                fail("CLI 设备授权已失效", 401)
        else:
            actor = _approval_actor(c, row)
            _select(c, actor, row, staff["id"], require_capacity=existing is None)
            if existing is None:
                staff = consume_link(c, staff, request, channel="cli")
                did, now = str(uuid.uuid4()), time.time()
                c.execute(
                    "INSERT INTO cli_devices(id,staff_id,public_key,fingerprint,"
                    "client_name,created,last_seen) VALUES (?,?,?,?,?,?,?)",
                    (
                        did,
                        staff["id"],
                        body.public_key,
                        row["fingerprint"],
                        row["client_name"],
                        now,
                        now,
                    ),
                )
                existing = c.execute(
                    "SELECT * FROM cli_devices WHERE id=?", (did,)
                ).fetchone()
                audit(c, staff["id"], "cli.device.create", did)
            c.execute(
                "UPDATE cli_device_requests SET state='claimed',device_id=? WHERE id=?",
                (existing["id"], row["id"]),
            )
            # Keep the binding decision and its approving actor visible in the
            # established device audit after the transient request is retired.
            audit(c, _actor(actor), "cli.product.claim", existing["id"])
        return _bind_view(existing, staff, already_bound)

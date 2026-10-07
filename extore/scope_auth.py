"""Merchant-reviewed, signed device requests for persistent product scopes."""

import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import time
import unicodedata
import uuid
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from . import pipeline_scopes as scopes
from .cli_auth import _body, _decode, _handshake, _verify
from .config import ORIGIN
from .db import audit, db
from .device_login import (
    CLEANUP_BATCH,
    CLEANUP_GRACE,
    CODE_ALPHABET,
    MAX_PENDING_PER_KEY,
    MAX_POLL_INTERVAL,
    MAX_REQUESTS,
    POLL_INTERVAL,
    REQUEST_TTL,
    DeviceProof,
    _actor,
    _bad_code,
    _browser,
    _InvalidCode,
    _review_digest,
)
from .security import authorize_management, fail, rate_limit, session, token
from .shops import shop_row

router = APIRouter(prefix="/api")


def init_schema(c):
    c.execute(
        "CREATE TABLE IF NOT EXISTS cli_scope_requests ("
        "id TEXT PRIMARY KEY,public_key TEXT NOT NULL,client_name TEXT NOT NULL,"
        "fingerprint TEXT NOT NULL,nonce TEXT NOT NULL,user_code TEXT UNIQUE NOT NULL,"
        "challenge TEXT NOT NULL,request_json TEXT NOT NULL,kind TEXT NOT NULL "
        "CHECK(kind IN ('product','shop.pipeline')),shop_id TEXT NOT NULL REFERENCES shops(id),"
        "requested_product_ids TEXT NOT NULL,requested_permissions TEXT NOT NULL,"
        "grant_expires REAL NOT NULL,current_authorization_id TEXT "
        "REFERENCES pipeline_authorizations(id),current_revision INTEGER,"
        "expires REAL NOT NULL,created REAL NOT NULL,state TEXT NOT NULL DEFAULT 'pending' "
        "CHECK(state IN ('pending','approved','claimed','denied')),"
        "approved_draft TEXT,approved_snapshot TEXT,approved_session_digest TEXT "
        "REFERENCES sessions(digest) ON DELETE SET NULL,approved_role TEXT,"
        "approved_shop_id TEXT,authorization_id TEXT REFERENCES pipeline_authorizations(id),"
        "claimed_revision INTEGER,poll_after REAL NOT NULL DEFAULT 0,"
        "poll_interval INTEGER NOT NULL DEFAULT 5,UNIQUE(public_key,nonce))"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS cli_scope_request_expiry ON cli_scope_requests(expires)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS cli_scope_request_shop ON cli_scope_requests(shop_id,state)"
    )


class ScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    public_key: str = Field(min_length=43, max_length=43)
    client_name: str = Field(min_length=1, max_length=100)
    nonce: str = Field(min_length=43, max_length=43)
    kind: Literal["product", "shop.pipeline"]
    shop_id: str | None = None
    product_ids: list[str] = Field(max_length=scopes.MAX_PRODUCTS)
    permissions: list[str] = Field(
        min_length=1, max_length=len(scopes.LINK_PERMISSIONS)
    )
    authorization_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=1, strict=True)
    reason: str = Field(default="", max_length=1000)
    signature: str = Field(min_length=86, max_length=86)


class ScopeOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_code: str = Field(min_length=1, max_length=64)
    product_ids: list[str] | None = Field(default=None, max_length=scopes.MAX_PRODUCTS)
    permissions: list[str] | None = Field(
        default=None, max_length=len(scopes.LINK_PERMISSIONS)
    )
    expires: float | None = None


class ScopeApproval(ScopeOptions):
    product_ids: list[str] = Field(min_length=1, max_length=scopes.MAX_PRODUCTS)
    permissions: list[str] = Field(
        min_length=1, max_length=len(scopes.LINK_PERMISSIONS)
    )
    expires: float
    review_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ScopeDeny(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_code: str = Field(min_length=1, max_length=64)


class ScopeRevoke(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _uuid(value):
    if not isinstance(value, str):
        fail("授权参数无效", 400)
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except ValueError:
        fail("授权参数无效", 400)
    return value


def cleanup_requests(c, now=None):
    now = time.time() if now is None else now
    return c.execute(
        "DELETE FROM cli_scope_requests WHERE id IN (SELECT id FROM cli_scope_requests "
        "WHERE expires<? ORDER BY expires,id LIMIT ?)",
        (now - CLEANUP_GRACE, CLEANUP_BATCH),
    ).rowcount


def _request(c, rid):
    row = c.execute("SELECT * FROM cli_scope_requests WHERE id=?", (rid,)).fetchone()
    if row is None:
        fail("设备授权请求无效", 401)
    return row


def _normalize_code(value):
    if not isinstance(value, str):
        raise _InvalidCode
    code = value.strip().upper().replace("-", "")
    if len(code) != 12 or any(char not in CODE_ALPHABET for char in code):
        raise _InvalidCode
    return "-".join(code[i : i + 4] for i in range(0, 12, 4))


async def handles_code(request):
    """Dispatch without consuming credentials or exposing code validity."""
    try:
        raw = await request.json()
        code = _normalize_code(raw.get("user_code") if isinstance(raw, dict) else None)
    except (ValueError, TypeError, _InvalidCode):
        return False
    with db() as c:
        return (
            c.execute(
                "SELECT 1 FROM cli_scope_requests WHERE user_code=?", (code,)
            ).fetchone()
            is not None
        )


def _pending(c, value):
    code = _normalize_code(value)
    row = c.execute(
        "SELECT * FROM cli_scope_requests WHERE user_code=?", (code,)
    ).fetchone()
    if row is None or row["expires"] <= time.time() or row["state"] != "pending":
        raise _InvalidCode
    return row


def _owner(c, actor, shop_id):
    authorize_management(c, actor)
    if actor["role"] != "admin" or (
        actor.get("shop_id") is not None and actor["shop_id"] != shop_id
    ):
        fail("此设备申请需要对应店铺的店长批准", 403)
    return shop_row(c, shop_id)


def _current(c, row):
    aid = row["current_authorization_id"]
    if aid is None:
        return None
    value = scopes.authorization(c, aid)
    raw = c.execute(
        "SELECT public_key FROM pipeline_authorizations WHERE id=?", (aid,)
    ).fetchone()
    if (
        value["revision"] != row["current_revision"]
        or raw is None
        or not hmac.compare_digest(raw["public_key"], row["public_key"])
        or value["shop_id"] != row["shop_id"]
        or value["kind"] != row["kind"]
    ):
        fail("原授权已变化，请重新申请追加权限", 409)
    scopes._bindings(c, aid)
    return value


def _products(c, row):
    result = []
    for pid in json.loads(row["requested_product_ids"]):
        product = scopes._product(c, pid, row["shop_id"], row["kind"])
        config = json.loads(product["config"])
        result.append({"id": pid, "name": config["name"], "mode": config["mode"]})
    return result


def _selected(c, row, body):
    current = _current(c, row)
    requested_products = json.loads(row["requested_product_ids"])
    requested_permissions = json.loads(row["requested_permissions"])
    pids = requested_products if body.product_ids is None else body.product_ids
    perms = requested_permissions if body.permissions is None else body.permissions
    if (
        len(pids) != len(set(pids))
        or not pids
        or not set(pids).issubset(requested_products)
        or not set(perms).issubset(requested_permissions)
    ):
        fail("批准范围不能超过本次设备申请", 403)
    perms = scopes._permissions(perms, row["kind"])
    expires = row["grant_expires"] if body.expires is None else body.expires
    if (
        isinstance(expires, bool)
        or not isinstance(expires, (int, float))
        or not math.isfinite(expires)
        or expires <= time.time()
        or expires > row["grant_expires"]
    ):
        fail("批准的有效期超出申请范围", 400)
    draft = {
        "shop_id": row["shop_id"],
        "kind": row["kind"],
        "product_ids": sorted(pids),
        "permissions": perms,
        "expires": expires,
    }
    existing = None
    if current:
        existing = c.execute(
            "SELECT * FROM pipeline_authorizations WHERE id=?", (current["id"],)
        ).fetchone()
    scopes._draft(c, draft, existing)
    return draft, current


def _snapshot(c, row, draft):
    # Live benign last_seen/poll fields are excluded. Product config, ownership,
    # bindings, ancestor authority and the complete reviewed selection are pinned.
    current = _current(c, row)
    shop = shop_row(c, row["shop_id"])
    products = []
    for pid in json.loads(row["requested_product_ids"]):
        product = scopes._product(c, pid, row["shop_id"], row["kind"])
        products.append(
            {
                "id": pid,
                "shop_id": product["shop_id"],
                "config_hash": hashlib.sha256(product["config"].encode()).hexdigest(),
            }
        )
    history = None
    if current:
        raw = c.execute(
            "SELECT * FROM pipeline_authorizations WHERE id=?", (current["id"],)
        ).fetchone()
        history = {
            "scope": {
                k: raw[k]
                for k in (
                    "id",
                    "shop_id",
                    "kind",
                    "public_key",
                    "fingerprint",
                    "client_name",
                    "permissions",
                    "expires",
                    "revision",
                    "revoked",
                    "issuer_role",
                    "issuer_shop_id",
                    "approved_actor",
                )
            },
            "bindings": [],
        }
        for binding in c.execute(
            "SELECT b.product_id,b.staff_id,b.device_id,s.permissions,s.expires,"
            "s.revoked,s.parent_id,d.public_key,d.fingerprint,d.revoked AS device_revoked "
            "FROM pipeline_bindings b JOIN staff s ON s.id=b.staff_id "
            "JOIN cli_devices d ON d.id=b.device_id WHERE b.authorization_id=? "
            "ORDER BY b.product_id",
            (current["id"],),
        ):
            history["bindings"].append(dict(binding))
    return _canonical(
        {
            "request": {
                k: row[k]
                for k in (
                    "id",
                    "public_key",
                    "client_name",
                    "fingerprint",
                    "nonce",
                    "challenge",
                    "request_json",
                    "kind",
                    "shop_id",
                    "requested_product_ids",
                    "requested_permissions",
                    "grant_expires",
                    "current_authorization_id",
                    "current_revision",
                    "expires",
                )
            },
            "shop": {
                "id": shop["id"],
                "name": shop["name"],
                "enabled": shop["enabled"],
            },
            "products": products,
            "current": history,
            "selected": draft,
        }
    )


def _request_view(row):
    return {
        "request_id": row["id"],
        "user_code": row["user_code"],
        "approval_url": ORIGIN + "/cli/device",
        "challenge": row["challenge"],
        "expires": row["expires"],
        "expires_in": max(0, int(row["expires"] - time.time() + 0.999)),
        "interval": row["poll_interval"],
        "fingerprint": row["fingerprint"],
        "flow": "scope",
    }


@router.post("/cli/scopes/request")
async def create_request(request: Request):
    _handshake(request)
    rate_limit(request, "scope-request", 10, 60)
    body = await _body(request, ScopeRequest)
    unsigned = body.model_dump(exclude={"signature"})
    canonical = _canonical(unsigned)
    raw = _verify(
        body.public_key,
        body.signature,
        f"extore-cli-scope-request-v1\n{ORIGIN}\n{canonical}",
    )
    issued = int.from_bytes(_decode(body.nonce, 32)[:8], "big")
    if body.client_name != body.client_name.strip() or any(
        unicodedata.category(char).startswith("C") for char in body.client_name
    ):
        fail("设备名称无效", 400)
    if any(
        unicodedata.category(char).startswith("C") and char not in "\n\t"
        for char in body.reason
    ):
        fail("申请原因无效", 400)
    for value in [*body.product_ids, body.shop_id, body.authorization_id]:
        if value is not None:
            _uuid(value)
    if len(body.product_ids) != len(set(body.product_ids)):
        fail("授权商品重复", 400)
    permissions = scopes._permissions(body.permissions, body.kind)
    if (body.authorization_id is None) != (body.expected_revision is None):
        fail("追加授权版本无效", 400)
    with db() as c:
        found = c.execute(
            "SELECT * FROM cli_scope_requests WHERE public_key=? AND nonce=?",
            (body.public_key, body.nonce),
        ).fetchone()
        if found:
            if found["request_json"] != canonical:
                fail("设备授权申请不匹配", 401)
            return _request_view(found)
        now = time.time()
        if issued > now + 60 or issued + REQUEST_TTL <= now:
            fail("设备申请签名已过期，请生成新的设备码", 401)
        cleanup_requests(c, now)
        if (
            c.execute("SELECT count(*) FROM cli_scope_requests").fetchone()[0]
            >= MAX_REQUESTS
            or c.execute(
                "SELECT count(*) FROM cli_scope_requests WHERE public_key=? AND expires>? "
                "AND state IN ('pending','approved')",
                (body.public_key, now),
            ).fetchone()[0]
            >= MAX_PENDING_PER_KEY
        ):
            fail("设备授权申请过多，请稍后重试", 429)
        pids = list(body.product_ids)
        sid = body.shop_id
        if body.kind == "product":
            if len(pids) != 1:
                fail("单商品申请必须指定一个商品", 400)
            product = c.execute(
                "SELECT shop_id FROM products WHERE id=?", (pids[0],)
            ).fetchone()
            if product is None:
                fail("商品不可用", 404)
            if sid is not None and sid != product["shop_id"]:
                fail("商品不属于申请的店铺", 403)
            sid = product["shop_id"]
        elif sid is None:
            fail("全店流水线申请必须指定店铺", 400)
        shop_row(c, sid)
        if body.kind == "shop.pipeline" and not pids:
            rows = c.execute(
                "SELECT id,config FROM products WHERE shop_id=? AND NOT EXISTS (SELECT 1 FROM product_lifecycle l WHERE l.product_id=products.id AND l.deleted_at IS NOT NULL) ORDER BY id",
                (sid,),
            ).fetchall()
            pids = [
                r["id"] for r in rows if json.loads(r["config"]).get("mode") == "manual"
            ]
        if not pids or len(pids) > scopes.MAX_PRODUCTS:
            fail("本次可申请的队列商品数量无效", 400)
        for pid in pids:
            scopes._product(c, pid, sid, body.kind)
        current = None
        if body.authorization_id:
            current = scopes.authorization(c, body.authorization_id)
            auth = c.execute(
                "SELECT public_key FROM pipeline_authorizations WHERE id=?",
                (current["id"],),
            ).fetchone()
            if not hmac.compare_digest(auth["public_key"], body.public_key):
                fail("只能用原设备密钥申请追加授权", 401)
            if current["revision"] != body.expected_revision and (
                current["revision"] < body.expected_revision
                or current["product_ids"] != sorted(pids)
                or current["permissions"] != permissions
            ):
                fail("原授权版本已变化，请刷新后再申请", 409)
        else:
            current = scopes.find_recovery(
                c, sid, body.kind, body.public_key, sorted(pids), permissions
            )
        if current and body.kind == "shop.pipeline":
            pids = sorted(set(pids) | set(current["product_ids"]))
        grant_expires = (
            current["expires"] if current else now + scopes.DEFAULT_DAYS * 86400
        )
        draft = {
            "shop_id": sid,
            "kind": body.kind,
            "product_ids": pids,
            "permissions": permissions,
            "expires": grant_expires,
        }
        existing = (
            c.execute(
                "SELECT * FROM pipeline_authorizations WHERE id=?", (current["id"],)
            ).fetchone()
            if current
            else None
        )
        scopes._draft(c, draft, existing)
        if current:
            scopes._bindings(c, current["id"])
        rid = str(uuid.uuid4())
        for _ in range(8):
            text = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
            code = "-".join(text[i : i + 4] for i in range(0, 12, 4))
            if c.execute(
                "SELECT 1 FROM cli_device_requests WHERE user_code=?", (code,)
            ).fetchone():
                continue
            try:
                c.execute(
                    "INSERT INTO cli_scope_requests(id,public_key,client_name,fingerprint,nonce,"
                    "user_code,challenge,request_json,kind,shop_id,requested_product_ids,"
                    "requested_permissions,grant_expires,current_authorization_id,current_revision,"
                    "expires,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        rid,
                        body.public_key,
                        body.client_name,
                        hashlib.sha256(raw).hexdigest(),
                        body.nonce,
                        code,
                        token(),
                        canonical,
                        body.kind,
                        sid,
                        _canonical(sorted(pids)),
                        _canonical(permissions),
                        grant_expires,
                        current["id"] if current else None,
                        current["revision"] if current else None,
                        min(now + REQUEST_TTL, issued + REQUEST_TTL),
                        now,
                    ),
                )
                break
            except sqlite3.IntegrityError:
                continue
        else:
            fail("暂时无法生成设备码，请稍后重试", 503)
        audit(c, "pending", "cli.scope.request", rid)
        return _request_view(_request(c, rid))


@router.post("/cli/scopes/status")
async def request_status(request: Request):
    _handshake(request)
    rate_limit(request, "scope-poll", 120, 60)
    body = await _body(request, DeviceProof)
    _verify(
        body.public_key,
        body.signature,
        f"extore-cli-scope-status-v1\n{ORIGIN}\n{body.request_id}\n{body.public_key}",
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
                "UPDATE cli_scope_requests SET poll_interval=?,poll_after=? WHERE id=?",
                (interval, now + interval, row["id"]),
            )
            return {
                "status": "slow_down",
                "expires": row["expires"],
                "interval": interval,
                "retry_after": interval,
            }
        c.execute(
            "UPDATE cli_scope_requests SET poll_after=? WHERE id=?",
            (now + row["poll_interval"], row["id"]),
        )
        return result


async def approval_options(request: Request):
    actor = _browser(request, "scope-options")
    body = await _body(request, ScopeOptions)
    try:
        with db() as c:
            row = _pending(c, body.user_code)
            shop = _owner(c, actor, row["shop_id"])
            draft, current = _selected(c, row, body)
            signed = json.loads(row["request_json"])
            result = {
                "flow": "scope",
                "request": {
                    "request_id": row["id"],
                    "user_code": row["user_code"],
                    "client_name": row["client_name"],
                    "fingerprint": row["fingerprint"],
                    "expires": row["expires"],
                    "kind": row["kind"],
                    "shop_id": row["shop_id"],
                    "requested_product_ids": json.loads(row["requested_product_ids"]),
                    "requested_permissions": json.loads(row["requested_permissions"]),
                    "grant_expires": row["grant_expires"],
                    "authorization_id": signed["authorization_id"],
                    "expected_revision": (
                        row["current_revision"] if signed["authorization_id"] else None
                    ),
                    "reason": signed["reason"],
                },
                "shop": {"id": shop["id"], "name": shop["name"]},
                "products": _products(c, row),
                "current": current,
                "selected": draft,
                "snapshot_digest": hashlib.sha256(
                    _snapshot(c, row, draft).encode()
                ).hexdigest(),
                "review_digest": _review_digest(_snapshot(c, row, draft), actor),
            }
            return result
    except _InvalidCode:
        _bad_code(request, actor)


async def approve_request(request: Request):
    from .account_auth import require_recent

    actor = _browser(request, "scope-approve")
    body = await _body(request, ScopeApproval)
    try:
        with db() as c:
            row = _pending(c, body.user_code)
            _owner(c, actor, row["shop_id"])
            require_recent(actor)
            draft, _ = _selected(c, row, body)
            snapshot = _snapshot(c, row, draft)
            if not hmac.compare_digest(
                _review_digest(snapshot, actor), body.review_digest
            ):
                fail("申请或商品授权范围已变化，请重新核对", 409)
            c.execute(
                "UPDATE cli_scope_requests SET state='approved',approved_draft=?,approved_snapshot=?,"
                "approved_session_digest=?,approved_role=?,approved_shop_id=? WHERE id=? AND state='pending'",
                (
                    _canonical(draft),
                    snapshot,
                    actor["digest"],
                    actor["role"],
                    actor.get("shop_id"),
                    row["id"],
                ),
            )
            audit(c, _actor(actor), "cli.scope.approve", row["id"])
            return {"ok": True, "status": "approved"}
    except _InvalidCode:
        _bad_code(request, actor)


async def deny_request(request: Request):
    actor = _browser(request, "scope-deny")
    body = await _body(request, ScopeDeny)
    try:
        with db() as c:
            row = _pending(c, body.user_code)
            _owner(c, actor, row["shop_id"])
            c.execute(
                "UPDATE cli_scope_requests SET state='denied',approved_session_digest=?,"
                "approved_role=?,approved_shop_id=? WHERE id=? AND state='pending'",
                (actor["digest"], actor["role"], actor.get("shop_id"), row["id"]),
            )
            audit(c, _actor(actor), "cli.scope.deny", row["id"])
            return {"ok": True, "status": "denied"}
    except _InvalidCode:
        _bad_code(request, actor)


def _approval_actor(c, row):
    actor = c.execute(
        "SELECT * FROM sessions WHERE digest=? AND revoked=0 AND expires>? AND channel='browser'",
        (row["approved_session_digest"], time.time()),
    ).fetchone()
    if (
        actor is None
        or actor["role"] != row["approved_role"]
        or actor["shop_id"] != row["approved_shop_id"]
    ):
        fail("浏览器设备审批已失效，请重新申请", 401)
    actor = dict(actor)
    _owner(c, actor, row["shop_id"])
    return actor


@router.post("/cli/scopes/claim")
async def claim_request(request: Request):
    _handshake(request)
    rate_limit(request, "scope-claim", 30, 60)
    body = await _body(request, DeviceProof)
    with db() as c:
        row = _request(c, body.request_id)
        _verify(
            body.public_key,
            body.signature,
            f"extore-cli-scope-claim-v1\n{ORIGIN}\n{row['id']}\n{row['challenge']}\n{body.public_key}",
        )
        if (
            not hmac.compare_digest(row["public_key"], body.public_key)
            or row["expires"] <= time.time()
            or row["state"] not in ("approved", "claimed")
        ):
            fail("设备授权尚未批准或已失效", 401)
        if row["state"] == "claimed":
            value = scopes.authorization(c, row["authorization_id"])
            raw = c.execute(
                "SELECT public_key FROM pipeline_authorizations WHERE id=?",
                (value["id"],),
            ).fetchone()
            if value["revision"] != row["claimed_revision"] or not hmac.compare_digest(
                raw["public_key"], body.public_key
            ):
                fail("已领取的授权发生变化，请刷新授权信息", 409)
            return {
                "authorization": value,
                "bindings": scopes._bindings(c, value["id"]),
            }
        actor = _approval_actor(c, row)
        draft = json.loads(row["approved_draft"])
        if row["approved_snapshot"] != _snapshot(c, row, draft):
            fail("已批准的范围发生变化，请重新申请", 409)
        # Approval never mutates the old grant. Only this signed atomic claim
        # replaces its complete effective permissions and provenance.
        result = scopes.materialize(
            c,
            draft,
            body.public_key,
            row["client_name"],
            row["fingerprint"],
            _actor(actor),
            existing_authorization_id=row["current_authorization_id"],
            expected_revision=row["current_revision"],
            issuer_role="shop" if actor.get("shop_id") else "root",
            issuer_shop_id=actor.get("shop_id"),
        )
        c.execute(
            "UPDATE cli_scope_requests SET state='claimed',authorization_id=?,claimed_revision=? WHERE id=?",
            (
                result["authorization"]["id"],
                result["authorization"]["revision"],
                row["id"],
            ),
        )
        audit(c, _actor(actor), "cli.scope.claim", result["authorization"]["id"])
        return result


@router.get("/admin/pipeline-authorizations")
async def list_authorizations(request: Request):
    actor = session(request)
    view = request.query_params.get("view", "active")
    sid = request.query_params.get("shop_id") or actor.get("shop_id")
    if view not in ("active", "revoked", "all"):
        fail("授权列表参数无效", 400)
    try:
        limit = int(request.query_params.get("limit", "200"))
        if not 1 <= limit <= 200:
            raise ValueError
    except ValueError:
        fail("授权列表参数无效", 400)
    if actor.get("shop_id") and sid != actor["shop_id"]:
        fail("没有此店铺的管理权限", 403)
    if sid:
        _uuid(sid)
    with db() as c:
        authorize_management(c, actor)
        sql, values = "SELECT id FROM pipeline_authorizations WHERE 1=1", []
        if sid:
            sql += " AND shop_id=?"
            values.append(sid)
        if view == "active":
            sql += " AND revoked=0 AND expires>?"
            values.append(time.time())
        elif view == "revoked":
            sql += " AND (revoked=1 OR expires<=?)"
            values.append(time.time())
        sql += " ORDER BY created DESC,id LIMIT ?"
        values.append(limit)
        result = []
        for row in c.execute(sql, values).fetchall():
            raw = c.execute(
                "SELECT * FROM pipeline_authorizations WHERE id=?", (row["id"],)
            ).fetchone()
            value = scopes._view(c, raw) | {"revoked": raw["revoked"]}
            shop = c.execute(
                "SELECT name FROM shops WHERE id=?", (value["shop_id"],)
            ).fetchone()
            products = []
            for pid in value["product_ids"]:
                product = c.execute(
                    "SELECT config,shop_id FROM products WHERE id=?", (pid,)
                ).fetchone()
                if product and product["shop_id"] == value["shop_id"]:
                    config = json.loads(product["config"])
                    products.append(
                        {"id": pid, "name": config["name"], "mode": config["mode"]}
                    )
                else:
                    # Keep the approved ID for audit without exposing content
                    # that now belongs to another merchant.
                    products.append(
                        {"id": pid, "name": "商品已不可用", "mode": "unavailable"}
                    )
            result.append(
                {
                    **value,
                    "shop_name": shop["name"] if shop else "",
                    "products": products,
                    "bindings_count": len(value["product_ids"]),
                }
            )
        return result


@router.delete("/admin/pipeline-authorizations/{authorization_id}")
async def revoke_authorization(authorization_id: str, request: Request):
    from .account_auth import require_recent

    actor = session(request)
    body = await _body(request, ScopeRevoke)
    _uuid(authorization_id)
    require_recent(actor)
    with db() as c:
        authorize_management(c, actor)
        row = c.execute(
            "SELECT * FROM pipeline_authorizations WHERE id=?", (authorization_id,)
        ).fetchone()
        if row is None:
            fail("授权不存在", 404)
        if actor.get("shop_id") and row["shop_id"] != actor["shop_id"]:
            fail("没有此店铺的管理权限", 403)
        if row["revision"] != body.expected_revision:
            fail("授权范围已变化，请重新核对后撤销", 409)
        result = scopes.revoke_authorization(c, authorization_id, _actor(actor))
        return {
            "ok": True,
            "id": authorization_id,
            "revoked_bindings": result["bindings"],
            "released_jobs": result["jobs"],
        }

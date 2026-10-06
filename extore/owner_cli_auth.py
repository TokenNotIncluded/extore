"""Passkey-approved merchant devices and one-use signatures for CLI writes."""

import hashlib
import hmac
import json
import re
import secrets
import time
import unicodedata
import uuid

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from webauthn import generate_authentication_options, verify_authentication_response
from webauthn.helpers import options_to_json
from webauthn.helpers.structs import UserVerificationRequirement

from .cli_auth import _body, _decode, _handshake, _verify
from .config import COOKIE_SECURE, ORIGIN, RP_ID
from .db import audit, db
from .security import (
    authorize_management,
    digest,
    fail,
    rate_limit,
    require_cli_bearer,
    session,
    token,
)

router = APIRouter(prefix="/api")
REQUEST_TTL = 600
CHALLENGE_TTL = 300
GRANT_TTL = 30 * 86400
SESSION_TTL = 28800
SCOPE = "shop.owner"
APPROVAL_COOKIE = "extore_owner_approval"


def init_schema(c):
    c.execute(
        "CREATE TABLE IF NOT EXISTS owner_cli_devices (id TEXT PRIMARY KEY,public_key TEXT UNIQUE NOT NULL,client_name TEXT NOT NULL,fingerprint TEXT NOT NULL,expires REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),approved_credential_id TEXT NOT NULL,created REAL NOT NULL,last_seen REAL NOT NULL)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS owner_cli_requests (id TEXT PRIMARY KEY,public_key TEXT NOT NULL,client_name TEXT NOT NULL,fingerprint TEXT NOT NULL,nonce TEXT NOT NULL,device_code TEXT NOT NULL,challenge TEXT NOT NULL,scope TEXT NOT NULL,expires REAL NOT NULL,grant_expires REAL NOT NULL,state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','approved','claimed','denied')),approved_credential_id TEXT,approved_snapshot TEXT,device_id TEXT REFERENCES owner_cli_devices(id),created REAL NOT NULL,UNIQUE(public_key,nonce))"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS owner_cli_approval_challenges (digest TEXT PRIMARY KEY,request_id TEXT NOT NULL REFERENCES owner_cli_requests(id) ON DELETE CASCADE,challenge BLOB NOT NULL,snapshot TEXT NOT NULL,expires REAL NOT NULL,created REAL NOT NULL)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS owner_cli_challenges (id TEXT PRIMARY KEY,device_id TEXT NOT NULL REFERENCES owner_cli_devices(id),challenge TEXT NOT NULL,expires REAL NOT NULL,created REAL NOT NULL)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS owner_cli_action_challenges (id TEXT PRIMARY KEY,device_id TEXT NOT NULL REFERENCES owner_cli_devices(id),session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,challenge TEXT NOT NULL,method TEXT NOT NULL,path TEXT NOT NULL,body_sha256 TEXT NOT NULL,expires REAL NOT NULL,created REAL NOT NULL)"
    )
    columns = {r["name"] for r in c.execute("PRAGMA table_info(sessions)")}
    if "owner_device_id" not in columns:
        c.execute(
            "ALTER TABLE sessions ADD COLUMN owner_device_id TEXT REFERENCES owner_cli_devices(id)"
        )
    request_columns = {
        r["name"] for r in c.execute("PRAGMA table_info(owner_cli_requests)")
    }
    if "approved_snapshot" not in request_columns:
        c.execute("ALTER TABLE owner_cli_requests ADD COLUMN approved_snapshot TEXT")
    c.execute(
        "CREATE INDEX IF NOT EXISTS sessions_owner_device ON sessions(owner_device_id)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS owner_cli_request_expiry ON owner_cli_requests(expires)"
    )


class KeyProof(BaseModel):
    model_config = ConfigDict(extra="forbid")
    public_key: str = Field(min_length=43, max_length=43)
    signature: str = Field(min_length=86, max_length=86)


class OwnerRequest(KeyProof):
    client_name: str = Field(min_length=1, max_length=100)
    nonce: str = Field(min_length=43, max_length=43)


class RequestProof(KeyProof):
    request_id: str = Field(min_length=1, max_length=100)


class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=100)
    device_code: str = Field(min_length=1, max_length=64)


class ApprovalVerify(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=100)
    credential: dict


class DeviceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str = Field(min_length=1, max_length=100)


class DeviceProof(DeviceInput):
    challenge_id: str = Field(min_length=1, max_length=100)
    signature: str = Field(min_length=86, max_length=86)


class ActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: str = Field(pattern=r"^(POST|PUT|PATCH|DELETE)$")
    path: str = Field(min_length=1, max_length=2048)
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _snapshot(row):
    return json.dumps(
        {
            k: row[k]
            for k in (
                "id",
                "public_key",
                "client_name",
                "scope",
                "grant_expires",
                "fingerprint",
                "nonce",
                "device_code",
                "challenge",
                "expires",
            )
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _request(c, rid):
    row = c.execute("SELECT * FROM owner_cli_requests WHERE id=?", (rid,)).fetchone()
    if row is None:
        fail("商家设备授权请求无效", 401)
    return row


def owner_device(c, did):
    row = c.execute(
        "SELECT * FROM owner_cli_devices WHERE id=? AND revoked=0 AND expires>?",
        (did, time.time()),
    ).fetchone()
    if row is None or not c.execute("SELECT 1 FROM credentials LIMIT 1").fetchone():
        fail("商家 CLI 设备授权已失效", 401)
    return row


def _request_view(row):
    return {
        "request_id": row["id"],
        "device_code": row["device_code"],
        "approval_url": ORIGIN + "/cli/owner#" + row["id"],
        "challenge": row["challenge"],
        "expires": row["expires"],
        "expires_in": max(0, int(row["expires"] - time.time() + 0.999)),
        "interval": 5,
        "fingerprint": row["fingerprint"],
    }


@router.post("/cli/owner/request")
async def request_owner(request: Request):
    _handshake(request)
    rate_limit(request, "owner-cli-request", 10, 60)
    body = await _body(request, OwnerRequest)
    if body.client_name != body.client_name.strip() or any(
        unicodedata.category(x).startswith("C") for x in body.client_name
    ):
        fail("设备名称无效", 400)
    _decode(body.nonce, 32)
    raw = _verify(
        body.public_key,
        body.signature,
        f"extore-cli-owner-request-v1\n{ORIGIN}\n{body.public_key}\n{body.client_name}\n{body.nonce}",
    )
    with db() as c:
        c.execute(
            "DELETE FROM owner_cli_requests WHERE expires<?",
            (time.time() - 90 * 86400,),
        )
        row = c.execute(
            "SELECT * FROM owner_cli_requests WHERE public_key=? AND nonce=?",
            (body.public_key, body.nonce),
        ).fetchone()
        if row is not None:
            if row["client_name"] != body.client_name:
                fail("商家设备授权请求不匹配", 401)
            return _request_view(row)
        now, rid = time.time(), str(uuid.uuid4())
        code = "".join(
            secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(12)
        )
        code = "-".join(code[i : i + 4] for i in range(0, 12, 4))
        c.execute(
            "INSERT INTO owner_cli_requests(id,public_key,client_name,fingerprint,nonce,device_code,challenge,scope,expires,grant_expires,created) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                rid,
                body.public_key,
                body.client_name,
                hashlib.sha256(raw).hexdigest(),
                body.nonce,
                code,
                token(),
                SCOPE,
                now + REQUEST_TTL,
                now + GRANT_TTL,
                now,
            ),
        )
        audit(c, "pending", "cli.owner.request", rid)
        return _request_view(_request(c, rid))


@router.post("/cli/owner/status")
async def pending_status(request: Request):
    _handshake(request)
    rate_limit(request, "owner-cli-poll", 120, 60)
    body = await _body(request, RequestProof)
    _verify(
        body.public_key,
        body.signature,
        f"extore-cli-owner-status-v1\n{ORIGIN}\n{body.request_id}\n{body.public_key}",
    )
    with db() as c:
        row = _request(c, body.request_id)
        if not hmac.compare_digest(row["public_key"], body.public_key):
            fail("商家设备授权请求不匹配", 401)
        state = "expired" if row["expires"] <= time.time() else row["state"]
        return {
            "status": "approved" if state == "claimed" else state,
            "expires": row["expires"],
        }


@router.post("/auth/cli-owner/options")
async def approval_options(request: Request, response: Response):
    if request.headers.get("authorization") is not None:
        fail("请在浏览器使用 Passkey 批准设备", 401)
    rate_limit(request, "owner-cli-approval", 20, 60)
    body = await _body(request, ApprovalInput)
    with db() as c:
        row = _request(c, body.request_id)
        normalized = body.device_code.strip().upper().replace("-", "")
        if not re.fullmatch(r"[A-Z2-9]{12}", normalized) or not hmac.compare_digest(
            row["device_code"].replace("-", ""), normalized
        ):
            fail("设备码不匹配", 401)
        if row["expires"] <= time.time() or row["state"] != "pending":
            fail("商家设备授权请求已过期或处理", 409)
        if not c.execute("SELECT 1 FROM credentials LIMIT 1").fetchone():
            fail("请先注册商家 Passkey", 409)
        options = generate_authentication_options(
            rp_id=RP_ID, user_verification=UserVerificationRequirement.REQUIRED
        )
        value, now = token(), time.time()
        c.execute("DELETE FROM owner_cli_approval_challenges WHERE expires<=?", (now,))
        c.execute(
            "DELETE FROM owner_cli_approval_challenges WHERE digest=?",
            (digest(request.cookies.get(APPROVAL_COOKIE, "")),),
        )
        c.execute(
            "INSERT INTO owner_cli_approval_challenges VALUES (?,?,?,?,?,?)",
            (
                digest(value),
                row["id"],
                options.challenge,
                _snapshot(row),
                min(now + CHALLENGE_TTL, row["expires"]),
                now,
            ),
        )
        response.set_cookie(
            APPROVAL_COOKIE,
            value,
            httponly=True,
            secure=COOKIE_SECURE,
            samesite="strict",
            max_age=CHALLENGE_TTL,
            path="/api/auth/cli-owner",
        )
        return {
            "options": json.loads(options_to_json(options)),
            "request_id": row["id"],
            "device_code": row["device_code"],
            "client_name": row["client_name"],
            "fingerprint": row["fingerprint"],
            "role": "admin",
            "scope": SCOPE,
            "grant_expires": row["grant_expires"],
            "expires": row["expires"],
        }


@router.post("/auth/cli-owner/verify")
async def approval_verify(request: Request, response: Response):
    if request.headers.get("authorization") is not None:
        fail("请在浏览器使用 Passkey 批准设备", 401)
    rate_limit(request, "owner-cli-approval-verify", 20, 60)
    body = await _body(request, ApprovalVerify)
    with db() as c:
        row = _request(c, body.request_id)
        ch = c.execute(
            "SELECT * FROM owner_cli_approval_challenges WHERE digest=? AND expires>?",
            (digest(request.cookies.get(APPROVAL_COOKIE, "")), time.time()),
        ).fetchone()
        if (
            ch is None
            or ch["request_id"] != row["id"]
            or ch["snapshot"] != _snapshot(row)
            or row["state"] != "pending"
            or row["expires"] <= time.time()
        ):
            fail("商家设备批准请求已失效", 401)
        key = c.execute(
            "SELECT * FROM credentials WHERE id=?", (body.credential.get("id", ""),)
        ).fetchone()
        if key is None:
            fail("Passkey 未注册", 401)
        try:
            verified = verify_authentication_response(
                credential=body.credential,
                expected_challenge=ch["challenge"],
                expected_rp_id=RP_ID,
                expected_origin=ORIGIN,
                credential_public_key=key["public_key"],
                credential_current_sign_count=key["sign_count"],
                require_user_verification=True,
            )
        except Exception:
            fail("Passkey 校验失败", 401)
        c.execute(
            "UPDATE credentials SET sign_count=? WHERE id=?",
            (verified.new_sign_count, key["id"]),
        )
        c.execute(
            "UPDATE owner_cli_requests SET state='approved',approved_credential_id=?,approved_snapshot=? WHERE id=?",
            (key["id"], _snapshot(row), row["id"]),
        )
        c.execute(
            "DELETE FROM owner_cli_approval_challenges WHERE request_id=?", (row["id"],)
        )
        audit(c, "owner", "cli.owner.approve", row["id"])
    response.delete_cookie(APPROVAL_COOKIE, path="/api/auth/cli-owner")
    return {"ok": True, "status": "approved"}


@router.post("/cli/owner/claim")
async def claim_owner(request: Request):
    _handshake(request)
    rate_limit(request, "owner-cli-claim", 30, 60)
    body = await _body(request, RequestProof)
    with db() as c:
        row = _request(c, body.request_id)
        if (
            row["public_key"] != body.public_key
            or row["expires"] <= time.time()
            or row["state"] not in ("approved", "claimed")
            or row["approved_snapshot"] != _snapshot(row)
        ):
            fail("商家设备授权尚未批准或已失效", 401)
        _verify(
            body.public_key,
            body.signature,
            f"extore-cli-owner-claim-v1\n{ORIGIN}\n{row['id']}\n{row['challenge']}\n{body.public_key}",
        )
        if not c.execute("SELECT 1 FROM credentials LIMIT 1").fetchone():
            fail("商家设备授权已失效", 401)
        existing = c.execute(
            "SELECT * FROM owner_cli_devices WHERE public_key=?", (body.public_key,)
        ).fetchone()
        already = row["state"] == "claimed"
        if existing is not None and existing["revoked"]:
            fail("此商家设备已撤销，请使用新的设备密钥", 401)
        if already:
            if existing is None or existing["id"] != row["device_id"]:
                fail("商家设备授权已失效", 401)
        elif existing is not None:
            c.execute(
                "UPDATE owner_cli_devices SET expires=?,approved_credential_id=? WHERE id=?",
                (row["grant_expires"], row["approved_credential_id"], existing["id"]),
            )
        else:
            did, now = str(uuid.uuid4()), time.time()
            c.execute(
                "INSERT INTO owner_cli_devices VALUES (?,?,?,?,?,0,?,?,?)",
                (
                    did,
                    row["public_key"],
                    row["client_name"],
                    row["fingerprint"],
                    row["grant_expires"],
                    row["approved_credential_id"],
                    now,
                    now,
                ),
            )
            existing = c.execute(
                "SELECT * FROM owner_cli_devices WHERE id=?", (did,)
            ).fetchone()
            audit(c, "owner", "cli.owner.device.create", did)
        c.execute(
            "UPDATE owner_cli_requests SET state='claimed',device_id=? WHERE id=?",
            (existing["id"], row["id"]),
        )
        device = owner_device(c, existing["id"])
        return {
            "device_id": device["id"],
            "role": "admin",
            "scope": SCOPE,
            "client_name": device["client_name"],
            "fingerprint": device["fingerprint"],
            "expires": device["expires"],
            "already_authorized": already,
        }


@router.post("/cli/owner/challenge")
async def owner_challenge(request: Request):
    _handshake(request)
    rate_limit(request, "owner-cli-challenge", 60, 60)
    body = await _body(request, DeviceInput)
    with db() as c:
        device = owner_device(c, body.device_id)
        now, cid, value = time.time(), str(uuid.uuid4()), token()
        c.execute("DELETE FROM owner_cli_challenges WHERE expires<=?", (now,))
        if (
            c.execute(
                "SELECT count(*) FROM owner_cli_challenges WHERE device_id=?",
                (device["id"],),
            ).fetchone()[0]
            >= 20
        ):
            fail("设备请求过于频繁", 429)
        expires = min(now + CHALLENGE_TTL, device["expires"])
        c.execute(
            "INSERT INTO owner_cli_challenges VALUES (?,?,?,?,?)",
            (cid, device["id"], value, expires, now),
        )
        return {
            "challenge_id": cid,
            "challenge": value,
            "expires": expires,
            "expires_in": max(0, int(expires - now)),
        }


@router.post("/cli/owner/session")
async def owner_login(request: Request):
    from .link_access import cleanup_sessions, request_metadata

    _handshake(request)
    rate_limit(request, "owner-cli-session", 60, 60)
    body = await _body(request, DeviceProof)
    with db() as c:
        device = owner_device(c, body.device_id)
        ch = c.execute(
            "SELECT * FROM owner_cli_challenges WHERE id=? AND device_id=? AND expires>?",
            (body.challenge_id, device["id"], time.time()),
        ).fetchone()
        if ch is None:
            fail("商家设备登录验证已失效", 401)
        _verify(
            device["public_key"],
            body.signature,
            f"extore-cli-owner-session-v1\n{ORIGIN}\n{device['id']}\n{ch['id']}\n{ch['challenge']}",
        )
        now, sid, access = time.time(), str(uuid.uuid4()), token()
        expires = min(now + SESSION_TTL, device["expires"])
        cleanup_sessions(c, now)
        c.execute("DELETE FROM owner_cli_challenges WHERE id=?", (ch["id"],))
        ip, ua = request_metadata(request)
        c.execute(
            "INSERT INTO sessions(digest,role,expires,created,id,last_seen,ip,ua,revoked,channel,owner_device_id,client_name) VALUES (?,'admin',?,?,?,?,?,?,0,'cli',?,?)",
            (
                digest(access),
                expires,
                now,
                sid,
                now,
                ip,
                ua,
                device["id"],
                device["client_name"],
            ),
        )
        c.execute(
            "UPDATE owner_cli_devices SET last_seen=? WHERE id=?", (now, device["id"])
        )
        audit(c, "owner", "session.create", sid)
        return {
            "access_token": access,
            "token_type": "Bearer",
            "role": "admin",
            "scope": SCOPE,
            "expires": expires,
            "expires_in": max(0, int(expires - now)),
            "device_id": device["id"],
            "session_id": sid,
        }


def owner_session(request):
    s = require_cli_bearer(request)
    if s["role"] != "admin" or not s.get("owner_device_id"):
        fail("此接口需要商家 CLI 授权", 401)
    return s


@router.get("/cli/owner/status")
def owner_status(request: Request):
    s = owner_session(request)
    return {
        "role": "admin",
        "channel": "cli",
        "scope": SCOPE,
        "origin": ORIGIN,
        "device_id": s["owner_device_id"],
        "client_name": s["client_name"],
        "fingerprint": s["fingerprint"],
        "session_id": s["id"],
        "expires": s["expires"],
        "grant_expires": s["grant_expires"],
    }


@router.delete("/cli/owner/session")
def owner_logout(request: Request):
    from .link_access import revoke_session

    s = owner_session(request)
    with db() as c:
        authorize_management(c, s)
        revoke_session(c, s["digest"], "owner", "session.logout")
    return {"ok": True, "id": s["id"], "current": True}


@router.post("/cli/owner/action-challenge")
async def action_challenge(request: Request):
    s = owner_session(request)
    rate_limit(request, "owner-cli-action", 120, 60)
    body = await _body(request, ActionInput)
    if (
        not body.path.startswith("/api/")
        or "#" in body.path
        or any(unicodedata.category(x).startswith("C") for x in body.path)
    ):
        fail("操作路径无效", 400)
    with db() as c:
        authorize_management(c, s)
        now, cid, value = time.time(), str(uuid.uuid4()), token()
        c.execute("DELETE FROM owner_cli_action_challenges WHERE expires<=?", (now,))
        expires = min(now + CHALLENGE_TTL, s["expires"], s["grant_expires"])
        c.execute(
            "INSERT INTO owner_cli_action_challenges VALUES (?,?,?,?,?,?,?,?,?)",
            (
                cid,
                s["owner_device_id"],
                s["id"],
                value,
                body.method,
                body.path,
                body.body_sha256,
                expires,
                now,
            ),
        )
        return {
            "challenge_id": cid,
            "challenge": value,
            "expires": expires,
            "expires_in": max(0, int(expires - now)),
        }


def verify_owner_cli_action(request, raw_body):
    """Consume a proof atomically before dispatch; failures need a new proof."""
    s = require_cli_bearer(request)
    if s["role"] != "admin":
        return False
    cid, signature = (
        request.headers.get("x-extore-cli-challenge", ""),
        request.headers.get("x-extore-cli-signature", ""),
    )
    if len(cid) > 100 or len(signature) != 86:
        fail("此操作需要新的商家设备签名", 401)
    try:
        raw_path = request.scope.get("raw_path", request.url.path.encode()).decode(
            "ascii"
        )
        query = request.scope.get("query_string", b"").decode("ascii")
    except UnicodeError:
        fail("操作路径编码无效", 400)
    target = raw_path + ("?" + query if query else "")
    body_hash = hashlib.sha256(raw_body).hexdigest()
    with db() as c:
        authorize_management(c, s)
        ch = c.execute(
            "SELECT * FROM owner_cli_action_challenges WHERE id=? AND expires>?",
            (cid, time.time()),
        ).fetchone()
        if (
            ch is None
            or ch["device_id"] != s["owner_device_id"]
            or ch["session_id"] != s["id"]
            or ch["method"] != request.method
            or ch["path"] != target
            or ch["body_sha256"] != body_hash
        ):
            fail("商家操作签名无效或已使用", 401)
        device = owner_device(c, s["owner_device_id"])
        _verify(
            device["public_key"],
            signature,
            f"extore-cli-owner-action-v1\n{ORIGIN}\n{device['id']}\n{s['id']}\n{ch['id']}\n{ch['challenge']}\n{request.method}\n{target}\n{body_hash}",
        )
        c.execute("DELETE FROM owner_cli_action_challenges WHERE id=?", (cid,))
        audit(c, "owner", "cli.owner.action", s["id"])
    request.state.owner_cli_action_verified = True
    return True


def revoke_owner_devices(c, actor="owner"):
    for row in c.execute("SELECT id FROM owner_cli_devices WHERE revoked=0").fetchall():
        c.execute("UPDATE owner_cli_devices SET revoked=1 WHERE id=?", (row["id"],))
        audit(c, actor, "cli.owner.device.revoke", row["id"])
    c.execute("DELETE FROM owner_cli_action_challenges")
    c.execute("DELETE FROM owner_cli_challenges")
    c.execute("DELETE FROM owner_cli_approval_challenges")
    c.execute(
        "UPDATE owner_cli_requests SET state='denied' WHERE state IN ('pending','approved')"
    )


@router.get("/admin/cli-owner-devices")
def list_owner_devices(request: Request):
    s = session(request)
    with db() as c:
        authorize_management(c, s)
        now = time.time()
        return [
            {
                "id": r["id"],
                "role": "admin",
                "scope": SCOPE,
                "client_name": r["client_name"],
                "fingerprint": r["fingerprint"],
                "created": r["created"],
                "last_seen": r["last_seen"],
                "expires": r["expires"],
                "revoked": bool(r["revoked"]),
                "active": not r["revoked"] and r["expires"] > now,
            }
            for r in c.execute("SELECT * FROM owner_cli_devices ORDER BY created DESC")
        ]


@router.delete("/admin/cli-owner-devices/{device_id}")
def revoke_owner_device(device_id: str, request: Request):
    from .link_access import revoke_session

    s = session(request)
    with db() as c:
        authorize_management(c, s)
        row = c.execute(
            "SELECT * FROM owner_cli_devices WHERE id=?", (device_id,)
        ).fetchone()
        if row is None:
            fail("商家 CLI 设备不存在", 404)
        if not row["revoked"]:
            c.execute("UPDATE owner_cli_devices SET revoked=1 WHERE id=?", (device_id,))
            audit(c, "owner", "cli.owner.device.revoke", device_id)
        c.execute("DELETE FROM owner_cli_challenges WHERE device_id=?", (device_id,))
        c.execute(
            "DELETE FROM owner_cli_action_challenges WHERE device_id=?", (device_id,)
        )
        rows = c.execute(
            "SELECT digest FROM sessions WHERE owner_device_id=? AND revoked=0",
            (device_id,),
        ).fetchall()
        for row in rows:
            revoke_session(c, row["digest"], "owner")
        return {"ok": True, "id": device_id, "revoked_sessions": len(rows)}

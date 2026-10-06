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
from pydantic import BaseModel, ConfigDict, Field, field_validator
from webauthn import generate_authentication_options, verify_authentication_response
from webauthn.helpers import base64url_to_bytes, options_to_json
from webauthn.helpers.structs import (
    PublicKeyCredentialDescriptor,
    UserVerificationRequirement,
)

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
_ALL_SHOPS = object()


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
    if "target_email" not in request_columns:
        c.execute("ALTER TABLE owner_cli_requests ADD COLUMN target_email TEXT")
    if "scope_bound" not in request_columns:
        c.execute(
            "ALTER TABLE owner_cli_requests ADD COLUMN scope_bound INTEGER NOT NULL DEFAULT 0 CHECK(scope_bound IN (0,1))"
        )
    c.execute(
        "CREATE TABLE IF NOT EXISTS owner_cli_password_challenges (digest TEXT PRIMARY KEY,request_id TEXT NOT NULL REFERENCES owner_cli_requests(id) ON DELETE CASCADE,shop_id TEXT NOT NULL REFERENCES shops(id),snapshot TEXT NOT NULL,expires REAL NOT NULL,created REAL NOT NULL)"
    )
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
    target_email: str | None = Field(default=None, max_length=254)

    @field_validator("target_email")
    @classmethod
    def normalize_target(cls, value):
        if value is not None:
            from .account_auth import normalize_email

            return normalize_email(value)
        return None


class RequestProof(KeyProof):
    request_id: str = Field(min_length=1, max_length=100)


class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=100)
    device_code: str = Field(min_length=1, max_length=64)
    shop_id: str | None = Field(default=None, min_length=1, max_length=100)


class PasswordOptions(ApprovalInput):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)
    code: str | None = Field(default=None, max_length=100)
    backup_code: str | None = Field(default=None, max_length=100)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        from .account_auth import normalize_email

        return normalize_email(value)


class PasswordApproval(ApprovalInput):
    approval_token: str = Field(min_length=43, max_length=43)


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
                "shop_id",
                "target_email",
                "scope_bound",
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
    if row is None:
        fail("商家 CLI 设备授权已失效", 401)
    _approval_valid(c, row["shop_id"], row["approved_credential_id"])
    return row


def _approval_valid(c, shop_id, credential_id):
    from .shops import shop_row

    if shop_id is not None:
        shop = shop_row(c, shop_id)
        if credential_id in (f"password:{shop_id}", f"password-totp:{shop_id}"):
            if not shop["password_hash"]:
                fail("商家 CLI 设备授权已失效", 401)
            return
    if not c.execute(
        "SELECT 1 FROM credentials WHERE id=? AND shop_id IS ?",
        (credential_id, shop_id),
    ).fetchone():
        fail("商家 CLI 设备批准身份已失效", 401)


def _authority(c, shop_id):
    from .shops import shop_row

    return {
        "role": "admin",
        "scope": SCOPE,
        "superadmin": shop_id is None,
        "shop_id": shop_id,
        "shop_name": "平台管理" if shop_id is None else shop_row(c, shop_id)["name"],
    }


def _actor(shop_id):
    return "owner" if shop_id is None else f"shop:{shop_id}"


def _browser_actor(request):
    if request.headers.get("authorization") is not None:
        fail("请使用新的浏览器认证批准设备", 401)
    return session(request) if request.cookies.get("extore_session") else None


def _pending_request(c, body):
    row = _request(c, body.request_id)
    normalized = body.device_code.strip().upper().replace("-", "")
    if not re.fullmatch(r"[A-Z2-9]{12}", normalized) or not hmac.compare_digest(
        row["device_code"].replace("-", ""), normalized
    ):
        fail("设备码不匹配", 401)
    if row["expires"] <= time.time() or row["state"] != "pending":
        fail("商家设备授权请求已过期或处理", 409)
    return row


def _bind_shop(c, row, shop_id):
    from .shops import shop_row

    shop = shop_row(c, shop_id) if shop_id is not None else None
    if row["scope_bound"] and row["shop_id"] != shop_id:
        fail("此设备请求已绑定其他管理范围", 403)
    if row["target_email"] and (shop is None or shop["email"] != row["target_email"]):
        fail("设备请求与店铺账号不匹配", 401)
    if not row["scope_bound"]:
        c.execute(
            "UPDATE owner_cli_requests SET shop_id=?,scope_bound=1 WHERE id=?",
            (shop_id, row["id"]),
        )
    return _request(c, row["id"])


def _approval_view(c, row):
    return {
        **_authority(c, row["shop_id"]),
        "request_id": row["id"],
        "device_code": row["device_code"],
        "client_name": row["client_name"],
        "fingerprint": row["fingerprint"],
        "grant_expires": row["grant_expires"],
        "expires": row["expires"],
    }


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
    proof = f"extore-cli-owner-request-v1\n{ORIGIN}\n{body.public_key}\n{body.client_name}\n{body.nonce}"
    if body.target_email is not None:
        proof = f"extore-cli-owner-request-v2\n{ORIGIN}\n{body.public_key}\n{body.client_name}\n{body.nonce}\n{body.target_email}"
    raw = _verify(
        body.public_key,
        body.signature,
        proof,
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
            if (
                row["client_name"] != body.client_name
                or row["target_email"] != body.target_email
            ):
                fail("商家设备授权请求不匹配", 401)
            return _request_view(row)
        now, rid = time.time(), str(uuid.uuid4())
        code = "".join(
            secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(12)
        )
        code = "-".join(code[i : i + 4] for i in range(0, 12, 4))
        c.execute(
            "INSERT INTO owner_cli_requests(id,public_key,client_name,fingerprint,nonce,device_code,challenge,scope,expires,grant_expires,created,target_email) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
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
                body.target_email,
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
        result = {
            "status": "approved" if state == "claimed" else state,
            "expires": row["expires"],
        }
        if state in ("approved", "claimed"):
            if not row["scope_bound"] or row["approved_snapshot"] != _snapshot(row):
                fail("商家设备授权范围已失效", 401)
            _approval_valid(c, row["shop_id"], row["approved_credential_id"])
            result.update(_authority(c, row["shop_id"]))
        return result


@router.post("/auth/cli-owner/options")
async def approval_options(request: Request, response: Response):
    actor = _browser_actor(request)
    rate_limit(request, "owner-cli-approval", 20, 60)
    body = await _body(request, ApprovalInput)
    with db() as c:
        if actor is not None:
            authorize_management(c, actor)
        own = actor.get("shop_id") if actor is not None else None
        selected = body.shop_id if "shop_id" in body.model_fields_set else own
        if own is not None and selected != own:
            fail("不能批准其他店铺的设备", 403)
        row = _bind_shop(c, _pending_request(c, body), selected)
        keys = c.execute(
            "SELECT id FROM credentials WHERE shop_id IS ?", (selected,)
        ).fetchall()
        methods = ["passkey"] if keys else []
        if selected is not None:
            from .shops import shop_row

            shop = shop_row(c, selected)
            if shop["password_hash"]:
                methods.append("password_totp" if shop["totp_secret"] else "password")
        if not methods:
            fail("请先注册商家 Passkey", 409)
        if not keys:
            response.delete_cookie(APPROVAL_COOKIE, path="/api/auth/cli-owner")
            return {
                **_approval_view(c, row),
                "options": None,
                "approval_methods": methods,
                "mfa_required": bool(shop["totp_secret"]),
            }
        options = generate_authentication_options(
            rp_id=RP_ID,
            user_verification=UserVerificationRequirement.REQUIRED,
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(key["id"]))
                for key in keys
            ],
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
            **_approval_view(c, row),
            "options": json.loads(options_to_json(options)),
            "approval_methods": methods,
            "mfa_required": bool(selected is not None and shop["totp_secret"]),
        }


@router.post("/auth/cli-owner/verify")
async def approval_verify(request: Request, response: Response):
    actor = _browser_actor(request)
    rate_limit(request, "owner-cli-approval-verify", 20, 60)
    body = await _body(request, ApprovalVerify)
    with db() as c:
        row = _request(c, body.request_id)
        if actor is not None:
            authorize_management(c, actor)
            if actor.get("shop_id") is not None and actor["shop_id"] != row["shop_id"]:
                fail("此批准请求属于其他店铺", 403)
        _authority(c, row["shop_id"])
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
            or not row["scope_bound"]
        ):
            fail("商家设备批准请求已失效", 401)
        key = c.execute(
            "SELECT * FROM credentials WHERE id=?", (body.credential.get("id", ""),)
        ).fetchone()
        if key is None or key["shop_id"] != row["shop_id"]:
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
        audit(c, _actor(row["shop_id"]), "cli.owner.approve", row["id"])
    response.delete_cookie(APPROVAL_COOKIE, path="/api/auth/cli-owner")
    return {"ok": True, "status": "approved"}


@router.post("/auth/cli-owner/password-options")
async def password_options(request: Request):
    from .account_auth import _throttle, verify_shop_password

    actor = _browser_actor(request)
    body = await _body(request, PasswordOptions)
    _throttle(request, "owner-cli-password", body.email, limit=5, window=300)
    with db() as c:
        row = _pending_request(c, body)
        shop = c.execute("SELECT * FROM shops WHERE email=?", (body.email,)).fetchone()
        if shop is None:
            from .account_auth import _password_valid

            _password_valid(None, body.password)
            fail("登录信息或二次验证码错误", 401)
        if actor is not None:
            authorize_management(c, actor)
            if actor.get("shop_id") is not None and actor["shop_id"] != shop["id"]:
                fail("此批准请求属于其他店铺", 403)
        if body.shop_id is not None and body.shop_id != shop["id"]:
            fail("设备请求与店铺账号不匹配", 401)
        verify_shop_password(c, shop, body.password, body.code, body.backup_code)
        row = _bind_shop(c, row, shop["id"])
        now, value = time.time(), token()
        expires = min(now + CHALLENGE_TTL, row["expires"])
        c.execute(
            "DELETE FROM owner_cli_password_challenges WHERE expires<=? OR request_id=?",
            (now, row["id"]),
        )
        c.execute(
            "INSERT INTO owner_cli_password_challenges VALUES (?,?,?,?,?,?)",
            (digest(value), row["id"], shop["id"], _snapshot(row), expires, now),
        )
        return {
            **_approval_view(c, row),
            "options": None,
            "approval_methods": [
                "password_totp" if shop["totp_secret"] else "password"
            ],
            "mfa_required": bool(shop["totp_secret"]),
            "approval_token": value,
            "approval_expires": expires,
        }


@router.post("/auth/cli-owner/password-approve")
async def password_approve(request: Request):
    actor = _browser_actor(request)
    body = await _body(request, PasswordApproval)
    rate_limit(request, "owner-cli-password-approve", 20, 60)
    with db() as c:
        row = _pending_request(c, body)
        proof = c.execute(
            "SELECT * FROM owner_cli_password_challenges WHERE digest=? AND expires>?",
            (digest(body.approval_token), time.time()),
        ).fetchone()
        if (
            proof is None
            or proof["request_id"] != row["id"]
            or proof["shop_id"] != row["shop_id"]
            or proof["snapshot"] != _snapshot(row)
            or row["shop_id"] is None
            or not row["scope_bound"]
        ):
            fail("新的密码批准验证已失效", 401)
        if actor is not None:
            authorize_management(c, actor)
            if actor.get("shop_id") is not None and actor["shop_id"] != row["shop_id"]:
                fail("此批准请求属于其他店铺", 403)
        marker = f"password:{row['shop_id']}"
        _approval_valid(c, row["shop_id"], marker)
        c.execute(
            "UPDATE owner_cli_requests SET state='approved',approved_credential_id=?,approved_snapshot=? WHERE id=?",
            (marker, _snapshot(row), row["id"]),
        )
        c.execute(
            "DELETE FROM owner_cli_password_challenges WHERE request_id=?", (row["id"],)
        )
        c.execute(
            "DELETE FROM owner_cli_approval_challenges WHERE request_id=?", (row["id"],)
        )
        audit(c, _actor(row["shop_id"]), "cli.owner.approve", row["id"])
        return {**_authority(c, row["shop_id"]), "ok": True, "status": "approved"}


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
            or not row["scope_bound"]
        ):
            fail("商家设备授权尚未批准或已失效", 401)
        _verify(
            body.public_key,
            body.signature,
            f"extore-cli-owner-claim-v1\n{ORIGIN}\n{row['id']}\n{row['challenge']}\n{body.public_key}",
        )
        _approval_valid(c, row["shop_id"], row["approved_credential_id"])
        existing = c.execute(
            "SELECT * FROM owner_cli_devices WHERE public_key=?", (body.public_key,)
        ).fetchone()
        already = row["state"] == "claimed"
        if existing is not None and existing["shop_id"] != row["shop_id"]:
            fail("此设备密钥已绑定其他管理范围，请使用新密钥", 403)
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
                "INSERT INTO owner_cli_devices(id,public_key,client_name,fingerprint,expires,revoked,approved_credential_id,created,last_seen,shop_id) VALUES (?,?,?,?,?,0,?,?,?,?)",
                (
                    did,
                    row["public_key"],
                    row["client_name"],
                    row["fingerprint"],
                    row["grant_expires"],
                    row["approved_credential_id"],
                    now,
                    now,
                    row["shop_id"],
                ),
            )
            existing = c.execute(
                "SELECT * FROM owner_cli_devices WHERE id=?", (did,)
            ).fetchone()
            audit(c, _actor(row["shop_id"]), "cli.owner.device.create", did)
        c.execute(
            "UPDATE owner_cli_requests SET state='claimed',device_id=? WHERE id=?",
            (existing["id"], row["id"]),
        )
        device = owner_device(c, existing["id"])
        return {
            **_authority(c, device["shop_id"]),
            "device_id": device["id"],
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
            "INSERT INTO sessions(digest,role,expires,created,id,last_seen,ip,ua,revoked,channel,owner_device_id,client_name,shop_id,auth_at,auth_method) VALUES (?,'admin',?,?,?,?,?,?,0,'cli',?,?,?,?,?)",
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
                device["shop_id"],
                now,
                "device_key",
            ),
        )
        c.execute(
            "UPDATE owner_cli_devices SET last_seen=? WHERE id=?", (now, device["id"])
        )
        audit(c, _actor(device["shop_id"]), "session.create", sid)
        return {
            **_authority(c, device["shop_id"]),
            "access_token": access,
            "token_type": "Bearer",
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
    with db() as c:
        authorize_management(c, s)
        authority = _authority(c, s.get("shop_id"))
    return {
        **authority,
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
        revoke_session(c, s["digest"], _actor(s.get("shop_id")), "session.logout")
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
        audit(c, _actor(s.get("shop_id")), "cli.owner.action", s["id"])
    request.state.owner_cli_action_verified = True
    return True


def invalidate_pending_owner_approvals(c, shop_id):
    """Invalidate reviewed factors in the same transaction as a policy change."""
    request_ids = [
        row["id"]
        for row in c.execute(
            "SELECT id FROM owner_cli_requests WHERE scope_bound=1 AND shop_id IS ? AND state IN ('pending','approved')",
            (shop_id,),
        )
    ]
    c.execute(
        "DELETE FROM owner_cli_password_challenges WHERE shop_id IS ?", (shop_id,)
    )
    c.execute(
        "DELETE FROM owner_cli_approval_challenges WHERE request_id IN (SELECT id FROM owner_cli_requests WHERE scope_bound=1 AND shop_id IS ?)",
        (shop_id,),
    )
    c.execute(
        "UPDATE owner_cli_requests SET state='denied' WHERE scope_bound=1 AND shop_id IS ? AND state IN ('pending','approved')",
        (shop_id,),
    )
    for rid in request_ids:
        audit(c, _actor(shop_id), "cli.owner.approval.invalidate", rid)


def revoke_owner_devices(c, actor="owner", *, shop_id=_ALL_SHOPS):
    from .link_access import revoke_session

    where, args = (
        ("", ()) if shop_id is _ALL_SHOPS else (" WHERE shop_id IS ?", (shop_id,))
    )
    devices = c.execute("SELECT id FROM owner_cli_devices" + where, args).fetchall()
    for row in devices:
        for item in c.execute(
            "SELECT digest FROM sessions WHERE owner_device_id=? AND revoked=0",
            (row["id"],),
        ).fetchall():
            revoke_session(c, item["digest"], actor)
        c.execute("UPDATE owner_cli_devices SET revoked=1 WHERE id=?", (row["id"],))
        audit(c, actor, "cli.owner.device.revoke", row["id"])
        c.execute(
            "DELETE FROM owner_cli_action_challenges WHERE device_id=?", (row["id"],)
        )
        c.execute("DELETE FROM owner_cli_challenges WHERE device_id=?", (row["id"],))
    request_ids = [
        row["id"]
        for row in c.execute("SELECT id FROM owner_cli_requests" + where, args)
    ]
    for rid in request_ids:
        c.execute(
            "DELETE FROM owner_cli_approval_challenges WHERE request_id=?", (rid,)
        )
        c.execute(
            "DELETE FROM owner_cli_password_challenges WHERE request_id=?", (rid,)
        )
        c.execute(
            "UPDATE owner_cli_requests SET state='denied' WHERE id=? AND state IN ('pending','approved')",
            (rid,),
        )


@router.get("/admin/cli-owner-devices")
def list_owner_devices(request: Request):
    s = session(request)
    with db() as c:
        authorize_management(c, s)
        now = time.time()
        return [
            {
                "shop_id": r["shop_id"],
                "shop_name": r["shop_name"] or "平台管理",
                "superadmin": r["shop_id"] is None,
                "id": r["id"],
                "role": "admin",
                "scope": SCOPE,
                "client_name": r["client_name"],
                "fingerprint": r["fingerprint"],
                "created": r["created"],
                "last_seen": r["last_seen"],
                "expires": r["expires"],
                "revoked": bool(r["revoked"]),
                "active": not r["revoked"]
                and r["expires"] > now
                and (r["shop_id"] is None or r["shop_enabled"] == 1),
            }
            for r in c.execute(
                "SELECT d.*,shops.name AS shop_name,shops.enabled AS shop_enabled FROM owner_cli_devices d LEFT JOIN shops ON shops.id=d.shop_id WHERE (? IS NULL OR d.shop_id=?) ORDER BY d.created DESC",
                (s.get("shop_id"), s.get("shop_id")),
            )
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
        if s.get("shop_id") is not None and row["shop_id"] != s["shop_id"]:
            fail("无权撤销其他店铺的 CLI 设备", 403)
        if not row["revoked"]:
            c.execute("UPDATE owner_cli_devices SET revoked=1 WHERE id=?", (device_id,))
            audit(c, _actor(s.get("shop_id")), "cli.owner.device.revoke", device_id)
        c.execute("DELETE FROM owner_cli_challenges WHERE device_id=?", (device_id,))
        c.execute(
            "DELETE FROM owner_cli_action_challenges WHERE device_id=?", (device_id,)
        )
        rows = c.execute(
            "SELECT digest FROM sessions WHERE owner_device_id=? AND revoked=0",
            (device_id,),
        ).fetchall()
        for row in rows:
            revoke_session(c, row["digest"], _actor(s.get("shop_id")))
        return {"ok": True, "id": device_id, "revoked_sessions": len(rows)}

import json
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url, options_to_json
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .config import COOKIE_SECURE, DATA, ORIGIN, RP_ID
from .db import audit, db, set_setting, setting
from .link_access import revoke_all_sessions, revoke_session
from .security import create_session, digest, fail, rate_limit, session, token

router = APIRouter(prefix="/api/auth")
ph = PasswordHasher()


class PasswordInput(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class CredentialInput(BaseModel):
    credential: dict
    name: str = Field(default="我的 Passkey", min_length=1, max_length=100)
    challenge_id: str | None = Field(default=None, min_length=1, max_length=100)


@router.get("/status")
def status(request: Request):
    with db() as c:
        configured = bool(setting(c, "bootstrap_password"))
        passkeys = c.execute("SELECT count(*) FROM credentials").fetchone()[0]
    s = None
    try:
        s = session(request, ("admin", "bootstrap", "staff"))
        role = s["role"]
    except Exception as e:
        from fastapi import HTTPException

        if not isinstance(e, HTTPException):
            raise
        role = None
    result = {
        "configured": configured or bool(passkeys),
        "password_enabled": configured and not passkeys,
        "role": role,
    }
    if role == "staff":
        result.update(
            product_id=s["product_id"],
            permissions=s["permissions"],
            link_id=s["staff_id"],
            link_name=s["name"],
            link_expires=s["link_expires"],
            parent_id=s["parent_id"],
            max_uses=s["max_uses"],
            uses=s["uses"],
            remaining_uses=s["remaining_uses"],
            max_cli_uses=s["max_cli_uses"],
            cli_uses=s["cli_uses"],
            remaining_cli_uses=s["remaining_cli_uses"],
        )
    elif role == "admin" and s["channel"] == "cli":
        result.update(
            channel="cli",
            scope="shop.owner",
            device_id=s["owner_device_id"],
            session_id=s["id"],
            expires=s["expires"],
            grant_expires=s["grant_expires"],
            client_name=s["client_name"],
            fingerprint=s["fingerprint"],
        )
    return result


@router.post("/password")
def password(body: PasswordInput, request: Request, response: Response):
    rate_limit(request, "login", 5, 300)
    with db() as c:
        if c.execute("SELECT count(*) FROM credentials").fetchone()[0]:
            fail("密码登录已禁用，请使用 Passkey", 403)
        hashed = setting(c, "bootstrap_password")
        try:
            if not hashed or not ph.verify(hashed, body.password):
                fail("密码错误", 401)
        except VerificationError:
            fail("密码错误", 401)
        create_session(c, response, "bootstrap", request=request)
    return {"role": "bootstrap"}


@router.post("/logout")
def logout(request: Request, response: Response):
    with db() as c:
        previous = digest(request.cookies.get("extore_session", ""))
        if c.execute(
            "SELECT 1 FROM sessions WHERE digest=? AND channel='browser'", (previous,)
        ).fetchone():
            revoke_session(c, previous, action="session.logout")
    response.delete_cookie("extore_session", path="/")
    return {"ok": True}


def challenge(c, request, response, kind, s=None):
    key = token()
    if s:
        if s["channel"] == "browser" and time.time() - s["created"] > 600:
            fail("添加 Passkey 前请重新登录", 401)
        rows = c.execute("SELECT id FROM credentials").fetchall()
        import base64

        opts = generate_registration_options(
            rp_id=RP_ID,
            rp_name="Extore · 兑所",
            user_id=b"extore-owner",
            user_name="owner",
            exclude_credentials=[
                PublicKeyCredentialDescriptor(
                    id=base64.urlsafe_b64decode(r["id"] + "=" * (-len(r["id"]) % 4))
                )
                for r in rows
            ],
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
    else:
        opts = generate_authentication_options(
            rp_id=RP_ID, user_verification=UserVerificationRequirement.REQUIRED
        )
    c.execute("DELETE FROM challenges WHERE expires<?", (time.time(),))
    c.execute(
        "INSERT INTO challenges VALUES (?,?,?,?,?)",
        (
            digest(key),
            opts.challenge,
            kind,
            s["digest"] if s else None,
            time.time() + 300,
        ),
    )
    if s and s["channel"] == "cli":
        return {**json.loads(options_to_json(opts)), "challenge_id": key}
    response.set_cookie(
        "extore_challenge",
        key,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        max_age=300,
        path="/api/auth",
    )
    return json.loads(options_to_json(opts))


def consume_challenge(c, request, kind, s=None, challenge_id=None):
    if s and s["channel"] == "cli":
        if not challenge_id:
            fail("请提供 CLI Passkey 注册挑战", 400)
        value = challenge_id
    else:
        value = request.cookies.get("extore_challenge", "")
    row = c.execute(
        "SELECT * FROM challenges WHERE digest=?",
        (digest(value),),
    ).fetchone()
    if (
        not row
        or row["expires"] < time.time()
        or row["kind"] != kind
        or (s and row["session_digest"] != s["digest"])
    ):
        fail("认证请求已过期，请重新开始", 400)
    c.execute("DELETE FROM challenges WHERE digest=?", (row["digest"],))
    return row["challenge"]


@router.post("/register/options")
def register_options(request: Request, response: Response):
    s = session(request, ("admin", "bootstrap"))
    with db() as c:
        return challenge(c, request, response, "register", s)


@router.post("/register/verify")
def register_verify(body: CredentialInput, request: Request, response: Response):
    s = session(request, ("admin", "bootstrap"))
    with db() as c:
        ch = consume_challenge(c, request, "register", s, body.challenge_id)
        try:
            v = verify_registration_response(
                credential=body.credential,
                expected_challenge=ch,
                expected_rp_id=RP_ID,
                expected_origin=ORIGIN,
                require_user_verification=True,
            )
        except Exception:
            fail("Passkey 校验失败，请重试")
        cid = bytes_to_base64url(v.credential_id)
        if c.execute("SELECT 1 FROM credentials WHERE id=?", (cid,)).fetchone():
            fail("此 Passkey 已存在", 409)
        c.execute(
            "INSERT INTO credentials VALUES (?,?,?,?,?)",
            (cid, v.credential_public_key, v.sign_count, body.name, time.time()),
        )
        set_setting(c, "bootstrap_password", "")
        if s["role"] == "bootstrap":
            revoke_all_sessions(c, "owner", "session.bootstrap_complete")
            c.execute("DELETE FROM challenges")
            create_session(c, response, "admin", request=request)
        audit(c, "owner", "passkey.add", cid)
    (DATA / "bootstrap-password.txt").unlink(missing_ok=True)
    return {"ok": True}


@router.post("/login/options")
def login_options(request: Request, response: Response):
    rate_limit(request, "passkey", 20, 60)
    with db() as c:
        return challenge(c, request, response, "login")


@router.post("/login/verify")
def login_verify(body: CredentialInput, request: Request, response: Response):
    rate_limit(request, "passkey-verify", 20, 60)
    with db() as c:
        ch = consume_challenge(c, request, "login")
        row = c.execute(
            "SELECT * FROM credentials WHERE id=?", (body.credential.get("id", ""),)
        ).fetchone()
        if not row:
            fail("Passkey 未注册", 401)
        try:
            v = verify_authentication_response(
                credential=body.credential,
                expected_challenge=ch,
                expected_rp_id=RP_ID,
                expected_origin=ORIGIN,
                credential_public_key=row["public_key"],
                credential_current_sign_count=row["sign_count"],
                require_user_verification=True,
            )
        except Exception:
            fail("Passkey 校验失败", 401)
        c.execute(
            "UPDATE credentials SET sign_count=? WHERE id=?",
            (v.new_sign_count, row["id"]),
        )
        create_session(c, response, "admin", request=request)
    return {"role": "admin"}


@router.get("/passkeys")
def keys(request: Request):
    session(request)
    with db() as c:
        return [dict(r) for r in c.execute("SELECT id,name,created FROM credentials")]


@router.delete("/passkeys/{cid}")
def delete_key(cid: str, request: Request):
    s = session(request)
    if s["channel"] == "browser" and time.time() - s["created"] > 600:
        fail("删除 Passkey 前请重新登录", 401)
    with db() as c:
        if c.execute("SELECT count(*) FROM credentials").fetchone()[0] <= 1:
            fail("不能删除最后一个 Passkey；丢失时使用服务器命令重置", 409)
        c.execute("DELETE FROM credentials WHERE id=?", (cid,))
        audit(c, "owner", "passkey.remove", cid)
    return {"ok": True}

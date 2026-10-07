"""Email accounts, shop invitations, and password second-factor protection."""

import hmac
import json
import time
import uuid

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import ORIGIN, UPLOAD_SHOP_BYTES
from .db import audit, db, set_setting, setting
from .security import create_session, digest, fail, rate_limit, token
from .shops import require_shop_owner, require_superadmin, shop_row

router = APIRouter(prefix="/api")
ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
_DUMMY_HASH = ph.hash(token())


def normalize_email(value):
    from .mail import email_address

    return email_address(value).casefold()


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("name", check_fields=False)
    @classmethod
    def valid_name(cls, value):
        value = value.strip()
        if not value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("名称不能为空或包含控制字符")
        return value


class EmailInput(Input):
    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def email_normalized(cls, value):
        return normalize_email(value)


class LoginInput(EmailInput):
    password: str = Field(min_length=1, max_length=200)
    code: str | None = Field(default=None, max_length=100)
    backup_code: str | None = Field(default=None, max_length=100)


class ConfirmationInput(Input):
    token: str = Field(min_length=20, max_length=100)
    password: str = Field(min_length=12, max_length=200)
    code: str | None = Field(default=None, max_length=100)
    backup_code: str | None = Field(default=None, max_length=100)


class RegisterInput(EmailInput):
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=12, max_length=200)


class RegisterConfirmInput(Input):
    token: str = Field(min_length=20, max_length=100)


class ShopCreate(EmailInput):
    name: str = Field(min_length=1, max_length=100)


class ShopUpdate(Input):
    enabled: bool | None = None
    storage_limit_bytes: int | None = Field(
        default=None, strict=True, gt=0, le=2**63 - 1
    )


class AccountUpdate(Input):
    name: str = Field(min_length=1, max_length=100)


class ReauthInput(Input):
    password: str = Field(min_length=1, max_length=200)
    code: str | None = Field(default=None, max_length=100)
    backup_code: str | None = Field(default=None, max_length=100)


class PasswordChange(ReauthInput):
    new_password: str = Field(min_length=12, max_length=200)


class TotpConfirm(Input):
    code: str = Field(min_length=6, max_length=6)


class PlatformSettings(Input):
    registration_enabled: bool | None = None
    smtp: dict | None = None


def registration_enabled(c):
    return setting(c, "registration_enabled", "false") == "true"


def require_recent(s):
    if s.get("channel") == "cli":
        # The owner CLI middleware verifies a new, one-use device signature over
        # every security write. MFA-changing endpoints additionally ask password
        # and the existing second factor below.
        return
    if time.time() - float(s.get("auth_at") or 0) > 600:
        fail("请重新认证后再修改安全设置", 401)


def _throttle(request, purpose, email=None, limit=5, window=300):
    rate_limit(request, purpose, limit, window)
    if email:
        # This bucket is keyed by account even when different peers target it.
        now = time.time()
        key = f"{purpose}-account:{digest(email)}"
        with db() as c:
            c.execute("DELETE FROM rate_limits WHERE expires<?", (now,))
            row = c.execute("SELECT * FROM rate_limits WHERE key=?", (key,)).fetchone()
            if row and row["count"] >= limit:
                fail("请求过于频繁，请稍后重试", 429)
            c.execute(
                "INSERT INTO rate_limits VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET count=count+1",
                (key, now + window),
            )


def _password_valid(hashed, password):
    try:
        return ph.verify(hashed or _DUMMY_HASH, password) and bool(hashed)
    except VerificationError:
        return False


def verify_shop_password(c, shop, password, code=None, backup_code=None):
    """Fresh factor verification for CLI approval under its caller's transaction."""
    if (
        not shop["enabled"]
        or not shop["verified"]
        or not _password_valid(shop["password_hash"], password)
    ):
        fail("登录信息或 2FA 验证码错误", 401)
    _verify_second_factor(c, shop, code, backup_code)
    return shop


def _actor(shop_id):
    return f"shop:{shop_id}" if shop_id is not None else "owner"


def _secret(value, sid, pending=False):
    from .secret_store import open_secret

    return open_secret(
        value,
        tenant_id=sid,
        resource_type="totp-pending" if pending else "totp",
        resource_id=sid,
    )


def _seal(value, sid, pending=False):
    from .secret_store import store_secret

    return store_secret(
        value,
        tenant_id=sid,
        resource_type="totp-pending" if pending else "totp",
        resource_id=sid,
    )


def _verify_second_factor(c, shop, code, backup_code):
    from .totp import backup_digest, match_counter

    if not shop["totp_secret"]:
        return
    if bool(code) == bool(backup_code):
        fail("请输入 2FA 验证码或恢复码", 401)
    if code:
        counter = match_counter(
            _secret(shop["totp_secret"], shop["id"]),
            code,
            time.time(),
            shop["last_totp_counter"],
        )
        if counter is None:
            fail("登录信息或 2FA 验证码错误", 401)
        c.execute(
            "UPDATE shops SET last_totp_counter=? WHERE id=?", (counter, shop["id"])
        )
    else:
        try:
            wanted = backup_digest(backup_code)
        except ValueError:
            fail("登录信息或 2FA 验证码错误", 401)
        remaining = json.loads(shop["totp_backup_digests"])
        found = next((x for x in remaining if hmac.compare_digest(x, wanted)), None)
        if found is None:
            fail("登录信息或 2FA 验证码错误", 401)
        remaining.remove(found)
        c.execute(
            "UPDATE shops SET totp_backup_digests=? WHERE id=?",
            (json.dumps(remaining), shop["id"]),
        )
        audit(c, _actor(shop["id"]), "account.backup_code.consume", shop["id"])


def revoke_shop_auth(c, sid, action="account.auth.reset"):
    from .link_access import revoke_session
    from .owner_cli_auth import revoke_owner_devices
    from .pipeline_scopes import revoke_authorizations

    # Persistent pipeline scopes survive ordinary logout, but never account
    # recovery. Root recovery must leave independently approved shop scopes.
    if sid is None:
        revoke_authorizations(c, actor=_actor(sid), issuer_role="root")
    else:
        revoke_authorizations(c, shop_id=sid, actor=_actor(sid))

    for row in c.execute(
        "SELECT digest FROM sessions WHERE shop_id IS ? AND role IN ('admin','bootstrap') AND revoked=0",
        (sid,),
    ).fetchall():
        revoke_session(c, row["digest"], _actor(sid), "session.auth_reset")
    revoke_owner_devices(c, _actor(sid), shop_id=sid)
    # Product links remain configured, but their currently authenticated devices
    # and sessions are revoked so a password reset cannot leave stale workers.
    for row in c.execute(
        "SELECT s.id FROM staff s JOIN products p ON p.id=s.product_id WHERE p.shop_id=?",
        (sid,),
    ).fetchall():
        c.execute("UPDATE cli_devices SET revoked=1 WHERE staff_id=?", (row["id"],))
        for item in c.execute(
            "SELECT digest FROM sessions WHERE staff_id=? AND revoked=0", (row["id"],)
        ).fetchall():
            revoke_session(c, item["digest"], _actor(sid), "session.auth_reset")
    c.execute(
        "DELETE FROM challenges WHERE session_digest IN (SELECT digest FROM sessions WHERE shop_id IS ? AND role IN ('admin','bootstrap'))",
        (sid,),
    )
    if sid is not None:
        c.execute("DELETE FROM account_tokens WHERE shop_id=? AND used IS NULL", (sid,))


def _revoke_account(c, sid, action):
    revoke_shop_auth(c, sid, action)


def _shop_view(c, row):
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "enabled": bool(row["enabled"]),
        "verified": bool(row["verified"]),
        "created": row["created"],
        "legacy": bool(row["legacy"]),
        "totp_enabled": bool(row["totp_secret"]),
        "storage_limit_bytes": row["storage_limit_bytes"],
        "passkeys": c.execute(
            "SELECT count(*) FROM credentials WHERE shop_id=?", (row["id"],)
        ).fetchone()[0],
    }


def _mail_token(c, kind, email, sid=None, *, password_hash=None, name=None):
    from .mail import enqueue, get_settings
    from .mail_templates import account_message

    if not get_settings(c)["enabled"]:
        fail("邮箱服务尚未启用，请联系管理者", 503)
    now = time.time()
    raw = token()
    expiry = now + (86400 if kind == "invite" else 1800)
    c.execute(
        "DELETE FROM account_tokens WHERE expires<? OR (kind=? AND email=? AND used IS NULL)",
        (now, kind, email),
    )
    c.execute(
        "INSERT INTO account_tokens(digest,kind,shop_id,email,password_hash,name,created,expires) VALUES (?,?,?,?,?,?,?,?)",
        (digest(raw), kind, sid, email, password_hash, name, now, expiry),
    )
    titles = {
        "invite": "Extore 店铺邀请",
        "register": "Extore 邮箱确认",
        "reset": "Extore 密码重置",
    }
    route = "register" if kind == "register" else kind
    url = f"{ORIGIN}/account/{route}#{raw}"
    shop = shop_row(c, sid) if sid is not None else None
    enqueue(
        c,
        email,
        titles[kind],
        f"{titles[kind]}\n\n请打开以下链接完成操作：\n{url}\n\n链接仅可使用一次，{24 if kind == 'invite' else 0.5} 小时后失效。如果不是你请求的，请忽略。",
        html=account_message(kind, url, shop_name=shop["name"] if shop else name),
        expires=expiry,
        shop_id=sid,
    )
    return expiry


def _consume_token(c, raw, kind):
    row = c.execute(
        "SELECT * FROM account_tokens WHERE digest=? AND kind=? AND used IS NULL AND expires>?",
        (digest(raw), kind, time.time()),
    ).fetchone()
    if row is None:
        fail("链接已失效或已使用", 400)
    c.execute(
        "UPDATE account_tokens SET used=? WHERE digest=? AND used IS NULL",
        (time.time(), row["digest"]),
    )
    return row


@router.get("/platform/settings")
def platform_settings(request: Request):
    from .mail import get_settings

    require_superadmin(request)
    with db() as c:
        return {
            "registration_enabled": registration_enabled(c),
            "smtp": get_settings(c),
        }


@router.put("/platform/settings")
def configure_platform(body: PlatformSettings, request: Request):
    from .mail import configure, get_settings

    s = require_superadmin(request)
    require_recent(s)
    with db() as c:
        if body.smtp is not None:
            try:
                configure(c, body.smtp, actor="superadmin")
            except ValueError:
                fail("邮箱服务器配置无效", 400)
        if body.registration_enabled is not None:
            if body.registration_enabled and not get_settings(c)["enabled"]:
                fail("启用注册前请配置并启用邮箱服务", 409)
            set_setting(
                c,
                "registration_enabled",
                "true" if body.registration_enabled else "false",
            )
            audit(
                c,
                "superadmin",
                "platform.registration.configure",
                str(body.registration_enabled),
            )
        if registration_enabled(c) and not get_settings(c)["enabled"]:
            set_setting(c, "registration_enabled", "false")
        return {
            "registration_enabled": registration_enabled(c),
            "smtp": get_settings(c),
        }


@router.get("/platform/shops")
def list_shops(request: Request):
    require_superadmin(request)
    with db() as c:
        return [
            _shop_view(c, r)
            for r in c.execute("SELECT * FROM shops ORDER BY created DESC")
        ]


@router.post("/platform/shops")
def create_shop(body: ShopCreate, request: Request):
    s = require_superadmin(request)
    require_recent(s)
    with db() as c:
        if c.execute("SELECT 1 FROM shops WHERE email=?", (body.email,)).fetchone():
            fail("此邮箱已有店铺账号", 409)
        sid = str(uuid.uuid4())
        c.execute(
            "INSERT INTO shops(id,name,email,created,storage_limit_bytes) VALUES (?,?,?,?,?)",
            (sid, body.name.strip(), body.email, time.time(), UPLOAD_SHOP_BYTES),
        )
        from .proxy_routes import ensure_shop_issuer

        ensure_shop_issuer(c, sid)
        expires = _mail_token(c, "invite", body.email, sid)
        audit(c, "superadmin", "shop.create", sid)
        return {
            "ok": True,
            "shop": _shop_view(c, shop_row(c, sid)),
            "invite_expires": expires,
        }


@router.post("/platform/shops/{shop_id}/invite")
def invite_shop_owner(shop_id: str, request: Request):
    s = require_superadmin(request)
    require_recent(s)
    with db() as c:
        row = shop_row(c, shop_id)
        if row["verified"] or not row["email"]:
            fail("此店铺无需领取邀请", 409)
        expires = _mail_token(c, "invite", row["email"], shop_id)
        audit(c, "superadmin", "shop.invite", shop_id)
    return {"ok": True, "invite_expires": expires}


@router.patch("/platform/shops/{shop_id}")
def update_shop(shop_id: str, body: ShopUpdate, request: Request):
    s = require_superadmin(request)
    require_recent(s)
    with db() as c:
        shop_row(c, shop_id, require_enabled=False)
        if body.enabled is not None:
            c.execute(
                "UPDATE shops SET enabled=? WHERE id=?", (int(body.enabled), shop_id)
            )
            if not body.enabled:
                _revoke_account(c, shop_id, "shop.disable")
            audit(
                c,
                "superadmin",
                "shop.enable" if body.enabled else "shop.disable",
                shop_id,
            )
        if body.storage_limit_bytes is not None:
            from .storage import set_shop_storage_limit

            set_shop_storage_limit(c, shop_id, body.storage_limit_bytes)
            audit(c, "superadmin", "shop.storage_limit.configure", shop_id)
        return _shop_view(c, shop_row(c, shop_id, require_enabled=False))


@router.get("/shop/account")
def account(request: Request):
    s = require_shop_owner(request)
    with db() as c:
        return _shop_view(c, shop_row(c, s["shop_id"]))


@router.patch("/shop/account")
def update_account(body: AccountUpdate, request: Request):
    s = require_shop_owner(request)
    with db() as c:
        c.execute(
            "UPDATE shops SET name=? WHERE id=?", (body.name.strip(), s["shop_id"])
        )
        audit(c, _actor(s["shop_id"]), "shop.rename", s["shop_id"])
        return _shop_view(c, shop_row(c, s["shop_id"]))


@router.post("/auth/email/login")
def email_login(body: LoginInput, request: Request, response: Response):
    _throttle(request, "email-login", body.email)
    with db() as c:
        row = c.execute("SELECT * FROM shops WHERE email=?", (body.email,)).fetchone()
        valid = _password_valid(row["password_hash"] if row else None, body.password)
        if not valid or not row or not row["enabled"] or not row["verified"]:
            fail("登录信息或 2FA 验证码错误", 401)
        _verify_second_factor(c, row, body.code, body.backup_code)
        if ph.check_needs_rehash(row["password_hash"]):
            c.execute(
                "UPDATE shops SET password_hash=? WHERE id=?",
                (ph.hash(body.password), row["id"]),
            )
        create_session(
            c,
            response,
            "admin",
            request=request,
            shop_id=row["id"],
            auth_method="email_password_totp"
            if row["totp_secret"]
            else "email_password",
        )
        audit(c, _actor(row["id"]), "account.login", row["id"])
    return {"role": "admin", "shop_id": row["id"], "superadmin": False}


@router.post("/auth/invite/claim")
def claim_invite(body: ConfirmationInput, request: Request, response: Response):
    _throttle(request, "invite-claim", limit=10)
    with db() as c:
        row = _consume_token(c, body.token, "invite")
        shop = shop_row(c, row["shop_id"])
        if shop["verified"] or shop["email"] != row["email"]:
            fail("链接已失效或已使用", 400)
        c.execute(
            "UPDATE shops SET password_hash=?,verified=1 WHERE id=?",
            (ph.hash(body.password), shop["id"]),
        )
        create_session(
            c,
            response,
            "admin",
            request=request,
            shop_id=shop["id"],
            auth_method="email_password",
        )
        audit(c, _actor(shop["id"]), "account.invite.claim", shop["id"])
    return {"ok": True, "role": "admin", "shop_id": shop["id"]}


@router.post("/auth/register/email/request")
def register_email(body: RegisterInput, request: Request):
    _throttle(request, "register-email", body.email, limit=3, window=600)
    with db() as c:
        if not registration_enabled(c):
            fail("当前不允许自行注册，请联系管理者获取邀请", 403)
        if not c.execute("SELECT 1 FROM shops WHERE email=?", (body.email,)).fetchone():
            _mail_token(
                c,
                "register",
                body.email,
                password_hash=ph.hash(body.password),
                name=body.name.strip(),
            )
    return {"ok": True, "message": "如果此邮箱可以注册，确认邮件将发送至该邮箱"}


@router.post("/auth/register/email/confirm")
def confirm_registration(
    body: RegisterConfirmInput, request: Request, response: Response
):
    _throttle(request, "register-confirm", limit=10)
    with db() as c:
        if not registration_enabled(c):
            fail("当前不允许自行注册", 403)
        row = _consume_token(c, body.token, "register")
        if c.execute("SELECT 1 FROM shops WHERE email=?", (row["email"],)).fetchone():
            fail("链接已失效或已使用", 400)
        sid = str(uuid.uuid4())
        c.execute(
            "INSERT INTO shops(id,name,email,password_hash,created,verified,storage_limit_bytes) VALUES (?,?,?,?,?,1,?)",
            (
                sid,
                row["name"],
                row["email"],
                row["password_hash"],
                time.time(),
                UPLOAD_SHOP_BYTES,
            ),
        )
        from .proxy_routes import ensure_shop_issuer

        ensure_shop_issuer(c, sid)
        create_session(
            c,
            response,
            "admin",
            request=request,
            shop_id=sid,
            auth_method="email_verified_registration",
        )
        audit(c, _actor(sid), "account.register", sid)
    return {"ok": True, "role": "admin", "shop_id": sid}


@router.post("/auth/password/reset/request")
def request_password_reset(body: EmailInput, request: Request):
    from .mail import get_settings

    _throttle(request, "password-reset", body.email, limit=3, window=600)
    with db() as c:
        row = c.execute(
            "SELECT * FROM shops WHERE email=? AND verified=1 AND enabled=1",
            (body.email,),
        ).fetchone()
        if row and get_settings(c)["enabled"]:
            _mail_token(c, "reset", body.email, row["id"])
    return {"ok": True, "message": "如果此邮箱有可用账号，重置邮件将发送至该邮箱"}


@router.post("/auth/password/reset/confirm")
def confirm_password_reset(
    body: ConfirmationInput, request: Request, response: Response
):
    _throttle(request, "password-reset-confirm", limit=5)
    with db() as c:
        row = _consume_token(c, body.token, "reset")
        shop = shop_row(c, row["shop_id"])
        if shop["email"] != row["email"] or not shop["verified"]:
            fail("链接已失效或已使用", 400)
        _verify_second_factor(c, shop, body.code, body.backup_code)
        c.execute(
            "UPDATE shops SET password_hash=? WHERE id=?",
            (ph.hash(body.password), shop["id"]),
        )
        _revoke_account(c, shop["id"], "account.password.reset")
        audit(c, _actor(shop["id"]), "account.password.reset", shop["id"])
    response.delete_cookie("extore_session", path="/")
    return {"ok": True, "message": "密码已重置，请重新登录"}


@router.post("/auth/reauth/password")
def reauthenticate(body: ReauthInput, request: Request):
    s = require_shop_owner(request)
    _throttle(request, "account-reauth", s["shop_id"])
    with db() as c:
        row = shop_row(c, s["shop_id"])
        if not _password_valid(row["password_hash"], body.password):
            fail("登录信息或 2FA 验证码错误", 401)
        _verify_second_factor(c, row, body.code, body.backup_code)
        c.execute(
            "UPDATE sessions SET auth_at=?,auth_method=? WHERE digest=?",
            (
                time.time(),
                "email_password_totp" if row["totp_secret"] else "email_password",
                s["digest"],
            ),
        )
        audit(c, _actor(row["id"]), "account.reauthenticate", row["id"])
    return {"ok": True}


@router.post("/auth/password/change")
def change_password(body: PasswordChange, request: Request, response: Response):
    s = require_shop_owner(request)
    _throttle(request, "password-change", s["shop_id"])
    with db() as c:
        shop = shop_row(c, s["shop_id"])
        if not _password_valid(shop["password_hash"], body.password):
            fail("登录信息或 2FA 验证码错误", 401)
        _verify_second_factor(c, shop, body.code, body.backup_code)
        c.execute(
            "UPDATE shops SET password_hash=? WHERE id=?",
            (ph.hash(body.new_password), shop["id"]),
        )
        _revoke_account(c, shop["id"], "account.password.change")
        if s.get("channel") != "cli":
            create_session(
                c,
                response,
                "admin",
                request=request,
                shop_id=shop["id"],
                auth_method="email_password_totp"
                if shop["totp_secret"]
                else "email_password",
            )
        audit(c, _actor(shop["id"]), "account.password.change", shop["id"])
    return {"ok": True}


@router.post("/auth/totp/setup")
def setup_totp(body: ReauthInput, request: Request):
    from .totp import new_secret, provisioning_uri

    s = require_shop_owner(request)
    _throttle(request, "totp-setup", s["shop_id"])
    with db() as c:
        row = shop_row(c, s["shop_id"])
        if row["totp_secret"]:
            fail("请先关闭现有 2FA 双因素认证", 409)
        if not _password_valid(row["password_hash"], body.password):
            fail("登录信息错误", 401)
        c.execute(
            "UPDATE sessions SET auth_at=?,auth_method='email_password' WHERE digest=?",
            (time.time(), s["digest"]),
        )
        secret = new_secret()
        expires = time.time() + 600
        c.execute(
            "UPDATE shops SET pending_totp_secret=?,pending_totp_expires=? WHERE id=?",
            (_seal(secret, row["id"], pending=True), expires, row["id"]),
        )
        audit(c, _actor(row["id"]), "account.totp.setup", row["id"])
        return {
            "secret": secret,
            "uri": provisioning_uri(secret, row["email"]),
            "expires": expires,
        }


@router.post("/auth/totp/confirm")
def confirm_totp(body: TotpConfirm, request: Request):
    from .owner_cli_auth import invalidate_pending_owner_approvals
    from .totp import generate_backup_codes, match_counter

    s = require_shop_owner(request)
    require_recent(s)
    _throttle(request, "totp-confirm", s["shop_id"])
    with db() as c:
        row = shop_row(c, s["shop_id"])
        if (
            row["totp_secret"]
            or not row["pending_totp_secret"]
            or row["pending_totp_expires"] < time.time()
        ):
            fail("验证设置已过期，请重新开始", 400)
        secret = _secret(row["pending_totp_secret"], row["id"], pending=True)
        counter = match_counter(
            secret, body.code, time.time(), row["last_totp_counter"]
        )
        if counter is None:
            fail("2FA 验证码错误", 401)
        raw, hashes = generate_backup_codes()
        c.execute(
            "UPDATE shops SET totp_secret=?,pending_totp_secret=NULL,pending_totp_expires=NULL,last_totp_counter=?,totp_backup_digests=? WHERE id=?",
            (_seal(secret, row["id"]), counter, json.dumps(hashes), row["id"]),
        )
        invalidate_pending_owner_approvals(c, row["id"])
        audit(c, _actor(row["id"]), "account.totp.enable", row["id"])
        return {"ok": True, "backup_codes": raw}


def _totp_security_write(c, s, body):
    row = shop_row(c, s["shop_id"])
    if not row["totp_secret"] or not _password_valid(
        row["password_hash"], body.password
    ):
        fail("登录信息或 2FA 验证码错误", 401)
    _verify_second_factor(c, row, body.code, body.backup_code)
    c.execute(
        "UPDATE sessions SET auth_at=?,auth_method='email_password_totp' WHERE digest=?",
        (time.time(), s["digest"]),
    )
    return row


@router.post("/auth/totp/disable")
def disable_totp(body: ReauthInput, request: Request):
    from .owner_cli_auth import invalidate_pending_owner_approvals

    s = require_shop_owner(request)
    _throttle(request, "totp-disable", s["shop_id"])
    with db() as c:
        row = _totp_security_write(c, s, body)
        c.execute(
            "UPDATE shops SET totp_secret=NULL,pending_totp_secret=NULL,pending_totp_expires=NULL,totp_backup_digests='[]',last_totp_counter=-1 WHERE id=?",
            (row["id"],),
        )
        invalidate_pending_owner_approvals(c, row["id"])
        audit(c, _actor(row["id"]), "account.totp.disable", row["id"])
    return {"ok": True}


@router.post("/auth/totp/backup-codes")
def rotate_backup_codes(body: ReauthInput, request: Request):
    from .owner_cli_auth import invalidate_pending_owner_approvals
    from .totp import generate_backup_codes

    s = require_shop_owner(request)
    _throttle(request, "totp-backup-rotate", s["shop_id"])
    with db() as c:
        row = _totp_security_write(c, s, body)
        raw, hashes = generate_backup_codes()
        c.execute(
            "UPDATE shops SET totp_backup_digests=? WHERE id=?",
            (json.dumps(hashes), row["id"]),
        )
        invalidate_pending_owner_approvals(c, row["id"])
        audit(c, _actor(row["id"]), "account.totp.backup.rotate", row["id"])
        return {"ok": True, "backup_codes": raw}

import hashlib
import hmac
import json
import math
import secrets
import time
import uuid

from fastapi import HTTPException, Request, Response

from .config import COOKIE_SECURE
from .db import db


def token():
    return secrets.token_urlsafe(32)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def card_digest(value):
    return digest(value.strip().upper().replace("-", "").replace(" ", ""))


def split_codes(value):
    """Split a pasted list, keeping the first copy of each normalized code."""
    import re

    seen = set()
    codes = []
    for part in re.split(r"[\s,，;；]+", str(value or "").strip()):
        if not part:
            continue
        key = part.strip().upper().replace("-", "").replace(" ", "")
        if not key or key in seen:
            continue
        seen.add(key)
        codes.append(part.strip())
    return codes


def new_card():
    # 160 bits of entropy, no modulo bias; ASCII base32 alphabet.
    import base64

    raw = base64.b32encode(secrets.token_bytes(20)).decode()
    return "-".join(raw[i : i + 8] for i in range(0, 32, 8))


def fail(message, code=400):
    raise HTTPException(code, message)


def rate_limit(request, bucket, limit=30, window=60):
    # Deliberately ignore forwarded headers; only trust the directly connected peer.
    key = f"{bucket}:{request.client.host if request.client else 'unknown'}"
    now = time.time()
    with db() as c:
        c.execute("DELETE FROM rate_limits WHERE expires<?", (now,))
        row = c.execute("SELECT * FROM rate_limits WHERE key=?", (key,)).fetchone()
        if row and row["count"] >= limit:
            fail("请求过于频繁，请稍后重试", 429)
        c.execute(
            "INSERT INTO rate_limits VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET count=count+1",
            (key, now + window),
        )


def create_session(
    c,
    response: Response,
    role,
    staff_id=None,
    request=None,
    *,
    shop_id=None,
    auth_at=None,
    auth_method=None,
):
    from .db import audit
    from .link_access import cleanup_sessions, request_metadata, revoke_session

    value = token()
    now = time.time()
    if role not in ("admin", "bootstrap", "staff"):
        raise ValueError("Unknown login role")
    if role == "staff":
        authorization = staff_authorization(c, staff_id)
        if shop_id is not None and shop_id != authorization["shop_id"]:
            raise ValueError("Management-link shop does not match the login")
        shop_id = authorization["shop_id"]
    elif role == "admin" and shop_id is not None:
        from .shops import shop_row

        shop_row(c, shop_id)
    elif role == "bootstrap" and shop_id is not None:
        raise ValueError("Bootstrap sessions cannot own a shop")
    actor = (
        staff_id
        if role == "staff"
        else (f"shop:{shop_id}" if shop_id is not None else "owner")
        if role == "admin"
        else "bootstrap"
    )
    if request is not None:
        previous = digest(request.cookies.get("extore_session", ""))
        if c.execute(
            "SELECT 1 FROM sessions WHERE digest=? AND channel='browser'", (previous,)
        ).fetchone():
            revoke_session(c, previous, actor, "session.replace")
    cleanup_sessions(c, now)
    sid = str(uuid.uuid4())
    ip, ua = request_metadata(request)
    c.execute(
        "INSERT INTO sessions(digest,role,staff_id,expires,created,id,last_seen,ip,ua,revoked,shop_id,auth_at,auth_method) "
        "VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?)",
        (
            digest(value),
            role,
            staff_id,
            now + (600 if role == "bootstrap" else 28800),
            now,
            sid,
            now,
            ip,
            ua,
            shop_id,
            now if auth_at is None else auth_at,
            auth_method or ("management_link" if role == "staff" else "legacy"),
        ),
    )
    audit(c, actor, "session.create", sid)
    response.set_cookie(
        "extore_session",
        value,
        secure=COOKIE_SECURE,
        httponly=True,
        samesite="strict",
        max_age=600 if role == "bootstrap" else 28800,
        path="/",
    )
    return value


def session_credential_digest(request):
    """Select one explicit channel; an invalid bearer never falls back to cookies."""
    authorization = request.headers.get("authorization")
    cookie = request.cookies.get("extore_session", "")
    if authorization is not None:
        if cookie:
            fail("不能混用浏览器与 CLI 登录凭证", 401)
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
            fail("CLI 登录凭证无效", 401)
        return digest(parts[1]), "cli"
    return digest(cookie), "browser"


def require_cli_bearer(request):
    if request.headers.get("authorization") is None:
        fail("请使用 CLI 登录凭证", 401)
    result = session(request, ("staff", "admin"))
    if result["channel"] != "cli":
        fail("CLI 登录凭证无效", 401)
    return result


def session(request: Request, roles=("admin",)):
    credential, channel = session_credential_digest(request)
    with db() as c:
        row = c.execute(
            "SELECT * FROM sessions WHERE digest=? AND expires>? AND revoked=0 AND channel=?",
            (credential, time.time(), channel),
        ).fetchone()
        if not row or row["role"] not in roles:
            fail("请先登录", 401)
        result = dict(row)
        if channel == "cli" and not _cli_route_allowed(request, result["role"]):
            fail("此接口不接受该 CLI 登录凭证", 401)
        now = time.time()
        if now - result["last_seen"] >= 30:
            c.execute(
                "UPDATE sessions SET last_seen=? WHERE digest=? AND revoked=0",
                (now, row["digest"]),
            )
            result["last_seen"] = now
        if row["role"] in ("admin", "staff"):
            authorize_management(c, result)
        expected_scope = request.headers.get("x-extore-shop-scope")
        expected_session = request.headers.get("x-extore-session-id")
        actual_scope = result.get("shop_id") or "platform"
        if (expected_scope is not None and expected_scope != actual_scope) or (
            expected_session is not None and expected_session != result["id"]
        ):
            fail("登录账号已切换，请刷新页面后重试", 409)
        return result


def _cli_route_allowed(request, role):
    path, method = request.url.path, request.method
    if role == "staff":
        return path.startswith("/api/manage/") or (method, path) in (
            ("GET", "/api/cli/status"),
            ("DELETE", "/api/cli/session"),
        )
    if role != "admin":
        return False
    return (
        path.startswith(("/api/admin/", "/api/manage/", "/api/platform/", "/api/shop/"))
        or (method, path)
        in (
            ("GET", "/api/cli/owner/status"),
            ("DELETE", "/api/cli/owner/session"),
            ("POST", "/api/cli/owner/action-challenge"),
            ("GET", "/api/auth/status"),
            ("GET", "/api/auth/passkeys"),
            ("POST", "/api/auth/register/options"),
            ("POST", "/api/auth/register/verify"),
            ("POST", "/api/auth/reauth/password"),
            ("POST", "/api/auth/password/change"),
            ("POST", "/api/auth/totp/setup"),
            ("POST", "/api/auth/totp/confirm"),
            ("POST", "/api/auth/totp/disable"),
            ("POST", "/api/auth/totp/backup-codes"),
        )
        or (method == "DELETE" and path.startswith("/api/auth/passkeys/"))
    )


def _staff_permissions(value):
    from .models import LINK_PERMISSIONS

    try:
        permissions = json.loads(value)
    except (TypeError, ValueError):
        fail("商品管理链接无效，请重新登录", 401)
    if (
        not isinstance(permissions, list)
        or not permissions
        or any(not isinstance(p, str) or p not in LINK_PERMISSIONS for p in permissions)
    ):
        fail("商品管理链接无效，请重新登录", 401)
    if (
        {"queue.process", "queue.retry"} & set(permissions)
        and "queue.view" not in permissions
    ) or ("fulfillment.configure" in permissions and "product.edit" not in permissions):
        fail("商品管理链接无效，请重新登录", 401)
    return [p for p in LINK_PERMISSIONS if p in permissions]


def staff_authorization(c, staff_id):
    """Resolve a link and its ancestors under the caller's transaction."""
    from .pipeline_scopes import validate_staff_scope

    now = time.time()
    seen = set()
    current_id = staff_id
    result = None
    effective_permissions = None
    effective_expires = None
    while current_id is not None:
        if current_id in seen:
            fail("商品管理链接已过期或撤销", 401)
        seen.add(current_id)
        row = c.execute("SELECT * FROM staff WHERE id=?", (current_id,)).fetchone()
        if not row or row["revoked"] or ("archived" in row.keys() and row["archived"]):
            fail("商品管理链接已过期或撤销", 401)
        try:
            expires = float(row["expires"])
        except (TypeError, ValueError, OverflowError):
            fail("商品管理链接已过期或撤销", 401)
        if not math.isfinite(expires) or expires <= now:
            fail("商品管理链接已过期或撤销", 401)
        # A scope's dedicated link is also an ancestor of delegated links.
        # Refresh every boundary on every use, including browser descendants.
        validate_staff_scope(c, row)
        permissions = _staff_permissions(row["permissions"])
        if result is None:
            result = dict(row)
            effective_permissions = permissions
            effective_expires = expires
        else:
            if row["product_id"] != result["product_id"]:
                fail("商品管理链接无效，请重新登录", 401)
            effective_permissions = [
                p for p in effective_permissions if p in permissions
            ]
            effective_expires = min(effective_expires, expires)
        if not effective_permissions:
            fail("商品管理链接无效，请重新登录", 401)
        current_id = row["parent_id"]
    if result is None:
        fail("商品管理链接已过期或撤销", 401)
    result["permissions"] = effective_permissions
    result["expires"] = effective_expires
    from .shops import default_shop, shop_row

    product = c.execute(
        "SELECT shop_id FROM products WHERE id=?", (result["product_id"],)
    ).fetchone()
    if product is None:
        fail("商品管理链接已失效", 401)
    result["shop_id"] = product["shop_id"] or default_shop(c)
    shop_row(c, result["shop_id"])
    return result


def authorize_management(c, s, permission=None):
    """Refresh authorization before accessing or changing management data."""
    from .models import LINK_PERMISSIONS

    current = c.execute(
        "SELECT role,staff_id,channel,device_id,owner_device_id,shop_id FROM sessions WHERE digest=? AND revoked=0 AND expires>?",
        (s.get("digest", ""), time.time()),
    ).fetchone()
    if (
        current is None
        or current["role"] != s["role"]
        or current["staff_id"] != s.get("staff_id")
        or current["channel"] != s.get("channel", "browser")
        or current["device_id"] != s.get("device_id")
        or current["owner_device_id"] != s.get("owner_device_id")
        or (current["role"] == "admin" and current["shop_id"] != s.get("shop_id"))
    ):
        fail("请先登录", 401)
    if current["channel"] == "cli":
        if s["role"] == "admin":
            from .owner_cli_auth import owner_device

            if (
                current["staff_id"] is not None
                or current["device_id"] is not None
                or not current["owner_device_id"]
            ):
                fail("商家 CLI 登录凭证无效", 401)
            device = owner_device(c, current["owner_device_id"])
            if device["shop_id"] != current["shop_id"]:
                fail("商家 CLI 设备与店铺不匹配", 401)
            s.update(
                scope="shop.owner",
                fingerprint=device["fingerprint"],
                grant_expires=device["expires"],
            )
            device_table = "owner_cli_devices"
        elif s["role"] == "staff" and current["owner_device_id"] is None:
            device = c.execute(
                "SELECT * FROM cli_devices WHERE id=? AND staff_id=? AND revoked=0",
                (current["device_id"], current["staff_id"]),
            ).fetchone()
            device_table = "cli_devices"
            if device is None:
                fail("CLI 设备授权已失效", 401)
        else:
            fail("CLI 登录凭证无效", 401)
        s["client_name"] = device["client_name"]
        if time.time() - device["last_seen"] >= 30:
            c.execute(
                f"UPDATE {device_table} SET last_seen=? WHERE id=?",
                (time.time(), device["id"]),
            )
    if s["role"] == "admin":
        from .shops import shop_row

        if current["shop_id"] is not None:
            shop_row(c, current["shop_id"])
        s["shop_id"] = current["shop_id"]
        s["account_id"] = (
            f"shop:{current['shop_id']}" if current["shop_id"] is not None else "owner"
        )
        s["permissions"] = list(LINK_PERMISSIONS)
    elif s["role"] == "staff":
        staff = staff_authorization(c, s.get("staff_id"))
        if current["channel"] == "cli":
            from .pipeline_scopes import check_device_scope

            pipeline_scope = check_device_scope(c, device, staff)
            if pipeline_scope is not None:
                s.update(
                    authorization_id=pipeline_scope["id"],
                    authorization_revision=pipeline_scope["revision"],
                    scope=pipeline_scope["kind"],
                )
        if current["shop_id"] not in (None, staff["shop_id"]):
            fail("商品管理登录与店铺不匹配", 401)
        s.update(
            product_id=staff["product_id"],
            shop_id=staff["shop_id"],
            permissions=staff["permissions"],
            name=staff["name"],
            link_expires=staff["expires"],
            parent_id=staff["parent_id"],
            max_uses=staff["max_uses"],
            uses=staff["uses"],
            remaining_uses=max(0, staff["max_uses"] - staff["uses"]),
            max_cli_uses=staff["max_cli_uses"],
            cli_uses=staff["cli_uses"],
            remaining_cli_uses=max(0, staff["max_cli_uses"] - staff["cli_uses"]),
        )
    else:
        fail("请先登录", 401)
    if permission is not None and permission not in s["permissions"]:
        fail("此商品管理链接没有执行该操作的权限", 403)
    return s


def link_descendant_ids(c, staff_id, include_self=True):
    """Enumerate descendants once each, even if a corrupt hierarchy cycles."""
    rows = c.execute(
        "WITH RECURSIVE descendants(id) AS ("
        " SELECT id FROM staff WHERE id=?"
        " UNION"
        " SELECT staff.id FROM staff JOIN descendants ON staff.parent_id=descendants.id"
        ") SELECT id FROM descendants ORDER BY id",
        (staff_id,),
    )
    return [r["id"] for r in rows if include_self or r["id"] != staff_id]


def grant(c, value):
    r = c.execute(
        "SELECT cards.* FROM grants JOIN cards ON cards.id=grants.card_id WHERE grants.digest=? AND grants.expires>?",
        (digest(value), time.time()),
    ).fetchone()
    if not r:
        fail("兑换凭证无效或已过期，请重新输入卡密", 404)
    return r


def batch_cards(c, value):
    rows = c.execute(
        "SELECT cards.* FROM receipt_batches "
        "JOIN receipt_batch_cards ON receipt_batch_cards.digest=receipt_batches.digest "
        "JOIN cards ON cards.id=receipt_batch_cards.card_id "
        "WHERE receipt_batches.digest=? AND receipt_batches.expires>? "
        "ORDER BY receipt_batch_cards.position",
        (digest(value), time.time()),
    ).fetchall()
    return rows


def resolve_customer_card(c, value, card_id=None):
    """Authorize one card from a single grant or from one link covering several codes."""
    single = c.execute(
        "SELECT cards.* FROM grants JOIN cards ON cards.id=grants.card_id WHERE grants.digest=? AND grants.expires>?",
        (digest(value), time.time()),
    ).fetchone()
    if single:
        if card_id and card_id != single["id"]:
            fail("这张卡密不在此领取链接中", 404)
        return single
    rows = batch_cards(c, value)
    if not rows:
        fail("兑换凭证无效或已过期，请重新输入卡密", 404)
    if not card_id:
        fail("请选择一张卡密", 400)
    found = next((row for row in rows if row["id"] == card_id), None)
    if not found:
        fail("这张卡密不在此领取链接中", 404)
    return found


def sign(secret, timestamp, nonce, body):
    return hmac.new(
        secret.encode(),
        timestamp.encode() + b"." + nonce.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()


def verify_signature(secret, timestamp, nonce, body, signature):
    try:
        valid_time = abs(time.time() - int(timestamp)) <= 300
    except (TypeError, ValueError):
        valid_time = False
    if (
        not valid_time
        or not nonce
        or len(nonce) > 128
        or not hmac.compare_digest(sign(secret, timestamp, nonce, body), signature)
    ):
        fail("回调签名无效或过期", 401)

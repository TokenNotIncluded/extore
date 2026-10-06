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


def create_session(c, response: Response, role, staff_id=None, request=None):
    from .db import audit
    from .link_access import cleanup_sessions, request_metadata, revoke_session

    value = token()
    now = time.time()
    if role not in ("admin", "bootstrap", "staff"):
        raise ValueError("Unknown login role")
    if role == "staff":
        staff_authorization(c, staff_id)
    actor = (
        staff_id if role == "staff" else ("owner" if role == "admin" else "bootstrap")
    )
    if request is not None:
        revoke_session(
            c,
            digest(request.cookies.get("extore_session", "")),
            actor,
            "session.replace",
        )
    cleanup_sessions(c, now)
    sid = str(uuid.uuid4())
    ip, ua = request_metadata(request)
    c.execute(
        "INSERT INTO sessions(digest,role,staff_id,expires,created,id,last_seen,ip,ua,revoked) "
        "VALUES (?,?,?,?,?,?,?,?,?,0)",
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


def session(request: Request, roles=("admin",)):
    with db() as c:
        row = c.execute(
            "SELECT * FROM sessions WHERE digest=? AND expires>? AND revoked=0",
            (digest(request.cookies.get("extore_session", "")), time.time()),
        ).fetchone()
        if not row or row["role"] not in roles:
            fail("请先登录", 401)
        result = dict(row)
        now = time.time()
        if now - result["last_seen"] >= 30:
            c.execute(
                "UPDATE sessions SET last_seen=? WHERE digest=? AND revoked=0",
                (now, row["digest"]),
            )
            result["last_seen"] = now
        if row["role"] == "staff":
            authorize_management(c, result)
        return result


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
        if not row or row["revoked"]:
            fail("商品管理链接已过期或撤销", 401)
        try:
            expires = float(row["expires"])
        except (TypeError, ValueError, OverflowError):
            fail("商品管理链接已过期或撤销", 401)
        if not math.isfinite(expires) or expires <= now:
            fail("商品管理链接已过期或撤销", 401)
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
    return result


def authorize_management(c, s, permission=None):
    """Refresh authorization before accessing or changing management data."""
    from .models import LINK_PERMISSIONS

    current = c.execute(
        "SELECT role,staff_id FROM sessions WHERE digest=? AND revoked=0 AND expires>?",
        (s.get("digest", ""), time.time()),
    ).fetchone()
    if (
        current is None
        or current["role"] != s["role"]
        or current["staff_id"] != s.get("staff_id")
    ):
        fail("请先登录", 401)
    if s["role"] == "admin":
        s["permissions"] = list(LINK_PERMISSIONS)
    elif s["role"] == "staff":
        staff = staff_authorization(c, s.get("staff_id"))
        s.update(
            product_id=staff["product_id"],
            permissions=staff["permissions"],
            name=staff["name"],
            link_expires=staff["expires"],
            parent_id=staff["parent_id"],
            max_uses=staff["max_uses"],
            uses=staff["uses"],
            remaining_uses=max(0, staff["max_uses"] - staff["uses"]),
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

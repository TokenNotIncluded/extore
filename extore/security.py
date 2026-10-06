import hashlib
import hmac
import secrets
import time

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


def create_session(c, response: Response, role, staff_id=None):
    value = token()
    now = time.time()
    c.execute("DELETE FROM sessions WHERE expires<?", (now,))
    c.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?)",
        (
            digest(value),
            role,
            staff_id,
            now + (600 if role == "bootstrap" else 28800),
            now,
        ),
    )
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
            "SELECT * FROM sessions WHERE digest=? AND expires>?",
            (digest(request.cookies.get("extore_session", "")), time.time()),
        ).fetchone()
        if not row or row["role"] not in roles:
            fail("请先登录", 401)
        result = dict(row)
        if row["role"] == "staff":
            staff = c.execute(
                "SELECT * FROM staff WHERE id=? AND revoked=0 AND expires>?",
                (row["staff_id"], time.time()),
            ).fetchone()
            if not staff:
                fail("员工授权已过期或撤销", 401)
            result["product_id"] = staff["product_id"]
        return result


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

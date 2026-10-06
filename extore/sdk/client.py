import hashlib
import hmac
import json
import secrets
import time
import urllib.request


def signature(secret, timestamp, nonce, body):
    return hmac.new(
        secret.encode(),
        timestamp.encode() + b"." + nonce.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()


def verify_event(secret, body, headers):
    headers = {k.lower(): v for k, v in headers.items()}
    ts = headers.get("x-extore-timestamp", "")
    nonce = headers.get("x-extore-nonce", "")
    try:
        if abs(time.time() - int(ts)) > 300 or not nonce:
            raise ValueError("expired signature")
    except (ValueError, TypeError):
        raise ValueError("expired signature") from None
    if not hmac.compare_digest(
        signature(secret, ts, nonce, body), headers.get("x-extore-signature", "")
    ):
        raise ValueError("invalid signature")
    return json.loads(body)


class Client:
    def __init__(self, origin, secret):
        self.origin = origin.rstrip("/")
        self.secret = secret

    def update(
        self,
        product_id,
        task_id,
        attempt,
        *,
        state,
        progress=0,
        message="",
        content=None,
        output=None,
        retryable=False,
    ):
        body = json.dumps(
            dict(
                attempt=attempt,
                state=state,
                progress=progress,
                message=message,
                content=content,
                output=output,
                retryable=retryable,
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        ts = str(int(time.time()))
        nonce = secrets.token_urlsafe(24)
        req = urllib.request.Request(
            f"{self.origin}/api/callbacks/{product_id}/{task_id}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Extore-Timestamp": ts,
                "X-Extore-Nonce": nonce,
                "X-Extore-Signature": signature(self.secret, ts, nonce, body),
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)

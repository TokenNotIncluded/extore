import hashlib
import hmac
import json
import math
import re
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


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
        completed_steps=None,
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
                completed_steps=completed_steps,
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


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}\Z")
_FIELD = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_SCOPE = (
    "shop_id",
    "product_id",
    "job_id",
    "attempt",
    "node_id",
    "flow_epoch",
    "action_id",
)
MAX_FILE_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class FlowScope:
    """Exact server-issued execution identity; never construct it from customer inputs."""

    shop_id: str
    product_id: str
    job_id: str
    attempt: int
    node_id: str
    flow_epoch: int
    action_id: str

    def __post_init__(self):
        for name in _SCOPE:
            value = getattr(self, name)
            if name in ("attempt", "flow_epoch"):
                if type(value) is not int or not 1 <= value <= 2**53 - 1:
                    raise ValueError("Invalid flow execution identity")
            elif not isinstance(value, str) or not _ID.fullmatch(value):
                raise ValueError("Invalid flow execution identity")

    @classmethod
    def from_context(cls, context):
        return cls(**{name: context[name] for name in _SCOPE})


def _json_v2(value):
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("ascii")


def signature_v2(
    secret, scope, *, direction, audience, method, path, timestamp, nonce, body_digest
):
    if direction not in ("worker-to-extore", "extore-to-worker"):
        raise ValueError("Invalid signature direction")
    if not isinstance(secret, str) or not secret:
        raise ValueError("Missing private-worker secret")
    key = hmac.new(
        secret.encode(),
        b"extore-private-worker-key-v2\0" + direction.encode(),
        hashlib.sha256,
    ).digest()
    values = [
        "extore-private-worker-signature-v2",
        direction,
        audience,
        method,
        path,
        timestamp,
        nonce,
        body_digest,
        *[getattr(scope, name) for name in _SCOPE],
    ]
    return hmac.new(key, _json_v2(values), hashlib.sha256).hexdigest()


def verify_flow_event(secret, body, headers, *, audience, path, method="POST"):
    """Verify one dispatch; the worker must durably deduplicate nonce/action_id itself."""
    try:
        values = {}
        for name, value in headers.items():
            name = name.lower()
            if name.startswith("x-extore-") and name in values:
                raise ValueError("Duplicate signed header")
            values[name] = value
        if (
            values["x-extore-version"] != "2"
            or values["x-extore-direction"] != "extore-to-worker"
            or values["x-extore-audience"] != audience
        ):
            raise ValueError("Invalid event target")
        ts, nonce = values["x-extore-timestamp"], values["x-extore-nonce"]
        if (
            not re.fullmatch(r"[0-9]{1,12}", ts)
            or abs(time.time() - int(ts)) > 300
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", nonce)
        ):
            raise ValueError("Expired event")
        scope_values = {}
        for name in _SCOPE:
            value = values["x-extore-" + name.replace("_", "-")]
            if name in ("attempt", "flow_epoch"):
                if not re.fullmatch(r"[1-9][0-9]*", value):
                    raise ValueError("Invalid execution identity")
                value = int(value)
            scope_values[name] = value
        scope = FlowScope(**scope_values)
        body_digest = hashlib.sha256(body).hexdigest()
        if values["x-extore-body-sha256"] != body_digest or not hmac.compare_digest(
            signature_v2(
                secret,
                scope,
                direction="extore-to-worker",
                audience=audience,
                method=method,
                path=path,
                timestamp=ts,
                nonce=nonce,
                body_digest=body_digest,
            ),
            values["x-extore-signature"],
        ):
            raise ValueError("Invalid event signature")

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("Duplicate JSON key")
                result[key] = value
            return result

        def constant(_):
            raise ValueError("Invalid JSON number")

        if len(body) > 256000:
            raise ValueError("Event is too large")
        result = json.loads(body, object_pairs_hook=pairs, parse_constant=constant)
        if not isinstance(result, dict):
            raise ValueError("Event must be an object")
        if (
            type(result.get("version")) is not int
            or result["version"] != 2
            or result.get("type") != "flow.process.requested"
            or not isinstance(result.get("scope"), dict)
            or set(result["scope"]) != set(_SCOPE)
            or FlowScope.from_context(result["scope"]) != scope
            or type(result.get("deadline")) not in (int, float)
            or not math.isfinite(result["deadline"])
            or result["deadline"] <= time.time()
        ):
            raise ValueError("Invalid or expired dispatch scope")
        return result
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError("Invalid private-worker event") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PrivateWorkerClient:
    """V2 callbacks and files, bound to one server-issued flow execution."""

    def __init__(self, origin, secret, *, opener=None):
        parsed = urlsplit(origin)
        if (
            not isinstance(origin, str)
            or any(ord(char) < 33 or ord(char) > 126 for char in origin)
            or parsed.port is not None
            and not 1 <= parsed.port <= 65535
            or "%" in parsed.netloc
            or "\\" in origin
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Use the exact HTTPS Extore origin")
        self.origin = origin.rstrip("/")
        if not isinstance(secret, str) or not secret:
            raise ValueError("Missing private-worker secret")
        self.secret = secret
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def _request(self, scope, method, path, body, *, headers=None, binary=False):
        ts, nonce = str(int(time.time())), secrets.token_urlsafe(24)
        body_digest = hashlib.sha256(body).hexdigest()
        signed = {
            "X-Extore-Version": "2",
            "X-Extore-Direction": "worker-to-extore",
            "X-Extore-Audience": self.origin,
            "X-Extore-Timestamp": ts,
            "X-Extore-Nonce": nonce,
            "X-Extore-Body-SHA256": body_digest,
            "X-Extore-Signature": signature_v2(
                self.secret,
                scope,
                direction="worker-to-extore",
                audience=self.origin,
                method=method,
                path=path,
                timestamp=ts,
                nonce=nonce,
                body_digest=body_digest,
            ),
            **{
                "X-Extore-" + name.replace("_", "-"): str(getattr(scope, name))
                for name in _SCOPE
            },
        }
        request = urllib.request.Request(
            self.origin + path,
            data=body if method == "POST" else None,
            headers={**(headers or {}), **signed},
            method=method,
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                maximum = MAX_FILE_BYTES if binary else 1000000
                raw = response.read(maximum + 1)
                if len(raw) > maximum:
                    raise ValueError("Response is too large")
                return raw if binary else json.loads(raw)
        except (urllib.error.URLError, ValueError, UnicodeError):
            raise ValueError("Private-worker request failed") from None

    def update(self, scope, result_id, *, state, **values):
        if (
            not isinstance(scope, FlowScope)
            or not isinstance(result_id, str)
            or not _ID.fullmatch(result_id)
        ):
            raise ValueError("Invalid result identity")
        allowed = {
            "progress",
            "message",
            "content",
            "output",
            "retryable",
            "completed_steps",
            "flow_epoch",
            "action_id",
        }
        if set(values) - allowed:
            raise ValueError("Unknown result field")
        if (
            values.get("flow_epoch", scope.flow_epoch) != scope.flow_epoch
            or values.get("action_id", scope.action_id) != scope.action_id
        ):
            raise ValueError("Result targets another flow execution")
        body = _json_v2(
            {
                "version": 2,
                "result_id": result_id,
                "update": {
                    "attempt": scope.attempt,
                    "state": state,
                    "flow_epoch": scope.flow_epoch,
                    "action_id": scope.action_id,
                    **values,
                },
            }
        )
        if len(body) > 256000:
            raise ValueError("Result is too large")
        return self._request(
            scope,
            "POST",
            f"/api/callbacks/v2/{scope.product_id}/{scope.job_id}/result",
            body,
            headers={"Content-Type": "application/json"},
        )

    def upload(self, scope, field, source, *, result_id):
        if (
            not isinstance(scope, FlowScope)
            or not _FIELD.fullmatch(field)
            or not _ID.fullmatch(result_id)
        ):
            raise ValueError("Invalid output file identity")
        source = Path(source)
        import os
        import stat

        with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), "rb") as upload:
            info = os.fstat(upload.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or not 1 <= info.st_size <= MAX_FILE_BYTES
            ):
                raise ValueError("Invalid output file")
            body = upload.read(MAX_FILE_BYTES + 1)
        if len(body) > MAX_FILE_BYTES:
            raise ValueError("Output file is too large")
        filename = source.name.encode("ascii", "replace").decode().replace("?", "_")
        return self._request(
            scope,
            "POST",
            f"/api/callbacks/v2/{scope.product_id}/{scope.job_id}/files/{field}/{result_id}/upload",
            body,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Extore-Filename": filename,
            },
        )

    def download(self, scope, field, file_id):
        if (
            not isinstance(scope, FlowScope)
            or not _FIELD.fullmatch(field)
            or not _ID.fullmatch(file_id)
        ):
            raise ValueError("Invalid input file identity")
        return self._request(
            scope,
            "GET",
            f"/api/callbacks/v2/{scope.product_id}/{scope.job_id}/files/{field}/{file_id}/download",
            b"",
            binary=True,
        )

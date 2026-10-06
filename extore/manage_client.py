"""Private, device-bound client for remote product management.

Each grant keeps its own origin, device key and permissions. Permissions from two
links are never combined to authorize one request.
"""

import argparse
import base64
import getpass
import hashlib
import json
import math
import os
import re
import secrets
import socket
import stat
import sys
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
PROFILE_VERSION = 1
DEVICE_LOGIN_TTL = 600


class ManageError(Exception):
    def __init__(self, message, *, code="error", status=None):
        super().__init__(message)
        self.code = code
        self.status = status

    def as_dict(self):
        result = {"ok": False, "error": str(self), "code": self.code}
        if self.status is not None:
            result["status"] = self.status
        return result


def _b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _unb64(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def origin_from_url(value, *, invitation=False):
    try:
        parsed = urlsplit(value.strip())
        parsed.port
    except (ValueError, AttributeError):
        raise ManageError("Invalid Extore URL", code="invalid_url") from None
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or any(ord(char) < 33 for char in value)
        or (
            parsed.scheme == "http"
            and parsed.hostname not in ("localhost", "127.0.0.1", "::1")
        )
    ):
        raise ManageError(
            "Use an HTTPS Extore URL (HTTP is limited to loopback)", code="invalid_url"
        )
    if invitation:
        if parsed.path.rstrip("/") not in ("/staff", "/cli") or not parsed.fragment:
            raise ManageError(
                "Use the complete /staff#… or /cli#… management link",
                code="invalid_link",
            )
        fragment = unquote(parsed.fragment)
        if (
            not fragment
            or len(fragment) > 200
            or any(ord(char) < 33 for char in fragment)
        ):
            raise ManageError("Invalid management link", code="invalid_link")
    elif parsed.path.rstrip("/") or parsed.fragment:
        raise ManageError(
            "The server address must be a bare origin", code="invalid_url"
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def profile_path(value=None):
    configured = value or os.environ.get("EXTORE_CLI_CONFIG")
    if configured:
        return Path(configured).expanduser().absolute()
    root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return (root / "extore" / "cli.json").absolute()


def _private_file(path, *, missing_ok=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return
        raise ManageError(
            "No CLI authorization; run extore manage login --device-code --origin https://your-server",
            code="no_auth",
        ) from None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise ManageError(
            "CLI credential files must be regular, owned by you, and mode 600",
            code="unsafe_profile",
        )


@contextmanager
def private_profile(path, *, create=False):
    """Serialize mutations and atomically write only to a private directory."""
    import fcntl

    directory = path.parent
    if create:
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        info = directory.lstat()
    except FileNotFoundError:
        raise ManageError(
            "No CLI authorization; run extore manage login --device-code --origin https://your-server",
            code="no_auth",
        ) from None
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise ManageError(
            "The CLI credential directory must be owned by you and mode 700",
            code="unsafe_profile",
        )
    _private_file(path, missing_ok=create)
    lock = directory / (path.name + ".lock")
    _private_file(lock, missing_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _private_file(path, missing_ok=create)
        if path.exists():
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "r") as source:
                if path.stat().st_size > 1024 * 1024:
                    raise ManageError("Invalid CLI profile", code="invalid_profile")
                data = json.load(source)
            if (
                not isinstance(data, dict)
                or data.get("version") != PROFILE_VERSION
                or not isinstance(data.get("grants"), list)
            ):
                raise ManageError("Invalid CLI profile", code="invalid_profile")
            if any(
                not isinstance(grant, dict)
                or not isinstance(grant.get("id"), str)
                or not isinstance(grant.get("origin"), str)
                or not isinstance(grant.get("private_key"), str)
                for grant in data["grants"]
            ):
                raise ManageError("Invalid CLI profile", code="invalid_profile")
        else:
            data = {"version": PROFILE_VERSION, "grants": []}
        before = json.dumps(data, sort_keys=True)
        yield data
        if not path.exists() or before != json.dumps(data, sort_keys=True):
            _save_profile(path, data)
    finally:
        os.close(fd)


def _save_profile(path, data):
    directory = path.parent
    temporary = directory / ("." + path.name + "." + secrets.token_hex(12))
    try:
        temporary_fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(temporary_fd, "w") as target:
            json.dump(data, target, ensure_ascii=False, separators=(",", ":"))
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _object(value):
    if not isinstance(value, dict):
        raise ManageError(
            "The Extore server returned an invalid response", code="invalid_response"
        )
    return value


def _objects(value):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ManageError(
            "The Extore server returned an invalid response", code="invalid_response"
        )
    return value


def _safe_grant(grant):
    return {
        key: grant[key]
        for key in (
            "id",
            "origin",
            "product_id",
            "link_id",
            "link_name",
            "client_name",
            "permissions",
            "link_expires",
            "expires",
            "authorization_id",
            "authorization_revision",
            "shop_id",
        )
        if key in grant
    }


class ManageClient:
    def __init__(self, data, *, transport=None, persist=None):
        self.data = data
        self.persist = persist or (lambda: None)
        self.http = httpx.Client(
            timeout=30, follow_redirects=False, transport=transport, trust_env=False
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.http.close()

    def _request(self, origin, method, path, *, grant=None, stream=False, **kwargs):
        origin_from_url(origin)
        headers = dict(kwargs.pop("headers", {}))
        if grant is not None:
            headers["Authorization"] = "Bearer " + grant["access_token"]
        try:
            self.http.cookies.clear()
            request = self.http.build_request(
                method, origin + path, headers=headers, **kwargs
            )
            response = self.http.send(request, stream=stream)
        except httpx.HTTPError:
            raise ManageError(
                "Cannot connect to the Extore server", code="connection_error"
            ) from None
        if not 200 <= response.status_code < 300:
            status = response.status_code
            response.close()
            messages = {
                401: "CLI session expired or revoked",
                403: "This grant cannot perform the requested operation",
                404: "The requested task, file, or authorization was not found",
                409: "The task changed or authorization capacity is exhausted; refresh before retrying",
                410: "This authorization or file is no longer available",
                413: "Upload exceeds the server's storage limit",
                422: "Invalid request; check the product schema and command inputs",
                429: "Too many requests; wait before retrying",
                503: "Server storage or capacity is temporarily unavailable",
            }
            raise ManageError(
                messages.get(status, "The Extore server rejected the request"),
                code="http_error",
                status=status,
            )
        return response

    def _json(self, origin, method, path, **kwargs):
        response = self._request(origin, method, path, stream=True, **kwargs)
        try:
            chunks = []
            size = 0
            for chunk in response.iter_bytes(64 * 1024):
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ManageError(
                        "The server response exceeds the CLI size limit",
                        code="invalid_response",
                    )
                chunks.append(chunk)
            return json.loads(b"".join(chunks))
        except httpx.HTTPError:
            raise ManageError(
                "The server response was interrupted", code="connection_error"
            ) from None
        except ValueError:
            raise ManageError(
                "The Extore server returned an invalid response",
                code="invalid_response",
            ) from None
        finally:
            response.close()

    def _renew(self, grant):
        origin = grant["origin"]
        key = Ed25519PrivateKey.from_private_bytes(_unb64(grant["private_key"]))
        challenge = self._json(
            origin, "POST", "/api/cli/challenge", json={"device_id": grant["device_id"]}
        )
        challenge = _object(challenge)
        if not all(
            isinstance(challenge.get(key), str) and challenge[key]
            for key in ("challenge_id", "challenge")
        ):
            raise ManageError(
                "The Extore server returned an invalid challenge",
                code="invalid_response",
            )
        proof = "\n".join(
            (
                "extore-cli-session-v1",
                origin,
                grant["device_id"],
                challenge["challenge_id"],
                challenge["challenge"],
            )
        )
        response = self._json(
            origin,
            "POST",
            "/api/cli/session",
            json={
                "device_id": grant["device_id"],
                "challenge_id": challenge["challenge_id"],
                "signature": _b64(key.sign(proof.encode())),
            },
        )
        response = _object(response)
        if (
            response.get("device_id") != grant["device_id"]
            or response.get("product_id") != grant["product_id"]
        ):
            raise ManageError(
                "The server returned a different authorization scope",
                code="invalid_response",
            )
        grant.update(
            {
                key: response[key]
                for key in ("access_token", "expires", "permissions", "session_id")
                if key in response
            }
        )
        if (
            not isinstance(grant.get("access_token"), str)
            or not grant["access_token"]
            or any(ord(char) < 33 or ord(char) > 126 for char in grant["access_token"])
            or type(grant.get("expires")) not in (float, int)
            or not math.isfinite(grant["expires"])
            or not isinstance(grant.get("permissions"), list)
            or not all(
                isinstance(permission, str) for permission in grant["permissions"]
            )
        ):
            raise ManageError(
                "The server returned an invalid CLI session", code="invalid_response"
            )

    def session(self, grant, *, refresh_scope=False):
        if not grant.get("device_id"):
            raise ManageError(
                "Device binding was interrupted; retry login with the original link",
                code="pending_binding",
            )
        if not grant.get("access_token") or grant.get("expires", 0) < time.time() + 30:
            self._renew(grant)
        if refresh_scope:
            try:
                response = self._json(
                    grant["origin"], "GET", "/api/cli/status", grant=grant
                )
            except ManageError as exc:
                if exc.status != 401:
                    raise
                self._renew(grant)
                response = self._json(
                    grant["origin"], "GET", "/api/cli/status", grant=grant
                )
            response = _object(response)
            if (
                response.get("product_id") != grant["product_id"]
                or response.get("device_id") != grant["device_id"]
            ):
                raise ManageError(
                    "The server returned a different authorization scope",
                    code="invalid_response",
                )
            if not isinstance(response.get("permissions"), list) or not all(
                isinstance(permission, str) for permission in response["permissions"]
            ):
                raise ManageError(
                    "The server returned invalid permissions", code="invalid_response"
                )
            grant.update(
                {
                    key: response[key]
                    for key in (
                        "link_id",
                        "link_name",
                        "client_name",
                        "permissions",
                        "link_expires",
                        "expires",
                    )
                    if key in response
                }
            )
        return grant

    def login(self, invitation, client_name):
        invitation = invitation.strip()
        origin = origin_from_url(invitation, invitation=True)
        name = client_name.strip()
        if not name or len(name) > 100 or any(ord(char) < 32 for char in name):
            raise ManageError(
                "Client name must contain 1 to 100 printable characters",
                code="invalid_input",
            )
        binding = hashlib.sha256(
            (origin + "\n" + unquote(urlsplit(invitation).fragment)).encode()
        ).hexdigest()
        existing = next(
            (
                item
                for item in self.data["grants"]
                if item.get("origin") == origin and item.get("binding") == binding
            ),
            None,
        )
        if existing and existing.get("device_id"):
            self.session(existing, refresh_scope=True)
            return {
                "ok": True,
                "grant": _safe_grant(existing),
                "already_authorized": True,
            }
        if existing:
            grant = existing
            private = Ed25519PrivateKey.from_private_bytes(_unb64(grant["private_key"]))
        else:
            private = Ed25519PrivateKey.generate()
            grant = {
                "id": "pending-" + secrets.token_hex(16),
                "origin": origin,
                "binding": binding,
                "private_key": _b64(
                    private.private_bytes(
                        Encoding.Raw, PrivateFormat.Raw, NoEncryption()
                    )
                ),
                "client_name": name,
            }
            self.data["grants"].append(grant)
        # Persist before consuming a one-use ticket, including if the process is
        # interrupted after the server binds the key but before the response.
        self.persist()
        public = _b64(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
        proof = "\n".join(("extore-cli-bind-v1", origin, invitation, public))
        response = self._json(
            origin,
            "POST",
            "/api/cli/authorize",
            json={
                "token": invitation,
                "public_key": public,
                "client_name": name,
                "signature": _b64(private.sign(proof.encode())),
            },
        )
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("device_id"), str)
            or not isinstance(response.get("product_id"), str)
        ):
            raise ManageError(
                "The server returned an invalid CLI binding", code="invalid_response"
            )
        grant.update(
            {
                "id": response["device_id"],
                "device_id": response["device_id"],
                "product_id": response["product_id"],
            }
        )
        self.persist()
        self.session(grant, refresh_scope=True)
        return {
            "ok": True,
            "grant": _safe_grant(grant),
            "already_authorized": response.get("already_authorized", False),
        }

    def device_login(
        self,
        origin,
        client_name,
        *,
        product=None,
        no_wait=False,
        scope=None,
        scope_key=None,
    ):
        """Ask a browser to approve this locally generated key, without a link."""
        origin = origin_from_url(origin)
        name = client_name.strip()
        if (
            not name
            or len(name) > 100
            or any(unicodedata.category(char).startswith("C") for char in name)
            or (
                product is not None
                and (
                    not product
                    or len(product) > 100
                    or any(ord(char) < 33 for char in product)
                )
            )
        ):
            raise ManageError("Invalid device name or product ID", code="invalid_input")
        pending = self.data.setdefault("device_requests", [])
        if not isinstance(pending, list) or any(
            not isinstance(item, dict) for item in pending
        ):
            raise ManageError("Invalid CLI profile", code="invalid_profile")
        active = [
            item
            for item in pending
            if item.get("scope_claim", {})
            .get("authorization", {})
            .get("expires", item.get("expires", time.time() + 1))
            > time.time()
        ]
        if len(active) != len(pending):
            pending[:] = active
            self.persist()
        request = next(
            (
                item
                for item in pending
                if item.get("origin") == origin
                and item.get("client_name") == name
                and item.get("requested_product") == product
                and item.get("scope_intent") == scope
            ),
            None,
        )
        if request is None and scope is not None:
            # A claim can already have committed the new scope revision before
            # session renewal loses its response. Resume that saved claim even
            # though the local authorization now has the newer revision.
            for item in pending:
                previous = item.get("scope_intent")
                if (
                    item.get("scope_claim")
                    and isinstance(previous, dict)
                    and item.get("origin") == origin
                    and item.get("client_name") == name
                    and all(
                        previous.get(key) == scope.get(key)
                        for key in ("kind", "shop_id", "authorization_id", "reason")
                    )
                    and set(previous.get("product_ids", []))
                    == set(scope["product_ids"])
                    and set(previous.get("permissions", []))
                    == set(scope["permissions"])
                ):
                    request = item
                    scope = previous
                    break
        if request is None:
            device_keys = self.data.setdefault("device_keys", {})
            if not isinstance(device_keys, dict):
                raise ManageError("Invalid CLI profile", code="invalid_profile")
            existing = next(
                (
                    item
                    for item in self.data["grants"]
                    if item.get("origin") == origin
                    and item.get("device_id")
                    and (product is None or item.get("product_id") == product)
                    and scope is None
                ),
                None,
            )
            key_scope = origin + "\n" + (product or "")
            if scope is not None:
                device_keys = self.data.setdefault("scoped_device_keys", {})
                if not isinstance(device_keys, dict):
                    raise ManageError("Invalid CLI profile", code="invalid_profile")
                key_scope = (
                    origin
                    + "\n"
                    + hashlib.sha256(
                        json.dumps(
                            scope,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=True,
                        ).encode()
                    ).hexdigest()
                )
                saved = device_keys.get(key_scope)
                saved_key = scope_key or (
                    saved.get("private_key") if isinstance(saved, dict) else saved
                )
            else:
                saved_key = device_keys.get(key_scope) or (
                    existing["private_key"] if existing else None
                )
            private = (
                Ed25519PrivateKey.from_private_bytes(_unb64(saved_key))
                if saved_key
                else Ed25519PrivateKey.generate()
            )
            # The code may expire after a binding response is lost. Keep its
            # device key so a fresh approval can recover that same binding.
            encoded_key = _b64(
                private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
            )
            device_keys[key_scope] = (
                {"private_key": encoded_key, "scope_intent": scope, "origin": origin}
                if scope is not None
                else encoded_key
            )
            issued_at = int(time.time())
            request = {
                "origin": origin,
                "client_name": name,
                "requested_product": product,
                "private_key": _b64(
                    private.private_bytes(
                        Encoding.Raw, PrivateFormat.Raw, NoEncryption()
                    )
                ),
                "nonce": _b64(issued_at.to_bytes(8, "big") + secrets.token_bytes(24)),
                "expires": issued_at + DEVICE_LOGIN_TTL,
            }
            if scope is not None:
                request["scope_intent"] = scope
                request["scope_key_selector"] = key_scope
            pending.append(request)
            # Save the key and nonce before requesting a code; the server can
            # return the same request if its first response was interrupted.
            self.persist()
        private = Ed25519PrivateKey.from_private_bytes(_unb64(request["private_key"]))
        public_raw = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        public = _b64(public_raw)
        fingerprint = hashlib.sha256(public_raw).hexdigest()
        if request.get("scope_claim"):
            return self._finish_scope_login(request, pending)
        if request.get("device_id"):
            return self._finish_device_login(request, pending)
        if not request.get("request_id"):
            if scope is None:
                payload = {
                    "public_key": public,
                    "client_name": name,
                    "nonce": request["nonce"],
                    "product_id": product,
                }
                proof = "\n".join(
                    (
                        "extore-cli-device-request-v1",
                        origin,
                        public,
                        name,
                        request["nonce"],
                        product or "",
                    )
                )
                request_path = "/api/cli/device/request"
            else:
                payload = {
                    **scope,
                    "public_key": public,
                    "client_name": name,
                    "nonce": request["nonce"],
                }
                proof = (
                    "extore-cli-scope-request-v1\n"
                    + origin
                    + "\n"
                    + json.dumps(
                        payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    )
                )
                request_path = "/api/cli/scopes/request"
            payload["signature"] = _b64(private.sign(proof.encode()))
            response = _object(
                self._json(
                    origin,
                    "POST",
                    request_path,
                    json=payload,
                )
            )
            self._validate_device_request(response, origin, fingerprint)
            request.update(
                {
                    key: response[key]
                    for key in (
                        "request_id",
                        "user_code",
                        "approval_url",
                        "challenge",
                        "expires",
                        "interval",
                        "fingerprint",
                    )
                }
            )
            self.persist()
        self._validate_device_request(request, origin, fingerprint)
        visible = {
            key: request[key]
            for key in ("user_code", "approval_url", "fingerprint", "expires")
        }
        print(
            "请在浏览器打开："
            + visible["approval_url"]
            + "\n设备码："
            + visible["user_code"]
            + "\n核对设备指纹："
            + visible["fingerprint"]
            + (
                "\n请店主在浏览器核对商品清单与请求权限后批准。"
                if scope is not None
                else "\n仅批准你刚刚发起的请求；管理链接留在浏览器中。"
            ),
            file=sys.stderr,
            flush=True,
        )
        if no_wait:
            result = {"ok": True, "pending": True, "authorization": visible}
            if scope is not None:
                result["requested"] = scope
            return result
        print(
            "等待浏览器授权，按 Ctrl+C 可停止；重复此命令可继续。",
            file=sys.stderr,
            flush=True,
        )
        remaining = min(DEVICE_LOGIN_TTL, request["expires"] - time.time())
        deadline = time.monotonic() + max(0, remaining)
        interval = request["interval"]
        while time.monotonic() < deadline:
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                break
            proof = "\n".join(
                (
                    "extore-cli-scope-status-v1"
                    if scope is not None
                    else "extore-cli-device-status-v1",
                    origin,
                    request["request_id"],
                    public,
                )
            )
            try:
                response = _object(
                    self._json(
                        origin,
                        "POST",
                        "/api/cli/scopes/status"
                        if scope is not None
                        else "/api/cli/device/status",
                        json={
                            "request_id": request["request_id"],
                            "public_key": public,
                            "signature": _b64(private.sign(proof.encode())),
                        },
                        timeout=min(30, max(1, deadline - time.monotonic())),
                    )
                )
            except ManageError as exc:
                if exc.code == "connection_error" or exc.status in (429, 503):
                    interval = min(60, interval + 5)
                    continue
                raise
            state = response.get("status")
            reported_interval = response.get("interval", interval)
            retry_after = response.get("retry_after", reported_interval)
            if (
                type(reported_interval) not in (int, float)
                or not 5 <= reported_interval <= DEVICE_LOGIN_TTL
                or not math.isfinite(reported_interval)
                or type(retry_after) not in (int, float)
                or not 0 <= retry_after <= DEVICE_LOGIN_TTL
                or not math.isfinite(retry_after)
            ):
                raise ManageError(
                    "Invalid device polling interval", code="invalid_response"
                )
            interval = max(interval, reported_interval, retry_after)
            request["interval"] = interval
            self.persist()
            if state in ("pending", "slow_down"):
                continue
            if state in ("denied", "expired"):
                pending.remove(request)
                self.persist()
                raise ManageError(
                    "设备授权已被拒绝"
                    if state == "denied"
                    else "设备码已过期，请重新登录",
                    code="authorization_denied"
                    if state == "denied"
                    else "device_code_expired",
                )
            if state not in ("approved", "claimed"):
                raise ManageError(
                    "Invalid device authorization status", code="invalid_response"
                )
            proof = "\n".join(
                (
                    "extore-cli-scope-claim-v1"
                    if scope is not None
                    else "extore-cli-device-claim-v1",
                    origin,
                    request["request_id"],
                    request["challenge"],
                    public,
                )
            )
            response = _object(
                self._json(
                    origin,
                    "POST",
                    "/api/cli/scopes/claim"
                    if scope is not None
                    else "/api/cli/device/claim",
                    json={
                        "request_id": request["request_id"],
                        "public_key": public,
                        "signature": _b64(private.sign(proof.encode())),
                    },
                )
            )
            if scope is not None:
                self._validate_scope_claim(response, request, fingerprint)
                request["scope_claim"] = {
                    "authorization": response["authorization"],
                    "bindings": response["bindings"],
                }
                self.persist()
                return self._finish_scope_login(request, pending)
            if (
                not isinstance(response.get("device_id"), str)
                or not response["device_id"]
                or not isinstance(response.get("product_id"), str)
                or not response["product_id"]
                or (product and response["product_id"] != product)
                or response.get("fingerprint") != fingerprint
            ):
                raise ManageError(
                    "Invalid device authorization scope", code="invalid_response"
                )
            request.update(
                {
                    "device_id": response["device_id"],
                    "product_id": response["product_id"],
                    "already_authorized": response.get("already_authorized", False),
                }
            )
            self.persist()
            return self._finish_device_login(request, pending)
        pending.remove(request)
        self.persist()
        raise ManageError("设备码已过期，请重新登录", code="device_code_expired")

    @staticmethod
    def _validate_device_request(response, origin, fingerprint):
        if (
            not isinstance(response.get("request_id"), str)
            or not response["request_id"]
            or len(response["request_id"]) > 100
            or not isinstance(response.get("user_code"), str)
            or not re.fullmatch(
                r"[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}", response["user_code"]
            )
            or response.get("approval_url") != origin + "/cli/device"
            or not isinstance(response.get("challenge"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", response["challenge"])
            or response.get("fingerprint") != fingerprint
            or type(response.get("expires")) not in (int, float)
            or not 0 < response["expires"] <= time.time() + DEVICE_LOGIN_TTL + 5
            or not math.isfinite(response["expires"])
            or type(response.get("interval")) not in (int, float)
            or not 5 <= response["interval"] <= DEVICE_LOGIN_TTL
            or not math.isfinite(response["interval"])
        ):
            raise ManageError(
                "Invalid device authorization response", code="invalid_response"
            )

    def _finish_device_login(self, request, pending):
        grant = next(
            (
                item
                for item in self.data["grants"]
                if item.get("origin") == request["origin"]
                and item.get("device_id") == request["device_id"]
            ),
            None,
        )
        if grant is None:
            grant = {
                "id": request["device_id"],
                "device_id": request["device_id"],
                "origin": request["origin"],
                "product_id": request["product_id"],
                "private_key": request["private_key"],
                "client_name": request["client_name"],
            }
            self.data["grants"].append(grant)
            self.persist()
        self.session(grant, refresh_scope=True)
        pending.remove(request)
        device_keys = self.data.setdefault("device_keys", {})
        device_keys[request["origin"] + "\n" + request["product_id"]] = request[
            "private_key"
        ]
        unscoped = request["origin"] + "\n"
        if device_keys.get(unscoped) == request["private_key"] and not any(
            item.get("origin") == request["origin"]
            and item.get("requested_product") is None
            and item.get("private_key") == request["private_key"]
            for item in pending
        ):
            del device_keys[unscoped]
        self.persist()
        return {
            "ok": True,
            "grant": _safe_grant(grant),
            "already_authorized": request.get("already_authorized", False),
        }

    def pipeline_login(
        self,
        origin,
        client_name,
        *,
        kind,
        shop=None,
        products=(),
        permissions=None,
        authorization=None,
        reason="",
        no_wait=False,
    ):
        from .models import LINK_PERMISSIONS

        products = list(products)
        permissions = list(
            permissions
            if permissions is not None
            else (
                authorization["permissions"] if authorization else LINK_PERMISSIONS[:3]
            )
        )
        if (
            kind not in ("product", "shop.pipeline")
            or (kind == "product" and (len(products) != 1 or shop is not None))
            or (kind == "shop.pipeline" and not shop)
            or len(products) != len(set(products))
            or any(
                not isinstance(item, str) or not item or len(item) > 100
                for item in products
            )
            or not permissions
            or len(permissions) != len(set(permissions))
            or set(permissions) - set(LINK_PERMISSIONS)
            or (
                kind == "shop.pipeline" and set(permissions) - set(LINK_PERMISSIONS[:3])
            )
            or (
                set(permissions) & {"queue.process", "queue.retry"}
                and "queue.view" not in permissions
            )
            or (
                "fulfillment.configure" in permissions
                and "product.edit" not in permissions
            )
            or not isinstance(reason, str)
            or len(reason) > 1000
            or (
                authorization is not None
                and (
                    authorization["origin"] != origin
                    or authorization["kind"] != kind
                    or (shop and authorization["shop_id"] != shop)
                    or set(authorization["permissions"]) - set(permissions)
                    or (products and set(authorization["product_ids"]) - set(products))
                )
            )
        ):
            raise ManageError(
                "Invalid pipeline scope; upgrades must retain existing products and permissions",
                code="invalid_input",
            )
        scope = {
            "kind": kind,
            "shop_id": shop,
            "product_ids": products,
            "permissions": permissions,
            "authorization_id": authorization["id"] if authorization else None,
            "expected_revision": authorization["revision"] if authorization else None,
            "reason": reason,
        }
        return self.device_login(
            origin,
            client_name,
            product=products[0] if kind == "product" else None,
            no_wait=no_wait,
            scope=scope,
            scope_key=authorization["private_key"] if authorization else None,
        )

    def pipeline_authorize(
        self,
        *,
        authorization_id=None,
        grant_id=None,
        origin=None,
        products=(),
        pipelines_all=False,
        permissions=None,
        reason="",
        client_name=None,
        no_wait=False,
    ):
        authorizations = self.data.get("authorizations", [])
        if not isinstance(authorizations, list):
            raise ManageError("Invalid CLI profile", code="invalid_profile")
        if grant_id:
            grants = [
                item
                for item in self.data["grants"]
                if item.get("id") == grant_id
                and (not origin or item["origin"] == origin)
            ]
            if len(grants) != 1:
                raise ManageError(
                    "Select a saved grant and its server origin", code="no_auth"
                )
            grant = grants[0]
            origin = grant["origin"]
            authorization_id = grant.get("authorization_id")
            if not authorization_id:
                if pipelines_all or (
                    products and set(products) != {grant["product_id"]}
                ):
                    raise ManageError(
                        "Legacy grants can request a separate scope for their own product",
                        code="invalid_input",
                    )
                # Never mutate a legacy staff link or its browser permissions.
                return self.pipeline_login(
                    origin,
                    client_name or grant["client_name"],
                    kind="product",
                    products=[grant["product_id"]],
                    permissions=permissions
                    if permissions is not None
                    else grant["permissions"],
                    reason=reason,
                    no_wait=no_wait,
                )
        matches = [
            item
            for item in authorizations
            if item.get("id") == authorization_id
            and (not origin or item.get("origin") == origin)
        ]
        if len(matches) != 1:
            raise ManageError(
                "Select a saved authorization and its server origin", code="no_auth"
            )
        authorization = matches[0]
        if authorization["kind"] == "product":
            if pipelines_all or (
                products and set(products) != set(authorization["product_ids"])
            ):
                raise ManageError(
                    "Single-product scopes cannot add products; use login for the new product",
                    code="invalid_input",
                )
            desired = authorization["product_ids"]
        else:
            if pipelines_all and products:
                raise ManageError(
                    "Use explicit product additions or --pipelines-all",
                    code="invalid_input",
                )
            desired = (
                []
                if pipelines_all
                else list(dict.fromkeys([*authorization["product_ids"], *products]))
            )
        return self.pipeline_login(
            authorization["origin"],
            client_name or authorization["client_name"],
            kind=authorization["kind"],
            shop=authorization["shop_id"]
            if authorization["kind"] == "shop.pipeline"
            else None,
            products=desired,
            permissions=permissions,
            authorization=authorization,
            reason=reason,
            no_wait=no_wait,
        )

    def _validate_scope_claim(self, response, request, fingerprint):
        from .models import LINK_PERMISSIONS

        authorization = _object(response.get("authorization"))
        bindings = _objects(response.get("bindings"))
        scope = request["scope_intent"]
        products = authorization.get("product_ids")
        permissions = authorization.get("permissions")
        if (
            not isinstance(authorization.get("id"), str)
            or not authorization["id"]
            or authorization.get("kind") != scope["kind"]
            or not isinstance(authorization.get("shop_id"), str)
            or not authorization["shop_id"]
            or (scope["shop_id"] and scope["shop_id"] != authorization["shop_id"])
            or authorization.get("fingerprint") != fingerprint
            or type(authorization.get("revision")) is not int
            or authorization["revision"] < 1
            or type(authorization.get("expires")) not in (int, float)
            or not 0 < authorization["expires"] < 10**15
            or not math.isfinite(authorization["expires"])
            or not isinstance(authorization.get("client_name"), str)
            or not isinstance(products, list)
            or not products
            or len(products) > 500
            or any(not isinstance(item, str) or not item for item in products)
            or len(products) != len(set(products))
            or (scope["product_ids"] and set(products) - set(scope["product_ids"]))
            or (scope["kind"] == "product" and products != scope["product_ids"])
            or not isinstance(permissions, list)
            or not permissions
            or any(not isinstance(item, str) for item in permissions)
            or len(permissions) != len(set(permissions))
            or set(permissions) - set(LINK_PERMISSIONS)
            or set(permissions) - set(scope["permissions"])
            or len(bindings) != len(products)
        ):
            raise ManageError(
                "The server returned an unrequested authorization scope",
                code="invalid_response",
            )
        if scope["authorization_id"]:
            existing = next(
                (
                    item
                    for item in self.data.get("authorizations", [])
                    if item.get("id") == scope["authorization_id"]
                    and item.get("origin") == request["origin"]
                ),
                None,
            )
            if (
                existing is None
                or authorization["id"] != existing["id"]
                or authorization["revision"] < scope["expected_revision"]
                or set(existing["product_ids"]) - set(products)
                or set(existing["permissions"]) - set(permissions)
                or authorization["expires"] > existing["expires"]
                or (
                    authorization["revision"] == scope["expected_revision"]
                    and (
                        set(products) != set(existing["product_ids"])
                        or set(permissions) != set(existing["permissions"])
                        or authorization["expires"] != existing["expires"]
                    )
                )
            ):
                raise ManageError(
                    "The server changed the existing authorization unexpectedly",
                    code="invalid_response",
                )
        seen = set()
        for binding in bindings:
            if (
                not isinstance(binding.get("device_id"), str)
                or not binding["device_id"]
                or binding.get("product_id") not in products
                or binding["product_id"] in seen
                or binding.get("authorization_id") != authorization["id"]
                or binding.get("shop_id") != authorization["shop_id"]
                or binding.get("fingerprint") != fingerprint
                or not isinstance(binding.get("staff_id"), str)
                or not binding["staff_id"]
                or binding.get("permissions") != permissions
            ):
                raise ManageError(
                    "The server returned an invalid product binding",
                    code="invalid_response",
                )
            existing_grant = next(
                (
                    item
                    for item in self.data["grants"]
                    if item.get("origin") == request["origin"]
                    and item.get("authorization_id") == authorization["id"]
                    and item.get("product_id") == binding["product_id"]
                ),
                None,
            )
            if existing_grant is not None and (
                existing_grant["device_id"] != binding["device_id"]
                or existing_grant.get("link_id") != binding["staff_id"]
            ):
                raise ManageError(
                    "The server replaced an existing task-processing identity",
                    code="invalid_response",
                )
            seen.add(binding["product_id"])

    def _finish_scope_login(self, request, pending):
        claimed = request["scope_claim"]
        authorization = {
            **claimed["authorization"],
            "origin": request["origin"],
            "private_key": request["private_key"],
        }
        authorizations = self.data.setdefault("authorizations", [])
        if not isinstance(authorizations, list):
            raise ManageError("Invalid CLI profile", code="invalid_profile")
        authorizations[:] = [
            item
            for item in authorizations
            if not (
                item.get("id") == authorization["id"]
                and item.get("origin") == authorization["origin"]
            )
        ]
        authorizations.append(authorization)
        grants = []
        for binding in claimed["bindings"]:
            grant = next(
                (
                    item
                    for item in self.data["grants"]
                    if item.get("origin") == request["origin"]
                    and item.get("device_id") == binding["device_id"]
                ),
                None,
            )
            if grant is None:
                grant = {}
                self.data["grants"].append(grant)
            changed_scope = (
                grant.get("authorization_revision") != authorization["revision"]
                or grant.get("permissions") != binding["permissions"]
            )
            grant.update(
                {
                    "id": binding["device_id"],
                    "device_id": binding["device_id"],
                    "origin": request["origin"],
                    "product_id": binding["product_id"],
                    "shop_id": authorization["shop_id"],
                    "authorization_id": authorization["id"],
                    "authorization_revision": authorization["revision"],
                    "private_key": request["private_key"],
                    "client_name": authorization["client_name"],
                    "link_id": binding["staff_id"],
                    "permissions": binding["permissions"],
                }
            )
            # A scope revision invalidates existing bearer sessions. Resume with
            # the stable device key after saving every binding atomically.
            if changed_scope:
                grant.pop("access_token", None)
                grant.pop("expires", None)
            grants.append(grant)
        self.persist()
        for grant in grants:
            self.session(grant, refresh_scope=True)
            self.persist()
        pending.remove(request)
        cached_key = self.data.get("scoped_device_keys", {}).get(
            request["scope_key_selector"]
        )
        if isinstance(cached_key, dict):
            cached_key["authorization_id"] = authorization["id"]
        self.persist()
        result = {
            "ok": True,
            "authorization": {
                key: authorization[key]
                for key in (
                    "id",
                    "origin",
                    "shop_id",
                    "kind",
                    "permissions",
                    "product_ids",
                    "expires",
                    "revision",
                    "client_name",
                    "fingerprint",
                )
                if key in authorization
            },
            "grants": [_safe_grant(grant) for grant in grants],
        }
        if len(grants) == 1:
            result["grant"] = result["grants"][0]
        return result

    def grants(self, *, product=None, origin=None, grant_id=None, permissions=()):
        matches = []
        errors = []
        for grant in self.data["grants"]:
            if (
                (product and grant.get("product_id") != product)
                or (origin and grant.get("origin") != origin)
                or (grant_id and grant.get("id") != grant_id)
            ):
                continue
            if not grant.get("device_id"):
                errors.append(
                    {
                        "grant_id": grant.get("id"),
                        "ok": False,
                        "error": "Device binding was interrupted; retry login with the original link",
                        "code": "pending_binding",
                    }
                )
                continue
            try:
                self.session(grant, refresh_scope=True)
            except ManageError as exc:
                errors.append(
                    {
                        "grant_id": grant.get("id"),
                        "product_id": grant.get("product_id"),
                        **exc.as_dict(),
                    }
                )
                continue
            if set(permissions) <= set(grant.get("permissions", [])):
                matches.append(grant)
        return matches, errors

    def grant(self, *, product, origin=None, grant_id=None, permissions=()):
        grants, errors = self.grants(
            product=product, origin=origin, grant_id=grant_id, permissions=permissions
        )
        if not grants:
            if errors:
                raise ManageError(
                    errors[0]["error"],
                    code=errors[0]["code"],
                    status=errors[0].get("status"),
                )
            raise ManageError(
                "No single grant authorizes this product and operation", code="no_scope"
            )
        origins = {item["origin"] for item in grants}
        if len(origins) > 1:
            raise ManageError(
                "This product exists on multiple servers; specify --origin",
                code="ambiguous_scope",
            )
        # One complete grant must cover the request; scopes are never unioned.
        return grants[0]

    def request(self, grant, method, path, **kwargs):
        self.session(grant)
        try:
            return self._json(grant["origin"], method, path, grant=grant, **kwargs)
        except ManageError as exc:
            if exc.status != 401:
                raise
            self._renew(grant)
            return self._json(grant["origin"], method, path, grant=grant, **kwargs)

    def products(self, *, origin=None, grant_id=None, detail=False):
        grants, errors = self.grants(origin=origin, grant_id=grant_id)
        products = {}
        for grant in grants:
            try:
                result = self.request(
                    grant,
                    "GET",
                    "/api/manage/products",
                    params={"compact": "false" if detail else "true"},
                )
                for product in _objects(result):
                    if product.get("id") != grant["product_id"]:
                        raise ManageError(
                            "The server returned a different product scope",
                            code="invalid_response",
                        )
                    if not detail:
                        product = {
                            **{
                                key: product[key]
                                for key in (
                                    "id",
                                    "name",
                                    "mode",
                                    "delivery",
                                    "view_policy",
                                )
                                if key in product
                            },
                            "parameters_count": len(product.get("parameters", []))
                            if "parameters" in product
                            else product.get("parameters_count", 0),
                            "outputs_count": len(product.get("outputs", []))
                            if "outputs" in product
                            else product.get("outputs_count", 0),
                            "variants": [
                                {
                                    key: variant[key]
                                    for key in (
                                        "id",
                                        "name",
                                        "price",
                                        "currency",
                                        "enabled",
                                    )
                                    if key in variant
                                }
                                for variant in product.get("variants", [])
                            ],
                        }
                    products[(grant["origin"], product["id"])] = {
                        **product,
                        "origin": grant["origin"],
                        "grant_id": grant["id"],
                        "permissions": grant["permissions"],
                    }
            except ManageError as exc:
                errors.append({"grant_id": grant["id"], **exc.as_dict()})
        return {"ok": not errors, "products": list(products.values()), "errors": errors}

    def queues(
        self,
        *,
        product=None,
        origin=None,
        grant_id=None,
        view="active",
        state="",
        limit=100,
    ):
        grants, errors = self.grants(
            product=product,
            origin=origin,
            grant_id=grant_id,
            permissions=("queue.view",),
        )
        if not grants and not errors:
            raise ManageError(
                "No product queue authorization; run extore manage login --device-code --origin https://your-server",
                code="no_scope",
            )
        queues = []
        seen = set()
        for grant in grants:
            key = (grant["origin"], grant["product_id"])
            if key in seen:
                continue
            try:
                jobs = self.jobs(
                    grant, view=view, state=state, limit=limit, compact=True
                )
            except ManageError as exc:
                errors.append(
                    {
                        "grant_id": grant["id"],
                        "product_id": grant["product_id"],
                        **exc.as_dict(),
                    }
                )
                continue
            seen.add(key)
            queues.append(
                {
                    "origin": grant["origin"],
                    "product_id": grant["product_id"],
                    "grant_id": grant["id"],
                    "jobs": [compact_job(item) for item in jobs],
                }
            )
        return {"ok": not errors, "view": view, "queues": queues, "errors": errors}

    def jobs(
        self, grant, *, view="active", state="", limit=100, job_id="", compact=False
    ):
        params = {"product_id": grant["product_id"], "view": view, "limit": limit}
        if compact:
            params["compact"] = "true"
        if state:
            params["state"] = state
        if job_id:
            params["job_id"] = job_id
        result = _objects(self.request(grant, "GET", "/api/manage/jobs", params=params))
        if any(
            not isinstance(item.get("id"), str)
            or item.get("product_id") != grant["product_id"]
            for item in result
        ):
            raise ManageError(
                "The server returned a different product scope", code="invalid_response"
            )
        return result

    def job(self, grant, job_id):
        result = self.jobs(grant, job_id=job_id, limit=1)
        if (
            len(result) != 1
            or result[0].get("id") != job_id
            or result[0].get("product_id") != grant["product_id"]
        ):
            raise ManageError(
                "Task not found in the selected product", code="not_found", status=404
            )
        return result[0]

    def batch(self, grant, ids, action, **values):
        return self.request(
            grant,
            "POST",
            "/api/manage/batch",
            json={
                "product_id": grant["product_id"],
                "ids": ids,
                "action": action,
                **values,
            },
        )

    def files(self, grant, job_id):
        self.job(grant, job_id)
        result = _objects(
            self.request(grant, "GET", "/api/manage/files", params={"job_id": job_id})
        )
        if any(
            not isinstance(item.get("id"), str) or item.get("job_id") != job_id
            for item in result
        ):
            raise ManageError(
                "The server returned files from a different task",
                code="invalid_response",
            )
        return result

    def upload(self, grant, job_id, field, source):
        self.job(grant, job_id)
        limits = _object(self._json(grant["origin"], "GET", "/api/upload-limits"))
        limit = limits.get("max_file_bytes")
        if type(limit) is not int or limit < 1:
            raise ManageError(
                "The server returned invalid upload limits", code="invalid_response"
            )
        limit = min(limit, MAX_UPLOAD_BYTES)
        if not source.is_file() or source.stat().st_size > limit:
            raise ManageError(
                f"Upload must be a regular file no larger than {limit} bytes",
                code="invalid_upload",
            )
        with source.open("rb") as upload:
            return self.request(
                grant,
                "POST",
                "/api/manage/files/upload",
                data={"job_id": job_id, "field_key": field},
                files={"file": (source.name, upload, "application/octet-stream")},
            )

    def download(self, grant, job_id, file_id, target):
        files = self.files(grant, job_id)
        descriptor = next(
            (
                item
                for item in files
                if item.get("id") == file_id and item.get("job_id") == job_id
            ),
            None,
        )
        if descriptor is None:
            raise ManageError(
                "File not found in the selected task", code="not_found", status=404
            )
        if descriptor.get("size", 0) > MAX_DOWNLOAD_BYTES:
            raise ManageError(
                "File exceeds the CLI download limit", code="invalid_download"
            )
        if target.exists() or target.is_symlink():
            raise ManageError(
                "Output already exists; choose another path", code="output_exists"
            )
        self.session(grant)
        response = self._request(
            grant["origin"],
            "GET",
            "/api/manage/files/" + quote(file_id, safe="") + "/download",
            grant=grant,
            stream=True,
        )
        try:
            fd = os.open(
                target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            size = 0
            try:
                with os.fdopen(fd, "wb") as destination:
                    for chunk in response.iter_bytes(64 * 1024):
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise ManageError(
                                "File exceeds the CLI download limit",
                                code="invalid_download",
                            )
                        destination.write(chunk)
                    if size != descriptor.get("size"):
                        raise ManageError(
                            "The download did not match its file size",
                            code="invalid_download",
                        )
                    destination.flush()
                    os.fsync(destination.fileno())
            except httpx.HTTPError:
                target.unlink(missing_ok=True)
                raise ManageError(
                    "The download was interrupted", code="connection_error"
                ) from None
            except BaseException:
                target.unlink(missing_ok=True)
                raise
        finally:
            response.close()
        return {"ok": True, "file_id": file_id, "output": str(target), "size": size}

    def logout(self, grants):
        removed = []
        errors = []
        for grant in grants:
            try:
                if grant.get("device_id"):
                    self.session(grant)
                    self.request(grant, "DELETE", "/api/cli/session")
            except ManageError as exc:
                errors.append({"grant_id": grant["id"], **exc.as_dict()})
            self.data["grants"].remove(grant)
            removed.append(grant["id"])
        return {"ok": not errors, "removed": removed, "errors": errors}


def compact_job(job):
    result = {
        key: job[key]
        for key in (
            "id",
            "product_id",
            "product_name",
            "variant",
            "state",
            "progress",
            "attempt",
            "message",
            "created",
            "updated",
            "claimed_by",
            "steps",
            "completed_steps",
            "queue_position",
            "attachments",
        )
        if key in job
    }
    if isinstance(result.get("variant"), dict):
        result["variant"] = {
            key: result["variant"][key]
            for key in ("id", "name")
            if key in result["variant"]
        }
    result["steps_total"] = len(result.pop("steps", []))
    result["steps_done"] = len(result.pop("completed_steps", []))
    if "files" not in job:
        return result
    result["files"] = [
        {
            key: item[key]
            for key in ("id", "field_key", "kind", "filename", "size")
            if key in item
        }
        for item in job.get("files", [])
    ]
    return result


def _number(value):
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer from 0 to 99") from None
    if not 0 <= parsed <= 99:
        raise argparse.ArgumentTypeError("must be an integer from 0 to 99")
    return parsed


def _limit(value):
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 500") from None
    if not 1 <= parsed <= 500:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 500")
    return parsed


def _permissions_csv(value):
    from .models import LINK_PERMISSIONS

    requested = [item.strip() for item in value.split(",")]
    if not requested or any(item not in LINK_PERMISSIONS for item in requested):
        raise argparse.ArgumentTypeError(
            "permissions must be a comma-separated list of product permission codes"
        )
    if len(requested) != len(set(requested)):
        raise argparse.ArgumentTypeError("permission codes cannot be repeated")
    return [item for item in LINK_PERMISSIONS if item in requested]


def add_parser(commands):
    manage = commands.add_parser(
        "manage",
        help="manage remote product queues with device-bound CLI authorization",
    )
    manage.add_argument(
        "--profile",
        type=Path,
        help="private CLI profile file (default: XDG_CONFIG_HOME/extore/cli.json)",
    )
    subcommands = manage.add_subparsers(dest="manage_command", required=True)
    login = subcommands.add_parser(
        "login",
        help="authorize this CLI device in a browser using a device code, or privately enter a management link",
    )
    methods = login.add_mutually_exclusive_group()
    methods.add_argument(
        "--link-stdin",
        action="store_true",
        help="read the complete private management link from standard input",
    )
    methods.add_argument(
        "--device-code",
        action="store_true",
        help="show a public browser URL and device code; no management link is sent by the CLI",
    )
    login.add_argument("--origin", help="HTTPS server origin for device-code login")
    login.add_argument("--product", help="limit browser approval to this product ID")
    login.add_argument(
        "--shop", help="request a snapshot of one shop's current pipeline products"
    )
    login.add_argument(
        "--pipelines-all",
        action="store_true",
        help="request the shop's current queue products; future products require another approval",
    )
    login.add_argument(
        "--existing-link",
        action="store_true",
        help="compatibility: bind an existing product management link in the browser",
    )
    login.add_argument(
        "--permissions",
        type=_permissions_csv,
        help="exact comma-separated requested permissions (default: queue.view,queue.process,queue.retry)",
    )
    login.add_argument(
        "--reason", default="", help="why this device needs the requested scope"
    )
    login.add_argument(
        "--no-wait",
        action="store_true",
        help="return the public code immediately; repeat without this option after browser approval",
    )
    login.add_argument(
        "--client-name",
        default="Extore CLI · " + socket.gethostname(),
        help="device name retained in the session audit",
    )
    authorize = subcommands.add_parser(
        "authorize",
        help="ask the shop owner to approve additions or changes to a saved scope",
    )
    scope = authorize.add_mutually_exclusive_group(required=True)
    scope.add_argument("--authorization", help="saved pipeline authorization ID")
    scope.add_argument(
        "--grant", help="saved device grant; legacy grants create a separate scope"
    )
    authorize.add_argument("--origin", help="select the saved server origin")
    authorize.add_argument(
        "--product",
        action="append",
        default=[],
        help="product to add; repeat to request several",
    )
    authorize.add_argument(
        "--pipelines-all",
        action="store_true",
        help="request the shop's current pipeline snapshot again",
    )
    authorize.add_argument(
        "--permissions",
        type=_permissions_csv,
        help="complete desired permission set; omit to keep current permissions",
    )
    authorize.add_argument(
        "--reason", default="", help="why added products or permissions are needed"
    )
    authorize.add_argument(
        "--client-name", help="audited device name; defaults to the saved name"
    )
    authorize.add_argument(
        "--no-wait",
        action="store_true",
        help="return a public device code; repeat without this flag after approval",
    )
    products = subcommands.add_parser(
        "products", help="list products authorized by all saved grants"
    )
    products.add_argument(
        "--detail",
        action="store_true",
        help="include current input/output schemas and tutorials",
    )
    queues = subcommands.add_parser(
        "queues", help="compact pending queues across all authorized products"
    )
    queues.add_argument(
        "--all",
        action="store_true",
        help="aggregate all authorized product queues (the default)",
    )
    queues.add_argument("--product", help="limit results to a product")
    for command in (products, queues):
        command.add_argument("--origin", help="select a saved server origin")
        command.add_argument("--grant", help="select an individual saved device grant")
    for name, description in (
        ("jobs", "list compact jobs for one product"),
        ("job", "fetch one task's complete inputs and output schema"),
        ("claim", "claim one or more tasks"),
        ("progress", "update a claimed task"),
        ("complete", "deliver a claimed task"),
        ("fail", "mark a processing failure"),
        ("retry", "allow customer retry after a failure"),
        ("request-retry", "return a claimed task for retry with a reason"),
        (
            "request-changes",
            "compatibility alias for request-retry with revised inputs",
        ),
        ("reject", "reject a claimed task with a reason"),
        ("files", "list task file metadata"),
        ("download", "download a task file to a new private local file"),
        ("upload", "upload a delivery file for a claimed task"),
    ):
        command = subcommands.add_parser(
            name, aliases=["succeed"] if name == "complete" else [], help=description
        )
        command.add_argument(
            "--product", required=True, help="explicit product ID for this operation"
        )
        command.add_argument("--origin", help="select a saved server origin")
        command.add_argument("--grant", help="select an individual saved device grant")
        if name != "jobs":
            command.add_argument("job_id", nargs="+" if name == "claim" else None)
        if name in ("progress", "complete", "fail"):
            command.add_argument("--message", default="")
        if name in ("claim", "progress"):
            command.add_argument("--progress", type=_number, default=0)
            command.add_argument(
                "--steps-file",
                type=Path,
                help="JSON processing-step plan for a task without an existing plan",
            )
        if name in ("claim", "progress", "complete"):
            command.add_argument(
                "--completed-step",
                action="append",
                dest="completed_steps",
                help="completed step ID; repeat for each completed step",
            )
        if name in ("request-retry", "request-changes", "reject"):
            command.add_argument(
                "--reason", required=True, help="mandatory customer-visible reason"
            )
        if name in ("request-retry", "request-changes"):
            command.add_argument(
                "--reason-type",
                choices=("customer_input", "external", "processor"),
                default="customer_input",
            )
            command.add_argument(
                "--retry-mode", choices=("revise", "reuse"), default="revise"
            )
        if name == "complete":
            command.add_argument(
                "--output-file",
                type=Path,
                help="JSON object matching the product's output schema",
            )
            command.add_argument(
                "--content-file", type=Path, help="UTF-8 legacy delivery content"
            )
        if name == "fail":
            command.add_argument("--retryable", action="store_true")
        if name == "download":
            command.add_argument("--file-id", required=True)
            command.add_argument("--output", type=Path, required=True)
        if name == "upload":
            command.add_argument(
                "--field", required=True, help="product output field key"
            )
            command.add_argument("--file", type=Path, required=True)
        if name == "jobs":
            _queue_arguments(command)
    _queue_arguments(queues)
    logout = subcommands.add_parser(
        "logout",
        help="revoke the CLI session and erase the selected local device credentials",
    )
    logout.add_argument("--product")
    logout.add_argument("--origin")
    logout.add_argument("--grant")
    logout.add_argument(
        "--all", action="store_true", help="remove all saved CLI devices"
    )
    from .manage_commands import add_commands

    add_commands(subcommands)
    return manage


def _queue_arguments(parser):
    parser.add_argument(
        "--view", choices=("active", "processed", "all"), default="active"
    )
    parser.add_argument(
        "--state", default="", help="explicit state filter overrides the queue view"
    )
    parser.add_argument(
        "--limit", type=_limit, default=100, help="maximum tasks per product"
    )


def _read_text(path, limit):
    if not path.is_file() or path.stat().st_size > limit:
        raise ManageError("Input file is missing or too large", code="invalid_input")
    with path.open("r", encoding="utf-8") as source:
        return source.read(limit + 1)


def execute(args, *, transport=None):
    command = "complete" if args.manage_command == "succeed" else args.manage_command
    path = profile_path(args.profile)
    origin = origin_from_url(args.origin) if getattr(args, "origin", None) else None
    error = None
    result = None
    # Network failure after binding must still commit a newly generated device
    # key. Catch inside the context; normal errors retain private profile state.
    with private_profile(path, create=command == "login") as data:
        with ManageClient(
            data, transport=transport, persist=lambda: _save_profile(path, data)
        ) as client:
            try:
                result = dispatch(client, args, command, origin)
            except ManageError as exc:
                error = exc
    if error:
        raise error
    return result


def dispatch(client, args, command, origin):
    from .manage_commands import COMMANDS
    from .manage_commands import dispatch as dispatch_management

    if command in COMMANDS:
        return dispatch_management(client, args, origin)
    grant_id = getattr(args, "grant", None)
    if command == "authorize":
        return client.pipeline_authorize(
            authorization_id=args.authorization,
            grant_id=args.grant,
            origin=origin,
            products=args.product,
            pipelines_all=args.pipelines_all,
            permissions=args.permissions,
            reason=args.reason,
            client_name=args.client_name,
            no_wait=args.no_wait,
        )
    if command == "login":
        if args.device_code:
            if not origin:
                raise ManageError(
                    "Device-code login requires --origin https://your-extore-server",
                    code="invalid_input",
                )
            if args.existing_link:
                if (
                    args.shop
                    or args.pipelines_all
                    or args.permissions is not None
                    or args.reason
                ):
                    raise ManageError(
                        "Existing-link login uses the link's original scope",
                        code="invalid_input",
                    )
            elif args.product or args.shop or args.pipelines_all:
                if args.product and (args.shop or args.pipelines_all):
                    raise ManageError(
                        "Select a product or one shop's pipeline snapshot",
                        code="invalid_input",
                    )
                if bool(args.shop) != args.pipelines_all:
                    raise ManageError(
                        "Shop login requires --shop and --pipelines-all together",
                        code="invalid_input",
                    )
                return client.pipeline_login(
                    origin,
                    args.client_name,
                    kind="product" if args.product else "shop.pipeline",
                    shop=args.shop,
                    products=[args.product] if args.product else [],
                    permissions=args.permissions,
                    reason=args.reason,
                    no_wait=args.no_wait,
                )
            elif args.permissions is not None or args.reason:
                raise ManageError(
                    "Requested permissions require a product or shop pipeline target",
                    code="invalid_input",
                )
            return client.device_login(
                origin, args.client_name, product=args.product, no_wait=args.no_wait
            )
        if (
            args.product
            or args.no_wait
            or origin
            or args.shop
            or args.pipelines_all
            or args.existing_link
            or args.permissions is not None
            or args.reason
        ):
            raise ManageError(
                "--origin, --product and --no-wait require --device-code",
                code="invalid_input",
            )
        if args.link_stdin:
            invitation = sys.stdin.read(4097).strip()
        else:
            invitation = getpass.getpass("Private Extore management link: ").strip()
        if not invitation or len(invitation) > 4096:
            raise ManageError(
                "Provide one complete management link", code="invalid_link"
            )
        return client.login(invitation, args.client_name)
    if command == "products":
        return client.products(origin=origin, grant_id=grant_id, detail=args.detail)
    if command == "queues":
        return client.queues(
            product=args.product,
            origin=origin,
            grant_id=grant_id,
            view=args.view,
            state=args.state,
            limit=args.limit,
        )
    if command == "logout":
        if not args.all and not any((args.product, origin, grant_id)):
            raise ManageError(
                "Select --product, --origin, --grant, or explicitly --all",
                code="invalid_input",
            )
        grants = [
            item
            for item in client.data["grants"]
            if (not args.product or item.get("product_id") == args.product)
            and (not origin or item.get("origin") == origin)
            and (not grant_id or item.get("id") == grant_id)
        ]
        result = client.logout(grants)
        pending = client.data.get("device_requests", [])
        retired_authorizations = {
            item["authorization_id"] for item in grants if item.get("authorization_id")
        }

        def selected_scope_request(item):
            scope = item.get("scope_intent")
            if not isinstance(scope, dict):
                return False
            authorization_id = scope.get("authorization_id") or item.get(
                "scope_claim", {}
            ).get("authorization", {}).get("id")
            selected_grant = authorization_id in retired_authorizations
            return (
                (not origin or item.get("origin") == origin)
                and (not grant_id or selected_grant)
                and (
                    not args.product
                    or args.product in scope.get("product_ids", [])
                    or selected_grant
                )
            )

        removed_pending = [
            item
            for item in pending
            if selected_scope_request(item)
            or (
                (
                    not args.product
                    or item.get("product_id", item.get("requested_product"))
                    == args.product
                )
                and (not origin or item.get("origin") == origin)
                and (not grant_id or item.get("device_id") == grant_id)
            )
        ]
        client.data["device_requests"] = [
            item for item in pending if item not in removed_pending
        ]
        device_keys = client.data.get("device_keys", {})
        retired_keys = {
            item["private_key"]
            for item in [*grants, *removed_pending]
            if "private_key" in item
        }
        retired_scopes = {
            (
                item["origin"],
                item.get("product_id", item.get("requested_product")) or "",
            )
            for item in [*grants, *removed_pending]
        }
        for scope, private_key in list(device_keys.items()):
            key_origin, _, key_product = scope.partition("\n")
            in_use = any(
                item.get("origin") == key_origin
                and item.get("private_key") == private_key
                and (
                    not key_product
                    or item.get("product_id", item.get("requested_product"))
                    == key_product
                )
                for item in [*client.data["grants"], *client.data["device_requests"]]
            )
            selected = (
                (
                    not grant_id
                    and (not origin or key_origin == origin)
                    and (not args.product or key_product == args.product)
                )
                or (key_origin, key_product) in retired_scopes
                or (not key_product and private_key in retired_keys)
            )
            if not in_use and selected:
                del device_keys[scope]
        authorizations = client.data.get("authorizations", [])
        retired_authorizations.update(
            item.get("scope_intent", {}).get("authorization_id")
            or item.get("scope_claim", {}).get("authorization", {}).get("id")
            for item in removed_pending
            if item.get("scope_intent")
        )
        retired_authorizations.discard(None)
        keep = []
        for authorization in authorizations:
            authorization_id = authorization["id"]
            selected = authorization_id in retired_authorizations or (
                not grant_id
                and (not origin or authorization["origin"] == origin)
                and (not args.product or args.product in authorization["product_ids"])
            )
            in_use = any(
                item.get("origin") == authorization["origin"]
                and (
                    item.get("authorization_id") == authorization_id
                    or item.get("scope_intent", {}).get("authorization_id")
                    == authorization_id
                    or item.get("scope_claim", {}).get("authorization", {}).get("id")
                    == authorization_id
                )
                for item in [*client.data["grants"], *client.data["device_requests"]]
            )
            if not selected or in_use:
                keep.append(authorization)
        if "authorizations" in client.data:
            client.data["authorizations"] = keep
        protected_keys = {
            item["private_key"]
            for item in [*client.data["grants"], *client.data["device_requests"], *keep]
            if item.get("private_key")
        }
        for selector, cached in list(client.data.get("scoped_device_keys", {}).items()):
            if not isinstance(cached, dict):
                continue
            scope = cached["scope_intent"]
            selected = (
                cached.get("authorization_id") in retired_authorizations
                or (
                    not grant_id
                    and (not origin or cached["origin"] == origin)
                    and (not args.product or args.product in scope["product_ids"])
                )
                or cached["private_key"] in retired_keys
            )
            if selected and cached["private_key"] not in protected_keys:
                del client.data["scoped_device_keys"][selector]
        return result
    permission = (
        "queue.view"
        if command in ("jobs", "job", "files", "download")
        else "queue.retry"
        if command == "retry"
        else "queue.process"
    )
    grant = client.grant(
        product=args.product,
        origin=origin,
        grant_id=grant_id,
        permissions=(permission,),
    )
    if command == "jobs":
        jobs = client.jobs(
            grant, view=args.view, state=args.state, limit=args.limit, compact=True
        )
        return {
            "ok": True,
            "product_id": args.product,
            "view": args.view,
            "jobs": [compact_job(item) for item in jobs],
        }
    if command == "job":
        return {"ok": True, "job": client.job(grant, args.job_id)}
    if command == "files":
        return {"ok": True, "files": client.files(grant, args.job_id)}
    if command == "download":
        return client.download(grant, args.job_id, args.file_id, args.output)
    if command == "upload":
        return {
            "ok": True,
            "file": client.upload(grant, args.job_id, args.field, args.file),
        }
    ids = args.job_id if command == "claim" else [args.job_id]
    values = {}
    if command in ("claim", "progress"):
        values["progress"] = args.progress
        if args.steps_file:
            try:
                steps = json.loads(_read_text(args.steps_file, MAX_OUTPUT_BYTES))
            except ValueError:
                raise ManageError(
                    "Steps file must contain a JSON array", code="invalid_input"
                ) from None
            if (
                not isinstance(steps, list)
                or not 1 <= len(steps) <= 30
                or not all(isinstance(step, dict) for step in steps)
            ):
                raise ManageError(
                    "Steps file must contain 1 to 30 step objects", code="invalid_input"
                )
            values["progress_steps"] = steps
    if command in ("progress", "complete", "fail"):
        values["message"] = args.message
    if (
        command in ("claim", "progress", "complete")
        and args.completed_steps is not None
    ):
        values["completed_steps"] = args.completed_steps
    if command in ("request-retry", "request-changes", "reject"):
        if not args.reason.strip() or len(args.reason) > 1000:
            raise ManageError(
                "A non-empty customer-visible reason of at most 1000 characters is required",
                code="invalid_input",
            )
        values["message"] = args.reason
        if command != "reject":
            values["retry_mode"] = args.retry_mode
            values["reason_type"] = args.reason_type
    if command == "complete":
        if args.output_file:
            try:
                output = json.loads(_read_text(args.output_file, MAX_OUTPUT_BYTES))
            except ValueError:
                raise ManageError(
                    "Output file must contain a JSON object", code="invalid_input"
                ) from None
            if not isinstance(output, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in output.items()
            ):
                raise ManageError(
                    "Output file must contain an object with string values",
                    code="invalid_input",
                )
            values["output"] = output
        if args.content_file:
            content = _read_text(args.content_file, 400000)
            if len(content) > 100000:
                raise ManageError(
                    "Delivery content must contain at most 100000 characters",
                    code="invalid_input",
                )
            values["content"] = content
    if command == "fail":
        values["retryable"] = args.retryable
    action = {
        "complete": "succeed",
        "request-changes": "request_changes",
        "request-retry": "request_retry",
    }.get(command, command)
    return client.batch(grant, ids, action, **values)


def main(args):
    try:
        result = execute(args)
    except ManageError as exc:
        print(
            json.dumps(exc.as_dict(), ensure_ascii=False, separators=(",", ":")),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "登录已停止；重复相同的设备码登录命令可继续",
                    "code": "interrupted",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        raise SystemExit(130) from None
    except (OSError, ValueError, KeyError, TypeError):
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "Cannot read or update private CLI credentials or command input",
                    "code": "local_error",
                },
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    if result.get("ok") is False:
        raise SystemExit(1)

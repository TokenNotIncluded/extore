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
import secrets
import socket
import stat
import sys
import time
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
            "No CLI authorization; run extore manage login --link-stdin", code="no_auth"
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
            "No CLI authorization; run extore manage login --link-stdin", code="no_auth"
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
                "No product queue authorization; run extore manage login --link-stdin",
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
        help="bind this CLI device using a management link; does not consume browser authorization",
    )
    login.add_argument(
        "--link-stdin",
        action="store_true",
        help="read the complete private management link from standard input",
    )
    login.add_argument(
        "--client-name",
        default="Extore CLI · " + socket.gethostname(),
        help="device name retained in the session audit",
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
    if command == "login":
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
        return client.logout(grants)
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

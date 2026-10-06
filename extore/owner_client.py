"""Remote merchant commands with Passkey-approved, device-bound authorization.

The browser approves the first binding. Normal business operations then use the
CLI key; a stolen short-lived read token cannot authorize a JSON mutation.
"""

import argparse
import getpass
import hashlib
import json
import math
import os
import re
import secrets
import socket
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote, urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from . import manage_client as remote
from . import manage_commands as business
from . import shop_commands
from .manage_client import (
    ManageClient,
    ManageError,
    _b64,
    _object,
    _objects,
    _save_profile,
    _unb64,
    origin_from_url,
    private_profile,
)

OWNER_SCOPE = "shop.owner"
OWNER_PREFIX = "/api/cli/owner"
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def profile_path(value=None):
    configured = value or os.environ.get("EXTORE_OWNER_CONFIG")
    if configured:
        return Path(configured).expanduser().absolute()
    root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return (root / "extore" / "owner.json").absolute()


def _no_symlinks(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ManageError(
                "Owner credential paths must not contain symlinks",
                code="unsafe_profile",
            )


def _safe_owner(owner):
    return {
        key: owner[key]
        for key in (
            "id",
            "origin",
            "device_id",
            "client_name",
            "fingerprint",
            "device_expires",
            "expires",
            "role",
            "scope",
            "shop_id",
            "shop_name",
            "superadmin",
        )
        if key in owner
    }


def _sign(owner, *parts):
    try:
        key = Ed25519PrivateKey.from_private_bytes(_unb64(owner["private_key"]))
        return _b64(key.sign("\n".join(parts).encode()))
    except (ValueError, KeyError, TypeError):
        raise ManageError("Invalid owner device key", code="invalid_profile") from None


def _authority(owner, response, *, binding=False, merchant=False):
    """Freeze the tenant identity; two admin roles never imply the same authority."""
    if (
        response.get("role") != "admin"
        or response.get("scope") != OWNER_SCOPE
        or "shop_id" not in response
        or type(response.get("superadmin")) is not bool
        or response["superadmin"] != (response["shop_id"] is None)
        or response["shop_id"] is not None
        and (not isinstance(response["shop_id"], str) or not response["shop_id"])
        or merchant
        and response["shop_id"] is None
    ):
        raise ManageError("Invalid owner tenant identity", code="invalid_response")
    if "shop_id" in owner:
        if response["shop_id"] != owner["shop_id"]:
            raise ManageError(
                "The server changed the authorized shop", code="invalid_response"
            )
    elif not binding and response["shop_id"] is not None:
        # Profiles created before multiple shops existed can only migrate to
        # their original platform-root identity, never to a merchant account.
        raise ManageError(
            "Legacy owner profile returned a different shop", code="invalid_response"
        )
    owner.update({key: response[key] for key in ("shop_id", "superadmin")})
    if isinstance(response.get("shop_name"), str):
        owner["shop_name"] = response["shop_name"]


class OwnerClient(ManageClient):
    def _select(self, origin=None, device_id=None, *, pending=False):
        candidates = [
            item
            for item in self.data["owners"]
            if (origin is None or item["origin"] == origin)
            and (device_id is None or item.get("device_id") == device_id)
            and (pending or item.get("device_id"))
        ]
        if not candidates:
            raise ManageError(
                "No owner authorization; run extore admin login --origin SERVER",
                code="no_auth",
            )
        if len(candidates) != 1:
            raise ManageError(
                "Select one owner server with --origin", code="ambiguous_scope"
            )
        return candidates[0]

    def _owner(self, grant):
        return self._select(grant["origin"], grant.get("device_id"))

    def _request(self, origin, method, path, *, grant=None, stream=False, **kwargs):
        # Every HTTP call clears the jar in the base implementation. A response
        # from a WebAuthn endpoint must never become a browser-cookie login.
        if (
            grant is None
            or method.upper() not in WRITE_METHODS
            or path == OWNER_PREFIX + "/action-challenge"
        ):
            return super()._request(
                origin, method, path, grant=grant, stream=stream, **kwargs
            )
        owner = self._owner(grant)
        self.session(owner)
        grant = owner
        if kwargs.get("files") is not None:
            # Uploads are bounded, authenticated draft attachments. The server
            # checks the owner claim and schema before accepting multipart data.
            return super()._request(
                origin, method, path, grant=grant, stream=stream, **kwargs
            )
        headers = dict(kwargs.pop("headers", {}))
        if "json" in kwargs:
            body = json.dumps(
                kwargs.pop("json"),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
            kwargs["content"] = body
            headers["Content-Type"] = "application/json"
        else:
            body = kwargs.get("content", b"")
            if isinstance(body, str):
                body = body.encode()
                kwargs["content"] = body
            if not isinstance(body, bytes):
                raise ManageError(
                    "Owner writes require a bounded JSON body", code="invalid_input"
                )
        if len(body) > business.JSON_LIMIT:
            raise ManageError("JSON input exceeds 256000 bytes", code="invalid_input")
        probe = self.http.build_request(method, origin + path, **kwargs)
        action_path = probe.url.raw_path.decode("ascii")
        body_hash = hashlib.sha256(body).hexdigest()
        challenge = _object(
            self._json(
                origin,
                "POST",
                OWNER_PREFIX + "/action-challenge",
                grant=owner,
                json={
                    "method": method.upper(),
                    "path": action_path,
                    "body_sha256": body_hash,
                },
            )
        )
        if not all(
            isinstance(challenge.get(key), str) and 1 <= len(challenge[key]) <= 256
            for key in ("challenge_id", "challenge")
        ):
            raise ManageError("Invalid owner action challenge", code="invalid_response")
        headers.update(
            {
                "X-Extore-CLI-Challenge": challenge["challenge_id"],
                "X-Extore-CLI-Signature": _sign(
                    owner,
                    "extore-cli-owner-action-v1",
                    origin,
                    owner["device_id"],
                    owner["session_id"],
                    challenge["challenge_id"],
                    challenge["challenge"],
                    method.upper(),
                    action_path,
                    body_hash,
                ),
            }
        )
        # No automatic mutation retry: a server may have committed the first
        # request even when the response was interrupted.
        return super()._request(
            origin, method, path, grant=owner, stream=stream, headers=headers, **kwargs
        )

    def _renew(self, owner):
        origin = owner["origin"]
        challenge = _object(
            self._json(
                origin,
                "POST",
                OWNER_PREFIX + "/challenge",
                json={"device_id": owner["device_id"]},
            )
        )
        if not all(
            isinstance(challenge.get(key), str) and 1 <= len(challenge[key]) <= 256
            for key in ("challenge_id", "challenge")
        ):
            raise ManageError(
                "Invalid owner session challenge", code="invalid_response"
            )
        response = _object(
            self._json(
                origin,
                "POST",
                OWNER_PREFIX + "/session",
                json={
                    "device_id": owner["device_id"],
                    "challenge_id": challenge["challenge_id"],
                    "signature": _sign(
                        owner,
                        "extore-cli-owner-session-v1",
                        origin,
                        owner["device_id"],
                        challenge["challenge_id"],
                        challenge["challenge"],
                    ),
                },
            )
        )
        if (
            response.get("device_id") != owner["device_id"]
            or response.get("role") != "admin"
            or response.get("scope") != OWNER_SCOPE
            or not isinstance(response.get("access_token"), str)
            or not response["access_token"]
            or any(
                ord(char) < 33 or ord(char) > 126 for char in response["access_token"]
            )
            or not isinstance(response.get("session_id"), str)
            or type(response.get("expires")) not in (int, float)
            or not math.isfinite(response["expires"])
        ):
            raise ManageError("Invalid owner session scope", code="invalid_response")
        _authority(owner, response)
        owner.update(
            {
                key: response[key]
                for key in ("access_token", "expires", "session_id", "role", "scope")
            }
        )
        self.persist()

    def session(self, grant, *, refresh_scope=False):
        owner = self._owner(grant) if "product_id" in grant else grant
        if not owner.get("device_id"):
            raise ManageError("Owner approval is pending", code="pending_approval")
        if not owner.get("access_token") or owner.get("expires", 0) < time.time() + 30:
            self._renew(owner)
        if refresh_scope:
            try:
                result = _object(
                    self._json(
                        owner["origin"], "GET", OWNER_PREFIX + "/status", grant=owner
                    )
                )
            except ManageError as exc:
                if exc.status != 401:
                    raise
                self._renew(owner)
                result = _object(
                    self._json(
                        owner["origin"], "GET", OWNER_PREFIX + "/status", grant=owner
                    )
                )
            if (
                result.get("device_id") != owner["device_id"]
                or result.get("role") != "admin"
                or result.get("scope") != OWNER_SCOPE
            ):
                raise ManageError(
                    "Invalid owner authorization scope", code="invalid_response"
                )
            _authority(owner, result)
            self.persist()
        if grant is not owner:
            grant.update(owner)
        return grant

    def request(self, grant, method, path, **kwargs):
        owner = self._owner(grant)
        self.session(owner)
        # A fresh GET is safe to retry after token expiry. Signed writes are
        # intentionally never replayed automatically.
        try:
            return self._json(owner["origin"], method, path, grant=owner, **kwargs)
        except ManageError as exc:
            if exc.status != 401 or method.upper() != "GET":
                raise
            self._renew(owner)
            return self._json(owner["origin"], method, path, grant=owner, **kwargs)

    def grant(self, *, product, origin=None, grant_id=None, permissions=()):
        active = getattr(self, "active_owner", None)
        if grant_id is None and active and origin in (None, active["origin"]):
            grant_id = active.get("device_id")
        owner = self._select(origin, grant_id)
        self.session(owner)
        return {
            **owner,
            "product_id": product,
            "permissions": list(business.PERMISSIONS),
        }

    def login(
        self,
        origin,
        client_name,
        *,
        wait=0,
        open_browser=False,
        email=None,
        credentials=None,
    ):
        name = client_name.strip()
        if not name or len(name) > 100 or any(ord(char) < 32 for char in name):
            raise ManageError(
                "Use a printable device name of at most 100 characters",
                code="invalid_input",
            )
        email = email.strip().casefold() if email else None
        existing = next(
            (
                item
                for item in self.data["owners"]
                if item["origin"] == origin and item.get("login_email") == email
            ),
            None,
        )
        if existing and existing.get("device_id"):
            try:
                self.session(existing, refresh_scope=True)
                return {
                    "ok": True,
                    "status": "authorized",
                    "owner": _safe_owner(existing),
                }
            except ManageError as exc:
                if exc.status != 401:
                    raise
                # The old device can no longer prove its authorization. A
                # new key still requires a fresh registered Passkey approval.
                self.data["owners"].remove(existing)
                existing = None
        owner = existing
        if owner and owner.get("request_id"):
            try:
                status = self.approval_status(owner)
            except ManageError as exc:
                if exc.status != 404:
                    raise
                status = {"status": "expired"}
            if status["status"] == "authorized":
                return status
            if status["status"] in ("expired", "denied"):
                owner["nonce"] = _b64(secrets.token_bytes(32))
                for field in (
                    "request_id",
                    "device_code",
                    "approval_url",
                    "challenge",
                    "fingerprint",
                    "expires",
                ):
                    owner.pop(field, None)
        if owner is None:
            key = Ed25519PrivateKey.generate()
            owner = {
                "id": "pending-" + secrets.token_hex(16),
                "origin": origin,
                "private_key": _b64(
                    key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
                ),
                "public_key": _b64(
                    key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
                ),
                "nonce": _b64(secrets.token_bytes(32)),
                "client_name": name,
            }
            if email:
                owner["login_email"] = email
            self.data["owners"].append(owner)
        # The same nonce and key recover a response lost after request creation.
        self.persist()
        if not owner.get("request_id"):
            request_body = {
                "public_key": owner["public_key"],
                "client_name": owner["client_name"],
                "nonce": owner["nonce"],
            }
            proof = [origin, owner["public_key"], owner["client_name"], owner["nonce"]]
            if email:
                request_body["target_email"] = email
                proof.append(email)
            request_body["signature"] = _sign(
                owner,
                "extore-cli-owner-request-v2"
                if email
                else "extore-cli-owner-request-v1",
                *proof,
            )
            result = _object(
                self._json(
                    origin,
                    "POST",
                    OWNER_PREFIX + "/request",
                    json=request_body,
                )
            )
            if not all(
                isinstance(result.get(key), str) and result[key]
                for key in (
                    "request_id",
                    "device_code",
                    "approval_url",
                    "challenge",
                    "fingerprint",
                )
            ) or not result["approval_url"].startswith(origin + "/cli/owner#"):
                raise ManageError(
                    "Invalid owner approval response", code="invalid_response"
                )
            owner.update(
                {
                    key: result[key]
                    for key in (
                        "request_id",
                        "device_code",
                        "approval_url",
                        "challenge",
                        "fingerprint",
                        "expires",
                    )
                    if key in result
                }
            )
            self.persist()
        if email:
            self.password_approve(owner, credentials)
            return self.approval_status(owner)
        if open_browser:
            webbrowser.open(owner["approval_url"])
        deadline = time.monotonic() + wait
        while True:
            status = self.approval_status(owner)
            if status["status"] != "pending" or time.monotonic() >= deadline:
                return status
            time.sleep(min(5, max(0, deadline - time.monotonic())))

    def password_approve(self, owner, credentials):
        if not isinstance(credentials, dict) or set(credentials) - {
            "password",
            "code",
            "backup_code",
        }:
            raise ManageError(
                "Provide password and optional second-factor credentials privately",
                code="invalid_input",
            )
        if (
            not isinstance(credentials.get("password"), str)
            or not 1 <= len(credentials["password"]) <= 200
        ):
            raise ManageError(
                "Password must be supplied privately", code="invalid_input"
            )
        body = {
            "request_id": owner["request_id"],
            "device_code": owner["device_code"],
            "email": owner["login_email"],
            **credentials,
        }
        options = _object(
            self._json(
                owner["origin"],
                "POST",
                "/api/auth/cli-owner/password-options",
                json=body,
                headers={"Origin": owner["origin"]},
            )
        )
        _authority(owner, options, binding=True, merchant=True)
        proof = options.get("approval_token")
        if not isinstance(proof, str) or not proof:
            raise ManageError(
                "Invalid password approval proof", code="invalid_response"
            )
        self.persist()
        approved = _object(
            self._json(
                owner["origin"],
                "POST",
                "/api/auth/cli-owner/password-approve",
                json={
                    "request_id": owner["request_id"],
                    "device_code": owner["device_code"],
                    "approval_token": proof,
                },
                headers={"Origin": owner["origin"]},
            )
        )
        _authority(owner, approved)
        self.persist()

    def approval_status(self, owner):
        if owner.get("device_id"):
            self.session(owner, refresh_scope=True)
            return {"ok": True, "status": "authorized", "owner": _safe_owner(owner)}
        if not owner.get("request_id"):
            raise ManageError(
                "Retry the initial owner login request", code="pending_approval"
            )
        origin = owner["origin"]
        result = _object(
            self._json(
                origin,
                "POST",
                OWNER_PREFIX + "/status",
                json={
                    "request_id": owner["request_id"],
                    "public_key": owner["public_key"],
                    "signature": _sign(
                        owner,
                        "extore-cli-owner-status-v1",
                        origin,
                        owner["request_id"],
                        owner["public_key"],
                    ),
                },
            )
        )
        state = result.get("status")
        if state == "approved":
            _authority(owner, result, binding=True)
            self.persist()
            claim = _object(
                self._json(
                    origin,
                    "POST",
                    OWNER_PREFIX + "/claim",
                    json={
                        "request_id": owner["request_id"],
                        "public_key": owner["public_key"],
                        "signature": _sign(
                            owner,
                            "extore-cli-owner-claim-v1",
                            origin,
                            owner["request_id"],
                            owner["challenge"],
                            owner["public_key"],
                        ),
                    },
                )
            )
            if (
                not isinstance(claim.get("device_id"), str)
                or claim.get("role") != "admin"
                or claim.get("scope") != OWNER_SCOPE
            ):
                raise ManageError(
                    "Invalid owner device binding", code="invalid_response"
                )
            _authority(owner, claim, binding=True)
            owner.update(
                {
                    "id": claim["device_id"],
                    "device_id": claim["device_id"],
                    "device_expires": claim.get("expires"),
                    "role": "admin",
                    "scope": OWNER_SCOPE,
                }
            )
            self.persist()
            self._renew(owner)
            return {"ok": True, "status": "authorized", "owner": _safe_owner(owner)}
        if state not in ("pending", "denied", "expired"):
            raise ManageError("Invalid owner approval state", code="invalid_response")
        return {
            "ok": state == "pending",
            "status": state,
            **{
                key: owner[key]
                for key in ("approval_url", "device_code", "fingerprint", "expires")
            },
            "next": "Approve this device with your Passkey, then run extore admin login-status",
        }


def _scope(parser, *, product=False):
    parser.add_argument("--origin", help="select one saved owner server")
    parser.add_argument("--grant", help="select one owner device ID")
    if product:
        parser.add_argument("--product", required=True)


def add_parser(commands):
    admin = commands.add_parser(
        "admin", help="all merchant operations using a Passkey-approved CLI device"
    )
    admin.add_argument(
        "--profile",
        type=Path,
        help="private owner profile (separate from product grants)",
    )
    sub = admin.add_subparsers(dest="manage_command", required=True)
    login = sub.add_parser(
        "login",
        help="request initial browser Passkey approval; private key stays local",
    )
    login.add_argument("--origin", required=True)
    login.add_argument(
        "--client-name", default="Extore owner CLI · " + socket.gethostname()
    )
    login.add_argument(
        "--wait", type=int, default=0, help="poll approval for at most 600 seconds"
    )
    login.add_argument("--open-browser", action="store_true")
    login.add_argument(
        "--email",
        help="merchant email; approve this device with private password and optional TOTP",
    )
    credentials = login.add_mutually_exclusive_group()
    credentials.add_argument(
        "--credentials-file",
        type=Path,
        help="mode-600 JSON containing password and optional code/backup_code",
    )
    credentials.add_argument(
        "--credentials-stdin",
        action="store_true",
        help="read password and optional second factor from private JSON stdin",
    )
    login.add_argument(
        "--use-backup-code",
        action="store_true",
        help="ask privately for a recovery code instead of a TOTP",
    )
    for name in ("login-status", "status", "logout", "storage"):
        command = sub.add_parser(name)
        _scope(command)
    logout = sub.choices["logout"]
    logout.add_argument("--all", action="store_true")
    products = sub.add_parser("products", help="compact global product list")
    _scope(products)
    products.add_argument("--detail", action="store_true")
    products.add_argument("--output", type=Path)
    queues = sub.add_parser(
        "queues", help="compact pending queues across merchant products"
    )
    _scope(queues)
    queues.add_argument("--product")
    remote._queue_arguments(queues)
    for name in (
        "jobs",
        "job",
        "claim",
        "progress",
        "complete",
        "fail",
        "retry",
        "request-changes",
        "reject",
        "files",
        "download",
        "upload",
    ):
        parser = sub.add_parser(name, aliases=["succeed"] if name == "complete" else [])
        _scope(parser, product=True)
        if name != "jobs":
            parser.add_argument("job_id", nargs="+" if name == "claim" else None)
        if name in ("progress", "complete", "fail"):
            parser.add_argument("--message", default="")
        if name in ("claim", "progress"):
            parser.add_argument("--progress", type=remote._number, default=0)
            parser.add_argument("--steps-file", type=Path)
        if name in ("claim", "progress", "complete"):
            parser.add_argument(
                "--completed-step", action="append", dest="completed_steps"
            )
        if name in ("request-changes", "reject"):
            parser.add_argument("--reason", required=True)
        if name == "complete":
            parser.add_argument("--output-file", type=Path)
            parser.add_argument("--content-file", type=Path)
        if name == "fail":
            parser.add_argument("--retryable", action="store_true")
        if name == "jobs":
            remote._queue_arguments(parser)
        if name == "download":
            parser.add_argument("--file-id", required=True)
            parser.add_argument("--output", type=Path, required=True)
        if name == "upload":
            parser.add_argument("--field", required=True)
            parser.add_argument("--file", type=Path, required=True)
    business.add_commands(sub)
    for name in ("sessions", "devices", "events"):
        operations = next(
            action
            for action in sub.choices[name]._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        for parser in operations.choices.values():
            for action in parser._actions:
                if action.dest == "product":
                    action.required = False
    for name in ("audit", "processors", "api"):
        for action in sub.choices[name]._actions:
            if action.dest == "product":
                action.required = False
    # Extend the shared product commands with owner-only lifecycle operations.
    product_sub = next(
        action
        for action in sub.choices["product"]._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    for operation in ("list", "create", "templates", "quick"):
        parser = product_sub.add_parser(operation)
        _scope(parser)
        parser.add_argument("--detail", action="store_true")
        parser.add_argument("--output", type=Path)
        if operation in ("create", "quick"):
            business._json_arguments(parser)
    # Owner configuration reads may be exported without redaction to a private
    # file; stdout never prints the connector secrets.
    passkeys = sub.add_parser(
        "passkeys",
        help="list/remove keys or perform a real WebAuthn registration ceremony",
    )
    key_sub = passkeys.add_subparsers(dest="operation", required=True)
    for operation in ("list", "remove", "register-options", "register-verify"):
        parser = key_sub.add_parser(operation)
        _scope(parser)
        parser.add_argument("--output", type=Path)
        if operation == "remove":
            parser.add_argument("id")
        if operation == "register-verify":
            business._json_arguments(parser)
    owners = sub.add_parser(
        "owner-devices", help="audit and revoke whole-shop CLI devices"
    )
    device_sub = owners.add_subparsers(dest="operation", required=True)
    for operation in ("list", "revoke"):
        parser = device_sub.add_parser(operation)
        _scope(parser)
        if operation == "revoke":
            parser.add_argument("id")
    shop_commands.add_commands(sub)
    return admin


def _owner_request(client, owner, method, path, **kwargs):
    return client.request(owner, method, path, **kwargs)


def _global_products(client, owner, *, compact=False):
    return _objects(
        _owner_request(
            client,
            owner,
            "GET",
            "/api/admin/products",
            params={"compact": "true"} if compact else None,
        )
    )


class _BusinessOwner(OwnerClient):
    """Adapt shared product operations to their owner audit endpoints."""

    def request(self, grant, method, path, **kwargs):
        if path == "/api/manage/product" and method == "PUT":
            body = kwargs.get("json", {})
            if (
                body.get("webhook_secret") == "[redacted]"
                or body.get("processor_config") == "[redacted]"
            ):
                raise ManageError(
                    "Export configuration to a private file before editing secret fields",
                    code="invalid_input",
                )
            path = "/api/admin/products/" + quote(grant["product_id"], safe="")
            kwargs.pop("params", None)
        elif path.startswith(
            ("/api/manage/sessions", "/api/manage/audit", "/api/manage/cli-devices")
        ):
            path = path.replace("/api/manage/", "/api/admin/", 1)
        return super().request(grant, method, path, **kwargs)


def _secret_output(args, prefix, callback):
    with business.OutputFile(args, prefix) as output:
        result = callback()
        saved = output.write(result)
        return saved


_API_ROUTES = (
    ("GET", r"/api/admin/maintenance"),
    ("PUT", r"/api/admin/maintenance/policy"),
    ("POST", r"/api/admin/maintenance/cleanup"),
    ("POST", r"/api/manage/links/cleanup"),
    ("GET", r"/api/admin/products"),
    ("POST", r"/api/admin/products(?:/quick)?"),
    ("PUT", r"/api/admin/products/[A-Za-z0-9_-]+"),
    (
        "GET",
        r"/api/admin/(?:product-templates|processors|storage|cards|card-stats|card-inventory|staff|events|sessions|audit|cli-devices)",
    ),
    ("POST", r"/api/admin/(?:cards|staff)"),
    ("POST", r"/api/admin/(?:cards|staff)/[A-Za-z0-9_-]+/revoke"),
    ("GET", r"/api/admin/cards/[A-Za-z0-9_-]+/history"),
    ("POST", r"/api/admin/events/[A-Za-z0-9_-]+/retry"),
    ("DELETE", r"/api/admin/(?:sessions|cli-devices)/[A-Za-z0-9_-]+"),
    (
        "GET",
        r"/api/manage/(?:products|product|processors|jobs|files|cards|card-stats|card-inventory|events|links)",
    ),
    ("POST", r"/api/manage/(?:batch|cards|links)"),
    ("PUT", r"/api/manage/product"),
    ("POST", r"/api/manage/(?:cards|links)/[A-Za-z0-9_-]+/revoke"),
    ("GET", r"/api/manage/cards/[A-Za-z0-9_-]+/history"),
    ("POST", r"/api/manage/events/[A-Za-z0-9_-]+/retry"),
    ("GET", r"/api/auth/passkeys"),
    ("DELETE", r"/api/auth/passkeys/[A-Za-z0-9_-]+"),
    ("POST", r"/api/auth/register/(?:options|verify)"),
    ("GET", r"/api/admin/cli-owner-devices"),
    ("DELETE", r"/api/admin/cli-owner-devices/[A-Za-z0-9_-]+"),
    ("GET", r"/api/platform/(?:settings|shops)"),
    ("PUT", r"/api/platform/settings"),
    ("POST", r"/api/platform/shops(?:/[A-Za-z0-9_-]+/invite)?"),
    ("PATCH", r"/api/platform/shops/[A-Za-z0-9_-]+"),
    ("GET", r"/api/shop/account"),
    ("PATCH", r"/api/shop/account"),
    (
        "POST",
        r"/api/auth/(?:reauth/password|password/change|totp/(?:setup|confirm|disable|backup-codes))",
    ),
    (
        "GET",
        r"/api/admin/processor-profiles(?:/[A-Za-z0-9_-]+|/bindings/[A-Za-z0-9_-]+)?",
    ),
    ("POST", r"/api/admin/processor-profiles"),
    (
        "PUT",
        r"/api/admin/processor-profiles/(?:[A-Za-z0-9_-]+|bindings/[A-Za-z0-9_-]+)",
    ),
    (
        "DELETE",
        r"/api/admin/processor-profiles/(?:[A-Za-z0-9_-]+|bindings/[A-Za-z0-9_-]+)",
    ),
)


def _api(client, owner, args):
    parsed = urlsplit(args.path)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or "%" in args.path
        or "\\" in args.path
        or any(ord(char) < 33 for char in args.path)
        or not any(
            method == args.method and re.fullmatch(pattern, args.path)
            for method, pattern in _API_ROUTES
        )
    ):
        raise ManageError("Use a known relative owner API path", code="invalid_path")
    query = {}
    for entry in args.query:
        key, separator, value = entry.partition("=")
        if not separator or not key or key in query:
            raise ManageError(
                "Queries use unique KEY=VALUE entries", code="invalid_input"
            )
        query[key] = value
    if args.path.startswith("/api/platform/"):
        shop_commands._require_identity(owner, platform=True)
    elif args.path.startswith(
        (
            "/api/shop/",
            "/api/auth/totp/",
            "/api/auth/reauth/",
            "/api/auth/password/change",
        )
    ):
        shop_commands._require_identity(owner)
    private_input = args.method in WRITE_METHODS and (
        args.path == "/api/platform/settings"
        or args.path.startswith(("/api/auth/", shop_commands.PROFILE_PREFIX))
    )
    body = (
        shop_commands.private_json(args) if private_input else business.read_json(args)
    )
    if args.product:
        if (
            query.get("product_id", args.product) != args.product
            or body
            and body.get("product_id", args.product) != args.product
        ):
            raise ManageError(
                "Request belongs to a different selected product", code="no_scope"
            )
        query.setdefault("product_id", args.product)
        if (
            body is not None
            and args.method == "POST"
            and args.path
            in (
                "/api/admin/cards",
                "/api/admin/staff",
                "/api/manage/cards",
                "/api/manage/links",
                "/api/manage/batch",
            )
        ):
            body.setdefault("product_id", args.product)
    if args.method == "GET" and args.path in (
        "/api/admin/products",
        "/api/manage/products",
        "/api/manage/jobs",
    ):
        query.setdefault("compact", "false" if args.detail or args.output else "true")
        if args.path == "/api/manage/jobs":
            query.setdefault("view", "active")
    kwargs = {"params": query or None}
    if body is not None:
        kwargs["json"] = body
    secret = args.method == "POST" and args.path in (
        "/api/admin/cards",
        "/api/admin/staff",
        "/api/admin/products/quick",
        "/api/manage/cards",
        "/api/manage/links",
    )
    secret = secret or (args.method, args.path) in shop_commands.SECRET_RESPONSES
    if args.path.startswith(shop_commands.PROFILE_PREFIX):
        if query.get("shop_id"):
            shop_commands._profile_shop(owner, query["shop_id"])
        if body and body.get("shop_id"):
            shop_commands._profile_shop(owner, body["shop_id"])
        value = shop_commands.profile_metadata(
            _owner_request(client, owner, args.method, args.path, **kwargs)
        )
        return business._finish(args, {"ok": True, "result": value})
    if secret or args.output:
        return _secret_output(
            args,
            "owner-api",
            lambda: _owner_request(client, owner, args.method, args.path, **kwargs),
        )
    result = _owner_request(client, owner, args.method, args.path, **kwargs)
    return {"ok": True, "result": business._public(result)}


def dispatch(client, args):
    origin = origin_from_url(args.origin) if getattr(args, "origin", None) else None
    command = args.manage_command
    if command == "login":
        if not 0 <= args.wait <= 600:
            raise ManageError(
                "--wait must be between 0 and 600 seconds", code="invalid_input"
            )
        credentials = None
        if args.email is not None:
            if not args.email.strip() or "@" not in args.email:
                raise ManageError(
                    "Provide a valid merchant email", code="invalid_input"
                )
            if args.open_browser:
                raise ManageError(
                    "Email device approval stays in the CLI", code="invalid_input"
                )
            if args.credentials_file or args.credentials_stdin:
                credentials = shop_commands.private_json(
                    argparse.Namespace(
                        json_file=args.credentials_file,
                        json_stdin=args.credentials_stdin,
                    )
                )
            else:
                if not sys.stdin.isatty():
                    raise ManageError(
                        "Noninteractive login requires --credentials-stdin or a mode-600 --credentials-file",
                        code="invalid_input",
                    )
                credentials = {"password": getpass.getpass("Password: ")}
                second = getpass.getpass(
                    "Recovery code: "
                    if args.use_backup_code
                    else "TOTP code (Enter if not enabled): "
                )
                if second:
                    credentials["backup_code" if args.use_backup_code else "code"] = (
                        second
                    )
        elif args.credentials_file or args.credentials_stdin or args.use_backup_code:
            raise ManageError(
                "Private password input requires --email", code="invalid_input"
            )
        return client.login(
            origin,
            args.client_name,
            wait=args.wait,
            open_browser=args.open_browser,
            email=args.email,
            credentials=credentials,
        )
    if command == "login-status":
        return client.approval_status(
            client._select(origin, getattr(args, "grant", None), pending=True)
        )
    if command == "logout":
        if not args.all and origin is None and getattr(args, "grant", None) is None:
            raise ManageError(
                "Select --origin, --grant, or explicitly --all", code="invalid_input"
            )
        owners = [
            item
            for item in client.data["owners"]
            if (origin is None or item["origin"] == origin)
            and (not args.grant or item.get("device_id") == args.grant)
        ]
        removed = []
        for owner in owners:
            if owner.get("device_id"):
                client.request(
                    owner,
                    "DELETE",
                    "/api/admin/cli-owner-devices/"
                    + quote(owner["device_id"], safe=""),
                )
            client.data["owners"].remove(owner)
            removed.append(owner["id"])
        return {"ok": True, "removed": removed}
    owner = client._select(origin, getattr(args, "grant", None))
    client.session(owner)
    client.active_owner = owner
    if command in shop_commands.COMMANDS:
        client.session(owner, refresh_scope=True)
        return shop_commands.dispatch(client, owner, args)
    if command == "status":
        client.session(owner, refresh_scope=True)
        return {"ok": True, "owner": _safe_owner(owner)}
    if command == "storage":
        return {
            "ok": True,
            "storage": _owner_request(client, owner, "GET", "/api/admin/storage"),
        }
    if command in (
        "sessions",
        "devices",
        "events",
        "audit",
        "processors",
    ) and not getattr(args, "product", None):
        route = {
            "sessions": "sessions",
            "devices": "cli-devices",
            "events": "events",
            "audit": "audit",
            "processors": "processors",
        }[command]
        path = "/api/admin/" + route
        if getattr(args, "operation", None) in ("revoke", "retry"):
            path += "/" + quote(args.id, safe="")
            method = "POST" if command == "events" else "DELETE"
            if command == "events":
                path += "/retry"
            result = _owner_request(client, owner, method, path)
            return business._finish(args, _object(result))
        params = {"limit": min(args.limit, 200)} if command == "audit" else None
        items = _objects(_owner_request(client, owner, "GET", path, params=params))
        fields = {
            "sessions": (
                "id",
                "role",
                "channel",
                "active",
                "current",
                "created",
                "last_seen",
                "expires",
                "product_id",
                "link_name",
                "client_name",
            ),
            "devices": (
                "id",
                "product_id",
                "link_name",
                "client_name",
                "fingerprint",
                "created",
                "last_seen",
                "revoked",
                "active",
            ),
            "events": ("id", "type", "job_id", "created", "webhook_state", "attempts"),
            "audit": ("id", "action", "target", "created", "channel", "client_name"),
            "processors": ("id", "name", "version", "delivery", "source_url"),
        }[command]
        result = {
            "ok": True,
            command: business._public(items)
            if args.detail
            else [business._only(item, fields) for item in items],
        }
        return business._finish(args, result)
    if command == "owner-devices":
        path = "/api/admin/cli-owner-devices"
        if args.operation == "revoke":
            result = _owner_request(
                client, owner, "DELETE", path + "/" + quote(args.id, safe="")
            )
            if args.id == owner.get("device_id"):
                client.data["owners"].remove(owner)
            return {"ok": True, **_object(result)}
        return {"ok": True, "devices": _owner_request(client, owner, "GET", path)}
    if command == "passkeys":
        if args.operation == "list":
            result = {
                "ok": True,
                "passkeys": _owner_request(client, owner, "GET", "/api/auth/passkeys"),
            }
        elif args.operation == "remove":
            result = _owner_request(
                client, owner, "DELETE", "/api/auth/passkeys/" + quote(args.id, safe="")
            )
        elif args.operation == "register-options":
            result = _owner_request(client, owner, "POST", "/api/auth/register/options")
        else:
            result = _owner_request(
                client,
                owner,
                "POST",
                "/api/auth/register/verify",
                json=business.read_json(args),
            )
        return business._finish(args, _object(result))
    if command == "products" or command == "product" and args.operation == "list":
        items = _global_products(
            client, owner, compact=not args.detail and not args.output
        )
        if args.output:
            return _secret_output(args, "products", lambda: items)
        fields = (
            "id",
            "shop_id",
            "name",
            "mode",
            "delivery",
            "view_policy",
            "public",
            "active",
            "parameters_count",
            "outputs_count",
            "variants",
        )
        return {
            "ok": True,
            "products": business._public(items)
            if args.detail
            else [business._only(item, fields) for item in items],
        }
    if command == "product" and args.operation in ("create", "quick", "templates"):
        if args.operation == "templates":
            return business._finish(
                args,
                {
                    "ok": True,
                    "templates": _owner_request(
                        client, owner, "GET", "/api/admin/product-templates"
                    ),
                },
            )
        body = business.read_json(args)
        path = "/api/admin/products" + ("/quick" if args.operation == "quick" else "")
        if args.operation == "quick" or args.output:
            return _secret_output(
                args,
                "product",
                lambda: _owner_request(client, owner, "POST", path, json=body),
            )
        result = _object(_owner_request(client, owner, "POST", path, json=body))
        return {
            "ok": True,
            "product": business._public(result)
            if args.detail
            else business._only(
                result, ("id", "name", "mode", "delivery", "public", "active")
            ),
        }
    if command == "api":
        return _api(client, owner, args)
    if command == "queues":
        ids = (
            [args.product]
            if args.product
            else [item["id"] for item in _global_products(client, owner, compact=True)]
        )
        queues = []
        for product_id in ids:
            grant = client.grant(product=product_id, origin=owner["origin"])
            jobs = client.jobs(
                grant, view=args.view, state=args.state, limit=args.limit, compact=True
            )
            queues.append(
                {
                    "product_id": product_id,
                    "jobs": [remote.compact_job(job) for job in jobs],
                }
            )
        return {"ok": True, "view": args.view, "queues": queues}
    # The shared parser gives product-level commands a required product scope;
    # a merchant still names the product explicitly instead of accidentally
    # applying a mutation to every pipeline.
    if command in business.COMMANDS:
        if command == "product" and args.operation == "schema":
            grant = client.grant(product=args.product, origin=owner["origin"])
            product = _object(
                client.request(
                    grant,
                    "GET",
                    "/api/manage/product",
                    params={"product_id": args.product},
                )
            )
            return business._finish(
                args,
                {
                    "ok": True,
                    "schema": business._schema(
                        product, language=args.language, detail=args.detail
                    ),
                },
            )
        if command == "product" and args.operation == "get" and args.output:
            grant = client.grant(product=args.product, origin=owner["origin"])
            return _secret_output(
                args,
                "product",
                lambda: client.request(
                    grant,
                    "GET",
                    "/api/manage/product",
                    params={"product_id": args.product},
                ),
            )
        return business.dispatch(client, args, owner["origin"])
    return remote.dispatch(
        client, args, "complete" if command == "succeed" else command, owner["origin"]
    )


def execute(args, *, transport=None):
    path = profile_path(args.profile)
    args.profile = path
    _no_symlinks(path)
    error = None
    result = None
    with private_profile(path, create=args.manage_command == "login") as data:
        owners = data.setdefault("owners", [])
        if not isinstance(owners, list) or any(
            not isinstance(owner, dict)
            or not isinstance(owner.get("origin"), str)
            or not isinstance(owner.get("private_key"), str)
            or not isinstance(owner.get("id"), str)
            for owner in owners
        ):
            raise ManageError("Invalid owner profile", code="invalid_profile")
        with _BusinessOwner(
            data, transport=transport, persist=lambda: _save_profile(path, data)
        ) as client:
            try:
                result = dispatch(client, args)
            except ManageError as exc:
                error = exc
    if error:
        raise error
    return result


def run(args):
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
                    "error": "Owner command failed; check private files and inputs",
                    "code": "invalid_input",
                },
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))

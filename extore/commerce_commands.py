"""Private-file machine CLI for Extore's open commerce import protocol."""

import fcntl
import json
import math
import os
import secrets
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

from .commerce_client import (
    AuthorizationRequest,
    CommerceClient,
    CommerceError,
    TokenSet,
    _credential,
    _identifier,
    _origin,
    _pairs,
    _redirect,
    _scopes,
)
from .http_proxy import add_proxy_arguments, resolve_proxy
from .manage_client import ManageError

INPUT_LIMIT = 2 * 1024 * 1024
AUTH_SCHEMA = "extore.commerce-authorization.v1"
TOKEN_SCHEMA = "extore.commerce-token.v1"
_AUTH_FIELDS = {
    "issuer",
    "client_id",
    "redirect_uri",
    "scopes",
    "url",
    "state",
    "code_verifier",
    "_consumed",
}
_TOKEN_FIELDS = {
    "schema",
    "issuer",
    "client_id",
    "grant_id",
    "scope",
    "expires_in",
    "grant_expires",
    "access_token",
    "refresh_token",
    "_refresh_consumed",
}
_MESSAGES = {
    "invalid_input": "Use the documented bounded private input files and fields.",
    "unsafe_input": "Input must be a regular, mode-600 file owned only by you.",
    "input_busy": "Another commerce operation holds this input file; do not replay it.",
    "input_consumed": "This authorization or refresh was already attempted; do not replay it.",
    "output_exists": "Output already exists; choose a new private file.",
    "unsafe_output": "Cannot create a new private output file in an owned directory.",
    "output_failed": "Private output could not be saved; do not repeat a mutation automatically.",
    "commerce_error": "Commerce request failed. A consumed authorization or refresh must not be replayed.",
    "invalid_proxy": "Use one explicit proxy or opt into environment proxy settings.",
}


class CommandError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(_MESSAGES[code])


def add_parser(commands):
    parent = commands.add_parser(
        "commerce", help="open commerce import protocol using private files"
    )
    children = parent.add_subparsers(dest="commerce_command", required=True)
    for operation in (
        "metadata",
        "authorize",
        "token",
        "refresh",
        "revoke",
        "products",
        "product",
        "cards",
    ):
        parser = children.add_parser(operation)
        parser.add_argument(
            "--origin", required=True, help="HTTPS Extore issuer origin"
        )
        parser.add_argument(
            "--client-id", required=True, help="registered public client ID"
        )
        parser.add_argument(
            "--output", type=Path, required=True, help="new mode-600 output file"
        )
        if operation == "authorize":
            parser.add_argument("--redirect-uri", required=True)
            parser.add_argument(
                "--scope", action="append", choices=("products.read", "cards.issue")
            )
            parser.add_argument("--product-id", action="append", default=[])
        if operation == "token":
            parser.add_argument("--authorization-file", type=Path, required=True)
            group = parser.add_mutually_exclusive_group(required=True)
            group.add_argument("--callback-file", type=Path)
            group.add_argument("--callback-stdin", action="store_true")
        if operation in ("refresh", "revoke", "products", "product", "cards"):
            group = parser.add_mutually_exclusive_group(required=True)
            group.add_argument("--token-file", type=Path)
            if operation != "refresh":
                group.add_argument("--token-stdin", action="store_true")
        if operation == "product":
            parser.add_argument("--product-id", required=True)
        if operation == "cards":
            group = parser.add_mutually_exclusive_group(required=True)
            group.add_argument("--request-file", type=Path)
            group.add_argument("--request-stdin", action="store_true")
    add_proxy_arguments(parent)
    return parent


def _finite(value, *, maximum=None):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or value <= 0
        or maximum is not None
        and value > maximum
    ):
        raise CommandError("invalid_input")
    return value


def _private_fd(path, flags=os.O_RDONLY):
    try:
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            os.close(fd)
            raise CommandError("unsafe_input")
        return fd
    except OSError:
        raise CommandError("unsafe_input") from None


def _read(path=None):
    if path is None:
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        value = stream.read(INPUT_LIMIT + 1)
        if isinstance(value, str):
            value = value.encode("utf-8")
    else:
        with os.fdopen(_private_fd(path), "rb") as stream:
            value = stream.read(INPUT_LIMIT + 1)
    if len(value) > INPUT_LIMIT:
        raise CommandError("invalid_input")
    try:
        return value.decode("utf-8")
    except UnicodeError:
        raise CommandError("invalid_input") from None


def _json(value):
    def invalid_number(_):
        raise ValueError

    try:
        result = json.loads(
            value, object_pairs_hook=_pairs, parse_constant=invalid_number
        )
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise CommandError("invalid_input") from None


def _payload(value):
    return (
        json.dumps(
            value, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        ).encode()
        + b"\n"
    )


def _owned_directory(path):
    info = path.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
    ):
        raise CommandError("unsafe_output")


def _atomic(path, value, identity):
    """Replace only the private inode already reserved/validated by this operation."""
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(12) + ".tmp")
    descriptor = None
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(_payload(value))
            stream.flush()
            os.fsync(stream.fileno())
        info = path.lstat()
        if (info.st_dev, info.st_ino) != identity:
            raise CommandError("output_failed")
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


class PrivateOutput:
    def __init__(self, path):
        self.path = Path(path)
        self.written = False
        try:
            _owned_directory(self.path.parent)
            fd = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            os.fchmod(fd, 0o600)
            info = os.fstat(fd)
            os.close(fd)
            self.identity = (info.st_dev, info.st_ino)
        except FileExistsError:
            raise CommandError("output_exists") from None
        except OSError:
            raise CommandError("unsafe_output") from None

    def __enter__(self):
        return self

    def write(self, value):
        try:
            _atomic(self.path, value, self.identity)
            self.written = True
            return {
                "ok": True,
                "output": str(self.path),
                "bytes": self.path.stat().st_size,
            }
        except (OSError, ValueError):
            raise CommandError("output_failed") from None

    def __exit__(self, *args):
        if not self.written:
            try:
                info = self.path.lstat()
                if (info.st_dev, info.st_ino) == self.identity:
                    self.path.unlink()
            except FileNotFoundError:
                pass


@contextmanager
def _locked_input(path):
    """Stable sidecar locks remain valid when the consumed input is atomically replaced."""
    path = Path(path)
    lock_path = path.with_name(path.name + ".commerce.lock")
    descriptor = None
    try:
        _owned_directory(path.parent)
        # Validate the source before creating even a lock-file side effect.
        os.close(_private_fd(path))
        try:
            descriptor = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
            os.fchmod(descriptor, 0o600)
        except FileExistsError:
            descriptor = _private_fd(lock_path, os.O_RDWR)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise CommandError("unsafe_input")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CommandError("input_busy") from None
        value = _json(_read(path))
        info = path.lstat()
        yield value, (info.st_dev, info.st_ino)
    except OSError:
        raise CommandError("unsafe_input") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _authorization(value, issuer, client_id):
    if (
        set(value) != {"schema", "transaction", "authorization_url"}
        or value.get("schema") != AUTH_SCHEMA
    ):
        raise CommandError("invalid_input")
    raw = value["transaction"]
    if not isinstance(raw, dict) or set(raw) != _AUTH_FIELDS:
        raise CommandError("invalid_input")
    if (
        raw["issuer"] != issuer
        or raw["client_id"] != client_id
        or type(raw["_consumed"]) is not bool
    ):
        raise CommandError("invalid_input")
    if raw["_consumed"]:
        raise CommandError("input_consumed")
    if not isinstance(raw["scopes"], list) or not all(
        isinstance(item, str) for item in raw["scopes"]
    ):
        raise CommandError("invalid_input")
    _redirect(raw["redirect_uri"])
    scopes = _scopes(raw["scopes"])
    _credential(raw["state"])
    verifier = _credential(raw["code_verifier"])
    if not 43 <= len(verifier) <= 128 or value["authorization_url"] != raw["url"]:
        raise CommandError("invalid_input")
    if not isinstance(raw["url"], str) or not raw["url"].startswith(
        issuer + "/oauth/authorize?"
    ):
        raise CommandError("invalid_input")
    return AuthorizationRequest(
        issuer,
        client_id,
        raw["redirect_uri"],
        scopes,
        raw["url"],
        raw["state"],
        verifier,
    )


def _token(value, issuer, client_id, *, refresh=False):
    if set(value) != _TOKEN_FIELDS or value.get("schema") != TOKEN_SCHEMA:
        raise CommandError("invalid_input")
    if (
        value["issuer"] != issuer
        or value["client_id"] != client_id
        or type(value["_refresh_consumed"]) is not bool
    ):
        raise CommandError("invalid_input")
    if refresh and value["_refresh_consumed"]:
        raise CommandError("input_consumed")
    if not isinstance(value["scope"], list) or not all(
        isinstance(item, str) for item in value["scope"]
    ):
        raise CommandError("invalid_input")
    scopes = _scopes(value["scope"])
    expires = value["expires_in"]
    if type(expires) is not int:
        raise CommandError("invalid_input")
    _finite(expires, maximum=900)
    _finite(value["grant_expires"])
    return TokenSet(
        issuer,
        client_id,
        _identifier(value["grant_id"]),
        scopes,
        expires,
        value["grant_expires"],
        _credential(value["access_token"]),
        _credential(value["refresh_token"]),
    )


def _token_export(value):
    return {
        "schema": TOKEN_SCHEMA,
        "issuer": value.issuer,
        "client_id": value.client_id,
        "grant_id": value.grant_id,
        "scope": list(value.scope),
        "expires_in": value.expires_in,
        "grant_expires": value.grant_expires,
        "access_token": value.access_token,
        "refresh_token": value.refresh_token,
        "_refresh_consumed": False,
    }


def _card_request(value):
    required = {
        "product_id",
        "variant_id",
        "count",
        "idempotency_key",
        "expected_revision",
    }
    if not required <= set(value) or set(value) - required - {"label", "expires"}:
        raise CommandError("invalid_input")
    _identifier(value["product_id"])
    _identifier(value["variant_id"])
    if type(value["count"]) is not int or not 1 <= value["count"] <= 100:
        raise CommandError("invalid_input")
    key = value["idempotency_key"]
    if (
        not isinstance(key, str)
        or not 8 <= len(key) <= 200
        or any(ord(c) < 33 or ord(c) > 126 for c in key)
    ):
        raise CommandError("invalid_input")
    revision = value["expected_revision"]
    if (
        not isinstance(revision, str)
        or len(revision) != 64
        or any(c not in "0123456789abcdef" for c in revision)
    ):
        raise CommandError("invalid_input")
    if "label" in value and (
        not isinstance(value["label"], str)
        or len(value["label"]) > 100
        or any(ord(c) < 32 for c in value["label"])
    ):
        raise CommandError("invalid_input")
    if "expires" in value:
        _finite(value["expires"])
    return value


def _execute(args, output, issuer, client_id, proxy):
    operation = args.commerce_command
    if operation == "token":
        callback = _read(getattr(args, "callback_file", None)).strip()
        if not callback or len(callback) > 8192 or any(ord(c) <= 32 for c in callback):
            raise CommandError("invalid_input")
        with _locked_input(args.authorization_file) as (value, identity):
            transaction = _authorization(value, issuer, client_id)
            with CommerceClient(issuer, client_id, proxy_settings=proxy) as client:
                try:
                    client.validate_callback(transaction, callback)
                except CommerceError:
                    value["transaction"]["_consumed"] = True
                    _atomic(args.authorization_file, value, identity)
                    raise
                value["transaction"]["_consumed"] = True
                _atomic(args.authorization_file, value, identity)
                result = client.complete_authorization(transaction, callback)
                return output.write(_token_export(result))
    if operation == "refresh":
        with _locked_input(args.token_file) as (value, identity):
            credentials = _token(value, issuer, client_id, refresh=True)
            with CommerceClient(issuer, client_id, proxy_settings=proxy) as client:
                value["_refresh_consumed"] = True
                _atomic(args.token_file, value, identity)
                result = client.refresh(credentials)
                return {
                    **output.write(_token_export(result)),
                    "previous_refresh_consumed": True,
                }
    credentials = None
    request = None
    if operation in ("revoke", "products", "product", "cards"):
        if getattr(args, "token_stdin", False) and getattr(
            args, "request_stdin", False
        ):
            raise CommandError("invalid_input")
        credentials = _token(
            _json(_read(getattr(args, "token_file", None))), issuer, client_id
        )
    if operation == "cards":
        request = _card_request(_json(_read(getattr(args, "request_file", None))))
    if operation == "product":
        _identifier(args.product_id)
    if operation == "authorize":
        _redirect(args.redirect_uri)
        scopes = _scopes(args.scope or ("products.read",))
        if len(args.product_id) > 100 or len(set(args.product_id)) != len(
            args.product_id
        ):
            raise CommandError("invalid_input")
        for product in args.product_id:
            _identifier(product)
    with CommerceClient(issuer, client_id, proxy_settings=proxy) as client:
        if operation == "metadata":
            result = client.metadata()
        elif operation == "authorize":
            authorization = client.authorize(
                args.redirect_uri, scopes=scopes, product_ids=args.product_id
            )
            result = {
                "schema": AUTH_SCHEMA,
                "authorization_url": authorization.url,
                "transaction": {
                    "issuer": issuer,
                    "client_id": client_id,
                    "redirect_uri": authorization.redirect_uri,
                    "scopes": list(authorization.scopes),
                    "url": authorization.url,
                    "state": authorization.state,
                    "code_verifier": authorization.code_verifier,
                    "_consumed": False,
                },
            }
        elif operation == "revoke":
            client.revoke(credentials)
            result = {"ok": True, "revoked": True}
        elif operation == "products":
            result = client.products(credentials)
        elif operation == "product":
            result = client.product(credentials, args.product_id)
        elif operation == "cards":
            result = client.issue_cards(credentials, **request).export()
        else:
            raise CommandError("invalid_input")
        status = output.write(result)
        if operation == "products":
            status["product_count"] = len(result["products"])
        if operation == "cards":
            status["count"] = result["count"]
        return status


def run(args):
    try:
        issuer, client_id = _origin(args.origin), _identifier(args.client_id)
        proxy = resolve_proxy(args)
        with PrivateOutput(args.output) as output:
            result = _execute(args, output, issuer, client_id, proxy)
        result["operation"] = args.commerce_command
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
        return 0
    except CommandError as error:
        code = error.code
    except CommerceError as error:
        code = error.code if isinstance(error.code, str) else "commerce_error"
    except ManageError:
        code = "invalid_proxy"
    except (ValueError, TypeError, AttributeError, KeyError, UnicodeError):
        code = "invalid_input"
    except OSError:
        code = "unsafe_input"
    print(
        json.dumps(
            {
                "ok": False,
                "error": code,
                "message": _MESSAGES.get(code, _MESSAGES["commerce_error"]),
            }
        ),
        file=sys.stderr,
    )
    raise SystemExit(2)

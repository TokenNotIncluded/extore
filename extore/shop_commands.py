"""Named platform, shop-account and processor-profile CLI operations."""

import os
import stat
from urllib.parse import quote

from . import manage_commands as business
from .manage_client import ManageError

COMMANDS = {"platform", "account", "totp", "processor-profiles", "maintenance"}
PROFILE_PREFIX = "/api/admin/processor-profiles"
SECRET_RESPONSES = {
    ("POST", "/api/auth/totp/setup"),
    ("POST", "/api/auth/totp/confirm"),
    ("POST", "/api/auth/totp/backup-codes"),
}


def _operations(parent, operations):
    children = parent.add_subparsers(dest="operation", required=True)
    parsers = {}
    for name in operations:
        parser = children.add_parser(name)
        business._scope(parser, product=False)
        parsers[name] = parser
    return parsers


def add_commands(subcommands):
    maintenance = _operations(
        subcommands.add_parser(
            "maintenance",
            help="bounded retention maintenance; cleanup previews by default",
        ),
        ("status", "policy", "cleanup"),
    )
    for parser in maintenance.values():
        parser.add_argument("--shop", help="platform root's explicit shop UUID")
    business._json_arguments(maintenance["policy"], required=False)
    maintenance["cleanup"].add_argument("--apply", action="store_true")
    maintenance["cleanup"].add_argument(
        "--area", action="append", dest="areas", choices=("links", "events", "audit")
    )
    maintenance["cleanup"].add_argument(
        "--limit", type=business.cleanup_limit, default=100
    )
    platform = subcommands.add_parser(
        "platform", help="platform settings and merchant accounts; platform root only"
    )
    groups = platform.add_subparsers(dest="platform_command", required=True)
    settings = _operations(groups.add_parser("settings"), ("get", "update"))
    business._json_arguments(settings["update"])
    shops = _operations(
        groups.add_parser("shops"),
        ("list", "create", "invite", "enable", "disable", "quota"),
    )
    business._json_arguments(shops["create"])
    for operation in ("invite", "enable", "disable", "quota"):
        shops[operation].add_argument("id", help="shop UUID")
    shops["quota"].add_argument(
        "--bytes", type=int, required=True, help="shop attachment budget in bytes"
    )

    account = _operations(
        subcommands.add_parser("account", help="current merchant account and password"),
        ("get", "update", "reauth", "password"),
    )
    for operation in ("update", "reauth", "password"):
        business._json_arguments(account[operation])

    totp = _operations(
        subcommands.add_parser("totp", help="merchant TOTP and private recovery codes"),
        ("setup", "confirm", "disable", "backup-codes"),
    )
    for parser in totp.values():
        business._json_arguments(parser)

    profiles = _operations(
        subcommands.add_parser(
            "processor-profiles",
            help="shop-isolated processor credentials; metadata only",
        ),
        ("list", "get", "create", "update", "revoke", "binding", "bind", "unbind"),
    )
    profiles["list"].add_argument("--shop", help="platform root's explicit shop UUID")
    profiles["create"].add_argument("--shop", help="platform root's explicit shop UUID")
    for operation in ("get", "update", "revoke"):
        profiles[operation].add_argument("id", help="processor-profile UUID")
    for operation in ("create", "update"):
        business._json_arguments(profiles[operation])
    for operation in ("binding", "bind", "unbind"):
        for action in profiles[operation]._actions:
            if action.dest == "product":
                action.required = True
    profiles["bind"].add_argument("id", help="processor-profile UUID")


def private_json(args):
    """Credential input uses stdin or a private regular file, never argv values."""
    path = getattr(args, "json_file", None)
    if path is not None:
        try:
            info = path.lstat()
        except OSError:
            raise ManageError(
                "Credential input file is unavailable", code="invalid_input"
            ) from None
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or any(part.is_symlink() for part in path.parents)
        ):
            raise ManageError(
                "Credential input files must be owned by you, mode 600, and contain no symlinks",
                code="unsafe_input",
            )
    return business.read_json(args)


def _require_identity(owner, *, platform=False):
    if "shop_id" not in owner:
        raise ManageError(
            "Refresh the pinned owner identity before this operation",
            code="invalid_response",
        )
    if platform and owner["shop_id"] is not None:
        raise ManageError(
            "This operation requires platform-root authorization", code="no_scope"
        )
    if not platform and owner["shop_id"] is None:
        raise ManageError(
            "Select a merchant device with --grant for this operation", code="no_scope"
        )


def _result(client, owner, args, method, path, *, body=None, query=None, secret=False):
    def request():
        return client.request(
            owner,
            method,
            path,
            **({"json": body} if body is not None else {}),
            **({"params": query} if query else {}),
        )

    if secret:
        with business.OutputFile(args, "account-secret") as output:
            return output.write(request())
    value = business._public(request())
    if path.startswith(PROFILE_PREFIX):
        value = profile_metadata(value)
    return business._finish(args, {"ok": True, "result": value})


def profile_metadata(value):
    """Private exports cannot turn profile-vault data into plaintext."""
    if isinstance(value, list):
        return [profile_metadata(item) for item in value]
    if not isinstance(value, dict):
        return value
    allowed = {
        "id",
        "shop_id",
        "processor_id",
        "name",
        "revision",
        "disabled",
        "created",
        "updated",
        "bound_revision",
        "product_id",
        "profile",
        "ok",
    }
    return {
        key: profile_metadata(item) for key, item in value.items() if key in allowed
    }


def dispatch(client, owner, args):
    command, operation = args.manage_command, args.operation
    if command == "maintenance":
        _profile_shop(owner, args.shop)
        if operation == "status":
            return _result(
                client,
                owner,
                args,
                "GET",
                "/api/admin/maintenance",
                query={"shop_id": args.shop} if args.shop else None,
            )
        if operation == "policy":
            body = business.read_json(args)
            if body is None:
                return _result(
                    client,
                    owner,
                    args,
                    "GET",
                    "/api/admin/maintenance",
                    query={"shop_id": args.shop} if args.shop else None,
                )
            if args.shop:
                if body.get("shop_id", args.shop) != args.shop:
                    raise ManageError(
                        "Policy belongs to a different selected shop", code="no_scope"
                    )
                body["shop_id"] = args.shop
            _profile_shop(owner, body.get("shop_id"))
            return _result(
                client, owner, args, "PUT", "/api/admin/maintenance/policy", body=body
            )
        body = {
            "areas": args.areas or ["links", "events", "audit"],
            "dry_run": not args.apply,
            "limit": args.limit,
        }
        if args.shop:
            body["shop_id"] = args.shop
        if args.product:
            body["product_id"] = args.product
        return _result(
            client, owner, args, "POST", "/api/admin/maintenance/cleanup", body=body
        )
    if command == "platform":
        _require_identity(owner, platform=True)
        if args.platform_command == "settings":
            path = "/api/platform/settings"
            body = private_json(args) if operation == "update" else None
            return _result(
                client,
                owner,
                args,
                "PUT" if operation == "update" else "GET",
                path,
                body=body,
            )
        path = "/api/platform/shops"
        if operation == "list":
            return _result(client, owner, args, "GET", path)
        if operation == "create":
            return _result(
                client, owner, args, "POST", path, body=business.read_json(args)
            )
        path += "/" + quote(args.id, safe="")
        if operation == "invite":
            return _result(client, owner, args, "POST", path + "/invite", body={})
        if operation == "quota":
            if args.bytes < 1:
                raise ManageError(
                    "Storage quota must be a positive byte count", code="invalid_input"
                )
            return _result(
                client,
                owner,
                args,
                "PATCH",
                path,
                body={"storage_limit_bytes": args.bytes},
            )
        return _result(
            client, owner, args, "PATCH", path, body={"enabled": operation == "enable"}
        )
    if command == "account":
        _require_identity(owner)
        if operation in ("get", "update"):
            return _result(
                client,
                owner,
                args,
                "PATCH" if operation == "update" else "GET",
                "/api/shop/account",
                body=business.read_json(args) if operation == "update" else None,
            )
        body = private_json(args)
        path = (
            "/api/auth/reauth/password"
            if operation == "reauth"
            else "/api/auth/password/change"
        )
        result = _result(client, owner, args, "POST", path, body=body)
        if operation == "password":
            # Changing credentials revokes the old device. A browser Set-Cookie
            # response is never adopted as a replacement CLI authorization.
            client.data["owners"].remove(owner)
            client.persist()
            result["login_required"] = True
        return result
    if command == "totp":
        _require_identity(owner)
        path = "/api/auth/totp/" + operation
        return _result(
            client,
            owner,
            args,
            "POST",
            path,
            body=private_json(args),
            secret=("POST", path) in SECRET_RESPONSES,
        )
    if command == "processor-profiles":
        if operation in ("binding", "bind", "unbind"):
            path = PROFILE_PREFIX + "/bindings/" + quote(args.product, safe="")
            method = {"binding": "GET", "bind": "PUT", "unbind": "DELETE"}[operation]
            return _result(
                client,
                owner,
                args,
                method,
                path,
                body={"profile_id": args.id} if operation == "bind" else None,
            )
        if operation == "list":
            query = {"shop_id": args.shop} if args.shop else None
            _profile_shop(owner, args.shop)
            return _result(client, owner, args, "GET", PROFILE_PREFIX, query=query)
        if operation in ("create", "update"):
            body = private_json(args)
            if operation == "create":
                requested = args.shop or body.get("shop_id")
                _profile_shop(owner, requested)
                if args.shop and body.get("shop_id", args.shop) != args.shop:
                    raise ManageError(
                        "Profile input names a different selected shop", code="no_scope"
                    )
                if requested:
                    body["shop_id"] = requested
            path = PROFILE_PREFIX + (
                "/" + quote(args.id, safe="") if operation == "update" else ""
            )
            return _result(
                client,
                owner,
                args,
                "PUT" if operation == "update" else "POST",
                path,
                body=body,
            )
        path = PROFILE_PREFIX + "/" + quote(args.id, safe="")
        return _result(
            client, owner, args, "DELETE" if operation == "revoke" else "GET", path
        )
    raise ManageError("Unknown shop command", code="invalid_input")


def _profile_shop(owner, selected):
    if "shop_id" not in owner:
        raise ManageError("Refresh the pinned owner identity", code="invalid_response")
    if owner["shop_id"] is not None and selected not in (None, owner["shop_id"]):
        raise ManageError("Profile belongs to a different shop", code="no_scope")

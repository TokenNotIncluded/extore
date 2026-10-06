"""Named platform, shop-account and processor-profile CLI operations."""

import os
import re
import stat
from urllib.parse import quote, urlsplit

from . import manage_commands as business
from .manage_client import ManageError

COMMANDS = {"platform", "account", "totp", "processor-profiles", "maintenance", "proxy"}
PROFILE_PREFIX = "/api/admin/processor-profiles"
PROXY_PREFIX = "/api/admin/proxy"
PROXY_PUBLIC_FIELDS = ("route_id", "issuer_id", "name", "origin", "path", "public_key")
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
    proxy = subcommands.add_parser(
        "proxy", help="shop-isolated signed card destinations; no private-key exports"
    )
    proxy_groups = proxy.add_subparsers(dest="proxy_command", required=True)
    identities = _operations(proxy_groups.add_parser("identities"), ("list", "create"))
    identities["create"].add_argument("--name", required=True)
    routes = _operations(
        proxy_groups.add_parser("routes"),
        ("list", "create", "import", "export", "enable", "disable", "default"),
    )
    for parser in (*identities.values(), *routes.values()):
        parser.add_argument(
            "--shop", help="explicit shop UUID; required for platform-root devices"
        )
    routes["create"].add_argument("--name", required=True)
    routes["create"].add_argument(
        "--identity", required=True, help="this shop's issuer identity ID"
    )
    routes["create"].add_argument(
        "--default-issuer",
        action="store_true",
        help="use this local route when issuing new codes",
    )
    business._json_arguments(routes["import"])
    routes["import"].add_argument("--name", help="optional local display name")
    for operation in ("export", "enable", "disable", "default"):
        routes[operation].add_argument("id", help="immutable route ID")
    routes["default"].add_argument(
        "--clear", action="store_true", help="stop using this route for new issuance"
    )
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
            help="shop-isolated processor settings and workflow; secret values are write-only",
        ),
        ("list", "get", "create", "update", "revoke", "binding", "bind", "unbind"),
    )
    profiles["list"].add_argument("--shop", help="platform root's explicit shop UUID")
    profiles["create"].add_argument("--shop", help="platform root's explicit shop UUID")
    for operation in ("get", "update", "revoke"):
        profiles[operation].add_argument("id", help="processor-profile UUID")
    for operation in ("create", "update"):
        business._json_arguments(profiles[operation])
        profiles[operation].description = (
            "Write processor configuration and workflow variables/secrets/runtime "
            "using private JSON input; secret values are never returned."
        )
        for action in profiles[operation]._actions:
            if action.dest == "json_file":
                action.help = (
                    "mode-600 JSON file containing configuration and/or workflow; "
                    "never put secret values in command arguments"
                )
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
    value = request()
    if path.startswith(PROFILE_PREFIX):
        value = profile_metadata(value)
    else:
        value = business._public(value)
    return business._finish(args, {"ok": True, "result": value})


def _workflow_metadata(value):
    """The public workflow contract contains no secret values or run commands."""
    if not isinstance(value, dict):
        return {}

    def valid_name(name):
        return (
            isinstance(name, str)
            and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name) is not None
            and not name.startswith("EXTORE_")
        )

    variables = value.get("variables", {})
    variables = variables if isinstance(variables, dict) else {}
    names = value.get("configured_secret_names", [])
    names = names if isinstance(names, list) else []
    secret_names = {name for name in names if valid_name(name)}
    unexpected_secrets = value.get("secrets", {})
    if isinstance(unexpected_secrets, dict):
        secret_names.update(name for name in unexpected_secrets if valid_name(name))
    names = list(dict.fromkeys(name for name in names if valid_name(name)))[:64]
    # A malformed response must not echo a value classified as a secret, even
    # when it also appears in the ordinary-variable map.
    ordinary = {}
    size = 0
    for name, text in variables.items():
        if (
            not valid_name(name)
            or name in secret_names
            or not isinstance(text, str)
            or len(ordinary) >= 64
        ):
            continue
        try:
            length = len(text.encode("utf-8"))
        except UnicodeError:
            continue
        if length > 8192 or size + length > 65536:
            continue
        ordinary[name] = text
        size += length
    bounds = {
        "timeout_seconds": (10, 120),
        "memory_mb": (64, 512),
        "cpu_seconds": (1, 120),
        "max_output_bytes": (65536, 1000000),
    }
    runtime = value.get("runtime", {})
    runtime = runtime if isinstance(runtime, dict) else {}
    return {
        "variables": ordinary,
        "runtime": {
            key: runtime[key]
            for key, (low, high) in bounds.items()
            if type(runtime.get(key)) is int and low <= runtime[key] <= high
        },
        "configured_secret_names": names,
    }


def profile_metadata(value):
    """Keep editable ordinary settings while excluding the profile's secrets."""
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
    result = {
        key: profile_metadata(item) for key, item in value.items() if key in allowed
    }
    if "configuration" in value or "configured_fields" in value:
        from .processors import configuration_fields, editable_configuration

        try:
            fields = configuration_fields(value.get("processor_id"))
            result["configuration"] = editable_configuration(
                value.get("processor_id"), value.get("configuration", {})
            )
        except (ValueError, KeyError, TypeError):
            fields = {}
            result["configuration"] = {}
        configured = value.get("configured_fields", [])
        result["configured_fields"] = (
            list(dict.fromkeys(name for name in configured if name in fields))
            if isinstance(configured, list)
            and all(isinstance(name, str) for name in configured)
            else []
        )
    if "workflow" in value:
        result["workflow"] = _workflow_metadata(value["workflow"])
    return result


def dispatch(client, owner, args):
    command, operation = args.manage_command, args.operation
    if command == "proxy":
        return _proxy_dispatch(client, owner, args)
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


def proxy_metadata(value, *, public=False):
    """Allow only public routing material; an unexpected secret is never echoed."""
    if isinstance(value, list):
        return [proxy_metadata(item, public=public) for item in value]
    if not isinstance(value, dict):
        raise ManageError("Invalid proxy metadata", code="invalid_response")
    keys = (
        PROXY_PUBLIC_FIELDS
        if public
        else (
            *PROXY_PUBLIC_FIELDS,
            "id",
            "shop_id",
            "identity_id",
            "enabled",
            "default_issuer",
            "created",
            "updated",
            "kind",
        )
    )
    return {key: value[key] for key in keys if key in value}


def _public_proxy_route(value, *, input=False):
    code = "invalid_input" if input else "invalid_response"
    if (
        not isinstance(value, dict)
        or not set(PROXY_PUBLIC_FIELDS) <= set(value)
        or input
        and set(value) != set(PROXY_PUBLIC_FIELDS)
    ):
        raise ManageError(
            "Provide the six public route fields exported by the issuing site",
            code=code,
        )
    route = proxy_metadata(value, public=True)
    if (
        any(not isinstance(route[key], str) for key in PROXY_PUBLIC_FIELDS)
        or any(
            not re.fullmatch(r"[0-9a-f]{32}", route[key])
            for key in ("route_id", "issuer_id")
        )
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", route["public_key"])
        or not route["name"].strip()
        or len(route["name"]) > 100
        or not re.fullmatch(r"/[A-Za-z0-9/_-]*", route["path"])
        or "//" in route["path"]
    ):
        raise ManageError("Invalid public route identity, key or path", code=code)
    try:
        url = urlsplit(route["origin"])
        if (
            url.scheme != "https"
            or not url.hostname
            or "." not in url.hostname
            or url.username
            or url.password
            or url.path
            or url.query
            or url.fragment
            or url.port not in (None, 443)
            or "\\" in route["origin"]
            or re.search(r"[\s\x00-\x1f\x7f]", route["origin"])
        ):
            raise ValueError
    except ValueError:
        raise ManageError(
            "The route target must be a bare public HTTPS origin", code=code
        ) from None
    return route


def _proxy_dispatch(client, owner, args):
    if "shop_id" not in owner:
        raise ManageError("Refresh the pinned owner identity", code="invalid_response")
    shop = args.shop or owner["shop_id"]
    if not shop:
        raise ManageError("Select a shop explicitly with --shop", code="no_scope")
    _profile_shop(owner, shop)
    query = {"shop_id": shop}
    group, operation = args.proxy_command, args.operation
    path = PROXY_PREFIX + "/" + group
    if group == "identities" and operation == "create":
        value = client.request(
            owner, "POST", path, json={"name": args.name, "shop_id": shop}
        )
        if not isinstance(value, dict) or value.get("shop_id") != shop:
            raise ManageError(
                "Issuer response belongs to a different shop", code="invalid_response"
            )
        return business._finish(args, {"ok": True, "result": proxy_metadata(value)})
    if group == "routes" and operation in ("create", "import"):
        if operation == "create":
            body = {
                "name": args.name,
                "identity_id": args.identity,
                "origin": owner["origin"],
                "path": "/",
                "default_issuer": args.default_issuer,
                "shop_id": shop,
            }
        else:
            body = _public_proxy_route(business.read_json(args), input=True)
            if args.name:
                body["name"] = args.name
            body["shop_id"] = shop
        value = client.request(owner, "POST", path, json=body)
        if not isinstance(value, dict) or value.get("shop_id") != shop:
            raise ManageError(
                "Route response belongs to a different shop", code="invalid_response"
            )
        return business._finish(args, {"ok": True, "result": proxy_metadata(value)})
    rows = client.request(owner, "GET", path, params=query)
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or row.get("shop_id") != shop for row in rows
    ):
        raise ManageError(
            "Proxy list belongs to a different shop", code="invalid_response"
        )
    if operation == "list":
        return business._finish(args, {"ok": True, "result": proxy_metadata(rows)})
    row = next((item for item in rows if item.get("route_id") == args.id), None)
    if row is None:
        raise ManageError("Route is not in the selected shop", code="no_scope")
    if operation == "export":
        return business._finish(args, _public_proxy_route(row))
    if operation == "default" and not row.get("identity_id"):
        raise ManageError(
            "An imported route cannot issue local codes", code="invalid_input"
        )
    body = (
        {"default_issuer": not args.clear}
        if operation == "default"
        else {"enabled": operation == "enable"}
    )
    value = client.request(
        owner, "PUT", path + "/" + quote(args.id, safe=""), json=body, params=query
    )
    if (
        not isinstance(value, dict)
        or value.get("shop_id") != shop
        or value.get("route_id") != args.id
    ):
        raise ManageError(
            "Route update response does not match the selected route",
            code="invalid_response",
        )
    return business._finish(args, {"ok": True, "result": proxy_metadata(value)})

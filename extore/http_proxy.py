"""Explicit CLI proxy selection; unrelated environment settings stay disabled."""

import argparse
import ipaddress
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx


def _invalid():
    from .manage_client import ManageError

    raise ManageError("Invalid CLI proxy configuration", code="invalid_proxy") from None


def validate_proxy(value):
    try:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 8192
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
        ):
            _invalid()
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https", "socks5", "socks5h")
            or not parsed.hostname
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or parsed.port is not None
            and not 1 <= parsed.port <= 65535
        ):
            _invalid()
        httpx.URL(value)
    except (ValueError, httpx.InvalidURL):
        _invalid()
    return value


def proxy_argument(value):
    try:
        return validate_proxy(value)
    except Exception:
        raise argparse.ArgumentTypeError(
            "use an HTTP(S) or SOCKS5 proxy URL without a path, query or fragment"
        ) from None


def add_proxy_arguments(parser):
    """Suppressed child defaults retain flags provided before a subcommand."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--proxy",
        type=proxy_argument,
        default=argparse.SUPPRESS,
        help="explicit HTTP(S)/SOCKS5 proxy URL; never saved in the CLI profile",
    )
    group.add_argument(
        "--proxy-env",
        action="store_true",
        default=argparse.SUPPRESS,
        help="opt in to HTTP_PROXY, HTTPS_PROXY, ALL_PROXY and NO_PROXY",
    )
    children = next(
        (
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ),
        None,
    )
    if children:
        seen = set()
        for child in children.choices.values():
            if id(child) not in seen:
                seen.add(id(child))
                add_proxy_arguments(child)


@dataclass(frozen=True, slots=True)
class ProxySettings:
    mode: str = "direct"
    url: str | None = field(default=None, repr=False)
    routes: tuple = field(default=(), repr=False)


def _env(name, environ):
    # Lowercase values take precedence, including an explicit empty value.
    return environ.get(name.lower(), environ.get(name, ""))


def resolve_proxy(args=None, *, environ=None):
    environ = os.environ if environ is None else environ
    explicit = getattr(args, "proxy", None)
    use_env = getattr(args, "proxy_env", False)
    if explicit is not None and use_env:
        _invalid()
    if explicit is not None:
        return ProxySettings("explicit", validate_proxy(explicit))
    if not use_env:
        dedicated = environ.get("EXTORE_PROXY", "")
        return (
            ProxySettings("explicit", validate_proxy(dedicated))
            if dedicated
            else ProxySettings()
        )
    routes = {}
    for scheme in ("http", "https", "all"):
        value = _env(scheme.upper() + "_PROXY", environ)
        if value:
            routes[scheme + "://"] = validate_proxy(
                value if "://" in value else "http://" + value
            )
    bypass = _env("NO_PROXY", environ)
    if len(bypass) > 16384:
        _invalid()
    bypass_all = False
    for hostname in (part.strip() for part in bypass.split(",")):
        if not hostname:
            continue
        if hostname == "*":
            bypass_all = True
            continue
        if any(ord(char) <= 32 or ord(char) == 127 for char in hostname):
            _invalid()
        try:
            if "://" in hostname:
                parsed = urlsplit(hostname)
                if (
                    parsed.scheme not in ("http", "https")
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.path not in ("", "/")
                    or parsed.query
                    or parsed.fragment
                ):
                    _invalid()
                pattern = hostname.rstrip("/")
            else:
                if any(char in hostname for char in "@/?#"):
                    _invalid()
                try:
                    address = ipaddress.ip_address(hostname.strip("[]"))
                except ValueError:
                    address = None
                if address:
                    pattern = "all://" + (
                        "[" + str(address) + "]"
                        if address.version == 6
                        else str(address)
                    )
                elif hostname.lower() == "localhost":
                    pattern = "all://localhost"
                else:
                    pattern = "all://*" + hostname
            # Validate routing before any credential profile or key is created.
            from httpx._utils import URLPattern

            URLPattern(pattern)
        except (ValueError, httpx.InvalidURL):
            _invalid()
        routes[pattern] = None
    return ProxySettings(
        "environment", routes=() if bypass_all else tuple(routes.items())
    )


def make_client(settings=None, *, timeout=30, transport=None):
    settings = resolve_proxy() if settings is None else settings
    opened = []
    try:
        kwargs = {
            "timeout": timeout,
            "transport": transport,
            "verify": True,
            "follow_redirects": False,
            "trust_env": False,
        }
        if settings.url is not None:
            kwargs["proxy"] = settings.url
        if settings.routes:
            mounts = {}
            for pattern, value in settings.routes:
                if value is None:
                    mounts[pattern] = None
                else:
                    mount = httpx.HTTPTransport(
                        proxy=value, verify=True, trust_env=False
                    )
                    opened.append(mount)
                    mounts[pattern] = mount
            kwargs["mounts"] = mounts
        return httpx.Client(**kwargs)
    except (ValueError, ImportError, httpx.InvalidURL):
        for opened_transport in opened:
            opened_transport.close()
        _invalid()

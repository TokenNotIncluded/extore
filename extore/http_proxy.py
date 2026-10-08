"""Explicit CLI proxy selection; unrelated environment settings stay disabled."""

import argparse
import ipaddress
import logging
import os
import ssl
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import certifi
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
    parser.add_argument(
        "--ca-bundle",
        default=argparse.SUPPRESS,
        help="additional trusted PEM CAs for this CLI only (or EXTORE_CA_BUNDLE)",
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
    bypass_networks: tuple = field(default=(), repr=False)
    tls_context: ssl.SSLContext | None = field(default=None, repr=False, compare=False)


class _SocksAuthenticationFilter(logging.Filter):
    """HTTPcore's SOCKS start trace includes the complete auth tuple."""

    def filter(self, record):
        return not record.getMessage().startswith("setup_socks5_connection.started")


def _protect_proxy_logging(settings):
    values = [settings.url, *(value for _, value in settings.routes)]
    if any(value and value.startswith(("socks5://", "socks5h://")) for value in values):
        logger = logging.getLogger("httpcore.socks")
        if not any(
            isinstance(item, _SocksAuthenticationFilter) for item in logger.filters
        ):
            logger.addFilter(_SocksAuthenticationFilter())


def _env(name, environ):
    # Lowercase values take precedence, including an explicit empty value.
    return environ.get(name.lower(), environ.get(name, ""))


def _ca_context(args, environ):
    path = getattr(args, "ca_bundle", None)
    if path is None:
        path = environ.get("EXTORE_CA_BUNDLE", "")
    if not path:
        return None
    try:
        # Ambient SSL_CERT_FILE/DIR remain disabled. Add explicit trust locally.
        context = ssl.create_default_context(cafile=certifi.where())
        context.load_verify_locations(cafile=path)
        return context
    except (OSError, ValueError, TypeError):
        from .manage_client import ManageError

        raise ManageError(
            "Invalid CLI CA bundle configuration", code="invalid_ca_bundle"
        ) from None


class _CIDRClient(httpx.Client):
    """CIDR rules match literal IP hosts only; never perform local proxy DNS."""

    def __init__(self, *, bypass_networks, **kwargs):
        self._bypass_networks = bypass_networks
        super().__init__(**kwargs)

    def _transport_for_url(self, url):
        try:
            address = ipaddress.ip_address(url.host)
        except ValueError:
            pass
        else:
            if any(address in network for network in self._bypass_networks):
                return self._transport
        return super()._transport_for_url(url)


def resolve_proxy(args=None, *, environ=None):
    environ = os.environ if environ is None else environ
    tls_context = _ca_context(args, environ)
    explicit = getattr(args, "proxy", None)
    use_env = getattr(args, "proxy_env", False)
    if explicit is not None and use_env:
        _invalid()
    if explicit is not None:
        return ProxySettings(
            "explicit", validate_proxy(explicit), tls_context=tls_context
        )
    if not use_env:
        dedicated = environ.get("EXTORE_PROXY", "")
        return (
            ProxySettings(
                "explicit", validate_proxy(dedicated), tls_context=tls_context
            )
            if dedicated
            else ProxySettings(tls_context=tls_context)
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
    networks = []
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
            elif "/" in hostname:
                networks.append(ipaddress.ip_network(hostname, strict=False))
                continue
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
        "environment",
        routes=() if bypass_all else tuple(routes.items()),
        bypass_networks=() if bypass_all else tuple(networks),
        tls_context=tls_context,
    )


def make_client(settings=None, *, timeout=30, transport=None):
    settings = resolve_proxy() if settings is None else settings
    _protect_proxy_logging(settings)
    opened = []
    verify = settings.tls_context or True

    def proxy_url(value):
        if settings.tls_context is not None and urlsplit(value).scheme == "https":
            return httpx.Proxy(value, ssl_context=settings.tls_context)
        return value

    try:
        kwargs = {
            "timeout": timeout,
            "transport": transport,
            "verify": verify,
            "follow_redirects": False,
            "trust_env": False,
        }
        if settings.url is not None:
            kwargs["proxy"] = proxy_url(settings.url)
        if settings.routes:
            mounts = {}
            for pattern, value in settings.routes:
                if value is None:
                    mounts[pattern] = None
                else:
                    mount = httpx.HTTPTransport(
                        proxy=proxy_url(value), verify=verify, trust_env=False
                    )
                    opened.append(mount)
                    mounts[pattern] = mount
            kwargs["mounts"] = mounts
        if settings.bypass_networks:
            return _CIDRClient(bypass_networks=settings.bypass_networks, **kwargs)
        return httpx.Client(**kwargs)
    except (ValueError, ImportError, httpx.InvalidURL):
        for opened_transport in opened:
            opened_transport.close()
        _invalid()

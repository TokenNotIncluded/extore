"""A read-only, explicit product-data boundary for external sales platforms.

This contract describes an offer, not sale inventory or fulfillment contents.
Authorization belongs to the calling catalog endpoint; this module never reads
cards, jobs, processor profiles, credentials, or customer data.
"""

import hashlib
import json
import math
import re
from urllib.parse import parse_qsl, urlsplit

from .config import ORIGIN
from .service import product
from .variants import default_variant

LISTING_SCHEMA = "extore.product-listing.v1"
SEMANTICS = {
    "price": "reference",
    "inventory": "not_exported",
    "payment": "external_sales_platform",
    "redemption": "extore",
}
_SECRET_NAMES = frozenset(
    {
        "api",
        "key",
        "jwt",
        "sig",
        "auth",
        "secret",
        "token",
        "password",
        "authorization",
        "api_key",
        "apikey",
        "access_token",
        "signature",
        "credential",
        "credentials",
        "client_secret",
        "webhook_secret",
        "authorization_token",
        "card_code",
        "redemption_code",
        "receipt_token",
        "management_token",
    }
)
_RESERVED_NAMES = frozenset({"__proto__", "constructor", "prototype"})
_LANGUAGE = re.compile(r"[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*\Z")
_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}\Z")
_FIELD_KEY = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_OPTION_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_FIELD_TYPES = frozenset(
    {
        "text",
        "email",
        "url",
        "textarea",
        "number",
        "file",
        "select",
        "boolean",
        "image",
        "images",
    }
)
_UNSET = object()


def _text(value, limit):
    if (
        isinstance(value, str)
        and len(value) <= limit
        and not any("\ud800" <= char <= "\udfff" for char in value)
    ):
        return value
    return _UNSET


def _secret_name(value):
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value).lower()
    return name in _SECRET_NAMES or bool(
        re.search(
            r"(?:^|[_-])(?:key|token|secret|password|signature|credential|authorization)(?:$|[_-])",
            name,
        )
    )


def _localized(value, limit):
    if isinstance(value, str):
        return _text(value, limit)
    if not isinstance(value, dict):
        return _UNSET
    return {
        key: text
        for key, content in value.items()
        if isinstance(key, str)
        and _LANGUAGE.fullmatch(key)
        and not _secret_name(key)
        and (text := _text(content, limit)) is not _UNSET
    }


def _assign(target, key, value):
    if value is not _UNSET:
        target[key] = value


def _asset_url(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2000
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
    ):
        return _UNSET
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            return _UNSET
        # Validate malformed ports without making any network or DNS request.
        parsed.port
        if any(
            _secret_name(key)
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        ):
            return _UNSET
    except ValueError:
        return _UNSET
    return _text(value, 2000)


def _reference_price(value):
    """Validate persisted decimal text without normalizing its representation."""
    if (
        not isinstance(value, str)
        or len(value) > 100
        or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,6})?", value)
    ):
        return None
    integer = value.partition(".")[0].lstrip("0") or "0"
    return value if len(integer) <= 12 else None


def _attributes(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for key, content in value.items():
        if (
            _text(key, 100) is _UNSET
            or not key.strip()
            or _secret_name(key)
            or key in _RESERVED_NAMES
        ):
            continue
        if content is None or isinstance(content, bool):
            pass
        elif isinstance(content, str):
            if _text(content, 1000) is _UNSET:
                continue
        elif isinstance(content, (int, float)):
            if (
                isinstance(content, float)
                and not math.isfinite(content)
                or (
                    isinstance(content, int)
                    or isinstance(content, float)
                    and content.is_integer()
                )
                and abs(content) > 9007199254740991
            ):
                continue
        else:
            continue
        result[key] = content
        if len(result) == 20:
            break
    return result


def safe_variants(values):
    """Return only SKU presentation fields; also reusable for issued SKU data."""
    if not isinstance(values, list):
        return []
    result = []
    for value in values[:100]:
        if not isinstance(value, dict):
            continue
        variant = {"attributes": _attributes(value.get("attributes"))}
        variant_id = value.get("id")
        if isinstance(variant_id, str) and _SLUG.fullmatch(variant_id):
            variant["id"] = variant_id
        name = _text(value.get("name"), 120)
        if name is not _UNSET and name.strip():
            variant["name"] = name
        _assign(variant, "description", _text(value.get("description"), 10000))
        # Unknown prices are explicit null, never zero or a guessed amount.
        variant["price"] = _reference_price(value.get("price"))
        currency = value.get("currency")
        if isinstance(currency, str) and re.fullmatch(r"[A-Z]{3,5}", currency):
            variant["currency"] = currency
        if isinstance(value.get("enabled"), bool):
            variant["enabled"] = value["enabled"]
        result.append(variant)
    return result


def _fields(values):
    if not isinstance(values, list):
        return []
    result = []
    for value in values[:30]:
        if not isinstance(value, dict):
            continue
        field = {}
        key = value.get("key")
        if isinstance(key, str) and _FIELD_KEY.fullmatch(key):
            field["key"] = key
        _assign(field, "label", _localized(value.get("label"), 200))
        _assign(field, "description", _localized(value.get("description"), 10000))
        kind = value.get("type")
        if isinstance(kind, str) and kind in _FIELD_TYPES:
            field["type"] = kind
        for key in ("required", "collapsed"):
            if isinstance(value.get(key), bool):
                field[key] = value[key]
        if kind == "select" and isinstance(value.get("options"), list):
            options = []
            for option in value["options"][:100]:
                if (
                    not isinstance(option, dict)
                    or not isinstance(option.get("value"), str)
                    or not _OPTION_VALUE.fullmatch(option["value"])
                ):
                    continue
                item = {"value": option["value"]}
                _assign(item, "label", _localized(option.get("label"), 200))
                options.append(item)
            field["options"] = options
        if kind == "images":
            limit = value.get("max_items", 10)
            if type(limit) is int and 1 <= limit <= 20:
                field["max_items"] = limit
        result.append(field)
    return result


def _progress_steps(values):
    if not isinstance(values, list):
        return []
    result = []
    for value in values[:30]:
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("id"), str)
            or not _SLUG.fullmatch(value["id"])
        ):
            continue
        step = {"id": value["id"]}
        _assign(step, "label", _localized(value.get("label"), 200))
        result.append(step)
    return result


def _identity(value, name):
    if (
        _text(value, 100) is _UNSET
        or not value
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"Invalid {name}")
    return value


def _redemption_url(origin):
    if _text(origin, 2000) is _UNSET or any(
        ord(char) <= 32 or ord(char) == 127 for char in origin
    ):
        raise ValueError("Invalid redemption origin")
    try:
        parsed = urlsplit(origin)
        parsed.port
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or parsed.scheme != "https"
            and not (
                parsed.scheme == "http"
                and parsed.hostname in ("localhost", "127.0.0.1", "::1")
            )
        ):
            raise ValueError("Invalid redemption origin")
    except ValueError as exc:
        raise ValueError("Invalid redemption origin") from exc
    return origin.rstrip("/") + "/"


def listing_data(source, *, origin, shop_id):
    """Filter an authorized product as plain data and compute its revision."""
    if not isinstance(source, dict):
        raise ValueError("Invalid product data")
    safe = {}
    _assign(safe, "name", _localized(source.get("name"), 120))
    _assign(safe, "description", _localized(source.get("description"), 20000))
    for key in ("logo", "image"):
        _assign(safe, key, _asset_url(source.get(key)))
    if isinstance(source.get("public"), bool):
        safe["public"] = source["public"]
    for key, choices in (
        ("mode", ("manual", "webhook", "script", "stock")),
        ("delivery", ("content", "service")),
        ("view_policy", ("repeat", "once")),
    ):
        if source.get(key) in choices:
            safe[key] = source[key]
    safe["parameters"] = _fields(source.get("parameters"))
    safe["outputs"] = _fields(source.get("outputs"))
    safe["progress_steps"] = _progress_steps(source.get("progress_steps"))
    support_email = source.get("support_email")
    if _text(support_email, 254) is not _UNSET and (
        not support_email or re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", support_email)
    ):
        safe["support_email"] = support_email
    policy = source.get("revision_policy", _UNSET)
    if policy is None:
        safe["revision_policy"] = None
    elif isinstance(policy, dict):
        key = policy.get("attribute_key")
        if (
            _text(key, 100) is not _UNSET
            and key.strip()
            and not _secret_name(key)
            and key not in _RESERVED_NAMES
        ):
            safe["revision_policy"] = {"attribute_key": key}
            _assign(
                safe["revision_policy"], "label", _localized(policy.get("label"), 200)
            )
    result = {
        "schema": LISTING_SCHEMA,
        "id": _identity(source.get("id"), "product ID"),
        "shop_id": _identity(shop_id, "shop ID"),
        "redemption_url": _redemption_url(origin),
        "semantics": dict(SEMANTICS),
        "product": safe,
        "variants": safe_variants(source.get("variants", [default_variant()])),
    }
    canonical = json.dumps(
        result,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    result["revision"] = hashlib.sha256(canonical).hexdigest()
    return result


def commerce_listing(c, pid, *, origin=ORIGIN):
    """Read the product and its real tenant ID, never owner configuration."""
    # Both identities come from the authorized database lookup, even if an old
    # imported config happens to contain its own unrelated identity fields.
    values = {**product(c, pid), "id": pid}
    row = c.execute("SELECT shop_id FROM products WHERE id=?", (pid,)).fetchone()
    return listing_data(values, origin=origin, shop_id=row["shop_id"])

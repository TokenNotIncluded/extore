"""Bounded, server-side client for Extore's open commerce import protocol.

The client never persists credentials, follows redirects, or retries a token
exchange/refresh. Callers own durable, encrypted storage and interprocess locks.
"""

import base64
import copy
import hashlib
import hmac
import json
import math
import re
import secrets
import threading
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import httpx

from .commerce_models import redirect_uri
from .http_proxy import make_client

SCOPES = frozenset({"products.read", "cards.issue"})
MAX_CATALOG_BYTES = 16 * 1024 * 1024
MAX_TOKEN_BYTES = 64 * 1024
MAX_CARD_BYTES = 2 * 1024 * 1024
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_ERRORS = {
    "invalid_request": "The commerce request is invalid",
    "invalid_client": "The registered commerce client is invalid",
    "invalid_grant": "The authorization expired, was consumed, or was revoked",
    "invalid_scope": "The requested commerce scope is invalid",
    "unsupported_grant_type": "The grant type is unsupported",
    "access_denied": "The merchant declined authorization",
    "unsupported_response_type": "Only authorization-code responses are supported",
    "invalid_token": "The commerce session expired or was revoked",
    "insufficient_scope": "The grant does not permit this operation",
    "quota_exceeded": "The approved issuance allowance is exhausted",
    "catalog_changed": "The product changed; review its current listing",
    "idempotency_conflict": "This idempotency key belongs to a different request",
    "issuance_expired": "The recovery response expired; do not issue replacement codes automatically",
    "product_unavailable": "The product is unavailable in this shop",
    "variant_unavailable": "The approved variant is unavailable",
    "unsupported_product": "This product cannot issue stock through the commerce protocol",
    "review_changed": "The consent data changed; review it again",
    "temporarily_unavailable": "The operation is temporarily unavailable; preserve the original request",
    "not_found": "The approved resource is unavailable",
}


class CommerceError(Exception):
    """A sanitized error. Server bodies and request credentials are never stored."""

    def __init__(self, code, *, status=None):
        self.code = (
            code if isinstance(code, str) and code in _ERRORS else "commerce_error"
        )
        self.status = status
        super().__init__(_ERRORS.get(self.code, "The commerce operation failed"))


def _invalid():
    raise ValueError("Invalid commerce client input") from None


def _origin(value):
    try:
        if not isinstance(value, str) or any(ord(c) <= 32 for c in value):
            _invalid()
        parsed = urlsplit(value)
        port = parsed.port
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or port is not None
            and not 1 <= port <= 65535
        ):
            _invalid()
        return str(
            httpx.URL(value).copy_with(path="", query=None, fragment=None)
        ).rstrip("/")
    except (ValueError, httpx.InvalidURL):
        _invalid()


def _redirect(value):
    try:
        return redirect_uri(value)
    except (ValueError, TypeError, AttributeError):
        _invalid()


def _scopes(values):
    if isinstance(values, str):
        values = values.split()
    try:
        result = tuple(sorted(set(values)))
    except (TypeError, ValueError):
        _invalid()
    if not result or any(value not in SCOPES for value in result):
        _invalid()
    return result


def _identifier(value):
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        _invalid()
    return value


def _credential(value):
    if (
        not isinstance(value, str)
        or not 16 <= len(value) <= 4096
        or any(ord(c) < 33 or ord(c) > 126 for c in value)
    ):
        _invalid()
    return value


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("Invalid JSON number")


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    issuer: str
    client_id: str
    redirect_uri: str = field(repr=False)
    scopes: tuple[str, ...]
    url: str = field(repr=False)
    state: str = field(repr=False)
    code_verifier: str = field(repr=False)
    _consumed: bool = field(default=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class TokenSet:
    issuer: str
    client_id: str
    grant_id: str
    scope: tuple[str, ...]
    expires_in: int
    grant_expires: float
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    _refresh_consumed: bool = field(default=False, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class CardBatch:
    """Codes only appear through an explicit attribute or export call."""

    grant_id: str
    product_id: str
    variant_id: str
    batch_id: str
    count: int
    created_at: float
    recovery_expires: float
    quota: dict = field(repr=False)
    variant: dict = field(repr=False)
    codes: tuple[str, ...] = field(repr=False)

    def export(self):
        """Explicit confidential export; encrypt/store it before sending any card."""
        return {
            "schema": "extore.card-batch.v1",
            "grant_id": self.grant_id,
            "product_id": self.product_id,
            "variant_id": self.variant_id,
            "batch_id": self.batch_id,
            "count": self.count,
            "created_at": self.created_at,
            "recovery_expires": self.recovery_expires,
            "quota": copy.deepcopy(self.quota),
            "variant": copy.deepcopy(self.variant),
            "codes": list(self.codes),
        }


class CommerceClient:
    def __init__(self, issuer, client_id, *, proxy_settings=None, transport=None):
        self.issuer = _origin(issuer)
        self.client_id = _identifier(client_id)
        self.http = make_client(proxy_settings, timeout=30, transport=transport)
        self._lock = threading.Lock()
        self._metadata = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.http.close()

    def _request(
        self, method, path, *, tokens=None, limit=MAX_TOKEN_BYTES, empty=False, **kwargs
    ):
        headers = dict(kwargs.pop("headers", {}))
        if tokens is not None:
            self._bound_tokens(tokens)
            headers["Authorization"] = "Bearer " + tokens.access_token
        headers["Accept"] = "application/json"
        try:
            self.http.cookies.clear()
            with self.http.stream(
                method, self.issuer + path, headers=headers, **kwargs
            ) as response:
                if 300 <= response.status_code < 400:
                    raise CommerceError("commerce_error", status=response.status_code)
                raw = bytearray()
                for chunk in response.iter_bytes(64 * 1024):
                    raw.extend(chunk)
                    if len(raw) > limit:
                        raise CommerceError(
                            "commerce_error", status=response.status_code
                        )
                if empty and 200 <= response.status_code < 300 and not raw:
                    return None
                try:
                    result = json.loads(
                        raw, object_pairs_hook=_pairs, parse_constant=_constant
                    )
                    if not isinstance(result, dict):
                        raise ValueError
                except (ValueError, UnicodeError, RecursionError):
                    raise CommerceError(
                        "commerce_error", status=response.status_code
                    ) from None
                if not 200 <= response.status_code < 300:
                    raise CommerceError(
                        result.get("error"), status=response.status_code
                    )
                return result
        except httpx.HTTPError:
            raise CommerceError("commerce_error") from None

    def metadata(self, *, refresh=False):
        if self._metadata is None or refresh:
            result = self._request("GET", "/.well-known/oauth-authorization-server")
            expected = {
                "issuer": self.issuer,
                "authorization_endpoint": self.issuer + "/oauth/authorize",
                "token_endpoint": self.issuer + "/api/integrations/commerce/token",
                "revocation_endpoint": self.issuer
                + "/api/integrations/commerce/revoke",
            }
            if any(result.get(name) != value for name, value in expected.items()):
                raise CommerceError("commerce_error")
            for name, required in (
                ("response_types_supported", {"code"}),
                ("grant_types_supported", {"authorization_code", "refresh_token"}),
                ("code_challenge_methods_supported", {"S256"}),
                ("token_endpoint_auth_methods_supported", {"none"}),
                ("scopes_supported", SCOPES),
            ):
                value = result.get(name)
                if (
                    not isinstance(value, list)
                    or any(not isinstance(item, str) for item in value)
                    or not required.issubset(set(value))
                ):
                    raise CommerceError("commerce_error")
            if result.get("authorization_response_iss_parameter_supported") is not True:
                raise CommerceError("commerce_error")
            self._metadata = result
        return copy.deepcopy(self._metadata)

    def authorize(self, redirect_uri, *, scopes=("products.read",), product_ids=()):
        """Create a server-session transaction. Do not put its verifier in a browser."""
        redirect_uri = _redirect(redirect_uri)
        scopes = _scopes(scopes)
        if not isinstance(product_ids, (tuple, list)) or len(product_ids) > 100:
            _invalid()
        product_ids = tuple(_identifier(value) for value in product_ids)
        if len(set(product_ids)) != len(product_ids):
            _invalid()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .decode("ascii")
            .rstrip("=")
        )
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if product_ids:
            params["product_ids"] = ",".join(product_ids)
        return AuthorizationRequest(
            self.issuer,
            self.client_id,
            redirect_uri,
            scopes,
            self.issuer + "/oauth/authorize?" + urlencode(params),
            state,
            verifier,
        )

    def _callback_code(self, transaction, callback_url):
        if (
            not isinstance(transaction, AuthorizationRequest)
            or transaction.issuer != self.issuer
            or transaction.client_id != self.client_id
            or transaction._consumed
        ):
            _invalid()
        try:
            callback, redirect = (
                urlsplit(callback_url),
                urlsplit(transaction.redirect_uri),
            )
            if (
                len(callback_url) > 8192
                or callback.fragment
                or any(ord(c) <= 32 for c in callback_url)
            ):
                _invalid()
            if _origin(f"{callback.scheme}://{callback.netloc}") != _origin(
                f"{redirect.scheme}://{redirect.netloc}"
            ) or (callback.path or "/") != (redirect.path or "/"):
                _invalid()
            values = _pairs(parse_qsl(callback.query, keep_blank_values=True))
            original = _pairs(parse_qsl(redirect.query, keep_blank_values=True))
            if (
                any(values.get(name) != value for name, value in original.items())
                or not hmac.compare_digest(values.get("state", ""), transaction.state)
                or values.get("iss") != self.issuer
            ):
                _invalid()
            if bool(values.get("code")) == bool(values.get("error")):
                _invalid()
            if "error" in values:
                raise CommerceError(values["error"])
            return _credential(values["code"])
        except (ValueError, TypeError, KeyError, AttributeError):
            _invalid()

    def validate_callback(self, transaction, callback_url):
        """Pure validation for durable clients; no secrets returned or request sent."""
        self._callback_code(transaction, callback_url)

    def complete_authorization(self, transaction, callback_url):
        """Validate callback target, state and issuer before a one-time code exchange."""
        with self._lock:
            try:
                code = self._callback_code(transaction, callback_url)
            except CommerceError:
                object.__setattr__(transaction, "_consumed", True)
                raise
            object.__setattr__(transaction, "_consumed", True)
            result = self._request(
                "POST",
                "/api/integrations/commerce/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": transaction.redirect_uri,
                    "client_id": self.client_id,
                    "code_verifier": transaction.code_verifier,
                },
            )
            return self._tokens(result, allowed_scopes=transaction.scopes)

    def _bound_tokens(self, tokens):
        if (
            not isinstance(tokens, TokenSet)
            or tokens.issuer != self.issuer
            or tokens.client_id != self.client_id
        ):
            _invalid()
        _credential(tokens.access_token)
        _credential(tokens.refresh_token)

    def _tokens(self, result, *, allowed_scopes):
        try:
            if not isinstance(result["scope"], str):
                raise ValueError
            scope = _scopes(result["scope"])
            expires = result["expires_in"]
            grant_expires = result["grant_expires"]
            if (
                result["token_type"] != "Bearer"
                or not set(scope).issubset(allowed_scopes)
                or type(expires) is not int
                or not 1 <= expires <= 900
                or type(grant_expires) not in (int, float)
                or not math.isfinite(grant_expires)
                or grant_expires <= 0
            ):
                raise ValueError
            return TokenSet(
                self.issuer,
                self.client_id,
                _identifier(result["grant_id"]),
                scope,
                expires,
                grant_expires,
                _credential(result["access_token"]),
                _credential(result["refresh_token"]),
            )
        except (ValueError, TypeError, KeyError):
            raise CommerceError("commerce_error") from None

    def refresh(self, tokens):
        """Rotate once. Hold your durable credential lock and save the new pair atomically."""
        with self._lock:
            self._bound_tokens(tokens)
            if tokens._refresh_consumed:
                _invalid()
            # Even a lost response is uncertain: do not replay and revoke the grant.
            object.__setattr__(tokens, "_refresh_consumed", True)
            result = self._request(
                "POST",
                "/api/integrations/commerce/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": tokens.refresh_token,
                    "client_id": self.client_id,
                },
            )
            updated = self._tokens(result, allowed_scopes=tokens.scope)
            if (
                updated.grant_id != tokens.grant_id
                or updated.refresh_token == tokens.refresh_token
            ):
                raise CommerceError("commerce_error")
            return updated

    def revoke(self, tokens):
        self._bound_tokens(tokens)
        return self._request(
            "POST",
            "/api/integrations/commerce/revoke",
            data={
                "token": tokens.refresh_token,
                "client_id": self.client_id,
                "token_type_hint": "refresh_token",
            },
            empty=True,
        )

    def products(self, tokens):
        result = self._request(
            "GET",
            "/api/integrations/commerce/products",
            tokens=tokens,
            limit=MAX_CATALOG_BYTES,
        )
        if (
            result.get("schema") != "extore.commerce-catalog.v1"
            or result.get("issuer") != self.issuer
            or result.get("grant_id") != tokens.grant_id
            or not isinstance(result.get("products"), list)
            or len(result["products"]) > 100
            or not isinstance(result.get("shop"), dict)
            or not isinstance(result["shop"].get("id"), str)
            or not isinstance(result["shop"].get("name"), str)
        ):
            raise CommerceError("commerce_error")
        identities = set()
        for product in result["products"]:
            self._listing(product)
            if (
                product["shop_id"] != result["shop"]["id"]
                or product["id"] in identities
            ):
                raise CommerceError("commerce_error")
            identities.add(product["id"])
        return result

    @staticmethod
    def _listing(result):
        if (
            not isinstance(result, dict)
            or result.get("schema") != "extore.product-listing.v1"
            or not isinstance(result.get("product"), dict)
            or not isinstance(result.get("variants"), list)
            or len(result["variants"]) > 100
            or not isinstance(result.get("id"), str)
            or not isinstance(result.get("shop_id"), str)
            or not isinstance(result.get("revision"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", result["revision"])
            or not isinstance(result.get("redemption_url"), str)
            or not isinstance(result.get("semantics"), dict)
            or any(
                result["semantics"].get(key) != value
                for key, value in {
                    "price": "reference",
                    "inventory": "not_exported",
                    "payment": "external_sales_platform",
                    "redemption": "extore",
                }.items()
            )
        ):
            raise CommerceError("commerce_error")
        for variant in result["variants"]:
            CommerceClient._variant(variant)
        return result

    @staticmethod
    def _variant(variant):
        if (
            not isinstance(variant, dict)
            or not isinstance(variant.get("id"), str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", variant["id"])
            or not isinstance(variant.get("attributes"), dict)
        ):
            raise CommerceError("commerce_error")
        price = variant.get("price")
        if price is not None and (
            not isinstance(price, str)
            or len(price) > 100
            or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,6})?", price)
            or len(price.partition(".")[0].lstrip("0") or "0") > 12
        ):
            raise CommerceError("commerce_error")

    def product(self, tokens, product_id):
        product_id = _identifier(product_id)
        result = self._listing(
            self._request(
                "GET",
                "/api/integrations/commerce/products/" + quote(product_id, safe=""),
                tokens=tokens,
                limit=MAX_CATALOG_BYTES,
            )
        )
        if result["id"] != product_id:
            raise CommerceError("commerce_error")
        return result

    def issue_cards(
        self,
        tokens,
        product_id,
        variant_id,
        count,
        *,
        idempotency_key,
        expected_revision=None,
        label=None,
        expires=None,
    ):
        """Mint one approved batch; persist the key/body before the request, codes afterwards."""
        product_id, variant_id = _identifier(product_id), _identifier(variant_id)
        if (
            type(count) is not int
            or not 1 <= count <= 100
            or not isinstance(idempotency_key, str)
            or not 8 <= len(idempotency_key) <= 200
            or any(ord(c) < 33 or ord(c) > 126 for c in idempotency_key)
        ):
            _invalid()
        body = {"product_id": product_id, "variant_id": variant_id, "count": count}
        if expected_revision is not None:
            if not isinstance(expected_revision, str) or not re.fullmatch(
                r"[a-f0-9]{64}", expected_revision
            ):
                _invalid()
            body["expected_revision"] = expected_revision
        if label is not None:
            if (
                not isinstance(label, str)
                or len(label) > 100
                or any(ord(c) < 32 for c in label)
            ):
                _invalid()
            body["label"] = label
        if expires is not None:
            if (
                type(expires) not in (int, float)
                or not math.isfinite(expires)
                or expires <= 0
            ):
                _invalid()
            body["expires"] = expires
        result = self._request(
            "POST",
            "/api/integrations/commerce/cards",
            tokens=tokens,
            headers={"Idempotency-Key": idempotency_key},
            json=body,
            limit=MAX_CARD_BYTES,
        )
        try:
            codes = result["codes"]
            if (
                result["schema"] != "extore.card-batch.v1"
                or result["grant_id"] != tokens.grant_id
                or result["product_id"] != product_id
                or result["variant_id"] != variant_id
                or type(result["count"]) is not int
                or result["count"] != count
                or not isinstance(codes, list)
                or len(codes) != count
                or len(set(codes)) != count
                or not isinstance(result["quota"], dict)
                or not isinstance(result["variant"], dict)
            ):
                raise ValueError
            codes = tuple(_credential(code) for code in codes)
            quota = result["quota"]
            if (
                any(
                    type(quota.get(name)) is not int or not 0 <= quota[name] <= 10000
                    for name in ("max_count", "issued_count", "remaining")
                )
                or quota["max_count"] < 1
                or quota["issued_count"] < count
                or quota["remaining"] != quota["max_count"] - quota["issued_count"]
            ):
                raise ValueError
            self._variant(result["variant"])
            if result["variant"]["id"] != variant_id:
                raise ValueError
            for name in ("created_at", "recovery_expires"):
                value = result[name]
                if (
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or value <= 0
                ):
                    raise ValueError
            if result["recovery_expires"] <= result["created_at"]:
                raise ValueError
            return CardBatch(
                tokens.grant_id,
                product_id,
                variant_id,
                _identifier(result["batch_id"]),
                count,
                result["created_at"],
                result["recovery_expires"],
                copy.deepcopy(result["quota"]),
                copy.deepcopy(result["variant"]),
                codes,
            )
        except (ValueError, TypeError, KeyError):
            raise CommerceError("commerce_error") from None

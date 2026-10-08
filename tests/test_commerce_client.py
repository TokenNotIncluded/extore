"""Commerce SDK wire-contract checks against synthetic, local HTTP transports."""

import base64
import copy
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
import pytest

from extore import commerce_client as sdk
from extore import http_proxy
from extore.http_proxy import ProxySettings

ISSUER = "https://extore.example.test"
CLIENT_ID = "synthetic-storefront"
REDIRECT = "https://shop.example.test/extore/callback?tenant=synthetic"
ACCESS = "synthetic-access-token-000000000000"
REFRESH = "synthetic-refresh-token-00000000000"
CODE = "synthetic-authorization-code-00000"
CARD = "SYNTHETIC-CARD-ONE-000000000000000"
REVISION = "a" * 64


def metadata_payload():
    return {
        "issuer": ISSUER,
        "authorization_endpoint": ISSUER + "/oauth/authorize",
        "token_endpoint": ISSUER + "/api/integrations/commerce/token",
        "revocation_endpoint": ISSUER + "/api/integrations/commerce/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["products.read", "cards.issue"],
        "authorization_response_iss_parameter_supported": True,
    }


def token_payload(**changes):
    return {
        "access_token": ACCESS,
        "refresh_token": REFRESH,
        "token_type": "Bearer",
        "expires_in": 900,
        "scope": "cards.issue products.read",
        "grant_id": "synthetic-grant",
        "grant_expires": 2000000000,
        **changes,
    }


def tokens(*, issuer=ISSUER, client_id=CLIENT_ID):
    return sdk.TokenSet(
        issuer,
        client_id,
        "synthetic-grant",
        ("cards.issue", "products.read"),
        900,
        2000000000,
        ACCESS,
        REFRESH,
    )


def listing_payload():
    return {
        "schema": "extore.product-listing.v1",
        "id": "synthetic-product",
        "shop_id": "synthetic-shop",
        "revision": REVISION,
        "redemption_url": ISSUER + "/",
        "semantics": {
            "price": "reference",
            "inventory": "not_exported",
            "payment": "external_sales_platform",
            "redemption": "extore",
        },
        "product": {
            "name": "Synthetic draft",
            "parameters": [],
            "outputs": [],
            "progress_steps": [],
        },
        "variants": [
            {"id": "standard", "name": "Standard", "price": "25.00", "attributes": {}},
            {"id": "custom", "name": "Custom", "price": None, "attributes": {}},
        ],
    }


def catalog_payload():
    return {
        "schema": "extore.commerce-catalog.v1",
        "issuer": ISSUER,
        "grant_id": "synthetic-grant",
        "shop": {"id": "synthetic-shop", "name": "Synthetic shop"},
        "products": [listing_payload()],
    }


def cards_payload(**changes):
    return {
        "schema": "extore.card-batch.v1",
        "grant_id": "synthetic-grant",
        "product_id": "synthetic-product",
        "variant_id": "standard",
        "batch_id": "synthetic-batch",
        "count": 1,
        "codes": [CARD],
        "created_at": 1900000000,
        "recovery_expires": 1900000300,
        "quota": {"max_count": 10, "issued_count": 1, "remaining": 9},
        "variant": {
            "id": "standard",
            "name": "Standard",
            "price": "25.00",
            "attributes": {},
        },
        **changes,
    }


@pytest.fixture
def client_factory():
    clients = []

    def create(handler):
        client = sdk.CommerceClient(
            ISSUER,
            CLIENT_ID,
            proxy_settings=ProxySettings(),
            transport=httpx.MockTransport(handler),
        )
        clients.append(client)
        return client

    yield create
    for client in clients:
        client.close()


def callback_url(transaction, **changes):
    values = {
        "tenant": "synthetic",
        "state": transaction.state,
        "iss": ISSUER,
        "code": CODE,
    }
    for key, value in changes.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    return REDIRECT.split("?", 1)[0] + "?" + urlencode(values)


def issue(client, **changes):
    values = {
        "product_id": "synthetic-product",
        "variant_id": "standard",
        "count": 1,
        "idempotency_key": "synthetic-order-00001",
        "expected_revision": REVISION,
    }
    values.update(changes)
    return client.issue_cards(tokens(), **values)


@pytest.mark.parametrize(
    "origin",
    [
        "http://extore.example.test",
        "https://user:password@extore.example.test",
        "https://extore.example.test/other",
        "https://extore.example.test?redirect=https://other.example.test",
        "https://extore.example.test/#fragment",
    ],
)
def test_constructor_rejects_non_origin_and_non_https_urls(origin):
    with pytest.raises(ValueError, match="Invalid commerce client input"):
        sdk.CommerceClient(origin, CLIENT_ID, proxy_settings=ProxySettings())


@pytest.mark.parametrize(
    "redirect",
    [
        "http://shop.example.test/callback",
        "https://shop.example.test/callback#token",
        "https://shop.example.test/callback?state=preseeded",
        "https://127.0.0.1/callback",
        "https://shop.internal/callback",
    ],
)
def test_authorize_rejects_unsafe_registered_redirects(client_factory, redirect):
    client = client_factory(lambda request: pytest.fail("No network during authorize"))
    with pytest.raises(ValueError):
        client.authorize(redirect)


@pytest.mark.parametrize(
    "field",
    ["issuer", "authorization_endpoint", "token_endpoint", "revocation_endpoint"],
)
def test_metadata_cannot_replace_pinned_issuer_or_endpoints(client_factory, field):
    payload = metadata_payload()
    payload[field] = "https://untrusted.example.test/collect"
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=payload)

    client = client_factory(handler)
    with pytest.raises(sdk.CommerceError):
        client.metadata()
    assert len(calls) == 1
    assert str(calls[0].url) == ISSUER + "/.well-known/oauth-authorization-server"


def test_metadata_is_cached_and_caller_mutation_cannot_change_trust(client_factory):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=metadata_payload())

    client = client_factory(handler)
    result = client.metadata()
    result["issuer"] = "https://untrusted.example.test"
    result["scopes_supported"].clear()
    assert client.metadata() == metadata_payload()
    assert len(calls) == 1
    assert client.metadata(refresh=True) == metadata_payload()
    assert len(calls) == 2


@pytest.mark.parametrize("capability", [[], [{"not": "a capability"}]])
def test_malformed_metadata_capabilities_fail_as_sanitized_errors(
    client_factory, capability
):
    payload = metadata_payload()
    payload["code_challenge_methods_supported"] = capability
    client = client_factory(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(sdk.CommerceError):
        client.metadata()


@pytest.mark.parametrize(
    "body",
    [
        '{"issuer":"https://extore.example.test","issuer":"https://evil.test"}',
        '{"expires_in":NaN}',
        '[{"issuer":"https://extore.example.test"}]',
    ],
)
def test_untrusted_duplicate_nonfinite_and_nonobject_json_are_rejected(
    client_factory, body
):
    client = client_factory(lambda request: httpx.Response(200, content=body))
    with pytest.raises(sdk.CommerceError):
        client.metadata()


def test_redirect_is_not_followed_even_to_same_origin(client_factory):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={"Location": ISSUER + "/redirected"})

    client = client_factory(handler)
    with pytest.raises(sdk.CommerceError) as error:
        client.metadata()
    assert error.value.status == 307
    assert len(calls) == 1


def test_catalog_and_card_response_limits_are_enforced(client_factory, monkeypatch):
    monkeypatch.setattr(sdk, "MAX_CATALOG_BYTES", 1024)
    monkeypatch.setattr(sdk, "MAX_CARD_BYTES", 1024)
    calls = []

    def handler(request):
        calls.append(request)
        limit = (
            sdk.MAX_TOKEN_BYTES
            if request.url.path == "/.well-known/oauth-authorization-server"
            else 1024
        )
        return httpx.Response(200, content=b" " * (limit + 1))

    client = client_factory(handler)
    with pytest.raises(sdk.CommerceError):
        client.products(tokens())
    with pytest.raises(sdk.CommerceError):
        issue(client)
    with pytest.raises(sdk.CommerceError):
        client.metadata()
    assert len(calls) == 3


def test_cookie_credentials_are_not_forwarded_between_api_requests(client_factory):
    calls = []

    def handler(request):
        calls.append(request)
        assert "cookie" not in request.headers
        return httpx.Response(
            200,
            json=metadata_payload(),
            headers={"Set-Cookie": "synthetic_session=not-a-real-session; Path=/"},
        )

    client = client_factory(handler)
    client.http.cookies.set("unrelated", "synthetic")
    client.metadata()
    client.metadata(refresh=True)
    assert len(calls) == 2


def test_authorization_uses_independent_random_state_and_s256_pkce(client_factory):
    client = client_factory(lambda request: pytest.fail("Authorize must be offline"))
    first = client.authorize(
        REDIRECT,
        scopes=("products.read", "cards.issue", "products.read"),
        product_ids=("synthetic-product", "synthetic-second"),
    )
    second = client.authorize(REDIRECT)
    values = dict(parse_qsl(urlsplit(first.url).query))
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(first.code_verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert values == {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT,
        "scope": "cards.issue products.read",
        "state": first.state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "product_ids": "synthetic-product,synthetic-second",
    }
    assert urlsplit(first.url).netloc == "extore.example.test"
    assert first.state != second.state
    assert first.code_verifier != second.code_verifier
    assert 43 <= len(first.code_verifier) <= 128
    assert len(first.state) >= 43
    assert first.state not in repr(first)
    assert first.code_verifier not in repr(first)
    assert REDIRECT not in repr(first)


@pytest.mark.parametrize(
    "change",
    [
        {"state": "wrong-synthetic-state"},
        {"state": None},
        {"iss": "https://other-extore.example.test"},
        {"tenant": "different-tenant"},
        {"code": None},
        {"code": "short"},
        {"error": "access_denied"},
    ],
)
def test_callback_validation_fails_before_code_transmission(client_factory, change):
    client = client_factory(lambda request: pytest.fail("Invalid callback leaked code"))
    transaction = client.authorize(REDIRECT)
    with pytest.raises(ValueError):
        client.complete_authorization(transaction, callback_url(transaction, **change))
    assert transaction._consumed is False


@pytest.mark.parametrize("key", ["state", "iss", "code"])
def test_duplicate_callback_response_parameters_fail_closed(client_factory, key):
    client = client_factory(
        lambda request: pytest.fail("Duplicate callback leaked code")
    )
    transaction = client.authorize(REDIRECT)
    callback = callback_url(transaction) + "&" + urlencode({key: "another-value"})
    with pytest.raises(ValueError):
        client.complete_authorization(transaction, callback)
    assert transaction._consumed is False


def test_callback_target_must_exactly_match_registered_redirect(client_factory):
    client = client_factory(
        lambda request: pytest.fail("Foreign callback exchanged code")
    )
    transaction = client.authorize(REDIRECT)
    original = callback_url(transaction)
    for target in (
        original.replace("https://", "http://", 1),
        original.replace("shop.example.test", "other.example.test", 1),
        original.replace("shop.example.test", "shop.example.test:8443", 1),
        original.replace("/extore/callback", "/other/callback", 1),
        original + "#unexpected-fragment",
    ):
        with pytest.raises(ValueError):
            client.complete_authorization(transaction, target)
        assert transaction._consumed is False


def test_authorization_exchange_is_exact_form_post_and_single_use(client_factory):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert str(request.url) == ISSUER + "/api/integrations/commerce/token"
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        assert request.headers["accept"] == "application/json"
        assert "authorization" not in request.headers
        assert dict(parse_qsl(request.content.decode())) == {
            "grant_type": "authorization_code",
            "code": CODE,
            "redirect_uri": REDIRECT,
            "client_id": CLIENT_ID,
            "code_verifier": transaction.code_verifier,
        }
        return httpx.Response(200, json=token_payload())

    client = client_factory(handler)
    transaction = client.authorize(REDIRECT, scopes=("products.read", "cards.issue"))
    result = client.complete_authorization(transaction, callback_url(transaction))
    assert result.access_token == ACCESS
    assert result.refresh_token == REFRESH
    assert result.grant_id == "synthetic-grant"
    assert result.scope == ("cards.issue", "products.read")
    with pytest.raises(ValueError):
        client.complete_authorization(transaction, callback_url(transaction))
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("registered", "browser_target"),
    [
        ("https://Store.Example.com/cb", "https://store.example.com/cb"),
        ("https://store.example.com:443/cb", "https://store.example.com/cb"),
        ("https://store.example.com", "https://store.example.com/"),
    ],
)
def test_normalized_browser_callback_keeps_original_registered_uri_binding(
    client_factory, registered, browser_target
):
    calls = []

    def handler(request):
        calls.append(request)
        assert dict(parse_qsl(request.content.decode()))["redirect_uri"] == registered
        return httpx.Response(200, json=token_payload(scope="products.read"))

    client = client_factory(handler)
    transaction = client.authorize(registered)
    callback = (
        browser_target
        + "?"
        + urlencode({"state": transaction.state, "iss": ISSUER, "code": CODE})
    )
    assert client.validate_callback(transaction, callback) is None
    assert not transaction._consumed
    assert (
        client.complete_authorization(transaction, callback).grant_id
        == "synthetic-grant"
    )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "registered",
    [
        "https://store.example.com/cb?error_description=example",
        "https://store.example.com/cb?error_description=",
        "https://store.example.com/cb?error_description",
        "https://store.example.com/cb?tag=one&tag=two",
        "https://0177.0.0.1/cb",
        "https://127.1/cb",
        "https://0x7f.0.0.1/cb",
    ],
)
def test_registered_callback_matches_shared_syntax_boundary(client_factory, registered):
    client = client_factory(
        lambda request: pytest.fail("Invalid callback must not connect")
    )
    with pytest.raises(ValueError):
        client.authorize(registered)


@pytest.mark.parametrize(
    "target",
    [
        "https://store.example.com:8443/cb",
        "https://store.example.com/other",
        "https://store.example.com/cb?tag=wrong",
    ],
)
def test_callback_normalization_never_relaxes_nondefault_port_path_or_query(
    client_factory, target
):
    client = client_factory(
        lambda request: pytest.fail("Invalid callback must not connect")
    )
    transaction = client.authorize("https://Store.Example.com:443/cb?tag=original")
    callback = (
        target
        + ("&" if "?" in target else "?")
        + urlencode({"state": transaction.state, "iss": ISSUER, "code": CODE})
    )
    with pytest.raises(ValueError):
        client.complete_authorization(transaction, callback)
    assert not transaction._consumed


def test_merchant_denial_consumes_transaction_without_network(client_factory):
    client = client_factory(
        lambda request: pytest.fail("Denial must not exchange code")
    )
    transaction = client.authorize(REDIRECT)
    with pytest.raises(sdk.CommerceError) as error:
        client.complete_authorization(
            transaction,
            callback_url(transaction, code=None, error="access_denied"),
        )
    assert error.value.code == "access_denied"
    assert transaction._consumed is True
    with pytest.raises(ValueError):
        client.complete_authorization(transaction, callback_url(transaction))


def test_lost_token_exchange_response_is_never_automatically_retried(client_factory):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("Synthetic response lost", request=request)

    client = client_factory(handler)
    transaction = client.authorize(REDIRECT)
    with pytest.raises(sdk.CommerceError):
        client.complete_authorization(transaction, callback_url(transaction))
    with pytest.raises(ValueError):
        client.complete_authorization(transaction, callback_url(transaction))
    assert len(calls) == 1


def test_token_exchange_accepts_narrower_granted_scope(client_factory):
    client = client_factory(
        lambda request: httpx.Response(200, json=token_payload(scope="products.read"))
    )
    transaction = client.authorize(REDIRECT, scopes=("products.read", "cards.issue"))
    result = client.complete_authorization(transaction, callback_url(transaction))
    assert result.scope == ("products.read",)


@pytest.mark.parametrize(
    "change",
    [
        {"scope": "products.read cards.issue"},
        {"scope": {"products.read": True}},
        {"expires_in": True},
        {"expires_in": 901},
        {"token_type": "bearer"},
    ],
)
def test_invalid_or_escalated_token_response_does_not_create_credentials(
    client_factory, change
):
    payload = token_payload(**{"scope": "products.read", **change})
    client = client_factory(lambda request: httpx.Response(200, json=payload))
    transaction = client.authorize(REDIRECT, scopes=("products.read",))
    with pytest.raises(sdk.CommerceError):
        client.complete_authorization(transaction, callback_url(transaction))
    assert transaction._consumed is True


def test_refresh_rotates_once_and_uses_exact_form(client_factory):
    calls = []
    original = tokens()

    def handler(request):
        calls.append(request)
        assert str(request.url) == ISSUER + "/api/integrations/commerce/token"
        assert dict(parse_qsl(request.content.decode())) == {
            "grant_type": "refresh_token",
            "refresh_token": REFRESH,
            "client_id": CLIENT_ID,
        }
        return httpx.Response(
            200,
            json=token_payload(
                access_token="synthetic-new-access-token-00000",
                refresh_token="synthetic-new-refresh-token-0000",
                scope="products.read",
            ),
        )

    client = client_factory(handler)
    result = client.refresh(original)
    assert result.refresh_token != original.refresh_token
    assert result.scope == ("products.read",)
    with pytest.raises(ValueError):
        client.refresh(original)
    assert len(calls) == 1


def test_concurrent_refresh_of_same_token_sends_one_request(client_factory):
    calls = []
    barrier = threading.Barrier(2)
    original = tokens()

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json=token_payload(refresh_token="synthetic-rotated-refresh-0000000")
        )

    client = client_factory(handler)

    def rotate():
        barrier.wait(timeout=5)
        try:
            return client.refresh(original)
        except ValueError:
            return "consumed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(rotate) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]
    assert sum(isinstance(result, sdk.TokenSet) for result in results) == 1
    assert results.count("consumed") == 1
    assert len(calls) == 1


def test_lost_refresh_response_cannot_be_replayed(client_factory):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadError("Synthetic response lost", request=request)

    client = client_factory(handler)
    original = tokens()
    with pytest.raises(sdk.CommerceError):
        client.refresh(original)
    with pytest.raises(ValueError):
        client.refresh(original)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        {
            "grant_id": "different-grant",
            "refresh_token": "synthetic-rotated-00000000000",
        },
        {"refresh_token": REFRESH},
    ],
)
def test_refresh_cannot_change_grant_or_reuse_refresh_credential(
    client_factory, change
):
    client = client_factory(
        lambda request: httpx.Response(200, json=token_payload(**change))
    )
    original = tokens()
    with pytest.raises(sdk.CommerceError):
        client.refresh(original)
    assert original._refresh_consumed is True


def test_revocation_posts_refresh_token_and_accepts_empty_success(client_factory):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert str(request.url) == ISSUER + "/api/integrations/commerce/revoke"
        assert dict(parse_qsl(request.content.decode())) == {
            "token": REFRESH,
            "client_id": CLIENT_ID,
            "token_type_hint": "refresh_token",
        }
        assert "authorization" not in request.headers
        return httpx.Response(200)

    client = client_factory(handler)
    assert client.revoke(tokens()) is None
    assert len(calls) == 1


def test_tokens_cannot_be_used_on_another_issuer_or_client(client_factory):
    client = client_factory(
        lambda request: pytest.fail("Foreign credential transmitted")
    )
    for foreign in (
        tokens(issuer="https://other-extore.example.test"),
        tokens(client_id="another-synthetic-client"),
    ):
        with pytest.raises(ValueError):
            client.products(foreign)


def test_catalog_preserves_decimal_prices_and_accepts_100_products(client_factory):
    payload = catalog_payload()
    payload["products"] = [
        {**listing_payload(), "id": f"synthetic-product-{index}"}
        for index in range(100)
    ]
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer " + ACCESS
        assert request.headers["accept"] == "application/json"
        assert str(request.url) == ISSUER + "/api/integrations/commerce/products"
        return httpx.Response(200, json=payload)

    client = client_factory(handler)
    result = client.products(tokens())
    assert len(result["products"]) == 100
    assert result["products"][0]["variants"][0]["price"] == "25.00"
    assert result["products"][0]["variants"][1]["price"] is None
    assert len(calls) == 1


@pytest.mark.parametrize("invalid", ["too_many", "foreign_grant", "bad_listing"])
def test_catalog_rejects_unapproved_or_malformed_listing(client_factory, invalid):
    payload = catalog_payload()
    if invalid == "too_many":
        payload["products"] = [listing_payload() for _ in range(101)]
    elif invalid == "foreign_grant":
        payload["grant_id"] = "unapproved-grant"
    else:
        payload["products"][0]["schema"] = "unsupported-listing.v2"
    client = client_factory(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(sdk.CommerceError):
        client.products(tokens())


def test_single_product_id_is_path_encoded_and_path_traversal_rejected(client_factory):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json={**listing_payload(), "id": "product:synthetic"}
        )

    client = client_factory(handler)
    assert client.product(tokens(), "product:synthetic") == {
        **listing_payload(),
        "id": "product:synthetic",
    }
    assert str(calls[0].url) == (
        ISSUER + "/api/integrations/commerce/products/product%3Asynthetic"
    )
    with pytest.raises(ValueError):
        client.product(tokens(), "../tokens")
    assert len(calls) == 1


def test_card_issuance_posts_json_idempotency_and_exports_only_explicitly(
    client_factory,
):
    calls = []
    payload = cards_payload()

    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert str(request.url) == ISSUER + "/api/integrations/commerce/cards"
        assert request.headers["authorization"] == "Bearer " + ACCESS
        assert request.headers["idempotency-key"] == "synthetic-order-00001"
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {
            "product_id": "synthetic-product",
            "variant_id": "standard",
            "count": 1,
            "expected_revision": REVISION,
            "label": "Synthetic batch",
            "expires": 2000000000,
        }
        return httpx.Response(200, json=payload)

    client = client_factory(handler)
    batch = issue(client, label="Synthetic batch", expires=2000000000)
    assert batch.codes == (CARD,)
    assert CARD not in repr(batch)
    assert batch.export() == payload
    exported = batch.export()
    exported["codes"].clear()
    exported["quota"]["remaining"] = 0
    exported["variant"]["price"] = "0"
    assert batch.export() == payload
    assert len(calls) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"grant_id": "other-grant"},
        {"product_id": "other-product"},
        {"variant_id": "other-variant"},
        {"count": 2, "codes": [CARD, CARD]},
        {"codes": [{"malformed": True}]},
    ],
)
def test_card_batch_identity_count_and_codes_must_match_request(client_factory, change):
    client = client_factory(
        lambda request: httpx.Response(200, json=cards_payload(**change))
    )
    with pytest.raises(sdk.CommerceError):
        issue(client)


def test_card_batch_rejects_duplicate_codes_and_ambiguous_json(client_factory):
    duplicate_batch = cards_payload(count=2, codes=[CARD, CARD])
    duplicate_json = '{"schema":"extore.card-batch.v1","count":2,"count":1}'
    for response in (
        httpx.Response(200, json=duplicate_batch),
        httpx.Response(200, content=duplicate_json),
    ):
        client = client_factory(lambda request, response=response: response)
        with pytest.raises(sdk.CommerceError):
            issue(client, count=2)


@pytest.mark.parametrize(
    "change",
    [
        {"count": True},
        {"idempotency_key": "invalid\nheader"},
        {"expected_revision": "not-a-catalog-revision"},
    ],
)
def test_invalid_issuance_inputs_do_not_transmit_tokens(client_factory, change):
    client = client_factory(
        lambda request: pytest.fail("Invalid card request was sent")
    )
    with pytest.raises(ValueError):
        issue(client, **change)


def test_lost_issuance_response_is_not_retried_but_caller_can_replay_same_key(
    client_factory,
):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("Synthetic response lost", request=request)
        return httpx.Response(200, json=cards_payload())

    client = client_factory(handler)
    with pytest.raises(sdk.CommerceError):
        issue(client)
    assert len(calls) == 1
    recovered = issue(client)
    assert recovered.codes == (CARD,)
    assert len(calls) == 2
    assert calls[0].content == calls[1].content
    assert calls[0].headers["idempotency-key"] == calls[1].headers["idempotency-key"]


def test_errors_and_credential_reprs_exclude_reflected_sensitive_data(
    client_factory, caplog
):
    reflected = "synthetic-reflected-value-000000000"
    client = client_factory(
        lambda request: httpx.Response(
            409,
            json={
                "error": "catalog_changed",
                "error_description": reflected,
                "access_token": ACCESS,
                "code": CARD,
            },
        )
    )
    transaction = client.authorize(REDIRECT)
    with pytest.raises(sdk.CommerceError) as caught:
        client.complete_authorization(transaction, callback_url(transaction))
    assert caught.value.code == "catalog_changed"
    assert caught.value.status == 409
    rendered = str(caught.value) + repr(caught.value) + repr(tokens()) + caplog.text
    for confidential in (reflected, ACCESS, REFRESH, CODE, CARD):
        assert confidential not in rendered
    batch = sdk.CardBatch(
        "synthetic-grant",
        "synthetic-product",
        "standard",
        "synthetic-batch",
        1,
        1900000000,
        1900000300,
        {"reflection": CARD},
        {"reflection": ACCESS},
        (CARD,),
    )
    assert CARD not in repr(batch)
    assert ACCESS not in repr(batch)


def test_nonstring_server_error_is_sanitized_not_echoed_or_raised_raw(
    client_factory,
):
    reflected = "synthetic-private-error-payload"
    client = client_factory(
        lambda request: httpx.Response(400, json={"error": {"payload": reflected}})
    )
    with pytest.raises(sdk.CommerceError) as caught:
        client.products(tokens())
    assert caught.value.code == "commerce_error"
    assert reflected not in repr(caught.value)


def test_constructor_uses_explicit_proxy_factory_with_environment_disabled(
    monkeypatch,
):
    real_client = httpx.Client
    calls = []
    settings = ProxySettings("explicit", "http://proxy.example.test:8080")

    def capture(**kwargs):
        calls.append(copy.copy(kwargs))
        assert kwargs.pop("proxy") == settings.url
        return real_client(**kwargs)

    monkeypatch.setenv("HTTPS_PROXY", "http://ignored.example.test:9999")
    monkeypatch.setenv("SSL_CERT_FILE", "/does/not/exist/synthetic-cert.pem")
    monkeypatch.setattr(http_proxy.httpx, "Client", capture)
    with sdk.CommerceClient(
        ISSUER,
        CLIENT_ID,
        proxy_settings=settings,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=metadata_payload())
        ),
    ) as client:
        assert client.metadata() == metadata_payload()
    assert len(calls) == 1
    assert calls[0]["trust_env"] is False
    assert calls[0]["follow_redirects"] is False
    assert calls[0]["verify"] is True
    assert calls[0]["timeout"] == 30

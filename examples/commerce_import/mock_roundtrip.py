"""Offline SDK contract example. Every identity and secret here is fictional."""

import hashlib
import json
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from extore.commerce_client import CommerceClient

ISSUER = "https://redemption.example"
CALLBACK = "https://sales.example/extore/callback"
CLIENT_ID = "example-client"
GRANT_ID = "example-grant"
REVISION = hashlib.sha256(b"fictional-listing").hexdigest()
LISTING = {
    "schema": "extore.product-listing.v1",
    "id": "document_service",
    "shop_id": "example_shop",
    "revision": REVISION,
    "redemption_url": ISSUER + "/",
    "semantics": {
        "price": "reference",
        "inventory": "not_exported",
        "payment": "external_sales_platform",
        "redemption": "extore",
    },
    "product": {
        "name": "示例文档整理",
        "description": "此商品仅存在于离线示例。",
        "public": True,
        "mode": "manual",
        "delivery": "content",
        "view_policy": "repeat",
        "parameters": [],
        "outputs": [],
        "progress_steps": [],
    },
    "variants": [
        {
            "id": "standard",
            "name": "标准版",
            "price": "25",
            "currency": "CNY",
            "attributes": {"revisions": 0},
            "enabled": True,
        }
    ],
}


def main():
    expires = time.time() + 86400
    minted = {}
    counters = {"issued": 0, "refreshes": 0, "revoked": False}
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)

    def token_response():
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": 900,
            "refresh_token": refresh,
            "scope": "products.read cards.issue",
            "grant_id": GRANT_ID,
            "grant_expires": expires,
        }

    def handler(request):
        nonlocal access, refresh
        path = request.url.path
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": ISSUER + "/oauth/authorize",
                    "token_endpoint": ISSUER + "/api/integrations/commerce/token",
                    "revocation_endpoint": ISSUER + "/api/integrations/commerce/revoke",
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    "code_challenge_methods_supported": ["S256"],
                    "scopes_supported": ["products.read", "cards.issue"],
                    "authorization_response_iss_parameter_supported": True,
                },
            )
        if path == "/api/integrations/commerce/token":
            body = parse_qs(request.content.decode())
            assert body["client_id"] == [CLIENT_ID]
            if body["grant_type"] == ["authorization_code"]:
                assert body["redirect_uri"] == [CALLBACK]
                assert body["code_verifier"] == [transaction.code_verifier]
                assert body["code"] == [authorization_code]
            else:
                assert body["grant_type"] == ["refresh_token"]
                assert body["refresh_token"] == [refresh]
                access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                counters["refreshes"] += 1
            return httpx.Response(200, json=token_response())
        if path == "/api/integrations/commerce/revoke":
            body = parse_qs(request.content.decode())
            assert body["token"] == [refresh]
            counters["revoked"] = True
            return httpx.Response(200)
        assert request.headers["authorization"] == "Bearer " + access
        assert not counters["revoked"]
        if path == "/api/integrations/commerce/products":
            return httpx.Response(
                200,
                json={
                    "schema": "extore.commerce-catalog.v1",
                    "issuer": ISSUER,
                    "shop": {"id": "example_shop", "name": "示例店铺"},
                    "grant_id": GRANT_ID,
                    "products": [LISTING],
                },
            )
        if path == "/api/integrations/commerce/products/document_service":
            return httpx.Response(200, json=LISTING)
        if path == "/api/integrations/commerce/cards":
            body = json.loads(request.content)
            assert body["expected_revision"] == REVISION
            assert (body["product_id"], body["variant_id"]) == (
                "document_service",
                "standard",
            )
            key = request.headers["idempotency-key"]
            if key not in minted:
                counters["issued"] += body["count"]
                now = time.time()
                minted[key] = {
                    "schema": "extore.card-batch.v1",
                    "grant_id": GRANT_ID,
                    "product_id": "document_service",
                    "variant_id": "standard",
                    "batch_id": "example-batch",
                    "count": body["count"],
                    "codes": [secrets.token_urlsafe(32) for _ in range(body["count"])],
                    "created_at": now,
                    "recovery_expires": now + 86400,
                    "quota": {
                        "max_count": 10,
                        "issued_count": body["count"],
                        "remaining": 10 - body["count"],
                    },
                    "variant": LISTING["variants"][0],
                }
            return httpx.Response(200, json=minted[key])
        raise AssertionError("Unexpected mock endpoint")

    with CommerceClient(
        ISSUER, CLIENT_ID, transport=httpx.MockTransport(handler)
    ) as client:
        client.metadata()
        transaction = client.authorize(
            CALLBACK,
            scopes=("products.read", "cards.issue"),
            product_ids=("document_service",),
        )
        assert parse_qs(urlsplit(transaction.url).query)["code_challenge_method"] == [
            "S256"
        ]
        authorization_code = secrets.token_urlsafe(32)
        callback = (
            CALLBACK
            + "?"
            + urlencode(
                {"code": authorization_code, "state": transaction.state, "iss": ISSUER}
            )
        )
        tokens = client.complete_authorization(transaction, callback)
        catalog = client.products(tokens)
        listing = client.product(tokens, "document_service")
        key = secrets.token_urlsafe(24)
        batch = client.issue_cards(
            tokens,
            listing["id"],
            "standard",
            2,
            idempotency_key=key,
            expected_revision=listing["revision"],
        )
        recovered = client.issue_cards(
            tokens,
            listing["id"],
            "standard",
            2,
            idempotency_key=key,
            expected_revision=listing["revision"],
        )
        assert batch.codes == recovered.codes and counters["issued"] == 2
        rotated = client.refresh(tokens)
        client.revoke(rotated)
        print(
            json.dumps(
                {
                    "ok": True,
                    "offline": True,
                    "products": len(catalog["products"]),
                    "issued": counters["issued"],
                    "recovered_original_batch": True,
                    "refreshes": counters["refreshes"],
                    "revoked": counters["revoked"],
                }
            )
        )


if __name__ == "__main__":
    main()

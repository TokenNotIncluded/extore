"""Documented schemas exercised with real Extore/SDK wire responses."""

import copy
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from extore.app import app
from extore.commerce_client import CommerceClient, CommerceError
from extore.commerce_listing import listing_data
from extore.commerce_models import Approval, CardLimit, Issue
from extore.commerce_schema import commerce_schema
from extore.config import ORIGIN
from extore.http_proxy import ProxySettings

ROOT = Path(__file__).resolve().parents[1]


def validator(name=None):
    schema = commerce_schema()
    if name:
        schema.pop("oneOf")
        schema["$ref"] = "#/$defs/" + name
    return Draft202012Validator(schema)


def test_public_schema_is_valid_documented_and_fresh(client):
    schema = commerce_schema()
    Draft202012Validator.check_schema(schema)
    documented = json.loads(
        (ROOT / "docs/schemas/commerce-import.v1.schema.json").read_text()
    )
    assert documented == schema
    response = client.get("/api/integrations/commerce/schema")
    assert response.status_code == 200
    assert response.json() == schema
    assert "no-store" in response.headers["cache-control"]
    schema["$defs"]["Catalog"]["properties"]["products"]["maxItems"] = 999
    assert (
        commerce_schema()["$defs"]["Catalog"]["properties"]["products"]["maxItems"]
        == 100
    )


def test_documented_product_token_card_and_cli_examples_match_contract():
    document = (ROOT / "docs/commerce-import-protocol.md").read_text()
    checked = []
    for raw in re.findall(r"```json\n(.*?)\n```", document, re.S):
        data = json.loads(raw)
        if data.get("schema") in {
            "extore.commerce-catalog.v1",
            "extore.product-listing.v1",
            "extore.card-batch.v1",
        }:
            validator().validate(data)
            checked.append(data["schema"])
        elif "access_token" in data:
            validator("TokenResponse").validate(data)
            checked.append("TokenResponse")
        elif "idempotency_key" in data:
            validator("CLIStockRequest").validate(data)
            checked.append("CLIStockRequest")
    assert set(checked) == {
        "extore.commerce-catalog.v1",
        "extore.product-listing.v1",
        "extore.card-batch.v1",
        "TokenResponse",
        "CLIStockRequest",
    }


def test_real_listing_filter_preserves_reference_price_and_matches_schema():
    source = {
        "id": "fictional_document",
        "name": {"zh-CN": "示例资料整理", "en": "Example document"},
        "description": "此数据仅用于测试。",
        "parameters": [
            {
                "key": "format",
                "label": {"zh-CN": "格式"},
                "type": "select",
                "options": [{"value": "word", "label": {"zh-CN": "文档"}}],
            },
            {"key": "materials", "type": "images", "max_items": 2},
        ],
        "outputs": [{"key": "delivery_file", "type": "file", "required": True}],
        "progress_steps": [{"id": "review", "label": "核对资料"}],
        "variants": [
            {
                "id": "standard",
                "name": "标准版",
                "price": "00025.00",
                "currency": "CNY",
                "attributes": {"revisions": 1},
                "enabled": True,
            }
        ],
        "webhook_secret": "fictional-secret-that-is-never-exported",
    }
    listing = listing_data(
        source, origin="https://redemption.example", shop_id="fictional_shop"
    )
    validator().validate(listing)
    assert listing["variants"][0]["price"] == "00025.00"
    assert "webhook_secret" not in json.dumps(listing)
    broken = copy.deepcopy(listing)
    broken["variants"][0]["price"] = 25
    assert list(validator().iter_errors(broken))
    broken = copy.deepcopy(listing)
    broken["semantics"]["price"] = "selling_price"
    assert list(validator().iter_errors(broken))


@pytest.mark.parametrize(
    "extra", [{"attributes": {"revisions": 99}}, {"routed": False}, {"unknown": True}]
)
def test_structural_issue_contract_rejects_privilege_override(extra):
    body = {"product_id": "fictional_document", "variant_id": "standard", "count": 1}
    validator("CardIssueRequest").validate(body)
    assert Issue.model_validate(body).count == 1
    assert list(validator("CardIssueRequest").iter_errors({**body, **extra}))
    with pytest.raises(ValueError):
        Issue.model_validate({**body, **extra})


def test_approved_limit_shapes_match_pydantic_and_documented_bounds():
    limit = {
        "product_id": "fictional_document",
        "variant_id": "standard",
        "max_count": 10000,
    }
    validator("CardLimit").validate(limit)
    assert CardLimit.model_validate(limit).max_count == 10000
    approval = {
        "shop_id": "fictional_shop",
        "review_digest": "a" * 64,
        "scopes": ["products.read", "cards.issue"],
        "product_ids": ["fictional_document"],
        "card_limits": [limit],
        "grant_expires": time.time() + 86400,
    }
    validator("Approval").validate(approval)
    assert Approval.model_validate(approval).product_ids == ["fictional_document"]
    for invalid in (0, 10001, True):
        assert list(validator("CardLimit").iter_errors({**limit, "max_count": invalid}))


def test_sdk_roundtrip_uses_real_backend_and_schema(owner, setup_product, monkeypatch):
    from extore import commerce_listing, commerce_oauth, commerce_store

    issuer = "https://redemption.example"
    callback = "https://sales.example/extore/callback?merchant=synthetic"
    monkeypatch.setattr(commerce_oauth, "ORIGIN", issuer)
    monkeypatch.setattr(commerce_store, "ORIGIN", issuer)
    original_listing = commerce_listing.commerce_listing
    monkeypatch.setattr(
        commerce_listing,
        "commerce_listing",
        lambda c, pid: original_listing(c, pid, origin=issuer),
    )
    pid, _ = setup_product()
    registered = owner.post(
        "/api/admin/commerce/clients",
        json={"client_name": "Synthetic SDK integration", "redirect_uris": [callback]},
    )
    assert registered.status_code == 200
    client_id = registered.json()["client_id"]
    wire = []
    with TestClient(app, base_url=ORIGIN) as machine:

        def handler(request):
            assert request.url.host == "redemption.example"
            response = machine.request(
                request.method,
                request.url.raw_path.decode("ascii"),
                headers=dict(request.headers),
                content=request.content,
            )
            if response.status_code == 200 and response.content:
                result = response.json()
                if request.url.path.endswith("/token"):
                    validator("TokenResponse").validate(result)
                elif request.url.path.endswith("/products"):
                    validator("Catalog").validate(result)
                elif "/products/" in request.url.path:
                    validator("Listing").validate(result)
                elif request.url.path.endswith("/cards"):
                    validator("CardBatch").validate(result)
            wire.append((request.method, request.url.path, response.status_code))
            return httpx.Response(
                response.status_code,
                headers=dict(response.headers),
                content=response.content,
            )

        with CommerceClient(
            issuer,
            client_id,
            proxy_settings=ProxySettings(),
            transport=httpx.MockTransport(handler),
        ) as sdk:
            discovery = sdk.metadata()
            assert discovery["extore_commerce"]["schema"] == "extore.commerce-import.v1"
            transaction = sdk.authorize(
                callback, scopes=("products.read", "cards.issue"), product_ids=(pid,)
            )
            request_url = urlsplit(transaction.url)
            requested = machine.get(
                request_url.path,
                params=parse_qs(request_url.query),
                follow_redirects=False,
            )
            assert requested.status_code == 303
            rid = urlsplit(requested.headers["location"]).fragment
            context = owner.get("/api/admin/commerce/requests/" + rid).json()
            approved = owner.post(
                "/api/admin/commerce/requests/" + rid + "/approve",
                json={
                    "shop_id": context["shop"]["id"],
                    "review_digest": context["review_digest"],
                    "scopes": ["products.read", "cards.issue"],
                    "product_ids": [pid],
                    "card_limits": [
                        {"product_id": pid, "variant_id": "default", "max_count": 5}
                    ],
                    "grant_expires": time.time() + 86400,
                },
            )
            assert approved.status_code == 200
            tokens = sdk.complete_authorization(
                transaction, approved.json()["redirect_uri"]
            )
            catalog = sdk.products(tokens)
            listing = sdk.product(tokens, pid)
            assert catalog["products"] == [listing]
            first = sdk.issue_cards(
                tokens,
                pid,
                "default",
                2,
                idempotency_key="synthetic-schema-recovery",
                expected_revision=listing["revision"],
            )
            recovered = sdk.issue_cards(
                tokens,
                pid,
                "default",
                2,
                idempotency_key="synthetic-schema-recovery",
                expected_revision=listing["revision"],
            )
            assert first.codes == recovered.codes
            assert recovered.quota == {
                "max_count": 5,
                "issued_count": 2,
                "remaining": 3,
            }
            rotated = sdk.refresh(tokens)
            assert rotated.refresh_token != tokens.refresh_token
            sdk.revoke(rotated)
            with pytest.raises(CommerceError) as error:
                sdk.products(rotated)
            assert error.value.code == "invalid_token"
    assert ("POST", "/api/integrations/commerce/revoke", 200) in wire

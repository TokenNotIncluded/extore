"""SKU configuration, real SQLite round-trips and private-data boundaries."""

import json
from typing import get_args

import pytest
from pydantic import ValidationError

from extore.app import managed_product_view
from extore.commerce_listing import commerce_listing
from extore.db import db
from extore.manage_commands import _public
from extore.marketplace_product import MarketplaceVariant, Template
from extore.models import ProductVariant
from extore.service import product, public_product
from extore.variants import card_variant

SECRET = "private-fixed-body-不要公开"


@pytest.mark.parametrize("template", get_args(Template))
def test_all_web_delivery_templates(template):
    config = MarketplaceVariant(
        template=template,
        fixed_content=SECRET if template == "fixed-content" else None,
        price_quota=500000,
    )
    assert config.template == template
    assert config.price_quota == 500000


@pytest.mark.parametrize(
    "price", [True, 500000.0, "500000", 0, 499999, 9007199254740992]
)
def test_credits_are_exact_bounded_integers(price):
    with pytest.raises(ValidationError):
        MarketplaceVariant(price_quota=price)


@pytest.mark.parametrize(
    "config",
    [
        {"template": "unsupported"},
        {"template": "fixed-content"},
        {"template": "fixed-content", "fixed_content": "  \n"},
        {"template": "fixed-content", "fixed_content": "字" * 5334},
        {"template": "fixed-content", "fixed_content": "\ud800"},
        {"template": "text", "fixed_content": SECRET},
        {"template": "card-key", "fixed_content": ""},
        {"unknown": "ignored?"},
    ],
)
def test_invalid_or_stale_private_content_is_rejected(config):
    with pytest.raises(ValidationError):
        MarketplaceVariant.model_validate(config)


def test_byte_limit_and_max_safe_integer():
    value = MarketplaceVariant(
        template="fixed-content",
        fixed_content="字" * 5333 + "x",
        price_quota=9007199254740991,
    )
    assert len(value.fixed_content.encode()) == 16000
    assert MarketplaceVariant().price_quota is None


def test_sku_defaults_do_not_invent_a_sales_price():
    variant = ProductVariant(id="default", name="legacy", price="19.90")
    assert variant.marketplace == MarketplaceVariant()
    assert variant.price == "19.9"


def test_private_sku_persistence_public_export_and_card_snapshot(owner, setup_product):
    pid, code = setup_product(
        public=True,
        variants=[
            {
                "id": "default",
                "name": "固定内容",
                "price": "19.90",
                "marketplace": {
                    "template": "fixed-content",
                    "fixed_content": SECRET,
                    "price_quota": 500001,
                },
            }
        ],
    )
    with db() as c:
        stored = product(c, pid)
        assert stored["variants"][0]["marketplace"]["fixed_content"] == SECRET
        listing = commerce_listing(c, pid)
        public = public_product(stored)
        assert "marketplace" not in public["variants"][0]
        assert _public(stored)["variants"][0]["marketplace"] == "[redacted]"
        assert SECRET not in json.dumps(_public(stored), ensure_ascii=False)
        assert SECRET not in json.dumps(listing, ensure_ascii=False)
        assert "marketplace" not in listing["variants"][0]
        revision = listing["revision"]
        # A private setting is not part of the ordinary listing/revision.
        stored["variants"][0]["marketplace"]["fixed_content"] = "changed"
        c.execute("UPDATE products SET config=? WHERE id=?", (json.dumps(stored), pid))
        assert commerce_listing(c, pid)["revision"] == revision
        card = c.execute("SELECT id FROM cards WHERE product_id=?", (pid,)).fetchone()
        assert "marketplace" not in card_variant(c, card["id"])
        raw = c.execute(
            "SELECT variant_snapshot FROM card_meta WHERE card_id=?", (card["id"],)
        ).fetchone()
        assert SECRET not in raw[0]
    # Public HTTP endpoint uses the same filter, rather than just a unit helper.
    response = owner.get("/api/products")
    assert response.status_code == 200
    assert SECRET not in response.text
    assert "marketplace" not in response.text


def test_edit_only_view_hides_private_config_without_mutating_input():
    original = {
        "variants": [{"id": "default", "marketplace": {"fixed_content": SECRET}}]
    }
    redacted = managed_product_view(original, {"permissions": ["product.edit"]})
    assert "marketplace" not in redacted["variants"][0]
    assert original["variants"][0]["marketplace"]["fixed_content"] == SECRET

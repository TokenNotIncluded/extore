"""Authoritative sales settings, independent of fulfilment processor secrets."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Template = Literal[
    "card-key",
    "text",
    "custom-text",
    "fixed-content",
    "redemption-code",
    "license-key",
    "download-link",
    "account-details",
]
Payment = Literal[
    "balance",
    "platform:waffo_pancake",
    "platform:linuxdo",
    "external:epay",
    "external:waffo_pancake",
]


class MarketplaceVariant(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price_quota: int | None = Field(
        default=None, ge=1, le=9007199254740991, strict=True
    )
    template: Template = "card-key"
    fixed_content: str | None = Field(default=None, max_length=131072)


class MarketplaceLink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(max_length=200)
    url: str = Field(max_length=2048)
    description: str = Field(default="", max_length=10000)


class MarketplaceProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=131072)
    image_urls: list[str] | None = Field(default=None, max_length=32)
    contact: str | None = Field(default=None, max_length=4096)
    links: list[MarketplaceLink] | None = Field(default=None, max_length=2000)
    category_id: str | None = Field(default=None, max_length=36)
    visibility: Literal["public", "private", "unlisted"] | None = None
    test_mode: bool = False
    purchase_login_required: bool = True
    max_quantity_per_order: int | None = Field(
        default=None, ge=1, le=2147483647, strict=True
    )
    max_quantity_per_buyer: int | None = Field(
        default=None, ge=1, le=2147483647, strict=True
    )
    sale_limit: int | None = Field(default=None, ge=1, le=2147483647, strict=True)
    delivery_strategy: Literal["sequential", "random"] = "sequential"
    payment_methods: list[Payment] = Field(
        default_factory=lambda: ["balance"], max_length=5
    )
    pickup_login_required: bool = True
    pickup_code_required: bool = False
    email_pickup_link: bool = False

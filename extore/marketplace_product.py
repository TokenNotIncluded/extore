"""Authoritative sales settings, independent of fulfilment processor secrets.

Template names follow the shop Web delivery contract. Availability must still be
negotiated with the target server; this is not a capability claim.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
        default=None, strict=True, ge=500000, le=9007199254740991
    )
    template: Template = "card-key"
    fixed_content: str | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def private_content(self):
        if self.fixed_content is not None:
            try:
                size = len(self.fixed_content.encode("utf-8"))
            except UnicodeEncodeError:
                raise ValueError("固定交付内容必须是有效 UTF-8 文本") from None
            if size > 16000:
                raise ValueError("固定交付内容最多 16000 字节")
        if self.template == "fixed-content":
            if not self.fixed_content or not self.fixed_content.strip():
                raise ValueError("固定内容规格必须填写交付正文")
        elif self.fixed_content is not None:
            raise ValueError("只有固定内容规格可以保存交付正文")
        return self


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

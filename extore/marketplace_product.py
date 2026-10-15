"""Private per-SKU sales settings; never redemption or public catalog data.

Template names follow api.lmm.best's Web delivery contract. Their availability
must still be negotiated with the target server; this is not a capability claim.
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


class MarketplaceVariant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: Template = "card-key"
    price_quota: int | None = Field(
        default=None, strict=True, ge=500000, le=9007199254740991
    )
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

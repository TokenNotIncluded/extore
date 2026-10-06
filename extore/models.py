import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .variants import default_variant, normalize_price, validate_attributes

LINK_PERMISSIONS = (
    "queue.view",
    "queue.process",
    "queue.retry",
    "product.edit",
    "fulfillment.configure",
    "cards.manage",
    "events.manage",
    "links.delegate",
)
DEFAULT_LINK_PERMISSIONS = ("queue.view", "queue.process")


class ProductLinkInput(BaseModel):
    product_id: str = Field(default="", max_length=100)
    name: str = Field(min_length=1, max_length=100)
    days: float | None = Field(default=None, gt=0, le=90)
    max_uses: int = Field(default=1, strict=True, ge=1, le=1000)
    max_cli_uses: int = Field(default=1, strict=True, ge=1, le=1000)
    permissions: list[str] = Field(
        default_factory=lambda: list(DEFAULT_LINK_PERMISSIONS),
        min_length=1,
        max_length=20,
    )

    @model_validator(mode="after")
    def permission_scope(self):
        if set(self.permissions) - set(LINK_PERMISSIONS):
            raise ValueError("存在未定义的商品管理权限")
        if {"queue.process", "queue.retry"} & set(
            self.permissions
        ) and "queue.view" not in self.permissions:
            raise ValueError("处理或重试队列必须同时允许查看队列")
        if (
            "fulfillment.configure" in self.permissions
            and "product.edit" not in self.permissions
        ):
            raise ValueError("配置发货必须同时允许管理商品展示与参数")
        self.permissions = [p for p in LINK_PERMISSIONS if p in self.permissions]
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("请输入商品管理链接名称")
        return self


class StaffInput(ProductLinkInput):
    product_id: str = Field(min_length=1, max_length=100)


class QuickProductInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: Literal["manual_content", "manual_service", "existing_product"]
    from_product_id: str | None = Field(default=None, min_length=1, max_length=100)
    name: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def template_source(self):
        if self.template_id == "existing_product":
            if self.from_product_id is None:
                raise ValueError("复制商品模板时请选择来源商品")
        elif self.from_product_id is not None:
            raise ValueError("内置模板不能同时指定来源商品")
        if self.name is not None:
            self.name = self.name.strip()
            if not self.name:
                raise ValueError("商品名称不能为空白")
        return self


class Parameter(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    label: dict[str, str]
    description: dict[str, str] = Field(default_factory=dict)
    collapsed: bool = True
    required: bool = True
    type: Literal["text", "email", "url", "textarea", "number", "file"] = "text"

    @model_validator(mode="after")
    def labels(self):
        if not self.label or any(
            not v.strip() or len(v) > 200 for v in self.label.values()
        ):
            raise ValueError("请输入参数显示名称")
        if any(len(v) > 10000 for v in self.description.values()):
            raise ValueError("教程过长")
        return self


class OutputField(Parameter):
    type: Literal["text", "email", "url", "textarea", "number", "file"] = "text"


class ProgressStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    label: dict[str, str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def labels(self):
        if any(not key.strip() or len(key) > 40 for key in self.label):
            raise ValueError("步骤语言代码必须是非空文本，最多四十字符")
        if any(not value.strip() or len(value) > 200 for value in self.label.values()):
            raise ValueError("请输入步骤显示名称，最多二百字符")
        return self


def validate_completed_steps(value):
    if value is not None and (
        len(set(value)) != len(value)
        or any(not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", step) for step in value)
    ):
        raise ValueError("已完成步骤代码必须有效且不能重复")
    return value


class ProductVariant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=10000)
    price: str | None = Field(
        default=None,
        description="Optional reference price for configuring an external store, as exact decimal text. Extore handles redemption and does not collect payments.",
    )
    currency: str = Field(default="CNY", pattern=r"^[A-Z]{3,5}$")
    attributes: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = Field(default=True, strict=True)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("price", mode="before")
    @classmethod
    def price_text(cls, value):
        return normalize_price(value)

    @field_validator("attributes", mode="before")
    @classmethod
    def scalar_attributes(cls, value):
        return validate_attributes(value)


class Product(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=20000)
    logo: str = Field(default="", max_length=2000)
    image: str = Field(default="", max_length=2000)
    public: bool = False
    support_email: str = Field(default="", max_length=254)
    progress_steps: list[ProgressStep] = Field(default_factory=list, max_length=30)
    variants: list[ProductVariant] = Field(
        default_factory=lambda: [ProductVariant(**default_variant())],
        min_length=1,
        max_length=100,
    )
    mode: Literal["manual", "webhook", "script"] = "manual"
    delivery: Literal["content", "service"] = "content"
    view_policy: Literal["repeat", "once"] = "repeat"
    allow_retry: bool = True
    max_attempts: int = Field(default=3, ge=1, le=20)
    parameters: list[Parameter] = Field(default_factory=list, max_length=30)
    outputs: list[OutputField] = Field(
        default_factory=lambda: [
            OutputField(
                key="content",
                label={"zh-CN": "交付内容", "en": "Delivery content"},
                type="textarea",
            )
        ],
        max_length=30,
    )
    webhook_url: str = Field(default="", max_length=2000)
    webhook_secret: str = Field(default="", max_length=200)
    script: str = Field(default="", max_length=100, pattern=r"^[a-zA-Z0-9_-]*$")
    processor_id: str = Field(default="", max_length=100, pattern=r"^[a-zA-Z0-9_-]*$")
    processor_config: dict[str, str] = Field(default_factory=dict, max_length=30)

    @field_validator("support_email")
    @classmethod
    def contact_email(cls, value):
        value = value.strip()
        if not value:
            return value
        local, separator, domain = value.rpartition("@")
        if (
            not separator
            or len(local) > 64
            or local.startswith(".")
            or local.endswith(".")
            or ".." in local
            or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
            or not re.fullmatch(
                r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
                r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",
                domain,
            )
        ):
            raise ValueError("联系邮箱格式不正确")
        return value

    @model_validator(mode="after")
    def validate_config(self):
        return self._validate_config()

    def _validate_config(self, allow_blank_secret=False):
        if len({step.id for step in self.progress_steps}) != len(self.progress_steps):
            raise ValueError("处理步骤代码不能重复")
        if len({variant.id for variant in self.variants}) != len(self.variants):
            raise ValueError("规格代码不能重复")
        if self.script:
            raise ValueError("不能指定或执行任意脚本；请选择商品处理器")
        if self.mode == "script":
            if not self.processor_id:
                raise ValueError("请选择商品处理器")
            from .processors import normalize_processor_product

            values = self.model_dump()
            for field in ("parameters", "outputs"):
                if field not in self.model_fields_set:
                    values.pop(field, None)
            canonical = normalize_processor_product(
                values, strict_schema=True, allow_incomplete=True
            )
            self.delivery = canonical["delivery"]
            self.parameters = [
                Parameter.model_validate(f) for f in canonical["parameters"]
            ]
            self.outputs = [OutputField.model_validate(f) for f in canonical["outputs"]]
            self.processor_config = (
                {} if not self.processor_config else canonical["processor_config"]
            )
        elif self.processor_id or self.processor_config:
            raise ValueError("只有商品处理器模式可以配置处理器")
        if len({p.key for p in self.parameters}) != len(self.parameters):
            raise ValueError("参数代码名不能重复")
        if self.delivery == "service":
            if "outputs" not in self.model_fields_set:
                self.outputs = []
            elif self.outputs:
                raise ValueError("服务型商品只返回状态，不配置输出字段")
        elif not self.outputs:
            raise ValueError("内容型商品至少需要一个输出字段")
        if len({f.key for f in self.outputs}) != len(self.outputs):
            raise ValueError("输出字段代码名不能重复")
        if self.webhook_url:
            u = urlsplit(self.webhook_url)
            if u.scheme != "https" or not u.hostname or u.username or u.fragment:
                raise ValueError("Webhook 必须使用 HTTPS 且不能含用户信息或片段")
            if len(self.webhook_secret) < 32 and not (
                allow_blank_secret and not self.webhook_secret
            ):
                raise ValueError("Webhook 密钥至少 32 字符")
        if self.mode == "webhook" and not self.webhook_url:
            raise ValueError("请配置 Webhook 地址")
        for value in (self.logo, self.image):
            if value and (
                urlsplit(value).scheme != "https" or not urlsplit(value).hostname
            ):
                raise ValueError("商品图片必须是 HTTPS 地址")
        return self


class ManagementProduct(Product):
    @model_validator(mode="after")
    def validate_config(self):
        # Management read/edit round trips may omit the masked signing secret.
        # The API merges the stored secret and validates a complete Product again.
        return self._validate_config(allow_blank_secret=True)


class CodeInput(BaseModel):
    code: str = Field(min_length=1, max_length=8000)


class BatchRedemptionItem(BaseModel):
    card_id: str = Field(min_length=1, max_length=80)
    params: dict[str, str] = Field(default_factory=dict)


class Redemption(BaseModel):
    token: str = Field(max_length=100)
    params: dict[str, str] = Field(default_factory=dict)
    items: list[BatchRedemptionItem] = Field(default_factory=list, max_length=30)


class TokenInput(BaseModel):
    token: str = Field(max_length=100)
    card_id: str | None = Field(default=None, max_length=80)


class IssueCards(BaseModel):
    product_id: str
    variant_id: str = Field(default="default", pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    count: int = Field(default=1, ge=1, le=1000)
    label: str = Field(default="", max_length=100)
    expires: float | None = Field(default=None, allow_inf_nan=False)


class JobUpdate(BaseModel):
    state: Literal["processing", "succeeded", "failed"]
    progress: int = Field(default=0, ge=0, le=100)
    completed_steps: list[str] | None = Field(default=None, max_length=30)
    message: str = Field(default="", max_length=1000)
    content: str | None = Field(default=None, max_length=100000)
    output: dict[str, str] | None = None
    retryable: bool = False
    attempt: int = Field(ge=1)

    @field_validator("completed_steps")
    @classmethod
    def completed_ids(cls, value):
        return validate_completed_steps(value)


class BatchUpdate(BaseModel):
    product_id: str = Field(min_length=1, max_length=100)
    progress: int = Field(default=0, ge=0, le=99)
    completed_steps: list[str] | None = Field(default=None, max_length=30)
    progress_steps: list[ProgressStep] | None = Field(
        default=None, min_length=1, max_length=30
    )
    ids: list[str] = Field(min_length=1, max_length=100)
    action: Literal[
        "claim",
        "progress",
        "succeed",
        "fail",
        "retry",
        "request_changes",
        "request_retry",
        "reject",
    ]
    message: str = Field(default="", max_length=1000)
    content: str | None = Field(default=None, max_length=100000)
    output: dict[str, str] | None = None
    retryable: bool = False
    retry_mode: Literal["revise", "reuse"] = "revise"
    reason_type: Literal["customer_input", "external", "processor"] = "customer_input"

    @field_validator("completed_steps")
    @classmethod
    def completed_ids(cls, value):
        return validate_completed_steps(value)

    @model_validator(mode="after")
    def unique_steps(self):
        if self.action in ("request_changes", "request_retry", "reject"):
            self.message = self.message.strip()
            if not self.message:
                raise ValueError("要求重试或拒绝任务必须填写原因")
            if self.output or self.content is not None:
                raise ValueError("要求重试或拒绝任务不能同时交付结果")
            if self.completed_steps is not None or self.progress_steps is not None:
                raise ValueError("要求重试或拒绝任务不修改处理步骤")
        if self.action == "retry" and (
            self.completed_steps is not None or self.progress_steps is not None
        ):
            raise ValueError("放行重试不修改步骤；重新提交时会保留计划并重置完成状态")
        if self.progress_steps is not None and len(
            {step.id for step in self.progress_steps}
        ) != len(self.progress_steps):
            raise ValueError("处理步骤代码不能重复")
        return self

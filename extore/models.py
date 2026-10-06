from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    type: Literal["text", "email", "url", "textarea", "number"] = "text"

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
    type: Literal["text", "email", "url", "textarea", "number"] = "text"


class Product(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=20000)
    logo: str = Field(default="", max_length=2000)
    image: str = Field(default="", max_length=2000)
    public: bool = False
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

    @model_validator(mode="after")
    def validate_config(self):
        return self._validate_config()

    def _validate_config(self, allow_blank_secret=False):
        if self.script:
            raise ValueError("不能指定或执行任意脚本；请选择官方处理器")
        if self.mode == "script":
            if not self.processor_id:
                raise ValueError("请选择官方处理器")
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
                {}
                if allow_blank_secret and not self.processor_config
                else canonical["processor_config"]
            )
        elif self.processor_id or self.processor_config:
            raise ValueError("只有官方处理器模式可以配置处理器")
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
    code: str = Field(min_length=1, max_length=128)


class Redemption(BaseModel):
    token: str = Field(max_length=100)
    params: dict[str, str] = Field(default_factory=dict)


class TokenInput(BaseModel):
    token: str = Field(max_length=100)


class IssueCards(BaseModel):
    product_id: str
    count: int = Field(default=1, ge=1, le=1000)
    label: str = Field(default="", max_length=100)
    expires: float | None = Field(default=None, allow_inf_nan=False)


class JobUpdate(BaseModel):
    state: Literal["processing", "succeeded", "failed"]
    progress: int = Field(default=0, ge=0, le=100)
    message: str = Field(default="", max_length=1000)
    content: str | None = Field(default=None, max_length=100000)
    output: dict[str, str] | None = None
    retryable: bool = False
    attempt: int = Field(ge=1)


class BatchUpdate(BaseModel):
    product_id: str = Field(min_length=1, max_length=100)
    progress: int = Field(default=0, ge=0, le=99)
    ids: list[str] = Field(min_length=1, max_length=100)
    action: Literal["claim", "progress", "succeed", "fail", "retry"]
    message: str = Field(default="", max_length=1000)
    content: str | None = Field(default=None, max_length=100000)
    output: dict[str, str] | None = None
    retryable: bool = False

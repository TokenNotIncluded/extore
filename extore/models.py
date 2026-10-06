from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator


class Parameter(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    label: dict[str, str]
    description: dict[str, str] = Field(default_factory=dict)
    collapsed: bool = True
    required: bool = True
    type: Literal["text", "email", "textarea", "number"] = "text"

    @model_validator(mode="after")
    def labels(self):
        if not self.label or any(
            not v.strip() or len(v) > 200 for v in self.label.values()
        ):
            raise ValueError("请输入参数显示名称")
        if any(len(v) > 10000 for v in self.description.values()):
            raise ValueError("教程过长")
        return self


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
    webhook_url: str = Field(default="", max_length=2000)
    webhook_secret: str = Field(default="", max_length=200)
    script: str = Field(default="", max_length=100, pattern=r"^[a-zA-Z0-9_-]*$")

    @model_validator(mode="after")
    def validate_config(self):
        if len({p.key for p in self.parameters}) != len(self.parameters):
            raise ValueError("参数代码名不能重复")
        if self.webhook_url:
            u = urlsplit(self.webhook_url)
            if u.scheme != "https" or not u.hostname or u.username or u.fragment:
                raise ValueError("Webhook 必须使用 HTTPS 且不能含用户信息或片段")
            if len(self.webhook_secret) < 32:
                raise ValueError("Webhook 密钥至少 32 字符")
        if self.mode == "webhook" and not self.webhook_url:
            raise ValueError("请配置 Webhook 地址")
        if self.mode == "script" and not self.script:
            raise ValueError("请选择服务器上的脚本")
        for value in (self.logo, self.image):
            if value and (
                urlsplit(value).scheme != "https" or not urlsplit(value).hostname
            ):
                raise ValueError("商品图片必须是 HTTPS 地址")
        return self


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


class JobUpdate(BaseModel):
    state: Literal["processing", "succeeded", "failed"]
    progress: int = Field(default=0, ge=0, le=100)
    message: str = Field(default="", max_length=1000)
    content: str | None = Field(default=None, max_length=100000)
    retryable: bool = False
    attempt: int = Field(ge=1)


class BatchUpdate(BaseModel):
    progress: int = Field(default=0, ge=0, le=99)
    ids: list[str] = Field(min_length=1, max_length=100)
    action: Literal["claim", "progress", "succeed", "fail", "retry"]
    message: str = Field(default="", max_length=1000)
    content: str | None = Field(default=None, max_length=100000)
    retryable: bool = False

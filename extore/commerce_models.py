"""Bounded inputs for the open, explicitly approved commerce import profile."""

import ipaddress
import re
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCOPES = ("products.read", "cards.issue")
MAX_PRODUCTS = 100
MAX_LIMITS = 500
MAX_GRANT_DAYS = 90
MAX_ISSUE_COUNT = 100


def redirect_uri(value):
    """Validate syntax only; registration never resolves or fetches this URL."""
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise ValueError("回调地址长度无效")
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in value) or "\\" in value:
        raise ValueError("回调地址须使用 ASCII HTTPS URL")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        raise ValueError("回调地址无效") from None
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or "#" in value
        or "%" in parsed.netloc
        or host.endswith(".")
        or "." not in host
        or len(host) > 253
        or not re.fullmatch(r"[A-Za-z0-9.-]+", host)
        or any(
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in host.split(".")
        )
        or host.rsplit(".", 1)[-1].isdigit()
        or host.lower().endswith((".localhost", ".local", ".internal", ".onion"))
        or host.lower() == "localhost"
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise ValueError("回调地址须为无凭据、无片段的 HTTPS 域名地址")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("回调地址不能使用 IP 地址")
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("回调地址不能包含重复查询参数")
    if any(
        key in {"code", "state", "iss", "error", "error_description"}
        for key, _ in pairs
    ):
        raise ValueError("回调地址不能预置 OAuth 响应参数")
    return value


def identifiers(values):
    if len(set(values)) != len(values) or any(
        not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value)
        for value in values
    ):
        raise ValueError("商品编号无效或重复")
    return values


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClientRegistration(Input):
    client_name: str = Field(min_length=1, max_length=100)
    redirect_uris: list[str] = Field(min_length=1, max_length=5)
    token_endpoint_auth_method: Literal["none"] = "none"
    grant_types: list[Literal["authorization_code", "refresh_token"]] = Field(
        default_factory=lambda: ["authorization_code", "refresh_token"]
    )
    response_types: list[Literal["code"]] = Field(default_factory=lambda: ["code"])

    @field_validator("client_name")
    @classmethod
    def name(cls, value):
        value = value.strip()
        if not value or any(
            ord(ch) < 32 or 127 <= ord(ch) <= 159 or 0xD800 <= ord(ch) <= 0xDFFF
            for ch in value
        ):
            raise ValueError("商城名称不能为空或包含控制字符")
        return value

    @field_validator("redirect_uris")
    @classmethod
    def redirects(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("回调地址不能重复")
        return [redirect_uri(value) for value in values]

    @model_validator(mode="after")
    def profile(self):
        if sorted(self.grant_types) != ["authorization_code", "refresh_token"]:
            raise ValueError("导入协议须支持授权码与刷新令牌")
        if self.response_types != ["code"]:
            raise ValueError("导入协议仅支持授权码")
        return self


class CardLimit(Input):
    product_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    variant_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    max_count: int = Field(strict=True, ge=1, le=10000)


class Consent(Input):
    shop_id: str = Field(min_length=1, max_length=100)
    review_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class Approval(Consent):
    scopes: list[Literal["products.read", "cards.issue"]] = Field(
        min_length=1, max_length=2
    )
    product_ids: list[str] = Field(min_length=1, max_length=MAX_PRODUCTS)
    card_limits: list[CardLimit] = Field(default_factory=list, max_length=MAX_LIMITS)
    grant_expires: float = Field(strict=True, allow_inf_nan=False)

    @field_validator("product_ids")
    @classmethod
    def products(cls, values):
        return identifiers(values)

    @model_validator(mode="after")
    def limits(self):
        if len(set(self.scopes)) != len(self.scopes):
            raise ValueError("权限不能重复")
        self.scopes = [scope for scope in SCOPES if scope in self.scopes]
        if ("cards.issue" in self.scopes) != bool(self.card_limits):
            raise ValueError("发行卡密权限必须明确设置至少一个规格额度")
        pairs = [(item.product_id, item.variant_id) for item in self.card_limits]
        if len(set(pairs)) != len(pairs):
            raise ValueError("发行规格额度不能重复")
        if any(item.product_id not in self.product_ids for item in self.card_limits):
            raise ValueError("发行额度必须属于批准的商品")
        return self


class Issue(Input):
    product_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    variant_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,39}$")
    count: int = Field(strict=True, ge=1, le=MAX_ISSUE_COUNT)
    label: str = Field(default="", max_length=100)
    expires: float | None = Field(default=None, strict=True, allow_inf_nan=False)
    expected_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("label")
    @classmethod
    def label_text(cls, value):
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("批次名称不能包含控制字符")
        return value

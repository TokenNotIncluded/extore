"""Merchant-authored instructions, separate from customer task payloads."""

import hashlib
import json
import unicodedata

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import audit, db
from .security import authorize_management, fail, session
from .shops import authorize_product, shop_row

router = APIRouter(prefix="/api")
MAX_SLOGAN_LENGTH = 4000
SCHEMA = "extore.work-instructions.v1"


def normalize_slogan(value):
    """Keep Markdown intact, while excluding invalid Unicode/control bytes."""
    if not isinstance(value, str) or len(value) > MAX_SLOGAN_LENGTH:
        raise ValueError("标语必须是文本，最多四千字符")
    if any(
        unicodedata.category(char) in {"Cc", "Cs"} and char not in "\t\r\n"
        for char in value
    ):
        raise ValueError("标语不能包含控制字符或无效 Unicode")
    return value if value.strip() else ""


def stored_slogan(value):
    try:
        return normalize_slogan(value)
    except ValueError:
        fail("标语配置无效，请由管理者修改", 409)


class FactoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    factory_slogan: str = Field(max_length=MAX_SLOGAN_LENGTH)

    @field_validator("factory_slogan")
    @classmethod
    def valid_slogan(cls, value):
        return normalize_slogan(value)


class WorkshopUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    workshop_slogan: str = Field(max_length=MAX_SLOGAN_LENGTH)

    @field_validator("workshop_slogan")
    @classmethod
    def valid_slogan(cls, value):
        return normalize_slogan(value)


def _factory_scope(c, s, requested):
    authorize_management(c, s)
    if s["role"] != "admin":
        fail("只有店铺管理者可以修改工厂标语", 403)
    own = s.get("shop_id")
    if own is not None and requested not in ("", own):
        fail("没有此店铺的管理权限", 403)
    sid = own or requested
    if not sid:
        fail("请先选择要配置的店铺", 400)
    return shop_row(c, sid)


def _factory_view(shop):
    return {
        "shop_id": shop["id"],
        "shop_name": shop["name"],
        "factory_slogan": stored_slogan(shop["factory_slogan"]),
    }


@router.get("/admin/factory")
def factory(request: Request, shop_id: str = Query(default="", max_length=100)):
    s = session(request, ("admin", "staff"))
    with db() as c:
        return _factory_view(_factory_scope(c, s, shop_id))


@router.put("/admin/factory")
def update_factory(
    body: FactoryUpdate,
    request: Request,
    shop_id: str = Query(default="", max_length=100),
):
    s = session(request, ("admin", "staff"))
    with db() as c:
        shop = _factory_scope(c, s, shop_id)
        c.execute(
            "UPDATE shops SET factory_slogan=? WHERE id=?",
            (body.factory_slogan, shop["id"]),
        )
        actor = s.get("account_id") or (
            "shop:" + s["shop_id"] if s.get("shop_id") is not None else "owner"
        )
        # Record the edit and scope, never the instruction text in the audit.
        audit(c, actor, "factory.slogan.update", shop["id"])
        return _factory_view(shop_row(c, shop["id"]))


def instructions(c, product_id):
    """Trusted internal projection: no secrets, inputs, outputs or job reads.

    Callers must check their live management/execution authority first. The
    product/shop association always comes from storage, never customer input.
    """
    row = c.execute(
        "SELECT p.id,p.shop_id,s.enabled,s.factory_slogan,"
        "CASE WHEN json_valid(p.config) THEN CASE "
        "WHEN json_type(p.config,'$.workshop_slogan')='text' "
        "THEN json_extract(p.config,'$.workshop_slogan') "
        "WHEN json_type(p.config,'$.workshop_slogan') IS NULL THEN '' END END "
        "workshop_slogan FROM products p JOIN shops s ON s.id=p.shop_id WHERE p.id=?",
        (product_id,),
    ).fetchone()
    if row is None:
        fail("商品不存在", 404)
    if not row["enabled"]:
        fail("店铺不可用", 401)
    result = {
        "schema": SCHEMA,
        "shop_id": row["shop_id"],
        "product_id": row["id"],
        "factory_slogan": stored_slogan(row["factory_slogan"]),
        "workshop_slogan": stored_slogan(row["workshop_slogan"]),
    }
    # A content revision is stable between reads and requires no extra history
    # table or timestamp writes. Identical text has an identical revision.
    raw = json.dumps(result, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    result["revision"] = hashlib.sha256(raw.encode("ascii")).hexdigest()
    return result


@router.put("/manage/workshop")
def update_workshop(
    body: WorkshopUpdate,
    request: Request,
    product_id: str = Query(default="", max_length=100),
):
    from .product_lifecycle import require_active

    s = session(request, ("admin", "staff"))
    with db() as c:
        authorize_management(c, s, "product.edit")
        if s["role"] == "staff":
            if product_id and product_id != s["product_id"]:
                fail("没有此商品的管理权限", 403)
            product_id = s["product_id"]
        if not product_id:
            fail("请选择要配置的商品车间", 400)
        authorize_product(c, s, product_id)
        require_active(c, product_id)
        # A one-field JSON update preserves every delivery/configuration field
        # and never materializes processor secrets to serve the editor.
        c.execute(
            "UPDATE products SET config=json_set(config,'$.workshop_slogan',?) WHERE id=?",
            (body.workshop_slogan, product_id),
        )
        actor = (
            s.get("staff_id")
            or s.get("account_id")
            or ("shop:" + s["shop_id"] if s.get("shop_id") is not None else "owner")
        )
        audit(c, actor, "workshop.slogan.update", product_id)
        return {"product_id": product_id, "workshop_slogan": body.workshop_slogan}


@router.get("/manage/instructions")
def read_instructions(
    request: Request, product_id: str = Query(default="", max_length=100)
):
    s = session(request, ("admin", "staff"))
    with db() as c:
        authorize_management(c, s)
        if s["role"] == "staff":
            if not {"queue.view", "queue.monitor", "product.edit"}.intersection(
                s["permissions"]
            ):
                fail("此商品管理链接没有查看车间标语的权限", 403)
            if product_id and product_id != s["product_id"]:
                fail("没有此商品的管理权限", 403)
            product_id = s["product_id"]
        if not product_id:
            fail("请选择要查看的商品车间", 400)
        authorize_product(c, s, product_id)
        return instructions(c, product_id)

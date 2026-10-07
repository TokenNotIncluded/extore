"""Reversible product retirement; existing fulfillment remains authorized."""

import time

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import audit
from .security import fail


class DeleteProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool = Field(strict=True)

    @field_validator("confirmed")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("删除商品需要明确确认")
        return value


def init_schema(c):
    c.execute(
        "CREATE TABLE IF NOT EXISTS product_lifecycle ("
        "product_id TEXT PRIMARY KEY REFERENCES products(id),"
        "deleted_at REAL,deleted_by TEXT)"
    )


def metadata(c, product_id):
    row = c.execute(
        "SELECT deleted_at FROM product_lifecycle WHERE product_id=?", (product_id,)
    ).fetchone()
    return (
        {"deleted": True, "deleted_at": row["deleted_at"]}
        if row and row["deleted_at"] is not None
        else {}
    )


def require_active(c, product_id):
    if metadata(c, product_id):
        fail("商品已删除，请先从回收站恢复", 409)


def decorate(c, values):
    return {**values, **metadata(c, values["id"])}


def matches(c, product_id, view):
    deleted = bool(metadata(c, product_id))
    return view == "all" or (view == "deleted") == deleted


def set_deleted(c, product_id, actor, deleted):
    # Callers hold BEGIN IMMEDIATE and have already authorized this product.
    previous = metadata(c, product_id)
    if bool(previous) != deleted:
        if deleted:
            c.execute(
                "INSERT INTO product_lifecycle VALUES (?,?,?) "
                "ON CONFLICT(product_id) DO UPDATE SET deleted_at=excluded.deleted_at,deleted_by=excluded.deleted_by",
                (product_id, time.time(), actor),
            )
        else:
            c.execute(
                "UPDATE product_lifecycle SET deleted_at=NULL,deleted_by=NULL WHERE product_id=?",
                (product_id,),
            )
        audit(c, actor, "product.delete" if deleted else "product.restore", product_id)
    current = metadata(c, product_id)
    return {
        "ok": True,
        "product_id": product_id,
        "deleted": bool(current),
        "deleted_at": current.get("deleted_at"),
    }

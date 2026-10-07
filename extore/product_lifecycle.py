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


class EmptyTrash(DeleteProduct):
    product_ids: list[str] = Field(min_length=1, max_length=500)

    @field_validator("product_ids")
    @classmethod
    def explicit_unique_products(cls, values):
        if len(set(values)) != len(values) or any(
            not value or len(value) > 100 for value in values
        ):
            raise ValueError("请选择不重复的商品编号")
        return values


def init_schema(c):
    c.execute(
        "CREATE TABLE IF NOT EXISTS product_lifecycle ("
        "product_id TEXT PRIMARY KEY REFERENCES products(id),"
        "deleted_at REAL,deleted_by TEXT)"
    )

    c.execute(
        "CREATE TABLE IF NOT EXISTS product_purges ("
        "product_id TEXT PRIMARY KEY REFERENCES products(id),"
        "purged_at REAL NOT NULL,purged_by TEXT NOT NULL)"
    )


def metadata(c, product_id):
    row = c.execute(
        "SELECT deleted_at FROM product_lifecycle WHERE product_id=?", (product_id,)
    ).fetchone()
    values = (
        {"deleted": True, "deleted_at": row["deleted_at"]}
        if row and row["deleted_at"] is not None
        else {}
    )
    purge = c.execute(
        "SELECT purged_at FROM product_purges WHERE product_id=?", (product_id,)
    ).fetchone()
    if purge:
        values.update(deleted=True, purged=True, purged_at=purge["purged_at"])
    return values


def require_active(c, product_id):
    values = metadata(c, product_id)
    if values.get("purged"):
        fail("商品已永久移出回收站，不能再次发行或修改", 409)
    if values:
        fail("商品已删除，请先从回收站恢复", 409)


def decorate(c, values):
    return {**values, **metadata(c, values["id"])}


def matches(c, product_id, view):
    values = metadata(c, product_id)
    if view == "history":
        return True
    if values.get("purged"):
        return False
    return view == "all" or (view == "deleted") == bool(values)


def set_deleted(c, product_id, actor, deleted):
    # Callers hold BEGIN IMMEDIATE and have already authorized this product.
    previous = metadata(c, product_id)
    if previous.get("purged") and not deleted:
        fail("商品已永久移出回收站，不能恢复", 409)
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


def require_trash(c, product_id):
    if not c.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone():
        fail("商品不存在", 404)
    values = metadata(c, product_id)
    if not values.get("deleted"):
        fail("只能清空已删除的商品，请重新确认回收站", 409)
    return values


def purge_products(c, product_ids, actor):
    # Validate the whole explicit snapshot before the first mutation. The
    # surrounding write transaction serializes restore/delete/issuance/purge.
    if not c.in_transaction:
        raise RuntimeError("Clearing trash requires a write transaction")
    for pid in product_ids:
        require_trash(c, pid)
    for pid in product_ids:
        if not metadata(c, pid).get("purged"):
            c.execute(
                "INSERT INTO product_purges VALUES (?,?,?)", (pid, time.time(), actor)
            )
            audit(c, actor, "product.purge", pid)
    return {
        "ok": True,
        "purged_product_ids": list(product_ids),
        "purged_count": len(product_ids),
        "preserved_fulfillment": True,
    }


def purge_product(c, product_id, actor):
    purge_products(c, [product_id], actor)
    return {
        "ok": True,
        "product_id": product_id,
        "deleted": True,
        "purged": True,
        "purged_at": metadata(c, product_id)["purged_at"],
    }

"""Revocable shop-wide catalogue credentials; never authorize card issuance."""

import re
import time
import uuid

from fastapi import APIRouter, Request
from pydantic import Field

from .commerce_models import MAX_GRANT_DAYS, Input
from .commerce_oauth import _browser, _scope
from .commerce_store import fail
from .config import ORIGIN
from .db import audit, db
from .security import digest, rate_limit, token

router = APIRouter(tags=["Shop catalogue binding"])
PREFIX = "extore_bind_v1_"


def init_schema(c):
    c.execute(
        "CREATE TABLE IF NOT EXISTS commerce_bindings ("
        "id TEXT PRIMARY KEY,shop_id TEXT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,"
        "key_digest TEXT UNIQUE NOT NULL,label TEXT NOT NULL,created REAL NOT NULL,"
        "expires REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)))"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS commerce_bindings_shop ON commerce_bindings(shop_id,revoked)"
    )


class BindingInput(Input):
    label: str = Field(default="商城", min_length=1, max_length=100)
    valid_days: int = Field(default=30, ge=1, le=MAX_GRANT_DAYS)


def view(row):
    return {
        key: row[key]
        for key in ("id", "shop_id", "label", "created", "expires", "revoked")
    }


@router.get("/api/admin/commerce/bindings")
def bindings(request: Request, shop_id: str = ""):
    actor = _browser(request)
    with db() as c:
        own = _scope(c, actor, shop_id)
        return {
            "bindings": [
                view(row)
                for row in c.execute(
                    "SELECT * FROM commerce_bindings WHERE shop_id=? ORDER BY created DESC,id",
                    (own,),
                )
            ]
        }


@router.post("/api/admin/commerce/bindings")
def create_binding(body: BindingInput, request: Request, shop_id: str = ""):
    actor = _browser(request, fresh=True)
    rate_limit(request, "commerce-binding-create", 20, 60)
    with db() as c:
        own = _scope(c, actor, shop_id)
        if (
            c.execute(
                "SELECT COUNT(*) FROM commerce_bindings WHERE shop_id=? AND revoked=0 AND expires>?",
                (own, time.time()),
            ).fetchone()[0]
            >= 50
        ):
            fail("binding_limit", "请先撤销不再使用的店铺绑定 ID", 409)
        identifier, secret, now = str(uuid.uuid4()), PREFIX + token(), time.time()
        c.execute(
            "INSERT INTO commerce_bindings VALUES (?,?,?,?,?,?,0)",
            (
                identifier,
                own,
                digest(secret),
                body.label,
                now,
                now + body.valid_days * 86400,
            ),
        )
        audit(c, "shop:" + own, "commerce.binding.create", identifier)
        row = c.execute(
            "SELECT * FROM commerce_bindings WHERE id=?", (identifier,)
        ).fetchone()
        # The secret is disclosed once, only to the authenticated shop owner.
        return {"binding": view(row), "binding_id": secret}


@router.delete("/api/admin/commerce/bindings/{binding_id}")
def revoke_binding(binding_id: str, request: Request, shop_id: str = ""):
    actor = _browser(request, fresh=True)
    with db() as c:
        own = _scope(c, actor, shop_id)
        changed = c.execute(
            "UPDATE commerce_bindings SET revoked=1 WHERE id=? AND shop_id=?",
            (binding_id, own),
        )
        if changed.rowcount != 1:
            fail("binding_not_found", "店铺绑定不存在", 404)
        audit(c, "shop:" + own, "commerce.binding.revoke", binding_id)
        return {"ok": True}


def authenticate(c, request):
    authorization = request.headers.get("authorization", "")
    if not re.fullmatch(r"Bearer " + PREFIX + r"[A-Za-z0-9_-]{43}", authorization):
        fail("invalid_token", "店铺绑定无效、已撤销或到期", 401)
    row = c.execute(
        "SELECT b.* FROM commerce_bindings b JOIN shops s ON s.id=b.shop_id "
        "WHERE b.key_digest=? AND b.revoked=0 AND b.expires>? AND s.enabled=1",
        (digest(authorization[7:]), time.time()),
    ).fetchone()
    if row is None:
        fail("invalid_token", "店铺绑定无效、已撤销或到期", 401)
    return row


@router.get("/api/integrations/commerce/binding/products")
def catalogue(request: Request, after: str = ""):
    from .card_tracking import _summary
    from .commerce_oauth import _marketplace_listing
    from .product_lifecycle import metadata as lifecycle
    from .shops import shop_row

    rate_limit(request, "commerce-binding-read", 120, 60)
    if len(after) > 100:
        fail("invalid_request", "商品游标无效")
    with db() as c:
        binding = authenticate(c, request)
        shop = shop_row(c, binding["shop_id"])
        # Read current shop membership on every call, including newly added products.
        rows = c.execute(
            "SELECT id FROM products WHERE shop_id=? AND id>? ORDER BY id LIMIT 101",
            (shop["id"], after),
        ).fetchall()
        values = []
        for row in rows[:100]:
            if lifecycle(c, row["id"]):
                continue
            listing = _marketplace_listing(c, row["id"])
            listing["card_summary"] = _summary(
                c, row["id"], time.time(), shop_id=shop["id"]
            )
            for variant in listing["variants"]:
                variant["card_summary"] = _summary(
                    c,
                    row["id"],
                    time.time(),
                    variant_id=variant["id"],
                    shop_id=shop["id"],
                )
            values.append(listing)
        return {
            "schema": "extore.shop-catalog.v1",
            "issuer": ORIGIN,
            "shop": {"id": shop["id"], "name": shop["name"]},
            "binding": view(binding),
            "products": values,
            "next_cursor": rows[99]["id"] if len(rows) > 100 else "",
        }

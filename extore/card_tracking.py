"""Safe card inventory and lifecycle reporting; redemption secrets stay private."""

import json
import math
import re
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Query, Request

from .card_batches import router as batch_router
from .db import db
from .security import authorize_management, card_digest, fail, session

router = APIRouter()
STATUSES = (
    "unused",
    "needs_input",
    "queued",
    "processing",
    "succeeded",
    "failed_retryable",
    "failed_terminal",
    "destroyed",
    "revoked",
    "expired",
    "rejected",
)
CardStatus = Literal[
    "",
    "unused",
    "needs_input",
    "queued",
    "processing",
    "succeeded",
    "failed_retryable",
    "failed_terminal",
    "destroyed",
    "revoked",
    "expired",
    "rejected",
]


def init_schema(c):
    # Execute individually: executescript would commit the surrounding migration.
    c.execute(
        "CREATE TABLE IF NOT EXISTS card_batches ("
        "id TEXT PRIMARY KEY, product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE, "
        "label TEXT NOT NULL DEFAULT '', created REAL NOT NULL, count INTEGER NOT NULL)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS card_meta ("
        "card_id TEXT PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE, "
        "batch_id TEXT REFERENCES card_batches(id) ON DELETE SET NULL, "
        "code_suffix TEXT, expires REAL, first_verified REAL)"
    )
    # Add SKU metadata without rewriting the cards table or existing rows.
    for table in ("card_meta", "card_batches"):
        columns = {row["name"] for row in c.execute(f"PRAGMA table_info({table})")}
        if "variant_id" not in columns:
            c.execute(
                f"ALTER TABLE {table} ADD COLUMN variant_id TEXT NOT NULL DEFAULT 'default'"
            )
        if table == "card_meta" and "variant_snapshot" not in columns:
            c.execute(
                "ALTER TABLE card_meta ADD COLUMN variant_snapshot TEXT NOT NULL DEFAULT '{}'"
            )
        # Archive folders independently of fulfillment. A purged batch leaves
        # job-linked card metadata hidden, never reclassified as legacy stock.
        archive_columns = (
            (("deleted_at", "REAL"),)
            if table == "card_batches"
            else (
                ("batch_deleted_at", "REAL"),
                ("batch_previous_state", "TEXT"),
            )
        )
        for name, kind in archive_columns:
            if name not in columns:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    c.execute("CREATE INDEX IF NOT EXISTS card_meta_batch ON card_meta(batch_id)")
    c.execute("CREATE INDEX IF NOT EXISTS card_meta_expiry ON card_meta(expires)")
    c.execute("CREATE INDEX IF NOT EXISTS card_meta_variant ON card_meta(variant_id)")
    c.execute(
        "CREATE INDEX IF NOT EXISTS cards_product_created ON cards(product_id,created)"
    )
    c.execute("CREATE INDEX IF NOT EXISTS events_job_created ON events(job_id,created)")


def record_issue(
    c,
    pid,
    codes,
    label="",
    expires=None,
    variant_id="default",
    variant_snapshot=None,
    attributes=None,
):
    """Attach one issuance batch without retaining plaintext codes or digests."""
    if not isinstance(label, str) or len(label.strip()) > 100:
        fail("卡密批次名称最多 100 字")
    if not isinstance(variant_id, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9_-]{0,39}", variant_id
    ):
        fail("规格代码无效")
    if variant_id != "default" and variant_snapshot is None:
        fail("发行此规格需要完整的规格快照")
    snapshot = {}
    if variant_snapshot is not None:
        from .models import ProductVariant

        if not isinstance(variant_snapshot, dict):
            fail("规格快照无效")
        if variant_snapshot.get("id", variant_id) != variant_id:
            fail("规格快照与发行规格不一致")
        # The caller supplies validated product metadata. Never freeze arbitrary
        # configuration, processor credentials, input parameters or results.
        snapshot = {
            key: variant_snapshot[key]
            for key in (
                "id",
                "name",
                "description",
                "price",
                "currency",
                "attributes",
                "enabled",
            )
            if key in variant_snapshot
        }
        snapshot["id"] = variant_id
        try:
            snapshot = ProductVariant.model_validate(snapshot).model_dump()
        except ValueError:
            fail("规格快照无效")
    try:
        frozen = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        fail("规格快照无效")
    if expires is not None:
        try:
            expiry = float(expires)
        except (TypeError, ValueError, OverflowError):
            fail("卡密到期时间必须是未来时间")
        if (
            isinstance(expires, bool)
            or not math.isfinite(expiry)
            or expiry <= time.time()
        ):
            fail("卡密到期时间必须是未来时间")
        expires = expiry
    rows = []
    seen = set()
    for code in codes:
        row = c.execute(
            "SELECT cards.id, cards.product_id, card_meta.card_id AS recorded "
            "FROM cards LEFT JOIN card_meta ON card_meta.card_id=cards.id WHERE cards.digest=?",
            (card_digest(code),),
        ).fetchone()
        if row is None or row["product_id"] != pid:
            fail("卡密不属于此商品", 409)
        if row["recorded"] is None and row["id"] not in seen:
            suffix = code.strip().upper().replace("-", "").replace(" ", "")[-6:]
            rows.append((row["id"], suffix))
            seen.add(row["id"])
    if not rows:
        return None
    batch_id = str(uuid.uuid4())
    c.execute(
        "INSERT INTO card_batches(id,product_id,label,created,count,variant_id) VALUES (?,?,?,?,?,?)",
        (batch_id, pid, label.strip(), time.time(), len(rows), variant_id),
    )
    c.executemany(
        "INSERT INTO card_meta(card_id,batch_id,code_suffix,expires,variant_id,variant_snapshot) VALUES (?,?,?,?,?,?)",
        [(cid, batch_id, suffix, expires, variant_id, frozen) for cid, suffix in rows],
    )
    # Issuance tools on pre-migration minimal databases keep their legacy SKU
    # snapshot. Normal installations always freeze the explicit null policy too.
    if c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='card_entitlements'"
    ).fetchone():
        from .card_entitlements import freeze_card
        from .variants import validate_attributes

        config = json.loads(
            c.execute("SELECT config FROM products WHERE id=?", (pid,)).fetchone()[0]
        )
        effective = validate_attributes(
            {**snapshot.get("attributes", {}), **(attributes or {})}
        )
        policy = config.get("revision_policy")
        for cid, _ in rows:
            freeze_card(c, cid, effective, policy)
    return batch_id


def record_verified(c, cid):
    """Record only the first successful card check, including legacy cards."""
    c.execute(
        "INSERT INTO card_meta(card_id,first_verified) VALUES (?,?) "
        "ON CONFLICT(card_id) DO UPDATE SET first_verified=COALESCE(card_meta.first_verified,excluded.first_verified)",
        (cid, time.time()),
    )


def card_expired(c, cid):
    row = c.execute("SELECT expires FROM card_meta WHERE card_id=?", (cid,)).fetchone()
    return bool(row and row["expires"] is not None and row["expires"] <= time.time())


def ensure_card_usable(c, card):
    if card["state"] == "revoked":
        fail("卡密已撤销", 410)
    row = c.execute("SELECT state FROM jobs WHERE card_id=?", (card["id"],)).fetchone()
    if card["state"] == "rejected" or (row and row["state"] == "rejected"):
        fail("卡密已被拒绝，无法再次兑换", 410)
    # Expiry limits new work, never interrupts an accepted order or old receipt.
    if (row is None or row["state"] in ("failed", "needs_input")) and card_expired(
        c, card["id"]
    ):
        fail("卡密已过期，无法兑换或重试", 410)


_INVENTORY = """
WITH inventory AS (
 SELECT c.id,c.product_id,json_extract(p.config,'$.name') AS product_name,
 c.state,c.created,m.code_suffix,m.batch_id,b.label AS batch_label,m.expires,m.first_verified,
 COALESCE(NULLIF(m.variant_id,''),'default') AS variant_id,
 COALESCE(NULLIF(json_extract(CASE WHEN json_valid(m.variant_snapshot) THEN m.variant_snapshot ELSE '{}' END,'$.name'),''),
  CASE WHEN COALESCE(NULLIF(m.variant_id,''),'default')='default' THEN '默认规格' ELSE m.variant_id END) AS variant_name,
 j.id AS job_id,j.state AS job_state,j.attempt,COALESCE(j.retryable,0) AS retryable,
 COALESCE(j.revealed,0) AS revealed,j.created AS used_at,
 MAX(c.created,COALESCE(j.updated,c.created),COALESCE(m.first_verified,c.created)) AS updated,
 CASE
  WHEN c.state='revoked' THEN 'revoked'
  WHEN c.state='rejected' OR j.state='rejected' THEN 'rejected'
  WHEN j.state='destroyed' THEN 'destroyed'
  WHEN j.state IN ('queued','processing','succeeded') THEN j.state
  WHEN j.state='failed' AND j.retryable=1
   AND COALESCE(json_extract(p.config,'$.allow_retry'),1)=1
   AND j.attempt-j.revision_start_attempt+1<COALESCE(json_extract(p.config,'$.max_attempts'),3)
   AND (m.expires IS NULL OR m.expires>:now) THEN 'failed_retryable'
  WHEN j.state='failed' THEN 'failed_terminal'
  WHEN (j.id IS NULL OR j.state='needs_input') AND m.expires IS NOT NULL AND m.expires<=:now THEN 'expired'
  WHEN j.state='needs_input' THEN 'needs_input'
  ELSE 'unused'
 END AS status
 FROM cards c JOIN products p ON p.id=c.product_id
 LEFT JOIN card_meta m ON m.card_id=c.id
 LEFT JOIN card_batches b ON b.id=m.batch_id
 LEFT JOIN jobs j ON j.card_id=c.id
 WHERE (:product_id='' OR c.product_id=:product_id)
 AND (:shop_id='' OR p.shop_id=:shop_id)
)
"""


def _scope(c, s, product_id):
    from .shops import authorize_product

    authorize_management(c, s, "cards.manage")
    if s["role"] == "staff":
        if product_id and product_id != s["product_id"]:
            fail("无权查看此商品的卡密", 403)
        product_id = s["product_id"]
    if product_id:
        authorize_product(c, s, product_id)
    return product_id


def _shop_scope(s):
    # Legacy administrators have no shop binding. Tenant administrators must
    # retain their SQL scope even when no explicit product filter is supplied.
    return s.get("shop_id") or ""


def _empty_summary():
    return {
        "total": 0,
        "remaining": 0,
        "available": 0,
        "used": 0,
        "verified": 0,
        "viewed": 0,
        "in_progress": 0,
        "completed": 0,
        "failed": 0,
        "rejected": 0,
        "states": dict.fromkeys(STATUSES, 0),
    }


def _summary(c, pid, now, variant_id="", shop_id=""):
    result = _empty_summary()
    for row in c.execute(
        _INVENTORY + "SELECT status,COUNT(*) AS n,"
        "SUM(job_id IS NOT NULL AND (job_state!='needs_input' OR EXISTS(SELECT 1 FROM job_delivery_versions v WHERE v.job_id=inventory.job_id))) AS used,"
        "SUM(status IN ('unused','needs_input') AND NOT EXISTS(SELECT 1 FROM job_delivery_versions v WHERE v.job_id=inventory.job_id)) AS remaining,"
        "SUM(status IN ('unused','needs_input','failed_retryable') AND NOT EXISTS(SELECT 1 FROM job_delivery_versions v WHERE v.job_id=inventory.job_id)) AS available,"
        "SUM(first_verified IS NOT NULL) AS verified,SUM(revealed!=0 OR EXISTS(SELECT 1 FROM job_delivery_versions v WHERE v.job_id=inventory.job_id AND v.revealed!=0)) AS viewed "
        "FROM inventory WHERE (:variant_id='' OR variant_id=:variant_id) GROUP BY status",
        {
            "product_id": pid,
            "shop_id": shop_id,
            "now": now,
            "variant_id": variant_id,
        },
    ):
        result["states"][row["status"]] = row["n"]
        result["total"] += row["n"]
        for key in ("used", "verified", "viewed", "remaining", "available"):
            result[key] += row[key]
    counts = result["states"]
    result.update(
        in_progress=counts["queued"] + counts["processing"],
        completed=counts["succeeded"] + counts["destroyed"],
        failed=counts["failed_retryable"] + counts["failed_terminal"],
        rejected=counts["rejected"],
    )
    return result


def _sum_summaries(summaries):
    result = _empty_summary()
    for summary in summaries:
        for key in result:
            if key == "states":
                for state in STATUSES:
                    result["states"][state] += summary["states"][state]
            else:
                result[key] += summary[key]
    return result


def _variant_buckets(c, pid, config, now, shop_id=""):
    from .variants import default_variant, resolve_product_variants

    configured = resolve_product_variants(config)
    metadata = {variant["id"]: variant for variant in configured}
    issued = [
        row[0]
        for row in c.execute(
            _INVENTORY
            + "SELECT DISTINCT variant_id FROM inventory ORDER BY variant_id",
            {"product_id": pid, "shop_id": shop_id, "now": now},
        )
    ]
    for variant_id in issued:
        if variant_id in metadata:
            continue
        row = c.execute(
            "SELECT m.variant_snapshot FROM cards c LEFT JOIN card_meta m ON m.card_id=c.id "
            "WHERE c.product_id=? AND COALESCE(NULLIF(m.variant_id,''),'default')=? "
            "ORDER BY c.created,c.id LIMIT 1",
            (pid, variant_id),
        ).fetchone()
        try:
            snapshot = json.loads(row[0]) if row and row[0] else {}
        except (TypeError, ValueError):
            snapshot = {}
        if not isinstance(snapshot, dict):
            snapshot = {}
        fallback = (
            default_variant()
            if variant_id == "default"
            else {
                "id": variant_id,
                "name": variant_id,
                "description": "",
                "price": None,
                "currency": "CNY",
                "attributes": {},
                "enabled": False,
            }
        )
        metadata[variant_id] = {
            **fallback,
            **{
                key: snapshot[key]
                for key in ("name", "description", "price", "currency")
                if key in snapshot
            },
            "enabled": False,
        }
    return [
        {
            "variant_id": variant_id,
            **{
                key: variant[key]
                for key in ("name", "description", "price", "currency", "enabled")
            },
            "summary": _summary(c, pid, now, variant_id, shop_id),
        }
        for variant_id, variant in metadata.items()
    ]


def _stats(c, pid, shop_id=""):
    now = time.time()
    products = []
    for row in c.execute(
        "SELECT id,json_extract(config,'$.name') AS name,config FROM products "
        "WHERE (?='' OR id=?) AND (?='' OR shop_id=?) ORDER BY created,id",
        (pid, pid, shop_id, shop_id),
    ).fetchall():
        variants = _variant_buckets(
            c, row["id"], json.loads(row["config"]), now, shop_id
        )
        products.append(
            {
                "product_id": row["id"],
                "product_name": row["name"],
                **_sum_summaries(variant["summary"] for variant in variants),
                "variants": variants,
            }
        )
    return {
        "summary": _sum_summaries(products),
        "products": products,
        "variants": products[0]["variants"] if pid and products else [],
    }


def _inventory(
    c, pid, status, batch_id, search, offset, limit, variant_id="", shop_id=""
):
    now = time.time()
    params = {
        "product_id": pid,
        "shop_id": shop_id,
        "now": now,
        "status": status,
        "batch_id": batch_id,
        "search": search.strip().upper(),
        "offset": offset,
        "limit": limit,
        "variant_id": variant_id,
    }
    where = (
        " WHERE (:status='' OR status=:status) AND (:batch_id='' OR batch_id=:batch_id"
        " OR (:batch_id='legacy' AND batch_id IS NULL AND id NOT IN"
        " (SELECT card_id FROM card_meta WHERE batch_deleted_at IS NOT NULL)))"
        " AND (:variant_id='' OR variant_id=:variant_id)"
        " AND (:search='' OR instr(upper(id),:search)>0 OR instr(upper(COALESCE(code_suffix,'')),:search)>0)"
    )
    total = c.execute(
        _INVENTORY + "SELECT COUNT(*) FROM inventory" + where, params
    ).fetchone()[0]
    items = [
        dict(row)
        for row in c.execute(
            _INVENTORY
            + "SELECT * FROM inventory"
            + where
            + " ORDER BY created DESC,id LIMIT :limit OFFSET :offset",
            params,
        )
    ]
    return {
        "items": items,
        "total": total,
        "summary": _summary(c, pid, now, shop_id=shop_id),
        "offset": offset,
        "limit": limit,
    }


def _history(c, s, cid, pid):
    from .shops import authorize_product

    row = c.execute("SELECT product_id FROM cards WHERE id=?", (cid,)).fetchone()
    if row is None:
        fail("卡密不存在", 404)
    if pid and row["product_id"] != pid:
        fail("无权查看此商品的卡密", 403)
    authorize_product(c, s, row["product_id"])
    card = dict(
        c.execute(
            _INVENTORY + "SELECT * FROM inventory WHERE id=:card_id",
            {
                "product_id": row["product_id"],
                "shop_id": _shop_scope(s),
                "now": time.time(),
                "card_id": cid,
            },
        ).fetchone()
    )
    timeline = [
        {"id": f"issued:{cid}", "type": "card.issued", "created": card["created"]}
    ]
    if card["first_verified"] is not None:
        timeline.append(
            {
                "id": f"verified:{cid}",
                "type": "card.verified",
                "created": card["first_verified"],
            }
        )
    if card["job_id"]:
        for event in c.execute(
            "SELECT id,type,created,payload FROM events WHERE job_id=? AND product_id=? ORDER BY created,id",
            (card["job_id"], card["product_id"]),
        ):
            try:
                payload = json.loads(event["payload"])
                data = payload.get("data", {}) if isinstance(payload, dict) else {}
                if not isinstance(data, dict):
                    data = {}
            except (TypeError, ValueError):
                data = {}
            item = {
                "id": event["id"],
                "type": event["type"],
                "created": event["created"],
            }
            # Never return arbitrary event payloads or free-text messages.
            for key in ("attempt", "progress"):
                value = data.get(key)
                if type(value) is int and 0 <= value <= (
                    100 if key == "progress" else 1000000
                ):
                    item[key] = value
            if data.get("state") in (
                "queued",
                "processing",
                "succeeded",
                "failed",
                "needs_input",
                "rejected",
                "destroyed",
            ):
                item["state"] = data["state"]
            if item["type"] == "redemption.requested" and item.get("attempt", 1) > 1:
                item["type"] = "redemption.retried"
            timeline.append(item)
    for audit in c.execute(
        "SELECT id,created FROM audit WHERE action='card.revoke' AND target=?", (cid,)
    ):
        timeline.append(
            {
                "id": f"revoked:{audit['id']}",
                "type": "card.revoked",
                "created": audit["created"],
            }
        )
    timeline.sort(key=lambda item: (item["created"], item["id"]))
    return {"card": card, "timeline": timeline}


@router.get("/api/admin/card-stats")
def admin_card_stats(
    request: Request, product_id: str = Query(default="", max_length=100)
):
    s = session(request)
    with db() as c:
        return _stats(c, _scope(c, s, product_id), _shop_scope(s))


@router.get("/api/manage/card-stats")
def managed_card_stats(
    request: Request, product_id: str = Query(default="", max_length=100)
):
    s = session(request, ("admin", "staff"))
    with db() as c:
        return _stats(c, _scope(c, s, product_id), _shop_scope(s))


@router.get("/api/admin/card-inventory")
def admin_card_inventory(
    request: Request,
    product_id: str = Query(default="", max_length=100),
    variant_id: str = Query(default="", max_length=40),
    status: CardStatus = "",
    batch_id: str = Query(default="", max_length=100),
    search: str = Query(default="", max_length=100),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    s = session(request)
    with db() as c:
        return _inventory(
            c,
            _scope(c, s, product_id),
            status,
            batch_id,
            search,
            offset,
            limit,
            variant_id,
            _shop_scope(s),
        )


@router.get("/api/manage/card-inventory")
def managed_card_inventory(
    request: Request,
    product_id: str = Query(default="", max_length=100),
    variant_id: str = Query(default="", max_length=40),
    status: CardStatus = "",
    batch_id: str = Query(default="", max_length=100),
    search: str = Query(default="", max_length=100),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    s = session(request, ("admin", "staff"))
    with db() as c:
        return _inventory(
            c,
            _scope(c, s, product_id),
            status,
            batch_id,
            search,
            offset,
            limit,
            variant_id,
            _shop_scope(s),
        )


@router.get("/api/admin/cards/{cid}/history")
def admin_card_history(
    cid: str, request: Request, product_id: str = Query(default="", max_length=100)
):
    s = session(request)
    with db() as c:
        return _history(c, s, cid, _scope(c, s, product_id))


@router.get("/api/manage/cards/{cid}/history")
def managed_card_history(
    cid: str, request: Request, product_id: str = Query(default="", max_length=100)
):
    s = session(request, ("admin", "staff"))
    with db() as c:
        return _history(c, s, cid, _scope(c, s, product_id))


router.include_router(batch_router)

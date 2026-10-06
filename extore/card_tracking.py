"""Safe card inventory and lifecycle reporting; redemption secrets stay private."""

import json
import math
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Query, Request

from .db import db
from .security import authorize_management, card_digest, fail, session

router = APIRouter()
STATUSES = (
    "unused",
    "queued",
    "processing",
    "succeeded",
    "failed_retryable",
    "failed_terminal",
    "destroyed",
    "revoked",
    "expired",
)
CardStatus = Literal[
    "",
    "unused",
    "queued",
    "processing",
    "succeeded",
    "failed_retryable",
    "failed_terminal",
    "destroyed",
    "revoked",
    "expired",
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
    c.execute("CREATE INDEX IF NOT EXISTS card_meta_batch ON card_meta(batch_id)")
    c.execute("CREATE INDEX IF NOT EXISTS card_meta_expiry ON card_meta(expires)")
    c.execute(
        "CREATE INDEX IF NOT EXISTS cards_product_created ON cards(product_id,created)"
    )
    c.execute("CREATE INDEX IF NOT EXISTS events_job_created ON events(job_id,created)")


def record_issue(c, pid, codes, label="", expires=None):
    """Attach one issuance batch without retaining plaintext codes or digests."""
    if not isinstance(label, str) or len(label.strip()) > 100:
        fail("卡密批次名称最多 100 字")
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
    for code in codes:
        row = c.execute(
            "SELECT cards.id, cards.product_id, card_meta.card_id AS recorded "
            "FROM cards LEFT JOIN card_meta ON card_meta.card_id=cards.id WHERE cards.digest=?",
            (card_digest(code),),
        ).fetchone()
        if row is None or row["product_id"] != pid:
            fail("卡密不属于此商品", 409)
        if row["recorded"] is None and row["id"] not in {r[0] for r in rows}:
            suffix = code.strip().upper().replace("-", "").replace(" ", "")[-6:]
            rows.append((row["id"], suffix))
    if not rows:
        return None
    batch_id = str(uuid.uuid4())
    c.execute(
        "INSERT INTO card_batches(id,product_id,label,created,count) VALUES (?,?,?,?,?)",
        (batch_id, pid, label.strip(), time.time(), len(rows)),
    )
    c.executemany(
        "INSERT INTO card_meta(card_id,batch_id,code_suffix,expires) VALUES (?,?,?,?)",
        [(cid, batch_id, suffix, expires) for cid, suffix in rows],
    )
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
    # Expiry limits new work, never interrupts an accepted order or old receipt.
    if (row is None or row["state"] == "failed") and card_expired(c, card["id"]):
        fail("卡密已过期，无法兑换或重试", 410)


_INVENTORY = """
WITH inventory AS (
 SELECT c.id,c.product_id,json_extract(p.config,'$.name') AS product_name,
 c.state,c.created,m.code_suffix,m.batch_id,b.label AS batch_label,m.expires,m.first_verified,
 j.id AS job_id,j.state AS job_state,j.attempt,COALESCE(j.retryable,0) AS retryable,
 COALESCE(j.revealed,0) AS revealed,j.created AS used_at,
 MAX(c.created,COALESCE(j.updated,c.created),COALESCE(m.first_verified,c.created)) AS updated,
 CASE
  WHEN c.state='revoked' THEN 'revoked'
  WHEN j.state='destroyed' THEN 'destroyed'
  WHEN j.state IN ('queued','processing','succeeded') THEN j.state
  WHEN j.state='failed' AND j.retryable=1
   AND COALESCE(json_extract(p.config,'$.allow_retry'),1)=1
   AND j.attempt<COALESCE(json_extract(p.config,'$.max_attempts'),3)
   AND (m.expires IS NULL OR m.expires>:now) THEN 'failed_retryable'
  WHEN j.state='failed' THEN 'failed_terminal'
  WHEN j.id IS NULL AND m.expires IS NOT NULL AND m.expires<=:now THEN 'expired'
  ELSE 'unused'
 END AS status
 FROM cards c JOIN products p ON p.id=c.product_id
 LEFT JOIN card_meta m ON m.card_id=c.id
 LEFT JOIN card_batches b ON b.id=m.batch_id
 LEFT JOIN jobs j ON j.card_id=c.id
 WHERE (:product_id='' OR c.product_id=:product_id)
)
"""


def _scope(c, s, product_id):
    authorize_management(c, s, "cards.manage")
    if s["role"] == "staff":
        if product_id and product_id != s["product_id"]:
            fail("无权查看此商品的卡密", 403)
        product_id = s["product_id"]
    if (
        product_id
        and not c.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone()
    ):
        fail("商品不存在", 404)
    return product_id


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
        "states": dict.fromkeys(STATUSES, 0),
    }


def _summary(c, pid, now):
    result = _empty_summary()
    for row in c.execute(
        _INVENTORY + "SELECT status,COUNT(*) AS n,SUM(job_id IS NOT NULL) AS used,"
        "SUM(first_verified IS NOT NULL) AS verified,SUM(revealed!=0) AS viewed "
        "FROM inventory GROUP BY status",
        {"product_id": pid, "now": now},
    ):
        result["states"][row["status"]] = row["n"]
        result["total"] += row["n"]
        for key in ("used", "verified", "viewed"):
            result[key] += row[key]
    counts = result["states"]
    result.update(
        remaining=counts["unused"],
        available=counts["unused"] + counts["failed_retryable"],
        in_progress=counts["queued"] + counts["processing"],
        completed=counts["succeeded"] + counts["destroyed"],
        failed=counts["failed_retryable"] + counts["failed_terminal"],
    )
    return result


def _stats(c, pid):
    now = time.time()
    products = []
    for row in c.execute(
        "SELECT id,json_extract(config,'$.name') AS name FROM products "
        "WHERE (?='' OR id=?) ORDER BY created,id",
        (pid, pid),
    ).fetchall():
        products.append(
            {
                "product_id": row["id"],
                "product_name": row["name"],
                **_summary(c, row["id"], now),
            }
        )
    return {"summary": _summary(c, pid, now), "products": products}


def _inventory(c, pid, status, batch_id, search, offset, limit):
    now = time.time()
    params = {
        "product_id": pid,
        "now": now,
        "status": status,
        "batch_id": batch_id,
        "search": search.strip().upper(),
        "offset": offset,
        "limit": limit,
    }
    where = (
        " WHERE (:status='' OR status=:status) AND (:batch_id='' OR batch_id=:batch_id)"
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
        "summary": _summary(c, pid, now),
        "offset": offset,
        "limit": limit,
    }


def _history(c, s, cid, pid):
    row = c.execute("SELECT product_id FROM cards WHERE id=?", (cid,)).fetchone()
    if row is None:
        fail("卡密不存在", 404)
    if pid and row["product_id"] != pid:
        fail("无权查看此商品的卡密", 403)
    card = dict(
        c.execute(
            _INVENTORY + "SELECT * FROM inventory WHERE id=:card_id",
            {"product_id": row["product_id"], "now": time.time(), "card_id": cid},
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
        return _stats(c, _scope(c, s, product_id))


@router.get("/api/manage/card-stats")
def managed_card_stats(
    request: Request, product_id: str = Query(default="", max_length=100)
):
    s = session(request, ("admin", "staff"))
    with db() as c:
        return _stats(c, _scope(c, s, product_id))


@router.get("/api/admin/card-inventory")
def admin_card_inventory(
    request: Request,
    product_id: str = Query(default="", max_length=100),
    status: CardStatus = "",
    batch_id: str = Query(default="", max_length=100),
    search: str = Query(default="", max_length=100),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    s = session(request)
    with db() as c:
        return _inventory(
            c, _scope(c, s, product_id), status, batch_id, search, offset, limit
        )


@router.get("/api/manage/card-inventory")
def managed_card_inventory(
    request: Request,
    product_id: str = Query(default="", max_length=100),
    status: CardStatus = "",
    batch_id: str = Query(default="", max_length=100),
    search: str = Query(default="", max_length=100),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    s = session(request, ("admin", "staff"))
    with db() as c:
        return _inventory(
            c, _scope(c, s, product_id), status, batch_id, search, offset, limit
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

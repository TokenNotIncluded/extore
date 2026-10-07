"""Batch folders and bounded cleanup that never deletes accepted fulfillment."""

import hashlib
import json
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import audit, db
from .security import fail, session

router = APIRouter()
BatchView = Literal["active", "deleted"]


def management_actor(s):
    if s["role"] == "staff":
        return s["staff_id"]
    return s.get("account_id") or (
        f"shop:{s['shop_id']}" if s.get("shop_id") is not None else "owner"
    )


class ConfirmBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: bool = Field(strict=True)

    @field_validator("confirmed")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("删除批次需要确认预览")
        return value


def _session(request):
    return (
        session(request, ("admin", "staff"))
        if request.url.path.startswith("/api/manage/")
        else session(request)
    )


def _product_scope(c, s, product_id):
    from .card_tracking import _scope

    pid = _scope(c, s, product_id)
    if not pid:
        fail("请先选择一个商品", 400)
    return pid


def _batch(c, pid, bid):
    if bid == "legacy":
        row = c.execute(
            "SELECT COUNT(*) AS count,MIN(c.created) AS created FROM cards c "
            "LEFT JOIN card_meta m ON m.card_id=c.id WHERE c.product_id=? "
            "AND m.batch_id IS NULL AND m.batch_deleted_at IS NULL",
            (pid,),
        ).fetchone()
        if not row["count"]:
            fail("批次不存在", 404)
        return {
            "id": "legacy",
            "label": "历史卡密",
            "created": row["created"],
            "count": row["count"],
            "product_id": pid,
            "deleted_at": None,
        }
    row = c.execute(
        "SELECT id,label,created,count,product_id,deleted_at FROM card_batches "
        "WHERE id=? AND product_id=?",
        (bid, pid),
    ).fetchone()
    if row is None:
        # Do not disclose whether the ID belongs to another tenant or product.
        fail("批次不存在", 404)
    if c.execute(
        "SELECT 1 FROM card_meta m JOIN cards c ON c.id=m.card_id "
        "WHERE m.batch_id=? AND c.product_id!=? LIMIT 1",
        (bid, pid),
    ).fetchone():
        fail("批次关联的商品范围异常，请检查数据", 409)
    return dict(row)


def _membership(bid):
    return (
        "c.product_id=:pid AND m.batch_id IS NULL AND m.batch_deleted_at IS NULL"
        if bid == "legacy"
        else "c.product_id=:pid AND m.batch_id=:bid"
    )


def list_batches(c, pid, view, variant_id, search, offset, limit):
    from .card_tracking import _INVENTORY, STATUSES

    params = {
        "product_id": pid,
        "shop_id": "",
        "now": time.time(),
        "view": view,
        "variant_id": variant_id,
        "search": search.strip(),
        "offset": offset,
        "limit": limit,
    }
    grouped = (
        _INVENTORY
        + ", folders AS ("
        + (
            "SELECT COALESCE(i.batch_id,'legacy') AS id,i.product_id,"
            "COALESCE(b.label,'历史卡密') AS label,COALESCE(b.created,MIN(i.created)) AS created,"
            "b.deleted_at,MIN(i.variant_id) AS variant_id,COUNT(*) AS total,"
            "SUM(i.status IN ('unused','needs_input')) AS remaining,"
            "SUM(i.status IN ('unused','needs_input','failed_retryable')) AS available,"
            "SUM(i.job_id IS NOT NULL AND i.job_state!='needs_input') AS used,"
            "SUM(i.status IN ('queued','processing')) AS in_progress,"
            "SUM(i.status IN ('succeeded','destroyed')) AS completed,"
            "SUM(i.status IN ('failed_retryable','failed_terminal')) AS failed "
            "FROM inventory i LEFT JOIN card_batches b ON b.id=i.batch_id "
            "LEFT JOIN card_meta m ON m.card_id=i.id "
            "WHERE (i.batch_id IS NOT NULL OR m.batch_deleted_at IS NULL) "
            "AND ((:view='active' AND b.deleted_at IS NULL) "
            "OR (:view='deleted' AND b.deleted_at IS NOT NULL)) "
            "AND (:variant_id='' OR i.variant_id=:variant_id) "
            "GROUP BY i.batch_id,i.product_id) "
        )
    )
    where = (
        "WHERE (:search='' OR instr(lower(label),lower(:search))>0 "
        "OR instr(lower(id),lower(:search))>0 OR id IN ("
        "SELECT COALESCE(batch_id,'legacy') FROM inventory WHERE "
        "(:variant_id='' OR variant_id=:variant_id) AND ("
        "instr(upper(id),upper(:search))>0 OR "
        "instr(upper(COALESCE(code_suffix,'')),upper(:search))>0)))"
    )
    total = c.execute(
        grouped + "SELECT COUNT(*) FROM folders " + where, params
    ).fetchone()[0]
    items = [
        dict(row)
        for row in c.execute(
            grouped
            + "SELECT * FROM folders "
            + where
            + " ORDER BY created DESC,id LIMIT :limit OFFSET :offset",
            params,
        )
    ]
    for item in items:
        item["legacy"] = item["id"] == "legacy"
        item["deleted"] = item["deleted_at"] is not None
        item["states"] = dict.fromkeys(STATUSES, 0)
        # The page is bounded; each query reads only one indexed batch. Legacy
        # is a stable per-product bucket and never includes purged history.
        for row in c.execute(
            _INVENTORY + "SELECT status,COUNT(*) AS n FROM inventory i "
            "LEFT JOIN card_meta m ON m.card_id=i.id WHERE "
            "((:bid='legacy' AND i.batch_id IS NULL AND m.batch_deleted_at IS NULL) OR i.batch_id=:bid) "
            "AND (:variant_id='' OR i.variant_id=:variant_id) GROUP BY status",
            {**params, "bid": item["id"]},
        ):
            item["states"][row["status"]] = row["n"]
    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "view": view,
    }


def preview(c, pid, bid, action):
    batch = _batch(c, pid, bid)
    if action == "purge" and batch["deleted_at"] is None:
        fail("请先把批次移入回收站，再清空", 409)
    if action == "delete" and batch["deleted_at"] is not None:
        fail("批次已经在回收站", 409)
    members = "FROM cards c LEFT JOIN card_meta m ON m.card_id=c.id "
    members += "LEFT JOIN jobs j ON j.card_id=c.id WHERE " + _membership(bid)
    params = {"pid": pid, "bid": bid}
    stats = c.execute(
        "SELECT COUNT(*) AS total,SUM(j.id IS NULL) AS unreferenced,"
        "SUM(c.state='ready' AND (j.id IS NULL OR j.state IN ('failed','needs_input'))) AS revocable,"
        "SUM(j.state IN ('queued','processing','waiting')) AS in_progress " + members,
        params,
    ).fetchone()
    revision = hashlib.sha256(
        json.dumps(
            ["extore.card-batch.v1", pid, bid, action, batch["deleted_at"]],
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    )
    for row in c.execute(
        "SELECT c.id,c.state,j.id,j.state,j.attempt,j.updated,m.first_verified,m.batch_deleted_at,m.batch_previous_state "
        + members
        + " ORDER BY c.id",
        params,
    ):
        revision.update(
            json.dumps(tuple(row), separators=(",", ":"), allow_nan=False).encode()
        )
    # A customer may upload a draft after the merchant sees a purge preview.
    # Bind that material to the revision too, without exposing names/content.
    for row in c.execute(
        "SELECT f.id,f.size,f.created,f.job_id,f.bound,f.released,f.consumed "
        "FROM job_files f JOIN cards c ON c.id=f.card_id "
        "LEFT JOIN card_meta m ON m.card_id=c.id WHERE "
        + _membership(bid)
        + " ORDER BY f.id",
        params,
    ):
        revision.update(
            json.dumps(tuple(row), separators=(",", ":"), allow_nan=False).encode()
        )
    total = stats["total"]
    removable = int(stats["unreferenced"] or 0) if action == "purge" else 0
    return {
        "action": action,
        "batch": {
            "id": bid,
            "label": batch["label"],
            "total": total,
            "deleted": batch["deleted_at"] is not None,
        },
        "delete_count": removable,
        "retain_count": total - removable,
        "revocable_count": int(stats["revocable"] or 0),
        "in_progress": int(stats["in_progress"] or 0),
        "revision": revision.hexdigest(),
        "explanation": "清空会永久删除未创建任务的卡密、预上传附件及文本库存；已创建任务和交付保持可领取。"
        if action == "purge"
        else "批次移入回收站，未开始及可重试卡密停止兑换；正在处理的任务和已有交付不受影响。",
    }


def _materialize_legacy(c, pid):
    now, bid = time.time(), str(uuid.uuid4())
    c.execute(
        "INSERT INTO card_batches(id,product_id,label,created,count,variant_id) "
        "SELECT ?,?,'历史卡密',MIN(c.created),COUNT(*),'default' FROM cards c "
        "LEFT JOIN card_meta m ON m.card_id=c.id WHERE c.product_id=? "
        "AND m.batch_id IS NULL AND m.batch_deleted_at IS NULL",
        (bid, pid, pid),
    )
    c.execute(
        "INSERT INTO card_meta(card_id,batch_id) SELECT c.id,? FROM cards c "
        "LEFT JOIN card_meta m ON m.card_id=c.id WHERE c.product_id=? AND m.card_id IS NULL",
        (bid, pid),
    )
    c.execute(
        "UPDATE card_meta SET batch_id=? WHERE batch_id IS NULL AND batch_deleted_at IS NULL "
        "AND card_id IN (SELECT id FROM cards WHERE product_id=?)",
        (bid, pid),
    )
    return bid, now


def delete_batch(c, s, pid, bid, body):
    current = preview(c, pid, bid, "delete")
    if body.revision != current["revision"]:
        fail("批次状态已变化，请重新查看删除预览", 409)
    if bid == "legacy":
        bid, _ = _materialize_legacy(c, pid)
    now = time.time()
    ids = "SELECT card_id FROM card_meta WHERE batch_id=?"
    c.execute(
        "UPDATE card_meta SET batch_deleted_at=?,batch_previous_state=CASE "
        "WHEN card_id IN (SELECT c.id FROM cards c LEFT JOIN jobs j ON j.card_id=c.id "
        "WHERE c.state='ready' AND (j.id IS NULL OR j.state IN ('failed','needs_input'))) "
        "THEN 'ready' ELSE NULL END WHERE batch_id=? "
        "AND card_id IN (SELECT id FROM cards WHERE product_id=?)",
        (now, bid, pid),
    )
    c.execute(
        "UPDATE cards SET state='revoked' WHERE id IN (" + ids + ") AND id IN "
        "(SELECT card_id FROM card_meta WHERE batch_previous_state='ready') AND product_id=?",
        (bid, pid),
    )
    c.execute(
        "UPDATE card_batches SET deleted_at=? WHERE id=? AND product_id=?",
        (now, bid, pid),
    )
    audit(c, management_actor(s), "cards.batch.delete", bid)
    return {
        "ok": True,
        "batch_id": bid,
        "deleted": True,
        "revoked": current["revocable_count"],
        "retained": current["retain_count"],
    }


def restore_batch(c, s, pid, bid):
    batch = _batch(c, pid, bid)
    if batch["deleted_at"] is None:
        fail("批次不在回收站", 409)
    # Restore only states changed by this archive; completed work and unrelated
    # manual revocation never become redeemable because a folder was restored.
    c.execute(
        "UPDATE cards SET state='ready' WHERE state='revoked' AND product_id=? AND id IN "
        "(SELECT card_id FROM card_meta WHERE batch_id=? AND batch_previous_state='ready')",
        (pid, bid),
    )
    c.execute(
        "UPDATE card_meta SET batch_deleted_at=NULL,batch_previous_state=NULL WHERE batch_id=? "
        "AND card_id IN (SELECT id FROM cards WHERE product_id=?)",
        (bid, pid),
    )
    c.execute(
        "UPDATE card_batches SET deleted_at=NULL WHERE id=? AND product_id=?",
        (bid, pid),
    )
    audit(c, management_actor(s), "cards.batch.restore", bid)
    return {"ok": True, "batch_id": bid, "deleted": False}


def purge_batch(c, s, pid, bid, body):
    current = preview(c, pid, bid, "purge")
    if body.revision != current["revision"]:
        fail("批次状态已变化，请重新查看清空预览", 409)
    audit(c, management_actor(s), "cards.batch.purge", bid)
    # All operations share BEGIN IMMEDIATE with preview verification. Only
    # no-job cards can be physically removed; FK cascades release stock/files.
    removable = "SELECT c.id FROM cards c JOIN card_meta m ON m.card_id=c.id "
    removable += "WHERE c.product_id=? AND m.batch_id=? AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.card_id=c.id)"
    for table in ("grants", "receipt_batch_cards"):
        c.execute(f"DELETE FROM {table} WHERE card_id IN ({removable})", (pid, bid))
    c.execute("DELETE FROM cards WHERE id IN (" + removable + ")", (pid, bid))
    c.execute("DELETE FROM card_batches WHERE id=? AND product_id=?", (bid, pid))
    return {
        "ok": True,
        "batch_id": bid,
        "purged": True,
        "deleted_cards": current["delete_count"],
        "retained_cards": current["retain_count"],
        "preserved_fulfillment": True,
    }


@router.get("/api/admin/card-batches")
@router.get("/api/manage/card-batches")
def batches(
    request: Request,
    product_id: str = Query(default="", max_length=100),
    view: BatchView = "active",
    variant_id: str = Query(default="", max_length=40),
    search: str = Query(default="", max_length=100),
    offset: int = Query(default=0, ge=0, le=1000000),
    limit: int = Query(default=50, ge=1, le=100),
):
    s = _session(request)
    with db() as c:
        return list_batches(
            c, _product_scope(c, s, product_id), view, variant_id, search, offset, limit
        )


@router.post("/api/admin/card-batches/{bid}/delete-preview")
@router.post("/api/manage/card-batches/{bid}/delete-preview")
def delete_preview(
    bid: str, request: Request, product_id: str = Query(default="", max_length=100)
):
    s = _session(request)
    with db() as c:
        return preview(c, _product_scope(c, s, product_id), bid, "delete")


@router.delete("/api/admin/card-batches/{bid}")
@router.delete("/api/manage/card-batches/{bid}")
def remove(
    bid: str,
    body: ConfirmBatch,
    request: Request,
    product_id: str = Query(default="", max_length=100),
):
    s = _session(request)
    with db() as c:
        return delete_batch(c, s, _product_scope(c, s, product_id), bid, body)


@router.post("/api/admin/card-batches/{bid}/restore")
@router.post("/api/manage/card-batches/{bid}/restore")
def restore(
    bid: str, request: Request, product_id: str = Query(default="", max_length=100)
):
    s = _session(request)
    with db() as c:
        return restore_batch(c, s, _product_scope(c, s, product_id), bid)


@router.post("/api/admin/card-batches/{bid}/purge-preview")
@router.post("/api/manage/card-batches/{bid}/purge-preview")
def purge_preview(
    bid: str, request: Request, product_id: str = Query(default="", max_length=100)
):
    s = _session(request)
    with db() as c:
        return preview(c, _product_scope(c, s, product_id), bid, "purge")


@router.post("/api/admin/card-batches/{bid}/purge")
@router.post("/api/manage/card-batches/{bid}/purge")
def purge(
    bid: str,
    body: ConfirmBatch,
    request: Request,
    product_id: str = Query(default="", max_length=100),
):
    s = _session(request)
    with db() as c:
        return purge_batch(c, s, _product_scope(c, s, product_id), bid, body)

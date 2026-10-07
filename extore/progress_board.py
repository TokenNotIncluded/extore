"""Progress metadata, with no task details, result access or snapshot decryption."""

import hashlib
import math
import time
from typing import Literal

from fastapi import APIRouter, Query, Request

from .db import db
from .security import authorize_management, fail, session

router = APIRouter(prefix="/api/manage", tags=["progress board"])
STATES = (
    "queued",
    "processing",
    "waiting",
    "failed",
    "needs_input",
    "succeeded",
    "rejected",
    "destroyed",
)
ACTIVE = STATES[:5]
PROCESSED = STATES[5:]
PHASES = {"await_start", "input", "display", "queued", "processing", "ended"}
MODES = {"manual", "script", "webhook", "stock"}
MAX_PRODUCTS = 500
MAX_WORKERS = 500
IDENTITY_JOIN = (
    " LEFT JOIN job_worker_identities wi ON wi.job_id=j.id AND wi.attempt=j.attempt "
    "AND wi.actor=j.claimed_by "
)
WORKER_KEY = "CASE WHEN wi.channel='cli' AND wi.device_ref IS NOT NULL THEN COALESCE(wi.worker_ref,'cli:'||wi.device_ref) ELSE j.claimed_by END"


def _counts():
    return dict.fromkeys(STATES, 0)


def _timestamp(value, *, nullable=False):
    if type(value) in (int, float) and math.isfinite(value) and value > 0:
        return value
    if nullable:
        return None
    fail("任务时间信息无效，无法显示进度", 409)


def _name(value, fallback):
    if not isinstance(value, str):
        return fallback
    return "".join(char for char in value if char.isprintable())[:120] or fallback


def _worker_id(shop_id, actor):
    return hashlib.sha256(
        f"extore-progress-board-v1\n{shop_id}\n{actor}".encode()
    ).hexdigest()


def _scope(c, s, shop_id, product_id):
    authorize_management(c, s)
    own = s.get("shop_id")
    if s["role"] == "staff":
        if not {"queue.monitor", "queue.view"}.intersection(s["permissions"]):
            fail("此商品管理链接没有查看进度看板的权限", 403)
        if product_id and product_id != s["product_id"]:
            fail("没有此商品的进度查看权限", 403)
        product_id = s["product_id"]
    if own is not None:
        if shop_id and shop_id != own:
            fail("没有此店铺的管理权限", 403)
        shop_id = own
    elif not shop_id:
        fail("请先选择要查看进度的店铺", 400)
    shop = c.execute(
        "SELECT id,name,enabled FROM shops WHERE id=?", (shop_id,)
    ).fetchone()
    if shop is None:
        fail("店铺不存在", 404)
    if not shop["enabled"]:
        fail("店铺不可用", 401)
    if product_id:
        target = c.execute(
            "SELECT id,shop_id FROM products WHERE id=?", (product_id,)
        ).fetchone()
        if target is None:
            fail("商品不存在", 404)
        if target["shop_id"] != shop_id:
            fail("商品不属于所选店铺", 403)
    return {"id": shop_id, "name": _name(shop["name"], "店铺")}, product_id


def _steps(c, row):
    if row["flow_phase"] is not None:
        steps = c.execute(
            "SELECT flow_epoch,state FROM task_flow_steps WHERE job_id=? AND attempt=? "
            "ORDER BY flow_epoch LIMIT 257",
            (row["id"], row["attempt"]),
        ).fetchall()
        if len(steps) > 256:
            fail("流程步骤数量超出看板安全范围", 409)
        return [
            {
                "position": i + 1,
                "state": "done"
                if step["state"] == "completed"
                else (
                    "current"
                    if step["state"] == "active"
                    and step["flow_epoch"] == row["flow_epoch"]
                    and row["flow_phase"] in PHASES - {"ended"}
                    else "pending"
                ),
            }
            for i, step in enumerate(steps)
        ]
    # SQLite projects only ordinal positions and matching completion booleans.
    # No labels, customer text, custom IDs or the plan JSON leave this query.
    steps = c.execute(
        "SELECT CAST(p.key AS INTEGER)+1 AS position, EXISTS("
        "SELECT 1 FROM json_each(CASE WHEN json_valid(j.completed_steps) "
        "THEN j.completed_steps ELSE '[]' END) d WHERE d.type='text' "
        "AND d.value=CASE WHEN p.type='object' THEN json_extract(p.value,'$.id') END) AS done "
        "FROM jobs j,json_each(CASE WHEN json_valid(j.progress_plan) THEN CASE "
        "WHEN json_type(j.progress_plan)='array' THEN j.progress_plan ELSE '[]' END ELSE '[]' END) p "
        "WHERE j.id=? ORDER BY CAST(p.key AS INTEGER) LIMIT 31",
        (row["id"],),
    ).fetchall()
    if len(steps) > 30:
        fail("处理步骤数量超出看板安全范围", 409)
    current = next((step["position"] for step in steps if not step["done"]), None)
    return [
        {
            "position": step["position"],
            "state": "done"
            if step["done"]
            else (
                "current"
                if row["state"] == "processing" and step["position"] == current
                else "pending"
            ),
        }
        for step in steps
    ]


@router.get("/progress-board")
def progress_board(
    request: Request,
    shop_id: str = Query(default="", max_length=100),
    product_id: str = Query(default="", max_length=100),
    view: Literal["active", "processed"] = "active",
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=1000000),
):
    s = session(request, ("admin", "staff"))
    generated = time.time()
    with db() as c:
        shop, pid = _scope(c, s, shop_id, product_id)
        sid = shop["id"]
        clause = "p.shop_id=? AND (?='' OR p.id=?)"
        args = (sid, pid, pid)
        products = c.execute(
            "SELECT p.id, CASE WHEN json_valid(p.config) THEN CASE "
            "WHEN json_type(p.config,'$.name')='text' THEN json_extract(p.config,'$.name') END END name, "
            "CASE WHEN json_valid(p.config) THEN CASE WHEN json_type(p.config,'$.mode')='text' "
            "THEN json_extract(p.config,'$.mode') END END mode "
            "FROM products p WHERE " + clause + " ORDER BY p.created,p.id LIMIT 501",
            args,
        ).fetchall()
        if len(products) > MAX_PRODUCTS:
            fail("商品数量超过看板范围，请按商品查看", 409)
        summaries = {
            r["id"]: {
                "id": r["id"],
                "name": _name(r["name"], "商品"),
                "mode": r["mode"] if r["mode"] in MODES else "unknown",
                "counts": _counts(),
                "jobs": [],
            }
            for r in products
        }
        totals = _counts()
        for count in c.execute(
            "SELECT j.product_id,j.state,COUNT(*) n FROM jobs j JOIN products p ON p.id=j.product_id "
            "WHERE " + clause + " GROUP BY j.product_id,j.state",
            args,
        ):
            if count["state"] in totals:
                totals[count["state"]] += count["n"]
                summaries[count["product_id"]]["counts"][count["state"]] += count["n"]
        selected_states = ACTIVE if view == "active" else PROCESSED
        total = sum(totals[state] for state in selected_states)
        state_slots = ",".join("?" for _ in selected_states)
        order = (
            "j.updated DESC,j.id"
            if view == "processed"
            else (
                "CASE j.state WHEN 'processing' THEN 0 WHEN 'waiting' THEN 1 WHEN 'needs_input' THEN 2 "
                "WHEN 'queued' THEN 3 ELSE 4 END,j.created,j.id"
            )
        )
        rows = c.execute(
            "SELECT j.id,j.product_id,j.state,j.progress,j.attempt,j.created,j.updated,j.claimed_by,"
            + WORKER_KEY
            + " worker_key,"
            "t.phase flow_phase,t.flow_epoch,CASE WHEN j.state='queued' THEN (SELECT COUNT(*) "
            "FROM jobs q WHERE q.product_id=j.product_id AND q.state='queued' AND "
            "(q.created<j.created OR (q.created=j.created AND q.id<=j.id))) END queue_position "
            "FROM jobs j JOIN products p ON p.id=j.product_id LEFT JOIN task_flow_runs t "
            "ON t.job_id=j.id AND t.attempt=j.attempt "
            + IDENTITY_JOIN
            + " WHERE "
            + clause
            + " AND j.state IN ("
            + state_slots
            + ") "
            "ORDER BY " + order + " LIMIT ? OFFSET ?",
            (*args, *selected_states, limit, offset),
        ).fetchall()
        keys = {r["worker_key"] for r in rows if r["worker_key"]}
        keys.update(
            r["worker_key"]
            for r in c.execute(
                "SELECT DISTINCT "
                + WORKER_KEY
                + " worker_key FROM jobs j JOIN products p "
                "ON p.id=j.product_id "
                + IDENTITY_JOIN
                + " WHERE "
                + clause
                + " AND j.state='processing' AND j.claimed_by IS NOT NULL AND j.claimed_by<>'' LIMIT 501",
                args,
            )
        )
        if len(keys) > MAX_WORKERS:
            fail("处理人员数量超过看板范围，请按商品查看", 409)
        workers = {}
        if keys:
            slots = ",".join("?" for _ in keys)
            names = {
                r["id"]: r["name"]
                for r in c.execute(
                    "SELECT s.id,s.name FROM staff s JOIN products p ON p.id=s.product_id WHERE "
                    + clause
                    + " AND s.id IN ("
                    + slots
                    + ")",
                    (*args, *sorted(keys)),
                )
            }
            identity_times = {}
            for aggregate in c.execute(
                "SELECT "
                + WORKER_KEY
                + " worker_key,j.claimed_by,j.state,wi.client_name,wi.agent_type,"
                "wi.channel,MAX(wi.claimed_at) identity_time,COUNT(*) n,MAX(j.updated) last_update "
                "FROM jobs j JOIN products p ON p.id=j.product_id "
                + IDENTITY_JOIN
                + " WHERE "
                + clause
                + " AND ("
                + WORKER_KEY
                + ") IN ("
                + slots
                + ") "
                "GROUP BY worker_key,j.claimed_by,j.state,wi.client_name,wi.agent_type,wi.channel",
                (*args, *sorted(keys)),
            ):
                key, actor = aggregate["worker_key"], aggregate["claimed_by"]
                name, kind, agent_type = (
                    ("商品处理器", "automatic", "processor")
                    if actor == "worker"
                    else (
                        ("店主", "merchant", "human")
                        if actor in {"owner", "shop:" + sid}
                        else (_name(names.get(actor), "处理人员"), "unknown", None)
                    )
                )
                if key not in workers:
                    workers[key] = {
                        "id": _worker_id(sid, key),
                        "name": name,
                        "kind": kind,
                        "agent_type": agent_type,
                        "active_jobs": 0,
                        "completed_jobs": 0,
                        "last_update": None,
                    }
                worker = workers[key]
                identity_time = _timestamp(aggregate["identity_time"], nullable=True)
                if identity_time is not None and identity_time >= identity_times.get(
                    key, 0
                ):
                    identity_times[key] = identity_time
                    worker.update(
                        name=_name(aggregate["client_name"], name),
                        kind={
                            "cli": "cli",
                            "browser": "human",
                            "automatic": "automatic",
                        }.get(aggregate["channel"], "unknown"),
                        agent_type=aggregate["agent_type"],
                    )
                if aggregate["state"] == "processing":
                    worker["active_jobs"] += aggregate["n"]
                if aggregate["state"] in PROCESSED:
                    worker["completed_jobs"] += aggregate["n"]
                updated = _timestamp(aggregate["last_update"], nullable=True)
                if updated is not None:
                    worker["last_update"] = max(
                        worker["last_update"] or updated, updated
                    )
        for row in rows:
            steps = _steps(c, row)
            actor = row["worker_key"]
            done = sum(step["state"] == "done" for step in steps)
            progress = row["progress"] if type(row["progress"]) is int else 0
            if row["flow_phase"] is not None or not steps:
                if type(row["progress"]) is not int or not 0 <= progress <= 100:
                    fail("任务进度信息无效，无法显示进度", 409)
            if row["flow_phase"] is None and steps:
                progress = (
                    100
                    if row["state"] in {"succeeded", "destroyed"}
                    else min(99, done * 100 // len(steps))
                )
            attempt = row["attempt"]
            if type(attempt) is not int or attempt < 1:
                fail("任务尝试次数无效，无法显示进度", 409)
            summaries[row["product_id"]]["jobs"].append(
                {
                    "id": row["id"],
                    "state": row["state"],
                    "progress": progress,
                    "attempt": attempt,
                    "created": _timestamp(row["created"]),
                    "updated": _timestamp(row["updated"]),
                    "queue_position": row["queue_position"],
                    "worker_id": workers[actor]["id"] if actor else None,
                    "step_count": len(steps),
                    "completed_step_count": done,
                    "steps": steps,
                    "flow_phase": row["flow_phase"]
                    if row["flow_phase"] in PHASES
                    else None,
                }
            )
        return {
            "schema": "extore.progress-board.v1",
            "generated_at": generated,
            "shop": shop,
            "totals": totals,
            "products": list(summaries.values()),
            "workers": list(workers.values()),
            "pagination": {
                "limit": limit,
                "offset": offset,
                "total": total,
                "has_more": offset + len(rows) < total,
            },
            "scope": {"product_ids": list(summaries)},
        }

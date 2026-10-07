"""Bounded, signed queue waiting and atomic claims for approved CLI devices."""

import asyncio
import hashlib
import importlib
import json
import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .cli_auth import _device, _verify
from .config import ORIGIN
from .db import audit, db
from .models import JobUpdate
from .security import authorize_management, fail, rate_limit, require_cli_bearer
from .service import apply_update, job, job_view, product
from .shops import require_enabled_product

router = APIRouter(prefix="/api/manage")
POLL_INTERVAL = 0.5
RECEIPT_TTL = 600
MAX_REQUESTS = 10000
MAX_DEVICE_REQUESTS = 100
MAX_PENDING = 3


def init_schema(c):
    # Receipts contain execution identities, never customer inputs or credentials.
    c.execute(
        "CREATE TABLE IF NOT EXISTS automation_requests ("
        "id TEXT PRIMARY KEY,device_id TEXT NOT NULL,body_hash TEXT NOT NULL,"
        "state TEXT NOT NULL CHECK(state IN ('pending','done')),"
        "created REAL NOT NULL,deadline REAL NOT NULL,expires REAL NOT NULL,"
        "items TEXT NOT NULL DEFAULT '[]')"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS automation_requests_expiry "
        "ON automation_requests(expires)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS automation_requests_device "
        "ON automation_requests(device_id,expires)"
    )
    cleanup(c)


def cleanup(c, *, now=None, limit=200):
    """Bounded maintenance hook; receipts do not become permanent history."""
    now = time.time() if now is None else now
    return c.execute(
        "DELETE FROM automation_requests WHERE id IN "
        "(SELECT id FROM automation_requests WHERE expires<=? LIMIT ?)",
        (now, max(1, min(limit, 1000))),
    ).rowcount


class NextGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    device_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    product_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    signature: str = Field(min_length=86, max_length=86)


class NextInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=16, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    issued_at: int
    wait_seconds: int = Field(default=25, ge=0, le=25)
    limit: int = Field(default=1, ge=1, le=10)
    grants: list[NextGrant] = Field(min_length=1, max_length=500)


def next_canonical(value):
    """Public wire format shared by the server and CLI; excludes signatures."""
    if isinstance(value, BaseModel):
        value = value.model_dump()
    payload = {
        name: value[name]
        for name in ("request_id", "issued_at", "wait_seconds", "limit")
    }
    payload["grants"] = sorted(
        [
            {name: item[name] for name in ("device_id", "product_id")}
            for item in value["grants"]
        ],
        key=lambda item: (item["device_id"], item["product_id"]),
    )
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def next_proof(origin, value, device_id):
    return (
        "extore-automation-next-v1\n"
        + origin
        + "\nPOST\n/api/manage/next\n"
        + device_id
        + "\n"
        + next_canonical(value)
    )


def _authorization(c, s, body):
    authorize_management(c, s, "queue.process")
    if s["channel"] != "cli" or s["role"] != "staff":
        fail("请使用商品处理 CLI 设备授权", 403)
    devices = [item.device_id for item in body.grants]
    products = [item.product_id for item in body.grants]
    if len(set(devices)) != len(devices) or len(set(products)) != len(products):
        fail("领取范围不能重复", 422)
    if s["device_id"] not in devices:
        fail("登录设备不在本次领取范围内", 403)
    bindings = {}
    for item in body.grants:
        device, staff = _device(c, item.device_id)
        _verify(
            device["public_key"],
            item.signature,
            next_proof(ORIGIN, body, item.device_id),
        )
        if staff["product_id"] != item.product_id:
            fail("设备授权不属于此商品", 403)
        if not {"queue.view", "queue.process"}.issubset(staff["permissions"]):
            fail("设备缺少查看和处理队列权限", 403)
        require_enabled_product(c, item.product_id)
        p = product(c, item.product_id)
        if p["mode"] != "manual":
            fail("此商品不是可领取的处理队列", 409)
        bindings[item.product_id] = {
            "device_id": item.device_id,
            "actor": staff["id"],
        }
    return bindings


def _flow_execution(c, row):
    # The optional flow module is supplied by the orchestration integration.
    try:
        task_flow = importlib.import_module("extore.task_flow")
    except ModuleNotFoundError as exc:
        if exc.name != "extore.task_flow":
            raise
        return None
    return task_flow.execution(c, row)


def _is_flow(c, row):
    try:
        task_flow = importlib.import_module("extore.task_flow")
    except ModuleNotFoundError as exc:
        if exc.name != "extore.task_flow":
            raise
        return False
    return task_flow.is_flow(c, row)


def _work_item(c, row, binding):
    flow = _is_flow(c, row)
    if flow:
        from . import task_flow

        if task_flow.view(c, row, staff=True).get("phase") != "processing":
            return None
    execution = _flow_execution(c, row)
    if execution is None and flow:
        return None
    details = job_view(c, row, execution is None)
    if execution is None:
        execution = {
            name: details[name] for name in ("params", "parameters", "outputs")
        }
        execution.update(flow_epoch=None, action_id=None)
    else:
        execution = {
            name: execution[name]
            for name in (
                "params",
                "parameters",
                "outputs",
                "flow_epoch",
                "node_id",
                "action_id",
                "deadline",
                "attempt",
                "mode",
            )
            if name in execution
        }
    return {
        "product_id": row["product_id"],
        "device_id": binding["device_id"],
        "job": {
            name: details[name]
            for name in (
                "id",
                "attempt",
                "state",
                "product_name",
                "variant",
                "message",
                "progress",
                "steps",
                "completed_steps",
            )
            if name in details
        },
        "execution": execution,
    }


def _claim(c, row, binding):
    execution = _flow_execution(c, row)
    if execution is None and _is_flow(c, row):
        return None
    if execution is not None:
        from . import task_flow

        if task_flow.view(c, row, staff=True).get("phase") != "queued":
            return None
        if execution["deadline"] <= time.time():
            return None
        effect = task_flow.claim(c, row, binding["actor"], execution["flow_epoch"])
        from .service import finalize_task_flow

        finalize_task_flow(c, effect)
        if effect.get("expired"):
            return None
    else:
        changed = c.execute(
            "UPDATE jobs SET claimed_by=? WHERE id=? AND state='queued' "
            "AND attempt=? AND claimed_by IS NULL",
            (binding["actor"], row["id"], row["attempt"]),
        ).rowcount
        if changed != 1:
            return None
        apply_update(
            c,
            row["id"],
            JobUpdate(state="processing", attempt=row["attempt"], message="正在处理"),
        )
    current = job(c, row["id"])
    if current["state"] != "processing" or current["claimed_by"] != binding["actor"]:
        return None
    from .agent_identity import record_claim

    record_claim(c, current, binding["actor"], device_id=binding["device_id"])
    item = _work_item(c, current, binding)
    if item is None:
        return None
    audit(c, binding["actor"], "queue.next.claim", current["id"])
    return item


def _identity(item):
    return {
        "job_id": item["job"]["id"],
        "product_id": item["product_id"],
        "device_id": item["device_id"],
        "attempt": item["job"]["attempt"],
        "flow_epoch": item["execution"].get("flow_epoch"),
        "action_id": item["execution"].get("action_id"),
    }


def _receipt(c, record, body, bindings):
    items = []
    stale = False
    for identity in json.loads(record["items"]):
        binding = bindings.get(identity["product_id"])
        row = c.execute(
            "SELECT * FROM jobs WHERE id=?", (identity["job_id"],)
        ).fetchone()
        if (
            binding is None
            or binding["device_id"] != identity["device_id"]
            or row is None
            or row["product_id"] != identity["product_id"]
            or row["attempt"] != identity["attempt"]
            or row["state"] != "processing"
            or row["claimed_by"] != binding["actor"]
            or row["lease"] is None
            or row["lease"] <= time.time()
        ):
            stale = True
            continue
        item = _work_item(c, row, binding)
        if (
            item is None
            or _identity(item) != identity
            or (
                item["execution"].get("deadline") is not None
                and item["execution"]["deadline"] <= time.time()
            )
        ):
            stale = True
            continue
        items.append(item)
    return {
        "items": items,
        "idle": not items,
        "request_id": body.request_id,
        "replayed": True,
        "stale": stale,
    }


def _tick(s, body, *, force_idle=False):
    """No transaction survives this function, including when the queue is empty."""
    with db() as c:
        bindings = _authorization(c, s, body)
        now = time.time()
        # One fully signed request keeps one receipt even when the client
        # refreshes a different listed device's Bearer session for recovery.
        device_scope = ",".join(sorted(item.device_id for item in body.grants))
        key = hashlib.sha256(
            (device_scope + ":" + body.request_id).encode()
        ).hexdigest()
        body_hash = hashlib.sha256(next_canonical(body).encode()).hexdigest()
        record = c.execute(
            "SELECT * FROM automation_requests WHERE id=?", (key,)
        ).fetchone()
        existing_request = record is not None
        if record is not None and record["expires"] <= now:
            fail("领取请求已过期，请使用新请求", 410)
        if record is not None and record["body_hash"] != body_hash:
            fail("相同领取请求不能修改范围或参数", 409)
        if record is None:
            if not now - 120 <= body.issued_at <= now + 30:
                fail("领取签名已过期", 401)
            cleanup(c, now=now)
            total = c.execute("SELECT count(*) FROM automation_requests").fetchone()[0]
            own = c.execute(
                "SELECT count(*),sum(state='pending' AND deadline>?) "
                "FROM automation_requests WHERE device_id=? AND expires>?",
                (now, s["device_id"], now),
            ).fetchone()
            if (
                total >= MAX_REQUESTS
                or own[0] >= MAX_DEVICE_REQUESTS
                or (own[1] or 0) >= MAX_PENDING
            ):
                fail("领取请求过多，请稍后重试", 429)
            c.execute(
                "INSERT INTO automation_requests VALUES (?,?,?,'pending',?,?,?,'[]')",
                (
                    key,
                    s["device_id"],
                    body_hash,
                    now,
                    now + body.wait_seconds,
                    now + RECEIPT_TTL,
                ),
            )
            record = c.execute(
                "SELECT * FROM automation_requests WHERE id=?", (key,)
            ).fetchone()
        if record["state"] == "done":
            return _receipt(c, record, body, bindings)
        if existing_request and now >= record["deadline"]:
            c.execute("UPDATE automation_requests SET state='done' WHERE id=?", (key,))
            return {
                "items": [],
                "idle": True,
                "request_id": body.request_id,
                "replayed": True,
                "stale": False,
            }
        # FIFO across this explicitly signed set; future products are never added.
        placeholders = ",".join("?" for _ in bindings)
        rows = c.execute(
            "SELECT * FROM jobs WHERE state='queued' AND claimed_by IS NULL "
            f"AND product_id IN ({placeholders}) ORDER BY created,id LIMIT ?",
            (*bindings, 500),
        ).fetchall()
        items = []
        for row in rows:
            item = _claim(c, row, bindings[row["product_id"]])
            if item is not None:
                items.append(item)
                if len(items) == body.limit:
                    break
        if items or now >= record["deadline"] or force_idle:
            c.execute(
                "UPDATE automation_requests SET state='done',items=? WHERE id=?",
                (json.dumps([_identity(item) for item in items]), key),
            )
            return {
                "items": items,
                "idle": not items,
                "request_id": body.request_id,
                "replayed": False,
                "stale": False,
            }
        return max(0, record["deadline"] - time.time())


@router.post("/next")
async def next_task(request: Request):
    s = require_cli_bearer(request)
    rate_limit(request, "automation-next", 120, 60)
    try:
        body = NextInput.model_validate(await request.json())
    except (ValueError, TypeError, ValidationError):
        fail("领取参数无效", 422)
    stop_at = time.monotonic() + body.wait_seconds
    while True:
        remaining = max(0, stop_at - time.monotonic())
        result = _tick(s, body, force_idle=remaining == 0)
        if isinstance(result, dict):
            return result
        if await request.is_disconnected():
            return {"items": [], "idle": True, "request_id": body.request_id}
        await asyncio.sleep(min(POLL_INTERVAL, result, remaining))

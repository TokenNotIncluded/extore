import json
import re
import time
import uuid

from .db import event
from .models import JobUpdate
from .security import card_digest, fail, new_card


def product(c, pid):
    row = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not row:
        fail("商品不存在", 404)
    return {"id": row["id"], **json.loads(row["config"])}


def public_product(p):
    return {
        k: v
        for k, v in p.items()
        if k not in ("webhook_url", "webhook_secret", "script")
    }


def issue_cards(c, pid, count):
    product(c, pid)
    codes = []
    for _ in range(count):
        code = new_card()
        c.execute(
            "INSERT INTO cards(id,digest,product_id,created) VALUES (?,?,?,?)",
            (str(uuid.uuid4()), card_digest(code), pid, time.time()),
        )
        codes.append(code)
    return codes


def validate_params(p, params):
    if set(params) - {v["key"] for v in p["parameters"]}:
        fail("提交了未定义的参数")
    clean = {}
    for f in p["parameters"]:
        v = params.get(f["key"], "").strip()
        if f["required"] and not v:
            fail(f"请填写 {next(iter(f['label'].values()))}")
        if len(v) > 10000:
            fail("参数内容过长")
        if (
            v
            and f["type"] == "email"
            and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", v)
        ):
            fail("邮箱格式不正确")
        if v and f["type"] == "number":
            import decimal

            try:
                if not decimal.Decimal(v).is_finite():
                    fail("请输入有限数字")
            except decimal.InvalidOperation:
                fail("请输入数字")
        clean[f["key"]] = v
    return clean


def job(c, jid):
    row = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        fail("任务不存在", 404)
    return row


def job_view(c, row, staff=False):
    p = product(c, row["product_id"])
    result = {
        k: row[k]
        for k in (
            "id",
            "product_id",
            "state",
            "message",
            "progress",
            "attempt",
            "created",
            "updated",
            "revealed",
        )
    }
    result["product_name"] = p["name"]
    result["delivery"] = p["delivery"]
    result["view_policy"] = p["view_policy"]
    result["can_retry"] = bool(
        row["state"] == "failed"
        and row["retryable"]
        and p["allow_retry"]
        and row["attempt"] < p["max_attempts"]
    )
    result["queue_ahead"] = (
        c.execute(
            "SELECT count(*) FROM jobs WHERE product_id=? AND state='queued' AND created<?",
            (row["product_id"], row["created"]),
        ).fetchone()[0]
        if row["state"] == "queued"
        else 0
    )
    if staff:
        result["params"] = json.loads(row["params"])
        result["claimed_by"] = row["claimed_by"]
        result["mode"] = p["mode"]
    return result


def submit(c, card, params):
    p = product(c, card["product_id"])
    clean = validate_params(p, params)
    row = c.execute("SELECT * FROM jobs WHERE card_id=?", (card["id"],)).fetchone()
    if row:
        if row["state"] != "failed":
            return row
        if (
            not row["retryable"]
            or not p["allow_retry"]
            or row["attempt"] >= p["max_attempts"]
        ):
            fail("此任务不能自动重试，请联系商家", 409)
        c.execute(
            "UPDATE jobs SET state='queued',params=?,content=NULL,message='',progress=0,attempt=attempt+1,retryable=0,claimed_by=NULL,lease=NULL,updated=? WHERE id=?",
            (json.dumps(clean), time.time(), row["id"]),
        )
        c.execute("UPDATE cards SET state='reserved' WHERE id=?", (card["id"],))
        jid = row["id"]
    else:
        if card["state"] != "ready":
            fail("此卡密无法兑换", 409)
        jid = str(uuid.uuid4())
        now = time.time()
        c.execute(
            "INSERT INTO jobs(id,card_id,product_id,state,params,created,updated) VALUES (?,?,?,'queued',?,?,?)",
            (jid, card["id"], p["id"], json.dumps(clean), now, now),
        )
        c.execute("UPDATE cards SET state='reserved' WHERE id=?", (card["id"],))
    row = job(c, jid)
    event(c, "redemption.requested", p["id"], row)
    return row


def apply_update(c, jid, update: JobUpdate):
    row = job(c, jid)
    if row["attempt"] != update.attempt:
        fail("回调对应的尝试已失效", 409)
    if row["state"] == update.state and row["state"] in ("succeeded", "failed"):
        return row  # idempotent completion, never overwrite a result
    if row["state"] not in ("queued", "processing"):
        fail("任务已经结束，不能覆盖结果", 409)
    p = product(c, row["product_id"])
    if (
        update.state == "succeeded"
        and p["delivery"] == "content"
        and not update.content
    ):
        fail("内容型商品需要交付内容")
    if update.state == "processing" and update.progress < row["progress"]:
        fail("进度不能倒退", 409)
    content = (
        update.content
        if update.state == "succeeded" and p["delivery"] == "content"
        else None
    )
    progress = 100 if update.state == "succeeded" else update.progress
    c.execute(
        "UPDATE jobs SET state=?,progress=?,message=?,content=?,retryable=?,updated=?,lease=? WHERE id=?",
        (
            update.state,
            progress,
            update.message,
            content,
            int(update.retryable and update.state == "failed"),
            time.time(),
            time.time() + 3600 if update.state == "processing" else None,
            jid,
        ),
    )
    if update.state == "succeeded":
        c.execute("UPDATE cards SET state='used' WHERE id=?", (row["card_id"],))
    row = job(c, jid)
    event(
        c,
        {
            "processing": "fulfillment.progress",
            "succeeded": "fulfillment.succeeded",
            "failed": "fulfillment.failed",
        }[update.state],
        row["product_id"],
        row,
    )
    return row

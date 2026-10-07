"""Queue retry and rejection outcomes without consuming redemption codes."""

import time

from .card_tracking import ensure_card_usable
from .db import event
from .files import purge_job_outputs
from .security import fail
from .service import job, job_product, progress_snapshot


def apply_queue_outcome(
    c, jid, action, message, actor, *, retry_mode="revise", reason_type="customer_input"
):
    """The caller authorizes queue.process and product scope in its transaction."""
    states = {
        "request_changes": "needs_input",
        "request_retry": "needs_input",
        "reject": "rejected",
    }
    if action not in states:
        fail("未定义的队列审核操作")
    if retry_mode not in ("revise", "reuse") or reason_type not in (
        "customer_input",
        "external",
        "processor",
    ):
        fail("未定义的重试方式或原因类别")
    reason = message.strip()
    if not reason or len(reason) > 1000:
        fail("请填写需要重试或拒绝处理的原因，最多一千字符")
    row = job(c, jid)
    p = job_product(c, row)
    if p["mode"] != "manual":
        fail("只能审核队列商品的任务", 409)
    if row["state"] != "processing" or row["claimed_by"] != actor:
        fail("请先领取任务，且只能审核自己领取的任务", 409)
    card = c.execute("SELECT * FROM cards WHERE id=?", (row["card_id"],)).fetchone()
    ensure_card_usable(c, card)
    progress_snapshot(c, row)
    purge_job_outputs(c, jid)
    state = states[action]
    c.execute(
        "UPDATE jobs SET state=?,message=?,content=NULL,result_json=NULL,retryable=0,"
        "claimed_by=NULL,lease=NULL,retry_mode=?,retry_reason_type=?,updated=? WHERE id=?",
        (state, reason, retry_mode, reason_type, time.time(), jid),
    )
    c.execute(
        "UPDATE cards SET state=? WHERE id=?",
        (
            ("used" if row["revision_round"] else "ready")
            if state == "needs_input"
            else "rejected",
            row["card_id"],
        ),
    )
    updated = job(c, jid)
    event(c, "fulfillment." + state, row["product_id"], updated)
    return updated

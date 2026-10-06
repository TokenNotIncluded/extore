"""HTTP and fulfillment boundary for optional, frozen task flows.

The graph engine only changes its own execution state. This boundary validates
attachments and finishes the ordinary redemption in the same transaction.
"""

import json
import time
import uuid

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from .db import audit, db, event
from .models import JobUpdate
from .security import fail, rate_limit, resolve_customer_card

router = APIRouter()


class FlowAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    token: str = Field(max_length=100)
    card_id: str | None = Field(default=None, max_length=80)
    flow_epoch: int = Field(ge=0)
    expected_revision: int = Field(ge=0)
    values: dict[str, str] = Field(default_factory=dict)


def init_schema(c):
    c.execute(
        "CREATE TABLE IF NOT EXISTS task_flow_files ("
        "file_id TEXT PRIMARY KEY REFERENCES job_files(id) ON DELETE CASCADE,"
        "job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,"
        "node_id TEXT NOT NULL,flow_epoch INTEGER NOT NULL,"
        "attempt INTEGER NOT NULL,kind TEXT NOT NULL)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS task_flow_files_scope "
        "ON task_flow_files(job_id,attempt,flow_epoch,kind)"
    )


def preview(p):
    """A credential-free entry projection; never serialize the whole graph."""
    definition = p.get("task_flow")
    if not definition:
        return None
    entry = next(n for n in definition["nodes"] if n["id"] == definition["entry"])
    current = {
        key: entry[key]
        for key in ("id", "kind", "label", "prompt", "start_policy", "content")
        if key in entry
    }
    return {
        "enabled": True,
        "flow_epoch": 0,
        "revision": 0,
        "phase": "await_start",
        "current": current,
        "deadline": None,
        "server_time": time.time(),
        "shown": [],
        "history": [],
        "actions": ["start"],
    }


def submit_flow(c, card, params):
    from . import task_flow
    from .card_tracking import ensure_card_usable
    from .service import job
    from .shops import require_enabled_product

    require_enabled_product(c, card["product_id"])
    ensure_card_usable(c, card)
    snapshot = task_flow.card_snapshot(c, card["id"])
    if snapshot is None:
        return None
    if params:
        fail("请在当前流程步骤提交信息", 409)
    row = c.execute("SELECT * FROM jobs WHERE card_id=?", (card["id"],)).fetchone()
    if row:
        if row["state"] == "rejected":
            fail("此任务已被拒绝，不能重新提交", 409)
        if row["state"] in ("failed", "needs_input"):
            from .service import job_view

            if not job_view(c, row)["can_retry"]:
                fail("此任务不能重试，请联系商家", 409)
            from .files import purge_job_files

            purge_job_files(c, row["id"])
            c.execute(
                "UPDATE jobs SET attempt=attempt+1,state='waiting',params='{}',"
                "content=NULL,result_json=NULL,claimed_by=NULL,lease=NULL,"
                "progress=0,completed_steps='[]',retryable=0,updated=? WHERE id=?",
                (time.time(), row["id"]),
            )
            c.execute("UPDATE cards SET state='reserved' WHERE id=?", (card["id"],))
            effect = task_flow.reset_attempt(c, job(c, row["id"]))
            return finalize(c, effect)
        return row
    if card["state"] != "ready":
        fail("此卡密无法兑换", 409)
    jid, now = str(uuid.uuid4()), time.time()
    frozen = snapshot.get("product", snapshot)
    c.execute(
        "INSERT INTO jobs(id,card_id,product_id,state,params,created,updated,progress_plan,schema_snapshot) "
        "VALUES (?,?,?,'waiting','{}',?,?,?,?)",
        (
            jid,
            card["id"],
            card["product_id"],
            now,
            now,
            json.dumps(frozen.get("progress_steps", []), ensure_ascii=False),
            json.dumps(
                {key: frozen[key] for key in ("parameters", "outputs")},
                ensure_ascii=False,
            ),
        ),
    )
    c.execute("UPDATE cards SET state='reserved' WHERE id=?", (card["id"],))
    return finalize(c, task_flow.initialize(c, job(c, jid)))


def finalize(c, effect):
    """Validate accepted step data, then finish at the one true end node."""
    from .service import _apply_simple_update, job

    previous = effect["row"]
    row = job(c, previous["id"])
    expected = effect.get("flow")
    run = c.execute(
        "SELECT * FROM task_flow_runs WHERE job_id=?", (row["id"],)
    ).fetchone()
    if (
        run is None
        or not isinstance(expected, dict)
        or previous["card_id"] != row["card_id"]
        or previous["product_id"] != row["product_id"]
        or previous["attempt"] != row["attempt"]
        or any(
            type(expected.get(key)) is not int or expected[key] != run[key]
            for key in ("attempt", "flow_epoch", "revision")
        )
        or run["attempt"] != row["attempt"]
        or expected.get("phase") != run["phase"]
        or expected.get("current", {}).get("id") != run["node_id"]
    ):
        fail("任务流程结果对应的状态已改变", 409)
    terminal = effect.get("terminal")
    if terminal:
        if run["phase"] != "ended":
            fail("只有结束的任务流程可以完成交付", 409)
        if row["state"] in ("succeeded", "failed", "rejected", "destroyed"):
            if row["state"] == terminal["state"]:
                return row  # Do not bind files, rewrite content or emit another event.
            fail("任务已经结束，不能覆盖结果", 409)
        if row["state"] not in ("waiting", "queued", "processing"):
            fail("当前任务状态不能完成流程交付", 409)
    accepted = effect.get("accepted")
    if accepted:
        validate_stage_files(c, row, accepted)
    if terminal:
        message = terminal.get("message", "")
        if isinstance(message, dict):
            message = message.get("zh-CN") or next(iter(message.values()), "")
        from . import task_flow

        frozen = task_flow.card_snapshot(c, row["card_id"])
        final_product = frozen.get("product", frozen)
        if terminal["state"] == "rejected":
            from .files import purge_job_outputs

            purge_job_outputs(c, row["id"])
            c.execute(
                "UPDATE jobs SET state='rejected',message=?,content=NULL,result_json=NULL,"
                "lease=NULL,claimed_by=NULL,retryable=0,updated=? WHERE id=?",
                (message, time.time(), row["id"]),
            )
            c.execute("UPDATE cards SET state='rejected' WHERE id=?", (row["card_id"],))
            event(c, "fulfillment.rejected", row["product_id"], job(c, row["id"]))
        else:
            promote_final_files(c, row, terminal)
            # The engine has ended; the legacy writer now applies final delivery
            # rules using the issuance snapshot rather than the current editor.
            c.execute("UPDATE jobs SET state='processing' WHERE id=?", (row["id"],))
            _apply_simple_update(
                c,
                row["id"],
                JobUpdate(
                    state=terminal["state"],
                    attempt=row["attempt"],
                    output=terminal.get("output"),
                    message=message,
                    retryable=terminal.get("retryable", False),
                ),
                product_override=final_product,
            )
    from . import flow_worker

    flow_worker.sync_dispatch(c, job(c, row["id"]))
    return job(c, row["id"])


def _ids(field, value):
    from .field_values import attachment_ids

    try:
        return attachment_ids(field, value)
    except ValueError:
        fail("附件字段格式不正确")


def bind_stage_file(c, row, node_id, epoch, kind, fid):
    c.execute(
        "INSERT INTO task_flow_files(file_id,job_id,node_id,flow_epoch,attempt,kind) "
        "VALUES (?,?,?,?,?,?)",
        (fid, row["id"], node_id, epoch, row["attempt"], kind),
    )


def validate_stage_files(c, row, accepted):
    from .field_values import ATTACHMENT_TYPES
    from .files import _file

    kind = accepted["kind"]
    epoch = accepted["flow_epoch"]
    node_id = accepted["node_id"]
    for field in accepted["fields"]:
        if field["type"] not in ATTACHMENT_TYPES:
            continue
        for fid in _ids(field, accepted["values"].get(field["key"], "")):
            item = _file(c, fid)
            scope = c.execute(
                "SELECT * FROM task_flow_files WHERE file_id=?", (fid,)
            ).fetchone()
            if (
                scope is None
                or scope["job_id"] != row["id"]
                or scope["node_id"] != node_id
                or scope["flow_epoch"] != epoch
                or scope["attempt"] != row["attempt"]
                or scope["kind"] != kind
                or item["job_id"] != row["id"]
                or item["product_id"] != row["product_id"]
                or item["card_id"] != row["card_id"]
                or item["attempt"] != row["attempt"]
                or item["field_key"] != field["key"]
                or item["kind"] != kind
                or not item["available"]
            ):
                fail("附件不属于当前任务步骤或字段", 403)
            c.execute("UPDATE job_files SET bound=1 WHERE id=?", (fid,))


def promote_final_files(c, row, terminal):
    from . import task_flow
    from .field_values import ATTACHMENT_TYPES
    from .files import _file

    snapshot = task_flow.card_snapshot(c, row["card_id"])
    frozen = snapshot.get("product", snapshot)
    sources = terminal.get("result_sources", {})
    seen = set()
    for field in frozen["outputs"]:
        if field["type"] not in ATTACHMENT_TYPES:
            continue
        ids = _ids(field, terminal.get("output", {}).get(field["key"], ""))
        if not ids:
            continue
        source = sources.get(field["key"])
        if source is None:
            fail("交付附件缺少经过验证的步骤来源", 403)
        for fid in ids:
            item = _file(c, fid)
            scope = c.execute(
                "SELECT * FROM task_flow_files WHERE file_id=?", (fid,)
            ).fetchone()
            if (
                fid in seen
                or scope is None
                or scope["job_id"] != row["id"]
                or scope["attempt"] != row["attempt"]
                or scope["kind"] != "output"
                or scope["node_id"] != source["node"]
                or scope["flow_epoch"] != source["flow_epoch"]
                or item["field_key"] != source["field"]
                or not item["bound"]
                or not item["available"]
                or item["card_id"] != row["card_id"]
                or item["product_id"] != row["product_id"]
                or item["job_id"] != row["id"]
            ):
                fail("最终交付附件的步骤来源无效", 403)
            seen.add(fid)
            c.execute(
                "UPDATE job_files SET field_key=? WHERE id=?", (field["key"], fid)
            )


def input_file_scope(c, card, field_key, *, flow_epoch, revision, node_id):
    from . import task_flow
    from .files import _field
    from .service import job_product

    row = c.execute("SELECT * FROM jobs WHERE card_id=?", (card["id"],)).fetchone()
    if row is None or not task_flow.is_flow(c, row):
        return None
    state = task_flow.view(c, row)
    if (
        state["phase"] != "input"
        or flow_epoch != state["flow_epoch"]
        or revision != state["revision"]
        or node_id != state["current"]["id"]
        or (state.get("deadline") is not None and state["deadline"] <= time.time())
    ):
        fail("上传所属步骤已改变或超时，请刷新", 409)
    p = {**job_product(c, row), "parameters": state["current"]["fields"]}
    _field(p, field_key, "input")
    return p, row


def output_file_scope(c, row, *, flow_epoch, node_id=None):
    from . import task_flow

    if not task_flow.is_flow(c, row):
        return None
    execution = task_flow.execution(c, row)
    if (
        execution is None
        or execution["flow_epoch"] != flow_epoch
        or row["state"] != "processing"
        or (node_id is not None and node_id != execution["node_id"])
        or execution["deadline"] <= time.time()
    ):
        fail("上传所属处理步骤已改变或超时", 409)
    return execution


def private_worker_file_scope(c, row, execution, field_key, kind, file_id=None):
    """Authorize exactly the inputs referenced by this active dispatch."""
    from .field_values import ATTACHMENT_TYPES
    from .files import _file

    if kind == "output":
        field = next((f for f in execution["outputs"] if f["key"] == field_key), None)
        if field is None or field["type"] not in ATTACHMENT_TYPES:
            fail("当前处理步骤没有这个附件输出字段")
        return field
    if kind != "input" or not file_id:
        fail("附件请求无效")
    params = execution["params"]
    field = next(
        (f for f in execution.get("parameters", []) if f["key"] == field_key), None
    )
    if (
        field is None
        or field["type"] not in ATTACHMENT_TYPES
        or file_id not in _ids(field, params.get(field_key, ""))
    ):
        fail("文件不是当前处理步骤授权的输入", 403)
    item = _file(c, file_id)
    if (
        item["job_id"] != row["id"]
        or item["card_id"] != row["card_id"]
        or item["product_id"] != row["product_id"]
        or item["attempt"] != row["attempt"]
        or not item["bound"]
        or not item["available"]
    ):
        fail("文件不属于本次任务", 403)
    return field


def bind_private_worker_file(c, row, execution, field_key, file_id):
    private_worker_file_scope(c, row, execution, field_key, "output")
    bind_stage_file(
        c, row, execution["node_id"], execution["flow_epoch"], "output", file_id
    )


def release_actor_tasks(c, actor):
    """Keep step and job claim state consistent when authorization is revoked."""
    from . import task_flow

    rows = c.execute(
        "SELECT * FROM jobs WHERE claimed_by=? AND state='processing'", (actor,)
    ).fetchall()
    for row in rows:
        if task_flow.is_flow(c, row):
            current = task_flow.view(c, row)
            finalize(c, task_flow.release_claim(c, row, actor, current["flow_epoch"]))


def _customer_action(operation, body, request):
    from . import task_flow
    from .service import job_view
    from .shops import require_enabled_product

    rate_limit(request, "task-flow", 60, 60)
    expired = False
    with db() as c:
        card = resolve_customer_card(c, body.token, body.card_id)
        require_enabled_product(c, card["product_id"])
        row = c.execute("SELECT * FROM jobs WHERE card_id=?", (card["id"],)).fetchone()
        epoch, revision = body.flow_epoch, body.expected_revision
        if row is None:
            if operation != "start" or epoch != 0 or revision != 0:
                fail("请先开始任务流程", 409)
            row = submit_flow(c, card, {})
            if row is None:
                fail("此卡密没有任务流程", 409)
            current = task_flow.view(c, row)
            epoch, revision = current["flow_epoch"], current["revision"]
        if not task_flow.is_flow(c, row):
            fail("此任务没有步骤流程", 409)
        if operation == "restart":
            if body.values:
                fail("重新开始不能提交字段值")
            if row["state"] not in ("failed", "needs_input"):
                fail("只有允许重试的失败任务可以重新开始", 409)
            current = task_flow.view(c, row)
            if current["flow_epoch"] != epoch or current["revision"] != revision:
                fail("任务流程已改变，请刷新", 409)
            updated = submit_flow(c, card, {})
            response = job_view(c, updated)
            audit(c, "customer", "task_flow.restart", row["id"])
            return response
        fn = {
            "start": task_flow.start,
            "answer": task_flow.answer,
            "continue": task_flow.continue_display,
            "cancel": task_flow.cancel,
        }[operation]
        if operation == "answer":
            effect = fn(c, row, body.values, epoch, revision)
        else:
            if body.values:
                fail("此操作不能提交字段值")
            effect = fn(c, row, epoch, revision)
        updated = finalize(c, effect)
        if operation == "cancel":
            from .files import purge_job_files

            purge_job_files(c, row["id"])
            task_flow.destroy(c, updated)
        expired = effect.get("expired", False)
        audit(c, "customer", "task_flow." + operation, row["id"])
        response = job_view(c, updated)
    if expired:
        fail("当前步骤已超时，请刷新查看后续步骤", 409)
    return response


@router.post("/api/task-flow/start")
def start(body: FlowAction, request: Request):
    return _customer_action("start", body, request)


@router.post("/api/task-flow/answer")
def answer(body: FlowAction, request: Request):
    return _customer_action("answer", body, request)


@router.post("/api/task-flow/continue")
def continue_flow(body: FlowAction, request: Request):
    return _customer_action("continue", body, request)


@router.post("/api/task-flow/cancel")
def cancel(body: FlowAction, request: Request):
    return _customer_action("cancel", body, request)


@router.post("/api/task-flow/restart")
def restart(body: FlowAction, request: Request):
    return _customer_action("restart", body, request)

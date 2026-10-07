import json
import re
import time
import uuid
from urllib.parse import urlsplit

from .card_tracking import card_expired, ensure_card_usable, record_issue
from .db import event
from .models import JobUpdate, OutputField
from .processors import normalize_product
from .security import card_digest, fail, new_card
from .variants import card_variant, resolve_product_variants


def product(c, pid):
    row = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not row:
        fail("商品不存在", 404)
    config = json.loads(row["config"])
    config.setdefault("progress_steps", [])
    config.setdefault("support_email", "")
    config.setdefault("workshop_slogan", "")
    config["variants"] = resolve_product_variants(config)
    config.setdefault("processor_id", "")
    config.setdefault("processor_config", {})
    config.setdefault("task_flow", None)
    config.setdefault("revision_policy", None)
    # Old products predate explicit result schemas. Keep their existing receipts
    # and callbacks compatible without rewriting stored merchant configuration.
    config.setdefault(
        "outputs",
        [
            OutputField(
                key="content",
                label={"zh-CN": "交付内容", "en": "Delivery content"},
                type="textarea",
            ).model_dump()
        ]
        if config["delivery"] == "content"
        else [],
    )
    if config.get("mode") == "script" and config.get("processor_id"):
        config = normalize_product(config)
        # Configuration is write-only and lives in the tenant credential vault.
        # Reads and product exports must not materialize decrypted settings.
        config["processor_config"] = {}
    return {"id": row["id"], **config}


def public_product(p):
    result = {
        k: v
        for k, v in p.items()
        if k
        not in (
            "webhook_url",
            "webhook_secret",
            "script",
            "processor_config",
            "shop_id",
            "profile_id",
            "processor_profile",
            "processor_profile_id",
            "processor_binding",
            "configured_fields",
            "task_flow",
            "workshop_slogan",
            "deleted",
            "deleted_at",
            "deleted_by",
            "purged",
            "purged_at",
            "purged_by",
        )
    }
    if p.get("task_flow"):
        from .flow_adapter import preview

        result["task_flow_view"] = preview(p)
    return result


def card_product(c, card):
    """Issued optional flows and instant text keep their issuance semantics."""
    from . import task_flow, text_cards

    snapshot = task_flow.card_snapshot(c, card["id"])
    if snapshot is not None:
        return {
            **product(c, card["product_id"]),
            **snapshot["product"],
            "task_flow": snapshot["definition"],
        }
    p = product(c, card["product_id"])
    from .card_entitlements import frozen_policy

    p["revision_policy"] = frozen_policy(c, card["id"])
    # An old card must not acquire a flow merely because its product was edited.
    p["task_flow"] = None
    frozen = text_cards.card_definition(c, card["id"])
    if frozen:
        p.update(frozen)
    return p


def issue_cards(
    c,
    pid,
    count,
    label="",
    expires=None,
    variant_id="default",
    routed=None,
    attributes=None,
):
    from .proxy_routes import default_issuer_route, wrap_issued_codes
    from .shops import require_enabled_product

    owner = require_enabled_product(c, pid)
    from .product_lifecycle import require_active

    require_active(c, pid)
    route = default_issuer_route(c, owner["shop_id"], routed)
    p = product(c, pid)
    if p["mode"] == "stock":
        fail("一卡一文本商品请粘贴文本或导入文件生成卡密", 409)
    variant = next((v for v in p["variants"] if v["id"] == variant_id), None)
    if variant is None:
        fail("商品规格不存在", 400)
    if not variant["enabled"]:
        fail("商品规格已停用，不能发行新卡密", 409)
    from .card_entitlements import validate_capacity
    from .variants import validate_attributes

    try:
        effective = validate_attributes({**variant["attributes"], **(attributes or {})})
        validate_capacity(effective, p.get("revision_policy"))
    except ValueError as error:
        fail(str(error))
    if p["mode"] == "script":
        from .processor_profiles import freeze_card_binding, issue_configuration

        binding = issue_configuration(c, pid)
    codes = []
    for _ in range(count):
        code = new_card()
        card_id = str(uuid.uuid4())
        c.execute(
            "INSERT INTO cards(id,digest,product_id,created) VALUES (?,?,?,?)",
            (card_id, card_digest(code), pid, time.time()),
        )
        if p["mode"] == "script":
            freeze_card_binding(c, card_id, pid, binding)
        if p.get("task_flow"):
            from .task_flow import freeze_card

            freeze_card(c, card_id, p)
        codes.append(code)
    record_issue(
        c,
        pid,
        codes,
        label=label,
        expires=expires,
        variant_id=variant_id,
        variant_snapshot=variant,
        attributes=attributes,
    )
    return wrap_issued_codes(c, codes, route)


def validate_params(p, params):
    from .field_values import normalize_rich_value

    if set(params) - {v["key"] for v in p["parameters"]}:
        fail("提交了未定义的参数")
    clean = {}
    for f in p["parameters"]:
        raw = params.get(f["key"], "")
        # Processor code owns normalization, including CSV rows and indentation.
        # Other products retain their existing field trimming behavior.
        v = raw if p["mode"] == "script" else raw.strip()
        if f["required"] and not v.strip():
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
        if v and f["type"] == "url" and not valid_delivery_url(v):
            fail("链接格式不正确")
        try:
            v = normalize_rich_value(f, v)
        except ValueError as error:
            fail(str(error))
        clean[f["key"]] = v
    if p["mode"] == "script":
        from extore_processors import validate_parameters

        try:
            clean = validate_parameters(p["processor_id"], clean)
        except ValueError:
            fail("填写内容不符合商品处理器要求")
    return clean


def valid_delivery_url(value):
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in ("https", "http")
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and "\\" not in value
            and not re.search(r"[\s\x00-\x1f\x7f]", value)
        )
        # urlsplit deliberately defers invalid numeric/range port errors.
        parsed.port
    except ValueError:
        return False
    return bool(valid)


def validate_output(p, output):
    """Validate a delivery against the product's declared result fields."""
    from .field_values import normalize_rich_value

    fields = p["outputs"]
    if set(output) - {f["key"] for f in fields}:
        fail("提交了未定义的输出字段")
    if any(not isinstance(value, str) for value in output.values()):
        fail("输出字段必须是文本")
    if sum(len(value) for value in output.values()) > 100000:
        fail("交付内容过长")
    clean = {}
    for field in fields:
        value = output.get(field["key"], "")
        if field["required"] and not value.strip():
            fail(f"请填写 {next(iter(field['label'].values()))}")
        kind = field["type"]
        # Text deliveries preserve exact formatting, including indentation.
        if kind in ("email", "number", "url"):
            value = value.strip()
        if (
            value
            and kind == "email"
            and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value)
        ):
            fail("输出邮箱格式不正确")
        if value and kind == "number":
            import decimal

            try:
                if not decimal.Decimal(value).is_finite():
                    fail("输出必须是有限数字")
            except decimal.InvalidOperation:
                fail("输出必须是数字")
        if value and kind == "url":
            if not valid_delivery_url(value):
                fail("输出链接格式不正确")
        try:
            value = normalize_rich_value(
                field,
                value.strip()
                if kind in ("select", "boolean", "image", "images")
                else value,
            )
        except ValueError as error:
            fail(str(error))
        clean[field["key"]] = value
    return clean


def output_content(p, output):
    """Readable compatibility value for clients using the original content API."""
    if len(p["outputs"]) == 1 and p["outputs"][0]["key"] == "content":
        return output["content"]
    return "\n\n".join(
        f"{field['label'].get('zh-CN', next(iter(field['label'].values())))}：\n"
        f"{output[field['key']]}"
        for field in p["outputs"]
        if output[field["key"]]
    )


def job(c, jid):
    row = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        fail("任务不存在", 404)
    return row


def freeze_product_schemas(c, pid, p):
    snapshot = json.dumps(
        {key: p[key] for key in ("parameters", "outputs")}, ensure_ascii=False
    )
    c.execute(
        "UPDATE jobs SET schema_snapshot=? WHERE product_id=? AND schema_snapshot IS NULL",
        (snapshot, pid),
    )


def job_product(c, row):
    card = c.execute("SELECT * FROM cards WHERE id=?", (row["card_id"],)).fetchone()
    p = card_product(c, card)
    raw = row["schema_snapshot"]
    if raw is None:
        raw = c.execute(
            "SELECT schema_snapshot FROM jobs WHERE id=?", (row["id"],)
        ).fetchone()[0]
    if raw is None:
        raw = json.dumps(
            {key: p[key] for key in ("parameters", "outputs")}, ensure_ascii=False
        )
        c.execute(
            "UPDATE jobs SET schema_snapshot=? WHERE id=? AND schema_snapshot IS NULL",
            (raw, row["id"]),
        )
    result = {**p, **json.loads(raw)}
    from . import task_flow

    if task_flow.is_flow(c, row):
        # Schemas are public metadata. Reading them must never consume or reopen
        # an OTP, and already-started output remains valid after its input expires.
        flow = task_flow.view(c, row)
        if flow["phase"] in ("queued", "processing"):
            snapshot = task_flow.card_snapshot(c, row["card_id"])
            nodes = {node["id"]: node for node in snapshot["definition"]["nodes"]}
            node = nodes[flow["current"]["id"]]
            result["parameters"] = [
                {
                    **next(
                        field
                        for field in nodes[ref["node"]].get(
                            "fields", nodes[ref["node"]].get("outputs", [])
                        )
                        if field["key"] == ref["field"]
                    ),
                    "key": key,
                }
                for key, ref in node["inputs"].items()
            ]
            result["outputs"] = node["outputs"]
    return result


def progress_snapshot(c, row):
    """Bind a legacy job once; future product edits never replace its plan."""
    raw_plan = row["progress_plan"]
    if raw_plan is None:
        # A caller may still hold a row read before a product edit froze it.
        raw_plan = c.execute(
            "SELECT progress_plan FROM jobs WHERE id=?", (row["id"],)
        ).fetchone()["progress_plan"]
    if raw_plan is None:
        steps = product(c, row["product_id"])["progress_steps"]
        c.execute(
            "UPDATE jobs SET progress_plan=? WHERE id=? AND progress_plan IS NULL",
            (json.dumps(steps, ensure_ascii=False), row["id"]),
        )
    else:
        steps = json.loads(raw_plan)
    return steps, json.loads(row["completed_steps"] or "[]")


def progress_view(c, row):
    steps, completed = progress_snapshot(c, row)
    done = set(completed)
    return [{**step, "done": step["id"] in done} for step in steps], completed


def freeze_product_plans(c, pid, steps):
    c.execute(
        "UPDATE jobs SET progress_plan=? WHERE product_id=? AND progress_plan IS NULL",
        (json.dumps(steps, ensure_ascii=False), pid),
    )


def bootstrap_progress_plan(c, row, steps):
    plan, completed = progress_snapshot(c, row)
    if row["state"] not in ("queued", "processing") or plan or completed:
        fail("只有尚未定义步骤的待处理任务可以设置处理步骤", 409)
    c.execute(
        "UPDATE jobs SET progress_plan=?,completed_steps='[]',progress=0 WHERE id=?",
        (json.dumps(steps, ensure_ascii=False), row["id"]),
    )


def job_view(c, row, staff=False, *, include_deliveries=True):
    p = job_product(c, row)
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
    result["variant"] = card_variant(c, row)
    result["steps"], result["completed_steps"] = progress_view(c, row)
    if result["steps"]:
        result["progress"] = (
            100
            if row["state"] in ("succeeded", "destroyed")
            else min(99, len(result["completed_steps"]) * 100 // len(result["steps"]))
        )
    result["support_email"] = p["support_email"]
    result["delivery"] = p["delivery"]
    result["view_policy"] = p["view_policy"]
    card_state = c.execute(
        "SELECT state FROM cards WHERE id=?", (row["card_id"],)
    ).fetchone()[0]
    result["can_retry"] = bool(
        (
            row["state"] == "needs_input"
            or (
                row["state"] == "failed"
                and row["retryable"]
                and p["allow_retry"]
                and _round_attempt(row) < p["max_attempts"]
            )
        )
        and not card_expired(c, row["card_id"])
        and card_state not in ("revoked", "rejected")
    )
    if row["state"] == "needs_input":
        result["params"] = json.loads(row["params"])
        result["retry_mode"] = (
            row["retry_mode"] if "retry_mode" in row.keys() else "revise"
        )
        result["retry_reason_type"] = (
            row["retry_reason_type"]
            if "retry_reason_type" in row.keys()
            else "customer_input"
        )
    result["queue_ahead"] = (
        c.execute(
            "SELECT count(*) FROM jobs WHERE product_id=? AND state IN ('queued','processing') "
            "AND (created<? OR (created=? AND id<?))",
            (row["product_id"], row["created"], row["created"], row["id"]),
        ).fetchone()[0]
        if row["state"] in ("queued", "processing")
        else 0
    )
    result["queue_position"] = (
        result["queue_ahead"] + 1 if row["state"] in ("queued", "processing") else 0
    )
    if staff:
        from .work_instructions import instructions

        result["instructions"] = instructions(c, row["product_id"])
        result["params"] = json.loads(row["params"])
        result["claimed_by"] = row["claimed_by"]
        from .agent_identity import processing_worker

        result["processing_worker"] = processing_worker(c, row)
        result["mode"] = p["mode"]
        result["parameters"] = p["parameters"]
        result["outputs"] = p["outputs"]
        from .files import listfiles

        result["files"] = listfiles(c, row)
    from .card_entitlements import job_context

    result.update(job_context(c, row, include_deliveries=include_deliveries))
    from . import task_flow

    if task_flow.is_flow(c, row):
        flow = task_flow.view(c, row, staff=staff)
        result["task_flow"] = flow
        if staff and flow["phase"] in ("queued", "processing"):
            authority = task_flow.frozen_authority(
                c, row, flow["flow_epoch"], row["attempt"]
            )
            result["flow_epoch"] = authority["flow_epoch"]
            result["action_id"] = authority["action_id"]
            result["protected_fields"] = sorted(
                f["key"] for f in p["parameters"] if f.get("sensitive")
            )
            execution = task_flow.execution(c, row)
            if execution:
                fields = execution.get("parameters", [])
                protected = {f["key"] for f in fields if f.get("sensitive")}
                result["params"] = {
                    key: value
                    for key, value in execution["params"].items()
                    if key not in protected
                }
                result["parameters"] = fields
                result["outputs"] = execution["outputs"]
                result["flow_epoch"] = execution["flow_epoch"]
                result["action_id"] = execution["action_id"]
                result["protected_fields"] = sorted(protected)
    return result


def _round_attempt(row):
    from .card_entitlements import round_attempt

    return round_attempt(row)


def submit(c, card, params):
    from .files import bind_inputs, purge_job_outputs, validate_input_files
    from .shops import require_enabled_product

    require_enabled_product(c, card["product_id"])
    ensure_card_usable(c, card)
    from .flow_adapter import submit_flow

    flow_row = submit_flow(c, card, params)
    if flow_row is not None:
        return flow_row
    row = c.execute("SELECT * FROM jobs WHERE card_id=?", (card["id"],)).fetchone()
    if row and row["state"] == "rejected":
        fail("此任务已被拒绝，不能重新提交", 409)
    p = job_product(c, row) if row else card_product(c, card)
    clean = validate_params(p, params)
    if (
        row
        and row["state"] == "needs_input"
        and "retry_mode" in row.keys()
        and row["retry_mode"] == "reuse"
        and clean != json.loads(row["params"])
    ):
        fail("此任务重试必须复用原有需求和附件，请使用重试入口", 409)
    validate_input_files(c, card, p, clean)
    if row:
        if row["state"] not in ("failed", "needs_input"):
            return row
        if row["state"] == "failed" and (
            not row["retryable"]
            or not p["allow_retry"]
            or _round_attempt(row) >= p["max_attempts"]
        ):
            fail("此任务不能自动重试，请联系商家", 409)
        progress_snapshot(c, row)
        purge_job_outputs(c, row["id"])
        c.execute(
            "UPDATE jobs SET state='queued',params=?,content=NULL,result_json=NULL,message='',progress=0,completed_steps='[]',attempt=attempt+1,retryable=0,claimed_by=NULL,lease=NULL,updated=? WHERE id=?",
            (json.dumps(clean), time.time(), row["id"]),
        )
        c.execute(
            "UPDATE cards SET state=? WHERE id=?",
            (
                "used"
                if "revision_round" in row.keys() and row["revision_round"]
                else "reserved",
                card["id"],
            ),
        )
        jid = row["id"]
    else:
        if card["state"] != "ready":
            fail("此卡密无法兑换", 409)
        jid = str(uuid.uuid4())
        now = time.time()
        c.execute(
            "INSERT INTO jobs(id,card_id,product_id,state,params,created,updated,progress_plan,schema_snapshot) VALUES (?,?,?,'queued',?,?,?,?,?)",
            (
                jid,
                card["id"],
                p["id"],
                json.dumps(clean),
                now,
                now,
                json.dumps(p["progress_steps"], ensure_ascii=False),
                json.dumps(
                    {key: p[key] for key in ("parameters", "outputs")},
                    ensure_ascii=False,
                ),
            ),
        )
        c.execute("UPDATE cards SET state='reserved' WHERE id=?", (card["id"],))
    row = job(c, jid)
    bind_inputs(c, row, clean)
    if p["mode"] == "stock":
        from .text_cards import assigned_payload, clear_assignment

        row = _apply_simple_update(
            c,
            jid,
            JobUpdate(
                state="succeeded",
                attempt=row["attempt"],
                output={"content": assigned_payload(c, card["id"])},
            ),
        )
        clear_assignment(c, card["id"])
    else:
        event(c, "redemption.requested", p["id"], row)
    return row


def finalize_task_flow(c, effect):
    from .flow_adapter import finalize

    return finalize(c, effect)


def apply_update(c, jid, update: JobUpdate):
    from . import task_flow

    row = job(c, jid)
    if task_flow.is_flow(c, row):
        if update.flow_epoch is None:
            fail("流程处理必须指定当前步骤的 flow_epoch", 409)
        return finalize_task_flow(
            c,
            task_flow.process_update(
                c, row, update, update.flow_epoch, actor=row["claimed_by"]
            ),
        )
    return _apply_simple_update(c, jid, update)


def _apply_simple_update(c, jid, update: JobUpdate, *, product_override=None):
    from .files import bind_outputs, validate_output_files

    row = job(c, jid)
    if row["attempt"] != update.attempt:
        fail("回调对应的尝试已失效", 409)
    if row["state"] == update.state and row["state"] in ("succeeded", "failed"):
        return row  # idempotent completion, never overwrite a result
    if row["state"] not in ("queued", "processing"):
        fail("任务已经结束，不能覆盖结果", 409)
    p = product_override or job_product(c, row)
    plan, previous = progress_snapshot(c, row)
    step_ids = [step["id"] for step in plan]
    completed = previous if update.completed_steps is None else update.completed_steps
    if set(completed) - set(step_ids):
        fail("提交了任务计划中未定义的处理步骤")
    if set(previous) - set(completed):
        fail("已完成步骤不能撤回", 409)
    if update.state == "succeeded":
        completed = step_ids
    else:
        completed = [step_id for step_id in step_ids if step_id in completed]
    output = None
    if update.state != "succeeded" and update.output:
        fail("只有成功状态可以包含交付结果")
    if update.state == "succeeded":
        if p["delivery"] == "content":
            supplied = update.output
            if update.content is not None:
                if len(p["outputs"]) != 1 or p["outputs"][0]["key"] != "content":
                    fail("请按商品定义提交输出字段")
                if supplied is not None and supplied.get("content") != update.content:
                    fail("content 与输出字段不一致")
                supplied = {"content": update.content} if supplied is None else supplied
            output = validate_output(p, supplied or {})
            validate_output_files(c, row, p, output)
        elif update.output:
            fail("服务型商品只返回状态，不包含交付结果")
    progress = min(99, len(completed) * 100 // len(plan)) if plan else update.progress
    if not plan and update.state == "processing" and progress < row["progress"]:
        fail("进度不能倒退", 409)
    content = output_content(p, output) if output is not None else None
    if output is not None and row["revision_round"] > 0:
        from .card_entitlements import allocated_bytes
        from .files import MAX_CARD_BYTES
        from .storage import check_storage_quota

        extra = len((content or "").encode("utf-8")) + len(
            json.dumps(output, ensure_ascii=False).encode("utf-8")
        )
        stored_files = c.execute(
            "SELECT COALESCE(SUM(size),0) FROM job_files WHERE card_id=? AND content IS NOT NULL",
            (row["card_id"],),
        ).fetchone()[0]
        if (
            stored_files + allocated_bytes(c, card_id=row["card_id"]) + extra
            > MAX_CARD_BYTES
        ):
            fail("此卡密的交付历史和文件总量已达到容量上限", 413)
        check_storage_quota(c, extra, product_id=row["product_id"])
    progress = 100 if update.state == "succeeded" else progress
    c.execute(
        "UPDATE jobs SET state=?,progress=?,message=?,content=?,result_json=?,completed_steps=?,retryable=?,updated=?,lease=? WHERE id=?",
        (
            update.state,
            progress,
            update.message,
            content,
            json.dumps(output, ensure_ascii=False) if output is not None else None,
            json.dumps(completed),
            int(update.retryable and update.state == "failed"),
            time.time(),
            time.time() + 3600 if update.state == "processing" else None,
            jid,
        ),
    )
    if update.state == "succeeded":
        c.execute("UPDATE cards SET state='used' WHERE id=?", (row["card_id"],))
    row = job(c, jid)
    if output is not None:
        bind_outputs(c, row, output)
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

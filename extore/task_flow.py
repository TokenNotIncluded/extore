"""Optional, issued-card-bound task graphs; no network or user code executes here.

Callers hold the existing BEGIN IMMEDIATE transaction and finalize every effect
with service.finalize_task_flow before committing. Private values in an effect
are for the trusted adapter only; HTTP responses use job_view/view instead.
"""

import hashlib
import hmac
import json
import time
import uuid

from .secret_store import MAX_SECRET_BYTES, _context, open_secret, store_secret
from .security import fail
from .task_flow_definition import MAX_TRANSITIONS, validate_definition, validate_values

MAX_RUN_CIPHERTEXT_BYTES = 2 * 1024 * 1024


def _quota(c, ciphertext, product_id, *, job_id=None, old_bytes=0):
    additional = len(ciphertext.encode("utf-8")) - old_bytes
    if job_id is not None and additional > 0:
        total = c.execute(
            "SELECT COALESCE(SUM(length(payload_ciphertext)),0) FROM task_flow_steps WHERE job_id=?",
            (job_id,),
        ).fetchone()[0]
        total += c.execute(
            "SELECT length(snapshot_ciphertext) FROM card_task_flows JOIN jobs "
            "ON jobs.card_id=card_task_flows.card_id WHERE jobs.id=?",
            (job_id,),
        ).fetchone()[0]
        if c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_flow_dispatches'"
        ).fetchone():
            total += c.execute(
                "SELECT COALESCE(SUM(length(payload_ciphertext)),0) FROM task_flow_dispatches WHERE job_id=?",
                (job_id,),
            ).fetchone()[0]
        if total + additional > MAX_RUN_CIPHERTEXT_BYTES:
            fail("任务流程文字存储达到上限，请使用附件", 413)
    if additional > 0:
        from .storage import check_storage_quota

        check_storage_quota(c, additional, product_id=product_id)


def init_schema(c):
    c.execute(
        "CREATE TABLE IF NOT EXISTS card_task_flows ("
        "card_id TEXT PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,"
        "product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,"
        "shop_id TEXT NOT NULL REFERENCES shops(id),definition_hash TEXT NOT NULL,"
        "snapshot_ciphertext TEXT NOT NULL,created REAL NOT NULL)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS task_flow_runs ("
        "job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,"
        "attempt INTEGER NOT NULL,flow_epoch INTEGER NOT NULL DEFAULT 0,"
        "revision INTEGER NOT NULL DEFAULT 0,node_id TEXT NOT NULL DEFAULT '',"
        "phase TEXT NOT NULL,entered_at REAL,started_at REAL,deadline REAL,input_expires_at REAL,"
        "transition_count INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,"
        "updated REAL NOT NULL)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS task_flow_steps ("
        "job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,"
        "flow_epoch INTEGER NOT NULL,attempt INTEGER NOT NULL,node_id TEXT NOT NULL,"
        "kind TEXT NOT NULL,state TEXT NOT NULL,action_id TEXT,entered_at REAL NOT NULL,"
        "started_at REAL,deadline REAL,completed_at REAL,payload_ciphertext TEXT NOT NULL,"
        "result_digest TEXT,PRIMARY KEY(job_id,flow_epoch))"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS task_flow_deadlines ON task_flow_runs(deadline,phase)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS task_flow_input_expiry ON task_flow_runs(input_expires_at,phase)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS task_flow_step_lookup "
        "ON task_flow_steps(job_id,attempt,node_id,flow_epoch DESC)"
    )


def _json(value):
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        fail("任务流程数据格式无效", 422)
    if len(raw) > MAX_SECRET_BYTES:
        fail("任务流程单步数据过大，请使用附件", 413)
    return raw


def _digest(value):
    return hashlib.sha256(_json(value)).hexdigest()


def fingerprint(value, shop_id, resource_id):
    """Keyed equality receipt: short secrets must not leave enumerable hashes."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    key, aad = _context(shop_id, "task-flow-receipt", resource_id)
    receipt_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"extore-task-flow-receipt-v1",
        info=aad,
    ).derive(key)
    return hmac.new(receipt_key, _json(value), hashlib.sha256).hexdigest()


def _receipt(value, snapshot, step):
    return fingerprint(
        value, snapshot["product"]["shop_id"], f"{step['job_id']}:{step['flow_epoch']}"
    )


def _values(fields, values, *, output=False):
    try:
        return validate_values(fields, values, output=output)
    except ValueError as exc:
        fail(str(exc), 413 if "大小上限" in str(exc) else 422)


def _seal(value, shop_id, resource_type, resource_id):
    _json(value)
    return store_secret(
        value, tenant_id=shop_id, resource_type=resource_type, resource_id=resource_id
    )


def _open(value, shop_id, resource_type, resource_id):
    return open_secret(
        value, tenant_id=shop_id, resource_type=resource_type, resource_id=resource_id
    )


def freeze_card(c, card_id, product):
    definition = validate_definition(product.get("task_flow"), product)
    if definition is None:
        return None
    card = c.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    actual = c.execute(
        "SELECT shop_id FROM products WHERE id=?", (product["id"],)
    ).fetchone()
    if card is None or actual is None or card["product_id"] != product["id"]:
        fail("任务流程发行范围不匹配", 409)
    if c.execute(
        "SELECT 1 FROM card_task_flows WHERE card_id=?", (card_id,)
    ).fetchone():
        fail("卡密的任务流程快照不能覆盖", 409)
    frozen_product = {
        key: product[key]
        for key in (
            "id",
            "name",
            "mode",
            "delivery",
            "view_policy",
            "parameters",
            "outputs",
            "allow_retry",
            "max_attempts",
            "processor_id",
            "webhook_url",
            "webhook_secret",
        )
        if key in product
    }
    frozen_product["shop_id"] = actual["shop_id"]
    snapshot = {"definition": definition, "product": frozen_product}
    definition_hash = fingerprint(snapshot, actual["shop_id"], "card:" + card_id)
    ciphertext = _seal(snapshot, actual["shop_id"], "task-flow-card", card_id)
    _quota(c, ciphertext, product["id"])
    c.execute(
        "INSERT INTO card_task_flows VALUES (?,?,?,?,?,?)",
        (
            card_id,
            product["id"],
            actual["shop_id"],
            definition_hash,
            ciphertext,
            time.time(),
        ),
    )
    return definition_hash


def card_snapshot(c, card_id):
    binding = c.execute(
        "SELECT * FROM card_task_flows WHERE card_id=?", (card_id,)
    ).fetchone()
    if binding is None:
        return None  # Missing issuance binding is always a legacy card.
    snapshot = _open(
        binding["snapshot_ciphertext"], binding["shop_id"], "task-flow-card", card_id
    )
    if (
        fingerprint(snapshot, binding["shop_id"], "card:" + card_id)
        != binding["definition_hash"]
    ):
        fail("任务流程快照无效", 409)
    return {**snapshot, "definition_hash": binding["definition_hash"]}


def is_flow(c, row):
    return (
        c.execute(
            "SELECT 1 FROM card_task_flows WHERE card_id=?", (row["card_id"],)
        ).fetchone()
        is not None
    )


def _job(c, row):
    actual = c.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
    if actual is None:
        fail("任务不存在", 404)
    return actual


def _run(c, row):
    return c.execute(
        "SELECT * FROM task_flow_runs WHERE job_id=?", (row["id"],)
    ).fetchone()


def _guard(c, row):
    row = _job(c, row)
    snapshot = card_snapshot(c, row["card_id"])
    if snapshot is None:
        fail("此卡密没有任务流程", 409)
    product = c.execute(
        "SELECT products.*,shops.enabled FROM products JOIN shops ON shops.id=products.shop_id "
        "WHERE products.id=?",
        (row["product_id"],),
    ).fetchone()
    frozen = snapshot["product"]
    if (
        product is None
        or not product["enabled"]
        or product["id"] != frozen["id"]
        or product["shop_id"] != frozen["shop_id"]
    ):
        fail("任务流程的店铺或商品授权已失效", 403)
    current = json.loads(product["config"])
    if current["mode"] != frozen["mode"]:
        fail("任务流程的处理方式已失效", 403)
    if frozen["mode"] == "webhook" and (
        not current.get("webhook_secret")
        or not hmac.compare_digest(current["webhook_secret"], frozen["webhook_secret"])
    ):
        fail("任务流程的回调授权已撤销", 403)
    if frozen["mode"] == "script" and current.get("processor_id") != frozen.get(
        "processor_id"
    ):
        fail("任务流程的处理器授权已失效", 403)
    card = c.execute("SELECT state FROM cards WHERE id=?", (row["card_id"],)).fetchone()
    if (
        card is None
        or card["state"] in ("revoked", "rejected")
        or row["state"] in ("destroyed", "rejected")
    ):
        fail("任务流程已撤销或销毁", 410)
    run = _run(c, row)
    if run is None or run["attempt"] != row["attempt"]:
        fail("任务流程尝试不匹配", 409)
    return row, run, snapshot


def _nodes(snapshot):
    return {node["id"]: node for node in snapshot["definition"]["nodes"]}


def _payload(c, snapshot, step):
    return _open(
        step["payload_ciphertext"],
        snapshot["product"]["shop_id"],
        "task-flow-step",
        f"{step['job_id']}:{step['flow_epoch']}",
    )


def _write_payload(c, snapshot, step, value):
    ciphertext = _seal(
        value,
        snapshot["product"]["shop_id"],
        "task-flow-step",
        f"{step['job_id']}:{step['flow_epoch']}",
    )
    old_bytes = c.execute(
        "SELECT length(payload_ciphertext) FROM task_flow_steps WHERE job_id=? AND flow_epoch=?",
        (step["job_id"], step["flow_epoch"]),
    ).fetchone()[0]
    _quota(
        c,
        ciphertext,
        snapshot["product"]["id"],
        job_id=step["job_id"],
        old_bytes=old_bytes,
    )
    c.execute(
        "UPDATE task_flow_steps SET payload_ciphertext=? WHERE job_id=? AND flow_epoch=?",
        (
            ciphertext,
            step["job_id"],
            step["flow_epoch"],
        ),
    )


def _step(c, run, epoch=None):
    return c.execute(
        "SELECT * FROM task_flow_steps WHERE job_id=? AND flow_epoch=?",
        (run["job_id"], run["flow_epoch"] if epoch is None else epoch),
    ).fetchone()


def _source(c, run, snapshot, reference):
    step = c.execute(
        "SELECT * FROM task_flow_steps WHERE job_id=? AND attempt=? AND node_id=? "
        "AND state='completed' ORDER BY flow_epoch DESC LIMIT 1",
        (run["job_id"], run["attempt"], reference["node"]),
    ).fetchone()
    if step is None:
        fail("任务流程引用的步骤尚未完成", 409)
    payload = _payload(c, snapshot, step)
    values = payload.get("input" if step["kind"] == "input" else "output", {})
    if reference["field"] not in values:
        fail("任务流程引用的数据已失效", 409)
    return values[reference["field"]], step, payload


def _references(c, run, snapshot, mapping):
    values, sources = {}, {}
    for key, reference in mapping.items():
        value, step, _ = _source(c, run, snapshot, reference)
        values[key] = value
        sources[key] = {
            "node": step["node_id"],
            "field": reference["field"],
            "flow_epoch": step["flow_epoch"],
            "kind": "input" if step["kind"] == "input" else "output",
        }
    return values, sources


def _field(snapshot, reference):
    node = _nodes(snapshot)[reference["node"]]
    fields = (
        node.get("fields", []) if node["kind"] == "input" else node.get("outputs", [])
    )
    return next(field for field in fields if field["key"] == reference["field"])


def _next(c, run, snapshot, transition):
    if isinstance(transition, str):
        return transition
    for case in transition["cases"]:
        condition = case["when"]
        operation = condition["op"]
        from fastapi import HTTPException

        try:
            value, _, _ = _source(c, run, snapshot, condition["source"])
        except HTTPException as exc:
            if operation == "exists" and exc.status_code == 409:
                continue
            raise
        if (
            operation == "eq"
            and value == condition["value"]
            or operation == "in"
            and value in condition["value"]
            or operation == "exists"
            and value != ""
        ):
            return case["to"]
    return transition["default"]


def _shown(c, run, snapshot, node):
    result = []
    for key, reference in node.get("show_from", {}).items():
        field = _field(snapshot, reference)
        if field["type"] in ("file", "image", "images") or field.get("sensitive"):
            continue  # Stage files need a separately authorized download view.
        from fastapi import HTTPException

        try:
            value, _, _ = _source(c, run, snapshot, reference)
        except HTTPException as exc:
            if exc.status_code == 409:
                continue  # A valid branch may not have visited this source.
            raise
        result.append(
            {"key": key, "label": field["label"], "type": field["type"], "value": value}
        )
    return result


def view(c, row, staff=False):
    snapshot = card_snapshot(c, row["card_id"])
    if snapshot is None:
        return None
    run = _run(c, row)
    if run is None:
        return preview(c, row["card_id"])
    node = _nodes(snapshot)[run["node_id"]]
    current = {key: node[key] for key in ("id", "kind", "label") if key in node}
    actions = []
    shown = []
    if run["phase"] == "await_start":
        current.update(
            prompt=node.get("prompt", node.get("content", {})),
            start_policy=node.get("start_policy", "confirm"),
        )
        actions = ["start"]
    elif run["phase"] == "input":
        current.update(
            question=node.get("question", {}),
            fields=node["fields"],
            start_policy=node.get("start_policy", "confirm"),
        )
        shown = _shown(c, run, snapshot, node)
        actions = ["answer"]
    elif run["phase"] == "display":
        current["content"] = node.get("content", {})
        shown = _shown(c, run, snapshot, node)
        actions = ["continue"]
    history = [
        dict(step)
        for step in c.execute(
            "SELECT node_id,flow_epoch,kind,state,entered_at,started_at,completed_at "
            "FROM task_flow_steps WHERE job_id=? ORDER BY flow_epoch LIMIT ?",
            (row["id"], MAX_TRANSITIONS),
        )
    ]
    return {
        "enabled": True,
        "version": 1,
        "definition_hash": snapshot["definition_hash"],
        "attempt": run["attempt"],
        "flow_epoch": run["flow_epoch"],
        "revision": run["revision"],
        "phase": run["phase"],
        "current": current,
        "deadline": run["deadline"],
        "server_time": time.time(),
        "shown": shown,
        "history": history,
        "actions": actions,
    }


def preview(c, card_id):
    snapshot = card_snapshot(c, card_id)
    if snapshot is None:
        return None
    node = _nodes(snapshot)[snapshot["definition"]["entry"]]
    current = {key: node[key] for key in ("id", "kind", "label") if key in node}
    current["prompt"] = node.get("prompt", node.get("content", {}))
    return {
        "enabled": True,
        "version": 1,
        "definition_hash": snapshot["definition_hash"],
        "attempt": 1,
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


def _effect(c, row, terminal=None, **extra):
    row = _job(c, row)
    result = {"row": row, "flow": view(c, row)}
    if terminal is not None:
        result["terminal"] = terminal
    return {**result, **extra}


def _expect(run, epoch, revision=None):
    if type(epoch) is not int or run["flow_epoch"] != epoch:
        fail("任务流程轮次已改变，请刷新", 409)
    if revision is not None and (
        type(revision) is not int or run["revision"] != revision
    ):
        fail("任务流程状态已改变，请刷新", 409)


def _mutate_run(c, run, **changes):
    fields = {**changes, "revision": run["revision"] + 1, "updated": time.time()}
    changed = c.execute(
        "UPDATE task_flow_runs SET "
        + ",".join(key + "=?" for key in fields)
        + " WHERE job_id=? AND revision=? AND flow_epoch=? AND attempt=?",
        (
            *fields.values(),
            run["job_id"],
            run["revision"],
            run["flow_epoch"],
            run["attempt"],
        ),
    ).rowcount
    if changed != 1:
        fail("任务流程状态已改变，请刷新", 409)
    return c.execute(
        "SELECT * FROM task_flow_runs WHERE job_id=?", (run["job_id"],)
    ).fetchone()


def _clear_sensitive(c, run, snapshot, process=None):
    references = process.get("inputs", {}).values() if process else None
    node_ids = {ref["node"] for ref in references} if references is not None else None
    for step in c.execute(
        "SELECT * FROM task_flow_steps WHERE job_id=? AND attempt=? AND kind='input'",
        (run["job_id"], run["attempt"]),
    ).fetchall():
        if node_ids is not None and step["node_id"] not in node_ids:
            continue
        payload = _payload(c, snapshot, step)
        fields = _nodes(snapshot)[step["node_id"]]["fields"]
        changed = False
        for field in fields:
            if field.get("sensitive"):
                changed |= field["key"] in payload.get("input", {})
                payload.get("input", {}).pop(field["key"], None)
                payload.get("sensitive_until", {}).pop(field["key"], None)
        if changed:
            _write_payload(c, snapshot, step, payload)


def _terminal(c, row, run, snapshot, node):
    state = node["state"]
    output, sources = (
        _references(c, run, snapshot, node.get("result", {}))
        if state == "succeeded"
        else ({}, {})
    )
    if state == "succeeded":
        output = _values(snapshot["product"]["outputs"], output, output=True)
    _clear_sensitive(c, run, snapshot)
    return {
        "state": state,
        "output": output,
        "result_sources": sources,
        "message": node.get("message", {}),
        "retryable": node.get("retryable", False),
        "needs_review": node.get("needs_review", False),
    }


def _sensitive_sources(c, run, snapshot, node):
    """Metadata used for bounded TTL sweep; raw values stay encrypted."""
    sources = []
    for reference in node.get("inputs", {}).values():
        if not _field(snapshot, reference).get("sensitive"):
            continue
        step = c.execute(
            "SELECT * FROM task_flow_steps WHERE job_id=? AND attempt=? AND node_id=? "
            "AND state='completed' ORDER BY flow_epoch DESC LIMIT 1",
            (run["job_id"], run["attempt"], reference["node"]),
        ).fetchone()
        payload = _payload(c, snapshot, step) if step is not None else {}
        field = _field(snapshot, reference)
        if payload.get("input", {}).get(reference["field"]) == "" and not field.get(
            "required", True
        ):
            continue
        expiry = payload.get("sensitive_until", {}).get(reference["field"])
        used = payload.get("consumed_by", {}).get(reference["field"])
        sources.append(
            {
                "node": reference["node"],
                "expiry": expiry,
                "valid": reference["field"] in payload.get("input", {})
                and expiry is not None
                and (used is None or used == run["flow_epoch"]),
            }
        )
    return sources


def _sensitive_expiry(c, run, snapshot, *, erase=False, now=None):
    """Track the earliest secret across intermediate input/display nodes too."""
    now = time.time() if now is None else now
    earliest = None
    for step in c.execute(
        "SELECT * FROM task_flow_steps WHERE job_id=? AND attempt=? AND kind='input'",
        (run["job_id"], run["attempt"]),
    ).fetchall():
        payload = _payload(c, snapshot, step)
        changed = False
        for field in _nodes(snapshot)[step["node_id"]]["fields"]:
            key = field["key"]
            if not field.get("sensitive") or not payload.get("input", {}).get(key):
                continue
            expiry = payload.get("sensitive_until", {}).get(key)
            if expiry is None or expiry <= now:
                if erase:
                    payload["input"].pop(key, None)
                    payload.get("sensitive_until", {}).pop(key, None)
                    payload.get("consumed_by", {}).pop(key, None)
                    changed = True
                    continue
                expiry = now if expiry is None else expiry
            earliest = expiry if earliest is None else min(earliest, expiry)
        if changed:
            _write_payload(c, snapshot, step, payload)
    return earliest


def _activate(c, row, run, snapshot, target, *, initial=False):
    if run["transition_count"] >= MAX_TRANSITIONS:
        run = _mutate_run(c, run, phase="ended", deadline=None)
        _clear_sensitive(c, run, snapshot)
        return _effect(
            c,
            row,
            terminal={
                "state": "failed",
                "output": {},
                "result_sources": {},
                "message": {"zh-CN": "任务流程超过执行次数上限，请联系商家"},
                "retryable": False,
                "needs_review": True,
            },
        )
    now = time.time()
    node = _nodes(snapshot)[target]
    kind = node["kind"]
    started = (
        now
        if kind in ("process", "end")
        or not initial
        and (kind == "display" or node.get("start_policy") == "automatic")
        else None
    )
    deadline = (
        now + node["timeout_seconds"]
        if started is not None and node.get("timeout_seconds") is not None
        else None
    )
    phase = {
        "input": "input" if started is not None else "await_start",
        "process": "queued",
        "display": "display" if started is not None else "await_start",
        "end": "ended",
    }[kind]
    epoch = run["flow_epoch"] + 1
    sources = _sensitive_sources(c, run, snapshot, node) if kind == "process" else []
    process_expiry = (
        min(source["expiry"] if source["valid"] else now for source in sources)
        if sources
        else None
    )
    expiry_candidates = (process_expiry, _sensitive_expiry(c, run, snapshot))
    input_expires_at = min(
        (expiry for expiry in expiry_candidates if expiry is not None), default=None
    )
    run = _mutate_run(
        c,
        run,
        flow_epoch=epoch,
        node_id=target,
        phase=phase,
        entered_at=now,
        started_at=started,
        deadline=deadline,
        input_expires_at=input_expires_at,
        transition_count=run["transition_count"] + 1,
    )
    empty_payload = _seal(
        {}, snapshot["product"]["shop_id"], "task-flow-step", f"{row['id']}:{epoch}"
    )
    _quota(c, empty_payload, snapshot["product"]["id"], job_id=row["id"])
    c.execute(
        "INSERT INTO task_flow_steps VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row["id"],
            epoch,
            run["attempt"],
            target,
            kind,
            "active",
            str(uuid.uuid4()) if kind == "process" else None,
            now,
            started,
            deadline,
            None,
            empty_payload,
            None,
        ),
    )
    if kind != "end":
        c.execute(
            "UPDATE jobs SET state=?,claimed_by=NULL,lease=NULL,progress=0,updated=? WHERE id=?",
            ("queued" if kind == "process" else "waiting", now, row["id"]),
        )
    else:
        c.execute(
            "UPDATE task_flow_steps SET state='completed',completed_at=? WHERE job_id=? AND flow_epoch=?",
            (now, row["id"], epoch),
        )
        return _effect(c, row, terminal=_terminal(c, row, run, snapshot, node))
    return _effect(c, row)


def initialize(c, row):
    row = _job(c, row)
    snapshot = card_snapshot(c, row["card_id"])
    if snapshot is None:
        return {"row": row, "flow": None}
    if _run(c, row) is not None:
        return _effect(c, row)
    now = time.time()
    c.execute(
        "INSERT INTO task_flow_runs(job_id,attempt,phase,created,updated) VALUES (?,?,'initializing',?,?)",
        (row["id"], row["attempt"], now, now),
    )
    return _activate(
        c, row, _run(c, row), snapshot, snapshot["definition"]["entry"], initial=True
    )


def _timeout(c, row, run, snapshot):
    node = _nodes(snapshot)[run["node_id"]]
    c.execute(
        "UPDATE task_flow_steps SET state='timed_out',completed_at=? WHERE job_id=? AND flow_epoch=?",
        (time.time(), row["id"], run["flow_epoch"]),
    )
    _clear_sensitive(c, run, snapshot, node if node["kind"] == "process" else None)
    if node["kind"] == "process":
        # A local deadline cannot retract work already started by an external
        # worker. Never turn a timeout into an automatic second side effect.
        _mutate_run(
            c,
            run,
            flow_epoch=run["flow_epoch"] + 1,
            phase="ended",
            deadline=None,
            input_expires_at=None,
        )
        effect = _effect(
            c,
            row,
            terminal={
                "state": "failed",
                "output": {},
                "result_sources": {},
                "message": {"zh-CN": "处理超时或中断，待商家核实"},
                "retryable": False,
                "needs_review": True,
            },
            configured_timeout_target=node["timeout_next"],
        )
    else:
        effect = _activate(c, row, run, snapshot, node["timeout_next"])
    return {**effect, "expired": True}


def _expired(c, row, run, snapshot):
    if run["deadline"] is not None and time.time() >= run["deadline"]:
        return _timeout(c, row, run, snapshot)
    return None


def start(c, row, epoch, revision):
    row, run, snapshot = _guard(c, row)
    _expect(run, epoch)
    if run["phase"] in ("input", "display"):
        return _expired(c, row, run, snapshot) or _effect(c, row)
    _expect(run, epoch, revision)
    if run["phase"] != "await_start":
        fail("当前步骤不能开始答题", 409)
    node = _nodes(snapshot)[run["node_id"]]
    now = time.time()
    deadline = (
        now + node["timeout_seconds"]
        if node.get("timeout_seconds") is not None
        else None
    )
    _mutate_run(
        c,
        run,
        phase="display" if node["kind"] == "display" else "input",
        started_at=now,
        deadline=deadline,
    )
    c.execute(
        "UPDATE task_flow_steps SET started_at=?,deadline=? WHERE job_id=? AND flow_epoch=?",
        (now, deadline, row["id"], epoch),
    )
    return _effect(c, row)


def answer(c, row, values, epoch, revision):
    row, run, snapshot = _guard(c, row)
    old = _step(c, run, epoch)
    if (
        old is not None
        and old["attempt"] == row["attempt"]
        and old["kind"] == "input"
        and old["state"] == "completed"
    ):
        old_node = _nodes(snapshot)[old["node_id"]]
        clean = _values(old_node["fields"], values)
        if hmac.compare_digest(
            old["result_digest"] or "", _receipt(clean, snapshot, old)
        ):
            return _effect(c, row, duplicate=True)
        fail("同一次回答不能更换内容", 409)
    _expect(run, epoch, revision)
    if run["phase"] != "input":
        fail("当前步骤不接受回答", 409)
    expired = _expired(c, row, run, snapshot)
    if expired:
        return expired
    node = _nodes(snapshot)[run["node_id"]]
    clean = _values(node["fields"], values)
    until = {}
    for field in node["fields"]:
        if field.get("sensitive") and clean.get(field["key"]):
            limit = time.time() + field.get("sensitive_ttl_seconds", 120)
            until[field["key"]] = (
                min(limit, run["deadline"]) if run["deadline"] is not None else limit
            )
    step = _step(c, run)
    _write_payload(c, snapshot, step, {"input": clean, "sensitive_until": until})
    c.execute(
        "UPDATE task_flow_steps SET state='completed',completed_at=?,result_digest=? WHERE job_id=? AND flow_epoch=?",
        (time.time(), _receipt(clean, snapshot, step), row["id"], epoch),
    )
    effect = _activate(c, row, run, snapshot, _next(c, run, snapshot, node["next"]))
    return {
        **effect,
        "accepted": {
            "node_id": node["id"],
            "flow_epoch": epoch,
            "kind": "input",
            "fields": node["fields"],
            "values": clean,
        },
    }


def continue_display(c, row, epoch, revision):
    row, run, snapshot = _guard(c, row)
    _expect(run, epoch, revision)
    if run["phase"] != "display":
        fail("当前步骤不能继续", 409)
    expired = _expired(c, row, run, snapshot)
    if expired:
        return expired
    node = _nodes(snapshot)[run["node_id"]]
    c.execute(
        "UPDATE task_flow_steps SET state='completed',completed_at=? WHERE job_id=? AND flow_epoch=?",
        (time.time(), row["id"], epoch),
    )
    return _activate(c, row, run, snapshot, node["next"])


def _inputs(c, run, snapshot, node, *, consume=False):
    values = {}
    earliest = None
    writes = {}
    for key, reference in node["inputs"].items():
        value, source, payload = _source(c, run, snapshot, reference)
        field = _field(snapshot, reference)
        if field.get("sensitive"):
            if value == "" and not field.get("required", True):
                values[key] = value
                continue
            expiry = payload.get("sensitive_until", {}).get(reference["field"])
            used = payload.get("consumed_by", {}).get(reference["field"])
            if (
                expiry is None
                or time.time() >= expiry
                or used is not None
                and used != run["flow_epoch"]
            ):
                fail("敏感输入已过期或已使用，请重新填写", 409)
            earliest = expiry if earliest is None else min(earliest, expiry)
            if consume and used is None:
                pending = writes.setdefault(source["flow_epoch"], (source, payload))[1]
                pending.setdefault("consumed_by", {})[reference["field"]] = run[
                    "flow_epoch"
                ]
        values[key] = value
    # Validate all references before writing any consumption marker.
    for source, payload in writes.values():
        _write_payload(c, snapshot, source, payload)
    return values, earliest


def execution(c, row):
    if not is_flow(c, row):
        return None
    run = _run(c, row)
    if run is None or run["phase"] not in ("queued", "processing"):
        return None  # Safe terminal receipts never acquire processor authority.
    row, run, snapshot = _guard(c, row)
    if (
        run["phase"] not in ("queued", "processing")
        or run["deadline"] is not None
        and time.time() >= run["deadline"]
    ):
        return None
    node = _nodes(snapshot)[run["node_id"]]
    if node["kind"] != "process":
        return None
    step = _step(c, run)
    if step is None or step["kind"] != "process" or step["attempt"] != row["attempt"]:
        return None
    from fastapi import HTTPException

    try:
        params, input_expires_at = _inputs(
            c, run, snapshot, node, consume=run["phase"] == "processing"
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            return None
        raise
    parameters = []
    for key, reference in node["inputs"].items():
        parameters.append({**_field(snapshot, reference), "key": key})
    product = snapshot["product"]
    return {
        "job_id": row["id"],
        "card_id": row["card_id"],
        "product_id": row["product_id"],
        "shop_id": product["shop_id"],
        "attempt": row["attempt"],
        "flow_epoch": run["flow_epoch"],
        "node_id": node["id"],
        "action_id": step["action_id"],
        "mode": product["mode"],
        "params": params,
        "parameters": parameters,
        "outputs": node["outputs"],
        "deadline": run["deadline"],
        "input_expires_at": input_expires_at,
        "processor_id": product.get("processor_id", ""),
        "webhook_url": product.get("webhook_url", ""),
        "webhook_secret": product.get("webhook_secret", ""),
    }


def frozen_authority(c, row, epoch, attempt=None):
    row, run, snapshot = _guard(c, row)
    if type(epoch) is not int:
        fail("任务流程轮次无效", 422)
    step = _step(c, run, epoch)
    if (
        step is None
        or step["kind"] != "process"
        or attempt is not None
        and step["attempt"] != attempt
    ):
        fail("处理步骤不存在或不匹配", 409)
    product = snapshot["product"]
    return {
        "job_id": row["id"],
        "card_id": row["card_id"],
        "product_id": row["product_id"],
        "shop_id": product["shop_id"],
        "attempt": step["attempt"],
        "flow_epoch": epoch,
        "node_id": step["node_id"],
        "action_id": step["action_id"],
        "mode": product["mode"],
        "webhook_url": product.get("webhook_url", ""),
        "webhook_secret": product.get("webhook_secret", ""),
        "deadline": step["deadline"],
        "step_state": step["state"],
    }


def claim(c, row, actor, epoch):
    row, run, snapshot = _guard(c, row)
    _expect(run, epoch)
    if run["phase"] != "queued" or row["state"] != "queued":
        fail("处理步骤已被领取或不在队列", 409)
    expired = _expired(c, row, run, snapshot)
    if expired:
        return expired
    if execution(c, row) is None:
        fail("处理步骤输入已失效", 409)
    lease = (
        min(time.time() + 3600, run["deadline"])
        if run["deadline"] is not None
        else time.time() + 3600
    )
    _mutate_run(c, run, phase="processing")
    c.execute(
        "UPDATE jobs SET state='processing',claimed_by=?,lease=?,updated=? WHERE id=? AND state='queued'",
        (actor, lease, time.time(), row["id"]),
    )
    return _effect(c, row)


def release_claim(c, row, actor, epoch):
    row, run, snapshot = _guard(c, row)
    _expect(run, epoch)
    if run["phase"] != "processing" or row["claimed_by"] != actor:
        fail("处理步骤不属于当前处理者", 409)
    expired = _expired(c, row, run, snapshot)
    if expired:
        return expired
    _mutate_run(c, run, phase="queued")
    c.execute(
        "UPDATE jobs SET state='queued',claimed_by=NULL,lease=NULL,updated=? WHERE id=?",
        (time.time(), row["id"]),
    )
    return _effect(c, row)


def process_update(c, row, update, epoch, actor=None):
    row, run, snapshot = _guard(c, row)
    supplied = update.model_dump() if hasattr(update, "model_dump") else dict(update)
    if supplied.get("attempt") != row["attempt"]:
        fail("回调对应的尝试已失效", 409)
    _expect(run, epoch)
    if run["phase"] not in ("queued", "processing") or row["state"] not in (
        "queued",
        "processing",
    ):
        fail("处理步骤已经结束", 409)
    if snapshot["product"]["mode"] == "manual" and (
        run["phase"] != "processing" or actor is None or row["claimed_by"] != actor
    ):
        fail("请先领取且只能处理自己的步骤", 409)
    expired = _expired(c, row, run, snapshot)
    if expired:
        return expired
    if snapshot["product"]["mode"] == "manual" and (
        row["lease"] is None or time.time() >= row["lease"]
    ):
        fail("处理步骤租约已过期，请重新领取", 409)
    node = _nodes(snapshot)[run["node_id"]]
    state = supplied.get("state")
    if state not in ("processing", "succeeded", "failed"):
        fail("处理步骤状态无效", 422)
    if state == "processing":
        if supplied.get("output") or supplied.get("content") is not None:
            fail("只有步骤完成可以包含结果", 422)
        progress = supplied.get("progress", 0)
        if (
            type(progress) is not int
            or not 0 <= progress <= 99
            or progress < row["progress"]
        ):
            fail("步骤进度无效或倒退", 409)
        _mutate_run(c, run, phase="processing")
        c.execute(
            "UPDATE jobs SET state='processing',progress=?,message=?,updated=? WHERE id=?",
            (progress, supplied.get("message", ""), time.time(), row["id"]),
        )
        return _effect(c, row)
    step = _step(c, run)
    values = supplied.get("output") or {}
    if supplied.get("content") is not None:
        if len(node["outputs"]) != 1 or node["outputs"][0]["key"] != "content":
            fail("请按步骤定义提交结果字段", 422)
        if values and values.get("content") != supplied["content"]:
            fail("步骤内容与结果字段不一致", 422)
        values = {"content": supplied["content"]}
    if state == "failed" and values:
        fail("失败步骤不能包含结果", 422)
    clean = (
        _values(node["outputs"], values, output=True) if state == "succeeded" else {}
    )
    _write_payload(
        c, snapshot, step, {"output": clean, "message": supplied.get("message", "")}
    )
    c.execute(
        "UPDATE task_flow_steps SET state=?,completed_at=?,result_digest=? WHERE job_id=? AND flow_epoch=?",
        (
            "completed" if state == "succeeded" else "failed",
            time.time(),
            _receipt(supplied, snapshot, step),
            row["id"],
            epoch,
        ),
    )
    _clear_sensitive(c, run, snapshot, node)
    if state == "failed" and not supplied.get("retryable", False):
        _mutate_run(c, run, phase="ended", deadline=None)
        return _effect(
            c,
            row,
            terminal={
                "state": "failed",
                "output": {},
                "result_sources": {},
                "message": supplied.get("message") or {"zh-CN": "处理失败，待商家核实"},
                "retryable": False,
                "needs_review": True,
            },
        )
    target = (
        _next(c, run, snapshot, node["next"])
        if state == "succeeded"
        else node["failure_next"]
    )
    effect = _activate(c, row, run, snapshot, target)
    if state == "succeeded":
        effect["accepted"] = {
            "node_id": node["id"],
            "flow_epoch": epoch,
            "kind": "output",
            "fields": node["outputs"],
            "values": clean,
        }
    return effect


def expire_due(c, now=None, limit=100):
    now = time.time() if now is None else now
    effects = []
    rows = c.execute(
        "SELECT jobs.* FROM jobs JOIN task_flow_runs ON task_flow_runs.job_id=jobs.id "
        "WHERE task_flow_runs.phase IN ('await_start','input','display','queued','processing') "
        "AND ((task_flow_runs.deadline IS NOT NULL AND task_flow_runs.deadline<=?) "
        "OR (task_flow_runs.input_expires_at IS NOT NULL AND task_flow_runs.input_expires_at<=?)) "
        "ORDER BY COALESCE(task_flow_runs.input_expires_at,task_flow_runs.deadline),jobs.id LIMIT ?",
        (now, now, max(1, min(int(limit), 1000))),
    ).fetchall()
    from fastapi import HTTPException

    for row in rows:
        run = _run(c, row)
        ttl_due = run["input_expires_at"] is not None and run["input_expires_at"] <= now
        # Retention cleanup is not permission to execute work. Even a disabled
        # shop or revoked card must not keep expired raw secrets indefinitely.
        remaining_expiry = (
            _sensitive_expiry(
                c, run, card_snapshot(c, row["card_id"]), erase=True, now=now
            )
            if ttl_due
            else None
        )
        try:
            current, run, snapshot = _guard(c, row)
        except HTTPException:
            if ttl_due:
                _mutate_run(c, run, input_expires_at=remaining_expiry)
            continue
        if run["deadline"] is not None and run["deadline"] <= now:
            effects.append(_timeout(c, current, run, snapshot))
        elif run["input_expires_at"] is not None and run["input_expires_at"] <= now:
            node = _nodes(snapshot)[run["node_id"]]
            if run["phase"] == "queued":
                sources = _sensitive_sources(c, run, snapshot, node)
                invalid = next(
                    (
                        source
                        for source in sources
                        if not source["valid"] or source["expiry"] <= now
                    ),
                    None,
                )
                if invalid is None:
                    _mutate_run(c, run, input_expires_at=remaining_expiry)
                    effects.append(_effect(c, current, input_expired=True))
                    continue
                c.execute(
                    "UPDATE task_flow_steps SET state='cancelled',completed_at=? WHERE job_id=? AND flow_epoch=?",
                    (now, current["id"], run["flow_epoch"]),
                )
                _clear_sensitive(c, run, snapshot, node)
                effect = _activate(
                    c, current, run, snapshot, invalid["node"], initial=True
                )
                c.execute(
                    "UPDATE jobs SET message=? WHERE id=?",
                    ("敏感输入已过期，请重新填写", current["id"]),
                )
                effects.append({**effect, "input_expired": True})
            else:
                # A worker may already have started an external action. Erase
                # expired values without restarting it or changing its timer.
                # Intermediate user steps also keep their original deadline.
                _mutate_run(c, run, input_expires_at=remaining_expiry)
                effects.append(_effect(c, current, input_expired=True))
    return effects


def reset_attempt(c, row):
    row = _job(c, row)
    snapshot = card_snapshot(c, row["card_id"])
    run = _run(c, row)
    if snapshot is None or run is None:
        fail("任务流程不存在", 409)
    if row["attempt"] <= run["attempt"]:
        fail("任务流程只能随整次重试更新尝试次数", 409)
    _clear_sensitive(c, run, snapshot)
    c.execute(
        "UPDATE task_flow_steps SET state='cancelled',completed_at=? WHERE job_id=? AND state='active'",
        (time.time(), row["id"]),
    )
    run = _mutate_run(
        c, run, attempt=row["attempt"], phase="initializing", deadline=None
    )
    return _activate(
        c, row, run, snapshot, snapshot["definition"]["entry"], initial=True
    )


def destroy(c, row):
    row = _job(c, row)
    snapshot = card_snapshot(c, row["card_id"])
    run = _run(c, row)
    if snapshot is None or run is None:
        return None
    for step in c.execute(
        "SELECT * FROM task_flow_steps WHERE job_id=?", (row["id"],)
    ).fetchall():
        _write_payload(c, snapshot, step, {})
    _mutate_run(c, run, phase="ended", deadline=None)
    return _effect(c, row)


def stop_for_outcome(c, row):
    """Invalidate work after the trusted legacy retry/reject outcome adapter."""
    row = _job(c, row)
    snapshot = card_snapshot(c, row["card_id"])
    run = _run(c, row)
    if snapshot is None or run is None:
        return {"row": row, "flow": None}
    if row["state"] not in ("needs_input", "rejected", "failed"):
        fail("只有重试、拒绝或失败结果可以停止流程", 409)
    for step in c.execute(
        "SELECT * FROM task_flow_steps WHERE job_id=?", (row["id"],)
    ).fetchall():
        _write_payload(c, snapshot, step, {})
    c.execute(
        "UPDATE task_flow_steps SET state='cancelled',completed_at=? "
        "WHERE job_id=? AND state='active'",
        (time.time(), row["id"]),
    )
    _mutate_run(c, run, flow_epoch=run["flow_epoch"] + 1, phase="ended", deadline=None)
    return _effect(c, row)


def cancel(c, row, epoch, revision):
    row, run, snapshot = _guard(c, row)
    _expect(run, epoch, revision)
    if run["phase"] == "ended":
        fail("已经结束的任务不能取消", 409)
    uncertain = run["phase"] == "processing"
    c.execute(
        "UPDATE task_flow_steps SET state='cancelled',completed_at=? "
        "WHERE job_id=? AND state='active'",
        (time.time(), row["id"]),
    )
    for step in c.execute(
        "SELECT * FROM task_flow_steps WHERE job_id=?", (row["id"],)
    ).fetchall():
        _write_payload(c, snapshot, step, {})
    _mutate_run(c, run, flow_epoch=run["flow_epoch"] + 1, phase="ended", deadline=None)
    return _effect(
        c,
        row,
        terminal={
            "state": "failed",
            "output": {},
            "result_sources": {},
            "message": {"zh-CN": "顾客已取消任务"},
            "retryable": not uncertain,
            "needs_review": uncertain,
        },
    )


def stage_values(c, row, epoch, kind):
    row, run, snapshot = _guard(c, row)
    step = _step(c, run, epoch)
    if step is None or step["attempt"] != row["attempt"]:
        fail("附件步骤轮次已失效", 409)
    if kind not in ("input", "output"):
        fail("步骤数据类别无效", 422)
    node = _nodes(snapshot)[step["node_id"]]
    fields = node.get("fields", []) if kind == "input" else node.get("outputs", [])
    return {
        "node_id": step["node_id"],
        "flow_epoch": epoch,
        "kind": kind,
        "fields": fields,
        "values": _payload(c, snapshot, step).get(kind, {}),
    }

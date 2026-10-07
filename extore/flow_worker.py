"""Trusted execution and encrypted delivery for issued task-flow process stages.

Network calls and the existing processor sandbox run outside SQLite transactions.
The normal events/outbox tables never receive the private execution envelope.
"""

import json
import time
import uuid

from fastapi import HTTPException

from . import task_flow
from .db import db
from .models import JobUpdate
from .secret_store import MAX_SECRET_BYTES, SecretStoreError, open_secret, store_secret
from .security import fail
from .storage import check_storage_quota

MAX_CARD_CIPHERTEXT_BYTES = 2 * 1024 * 1024
MAX_DELIVERY_ATTEMPTS = 8
DELIVERY_LEASE_SECONDS = 60
_PRIVATE_FIELDS = (
    "shop_id",
    "product_id",
    "job_id",
    "attempt",
    "node_id",
    "flow_epoch",
    "action_id",
    "mode",
    "webhook_url",
    "webhook_secret",
    "params",
    "parameters",
    "outputs",
    "deadline",
    "input_expires_at",
)


class DispatchInvalid(ValueError):
    """A stale, revoked, or expired private dispatch; contains no private data."""


def init_schema(c):
    c.execute(
        "CREATE TABLE IF NOT EXISTS task_flow_dispatches ("
        "id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,"
        "product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,"
        "shop_id TEXT NOT NULL REFERENCES shops(id),attempt INTEGER NOT NULL,"
        "flow_epoch INTEGER NOT NULL,node_id TEXT NOT NULL,action_id TEXT NOT NULL UNIQUE,"
        "state TEXT NOT NULL,payload_ciphertext TEXT NOT NULL,ciphertext_bytes INTEGER NOT NULL,"
        "payload_digest TEXT NOT NULL,deadline REAL,input_expires_at REAL,due REAL NOT NULL,"
        "attempts INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL,"
        "finished_at REAL,error TEXT NOT NULL DEFAULT '')"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS task_flow_dispatch_due ON task_flow_dispatches(state,due)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS task_flow_dispatch_job ON task_flow_dispatches(job_id,attempt,flow_epoch)"
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
        fail("流程回调数据格式无效", 422)
    if len(raw) > MAX_SECRET_BYTES:
        fail("流程回调内容过大，请使用附件", 413)
    return raw


def _context(execution):
    return {key: execution.get(key) for key in _PRIVATE_FIELDS}


def _public(row):
    return {
        key: row[key]
        for key in (
            "id",
            "job_id",
            "product_id",
            "shop_id",
            "attempt",
            "flow_epoch",
            "node_id",
            "action_id",
            "state",
            "attempts",
            "due",
            "deadline",
            "input_expires_at",
            "created",
            "updated",
            "finished_at",
            "error",
        )
    }


def _finish(c, row, state, *, attempts=None, error=""):
    now = time.time()
    c.execute(
        "UPDATE task_flow_dispatches SET state=?,payload_ciphertext='',ciphertext_bytes=0,"
        "attempts=?,error=?,updated=?,finished_at=? WHERE id=?",
        (
            state,
            row["attempts"] if attempts is None else attempts,
            error,
            now,
            now,
            row["id"],
        ),
    )


def _cancel_other(c, job_id, action_id=None):
    rows = c.execute(
        "SELECT * FROM task_flow_dispatches WHERE job_id=? AND state IN ('pending','sending')",
        (job_id,),
    ).fetchall()
    for row in rows:
        if row["action_id"] != action_id:
            _finish(c, row, "cancelled")


def _cancel_stale(c):
    # Sweep independently of retry due dates: expired secrets must not remain
    # encrypted until a possibly hour-long transport backoff finishes.
    rows = c.execute(
        "SELECT * FROM task_flow_dispatches WHERE state IN ('pending','sending') "
        "ORDER BY CASE WHEN (deadline IS NOT NULL AND deadline<=?) OR "
        "(input_expires_at IS NOT NULL AND input_expires_at<=?) THEN 0 ELSE 1 END,updated,id LIMIT 100",
        (time.time(), time.time()),
    ).fetchall()
    cancelled = False
    for row in rows:
        try:
            _validated_context(c, row)
        except DispatchInvalid:
            _finish(c, row, "cancelled")
            cancelled = True
        else:
            c.execute(
                "UPDATE task_flow_dispatches SET updated=? WHERE id=?",
                (time.time(), row["id"]),
            )
    return cancelled


def _card_ciphertext_bytes(c, card_id):
    frozen = c.execute(
        "SELECT COALESCE(SUM(LENGTH(CAST(snapshot_ciphertext AS BLOB))),0) FROM card_task_flows WHERE card_id=?",
        (card_id,),
    ).fetchone()[0]
    steps = c.execute(
        "SELECT COALESCE(SUM(LENGTH(CAST(task_flow_steps.payload_ciphertext AS BLOB))),0) "
        "FROM task_flow_steps JOIN jobs ON jobs.id=task_flow_steps.job_id WHERE jobs.card_id=?",
        (card_id,),
    ).fetchone()[0]
    dispatches = c.execute(
        "SELECT COALESCE(SUM(task_flow_dispatches.ciphertext_bytes),0) FROM task_flow_dispatches "
        "JOIN jobs ON jobs.id=task_flow_dispatches.job_id WHERE jobs.card_id=?",
        (card_id,),
    ).fetchone()[0]
    return frozen + steps + dispatches


def _active_execution(c, row):
    try:
        execution = task_flow.execution(c, row)
    except (HTTPException, SecretStoreError, ValueError, KeyError, TypeError):
        return None
    if execution is None or execution["mode"] != "webhook":
        return None
    now = time.time()
    if any(
        execution.get(key) is not None and now >= execution[key]
        for key in ("deadline", "input_expires_at")
    ):
        return None
    current = c.execute(
        "SELECT products.config,products.shop_id,shops.enabled FROM products "
        "JOIN shops ON shops.id=products.shop_id WHERE products.id=?",
        (row["product_id"],),
    ).fetchone()
    if (
        current is None
        or not current["enabled"]
        or current["shop_id"] != execution["shop_id"]
    ):
        return None
    try:
        product = json.loads(current["config"])
    except (ValueError, TypeError):
        return None
    if any(
        product.get(key) != execution.get(key)
        for key in ("mode", "webhook_url", "webhook_secret")
    ):
        return None
    return execution


def sync_dispatch(c, row):
    """Synchronize one current webhook action in the caller's write transaction."""
    execution = _active_execution(c, row)
    if execution is None:
        _cancel_other(c, row["id"])
        return None
    _cancel_other(c, row["id"], execution["action_id"])
    existing = c.execute(
        "SELECT * FROM task_flow_dispatches WHERE action_id=?",
        (execution["action_id"],),
    ).fetchone()
    if existing is not None:
        # Sent/dead/cancelled actions are never implicitly resubmitted.
        return _public(existing)
    context = _context(execution)
    _json(context)
    identity = str(uuid.uuid4())
    digest = task_flow.fingerprint(context, execution["shop_id"], identity)
    ciphertext = store_secret(
        context,
        tenant_id=execution["shop_id"],
        resource_type="task-flow-dispatch",
        resource_id=identity,
    )
    size = len(ciphertext.encode("utf-8"))
    if _card_ciphertext_bytes(c, row["card_id"]) + size > MAX_CARD_CIPHERTEXT_BYTES:
        fail("此卡密的流程存储额度已满，请减少文本并使用附件", 413)
    check_storage_quota(c, size, product_id=row["product_id"])
    now = time.time()
    c.execute(
        "INSERT INTO task_flow_dispatches "
        "(id,job_id,product_id,shop_id,attempt,flow_epoch,node_id,action_id,state,payload_ciphertext,"
        "ciphertext_bytes,payload_digest,deadline,input_expires_at,due,created,updated) "
        "VALUES (?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,?)",
        (
            identity,
            row["id"],
            row["product_id"],
            execution["shop_id"],
            execution["attempt"],
            execution["flow_epoch"],
            execution["node_id"],
            execution["action_id"],
            ciphertext,
            size,
            digest,
            execution["deadline"],
            execution.get("input_expires_at"),
            now,
            now,
            now,
        ),
    )
    return _public(
        c.execute(
            "SELECT * FROM task_flow_dispatches WHERE id=?", (identity,)
        ).fetchone()
    )


def _validated_context(c, dispatch, *, state=None):
    if (
        state is not None
        and dispatch["state"] != state
        or not dispatch["payload_ciphertext"]
    ):
        raise DispatchInvalid("流程回调已失效")
    row = c.execute("SELECT * FROM jobs WHERE id=?", (dispatch["job_id"],)).fetchone()
    if row is None:
        raise DispatchInvalid("流程回调已失效")
    execution = _active_execution(c, row)
    if execution is None:
        raise DispatchInvalid("流程回调已失效")
    try:
        context = open_secret(
            dispatch["payload_ciphertext"],
            tenant_id=dispatch["shop_id"],
            resource_type="task-flow-dispatch",
            resource_id=dispatch["id"],
        )
    except SecretStoreError:
        raise DispatchInvalid("流程回调已失效") from None
    if not isinstance(context, dict) or context != _context(execution):
        raise DispatchInvalid("流程回调已失效")
    if any(
        context.get(key) != dispatch[key]
        for key in (
            "shop_id",
            "product_id",
            "job_id",
            "attempt",
            "node_id",
            "flow_epoch",
            "action_id",
            "deadline",
            "input_expires_at",
        )
    ):
        raise DispatchInvalid("流程回调已失效")
    if (
        task_flow.fingerprint(context, dispatch["shop_id"], dispatch["id"])
        != dispatch["payload_digest"]
    ):
        raise DispatchInvalid("流程回调已失效")
    from .card_entitlements import job_context
    from .service import job
    from .work_instructions import instructions

    # Keep the encrypted execution/fingerprint unchanged for pending historical
    # dispatches. Attach live merchant configuration only after scope validation.
    try:
        context["instructions"] = instructions(c, dispatch["product_id"])
        context.update(
            job_context(c, job(c, dispatch["job_id"]), include_deliveries=False)
        )
    except HTTPException:
        raise DispatchInvalid("流程回调已失效") from None
    return context


async def _deliver(context, *, recheck):
    from .private_worker import deliver_v2

    await deliver_v2(context, recheck=recheck)


async def outbox_once():
    """Send one encrypted action with a final, transaction-free network boundary."""
    with db() as c:
        dispatch = c.execute(
            "SELECT * FROM task_flow_dispatches WHERE state IN ('pending','sending') AND due<=? "
            "ORDER BY due,id LIMIT 1",
            (time.time(),),
        ).fetchone()
        if dispatch is None:
            return False
        dispatch = dict(dispatch)
        if dispatch["attempts"] >= MAX_DELIVERY_ATTEMPTS:
            _finish(c, dispatch, "dead", error="delivery_failed")
            return True
        try:
            context = _validated_context(c, dispatch)
        except DispatchInvalid:
            _finish(c, dispatch, "cancelled")
            return True
        c.execute(
            "UPDATE task_flow_dispatches SET state='sending',due=?,updated=? WHERE id=?",
            (time.time() + DELIVERY_LEASE_SECONDS, time.time(), dispatch["id"]),
        )

    def recheck():
        with db() as c:
            current = c.execute(
                "SELECT * FROM task_flow_dispatches WHERE id=?", (dispatch["id"],)
            ).fetchone()
            if current is None:
                raise DispatchInvalid("流程回调已失效")
            _validated_context(c, current, state="sending")

    try:
        await _deliver(context, recheck=recheck)
    except Exception as exc:
        with db() as c:
            current = c.execute(
                "SELECT * FROM task_flow_dispatches WHERE id=?", (dispatch["id"],)
            ).fetchone()
            if current is None or current["state"] != "sending":
                return True
            attempts = current["attempts"] + 1
            try:
                if isinstance(exc, DispatchInvalid):
                    raise exc
                _validated_context(c, current, state="sending")
            except DispatchInvalid:
                _finish(c, current, "cancelled", attempts=attempts)
            else:
                if attempts >= MAX_DELIVERY_ATTEMPTS:
                    _finish(
                        c, current, "dead", attempts=attempts, error="delivery_failed"
                    )
                else:
                    now = time.time()
                    c.execute(
                        "UPDATE task_flow_dispatches SET state='pending',attempts=?,due=?,updated=?,error='delivery_failed' WHERE id=?",
                        (
                            attempts,
                            now + min(3600, 2**attempts * 5),
                            now,
                            current["id"],
                        ),
                    )
    else:
        with db() as c:
            current = c.execute(
                "SELECT * FROM task_flow_dispatches WHERE id=?", (dispatch["id"],)
            ).fetchone()
            if current is not None and current["state"] == "sending":
                _finish(c, current, "sent", attempts=current["attempts"] + 1)
    return True


def _finalize(c, effect):
    from .service import finalize_task_flow

    return finalize_task_flow(c, effect)


def _script_authority(c, row, selected, epoch):
    # An action already launched while its input was valid may finish after a
    # sensitive value was wiped. Completion is not another use of that input.
    run = c.execute(
        "SELECT * FROM task_flow_runs WHERE job_id=?", (row["id"],)
    ).fetchone()
    if (
        run is None
        or run["flow_epoch"] != epoch
        or run["phase"] != "processing"
        or run["attempt"] != selected["attempt"]
        or row["attempt"] != selected["attempt"]
        or row["state"] != "processing"
        or row["claimed_by"] != "worker"
    ):
        return False
    try:
        authority = task_flow.frozen_authority(c, row, epoch, selected["attempt"])
        from .processor_profiles import runtime_execution

        runtime_execution(c, row, selected["processor_id"])
    except (HTTPException, SecretStoreError, ValueError):
        return False
    return bool(
        authority["mode"] == "script"
        and authority["action_id"] == selected["action_id"]
        and authority["step_state"] == "active"
        and (authority["deadline"] is None or time.time() < authority["deadline"])
    )


async def task_flow_once():
    """Expire stages, enqueue webhook actions, and run at most one script stage."""
    selected, frozen_product, epoch = None, None, None
    busy = False
    with db() as c:
        busy |= _cancel_stale(c)
        for effect in task_flow.expire_due(c):
            _finalize(c, effect)
            busy = True
        rows = c.execute(
            "SELECT jobs.* FROM jobs JOIN task_flow_runs ON task_flow_runs.job_id=jobs.id "
            "WHERE task_flow_runs.phase IN ('queued','processing') AND jobs.state IN ('queued','processing') "
            "ORDER BY CASE WHEN jobs.state='queued' AND "
            "json_extract((SELECT config FROM products WHERE id=jobs.product_id),'$.mode')='script' "
            "THEN 0 ELSE 1 END,jobs.created,jobs.id LIMIT 100"
        ).fetchall()
        for row in rows:
            try:
                execution = task_flow.execution(c, row)
            except HTTPException:
                _cancel_other(c, row["id"])
                continue
            if execution is None:
                _cancel_other(c, row["id"])
                continue
            if execution["mode"] == "webhook":
                before = c.total_changes
                sync_dispatch(c, row)
                busy |= c.total_changes != before
                continue
            if (
                execution["mode"] != "script"
                or row["state"] != "queued"
                or selected is not None
            ):
                continue
            epoch = execution["flow_epoch"]
            effect = task_flow.claim(c, row, "worker", epoch)
            _finalize(c, effect)
            busy = True
            current = c.execute(
                "SELECT * FROM jobs WHERE id=?", (row["id"],)
            ).fetchone()
            fresh = task_flow.execution(c, current)
            if (
                fresh is None
                or fresh["flow_epoch"] != epoch
                or current["state"] != "processing"
                or current["claimed_by"] != "worker"
            ):
                continue
            selected = {
                **dict(current),
                **fresh,
                "id": current["id"],
                "params": _json(fresh["params"]).decode("utf-8"),
            }
            frozen_product = task_flow.card_snapshot(c, current["card_id"])["product"]
    if selected is None:
        return busy
    try:
        from .worker import execute_script

        result = await execute_script(selected, frozen_product, task_flow_epoch=epoch)
    except Exception:
        result = JobUpdate(
            state="failed",
            attempt=selected["attempt"],
            retryable=False,
            message="处理器未正常完成，待商家核实是否已交付",
        )
    with db() as c:
        current = c.execute(
            "SELECT * FROM jobs WHERE id=?", (selected["id"],)
        ).fetchone()
        if current is None:
            return True
        try:
            if not _script_authority(c, current, selected, epoch):
                return True
            c.execute("SAVEPOINT flow_script_result")
            try:
                effect = task_flow.process_update(
                    c, current, result, epoch, actor="worker"
                )
                _finalize(c, effect)
            except Exception:
                c.execute("ROLLBACK TO flow_script_result")
                c.execute("RELEASE flow_script_result")
                raise
            else:
                c.execute("RELEASE flow_script_result")
        except (HTTPException, ValueError):
            fresh = c.execute(
                "SELECT * FROM jobs WHERE id=?", (selected["id"],)
            ).fetchone()
            if fresh is not None and _script_authority(c, fresh, selected, epoch):
                effect = task_flow.process_update(
                    c,
                    fresh,
                    JobUpdate(
                        state="failed",
                        attempt=selected["attempt"],
                        retryable=False,
                        message="处理器返回的结果不符合步骤定义，待商家核实",
                    ),
                    epoch,
                    actor="worker",
                )
                _finalize(c, effect)
    return True

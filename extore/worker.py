import asyncio
import fcntl
import ipaddress
import json
import socket
import time
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

from .config import DATA
from .db import db, init
from .models import JobUpdate, ProgressStep
from .processors import normalize_product
from .security import sign, token
from .service import apply_update, bootstrap_progress_plan, job, product, progress_view
from .variants import card_variant


def _has_table(c, name):
    return (
        c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _flow_exclusion(c):
    # Compatibility with existing databases/tests before the additive migration.
    if not _has_table(c, "card_task_flows"):
        return ""
    return " AND NOT EXISTS (SELECT 1 FROM card_task_flows WHERE card_task_flows.card_id=jobs.card_id)"


def _check_script_execution(c, row, processor_id, *, require_input=False):
    """Recheck live authority without putting private configuration in a task."""
    current = job(c, row["id"])
    if (
        current["product_id"] != row["product_id"]
        or current["card_id"] != row["card_id"]
        or current["attempt"] != row["attempt"]
        or current["state"] != "processing"
        or current["lease"] is not None
        and time.time() >= current["lease"]
    ):
        raise ValueError("处理任务已失效")
    binding = c.execute(
        "SELECT binding.*,profiles.disabled,profiles.shop_id AS profile_shop_id,"
        "profiles.processor_id AS profile_processor_id,shops.enabled,products.config,"
        "products.shop_id AS product_shop_id,cards.state AS card_state "
        "FROM processor_card_bindings binding "
        "JOIN processor_profiles profiles ON profiles.id=binding.profile_id "
        "JOIN cards ON cards.id=binding.card_id "
        "JOIN products ON products.id=cards.product_id "
        "JOIN shops ON shops.id=products.shop_id WHERE binding.card_id=?",
        (current["card_id"],),
    ).fetchone()
    context = row["shop_context"]
    if (
        binding is None
        or not binding["enabled"]
        or binding["disabled"]
        or binding["card_state"] in ("revoked", "rejected")
        or binding["product_id"] != current["product_id"]
        or any(
            binding[key] != context["shop_id"]
            for key in ("shop_id", "profile_shop_id", "product_shop_id")
        )
        or binding["profile_id"] != context["profile_id"]
        or binding["revision"] != context["revision"]
        or binding["processor_id"] != processor_id
        or binding["profile_processor_id"] != processor_id
    ):
        raise ValueError("商品处理器授权已失效")
    configured = json.loads(binding["config"])
    if (
        configured.get("mode") != "script"
        or configured.get("processor_id") != processor_id
    ):
        raise ValueError("商品处理器授权已失效")
    epoch = row.get("flow_epoch")
    if epoch is None:
        return current
    from . import task_flow

    run = c.execute(
        "SELECT * FROM task_flow_runs WHERE job_id=?", (current["id"],)
    ).fetchone()
    authority = task_flow.frozen_authority(c, current, epoch, row["attempt"])
    if (
        run is None
        or run["flow_epoch"] != epoch
        or run["phase"] != "processing"
        or run["attempt"] != row["attempt"]
        or current["claimed_by"] != "worker"
        or authority["mode"] != "script"
        or authority["action_id"] != row["action_id"]
        or authority["step_state"] != "active"
        or authority["deadline"] is not None
        and time.time() >= authority["deadline"]
    ):
        raise ValueError("处理步骤已失效")
    if require_input:
        execution = task_flow.execution(c, current)
        if (
            execution is None
            or execution["flow_epoch"] != epoch
            or execution["action_id"] != row["action_id"]
            or execution["processor_id"] != processor_id
        ):
            raise ValueError("处理步骤输入已失效")
    # After launch, a valid action can complete after its input's retention TTL.
    # Checking frozen authority does not read or consume the cleared input again.
    return current


def _check_event_authority(row):
    if row.get("id"):
        with db() as c:
            enabled = c.execute(
                "SELECT shops.enabled FROM events JOIN products ON products.id=events.product_id JOIN shops ON shops.id=products.shop_id WHERE events.id=?",
                (row["id"],),
            ).fetchone()
            if enabled is None or not enabled["enabled"]:
                raise ValueError("店铺已停用，不能发起处理")


async def deliver_event(row):
    _check_event_authority(row)
    u = urlsplit(row["url"])
    # Pin the validated IP, retaining original Host and TLS SNI; no DNS rebinding.
    answers = await asyncio.to_thread(
        socket.getaddrinfo, u.hostname, u.port or 443, type=socket.SOCK_STREAM
    )
    _check_event_authority(row)
    addresses = {a[4][0] for a in answers}
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise ValueError("Webhook 地址必须解析到公网 IP")
    address = sorted(addresses)[0]
    netloc = f"[{address}]" if ":" in address else address
    if u.port:
        netloc += f":{u.port}"
    url = u._replace(netloc=netloc).geturl()
    body = row["payload"].encode()
    ts = str(int(time.time()))
    nonce = token()
    host = u.hostname + (f":{u.port}" if u.port and u.port != 443 else "")
    async with httpx.AsyncClient(
        timeout=15, follow_redirects=False, trust_env=False
    ) as client:
        _check_event_authority(row)
        async with client.stream(
            "POST",
            url,
            content=body,
            headers={
                "Host": host,
                "Content-Type": "application/json",
                "X-Extore-Timestamp": ts,
                "X-Extore-Nonce": nonce,
                "X-Extore-Signature": sign(row["secret"], ts, nonce, body),
                "X-Extore-Event-Id": row["id"],
            },
            extensions={"sni_hostname": u.hostname.encode("idna").decode()},
        ) as response:
            if not 200 <= response.status_code < 300:
                raise ValueError(f"HTTP {response.status_code}")


async def outbox_once():
    with db() as c:
        row = c.execute(
            "SELECT outbox.*,events.payload FROM outbox JOIN events ON events.id=outbox.id WHERE state='pending' AND due<=? ORDER BY due LIMIT 1",
            (time.time(),),
        ).fetchone()
        if not row:
            return False
        row = dict(row)
        payload = json.loads(row["payload"])
        shop = c.execute(
            "SELECT shops.enabled FROM products JOIN shops ON shops.id=products.shop_id WHERE products.id=?",
            (payload["product_id"],),
        ).fetchone()
        if shop is None or not shop["enabled"]:
            c.execute(
                "UPDATE outbox SET state='cancelled',finished_at=? WHERE id=?",
                (time.time(), row["id"]),
            )
            return True
        if payload["type"] == "redemption.requested":
            r = c.execute(
                "SELECT state,attempt,card_id FROM jobs WHERE id=?",
                (payload["data"]["id"],),
            ).fetchone()
            if (
                not r
                or r["state"] not in ("queued", "processing")
                or r["attempt"] != payload["data"]["attempt"]
                or _has_table(c, "card_task_flows")
                and c.execute(
                    "SELECT 1 FROM card_task_flows WHERE card_id=?", (r["card_id"],)
                ).fetchone()
                is not None
            ):
                c.execute(
                    "UPDATE outbox SET state='cancelled',finished_at=? WHERE id=?",
                    (time.time(), row["id"]),
                )
                return True
    try:
        await deliver_event(row)
        with db() as c:
            c.execute(
                "UPDATE outbox SET state='delivered',attempts=attempts+1,error='',finished_at=? WHERE id=?",
                (time.time(), row["id"]),
            )
    except Exception as exc:
        attempts = row["attempts"] + 1
        # Never retain response bodies or network exception strings containing secrets.
        error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        with db() as c:
            c.execute(
                "UPDATE outbox SET state=?,attempts=?,due=?,error=?,finished_at=? WHERE id=?",
                (
                    "dead" if attempts >= 8 else "pending",
                    attempts,
                    time.time() + min(3600, 2**attempts * 5),
                    error,
                    time.time() if attempts >= 8 else None,
                    row["id"],
                ),
            )
    return True


async def execute_script(row, p, task_flow_epoch=None):
    # A product selects a catalog ID, never an arbitrary executable or filename.
    import extore_processors

    p = normalize_product(p)
    row = dict(row)
    # Trust the database's issuance binding, never caller/customer metadata or
    # the product's current account selection for an already-issued card.
    from .processor_profiles import runtime_execution

    with db() as c:
        trusted_job = job(c, row["id"])
        if (
            trusted_job["product_id"] != row["product_id"]
            or trusted_job["attempt"] != row["attempt"]
            or trusted_job["state"] != "processing"
        ):
            raise ValueError("处理任务已失效")
        is_flow = (
            _has_table(c, "card_task_flows")
            and c.execute(
                "SELECT 1 FROM card_task_flows WHERE card_id=?",
                (trusted_job["card_id"],),
            ).fetchone()
            is not None
        )
        if is_flow:
            from . import task_flow

            execution = task_flow.execution(c, trusted_job)
            if (
                type(task_flow_epoch) is not int
                or execution is None
                or execution["flow_epoch"] != task_flow_epoch
                or execution["mode"] != "script"
                or execution["processor_id"] != p["processor_id"]
                or trusted_job["claimed_by"] != "worker"
            ):
                raise ValueError("处理步骤已失效")
            row["flow_epoch"] = task_flow_epoch
            row["action_id"] = execution["action_id"]
            row["params"] = json.dumps(execution["params"], ensure_ascii=False)
        elif task_flow_epoch is not None:
            raise ValueError("此任务没有流程步骤")
        else:
            row.pop("flow_epoch", None)
            row.pop("action_id", None)
            row["params"] = trusted_job["params"]
        row["card_id"] = trusted_job["card_id"]
        configuration, shop_context, workflow = runtime_execution(
            c, trusted_job, p["processor_id"]
        )
        row["variant"] = card_variant(c, trusted_job)
        row["steps"], row["completed_steps"] = progress_view(c, trusted_job)
    p = {**p, "processor_config": configuration}
    row["shop_context"] = shop_context
    row["workflow"] = workflow

    def check_execution(*, require_input=False, connection=None):
        if connection is not None:
            return _check_script_execution(
                connection, row, p["processor_id"], require_input=require_input
            )
        with db() as c:
            return _check_script_execution(
                c, row, p["processor_id"], require_input=require_input
            )

    row["_check_execution"] = check_execution
    check_execution(require_input=True)
    return await _execute_processor(row, p, extore_processors)


async def _execute_processor(row, p, processor_package):
    from .processor_runtime import (
        MAX_INPUT_BYTES,
        MAX_PROGRESS_EVENTS,
        environment,
        launch_command,
        process_environment,
        sandbox_command,
        validate_workflow,
    )

    workflow = validate_workflow(row["workflow"])
    check = row.get("_check_execution", lambda **kwargs: None)
    limits = workflow["runtime"]
    payload = {
        "params": json.loads(row["params"]),
        "configuration": p["processor_config"],
        "variant": row["variant"],
        "steps": [{"id": step["id"], "label": step["label"]} for step in row["steps"]],
        "completed_steps": row.get("completed_steps", []),
        "shop_context": row["shop_context"],
        "environment": environment(workflow),
    }
    encoded_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(encoded_payload) > MAX_INPUT_BYTES:
        raise ValueError("处理器任务输入超过限制")
    check(require_input=True)
    sandbox = await asyncio.to_thread(
        sandbox_command, p["processor_id"], processor_package
    )
    check(require_input=True)
    proc = await asyncio.create_subprocess_exec(
        *launch_command(sandbox, workflow),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd="/",
        env=process_environment(workflow),
        limit=131072,
        start_new_session=True,
        close_fds=True,
    )
    result = None
    total = 0
    updates = 0

    async def send():
        check(require_input=True)
        proc.stdin.write(encoded_payload)
        await proc.stdin.drain()
        check()
        proc.stdin.close()

    async def read():
        nonlocal result, total, updates
        while True:
            check()
            line = await proc.stdout.readline()
            check()
            if not line:
                break
            total += len(line)
            if total > limits["max_output_bytes"]:
                raise ValueError("处理器输出超过限制")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("处理器输出格式错误")
            if result is not None:
                raise ValueError("结果后不能继续输出")
            if value.get("kind") == "progress":
                updates += 1
                if updates > MAX_PROGRESS_EVENTS:
                    raise ValueError("处理器进度更新超过限制")
                with db() as c:
                    current = check(connection=c) or job(c, row["id"])
                    if (
                        current["attempt"] != row["attempt"]
                        or current["state"] != "processing"
                    ):
                        raise ValueError("处理任务已失效")
                    if "progress_steps" in value and row.get("flow_epoch") is None:
                        raw_steps = value["progress_steps"]
                        if (
                            not isinstance(raw_steps, list)
                            or not 1 <= len(raw_steps) <= 30
                        ):
                            raise ValueError("处理步骤数量无效")
                        steps = [
                            ProgressStep.model_validate(step).model_dump()
                            for step in raw_steps
                        ]
                        if len({step["id"] for step in steps}) != len(steps):
                            raise ValueError("处理步骤代码不能重复")
                        bootstrap_progress_plan(c, current, steps)
                    update = JobUpdate(
                        state="processing",
                        progress=value.get("progress", 0),
                        completed_steps=value.get("completed_steps")
                        if row.get("flow_epoch") is None
                        else None,
                        message=value.get("message", ""),
                        attempt=row["attempt"],
                        flow_epoch=row.get("flow_epoch"),
                        action_id=row.get("action_id"),
                    )
                    if row.get("flow_epoch") is not None:
                        from . import task_flow
                        from .service import finalize_task_flow

                        finalize_task_flow(
                            c,
                            task_flow.process_update(
                                c, current, update, row["flow_epoch"], actor="worker"
                            ),
                        )
                    else:
                        apply_update(c, row["id"], update)
            elif value.get("kind") == "result":
                if value.get("state") not in ("succeeded", "failed"):
                    raise ValueError("处理器必须返回终态")
                result = JobUpdate.model_validate(
                    {
                        **value,
                        "attempt": row["attempt"],
                        "flow_epoch": row.get("flow_epoch"),
                        "action_id": row.get("action_id"),
                    }
                )
            else:
                raise ValueError("处理器输出格式错误")
        check()
        await proc.wait()
        check()
        if proc.returncode or result is None:
            raise ValueError("处理器未正常完成")
        return result

    async def watch_authority():
        while True:
            check()
            await asyncio.sleep(0.25)

    tasks, work = [], None
    try:
        check(require_input=True)
        tasks = [asyncio.create_task(send()), asyncio.create_task(read())]
        work = asyncio.gather(*tasks)
        watcher = asyncio.create_task(watch_authority())
        tasks.append(watcher)
        done, _ = await asyncio.wait(
            (work, watcher),
            timeout=limits["timeout_seconds"],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise TimeoutError("处理器运行超时")
        if watcher in done:
            await watcher
        return work.result()[1]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if work is not None:
            await asyncio.gather(work, return_exceptions=True)
        if proc.returncode is None:
            import os
            import signal

            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()


async def job_once():
    with db() as c:
        exclusion = _flow_exclusion(c)
        # A crash/timeout may already have performed external effects. Never retry silently.
        expired = c.execute(
            "SELECT * FROM jobs WHERE state='processing' AND lease<?" + exclusion,
            (time.time(),),
        ).fetchall()
        for r in expired:
            apply_update(
                c,
                r["id"],
                JobUpdate(
                    state="failed",
                    attempt=r["attempt"],
                    message="处理超时或中断，待商家核实",
                    retryable=False,
                ),
            )
        rows = c.execute(
            "SELECT jobs.* FROM jobs JOIN products ON products.id=jobs.product_id JOIN shops ON shops.id=products.shop_id WHERE shops.enabled=1 AND jobs.state='queued' AND json_extract(products.config,'$.mode') IN ('script','webhook')"
            + exclusion
            + " ORDER BY jobs.created,jobs.id LIMIT 100"
        ).fetchall()
        selected = None
        for r in rows:
            p = product(c, r["product_id"])
            if p["mode"] == "script":
                selected = dict(r)
                selected["variant"] = card_variant(c, r)
                selected["steps"], selected["completed_steps"] = progress_view(c, r)
                apply_update(
                    c,
                    r["id"],
                    JobUpdate(
                        state="processing", attempt=r["attempt"], message="自动处理中"
                    ),
                )
                c.execute(
                    "UPDATE jobs SET lease=? WHERE id=?", (time.time() + 150, r["id"])
                )
                break
            if p["mode"] == "webhook":
                # Notification is already in the durable outbox; callback drives completion.
                apply_update(
                    c,
                    r["id"],
                    JobUpdate(
                        state="processing",
                        attempt=r["attempt"],
                        message="等待发货平台处理",
                    ),
                )
        if not selected:
            return False
    try:
        result = await execute_script(selected, p)
    except Exception:
        result = JobUpdate(
            state="failed",
            attempt=selected["attempt"],
            message="处理器未正常完成，待商家核实是否已交付",
            retryable=False,
        )
    with db() as c:
        current = job(c, selected["id"])
        if (
            current["state"] == "processing"
            and current["attempt"] == selected["attempt"]
        ):
            try:
                apply_update(c, selected["id"], result)
            except HTTPException:
                # A reviewed handler can still contain a bug. Reject malformed
                # results immediately instead of leaving the job on its lease.
                apply_update(
                    c,
                    selected["id"],
                    JobUpdate(
                        state="failed",
                        attempt=selected["attempt"],
                        message="处理器返回的结果不符合商品定义，待商家核实",
                        retryable=False,
                    ),
                )
    return True


def automation_maintenance_once():
    from .automation import cleanup

    with db() as c:
        if not _has_table(c, "automation_requests"):
            return 0
        return cleanup(c, limit=200)


async def loop():
    print("Extore worker running", flush=True)

    async def pump(handler):
        while True:
            try:
                busy = await handler()
            except Exception as exc:
                print("Worker error:", type(exc).__name__, flush=True)
                busy = False
            await asyncio.sleep(0.1 if busy else 1)

    async def maintenance():
        from .maintenance import maintenance_once as record_maintenance_once
        from .storage import maintenance_once

        while True:
            try:
                await asyncio.to_thread(maintenance_once)
            except Exception as exc:
                print("Storage maintenance error:", type(exc).__name__, flush=True)
            try:
                await asyncio.to_thread(record_maintenance_once)
            except Exception as exc:
                print("Record maintenance error:", type(exc).__name__, flush=True)
            try:
                await asyncio.to_thread(automation_maintenance_once)
            except Exception as exc:
                print("Automation maintenance error:", type(exc).__name__, flush=True)
            await asyncio.sleep(60)

    from .flow_worker import outbox_once as flow_outbox_once
    from .flow_worker import task_flow_once
    from .mail import process_outbox_once

    await asyncio.gather(
        pump(job_once),
        pump(outbox_once),
        pump(task_flow_once),
        pump(flow_outbox_once),
        pump(process_outbox_once),
        maintenance(),
    )


def main():
    init()
    with (DATA / "worker.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Only one worker per database is supported")
        asyncio.run(loop())


if __name__ == "__main__":
    main()

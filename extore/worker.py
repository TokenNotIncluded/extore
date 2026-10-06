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
from .variants import card_variant, default_variant


async def deliver_event(row):
    if row.get("id"):
        with db() as c:
            enabled = c.execute(
                "SELECT shops.enabled FROM events JOIN products ON products.id=events.product_id JOIN shops ON shops.id=products.shop_id WHERE events.id=?",
                (row["id"],),
            ).fetchone()
            if enabled is None or not enabled["enabled"]:
                raise ValueError("店铺已停用，不能发起处理")
    u = urlsplit(row["url"])
    # Pin the validated IP, retaining original Host and TLS SNI; no DNS rebinding.
    answers = await asyncio.to_thread(
        socket.getaddrinfo, u.hostname, u.port or 443, type=socket.SOCK_STREAM
    )
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
                "SELECT state,attempt FROM jobs WHERE id=?", (payload["data"]["id"],)
            ).fetchone()
            if (
                not r
                or r["state"] not in ("queued", "processing")
                or r["attempt"] != payload["data"]["attempt"]
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


async def execute_script(row, p):
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
        configuration, shop_context, workflow = runtime_execution(
            c, trusted_job, p["processor_id"]
        )
        row["params"] = trusted_job["params"]
    p = {**p, "processor_config": configuration}
    row["shop_context"] = shop_context
    row["workflow"] = workflow
    # Freeze trusted issuance metadata separately from all customer parameters.
    if "variant" not in row:
        if row.get("card_id"):
            with db() as c:
                row["variant"] = card_variant(c, row)
        else:
            row["variant"] = default_variant()
    if "steps" not in row:
        if row.get("card_id"):
            with db() as c:
                row["steps"], row["completed_steps"] = progress_view(
                    c, job(c, row["id"])
                )
        else:
            row["steps"], row["completed_steps"] = [], []
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
    sandbox = await asyncio.to_thread(
        sandbox_command, p["processor_id"], processor_package
    )
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
        proc.stdin.write(encoded_payload)
        await proc.stdin.drain()
        proc.stdin.close()

    async def read():
        nonlocal result, total, updates
        while True:
            line = await proc.stdout.readline()
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
                    current = job(c, row["id"])
                    if (
                        current["attempt"] != row["attempt"]
                        or current["state"] != "processing"
                    ):
                        raise ValueError("处理任务已失效")
                    if "progress_steps" in value:
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
                    apply_update(
                        c,
                        row["id"],
                        JobUpdate(
                            state="processing",
                            progress=value.get("progress", 0),
                            completed_steps=value.get("completed_steps"),
                            message=value.get("message", ""),
                            attempt=row["attempt"],
                        ),
                    )
            elif value.get("kind") == "result":
                if value.get("state") not in ("succeeded", "failed"):
                    raise ValueError("处理器必须返回终态")
                result = JobUpdate.model_validate({**value, "attempt": row["attempt"]})
            else:
                raise ValueError("处理器输出格式错误")
        await proc.wait()
        if proc.returncode or result is None:
            raise ValueError("处理器未正常完成")
        return result

    tasks = [asyncio.create_task(send()), asyncio.create_task(read())]
    try:
        _, completed = await asyncio.wait_for(
            asyncio.gather(*tasks), limits["timeout_seconds"]
        )
        return completed
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
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
        # A crash/timeout may already have performed external effects. Never retry silently.
        expired = c.execute(
            "SELECT * FROM jobs WHERE state='processing' AND lease<?", (time.time(),)
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
            "SELECT jobs.* FROM jobs JOIN products ON products.id=jobs.product_id JOIN shops ON shops.id=products.shop_id WHERE shops.enabled=1 AND jobs.state='queued' AND json_extract(products.config,'$.mode') IN ('script','webhook') ORDER BY jobs.created,jobs.id LIMIT 100"
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
            await asyncio.sleep(60)

    from .mail import process_outbox_once

    await asyncio.gather(
        pump(job_once), pump(outbox_once), pump(process_outbox_once), maintenance()
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

import asyncio
import fcntl
import ipaddress
import json
import os
import socket
import sys
import time
from urllib.parse import urlsplit

import httpx

from .config import DATA, SCRIPT_DIR
from .db import db, init
from .models import JobUpdate
from .security import sign, token
from .service import apply_update, job, product


async def deliver_event(row):
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
                    "UPDATE outbox SET state='cancelled' WHERE id=?", (row["id"],)
                )
                return True
    try:
        await deliver_event(row)
        with db() as c:
            c.execute(
                "UPDATE outbox SET state='delivered',attempts=attempts+1,error='' WHERE id=?",
                (row["id"],),
            )
    except Exception as exc:
        attempts = row["attempts"] + 1
        # Never retain response bodies or network exception strings containing secrets.
        error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        with db() as c:
            c.execute(
                "UPDATE outbox SET state=?,attempts=?,due=?,error=? WHERE id=?",
                (
                    "dead" if attempts >= 8 else "pending",
                    attempts,
                    time.time() + min(3600, 2**attempts * 5),
                    error,
                    row["id"],
                ),
            )
    return True


async def execute_script(row, p):
    path = (SCRIPT_DIR / (p["script"] + ".py")).resolve()
    if path.parent != SCRIPT_DIR or not path.is_file():
        raise ValueError("服务器上未安装此脚本")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=SCRIPT_DIR,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(
                __import__("pathlib").Path(__file__).resolve().parent.parent
            ),
            "PYTHONUNBUFFERED": "1",
            **{k: v for k, v in os.environ.items() if k.startswith("EXTORE_SCRIPT_")},
        },
        limit=150000,
        start_new_session=True,
    )
    payload = {
        "id": row["id"],
        "product_id": row["product_id"],
        "attempt": row["attempt"],
        "params": json.loads(row["params"]),
    }
    result = None
    total = 0

    async def read():
        nonlocal result, total
        proc.stdin.write(json.dumps(payload).encode())
        await proc.stdin.drain()
        proc.stdin.close()
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            total += len(line)
            if total > 1000000:
                raise ValueError("脚本输出超过限制")
            value = json.loads(line)
            if result is not None:
                raise ValueError("结果后不能继续输出")
            if value.get("kind") == "progress":
                with db() as c:
                    apply_update(
                        c,
                        row["id"],
                        JobUpdate(
                            state="processing",
                            progress=value["progress"],
                            message=value.get("message", ""),
                            attempt=row["attempt"],
                        ),
                    )
            elif value.get("kind") == "result":
                if value.get("state") not in ("succeeded", "failed"):
                    raise ValueError("脚本必须返回终态")
                result = JobUpdate.model_validate({**value, "attempt": row["attempt"]})
            else:
                raise ValueError("脚本输出格式错误")
        await proc.wait()
        if proc.returncode or result is None:
            raise ValueError("脚本未正常完成")
        return result

    try:
        return await asyncio.wait_for(read(), 120)
    finally:
        if proc.returncode is None:
            import signal

            os.killpg(proc.pid, signal.SIGKILL)
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
            "SELECT jobs.* FROM jobs JOIN products ON products.id=jobs.product_id WHERE jobs.state='queued' AND json_extract(products.config,'$.mode') IN ('script','webhook') ORDER BY jobs.created LIMIT 100"
        ).fetchall()
        selected = None
        for r in rows:
            p = product(c, r["product_id"])
            if p["mode"] == "script":
                selected = dict(r)
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
            message="脚本未正常完成，待商家核实是否已交付",
            retryable=False,
        )
    with db() as c:
        current = job(c, selected["id"])
        if (
            current["state"] == "processing"
            and current["attempt"] == selected["attempt"]
        ):
            apply_update(c, selected["id"], result)
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

    await asyncio.gather(pump(job_once), pump(outbox_once))


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

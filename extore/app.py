import json
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth
from .config import ORIGIN, check_config
from .db import audit, db, event, init, setting
from .models import (
    BatchUpdate,
    CodeInput,
    IssueCards,
    JobUpdate,
    Product,
    Redemption,
    TokenInput,
)
from .security import (
    card_digest,
    create_session,
    digest,
    fail,
    grant,
    rate_limit,
    session,
    token,
    verify_signature,
)
from .service import (
    apply_update,
    issue_cards,
    job,
    job_view,
    product,
    public_product,
    submit,
)


@asynccontextmanager
async def lifespan(app):
    check_config()
    init()
    yield


app = FastAPI(title="Extore API", version="0.1.0", lifespan=lifespan)
app.include_router(auth.router)


@app.middleware("http")
async def guard(request: Request, call_next):
    if (
        request.method not in ("GET", "HEAD", "OPTIONS")
        and request.url.path.startswith("/api/")
        and not request.url.path.startswith("/api/callbacks/")
        and not request.url.path.startswith("/api/integrations/")
    ):
        if request.headers.get("origin") != ORIGIN:
            return JSONResponse({"detail": "请求来源不匹配"}, status_code=403)
    # Streaming bounded read prevents unbounded webhook / JSON memory usage.
    if request.method not in ("GET", "HEAD"):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 256000:
                return JSONResponse({"detail": "请求过大"}, status_code=413)
        request._body = bytes(body)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    if ORIGIN.startswith("https:"):
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    with db() as c:
        c.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/products")
def public_products():
    with db() as c:
        rows = [
            public_product({"id": r["id"], **json.loads(r["config"])})
            for r in c.execute("SELECT * FROM products ORDER BY created")
        ]
    return [p for p in rows if p["public"]]


@app.post("/api/exchange")
def exchange(body: CodeInput, request: Request):
    rate_limit(request, "exchange", 20, 60)
    with db() as c:
        card = c.execute(
            "SELECT * FROM cards WHERE digest=?", (card_digest(body.code),)
        ).fetchone()
        if not card or card["state"] == "revoked":
            fail("卡密无效，请检查后重试", 404)
        row = c.execute("SELECT * FROM jobs WHERE card_id=?", (card["id"],)).fetchone()
        p = product(c, card["product_id"])
        if row and (
            row["state"] == "destroyed"
            or (
                row["state"] == "succeeded"
                and (p["view_policy"] == "once" or p["delivery"] == "service")
            )
        ):
            fail("卡密已使用，无法再次领取", 410)
        value = token()
        c.execute("DELETE FROM grants WHERE expires<?", (time.time(),))
        c.execute(
            "INSERT INTO grants VALUES (?,?,?)",
            (digest(value), card["id"], time.time() + 30 * 86400),
        )
        return {
            "token": value,
            "product": public_product(p),
            "job": job_view(c, row) if row else None,
        }


@app.post("/api/redeem")
def redeem(body: Redemption, request: Request):
    rate_limit(request, "redeem", 20, 60)
    with db() as c:
        card = grant(c, body.token)
        return job_view(c, submit(c, card, body.params))


@app.post("/api/receipt")
def receipt(body: TokenInput):
    with db() as c:
        card = grant(c, body.token)
        row = c.execute("SELECT * FROM jobs WHERE card_id=?", (card["id"],)).fetchone()
        return {
            "product": public_product(product(c, card["product_id"])),
            "job": job_view(c, row) if row else None,
        }


@app.post("/api/receipt/reveal")
def reveal(body: TokenInput):
    with db() as c:
        card = grant(c, body.token)
        row = c.execute("SELECT * FROM jobs WHERE card_id=?", (card["id"],)).fetchone()
        if not row or row["state"] != "succeeded":
            fail("尚无可领取内容", 409)
        p = product(c, card["product_id"])
        if p["delivery"] == "service":
            return {"content": None}
        if p["view_policy"] == "once" and row["revealed"]:
            fail("内容已领取，无法再次查看", 410)
        if not row["content"]:
            fail("内容已销毁", 410)
        content = row["content"]
        c.execute(
            "UPDATE jobs SET revealed=1,content=?,updated=? WHERE id=?",
            (None if p["view_policy"] == "once" else content, time.time(), row["id"]),
        )
        event(c, "delivery.viewed", row["product_id"], job(c, row["id"]))
        return {"content": content}


@app.post("/api/receipt/destroy")
def destroy(body: TokenInput):
    with db() as c:
        card = grant(c, body.token)
        row = c.execute("SELECT * FROM jobs WHERE card_id=?", (card["id"],)).fetchone()
        if not row or row["state"] not in ("succeeded", "destroyed"):
            fail("只能销毁已完成的交付", 409)
        if row["state"] == "destroyed":
            return {"ok": True}
        c.execute(
            "UPDATE jobs SET state='destroyed',content=NULL,params='{}',message='',updated=? WHERE id=?",
            (time.time(), row["id"]),
        )
        # Scrub queued/history payloads of customer parameters.
        for e in c.execute(
            "SELECT id,payload FROM events WHERE job_id=?", (row["id"],)
        ).fetchall():
            payload = json.loads(e["payload"])
            payload["data"].pop("params", None)
            c.execute(
                "UPDATE events SET payload=? WHERE id=?", (json.dumps(payload), e["id"])
            )
        event(c, "delivery.destroyed", row["product_id"], job(c, row["id"]))
        audit(c, "customer", "delivery.destroy", row["id"])
    return {"ok": True}


@app.get("/api/admin/products")
def admin_products(request: Request):
    session(request)
    with db() as c:
        return [
            {"id": r["id"], **json.loads(r["config"])}
            for r in c.execute("SELECT * FROM products ORDER BY created")
        ]


@app.post("/api/admin/products")
def create_product(body: Product, request: Request):
    session(request)
    pid = str(uuid.uuid4())
    with db() as c:
        c.execute(
            "INSERT INTO products VALUES (?,?,?)",
            (pid, body.model_dump_json(), time.time()),
        )
        audit(c, "owner", "product.create", pid)
    return {"id": pid, **body.model_dump()}


@app.put("/api/admin/products/{pid}")
def edit_product(pid: str, body: Product, request: Request):
    session(request)
    with db() as c:
        old = product(c, pid)
        # Delivery semantics and automation must not change under outstanding cards.
        if c.execute(
            "SELECT 1 FROM cards WHERE product_id=? LIMIT 1", (pid,)
        ).fetchone():
            for field in (
                "mode",
                "delivery",
                "view_policy",
                "script",
                "webhook_secret",
            ):
                if old[field] != getattr(body, field):
                    fail(
                        "已发行卡密的商品不能修改处理方式、交付方式、查看规则、脚本或签名密钥；请新建商品",
                        409,
                    )
        c.execute(
            "UPDATE products SET config=? WHERE id=?", (body.model_dump_json(), pid)
        )
        audit(c, "owner", "product.update", pid)
    return {"id": pid, **body.model_dump()}


@app.post("/api/admin/cards")
def cards(body: IssueCards, request: Request):
    session(request)
    with db() as c:
        codes = issue_cards(c, body.product_id, body.count)
        audit(c, "owner", "cards.issue", f"{body.product_id}:{body.count}")
    return {"codes": codes}


@app.get("/api/admin/cards")
def list_cards(request: Request, product_id: str = "", limit: int = 100):
    session(request)
    with db() as c:
        return [
            dict(r)
            for r in c.execute(
                "SELECT id,product_id,state,created FROM cards WHERE (?='' OR product_id=?) ORDER BY created DESC LIMIT ?",
                (product_id, product_id, max(1, min(limit, 500))),
            )
        ]


@app.post("/api/admin/cards/{cid}/revoke")
def revoke_card(cid: str, request: Request):
    session(request)
    with db() as c:
        row = c.execute("SELECT state FROM cards WHERE id=?", (cid,)).fetchone()
        if not row or row["state"] != "ready":
            fail("只能撤销尚未兑换的卡密", 409)
        c.execute("UPDATE cards SET state='revoked' WHERE id=?", (cid,))
        c.execute("DELETE FROM grants WHERE card_id=?", (cid,))
        audit(c, "owner", "card.revoke", cid)
    return {"ok": True}


class StaffInput(BaseModel):
    product_id: str
    name: str = Field(min_length=1, max_length=100)
    days: int = Field(default=7, ge=1, le=90)


@app.post("/api/admin/staff")
def create_staff(body: StaffInput, request: Request):
    session(request)
    value = token()
    sid = str(uuid.uuid4())
    with db() as c:
        p = product(c, body.product_id)
        if p["mode"] != "manual":
            fail("只能为人工处理商品授权员工")
        c.execute(
            "INSERT INTO staff(id,digest,product_id,name,expires) VALUES (?,?,?,?,?)",
            (
                sid,
                digest(value),
                body.product_id,
                body.name,
                time.time() + body.days * 86400,
            ),
        )
        audit(c, "owner", "staff.create", sid)
    return {"id": sid, "url": ORIGIN + "/staff#" + value}


@app.get("/api/admin/staff")
def list_staff(request: Request):
    session(request)
    with db() as c:
        return [
            dict(r)
            for r in c.execute("SELECT id,product_id,name,expires,revoked FROM staff")
        ]


@app.post("/api/admin/staff/{sid}/revoke")
def revoke_staff(sid: str, request: Request):
    session(request)
    with db() as c:
        c.execute("UPDATE staff SET revoked=1 WHERE id=?", (sid,))
        c.execute("DELETE FROM sessions WHERE staff_id=?", (sid,))
        # Explicitly release that employee's unfinished tasks to the queue.
        c.execute(
            "UPDATE jobs SET state='queued',claimed_by=NULL,lease=NULL,progress=0 WHERE claimed_by=? AND state='processing'",
            (sid,),
        )
        audit(c, "owner", "staff.revoke", sid)
    return {"ok": True}


@app.post("/api/staff/login")
def staff_login(body: TokenInput, request: Request, response: Response):
    rate_limit(request, "staff-login", 10, 60)
    with db() as c:
        row = c.execute(
            "SELECT * FROM staff WHERE digest=? AND revoked=0 AND expires>?",
            (digest(body.token), time.time()),
        ).fetchone()
        if not row:
            fail("员工链接无效或已过期", 401)
        c.execute(
            "DELETE FROM sessions WHERE digest=?",
            (digest(request.cookies.get("extore_session", "")),),
        )
        create_session(c, response, "staff", row["id"])
    return {"role": "staff"}


@app.get("/api/manage/jobs")
def jobs(request: Request, state: str = "", product_id: str = "", limit: int = 100):
    s = session(request, ("admin", "staff"))
    if s["role"] == "staff":
        product_id = s["product_id"]
    with db() as c:
        rows = c.execute(
            "SELECT * FROM jobs WHERE (?='' OR product_id=?) AND (?='' OR state=?) ORDER BY created LIMIT ?",
            (product_id, product_id, state, state, max(1, min(limit, 500))),
        ).fetchall()
        return [job_view(c, r, True) for r in rows]


@app.post("/api/manage/batch")
def batch(body: BatchUpdate, request: Request):
    s = session(request, ("admin", "staff"))
    actor = s["staff_id"] if s["role"] == "staff" else "owner"
    with db() as c:
        if (
            s["role"] == "staff"
            and not c.execute(
                "SELECT 1 FROM staff WHERE id=? AND revoked=0 AND expires>?",
                (s["staff_id"], time.time()),
            ).fetchone()
        ):
            fail("员工授权已失效", 401)
        for jid in dict.fromkeys(body.ids):
            r = job(c, jid)
            p = product(c, r["product_id"])
            if s["role"] == "staff" and r["product_id"] != s["product_id"]:
                fail("无权处理此商品", 403)
            if p["mode"] != "manual" and body.action != "retry":
                fail("自动处理任务不能由人工覆盖", 409)
            if body.action == "claim":
                if r["state"] != "queued":
                    fail("任务已被领取或完成，请刷新列表", 409)
                c.execute("UPDATE jobs SET claimed_by=? WHERE id=?", (actor, jid))
                apply_update(
                    c,
                    jid,
                    JobUpdate(
                        state="processing", attempt=r["attempt"], message="正在处理"
                    ),
                )
            elif body.action in ("progress", "succeed", "fail"):
                if r["state"] != "processing" or r["claimed_by"] != actor:
                    fail("请先领取任务，且只能处理自己领取的任务", 409)
                apply_update(
                    c,
                    jid,
                    JobUpdate(
                        state={
                            "succeed": "succeeded",
                            "fail": "failed",
                            "progress": "processing",
                        }[body.action],
                        progress=body.progress,
                        attempt=r["attempt"],
                        content=body.content,
                        message=body.message,
                        retryable=body.retryable,
                    ),
                )
            elif body.action == "retry":
                if s["role"] != "admin":
                    fail("只有商家能核实并放行重试", 403)
                if r["state"] != "failed":
                    fail("只能放行失败的任务", 409)
                c.execute("UPDATE jobs SET retryable=1 WHERE id=?", (jid,))
            audit(c, actor, "job." + body.action, jid)
    return {"ok": True}


@app.get("/api/admin/events")
def events(request: Request):
    session(request)
    with db() as c:
        return [
            dict(r)
            for r in c.execute(
                "SELECT events.id,type,job_id,created,outbox.state AS webhook_state,attempts,error FROM events LEFT JOIN outbox ON outbox.id=events.id ORDER BY created DESC LIMIT 100"
            )
        ]


@app.post("/api/admin/events/{eid}/retry")
def retry_event(eid: str, request: Request):
    session(request)
    with db() as c:
        row = c.execute("SELECT state FROM outbox WHERE id=?", (eid,)).fetchone()
        if not row or row["state"] != "dead":
            fail("只能重试已停止投递的事件", 409)
        c.execute(
            "UPDATE outbox SET state='pending',attempts=0,due=? WHERE id=?",
            (time.time(), eid),
        )
        audit(c, "owner", "webhook.retry", eid)
    return {"ok": True}


@app.post("/api/callbacks/{pid}/{jid}")
async def callback(pid: str, jid: str, request: Request):
    body = await request.body()
    timestamp = request.headers.get("x-extore-timestamp", "")
    nonce = request.headers.get("x-extore-nonce", "")
    signature = request.headers.get("x-extore-signature", "")
    with db() as c:
        p = product(c, pid)
        if p["mode"] != "webhook":
            fail("此商品不支持回调", 403)
        verify_signature(p["webhook_secret"], timestamp, nonce, body, signature)
        if job(c, jid)["product_id"] != pid:
            fail("任务不属于此商品", 403)
        c.execute("DELETE FROM callback_nonces WHERE created<?", (time.time() - 600,))
        nonce_key = digest(pid + ":" + nonce)
        if c.execute(
            "SELECT 1 FROM callback_nonces WHERE nonce=?", (nonce_key,)
        ).fetchone():
            fail("回调重复", 409)
        try:
            update = JobUpdate.model_validate_json(body)
        except ValueError:
            fail("回调格式错误", 422)
        result = job_view(c, apply_update(c, jid, update))
        c.execute("INSERT INTO callback_nonces VALUES (?,?)", (nonce_key, time.time()))
    return result


@app.post("/api/integrations/cards")
def platform_cards(body: IssueCards, request: Request):
    rate_limit(request, "integration", 60, 60)
    with db() as c:
        key = request.headers.get("authorization", "").removeprefix("Bearer ")
        expected = setting(c, "integration_key")
        if not expected or not secrets.compare_digest(digest(key), expected):
            fail("平台密钥无效", 401)
        key = request.headers.get("idempotency-key", "")
        if not 8 <= len(key) <= 200:
            fail("请提供 8–200 字符的 Idempotency-Key", 400)
        fingerprint = digest(body.model_dump_json())
        request_key = digest(key)
        old = c.execute(
            "SELECT * FROM api_requests WHERE key=?", (request_key,)
        ).fetchone()
        from cryptography.fernet import Fernet

        from .config import DATA

        cipher = Fernet((DATA / "issuance.key").read_bytes())
        if old:
            if old["fingerprint"] != fingerprint:
                fail("同一个幂等键不能用于不同请求", 409)
            return json.loads(cipher.decrypt(old["response"]))
        result = {"codes": issue_cards(c, body.product_id, body.count)}
        c.execute(
            "INSERT INTO api_requests VALUES (?,?,?,?)",
            (
                request_key,
                fingerprint,
                cipher.encrypt(json.dumps(result).encode()),
                time.time(),
            ),
        )
        audit(c, "platform", "cards.issue", f"{body.product_id}:{body.count}")
        return result


STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/admin", methods=["GET", "HEAD"])
@app.api_route("/staff", methods=["GET", "HEAD"])
@app.api_route("/receipt", methods=["GET", "HEAD"])
def index():
    return FileResponse(STATIC / "index.html")

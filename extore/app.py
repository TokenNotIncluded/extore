import json
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth
from .card_tracking import router as card_tracking_router
from .config import ORIGIN, check_config
from .db import audit, db, event, init, setting
from .files import (
    MAX_MULTIPART_BYTES,
    purge_job_files,
    release_output_files,
)
from .files import (
    router as files_router,
)
from .link_access import augment_link_view, consume_link, revoke_staff_sessions
from .link_access import router as link_access_router
from .models import (
    BatchUpdate,
    CodeInput,
    IssueCards,
    JobUpdate,
    ManagementProduct,
    Product,
    ProductLinkInput,
    QuickProductInput,
    Redemption,
    StaffInput,
    TokenInput,
)
from .processors import processor_catalog, public_configuration
from .security import (
    authorize_management,
    batch_cards,
    card_digest,
    create_session,
    digest,
    fail,
    grant,
    link_descendant_ids,
    rate_limit,
    resolve_customer_card,
    session,
    split_codes,
    staff_authorization,
    token,
    verify_signature,
)
from .service import (
    apply_update,
    bootstrap_progress_plan,
    freeze_product_plans,
    freeze_product_schemas,
    issue_cards,
    job,
    job_product,
    job_view,
    product,
    public_product,
    submit,
)
from .source import router as source_router
from .variants import card_variant, issued_variant_ids


@asynccontextmanager
async def lifespan(app):
    check_config()
    init()
    yield


app = FastAPI(title="Extore API", version="0.4.0", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(card_tracking_router)
app.include_router(files_router)
app.include_router(source_router)
app.include_router(link_access_router)


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
    upload = request.url.path in ("/api/files/upload", "/api/manage/files/upload")
    if (
        upload
        and request.headers.get("content-length", "").isdigit()
        and int(request.headers["content-length"]) > MAX_MULTIPART_BYTES
    ):
        return JSONResponse({"detail": "上传文件超过 20 MiB 限制"}, status_code=413)
    if request.method not in ("GET", "HEAD") and not upload:
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
    response.headers.setdefault(
        "Content-Security-Policy",
        (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        ),
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
            public_product(product(c, r["id"]))
            for r in c.execute("SELECT * FROM products ORDER BY created")
        ]
    return [p for p in rows if p["public"]]


def _screen_exchange_card(c, card, index=None):
    from .card_tracking import ensure_card_usable

    prefix = f"第 {index} 张：" if index else ""
    try:
        if not card or card["state"] == "revoked":
            batch_revoked = card is not None and index is not None
            fail(
                "卡密已撤销" if batch_revoked else "卡密无效，请检查后重试",
                410 if batch_revoked else 404,
            )
        ensure_card_usable(c, card)
        row = c.execute("SELECT * FROM jobs WHERE card_id=?", (card["id"],)).fetchone()
        p = job_product(c, row) if row else product(c, card["product_id"])
        if row and (
            row["state"] == "destroyed"
            or (
                row["state"] == "succeeded"
                and (p["view_policy"] == "once" or p["delivery"] == "service")
            )
        ):
            fail("卡密已使用，无法再次领取", 410)
    except HTTPException as exc:
        if prefix and isinstance(exc.detail, str):
            fail(prefix + exc.detail, exc.status_code)
        raise
    return row, p


def _batch_receipt(c, value):
    cards = batch_cards(c, value)
    if not cards:
        fail("兑换凭证无效或已过期，请重新输入卡密", 404)
    items = []
    for card in cards:
        row = c.execute("SELECT * FROM jobs WHERE card_id=?", (card["id"],)).fetchone()
        meta = c.execute(
            "SELECT code_suffix FROM card_meta WHERE card_id=?", (card["id"],)
        ).fetchone()
        items.append(
            {
                "card_id": card["id"],
                "suffix": meta["code_suffix"] if meta and meta["code_suffix"] else "",
                "variant": card_variant(c, card),
                "product": public_product(
                    job_product(c, row) if row else product(c, card["product_id"])
                ),
                "job": job_view(c, row) if row else None,
            }
        )
    first = cards[0]
    return {
        "batch": True,
        "product": public_product(product(c, first["product_id"])),
        "items": items,
    }


@app.post("/api/exchange")
def exchange(body: CodeInput, request: Request):
    rate_limit(request, "exchange", 20, 60)
    with db() as c:
        from .card_tracking import record_verified

        # Preserve the original single-code format, including spaces between
        # its groups, before treating whitespace as a separator between codes.
        single = c.execute(
            "SELECT 1 FROM cards WHERE digest=?", (card_digest(body.code),)
        ).fetchone()
        codes = [body.code] if single else split_codes(body.code)
        if not codes:
            fail("请输入卡密")
        if len(codes) > 30:
            fail("一次最多兑换 30 张卡密")
        loaded = []
        for index, code in enumerate(codes, start=1):
            card = c.execute(
                "SELECT * FROM cards WHERE digest=?", (card_digest(code),)
            ).fetchone()
            row, p = _screen_exchange_card(c, card, index if len(codes) > 1 else None)
            loaded.append((card, row, p))
        if len({card["product_id"] for card, _, _ in loaded}) > 1:
            fail("这些卡密不是同一件商品，请分开兑换")
        value = token()
        expires = time.time() + 30 * 86400
        now = time.time()
        c.execute("DELETE FROM grants WHERE expires<?", (now,))
        # Existing installations have non-cascading foreign keys. Remove the
        # children first rather than depending on a changed CREATE TABLE.
        c.execute(
            "DELETE FROM receipt_batch_cards WHERE digest IN "
            "(SELECT digest FROM receipt_batches WHERE expires<?)",
            (now,),
        )
        c.execute("DELETE FROM receipt_batches WHERE expires<?", (now,))
        if len(loaded) == 1:
            card, row, p = loaded[0]
            c.execute(
                "INSERT INTO grants VALUES (?,?,?)",
                (digest(value), card["id"], expires),
            )
            record_verified(c, card["id"])
            return {
                "token": value,
                "product": public_product(p),
                "variant": card_variant(c, card),
                "job": job_view(c, row) if row else None,
            }
        c.execute(
            "INSERT INTO receipt_batches VALUES (?,?,?)",
            (digest(value), expires, time.time()),
        )
        c.executemany(
            "INSERT INTO receipt_batch_cards VALUES (?,?,?)",
            [
                (digest(value), card["id"], position)
                for position, (card, _, _) in enumerate(loaded)
            ],
        )
        for card, _, _ in loaded:
            record_verified(c, card["id"])
        result = _batch_receipt(c, value)
        result["token"] = value
        return result


@app.post("/api/redeem")
def redeem(body: Redemption, request: Request):
    rate_limit(request, "redeem", 20, 60)
    with db() as c:
        cards = batch_cards(c, body.token)
        if cards:
            if not body.items:
                fail("请为每张待兑换的卡密填写启动参数")
            by_id = {card["id"]: card for card in cards}
            seen = set()
            for item in body.items:
                if item.card_id in seen:
                    fail("卡密重复提交")
                seen.add(item.card_id)
                card = by_id.get(item.card_id)
                if not card:
                    fail("这张卡密不在此领取链接中", 404)
                submit(c, card, item.params)
            return _batch_receipt(c, body.token)
        if body.items:
            fail("单张卡密请直接提交参数")
        card = grant(c, body.token)
        return job_view(c, submit(c, card, body.params))


@app.post("/api/receipt")
def receipt(body: TokenInput):
    with db() as c:
        if batch_cards(c, body.token):
            return _batch_receipt(c, body.token)
        card = grant(c, body.token)
        row = c.execute("SELECT * FROM jobs WHERE card_id=?", (card["id"],)).fetchone()
        return {
            "product": public_product(
                job_product(c, row) if row else product(c, card["product_id"])
            ),
            "variant": card_variant(c, card),
            "job": job_view(c, row) if row else None,
        }


@app.post("/api/receipt/reveal")
def reveal(body: TokenInput):
    with db() as c:
        card = resolve_customer_card(c, body.token, body.card_id)
        row = c.execute("SELECT * FROM jobs WHERE card_id=?", (card["id"],)).fetchone()
        if not row or row["state"] != "succeeded":
            fail("尚无可领取内容", 409)
        p = job_product(c, row)
        if p["delivery"] == "service":
            return {"content": None, "output": {}}
        if p["view_policy"] == "once" and row["revealed"]:
            fail("内容已领取，无法再次查看", 410)
        if not row["content"] and row["result_json"] is None:
            fail("内容已销毁", 410)
        content = row["content"]
        output = (
            json.loads(row["result_json"])
            if row["result_json"] is not None
            else {"content": content}
        )
        files = release_output_files(c, row)
        c.execute(
            "UPDATE jobs SET revealed=1,content=?,result_json=?,updated=? WHERE id=?",
            (
                None if p["view_policy"] == "once" else content,
                None if p["view_policy"] == "once" else row["result_json"],
                time.time(),
                row["id"],
            ),
        )
        event(c, "delivery.viewed", row["product_id"], job(c, row["id"]))
        return {
            "content": content,
            "output": output,
            **({"files": files} if files else {}),
        }


@app.post("/api/receipt/destroy")
def destroy(body: TokenInput):
    with db() as c:
        card = resolve_customer_card(c, body.token, body.card_id)
        row = c.execute("SELECT * FROM jobs WHERE card_id=?", (card["id"],)).fetchone()
        if not row or row["state"] not in ("succeeded", "destroyed"):
            fail("只能销毁已完成的交付", 409)
        if row["state"] == "destroyed":
            return {"ok": True}
        purge_job_files(c, row["id"])
        c.execute(
            "UPDATE jobs SET state='destroyed',content=NULL,result_json=NULL,params='{}',message='',updated=? WHERE id=?",
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
            product(c, r["id"])
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


PRODUCT_TEMPLATES = (
    {
        "id": "manual_content",
        "name": "队列内容交付",
        "description": "由人员或 AI 从队列领取任务并提供交付内容。创建后请完善商品信息与输入输出。",
        "mode": "manual",
        "delivery": "content",
    },
    {
        "id": "manual_service",
        "name": "队列服务办理",
        "description": "由人员或 AI 从队列领取任务办理，仅显示办理状态。创建后请完善商品信息与输入。",
        "mode": "manual",
        "delivery": "service",
    },
)


@app.get("/api/admin/product-templates")
def product_templates(request: Request):
    session(request)
    return list(PRODUCT_TEMPLATES)


@app.get("/api/admin/processors")
def admin_processors(request: Request):
    session(request)
    return processor_catalog()


@app.get("/api/manage/processors")
def managed_processors(request: Request):
    s = session(request, ("admin", "staff"))
    with db() as c:
        authorize_management(c, s, "product.edit")
        return processor_catalog()


@app.post("/api/admin/products/quick")
def quick_product(body: QuickProductInput, request: Request):
    s = session(request)
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    name = body.name or "未命名商品 · " + "".join(
        secrets.choice(alphabet) for _ in range(6)
    )
    pid = str(uuid.uuid4())
    with db() as c:
        if body.template_id == "existing_product":
            source = product(c, body.from_product_id)
            values = {key: value for key, value in source.items() if key != "id"}
            # A new configuration link must never inherit the source product's
            # callback signing authority, even when the connector is copied.
            if values["webhook_secret"]:
                values["webhook_secret"] = secrets.token_urlsafe(32)
            # Copy processor structure without the source's delivery secrets.
            values["processor_config"] = public_configuration(values)
            config = Product.model_validate({**values, "name": name, "public": False})
        else:
            template = next(t for t in PRODUCT_TEMPLATES if t["id"] == body.template_id)
            config = Product(
                name=name, mode=template["mode"], delivery=template["delivery"]
            )
        c.execute(
            "INSERT INTO products VALUES (?,?,?)",
            (pid, config.model_dump_json(), time.time()),
        )
        audit(c, "owner", "product.create", pid)
        link = create_product_link(
            c,
            ProductLinkInput(
                product_id=pid,
                name=("AI 配置 · " + name)[:100],
                days=7,
                permissions=["product.edit", "fulfillment.configure"],
            ),
            s,
        )
        return {"product": {"id": pid, **config.model_dump()}, "management_link": link}


@app.put("/api/admin/products/{pid}")
def edit_product(pid: str, body: Product, request: Request):
    session(request)
    with db() as c:
        save_product(c, pid, body, "owner")
    return {"id": pid, **body.model_dump()}


def save_product(c, pid, body, actor):
    old = product(c, pid)
    freeze_product_plans(c, pid, old["progress_steps"])
    freeze_product_schemas(c, pid, old)
    removed = issued_variant_ids(c, pid) - {v.id for v in body.variants}
    if removed:
        fail(
            "已发行卡密的规格不能删除；请停用该规格：" + "、".join(sorted(removed)),
            409,
        )
    # Delivery semantics and automation must not change under outstanding cards.
    if c.execute("SELECT 1 FROM cards WHERE product_id=? LIMIT 1", (pid,)).fetchone():
        for field in (
            "mode",
            "delivery",
            "view_policy",
            "script",
            "webhook_secret",
            "processor_id",
        ):
            if old[field] != getattr(body, field):
                fail(
                    "已发行卡密的商品不能修改处理方式、交付方式、查看规则、处理器或签名密钥；请新建商品",
                    409,
                )
        values = body.model_dump()
        for field, label in (("parameters", "输入"), ("outputs", "输出")):
            if old["mode"] != "manual" and field_schema(old[field]) != field_schema(
                values[field]
            ):
                fail(
                    f"已发行卡密的商品不能修改{label}字段的代码名、类型或必填规则；请新建商品",
                    409,
                )
    c.execute("UPDATE products SET config=? WHERE id=?", (body.model_dump_json(), pid))
    audit(c, actor, "product.update", pid)


def field_schema(fields):
    return {field["key"]: (field["type"], field["required"]) for field in fields}


@app.post("/api/admin/cards")
def cards(body: IssueCards, request: Request):
    session(request)
    with db() as c:
        codes = issue_cards(
            c,
            body.product_id,
            body.count,
            label=body.label,
            expires=body.expires,
            variant_id=body.variant_id,
        )
        audit(c, "owner", "cards.issue", f"{body.product_id}:{body.count}")
        return {"codes": codes, "batch_id": card_batch_id(c, codes)}


def card_batch_id(c, codes):
    if not codes:
        return None
    row = c.execute(
        "SELECT card_meta.batch_id FROM card_meta JOIN cards ON cards.id=card_meta.card_id WHERE cards.digest=?",
        (card_digest(codes[0]),),
    ).fetchone()
    return row["batch_id"] if row else None


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
        c.execute("DELETE FROM receipt_batch_cards WHERE card_id=?", (cid,))
        audit(c, "owner", "card.revoke", cid)
    return {"ok": True}


def link_view(row):
    return {
        **augment_link_view(row),
        **{
            key: json.loads(row[key]) if key == "permissions" else row[key]
            for key in (
                "id",
                "product_id",
                "name",
                "expires",
                "revoked",
                "permissions",
                "parent_id",
                "created",
            )
        },
    }


def create_product_link(c, body, s):
    pid = queue_product_id(s, body.product_id)
    product(c, pid)
    now = time.time()
    parent_id = s["staff_id"] if s["role"] == "staff" else None
    if parent_id:
        parent = c.execute(
            "SELECT max_uses FROM staff WHERE id=?", (parent_id,)
        ).fetchone()
        if body.max_uses > parent["max_uses"]:
            fail("下级链接可用次数不能超过当前链接的上限", 403)
        if not set(body.permissions) < set(s["permissions"]):
            fail("下级权限必须是当前商品管理权限的严格子集", 403)
        parent_expires = s["link_expires"]
        expires = (
            min(now + 7 * 86400, parent_expires)
            if body.days is None
            else now + body.days * 86400
        )
        if expires > parent_expires:
            fail("下级链接期限不能超过当前商品管理链接", 403)
    else:
        expires = now + (7 if body.days is None else body.days) * 86400
    sid = str(uuid.uuid4())
    value = token()
    c.execute(
        "INSERT INTO staff(id,digest,product_id,name,expires,permissions,parent_id,created,max_uses) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            sid,
            digest(value),
            pid,
            body.name,
            expires,
            json.dumps(body.permissions),
            parent_id,
            now,
            body.max_uses,
        ),
    )
    audit(c, parent_id or "owner", "staff.create", sid)
    result = link_view(c.execute("SELECT * FROM staff WHERE id=?", (sid,)).fetchone())
    return {**result, "url": ORIGIN + "/staff#" + value}


def revoke_product_link(c, sid, actor):
    if not c.execute("SELECT 1 FROM staff WHERE id=?", (sid,)).fetchone():
        fail("商品管理链接不存在", 404)
    ids = link_descendant_ids(c, sid)
    for target in ids:
        c.execute("UPDATE staff SET revoked=1 WHERE id=?", (target,))
        revoke_staff_sessions(c, target, actor)
        # Return unfinished delegated tasks to their product queue.
        c.execute(
            "UPDATE jobs SET state='queued',claimed_by=NULL,lease=NULL,"
            "progress=CASE WHEN json_array_length(COALESCE(progress_plan,'[]'))>0 "
            "THEN progress ELSE 0 END WHERE claimed_by=? AND state='processing'",
            (target,),
        )
    audit(c, actor, "staff.revoke", sid)
    return {"ok": True, "revoked_count": len(ids)}


@app.post("/api/admin/staff")
def create_staff(body: StaffInput, request: Request):
    s = session(request)
    with db() as c:
        return create_product_link(c, body, s)


@app.get("/api/admin/staff")
def list_staff(request: Request):
    session(request)
    with db() as c:
        return [link_view(r) for r in c.execute("SELECT * FROM staff ORDER BY created")]


@app.post("/api/admin/staff/{sid}/revoke")
def revoke_staff(sid: str, request: Request):
    session(request)
    with db() as c:
        return revoke_product_link(c, sid, "owner")


@app.post("/api/staff/login")
def staff_login(body: TokenInput, request: Request, response: Response):
    rate_limit(request, "staff-login", 10, 60)
    with db() as c:
        row = c.execute(
            "SELECT * FROM staff WHERE digest=?", (digest(body.token),)
        ).fetchone()
        if not row:
            fail("商品管理链接无效或已过期", 401)
        staff_authorization(c, row["id"])
        current = c.execute(
            "SELECT * FROM sessions WHERE digest=? AND expires>? AND revoked=0",
            (digest(request.cookies.get("extore_session", "")), time.time()),
        ).fetchone()
        if current and current["role"] == "staff" and current["staff_id"] == row["id"]:
            return {"role": "staff"}
        consume_link(c, row, request)
        create_session(c, response, "staff", row["id"], request=request)
    return {"role": "staff"}


@app.get("/api/manage/products")
def managed_products(request: Request):
    s = session(request, ("admin", "staff"))
    with db() as c:
        queue_staff_authorization(c, s)
        scope = s["product_id"] if s["role"] == "staff" else ""
        rows = c.execute(
            "SELECT * FROM products WHERE (?='' OR id=?) ORDER BY created",
            (scope, scope),
        ).fetchall()
        result = []
        for r in rows:
            p = product(c, r["id"])
            result.append(
                {
                    key: p[key]
                    for key in (
                        "id",
                        "name",
                        "mode",
                        "delivery",
                        "view_policy",
                        "parameters",
                        "outputs",
                        "variants",
                        "progress_steps",
                        "support_email",
                    )
                }
            )
        return result


def queue_staff_authorization(c, s):
    return authorize_management(c, s)


def queue_product_id(s, product_id):
    if s["role"] == "staff":
        if product_id and product_id != s["product_id"]:
            fail("无权处理此商品", 403)
        return s["product_id"]
    if not product_id:
        fail("请先选择商品")
    return product_id


@app.get("/api/manage/jobs")
def jobs(
    request: Request,
    state: str = "",
    product_id: str = "",
    limit: int = 100,
    job_id: str = "",
    view: Literal["active", "processed", "all"] = "active",
):
    s = session(request, ("admin", "staff"))
    with db() as c:
        authorize_management(c, s, "queue.view")
        product_id = queue_product_id(s, product_id)
        product(c, product_id)
        conditions = ["product_id=?"]
        values = [product_id]
        if state:
            conditions.append("state=?")
            values.append(state)
        elif not job_id:
            if view == "active":
                conditions.append("state IN ('queued','processing','failed')")
            elif view == "processed":
                conditions.append("state IN ('succeeded','destroyed')")
        if job_id:
            conditions.append("id=?")
            values.append(job_id)
        values.append(max(1, min(limit, 500)))
        rows = c.execute(
            "SELECT * FROM jobs WHERE "
            + " AND ".join(conditions)
            + " ORDER BY created,id LIMIT ?",
            values,
        ).fetchall()
        return [job_view(c, r, True) for r in rows]


@app.post("/api/manage/batch")
def batch(body: BatchUpdate, request: Request):
    s = session(request, ("admin", "staff"))
    actor = s["staff_id"] if s["role"] == "staff" else "owner"
    with db() as c:
        authorize_management(
            c, s, "queue.retry" if body.action == "retry" else "queue.process"
        )
        if body.progress_steps is not None:
            authorize_management(c, s, "queue.process")
        product_id = queue_product_id(s, body.product_id)
        p = product(c, product_id)
        rows = [job(c, jid) for jid in dict.fromkeys(body.ids)]
        if any(r["product_id"] != product_id for r in rows):
            fail("批处理只能包含所选商品的任务", 403)
        for r in rows:
            jid = r["id"]
            if p["mode"] != "manual" and body.action != "retry":
                fail("自动处理任务不能由队列处理覆盖", 409)
            if body.progress_steps is not None:
                bootstrap_progress_plan(
                    c, r, [step.model_dump() for step in body.progress_steps]
                )
                r = job(c, jid)
            if body.action == "claim":
                if r["state"] != "queued":
                    fail("任务已被领取或完成，请刷新列表", 409)
                c.execute("UPDATE jobs SET claimed_by=? WHERE id=?", (actor, jid))
                apply_update(
                    c,
                    jid,
                    JobUpdate(
                        state="processing",
                        attempt=r["attempt"],
                        progress=body.progress,
                        completed_steps=body.completed_steps,
                        message=body.message or "正在处理",
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
                        completed_steps=body.completed_steps,
                        attempt=r["attempt"],
                        content=body.content,
                        output=body.output,
                        message=body.message,
                        retryable=body.retryable,
                    ),
                )
            elif body.action == "retry":
                if r["state"] != "failed":
                    fail("只能放行失败的任务", 409)
                from .card_tracking import ensure_card_usable

                ensure_card_usable(
                    c,
                    c.execute(
                        "SELECT * FROM cards WHERE id=?", (r["card_id"],)
                    ).fetchone(),
                )
                c.execute("UPDATE jobs SET retryable=1 WHERE id=?", (jid,))
            audit(c, actor, "job." + body.action, jid)
    return {"ok": True}


def management_scope(c, s, product_id, permission):
    authorize_management(c, s, permission)
    pid = queue_product_id(s, product_id)
    product(c, pid)
    return pid


def management_actor(s):
    return s["staff_id"] if s["role"] == "staff" else "owner"


@app.get("/api/manage/product")
def managed_product(request: Request, product_id: str = ""):
    s = session(request, ("admin", "staff"))
    with db() as c:
        pid = management_scope(c, s, product_id, "product.edit")
        return managed_product_view(product(c, pid), s)


def managed_product_view(p, s):
    if "fulfillment.configure" not in s["permissions"]:
        return {**p, "webhook_secret": "", "processor_config": {}}
    return p


@app.put("/api/manage/product")
def edit_managed_product(
    body: ManagementProduct, request: Request, product_id: str = ""
):
    s = session(request, ("admin", "staff"))
    with db() as c:
        pid = management_scope(c, s, product_id, "product.edit")
        old = product(c, pid)
        values = body.model_dump()
        if not values["webhook_secret"]:
            values["webhook_secret"] = old["webhook_secret"]
        if "fulfillment.configure" not in s["permissions"]:
            if not values["processor_config"]:
                values["processor_config"] = old["processor_config"]
            for field in (
                "mode",
                "delivery",
                "view_policy",
                "script",
                "webhook_url",
                "webhook_secret",
                "allow_retry",
                "max_attempts",
                "processor_id",
                "processor_config",
            ):
                if values[field] != old[field]:
                    fail("修改发货、查看或重试配置需要配置发货权限", 403)
            if field_schema(values["outputs"]) != field_schema(old["outputs"]):
                fail("修改输出字段架构需要配置发货权限", 403)
        try:
            updated = Product.model_validate(values)
        except ValueError:
            fail("商品配置格式错误", 422)
        save_product(c, pid, updated, management_actor(s))
        return managed_product_view({"id": pid, **updated.model_dump()}, s)


@app.get("/api/manage/cards")
def managed_cards(request: Request, product_id: str = "", limit: int = 100):
    s = session(request, ("admin", "staff"))
    with db() as c:
        pid = management_scope(c, s, product_id, "cards.manage")
        return [
            dict(r)
            for r in c.execute(
                "SELECT id,product_id,state,created FROM cards WHERE product_id=? ORDER BY created DESC LIMIT ?",
                (pid, max(1, min(limit, 500))),
            )
        ]


@app.post("/api/manage/cards")
def issue_managed_cards(body: IssueCards, request: Request):
    s = session(request, ("admin", "staff"))
    with db() as c:
        pid = management_scope(c, s, body.product_id, "cards.manage")
        codes = issue_cards(
            c,
            pid,
            body.count,
            label=body.label,
            expires=body.expires,
            variant_id=body.variant_id,
        )
        audit(c, management_actor(s), "cards.issue", f"{pid}:{body.count}")
        return {"codes": codes, "batch_id": card_batch_id(c, codes)}


@app.post("/api/manage/cards/{cid}/revoke")
def revoke_managed_card(cid: str, request: Request, product_id: str = ""):
    s = session(request, ("admin", "staff"))
    with db() as c:
        pid = management_scope(c, s, product_id, "cards.manage")
        row = c.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
        if not row:
            fail("卡密不存在", 404)
        if row["product_id"] != pid:
            fail("无权管理此商品的卡密", 403)
        if row["state"] != "ready":
            fail("只能撤销尚未兑换的卡密", 409)
        c.execute("UPDATE cards SET state='revoked' WHERE id=?", (cid,))
        c.execute("DELETE FROM grants WHERE card_id=?", (cid,))
        c.execute("DELETE FROM receipt_batch_cards WHERE card_id=?", (cid,))
        audit(c, management_actor(s), "card.revoke", cid)
    return {"ok": True}


@app.get("/api/manage/events")
def managed_events(request: Request, product_id: str = "", limit: int = 100):
    s = session(request, ("admin", "staff"))
    with db() as c:
        pid = management_scope(c, s, product_id, "events.manage")
        return [
            dict(r)
            for r in c.execute(
                "SELECT events.id,type,job_id,product_id,created,outbox.state AS webhook_state,attempts,error FROM events LEFT JOIN outbox ON outbox.id=events.id WHERE events.product_id=? ORDER BY created DESC LIMIT ?",
                (pid, max(1, min(limit, 500))),
            )
        ]


@app.post("/api/manage/events/{eid}/retry")
def retry_managed_event(eid: str, request: Request, product_id: str = ""):
    s = session(request, ("admin", "staff"))
    with db() as c:
        pid = management_scope(c, s, product_id, "events.manage")
        row = c.execute(
            "SELECT events.product_id,outbox.state FROM events LEFT JOIN outbox ON outbox.id=events.id WHERE events.id=?",
            (eid,),
        ).fetchone()
        if not row:
            fail("事件不存在", 404)
        if row["product_id"] != pid:
            fail("无权管理此商品的事件", 403)
        if row["state"] != "dead":
            fail("只能重试已停止投递的事件", 409)
        c.execute(
            "UPDATE outbox SET state='pending',attempts=0,due=? WHERE id=?",
            (time.time(), eid),
        )
        audit(c, management_actor(s), "webhook.retry", eid)
    return {"ok": True}


@app.get("/api/manage/links")
def managed_links(request: Request, product_id: str = ""):
    s = session(request, ("admin", "staff"))
    with db() as c:
        pid = management_scope(c, s, product_id, "links.delegate")
        descendants = (
            set(link_descendant_ids(c, s["staff_id"], include_self=False))
            if s["role"] == "staff"
            else None
        )
        return [
            link_view(r)
            for r in c.execute(
                "SELECT * FROM staff WHERE product_id=? ORDER BY created", (pid,)
            )
            if descendants is None or r["id"] in descendants
        ]


@app.post("/api/manage/links")
def delegate_link(body: ProductLinkInput, request: Request):
    s = session(request, ("admin", "staff"))
    with db() as c:
        authorize_management(c, s, "links.delegate")
        return create_product_link(c, body, s)


@app.post("/api/manage/links/{sid}/revoke")
def revoke_managed_link(sid: str, request: Request, product_id: str = ""):
    s = session(request, ("admin", "staff"))
    with db() as c:
        pid = management_scope(c, s, product_id, "links.delegate")
        if s["role"] == "staff" and sid not in link_descendant_ids(
            c, s["staff_id"], include_self=False
        ):
            fail("只能撤销自己创建的下级商品管理链接", 403)
        row = c.execute("SELECT product_id FROM staff WHERE id=?", (sid,)).fetchone()
        if not row:
            fail("商品管理链接不存在", 404)
        if row["product_id"] != pid:
            fail("无权管理此商品的链接", 403)
        return revoke_product_link(c, sid, management_actor(s))


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
        # Default SKU requests retain the exact pre-SKU fingerprint, including
        # existing label/expiry metadata, so old idempotency keys remain valid.
        excluded = {"variant_id"} if body.variant_id == "default" else set()
        if not body.label and body.expires is None:
            excluded.update(("label", "expires"))
        fingerprint = digest(body.model_dump_json(exclude=excluded))
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
        result = {
            "codes": issue_cards(
                c,
                body.product_id,
                body.count,
                label=body.label,
                expires=body.expires,
                variant_id=body.variant_id,
            )
        }
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

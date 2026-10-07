import json
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth, product_lifecycle, shops
from .account_auth import router as account_auth_router
from .automation import router as automation_router
from .batch_redemption import router as batch_redemption_router
from .card_tracking import router as card_tracking_router
from .cli_auth import router as cli_auth_router
from .config import ORIGIN, check_config
from .db import audit, db, event, init, setting
from .device_login import router as device_login_router
from .files import (
    MAX_MULTIPART_BYTES,
    file_limit_message,
    purge_job_files,
    release_output_files,
)
from .files import (
    router as files_router,
)
from .flow_adapter import router as flow_router
from .link_access import augment_link_view, consume_link, revoke_staff_sessions
from .link_access import router as link_access_router
from .link_cleanup import link_state
from .maintenance import router as maintenance_router
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
from .owner_cli_auth import router as owner_cli_router
from .owner_cli_auth import verify_owner_cli_action
from .private_worker import is_upload_path as private_worker_upload_path
from .private_worker import router as private_worker_router
from .processor_profiles import router as processor_profiles_router
from .processors import processor_catalog
from .product_lifecycle import DeleteProduct, EmptyTrash
from .progress_board import router as progress_board_router
from .proxy_routes import router as proxy_routes_router
from .scope_auth import router as scope_auth_router
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
    require_cli_bearer,
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
    card_product,
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
from .text_cards import router as text_cards_router
from .variants import card_variant, issued_variant_ids


@asynccontextmanager
async def lifespan(app):
    check_config()
    init()
    yield


app = FastAPI(title="Extore API", version=package_version("extore"), lifespan=lifespan)
app.include_router(auth.router)
app.include_router(account_auth_router)
app.include_router(batch_redemption_router)
app.include_router(card_tracking_router)
app.include_router(files_router)
app.include_router(flow_router)
app.include_router(source_router)
app.include_router(link_access_router)
app.include_router(cli_auth_router)
app.include_router(device_login_router)
app.include_router(scope_auth_router)
app.include_router(automation_router)
app.include_router(owner_cli_router)
app.include_router(processor_profiles_router)
app.include_router(progress_board_router)
app.include_router(private_worker_router)
app.include_router(proxy_routes_router)
app.include_router(maintenance_router)
app.include_router(text_cards_router)


@app.middleware("http")
async def guard(request: Request, call_next):
    cli_handshake = request.method == "POST" and request.url.path in {
        "/api/cli/authorize",
        "/api/cli/challenge",
        "/api/cli/session",
        "/api/cli/device/request",
        "/api/cli/device/status",
        "/api/cli/device/claim",
        "/api/cli/scopes/request",
        "/api/cli/scopes/status",
        "/api/cli/scopes/claim",
        "/api/cli/owner/request",
        "/api/cli/owner/status",
        "/api/cli/owner/claim",
        "/api/cli/owner/challenge",
        "/api/cli/owner/session",
    }
    cli_authenticated = False
    if (
        request.url.path.startswith("/api/")
        and request.headers.get("authorization") is not None
        and not cli_handshake
        and not request.url.path.startswith(("/api/callbacks/", "/api/integrations/"))
    ):
        try:
            require_cli_bearer(request)
            cli_authenticated = True
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    if (
        request.method not in ("GET", "HEAD", "OPTIONS")
        and request.url.path.startswith("/api/")
        and not request.url.path.startswith("/api/callbacks/")
        and not request.url.path.startswith("/api/integrations/")
    ):
        if request.headers.get("origin") != ORIGIN:
            if request.headers.get("origin") is not None or not (
                cli_handshake or cli_authenticated
            ):
                return JSONResponse({"detail": "请求来源不匹配"}, status_code=403)
    # Streaming bounded read prevents unbounded webhook / JSON memory usage.
    upload = request.url.path in (
        "/api/files/upload",
        "/api/manage/files/upload",
    ) or private_worker_upload_path(request.url.path)
    if (
        upload
        and request.headers.get("content-length", "").isdigit()
        and int(request.headers["content-length"]) > MAX_MULTIPART_BYTES
    ):
        return JSONResponse({"detail": file_limit_message()}, status_code=413)
    if request.method not in ("GET", "HEAD") and not upload:
        maximum_body = 256000
        if request.url.path in (
            "/api/admin/cards/import-text",
            "/api/manage/cards/import-text",
        ):
            from .text_cards import MAX_IMPORT_BYTES

            # JSON escape sequences can be six bytes per decoded character.
            maximum_body = 6 * MAX_IMPORT_BYTES + 4096
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > maximum_body:
                return JSONResponse({"detail": "请求过大"}, status_code=413)
        request._body = bytes(body)
        if (
            cli_authenticated
            and request.method not in ("GET", "HEAD", "OPTIONS")
            and request.url.path != "/api/cli/owner/action-challenge"
        ):
            try:
                verify_owner_cli_action(request, request._body)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers.setdefault(
        "Content-Security-Policy",
        (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
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


@app.get("/api/admin/storage")
def storage_status(request: Request, shop_id: str = ""):
    s = session(request)
    from .storage import storage_usage

    with db() as c:
        authorize_management(c, s)
        return storage_usage(c, shop_id=selected_shop_scope(c, s, shop_id))


def selected_shop_scope(c, s, requested_shop_id=""):
    """Root may select a shop; merchants cannot widen their session scope."""
    own = shops.scoped_shop_id(s)
    if not requested_shop_id:
        return own
    if own is not None and requested_shop_id != own:
        fail("没有此店铺的管理权限", 403)
    shops.shop_row(c, requested_shop_id, require_enabled=False)
    return requested_shop_id


@app.get("/api/products")
def public_products(shop_id: str = ""):
    with db() as c:
        if shop_id:
            shop = c.execute(
                "SELECT id FROM shops WHERE id=? AND enabled=1", (shop_id,)
            ).fetchone()
            if not shop:
                fail("店铺不存在", 404)
        rows = [
            public_product(product(c, r["id"]))
            for r in c.execute(
                "SELECT products.id FROM products JOIN shops ON shops.id=products.shop_id "
                "WHERE shops.enabled=1 AND NOT EXISTS (SELECT 1 FROM product_lifecycle l WHERE l.product_id=products.id AND l.deleted_at IS NOT NULL) AND (?='' OR products.shop_id=?) ORDER BY products.created",
                (shop_id, shop_id),
            )
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
        shops.require_enabled_product(c, card["product_id"])
        row = c.execute("SELECT * FROM jobs WHERE card_id=?", (card["id"],)).fetchone()
        p = job_product(c, row) if row else card_product(c, card)
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
                    job_product(c, row) if row else card_product(c, card)
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
        from .proxy_routes import is_routed_code, unwrap_local_code

        # Preserve the original single-code format, including spaces between
        # its groups, before treating whitespace as a separator between codes.
        if any(is_routed_code(part) for part in re.split(r"[\s,，;；]+", body.code)):
            # Verify every wrapper before deduplication. Case-folding a malformed
            # signature must not discard it as a duplicate of a valid wrapper.
            raw_codes = re.split(r"[\s,，;；]+", body.code.strip())
            codes = []
            seen = set()
            for item in raw_codes:
                if not item:
                    continue
                value = unwrap_local_code(c, item)
                identity = card_digest(value)
                if identity not in seen:
                    seen.add(identity)
                    codes.append(value)
        else:
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
                shops.require_enabled_product(c, card["product_id"])
                submit(c, card, item.params)
            return _batch_receipt(c, body.token)
        if body.items:
            fail("单张卡密请直接提交参数")
        card = grant(c, body.token)
        shops.require_enabled_product(c, card["product_id"])
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
                job_product(c, row) if row else card_product(c, card)
            ),
            "variant": card_variant(c, card),
            "job": job_view(c, row) if row else None,
        }


@app.post("/api/retry")
def retry_original_submission(body: TokenInput, request: Request):
    """Retry only the original frozen inputs after an explicit reuse outcome."""
    rate_limit(request, "retry", 20, 60)
    with db() as c:
        card = resolve_customer_card(c, body.token, body.card_id)
        shops.require_enabled_product(c, card["product_id"])
        row = c.execute("SELECT * FROM jobs WHERE card_id=?", (card["id"],)).fetchone()
        if not row or row["state"] != "needs_input" or row["retry_mode"] != "reuse":
            fail("此任务需要重新填写信息或当前不能重试", 409)
        return job_view(c, submit(c, card, json.loads(row["params"])))


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
        if p["view_policy"] == "once":
            from . import task_flow
            from .text_cards import discard_assignment

            task_flow.destroy(c, row)
            discard_assignment(c, card["id"])
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
        from . import task_flow
        from .text_cards import discard_assignment

        task_flow.destroy(c, row)
        discard_assignment(c, card["id"])
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
def admin_products(
    request: Request,
    compact: bool = False,
    shop_id: str = "",
    view: Literal["active", "deleted", "all", "history"] = "active",
):
    s = session(request)
    with db() as c:
        authorize_management(c, s)
        scope = selected_shop_scope(c, s, shop_id)
        return [
            product_summary(owner_product_view(c, r["id"]))
            if compact
            else owner_product_view(c, r["id"], s)
            for r in c.execute(
                "SELECT id FROM products WHERE (? IS NULL OR shop_id=?) ORDER BY created",
                (scope, scope),
            )
            if product_lifecycle.matches(c, r["id"], view)
        ]


def owner_product_view(c, pid, owner_session=None):
    row = c.execute(
        "SELECT products.shop_id,shops.enabled AS shop_enabled FROM products "
        "JOIN shops ON shops.id=products.shop_id WHERE products.id=?",
        (pid,),
    ).fetchone()
    values = {**product(c, pid), "shop_id": row["shop_id"]}
    if owner_session is not None and values["mode"] == "script" and row["shop_enabled"]:
        from .processor_profiles import product_configuration_view

        values.update(product_configuration_view(c, pid, owner_session))
    return product_lifecycle.decorate(c, values)


def product_summary(p):
    return {
        **{key: p[key] for key in ("id", "name", "mode", "delivery", "view_policy")},
        **(
            {"deleted": True, "deleted_at": p["deleted_at"]} if p.get("deleted") else {}
        ),
        **({"purged": True, "purged_at": p["purged_at"]} if p.get("purged") else {}),
        "parameters_count": len(p["parameters"]),
        "outputs_count": len(p["outputs"]),
        **({"shop_id": p["shop_id"]} if "shop_id" in p else {}),
        "variants": [
            {key: v[key] for key in ("id", "name", "price", "currency", "enabled")}
            for v in p["variants"]
        ],
    }


@app.post("/api/admin/products")
def create_product(body: Product, request: Request, shop_id: str = ""):
    s = session(request)
    pid = str(uuid.uuid4())
    with db() as c:
        authorize_management(c, s)
        shop_id = shops.resolve_create_shop(c, s, shop_id or None)
        values = body.model_dump()
        c.execute(
            "INSERT INTO products(id,config,created,shop_id) VALUES (?,?,?,?)",
            (
                pid,
                json.dumps({**values, "processor_config": {}}, ensure_ascii=False),
                time.time(),
                shop_id,
            ),
        )
        from .processor_profiles import persist_product_configuration

        persist_product_configuration(c, pid, values, actor_session=s)
        c.execute(
            "UPDATE products SET config=? WHERE id=?",
            (json.dumps(values, ensure_ascii=False), pid),
        )
        audit(c, management_actor(s), "product.create", pid)
        return owner_product_view(c, pid, s)


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
def quick_product(body: QuickProductInput, request: Request, shop_id: str = ""):
    s = session(request)
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    name = body.name or "未命名商品 · " + "".join(
        secrets.choice(alphabet) for _ in range(6)
    )
    pid = str(uuid.uuid4())
    with db() as c:
        authorize_management(c, s)
        shop_id = shops.resolve_create_shop(c, s, shop_id or None)
        if body.template_id == "existing_product":
            source_row = shops.authorize_product(c, s, body.from_product_id)
            if source_row["shop_id"] != shop_id:
                fail("商品模板只能复制到同一店铺", 403)
            product_lifecycle.require_active(c, body.from_product_id)
            source = product(c, body.from_product_id)
            values = {key: value for key, value in source.items() if key != "id"}
            # A new configuration link must never inherit the source product's
            # callback signing authority, even when the connector is copied.
            if values["webhook_secret"]:
                values["webhook_secret"] = secrets.token_urlsafe(32)
            # Copy processor structure without the source's delivery secrets.
            values["processor_config"] = {}
            config = Product.model_validate({**values, "name": name, "public": False})
        else:
            template = next(t for t in PRODUCT_TEMPLATES if t["id"] == body.template_id)
            config = Product(
                name=name, mode=template["mode"], delivery=template["delivery"]
            )
        c.execute(
            "INSERT INTO products(id,config,created,shop_id) VALUES (?,?,?,?)",
            (pid, config.model_dump_json(), time.time(), shop_id),
        )
        audit(c, management_actor(s), "product.create", pid)
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
        return {"product": owner_product_view(c, pid, s), "management_link": link}


@app.delete("/api/admin/products/{pid}")
def delete_product(pid: str, body: DeleteProduct, request: Request):
    s = session(request)
    with db() as c:
        authorize_management(c, s)
        shops.authorize_product(c, s, pid)
        return product_lifecycle.set_deleted(c, pid, management_actor(s), True)


@app.post("/api/admin/products/{pid}/restore")
def restore_product(pid: str, request: Request):
    s = session(request)
    with db() as c:
        authorize_management(c, s)
        shops.authorize_product(c, s, pid)
        return product_lifecycle.set_deleted(c, pid, management_actor(s), False)


@app.delete("/api/manage/product")
def delete_managed_product(body: DeleteProduct, request: Request, product_id: str = ""):
    s = session(request, ("admin", "staff"))
    with db() as c:
        pid = management_scope(c, s, product_id, "product.delete")
        return product_lifecycle.set_deleted(c, pid, management_actor(s), True)


@app.post("/api/manage/product/restore")
def restore_managed_product(request: Request, product_id: str = ""):
    s = session(request, ("admin", "staff"))
    with db() as c:
        pid = management_scope(c, s, product_id, "product.delete")
        return product_lifecycle.set_deleted(c, pid, management_actor(s), False)


@app.post("/api/admin/products/{pid}/purge")
def purge_product(pid: str, body: DeleteProduct, request: Request):
    s = session(request)
    with db() as c:
        authorize_management(c, s, "product.purge")
        shops.authorize_product(c, s, pid)
        return product_lifecycle.purge_product(c, pid, management_actor(s))


@app.post("/api/manage/product/purge")
def purge_managed_product(body: DeleteProduct, request: Request, product_id: str = ""):
    s = session(request, ("admin", "staff"))
    with db() as c:
        pid = management_scope(c, s, product_id, "product.purge")
        return product_lifecycle.purge_product(c, pid, management_actor(s))


def _empty_product_trash(c, s, body, *, product_id="", shop_id=""):
    authorize_management(c, s, "product.purge")
    scope = selected_shop_scope(c, s, shop_id)
    if s["role"] == "staff" or product_id:
        pid = queue_product_id(s, product_id)
        if any(candidate != pid for candidate in body.product_ids):
            fail("只能清空当前授权商品的回收站", 403)
    target_shop = scope
    for pid in body.product_ids:
        row = shops.authorize_product(c, s, pid)
        if target_shop is None:
            target_shop = row["shop_id"]
        if row["shop_id"] != target_shop:
            fail("一次清空只能操作同一店铺的商品", 403)
    return product_lifecycle.purge_products(c, body.product_ids, management_actor(s))


@app.post("/api/admin/products/empty-trash")
def empty_product_trash(body: EmptyTrash, request: Request, shop_id: str = ""):
    s = session(request)
    with db() as c:
        return _empty_product_trash(c, s, body, shop_id=shop_id)


@app.post("/api/manage/products/empty-trash")
def empty_managed_product_trash(
    body: EmptyTrash, request: Request, product_id: str = "", shop_id: str = ""
):
    s = session(request, ("admin", "staff"))
    with db() as c:
        return _empty_product_trash(c, s, body, product_id=product_id, shop_id=shop_id)


@app.put("/api/admin/products/{pid}")
def edit_product(pid: str, body: Product, request: Request):
    s = session(request)
    with db() as c:
        authorize_management(c, s)
        shops.authorize_product(c, s, pid)
        save_product(c, pid, body, management_actor(s), s)
        return owner_product_view(c, pid, s)


def save_product(c, pid, body, actor, actor_session=None):
    product_lifecycle.require_active(c, pid)
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
    from .processor_profiles import persist_product_configuration

    values = body.model_dump()
    persist_product_configuration(c, pid, values, actor_session=actor_session)
    c.execute(
        "UPDATE products SET config=? WHERE id=?",
        (json.dumps(values, ensure_ascii=False), pid),
    )
    audit(c, actor, "product.update", pid)


def field_schema(fields):
    return {field["key"]: (field["type"], field["required"]) for field in fields}


@app.post("/api/admin/cards")
def cards(body: IssueCards, request: Request):
    s = session(request)
    with db() as c:
        authorize_management(c, s)
        shops.authorize_product(c, s, body.product_id)
        codes = issue_cards(
            c,
            body.product_id,
            body.count,
            label=body.label,
            expires=body.expires,
            variant_id=body.variant_id,
            routed=body.routed,
        )
        audit(c, management_actor(s), "cards.issue", f"{body.product_id}:{body.count}")
        return {"codes": codes, "batch_id": card_batch_id(c, codes)}


def card_batch_id(c, codes):
    if not codes:
        return None
    from .proxy_routes import unwrap_local_code

    row = c.execute(
        "SELECT card_meta.batch_id FROM card_meta JOIN cards ON cards.id=card_meta.card_id WHERE cards.digest=?",
        (card_digest(unwrap_local_code(c, codes[0])),),
    ).fetchone()
    return row["batch_id"] if row else None


@app.get("/api/admin/cards")
def list_cards(
    request: Request, product_id: str = "", limit: int = 100, shop_id: str = ""
):
    s = session(request)
    with db() as c:
        authorize_management(c, s)
        scope = selected_shop_scope(c, s, shop_id)
        if product_id:
            shops.authorize_product(c, s, product_id)
        return [
            dict(r)
            for r in c.execute(
                "SELECT cards.id,product_id,state,cards.created FROM cards "
                "JOIN products ON products.id=cards.product_id "
                "WHERE (?='' OR product_id=?) AND (? IS NULL OR products.shop_id=?) "
                "ORDER BY cards.created DESC LIMIT ?",
                (
                    product_id,
                    product_id,
                    scope,
                    scope,
                    max(1, min(limit, 500)),
                ),
            )
        ]


@app.post("/api/admin/cards/{cid}/revoke")
def revoke_card(cid: str, request: Request):
    s = session(request)
    with db() as c:
        authorize_management(c, s)
        row = c.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
        if row:
            shops.authorize_product(c, s, row["product_id"])
        if not row or row["state"] != "ready":
            fail("只能撤销尚未兑换的卡密", 409)
        c.execute("UPDATE cards SET state='revoked' WHERE id=?", (cid,))
        c.execute("DELETE FROM grants WHERE card_id=?", (cid,))
        c.execute("DELETE FROM receipt_batch_cards WHERE card_id=?", (cid,))
        audit(c, management_actor(s), "card.revoke", cid)
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
    authorize_management(c, s)
    shops.authorize_product(c, s, pid)
    product_lifecycle.require_active(c, pid)
    now = time.time()
    parent_id = s["staff_id"] if s["role"] == "staff" else None
    if parent_id:
        parent = c.execute(
            "SELECT max_uses,max_cli_uses FROM staff WHERE id=?", (parent_id,)
        ).fetchone()
        scope_parent = c.execute(
            "SELECT device_id FROM pipeline_bindings WHERE staff_id=?", (parent_id,)
        ).fetchone()
        # A key-bound scope has no browser admission itself. Its explicitly
        # approved links.delegate permission may create one-use child links.
        if scope_parent and (
            s.get("channel") != "cli" or s.get("device_id") != scope_parent["device_id"]
        ):
            fail("请通过已授权的流水线 CLI 设备创建下级链接", 403)
        browser_ceiling = 1 if scope_parent else parent["max_uses"]
        if body.max_uses > browser_ceiling:
            fail("下级链接可用次数不能超过当前链接的上限", 403)
        if body.max_cli_uses > parent["max_cli_uses"]:
            fail("下级链接 CLI 绑定次数不能超过当前链接的上限", 403)
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
        "INSERT INTO staff(id,digest,product_id,name,expires,permissions,parent_id,created,max_uses,max_cli_uses) VALUES (?,?,?,?,?,?,?,?,?,?)",
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
            body.max_cli_uses,
        ),
    )
    audit(c, management_actor(s), "staff.create", sid)
    result = link_view(c.execute("SELECT * FROM staff WHERE id=?", (sid,)).fetchone())
    return {**result, "url": ORIGIN + "/staff#" + value}


def revoke_product_link(c, sid, actor):
    if not c.execute("SELECT 1 FROM staff WHERE id=?", (sid,)).fetchone():
        fail("商品管理链接不存在", 404)
    ids = link_descendant_ids(c, sid)
    for target in ids:
        c.execute("UPDATE staff SET revoked=1 WHERE id=?", (target,))
        revoke_staff_sessions(c, target, actor)
        from .flow_adapter import release_actor_tasks

        release_actor_tasks(c, target)
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
def list_staff(
    request: Request,
    view: Literal["active", "history", "all"] = "active",
    shop_id: str = "",
):
    s = session(request)
    with db() as c:
        authorize_management(c, s)
        scope = selected_shop_scope(c, s, shop_id)
        return [
            link_view(r)
            for r in c.execute(
                "SELECT staff.* FROM staff JOIN products ON products.id=staff.product_id "
                "WHERE (? IS NULL OR products.shop_id=?) AND NOT EXISTS "
                "(SELECT 1 FROM pipeline_bindings WHERE staff_id=staff.id) "
                "ORDER BY staff.created",
                (scope, scope),
            )
            if view == "all" or link_state(c, r) == view
        ]


@app.post("/api/admin/staff/{sid}/revoke")
def revoke_staff(sid: str, request: Request):
    s = session(request)
    with db() as c:
        authorize_management(c, s)
        row = c.execute(
            "SELECT product_id FROM staff WHERE id=? AND NOT EXISTS "
            "(SELECT 1 FROM pipeline_bindings WHERE staff_id=staff.id)",
            (sid,),
        ).fetchone()
        if not row:
            fail("商品管理链接不存在", 404)
        shops.authorize_product(c, s, row["product_id"])
        return revoke_product_link(c, sid, management_actor(s))


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
def managed_products(
    request: Request,
    compact: bool = False,
    shop_id: str = "",
    view: Literal["active", "deleted", "all", "history"] = "active",
):
    s = session(request, ("admin", "staff"))
    with db() as c:
        queue_staff_authorization(c, s)
        shop_scope = selected_shop_scope(c, s, shop_id)
        scope = s["product_id"] if s["role"] == "staff" else ""
        rows = c.execute(
            "SELECT * FROM products WHERE (?='' OR id=?) "
            "AND (? IS NULL OR shop_id=?) ORDER BY created",
            (scope, scope, shop_scope, shop_scope),
        ).fetchall()
        result = []
        for r in rows:
            if not product_lifecycle.matches(c, r["id"], view):
                continue
            p = product_lifecycle.decorate(c, product(c, r["id"]))
            if compact:
                result.append(product_summary(p))
                continue
            result.append(
                {
                    **{
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
                    },
                    **product_lifecycle.metadata(c, r["id"]),
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
    compact: bool = False,
    shop_id: str = "",
):
    s = session(request, ("admin", "staff"))
    with db() as c:
        authorize_management(c, s, "queue.view")
        scope = selected_shop_scope(c, s, shop_id)
        product_id = queue_product_id(s, product_id)
        ownership = shops.authorize_product(c, s, product_id)
        if scope is not None and ownership["shop_id"] != scope:
            fail("商品不属于所选店铺", 403)
        conditions = ["product_id=?"]
        values = [product_id]
        if state:
            conditions.append("state=?")
            values.append(state)
        elif not job_id:
            if view == "active":
                conditions.append(
                    "state IN ('queued','processing','waiting','failed','needs_input')"
                )
            elif view == "processed":
                conditions.append("state IN ('succeeded','destroyed','rejected')")
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
        if not compact:
            return [job_view(c, r, True) for r in rows]
        summaries = []
        keys = (
            "id",
            "product_id",
            "state",
            "message",
            "progress",
            "attempt",
            "created",
            "updated",
            "variant",
            "steps",
            "completed_steps",
            "queue_position",
            "queue_ahead",
            "can_retry",
            "retry_mode",
            "retry_reason_type",
        )
        for row in rows:
            details = job_view(c, row)
            summary = {key: details[key] for key in keys if key in details}
            summary["claimed_by"] = row["claimed_by"]
            summary["attachments"] = {
                kind: {
                    "count": metadata["count"],
                    "bytes": metadata["bytes"],
                }
                for kind in ("input", "output")
                for metadata in [
                    c.execute(
                        "SELECT COUNT(*) AS count,COALESCE(SUM(size),0) AS bytes "
                        "FROM job_files WHERE job_id=? AND kind=? AND content IS NOT NULL",
                        (row["id"], kind),
                    ).fetchone()
                ]
            }
            summaries.append(summary)
        return summaries


@app.post("/api/manage/batch")
def batch(body: BatchUpdate, request: Request):
    s = session(request, ("admin", "staff"))
    actor = management_actor(s)
    with db() as c:
        authorize_management(
            c, s, "queue.retry" if body.action == "retry" else "queue.process"
        )
        if body.progress_steps is not None:
            authorize_management(c, s, "queue.process")
        product_id = queue_product_id(s, body.product_id)
        shops.authorize_product(c, s, product_id)
        p = product(c, product_id)
        rows = [job(c, jid) for jid in dict.fromkeys(body.ids)]
        if any(r["product_id"] != product_id for r in rows):
            fail("批处理只能包含所选商品的任务", 403)
        if set(body.flow_scopes) - set(body.ids):
            fail("流程身份包含未选择的任务")
        for r in rows:
            jid = r["id"]
            if body.attempt is not None and body.attempt != r["attempt"]:
                fail("提交对应的任务尝试已失效", 409)
            scope = body.flow_scopes.get(jid, {})
            if set(scope) - {"flow_epoch", "action_id", "attempt"}:
                fail("流程身份字段无效")
            epoch = scope.get("flow_epoch", body.flow_epoch)
            action_id = scope.get("action_id", body.action_id)
            if scope.get("attempt", r["attempt"]) != r["attempt"]:
                fail("提交对应的流程尝试已失效", 409)
            if epoch is not None and (type(epoch) is not int or epoch < 1):
                fail("流程步骤身份无效")
            from . import task_flow

            if task_flow.is_flow(c, r) and body.action != "retry":
                current = task_flow.view(c, r)
                if epoch is None or epoch != current["flow_epoch"]:
                    fail("提交对应的流程步骤已失效", 409)
                authority = task_flow.frozen_authority(c, r, epoch, r["attempt"])
                if current["phase"] not in ("queued", "processing"):
                    fail("当前流程步骤不能由队列处理", 409)
                if action_id is not None and action_id != authority["action_id"]:
                    fail("提交对应的流程动作已失效", 409)
            if p["mode"] != "manual" and body.action != "retry":
                fail("自动处理任务不能由队列处理覆盖", 409)
            if body.progress_steps is not None:
                bootstrap_progress_plan(
                    c, r, [step.model_dump() for step in body.progress_steps]
                )
                r = job(c, jid)
            if body.action == "claim":
                from . import task_flow

                if task_flow.is_flow(c, r):
                    if epoch is None:
                        fail("领取流程步骤必须指定 flow_epoch", 409)
                    from .service import finalize_task_flow

                    finalize_task_flow(c, task_flow.claim(c, r, actor, epoch))
                    from .agent_identity import record_claim

                    claimed = job(c, jid)
                    if (
                        claimed["state"] == "processing"
                        and claimed["claimed_by"] == actor
                    ):
                        record_claim(c, claimed, actor, session=s)
                    audit(c, actor, "job.claim", jid)
                    continue
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
                from .agent_identity import record_claim

                record_claim(c, job(c, jid), actor, session=s)
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
                        flow_epoch=epoch,
                        action_id=action_id,
                    ),
                )
            elif body.action in ("request_changes", "request_retry", "reject"):
                from .task_outcomes import apply_queue_outcome

                apply_queue_outcome(
                    c,
                    jid,
                    body.action,
                    body.message,
                    actor,
                    retry_mode=body.retry_mode,
                    reason_type=body.reason_type,
                )
                from . import task_flow

                if task_flow.is_flow(c, r):
                    from .service import finalize_task_flow

                    finalize_task_flow(c, task_flow.stop_for_outcome(c, job(c, jid)))
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
    shops.authorize_product(c, s, pid)
    return pid


def management_actor(s):
    if s["role"] == "staff":
        return s["staff_id"]
    return s.get("account_id") or (
        "shop:" + s["shop_id"] if s.get("shop_id") is not None else "owner"
    )


@app.get("/api/manage/product")
def managed_product(request: Request, product_id: str = ""):
    s = session(request, ("admin", "staff"))
    with db() as c:
        pid = management_scope(c, s, product_id, "product.edit")
        p = (
            owner_product_view(c, pid, s)
            if s["role"] == "admin"
            else product_lifecycle.decorate(c, product(c, pid))
        )
        return managed_product_view(p, s)


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
        if s["role"] == "staff" and any(
            values[field] != old[field]
            for field in ("processor_id", "processor_config")
        ):
            fail("商品管理链接不能选择处理器账户或修改店铺凭证", 403)
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
                "task_flow",
            ):
                if values[field] != old[field]:
                    fail("修改发货、查看或重试配置需要配置发货权限", 403)
            if field_schema(values["outputs"]) != field_schema(old["outputs"]):
                fail("修改输出字段架构需要配置发货权限", 403)
        try:
            updated = Product.model_validate(values)
        except ValueError:
            fail("商品配置格式错误", 422)
        save_product(c, pid, updated, management_actor(s), s)
        p = owner_product_view(c, pid, s) if s["role"] == "admin" else product(c, pid)
        return managed_product_view(p, s)


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
            routed=body.routed,
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
            "UPDATE outbox SET state='pending',attempts=0,due=?,finished_at=NULL WHERE id=?",
            (time.time(), eid),
        )
        audit(c, management_actor(s), "webhook.retry", eid)
    return {"ok": True}


@app.get("/api/manage/links")
def managed_links(
    request: Request,
    product_id: str = "",
    view: Literal["active", "history", "all"] = "active",
):
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
                "SELECT * FROM staff WHERE product_id=? AND NOT EXISTS "
                "(SELECT 1 FROM pipeline_bindings WHERE staff_id=staff.id) "
                "ORDER BY created",
                (pid,),
            )
            if (descendants is None or r["id"] in descendants)
            and (view == "all" or link_state(c, r) == view)
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
        row = c.execute(
            "SELECT product_id FROM staff WHERE id=? AND NOT EXISTS "
            "(SELECT 1 FROM pipeline_bindings WHERE staff_id=staff.id)",
            (sid,),
        ).fetchone()
        if not row:
            fail("商品管理链接不存在", 404)
        if row["product_id"] != pid:
            fail("无权管理此商品的链接", 403)
        return revoke_product_link(c, sid, management_actor(s))


@app.get("/api/admin/events")
def events(request: Request, shop_id: str = ""):
    s = session(request)
    with db() as c:
        authorize_management(c, s)
        scope = selected_shop_scope(c, s, shop_id)
        return [
            dict(r)
            for r in c.execute(
                "SELECT events.id,type,job_id,events.created,outbox.state AS webhook_state,attempts,error "
                "FROM events JOIN products ON products.id=events.product_id "
                "LEFT JOIN outbox ON outbox.id=events.id "
                "WHERE (? IS NULL OR products.shop_id=?) ORDER BY events.created DESC LIMIT 100",
                (scope, scope),
            )
        ]


@app.post("/api/admin/events/{eid}/retry")
def retry_event(eid: str, request: Request):
    s = session(request)
    with db() as c:
        authorize_management(c, s)
        row = c.execute(
            "SELECT events.product_id,outbox.state FROM events "
            "LEFT JOIN outbox ON outbox.id=events.id WHERE events.id=?",
            (eid,),
        ).fetchone()
        if row:
            shops.authorize_product(c, s, row["product_id"])
        if not row or row["state"] != "dead":
            fail("只能重试已停止投递的事件", 409)
        c.execute(
            "UPDATE outbox SET state='pending',attempts=0,due=?,finished_at=NULL WHERE id=?",
            (time.time(), eid),
        )
        audit(c, management_actor(s), "webhook.retry", eid)
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
        from . import task_flow

        if task_flow.is_flow(c, job(c, jid)):
            fail("步骤流程必须使用绑定执行身份的私有 Worker v2 回调", 409)
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
        secret = request.headers.get("authorization", "").removeprefix("Bearer ")
        secret_digest = digest(secret)
        scoped_key = c.execute(
            "SELECT shop_id FROM shop_integration_keys WHERE key_digest=?",
            (secret_digest,),
        ).fetchone()
        legacy = False
        if scoped_key:
            shop_id = scoped_key["shop_id"]
        else:
            expected = setting(c, "integration_key")
            if not expected or not secrets.compare_digest(secret_digest, expected):
                fail("平台密钥无效", 401)
            shop_id = shops.default_shop(c)
            legacy = True
        shops.shop_row(c, shop_id)
        target = shops.require_enabled_product(c, body.product_id)
        if target["shop_id"] != shop_id:
            fail("平台密钥无权发行此店铺的卡密", 403)
        if not secret:
            fail("平台密钥无效", 401)
        key = request.headers.get("idempotency-key", "")
        if not 8 <= len(key) <= 200:
            fail("请提供 8–200 字符的 Idempotency-Key", 400)
        # Default SKU requests retain the exact pre-SKU fingerprint, including
        # existing label/expiry metadata, so old idempotency keys remain valid.
        excluded = {"variant_id"} if body.variant_id == "default" else set()
        if body.routed is None:
            excluded.add("routed")
        if not body.label and body.expires is None:
            excluded.update(("label", "expires"))
        fingerprint = digest(body.model_dump_json(exclude=excluded))
        request_key = (
            digest(key)
            if legacy
            else digest("shop:" + shop_id + ":" + secret_digest + ":" + key)
        )
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
                routed=body.routed,
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
        audit(
            c,
            "platform" if legacy else "shop:" + shop_id,
            "cards.issue",
            f"{body.product_id}:{body.count}",
        )
        return result


@app.get("/api/shop/integration-key")
def integration_key_status(request: Request, shop_id: str = ""):
    s = session(request)
    with db() as c:
        authorize_management(c, s)
        shop_id = shops.resolve_create_shop(c, s, shop_id or None)
        row = c.execute(
            "SELECT created FROM shop_integration_keys WHERE shop_id=?", (shop_id,)
        ).fetchone()
        return {
            "shop_id": shop_id,
            "configured": row is not None,
            "created": row["created"] if row else None,
        }


def _integration_key_owner(s, c, shop_id):
    from .account_auth import require_recent

    authorize_management(c, s)
    if s.get("channel") != "cli":
        require_recent(s)
    return s, shops.resolve_create_shop(c, s, shop_id or None)


@app.post("/api/shop/integration-key")
def rotate_integration_key(request: Request, shop_id: str = ""):
    s = session(request)
    with db() as c:
        s, shop_id = _integration_key_owner(s, c, shop_id)
        value = token()
        c.execute(
            "INSERT INTO shop_integration_keys(shop_id,key_digest,created) VALUES (?,?,?) "
            "ON CONFLICT(shop_id) DO UPDATE SET key_digest=excluded.key_digest,created=excluded.created",
            (shop_id, digest(value), time.time()),
        )
        audit(c, management_actor(s), "integration_key.rotate", shop_id)
        return {"shop_id": shop_id, "key": value}


@app.delete("/api/shop/integration-key")
def revoke_integration_key(request: Request, shop_id: str = ""):
    s = session(request)
    with db() as c:
        s, shop_id = _integration_key_owner(s, c, shop_id)
        c.execute("DELETE FROM shop_integration_keys WHERE shop_id=?", (shop_id,))
        audit(c, management_actor(s), "integration_key.revoke", shop_id)
        return {"ok": True}


STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/admin", methods=["GET", "HEAD"])
@app.api_route("/staff", methods=["GET", "HEAD"])
@app.api_route("/receipt", methods=["GET", "HEAD"])
@app.api_route("/proxy", methods=["GET", "HEAD"])
@app.api_route("/cli/owner", methods=["GET", "HEAD"])
@app.api_route("/cli/device", methods=["GET", "HEAD"])
@app.api_route("/account", methods=["GET", "HEAD"])
@app.api_route("/account/login", methods=["GET", "HEAD"])
@app.api_route("/account/invite", methods=["GET", "HEAD"])
@app.api_route("/account/register", methods=["GET", "HEAD"])
@app.api_route("/account/reset", methods=["GET", "HEAD"])
@app.api_route("/account/security", methods=["GET", "HEAD"])
def index():
    return FileResponse(STATIC / "index.html")

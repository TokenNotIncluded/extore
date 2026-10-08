"""OAuth 2.0 + S256 PKCE for merchant-approved product and new-stock import.

Commerce credentials never become management sessions. No callback URL is
resolved or fetched, and only separately approved issuance quotas can mint cards.
"""

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import ValidationError

from .commerce_models import (
    MAX_PRODUCTS,
    SCOPES,
    Approval,
    ClientRegistration,
    Consent,
    Issue,
    identifiers,
)
from .commerce_store import (
    CODE_TTL,
    MAX_ACTIVE_CLIENTS,
    MAX_ACTIVE_GRANTS,
    RECOVERY_TTL,
    REQUEST_TTL,
    CommerceError,
    actor_id,
    canonical,
    cleanup,
    client_row,
    client_view,
    fail,
    grant_row,
    grant_view,
    issue_tokens,
    metadata,
    request_context,
    revoke_grant,
)
from .config import ORIGIN
from .db import audit, db
from .security import (
    authorize_management,
    card_digest,
    digest,
    rate_limit,
    session,
    token,
)
from .shops import resolve_create_shop

router = APIRouter(tags=["Commerce import"])
MACHINE_PATHS = frozenset(
    {
        "/api/integrations/commerce/token",
        "/api/integrations/commerce/revoke",
        "/api/integrations/commerce/cards",
    }
)
RESOURCE_PREFIX = "/api/integrations/commerce/products"


def machine_path(path):
    return (
        path in MACHINE_PATHS
        or path == RESOURCE_PREFIX
        or path.startswith(RESOURCE_PREFIX + "/")
    )


async def error_response(request, exc):
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    if exc.status in (401, 403) and exc.error in (
        "invalid_token",
        "insufficient_scope",
    ):
        headers["WWW-Authenticate"] = f'Bearer error="{exc.error}"'
    return JSONResponse(
        {
            "error": exc.error,
            "error_description": exc.description,
            "detail": exc.description,
        },
        status_code=exc.status,
        headers=headers,
    )


def _browser(request, fresh=False):
    if request.headers.get("authorization") is not None:
        fail("access_denied", "请由商家在浏览器中管理商城授权", 401)
    actor = session(request)
    if actor.get("channel") != "browser" or actor["role"] != "admin":
        fail("access_denied", "此操作需要商家浏览器登录", 403)
    if fresh:
        from .account_auth import require_recent

        require_recent(actor)
    return actor


def _management(request, fresh=False):
    """Owner CLI has its established signed writes; commerce tokens never do."""
    actor = session(request)
    if actor["role"] != "admin":
        fail("access_denied", "此操作需要商家管理权限", 403)
    if fresh:
        from .account_auth import require_recent

        require_recent(actor)
    return actor


def _scope(c, actor, shop_id):
    authorize_management(c, actor)
    return resolve_create_shop(c, actor, shop_id or None)


def _owned_client(c, actor, client_id, shop_id="", active=True):
    row = client_row(c, client_id, active=active)
    own = _scope(c, actor, shop_id or row["shop_id"])
    if row["shop_id"] != own:
        fail("access_denied", "没有此商城应用的管理权限", 403)
    return row


def _pending(c, request_id):
    row = c.execute(
        "SELECT * FROM commerce_requests WHERE id=?", (request_id,)
    ).fetchone()
    if row is None or row["expires"] <= time.time() or row["status"] != "pending":
        fail("invalid_request", "授权申请无效、已到期或已处理", 409)
    return row


def _approval_context(c, actor, request_id, shop_id):
    row = _pending(c, request_id)
    _owned_client(c, actor, row["client_id"], shop_id)
    return row, request_context(c, row)


def _redirect(row, **response):
    parsed = urlsplit(row["redirect_uri"])
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend({**response, "state": row["state"], "iss": ORIGIN}.items())
    return urlunsplit(parsed._replace(query=urlencode(query)))


@router.get("/.well-known/oauth-authorization-server")
def discovery():
    return metadata()


@router.get("/api/integrations/commerce/schema")
def schema():
    from .commerce_schema import commerce_schema

    return commerce_schema()


@router.get("/api/admin/commerce/clients")
def clients(
    request: Request, shop_id: str = "", view: Literal["active", "all"] = "active"
):
    actor = _management(request)
    with db() as c:
        own = _scope(c, actor, shop_id)
        cleanup(c)
        return {
            "clients": [
                client_view(row)
                for row in c.execute(
                    "SELECT * FROM commerce_clients WHERE shop_id=? "
                    "AND (?='all' OR revoked=0) ORDER BY created DESC,id LIMIT 200",
                    (own, view),
                )
            ]
        }


@router.post("/api/admin/commerce/clients")
def register_client(body: ClientRegistration, request: Request, shop_id: str = ""):
    actor = _management(request, fresh=True)
    rate_limit(request, "commerce-client-register", 20, 60)
    with db() as c:
        own = _scope(c, actor, shop_id)
        cleanup(c)
        count = c.execute(
            "SELECT COUNT(*) FROM commerce_clients WHERE shop_id=? AND revoked=0",
            (own,),
        ).fetchone()[0]
        if count >= MAX_ACTIVE_CLIENTS:
            fail("invalid_request", "此店铺商城应用数量已达上限", 409)
        cid = str(uuid.uuid4())
        c.execute(
            "INSERT INTO commerce_clients(id,shop_id,metadata,created) VALUES (?,?,?,?)",
            (cid, own, canonical(body.model_dump()), time.time()),
        )
        audit(c, actor_id(actor), "commerce.client.create", cid)
        return client_view(client_row(c, cid))


@router.delete("/api/admin/commerce/clients/{client_id}")
def remove_client(client_id: str, request: Request, shop_id: str = ""):
    actor = _management(request, fresh=True)
    with db() as c:
        _owned_client(c, actor, client_id, shop_id, active=False)
        for grant in c.execute(
            "SELECT id FROM commerce_grants WHERE client_id=? AND revoked=0",
            (client_id,),
        ).fetchall():
            revoke_grant(c, grant["id"], actor_id(actor))
        c.execute("UPDATE commerce_clients SET revoked=1 WHERE id=?", (client_id,))
        c.execute(
            "UPDATE commerce_requests SET status='denied' WHERE client_id=? AND status='pending'",
            (client_id,),
        )
        audit(c, actor_id(actor), "commerce.client.revoke", client_id)
    return {"ok": True}


@router.get("/oauth/authorize")
def authorize(request: Request):
    rate_limit(request, "commerce-authorize", 60, 60)
    pairs = request.query_params.multi_items()
    if len(pairs) > 20 or len({key for key, _ in pairs}) != len(pairs):
        fail("invalid_request", "授权参数重复或过多")
    params = dict(pairs)
    if params.get("response_type") != "code":
        fail("unsupported_response_type", "仅支持授权码模式")
    if params.get("code_challenge_method") != "S256" or not re.fullmatch(
        r"[A-Za-z0-9_-]{43}", params.get("code_challenge", "")
    ):
        fail("invalid_request", "授权必须使用 S256 PKCE")
    state = params.get("state", "")
    if not 16 <= len(state) <= 512 or any(not 32 <= ord(ch) <= 126 for ch in state):
        fail("invalid_request", "请提供有效的随机 state")
    scopes = params.get("scope", "").split(" ")
    if not scopes or len(set(scopes)) != len(scopes) or set(scopes) - set(SCOPES):
        fail("invalid_scope", "申请了未支持的商城权限")
    scopes = [scope for scope in SCOPES if scope in scopes]
    try:
        product_ids = (
            identifiers(params["product_ids"].split(","))
            if params.get("product_ids")
            else []
        )
    except ValueError:
        fail("invalid_request", "申请的商品编号无效")
    if len(product_ids) > MAX_PRODUCTS:
        fail("invalid_request", "单次授权最多 100 件商品")
    with db() as c:
        client = client_row(c, params.get("client_id", ""))
        if (
            params.get("redirect_uri")
            not in json.loads(client["metadata"])["redirect_uris"]
        ):
            fail("invalid_request", "回调地址与已登记地址不匹配")
        # No external redirect is performed until an authenticated merchant has
        # explicitly approved or denied this exact client request.
        cleanup(c)
        if (
            c.execute(
                "SELECT COUNT(*) FROM commerce_requests WHERE client_id=? AND status='pending' "
                "AND expires>?",
                (client["id"], time.time()),
            ).fetchone()[0]
            >= 40
        ):
            fail("temporarily_unavailable", "此商城待批准申请过多，请稍后重试", 429)
        rid, now = token(), time.time()
        c.execute(
            "INSERT INTO commerce_requests(id,client_id,redirect_uri,state,code_challenge,"
            "scopes,product_ids,created,expires) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                rid,
                client["id"],
                params["redirect_uri"],
                state,
                params["code_challenge"],
                canonical(scopes),
                canonical(product_ids),
                now,
                now + REQUEST_TTL,
            ),
        )
    return RedirectResponse(ORIGIN + "/connect/authorize#" + rid, status_code=303)


@router.get("/api/admin/commerce/requests/{request_id}")
def consent_context(request_id: str, request: Request, shop_id: str = ""):
    actor = _browser(request)
    with db() as c:
        _, context = _approval_context(c, actor, request_id, shop_id)
        return context


@router.post("/api/admin/commerce/requests/{request_id}/approve")
def approve(request_id: str, body: Approval, request: Request):
    actor = _browser(request, fresh=True)
    rate_limit(request, "commerce-consent", 20, 60)
    with db() as c:
        row, context = _approval_context(c, actor, request_id, body.shop_id)
        if not hmac.compare_digest(body.review_digest, context["review_digest"]):
            fail("review_changed", "商城申请或商品资料已变化，请重新查看并批准", 409)
        if set(body.scopes) - set(json.loads(row["scopes"])):
            fail("invalid_scope", "不能批准商城未申请的权限")
        allowed = {item["id"]: item for item in context["products"]}
        if set(body.product_ids) - set(allowed):
            fail("access_denied", "不能批准未展示或跨店铺的商品", 403)
        for limit in body.card_limits:
            if not any(
                variant["id"] == limit.variant_id and variant["enabled"]
                for variant in allowed[limit.product_id]["variants"]
            ):
                fail("invalid_request", "发行额度包含不存在或已停用的规格")
            from .service import product

            if product(c, limit.product_id)["mode"] == "stock":
                fail(
                    "unsupported_product",
                    "一卡一文本商品须先由商家导入内容，不能通过此协议生成库存",
                    409,
                )
        now = time.time()
        if (
            not now + 60
            <= body.grant_expires
            <= context["request"]["max_grant_expires"]
        ):
            fail("invalid_request", "授权有效期须在一分钟至九十天内")
        if (
            c.execute(
                "SELECT COUNT(*) FROM commerce_grants WHERE shop_id=? AND revoked=0 AND expires>?",
                (body.shop_id, now),
            ).fetchone()[0]
            >= MAX_ACTIVE_GRANTS
        ):
            fail("invalid_request", "此店铺有效商城授权数量已达上限", 409)
        gid, code = str(uuid.uuid4()), token()
        limits = [
            dict(limit.model_dump(), issued_count=0) for limit in body.card_limits
        ]
        c.execute(
            "INSERT INTO commerce_grants(id,client_id,shop_id,request_id,scopes,issuer_role,"
            "product_ids,card_limits,created,expires,code_digest,code_expires,redirect_uri,"
            "code_challenge) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                gid,
                row["client_id"],
                body.shop_id,
                row["id"],
                canonical(body.scopes),
                "shop" if actor.get("shop_id") else "root",
                canonical(body.product_ids),
                canonical(limits),
                now,
                body.grant_expires,
                digest(code),
                now + CODE_TTL,
                row["redirect_uri"],
                row["code_challenge"],
            ),
        )
        c.execute(
            "UPDATE commerce_requests SET status='approved' WHERE id=?", (row["id"],)
        )
        audit(c, actor_id(actor), "commerce.grant.approve", gid)
        return {"ok": True, "redirect_uri": _redirect(row, code=code)}


@router.post("/api/admin/commerce/requests/{request_id}/deny")
def deny(request_id: str, body: Consent, request: Request):
    actor = _browser(request, fresh=True)
    with db() as c:
        row, context = _approval_context(c, actor, request_id, body.shop_id)
        if not hmac.compare_digest(body.review_digest, context["review_digest"]):
            fail("review_changed", "申请已变化，请重新查看", 409)
        c.execute(
            "UPDATE commerce_requests SET status='denied' WHERE id=?", (row["id"],)
        )
        audit(c, actor_id(actor), "commerce.request.deny", row["id"])
        return {"ok": True, "redirect_uri": _redirect(row, error="access_denied")}


@router.get("/api/admin/commerce/grants")
def grants(
    request: Request, shop_id: str = "", view: Literal["active", "all"] = "active"
):
    actor = _management(request)
    with db() as c:
        own = _scope(c, actor, shop_id)
        cleanup(c)
        return {
            "grants": [
                grant_view(c, row)
                for row in c.execute(
                    "SELECT * FROM commerce_grants WHERE shop_id=? "
                    "AND (?='all' OR (revoked=0 AND expires>?)) "
                    "ORDER BY created DESC,id LIMIT 200",
                    (own, view, time.time()),
                )
            ]
        }


@router.delete("/api/admin/commerce/grants/{grant_id}")
def remove_grant(grant_id: str, request: Request, shop_id: str = ""):
    actor = _management(request, fresh=True)
    with db() as c:
        row = grant_row(c, grant_id, active=False)
        _owned_client(c, actor, row["client_id"], shop_id, active=False)
        revoke_grant(c, row["id"], actor_id(actor))
    return {"ok": True}


def _machine(request, bearer=False):
    if request.cookies.get("extore_session"):
        fail("invalid_request", "商城接口不能混用浏览器登录凭证", 401)
    authorization = request.headers.get("authorization")
    if bearer:
        if authorization is None or not re.fullmatch(
            r"(?i:Bearer) +[A-Za-z0-9_-]{43}", authorization
        ):
            fail("invalid_token", "请提供有效的商城访问令牌", 401)
        return authorization.split(" ")[-1]
    if authorization is not None:
        fail("invalid_client", "此协议使用 PKCE 公共客户端，不接受其他认证方式", 401)


async def _form(request):
    _machine(request)
    if (
        request.headers.get("content-type", "").split(";", 1)[0].lower()
        != "application/x-www-form-urlencoded"
    ):
        fail("invalid_request", "令牌接口须使用 application/x-www-form-urlencoded")
    raw = await request.body()
    if len(raw) > 8192:
        fail("invalid_request", "令牌请求过大", 413)
    try:
        pairs = parse_qsl(
            raw.decode("ascii"),
            keep_blank_values=True,
            max_num_fields=20,
            errors="strict",
        )
    except (ValueError, UnicodeError):
        fail("invalid_request", "令牌请求格式无效")
    if len({key for key, _ in pairs}) != len(pairs):
        fail("invalid_request", "令牌参数不能重复")
    return dict(pairs)


def _grant_client(c, gid, cid):
    grant = grant_row(c, gid)
    if grant["client_id"] != cid:
        fail("invalid_grant", "授权与商城应用不匹配")
    return grant


@router.post("/api/integrations/commerce/token")
async def token_endpoint(request: Request):
    rate_limit(request, "commerce-token", 120, 60)
    body = await _form(request)
    kind, cid = body.get("grant_type"), body.get("client_id", "")
    if kind not in ("authorization_code", "refresh_token"):
        fail("unsupported_grant_type", "只支持授权码和刷新令牌")
    result = None
    with db() as c:
        client_row(c, cid)
        cleanup(c)
        if kind == "authorization_code":
            code, verifier = body.get("code", ""), body.get("code_verifier", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", code) or not re.fullmatch(
                r"[A-Za-z0-9._~-]{43,128}", verifier
            ):
                fail("invalid_grant", "授权码或 PKCE 校验信息无效")
            grant = c.execute(
                "SELECT * FROM commerce_grants WHERE code_digest=?", (digest(code),)
            ).fetchone()
            if grant is None:
                fail("invalid_grant", "授权码无效或已到期")
            try:
                grant = _grant_client(c, grant["id"], cid)
            except CommerceError:
                fail("invalid_grant", "授权码无效或已到期")
            challenge = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(verifier.encode("ascii")).digest()
                )
                .decode()
                .rstrip("=")
            )
            if body.get("redirect_uri") != grant[
                "redirect_uri"
            ] or not hmac.compare_digest(challenge, grant["code_challenge"]):
                fail("invalid_grant", "授权码、回调地址或 PKCE 校验信息不匹配")
            if grant["code_used"] is not None:
                revoke_grant(c, grant["id"], "commerce:" + cid, "commerce.code.replay")
            elif grant["code_expires"] <= time.time():
                fail("invalid_grant", "授权码无效或已到期")
            else:
                c.execute(
                    "UPDATE commerce_grants SET code_used=? WHERE id=?",
                    (time.time(), grant["id"]),
                )
                result = issue_tokens(c, grant)
                audit(c, "commerce:" + cid, "commerce.token.exchange", grant["id"])
        else:
            refresh = body.get("refresh_token", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", refresh):
                fail("invalid_grant", "刷新令牌无效或已到期")
            stored = c.execute(
                "SELECT * FROM commerce_tokens WHERE digest=? AND kind='refresh'",
                (digest(refresh),),
            ).fetchone()
            if stored is None or stored["expires"] <= time.time():
                fail("invalid_grant", "刷新令牌无效或已到期")
            try:
                grant = _grant_client(c, stored["grant_id"], cid)
            except CommerceError:
                fail("invalid_grant", "刷新令牌无效或已到期")
            if stored["used"] is not None:
                revoke_grant(
                    c, grant["id"], "commerce:" + cid, "commerce.refresh.replay"
                )
            else:
                requested = body.get("scope")
                if requested is not None and set(requested.split(" ")) != set(
                    json.loads(grant["scopes"])
                ):
                    fail("invalid_scope", "刷新不能改变已批准的权限")
                c.execute(
                    "UPDATE commerce_tokens SET used=? WHERE digest=?",
                    (time.time(), stored["digest"]),
                )
                result = issue_tokens(c, grant)
                audit(c, "commerce:" + cid, "commerce.token.refresh", grant["id"])
    # Reuse revocation has committed before returning the error.
    if result is None:
        fail("invalid_grant", "令牌或授权码已使用，商城授权已撤销")
    return JSONResponse(
        result, headers={"Cache-Control": "no-store", "Pragma": "no-cache"}
    )


@router.post("/api/integrations/commerce/revoke")
async def revoke_endpoint(request: Request):
    rate_limit(request, "commerce-revoke", 120, 60)
    body = await _form(request)
    raw, cid = body.get("token", ""), body.get("client_id", "")
    with db() as c:
        # RFC 7009: nonexistent, malformed or unrelated tokens also return 200.
        stored = c.execute(
            "SELECT t.grant_id FROM commerce_tokens t JOIN commerce_grants g ON g.id=t.grant_id "
            "WHERE t.digest=? AND g.client_id=?",
            (digest(raw), cid),
        ).fetchone()
        if stored is not None:
            revoke_grant(c, stored["grant_id"], "commerce:" + cid)
    return Response(
        status_code=200, headers={"Cache-Control": "no-store", "Pragma": "no-cache"}
    )


def _access(c, request, scope):
    raw = _machine(request, bearer=True)
    stored = c.execute(
        "SELECT * FROM commerce_tokens WHERE digest=? AND kind='access' AND expires>?",
        (digest(raw), time.time()),
    ).fetchone()
    if stored is None:
        fail("invalid_token", "商城访问令牌无效或已到期", 401)
    grant = grant_row(c, stored["grant_id"])
    if scope not in json.loads(grant["scopes"]):
        fail("insufficient_scope", "商城授权未允许此操作", 403)
    cleanup(c)
    c.execute(
        "UPDATE commerce_grants SET last_used=? WHERE id=?", (time.time(), grant["id"])
    )
    return grant


def _product(c, grant, pid):
    from .product_lifecycle import metadata as lifecycle
    from .service import product

    if pid not in json.loads(grant["product_ids"]):
        fail("access_denied", "商品不在已批准范围内", 403)
    row = c.execute("SELECT shop_id FROM products WHERE id=?", (pid,)).fetchone()
    if row is None or row["shop_id"] != grant["shop_id"] or lifecycle(c, pid):
        fail("product_unavailable", "商品已停用或不在当前店铺", 409)
    return product(c, pid)


@router.get("/api/integrations/commerce/products")
def catalog(request: Request):
    from .commerce_listing import commerce_listing
    from .product_lifecycle import metadata as lifecycle
    from .shops import shop_row

    rate_limit(request, "commerce-catalog", 120, 60)
    with db() as c:
        grant = _access(c, request, "products.read")
        shop = shop_row(c, grant["shop_id"])
        values = []
        for pid in json.loads(grant["product_ids"]):
            if lifecycle(c, pid):
                continue
            _product(c, grant, pid)
            values.append(commerce_listing(c, pid))
        return {
            "schema": "extore.commerce-catalog.v1",
            "issuer": ORIGIN,
            "shop": {"id": shop["id"], "name": shop["name"]},
            "grant_id": grant["id"],
            "products": values,
        }


@router.get("/api/integrations/commerce/products/{product_id}")
def catalog_product(product_id: str, request: Request):
    from .commerce_listing import commerce_listing

    rate_limit(request, "commerce-catalog", 120, 60)
    with db() as c:
        grant = _access(c, request, "products.read")
        _product(c, grant, product_id)
        return commerce_listing(c, product_id)


@router.post("/api/integrations/commerce/cards")
async def issue_endpoint(request: Request):
    from .commerce_listing import commerce_listing, safe_variants
    from .proxy_routes import unwrap_local_code
    from .secret_store import SecretStoreError, open_secret, store_secret
    from .service import issue_cards

    rate_limit(request, "commerce-cards", 60, 60)
    _machine(request, bearer=True)
    try:
        body = Issue.model_validate_json(await request.body())
    except ValidationError:
        fail("invalid_request", "发行请求字段无效；不能覆盖卡密属性或发行路由")
    key = request.headers.get("idempotency-key", "")
    if not 8 <= len(key) <= 200 or any(not 33 <= ord(ch) <= 126 for ch in key):
        fail("invalid_request", "请提供 8–200 字符的 Idempotency-Key")
    with db() as c:
        grant = _access(c, request, "cards.issue")
        p = _product(c, grant, body.product_id)
        if p["mode"] == "stock":
            fail("unsupported_product", "一卡一文本商品不能通过此协议生成库存", 409)
        variant = next((v for v in p["variants"] if v["id"] == body.variant_id), None)
        if variant is None or not variant["enabled"]:
            fail("variant_unavailable", "商品规格不存在或已停用", 409)
        limits = json.loads(grant["card_limits"])
        limit = next(
            (
                item
                for item in limits
                if item["product_id"] == body.product_id
                and item["variant_id"] == body.variant_id
            ),
            None,
        )
        if limit is None:
            fail("insufficient_scope", "此商品规格没有批准的发行额度", 403)
        old = c.execute(
            "SELECT * FROM commerce_issuances WHERE grant_id=? AND key_digest=?",
            (grant["id"], digest(key)),
        ).fetchone()
        fingerprint = digest(canonical(body.model_dump()))
        if old is not None:
            if not hmac.compare_digest(old["fingerprint"], fingerprint):
                fail("idempotency_conflict", "同一个幂等键不能用于不同请求", 409)
            if (
                old["recovery_expires"] <= time.time()
                or old["response_ciphertext"] is None
            ):
                # The tombstone must remain: an expired recovery is never a new mint.
                c.execute(
                    "UPDATE commerce_issuances SET response_ciphertext=NULL WHERE id=?",
                    (old["id"],),
                )
                # Commit the ciphertext deletion, then return 410 below.
                expired = True
            else:
                try:
                    return open_secret(
                        old["response_ciphertext"],
                        tenant_id=grant["shop_id"],
                        resource_type="commerce-issuance",
                        resource_id=old["id"],
                    )
                except SecretStoreError:
                    fail(
                        "temporarily_unavailable",
                        "发行结果暂时无法恢复，请联系商家，不要换键重复补货",
                        503,
                    )
        else:
            expired = False
            if (
                body.expected_revision is not None
                and body.expected_revision
                != commerce_listing(c, body.product_id)["revision"]
            ):
                fail("catalog_changed", "商品资料已变化，请重新读取后再发行", 409)
            if body.expires is not None and body.expires <= time.time():
                fail("invalid_request", "卡密到期时间必须是未来时间")
            if limit["issued_count"] + body.count > limit["max_count"]:
                fail("quota_exceeded", "此商品规格的累计发行额度不足", 409)
            now, iid = time.time(), str(uuid.uuid4())
            codes = issue_cards(
                c,
                body.product_id,
                body.count,
                label=body.label,
                expires=body.expires,
                variant_id=body.variant_id,
            )
            batch = c.execute(
                "SELECT m.batch_id FROM card_meta m JOIN cards c ON c.id=m.card_id WHERE c.digest=?",
                (card_digest(unwrap_local_code(c, codes[0])),),
            ).fetchone()
            if batch is None or not batch["batch_id"]:
                fail("temporarily_unavailable", "发行批次无法确认", 503)
            limit["issued_count"] += body.count
            result = {
                "schema": "extore.card-batch.v1",
                "grant_id": grant["id"],
                "product_id": body.product_id,
                "variant_id": body.variant_id,
                "batch_id": batch["batch_id"],
                "count": body.count,
                "codes": codes,
                "created_at": now,
                "recovery_expires": now + RECOVERY_TTL,
                "quota": {key: limit[key] for key in ("max_count", "issued_count")},
                "variant": safe_variants([variant])[0],
            }
            result["quota"]["remaining"] = limit["max_count"] - limit["issued_count"]
            try:
                encrypted = store_secret(
                    result,
                    tenant_id=grant["shop_id"],
                    resource_type="commerce-issuance",
                    resource_id=iid,
                )
            except SecretStoreError:
                fail("temporarily_unavailable", "发行结果无法安全保存，请联系商家", 503)
            c.execute(
                "INSERT INTO commerce_issuances VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    iid,
                    grant["id"],
                    digest(key),
                    fingerprint,
                    body.product_id,
                    body.variant_id,
                    batch["batch_id"],
                    now,
                    now + RECOVERY_TTL,
                    encrypted,
                ),
            )
            c.execute(
                "UPDATE commerce_grants SET card_limits=? WHERE id=?",
                (canonical(limits), grant["id"]),
            )
            audit(
                c,
                "commerce:" + grant["client_id"],
                "commerce.cards.issue",
                batch["batch_id"],
            )
            return result
    if expired:
        fail("issuance_expired", "发行结果恢复窗口已过期；此请求不会重新生成卡密", 410)

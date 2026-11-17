use super::*;
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use subtle::ConstantTimeEq;

fn form(ctx: &Context) -> CommerceResult<()> {
    machine(ctx, false)?;
    if ctx.header("content-type").split(';').next() != Some("application/x-www-form-urlencoded")
        || ctx.raw_body.len() > 8192
        || !ctx.raw_body.is_ascii()
    {
        return Err(error(
            "invalid_request",
            "令牌接口须使用 ASCII application/x-www-form-urlencoded",
            400,
        ));
    }
    let pairs = url::form_urlencoded::parse(&ctx.raw_body).collect::<Vec<_>>();
    if pairs.len() > 20
        || pairs
            .iter()
            .map(|p| p.0.as_ref())
            .collect::<BTreeSet<_>>()
            .len()
            != pairs.len()
    {
        return Err(error("invalid_request", "令牌参数重复或过多", 400));
    }
    Ok(())
}
fn mint(ctx: &Context, g: &Value) -> CommerceResult<Value> {
    let access = token();
    let refresh = token();
    let time = now();
    let expiry = (time + 900.0).min(number(g, "expires"));
    if expiry - time < 1.0 {
        return Err(error("invalid_grant", "商城授权即将到期", 400));
    }
    for (raw, kind, expires) in [
        (&access, "access", expiry),
        (&refresh, "refresh", number(g, "expires")),
    ] {
        ctx.conn.execute(
            "INSERT INTO commerce_tokens(digest,grant_id,kind,created,expires) VALUES (?,?,?,?,?)",
            params![digest(raw), text(g, "id"), kind, time, expires],
        )?;
    }
    Ok(
        json!({"access_token":access,"token_type":"Bearer","expires_in":(expiry-time) as i64,"refresh_token":refresh,"scope":json_field(g,"scopes",json!([])).as_array().unwrap().iter().map(|v|v.as_str().unwrap_or("")).collect::<Vec<_>>().join(" "),"grant_id":g["id"],"grant_expires":g["expires"]}),
    )
}
fn same_client(ctx: &Context, gid: &str, cid: &str) -> CommerceResult<Value> {
    let g = grant(ctx, gid, true)
        .map_err(|_| error("invalid_grant", "授权无效、已到期或已撤销", 400))?;
    if text(&g, "client_id") != cid {
        return Err(error("invalid_grant", "授权与商城应用不匹配", 400));
    }
    Ok(g)
}
fn replay(ctx: &Context, g: &Value, action: &str) -> CommerceResult<Reply> {
    revoke(
        ctx,
        text(g, "id"),
        &format!("commerce:{}", text(g, "client_id")),
        action,
    )?;
    let mut e = error("invalid_grant", "令牌或授权码已使用，商城授权已撤销", 400);
    e.commit = true;
    Err(e)
}
fn grant_token(ctx: &Context) -> CommerceResult<Reply> {
    form(ctx)?;
    let cid = ctx.b("client_id");
    client(ctx, cid, true)?;
    let kind = ctx.b("grant_type");
    let g = if kind == "authorization_code" {
        let code = ctx.b("code");
        let verifier = ctx.b("code_verifier");
        if code.len() != 43
            || !(43..=128).contains(&verifier.len())
            || !verifier
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'~' | b'-'))
        {
            return Err(error("invalid_grant", "授权码或 PKCE 信息无效", 400));
        }
        let stored = row(
            &ctx.conn,
            "SELECT id FROM commerce_grants WHERE code_digest=?",
            [digest(code)],
        )?
        .ok_or_else(|| error("invalid_grant", "授权码无效", 400))?;
        let g = same_client(ctx, text(&stored, "id"), cid)?;
        let challenge = URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()));
        if ctx.b("redirect_uri") != text(&g, "redirect_uri")
            || !bool::from(
                challenge
                    .as_bytes()
                    .ct_eq(text(&g, "code_challenge").as_bytes()),
            )
        {
            return Err(error("invalid_grant", "回调地址或 PKCE 信息不匹配", 400));
        }
        if !g["code_used"].is_null() {
            return replay(ctx, &g, "commerce.code.replay");
        }
        if number(&g, "code_expires") <= now() {
            return Err(error("invalid_grant", "授权码已到期", 400));
        }
        ctx.conn.execute(
            "UPDATE commerce_grants SET code_used=? WHERE id=?",
            params![now(), text(&g, "id")],
        )?;
        g
    } else if kind == "refresh_token" {
        let refresh = ctx.b("refresh_token");
        if refresh.len() != 43 {
            return Err(error("invalid_grant", "刷新令牌无效", 400));
        }
        let stored = row(
            &ctx.conn,
            "SELECT * FROM commerce_tokens WHERE digest=? AND kind='refresh' AND expires>?",
            params![digest(refresh), now()],
        )?
        .ok_or_else(|| error("invalid_grant", "刷新令牌无效或已到期", 400))?;
        let g = same_client(ctx, text(&stored, "grant_id"), cid)?;
        if !stored["used"].is_null() {
            return replay(ctx, &g, "commerce.refresh.replay");
        }
        if ctx.body.get("scope").is_some() {
            let requested = ctx.b("scope").split(' ').collect::<BTreeSet<_>>();
            let scopes = json_field(&g, "scopes", json!([]));
            let allowed = scopes
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap_or(""))
                .collect::<BTreeSet<_>>();
            if requested != allowed {
                return Err(error("invalid_scope", "刷新不能改变已批准权限", 400));
            }
        }
        ctx.conn.execute(
            "UPDATE commerce_tokens SET used=? WHERE digest=?",
            params![now(), text(&stored, "digest")],
        )?;
        g
    } else {
        return Err(error(
            "unsupported_grant_type",
            "仅支持授权码和刷新令牌",
            400,
        ));
    };
    let result = mint(ctx, &g)?;
    audit(
        &ctx.conn,
        &format!("commerce:{cid}"),
        if kind == "refresh_token" {
            "commerce.token.refresh"
        } else {
            "commerce.token.exchange"
        },
        text(&g, "id"),
        Some(text(&g, "shop_id")),
    )?;
    Ok(Reply::json(result))
}
fn issue(ctx: &Context) -> CommerceResult<Reply> {
    let g = access(ctx, "cards.issue")?;
    let allowed = [
        "product_id",
        "variant_id",
        "count",
        "label",
        "expires",
        "expected_revision",
    ];
    if ctx
        .body
        .as_object()
        .is_none_or(|o| o.keys().any(|k| !allowed.contains(&k.as_str())))
    {
        return Err(error(
            "invalid_request",
            "发行请求字段无效，不能覆盖卡密属性或发行路由",
            400,
        ));
    }
    let pid = ctx.b("product_id");
    let vid = ctx.b("variant_id");
    let count = integer(&ctx.body, "count", 0);
    if !(1..=100).contains(&count) {
        return Err(error("invalid_request", "每次可发行 1–100 张卡密", 400));
    }
    if ctx
        .body
        .get("expires")
        .filter(|v| !v.is_null())
        .is_some_and(|v| v.as_f64().is_none_or(|n| !n.is_finite() || n <= now()))
        || ctx
            .body
            .get("label")
            .is_some_and(|v| v.as_str().is_none_or(|s| s.chars().count() > 120))
        || ctx
            .body
            .get("expected_revision")
            .filter(|v| !v.is_null())
            .is_some_and(|v| {
                v.as_str().is_none_or(|s| {
                    s.len() != 64
                        || !s
                            .bytes()
                            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                })
            })
    {
        return Err(error(
            "invalid_request",
            "发行请求的有效期、标签或版本格式无效",
            400,
        ));
    }
    let key = ctx.header("idempotency-key");
    if !(8..=200).contains(&key.len()) || key.bytes().any(|b| !(33..=126).contains(&b)) {
        return Err(error(
            "invalid_request",
            "请提供 8–200 字符的 Idempotency-Key",
            400,
        ));
    }
    let p = allowed_product(ctx, &g, pid)?;
    if text(&p, "mode") == "stock" {
        return Err(error(
            "unsupported_product",
            "文本库存须先由商家导入内容",
            409,
        ));
    }
    let variant = p["variants"]
        .as_array()
        .unwrap()
        .iter()
        .find(|v| text(v, "id") == vid && flag(v, "enabled"))
        .ok_or_else(|| error("variant_unavailable", "规格不存在或已停用", 409))?;
    let mut limits = json_field(&g, "card_limits", json!([]));
    let limit = limits
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|v| text(v, "product_id") == pid && text(v, "variant_id") == vid)
        .ok_or_else(|| error("insufficient_scope", "规格没有批准的发行额度", 403))?;
    let canonical = json!({"product_id":pid,"variant_id":vid,"count":count,"label":ctx.b("label"),"expires":ctx.body.get("expires").unwrap_or(&Value::Null),"expected_revision":ctx.body.get("expected_revision").unwrap_or(&Value::Null)});
    let fingerprint = digest(&listing::canonical_ascii(&canonical));
    if let Some(old) = row(
        &ctx.conn,
        "SELECT * FROM commerce_issuances WHERE grant_id=? AND key_digest=?",
        params![text(&g, "id"), digest(key)],
    )? {
        if !bool::from(
            text(&old, "fingerprint")
                .as_bytes()
                .ct_eq(fingerprint.as_bytes()),
        ) {
            return Err(error(
                "idempotency_conflict",
                "同一幂等键不能用于不同请求",
                409,
            ));
        }
        if number(&old, "recovery_expires") <= now() || old["response_ciphertext"].is_null() {
            ctx.conn.execute(
                "UPDATE commerce_issuances SET response_ciphertext=NULL WHERE id=?",
                [text(&old, "id")],
            )?;
            let mut e = error(
                "issuance_expired",
                "恢复窗口已过期，此请求不会重新发行卡密",
                410,
            );
            e.commit = true;
            return Err(e);
        }
        return Ok(Reply::json(auth::secret::open_secret(
            &ctx.app,
            Some(text(&g, "shop_id")),
            "commerce-issuance",
            text(&old, "id"),
            text(&old, "response_ciphertext"),
        )?));
    }
    if let Some(expected) = ctx.body["expected_revision"].as_str() {
        if listing::listing(ctx, pid, false)?["revision"] != expected {
            return Err(error("catalog_changed", "商品资料已变化，请重新读取", 409));
        }
    }
    if integer(limit, "issued_count", 0) + count > integer(limit, "max_count", 0) {
        return Err(error("quota_exceeded", "累计发行额度不足", 409));
    }
    let batch = catalog::issue_cards(ctx, pid, &canonical)?;
    let time = now();
    let iid = id();
    limit["issued_count"] = json!(integer(limit, "issued_count", 0) + count);
    let response = json!({"schema":"extore.card-batch.v1","grant_id":g["id"],"product_id":pid,"variant_id":vid,"batch_id":batch["batch_id"],"count":count,"codes":batch["codes"],"created_at":time,"recovery_expires":time+86400.0,"quota":{"max_count":limit["max_count"],"issued_count":limit["issued_count"],"remaining":integer(limit,"max_count",0)-integer(limit,"issued_count",0)},"variant":listing::safe_variants(&json!([variant]))[0]});
    let encrypted = auth::secret::store_secret(
        &ctx.app,
        Some(text(&g, "shop_id")),
        "commerce-issuance",
        &iid,
        &response,
    )?;
    ctx.conn.execute(
        "INSERT INTO commerce_issuances VALUES (?,?,?,?,?,?,?,?,?,?)",
        params![
            iid,
            text(&g, "id"),
            digest(key),
            fingerprint,
            pid,
            vid,
            text(&batch, "batch_id"),
            time,
            time + 86400.0,
            encrypted
        ],
    )?;
    ctx.conn.execute(
        "UPDATE commerce_grants SET card_limits=? WHERE id=?",
        params![listing::canonical_ascii(&limits), text(&g, "id")],
    )?;
    audit(
        &ctx.conn,
        &format!("commerce:{}", text(&g, "client_id")),
        "commerce.cards.issue",
        text(&batch, "batch_id"),
        Some(text(&g, "shop_id")),
    )?;
    Ok(Reply::json(response))
}
pub(super) fn handle(ctx: &mut Context) -> CommerceResult<Option<Reply>> {
    if ctx.is("POST", "/api/integrations/commerce/token") {
        return grant_token(ctx).map(Some);
    }
    if ctx.is("POST", "/api/integrations/commerce/cards") {
        return issue(ctx).map(Some);
    }
    if ctx.is("POST", "/api/integrations/commerce/revoke") {
        form(ctx)?;
        let cid = ctx.b("client_id");
        client(ctx, cid, true)?;
        if let Some(stored) = row(
            &ctx.conn,
            "SELECT grant_id FROM commerce_tokens WHERE digest=?",
            [digest(ctx.b("token"))],
        )? {
            let g = grant(ctx, text(&stored, "grant_id"), false)?;
            if text(&g, "client_id") == cid {
                revoke(
                    ctx,
                    text(&g, "id"),
                    &format!("commerce:{cid}"),
                    "commerce.grant.revoke",
                )?;
            }
        }
        let mut reply = Reply::json(json!({}));
        reply.body.clear();
        return Ok(Some(reply));
    }
    Ok(None)
}

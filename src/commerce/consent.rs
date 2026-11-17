use super::*;
use std::collections::BTreeSet;
use subtle::ConstantTimeEq;

pub(super) fn validate_redirect(value: &str) -> CommerceResult<()> {
    if value.len() > 2048
        || value.bytes().any(|b| !(33..=126).contains(&b))
        || value.contains('\\')
        || value.contains('#')
    {
        return Err(error(
            "invalid_request",
            "回调地址须为 ASCII HTTPS URL",
            400,
        ));
    }
    let url = url::Url::parse(value).map_err(|_| error("invalid_request", "回调地址无效", 400))?;
    let host = url.host_str().unwrap_or("");
    if url.scheme() != "https"
        || host.is_empty()
        || !host.contains('.')
        || host.ends_with('.')
        || host.split('.').any(|label| {
            label.is_empty()
                || label.len() > 63
                || label.starts_with('-')
                || label.ends_with('-')
                || !label
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'-')
        })
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
        || host.parse::<std::net::IpAddr>().is_ok()
        || [".localhost", ".local", ".internal", ".onion"]
            .iter()
            .any(|v| host.ends_with(v))
    {
        return Err(error(
            "invalid_request",
            "回调地址须为无凭据、无片段的 HTTPS 域名地址",
            400,
        ));
    }
    let mut names = BTreeSet::new();
    for (k, _) in url.query_pairs() {
        if matches!(
            k.as_ref(),
            "code" | "state" | "iss" | "error" | "error_description"
        ) || !names.insert(k.to_string())
        {
            return Err(error(
                "invalid_request",
                "回调地址不能预置 OAuth 参数或重复参数",
                400,
            ));
        }
    }
    Ok(())
}
fn valid_slug(s: &str, max: usize) -> bool {
    !s.is_empty()
        && s.len() <= max
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
}
fn context(ctx: &Context, request: &Value, shop_id: &str) -> CommerceResult<Value> {
    let c = client(ctx, text(request, "client_id"), true)?;
    let shop = required(
        &ctx.conn,
        "SELECT id,name FROM shops WHERE id=? AND enabled=1",
        [shop_id],
        "店铺不存在或已停用",
    )?;
    let requested = json_field(request, "product_ids", json!([]));
    let requested = requested.as_array().unwrap();
    let mut products = Vec::new();
    let mut revisions = json!({});
    for item in rows(
        &ctx.conn,
        "SELECT id FROM products WHERE shop_id=? ORDER BY created,id",
        [shop_id],
    )? {
        let pid = text(&item, "id");
        let p = catalog::product(ctx, pid)?;
        if flag(&p, "deleted") || (!requested.is_empty() && !requested.contains(&json!(pid))) {
            continue;
        }
        let l = listing::listing(ctx, pid, false)?;
        let variants = l["variants"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| json!({"id":v["id"],"name":v["name"],"enabled":v["enabled"]}))
            .collect::<Vec<_>>();
        products.push(json!({"id":pid,"name":l["product"]["name"],"variants":variants}));
        revisions[pid] = l["revision"].clone();
    }
    if products.len() > 100
        || requested
            .iter()
            .any(|v| !products.iter().any(|p| p["id"] == *v))
    {
        return Err(error(
            "invalid_request",
            "商品范围不可用或超过单次 100 件上限",
            409,
        ));
    }
    let u = url::Url::parse(text(request, "redirect_uri"))
        .map_err(|_| error("invalid_request", "回调地址无效", 400))?;
    let mut result = json!({"request":{"id":request["id"],"client_id":request["client_id"],"client_name":json_field(&c,"metadata",json!({}))["client_name"],"redirect_uri":request["redirect_uri"],"redirect_host":u.host_str().unwrap_or(""),"scopes":json_field(request,"scopes",json!([])),"requested_product_ids":requested,"expires":request["expires"],"max_grant_expires":number(request,"created")+1095.0*86400.0},"shop":shop,"products":products});
    let mut review = result.clone();
    review["client_metadata"] = c["metadata"].clone();
    review["revisions"] = revisions;
    result["review_digest"] = json!(digest(&listing::canonical_ascii(&review)));
    Ok(result)
}
fn approval(ctx: &Context, s: &Value, rid: &str, shop: &str) -> CommerceResult<(Value, Value)> {
    let req = row(
        &ctx.conn,
        "SELECT * FROM commerce_requests WHERE id=? AND status='pending' AND expires>?",
        params![rid, now()],
    )?
    .ok_or_else(|| error("invalid_request", "授权申请已失效或已处理", 409))?;
    let own = if text(&req, "client_id") == "lmm-best-store" {
        catalog::selected_shop(ctx, s, shop)?
    } else {
        let c = owned_client(ctx, s, text(&req, "client_id"), true)?;
        if !shop.is_empty() && shop != text(&c, "shop_id") {
            return Err(error("access_denied", "店铺与商城应用不匹配", 403));
        }
        text(&c, "shop_id").to_owned()
    };
    let value = context(ctx, &req, &own)?;
    Ok((req, value))
}
fn redirect(ctx: &Context, req: &Value, key: &str, value: &str) -> CommerceResult<String> {
    let mut u = url::Url::parse(text(req, "redirect_uri"))
        .map_err(|_| error("invalid_request", "回调地址无效", 400))?;
    u.query_pairs_mut()
        .append_pair(key, value)
        .append_pair("state", text(req, "state"))
        .append_pair("iss", &ctx.app.origin);
    Ok(u.into())
}

pub(super) fn handle(ctx: &mut Context) -> CommerceResult<Option<Reply>> {
    if ctx.is("GET", "/oauth/authorize") {
        let pairs = url::form_urlencoded::parse(
            ctx.raw_target
                .split_once('?')
                .map(|p| p.1)
                .unwrap_or("")
                .as_bytes(),
        )
        .collect::<Vec<_>>();
        let unique = pairs
            .iter()
            .map(|(k, _)| k.as_ref())
            .collect::<BTreeSet<_>>();
        if pairs.len() > 20 || unique.len() != pairs.len() {
            return Err(error("invalid_request", "授权参数重复或过多", 400));
        }
        if ctx.q("response_type") != "code" {
            return Err(error("unsupported_response_type", "仅支持授权码模式", 400));
        }
        if ctx.q("code_challenge_method") != "S256"
            || ctx.q("code_challenge").len() != 43
            || !valid_slug(ctx.q("code_challenge"), 43)
        {
            return Err(error("invalid_request", "授权必须使用 S256 PKCE", 400));
        }
        let state = ctx.q("state");
        if !(16..=512).contains(&state.len()) || state.bytes().any(|b| !(32..=126).contains(&b)) {
            return Err(error("invalid_request", "请提供有效随机 state", 400));
        }
        let scopes = ctx.q("scope").split(' ').collect::<Vec<_>>();
        if scopes.is_empty()
            || scopes.len() != scopes.iter().collect::<BTreeSet<_>>().len()
            || scopes
                .iter()
                .any(|s| !matches!(*s, "products.read" | "cards.issue" | "marketplace.sync"))
        {
            return Err(error("invalid_scope", "申请了未支持的商城权限", 400));
        }
        let pids = if ctx.q("product_ids").is_empty() {
            vec![]
        } else {
            ctx.q("product_ids").split(',').collect::<Vec<_>>()
        };
        if pids.len() > 100
            || pids.len() != pids.iter().collect::<BTreeSet<_>>().len()
            || pids.iter().any(|s| !valid_slug(s, 100))
        {
            return Err(error("invalid_request", "申请商品编号无效", 400));
        }
        if ctx.q("client_id") == "lmm-best-store" {
            let metadata = json!({"client_name":"LMM API","redirect_uris":["https://api.lmm.best/store/manage","https://native.api.lmm.best/store/manage"],"token_endpoint_auth_method":"none","grant_types":["authorization_code","refresh_token"],"response_types":["code"]});
            ctx.conn.execute("INSERT INTO commerce_clients(id,shop_id,metadata,created) VALUES ('lmm-best-store',?,?,?) ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata",params![setting(&ctx.conn,"legacy_shop_id","")?,listing::canonical_ascii(&metadata),now()])?;
        }
        let c = client(ctx, ctx.q("client_id"), true)?;
        if !json_field(&c, "metadata", json!({}))["redirect_uris"]
            .as_array()
            .is_some_and(|v| v.contains(&json!(ctx.q("redirect_uri"))))
        {
            return Err(error("invalid_request", "回调地址与登记地址不匹配", 400));
        }
        let pending=ctx.conn.query_row("SELECT COUNT(*) FROM commerce_requests WHERE client_id=? AND status='pending' AND expires>?",params![text(&c,"id"),now()],|r|r.get::<_,i64>(0))?;
        if pending >= 40 {
            return Err(error("temporarily_unavailable", "待批准申请过多", 429));
        }
        let rid = token();
        let scopes = ["products.read", "cards.issue", "marketplace.sync"]
            .into_iter()
            .filter(|s| scopes.contains(s))
            .collect::<Vec<_>>();
        ctx.conn.execute("INSERT INTO commerce_requests(id,client_id,redirect_uri,state,code_challenge,scopes,product_ids,created,expires) VALUES (?,?,?,?,?,?,?,?,?)",params![rid,text(&c,"id"),ctx.q("redirect_uri"),state,ctx.q("code_challenge"),json!(scopes).to_string(),json!(pids).to_string(),now(),now()+600.0])?;
        return Ok(Some(Reply::redirect(&format!(
            "{}/connect/authorize#{rid}",
            ctx.app.origin
        ))?));
    }
    let Some(suffix) = ctx.path.strip_prefix("/api/admin/commerce/requests/") else {
        return Ok(None);
    };
    let parts = suffix.split('/').collect::<Vec<_>>();
    let rid = parts[0];
    if ctx.method == axum::http::Method::GET && parts.len() == 1 {
        let s = browser(ctx, false)?;
        let (_, value) = approval(ctx, &s, rid, ctx.q("shop_id"))?;
        return Ok(Some(Reply::json(value)));
    }
    if ctx.method == axum::http::Method::POST
        && parts.len() == 2
        && matches!(parts[1], "approve" | "deny")
    {
        let s = browser(ctx, true)?;
        let (req, value) = approval(ctx, &s, rid, ctx.b("shop_id"))?;
        if !bool::from(
            ctx.b("review_digest")
                .as_bytes()
                .ct_eq(text(&value, "review_digest").as_bytes()),
        ) {
            return Err(error(
                "review_changed",
                "商城申请或商品资料已变化，请重新查看",
                409,
            ));
        }
        if parts[1] == "deny" {
            ctx.conn.execute(
                "UPDATE commerce_requests SET status='denied' WHERE id=?",
                [rid],
            )?;
            audit(
                &ctx.conn,
                &actor(&s),
                "commerce.request.deny",
                rid,
                Some(ctx.b("shop_id")),
            )?;
            return Ok(Some(Reply::json(
                json!({"ok":true,"redirect_uri":redirect(ctx,&req,"error","access_denied")?}),
            )));
        }
        let scopes = ctx.body["scopes"]
            .as_array()
            .ok_or_else(|| error("invalid_scope", "请选择批准权限", 400))?;
        let requested = json_field(&req, "scopes", json!([]));
        let requested = requested.as_array().unwrap();
        if scopes.is_empty()
            || scopes.len() > 3
            || scopes.iter().any(|s| !requested.contains(s))
            || scopes
                .iter()
                .map(Value::to_string)
                .collect::<BTreeSet<_>>()
                .len()
                != scopes.len()
        {
            return Err(error("invalid_scope", "只能批准请求中的权限", 400));
        }
        let ids = ctx.body["product_ids"]
            .as_array()
            .ok_or_else(|| error("invalid_request", "请选择商品", 400))?;
        let candidates = value["products"].as_array().unwrap();
        if ids.is_empty()
            || ids.len() > 100
            || ids
                .iter()
                .any(|p| !candidates.iter().any(|c| c["id"] == *p))
            || ids
                .iter()
                .map(Value::to_string)
                .collect::<BTreeSet<_>>()
                .len()
                != ids.len()
        {
            return Err(error("access_denied", "只能批准展示的当前商品", 403));
        }
        let limits = ctx.body.get("card_limits").cloned().unwrap_or(json!([]));
        let limits = limits
            .as_array()
            .ok_or_else(|| error("invalid_request", "规格额度无效", 400))?;
        if scopes.contains(&json!("cards.issue")) == limits.is_empty() || limits.len() > 500 {
            return Err(error(
                "invalid_request",
                "发行权限必须设置独立规格额度",
                400,
            ));
        }
        let mut seen = BTreeSet::new();
        let mut approved = Vec::new();
        for limit in limits {
            let pid = text(limit, "product_id");
            let vid = text(limit, "variant_id");
            let maximum = integer(limit, "max_count", 0);
            if !ids.contains(&json!(pid))
                || !(1..=10000).contains(&maximum)
                || !seen.insert((pid, vid))
            {
                return Err(error("invalid_request", "规格额度无效或重复", 400));
            }
            let p = catalog::require_active(ctx, pid)?;
            if text(&p, "mode") == "stock" {
                return Err(error(
                    "unsupported_product",
                    "文本库存须先导入交付内容",
                    409,
                ));
            }
            if !p["variants"]
                .as_array()
                .unwrap()
                .iter()
                .any(|v| text(v, "id") == vid && flag(v, "enabled"))
            {
                return Err(error("invalid_request", "规格不存在或已停用", 400));
            }
            approved.push(
                json!({"product_id":pid,"variant_id":vid,"max_count":maximum,"issued_count":0}),
            );
        }
        let expires = number(&ctx.body, "grant_expires");
        if expires < now() + 60.0 || expires > number(&value["request"], "max_grant_expires") {
            return Err(error(
                "invalid_request",
                "授权有效期须在一分钟至三年内",
                400,
            ));
        }
        let own = text(&value["shop"], "id");
        let count = ctx.conn.query_row(
            "SELECT COUNT(*) FROM commerce_grants WHERE shop_id=? AND revoked=0 AND expires>?",
            params![own, now()],
            |r| r.get::<_, i64>(0),
        )?;
        if count >= 100 {
            return Err(error("invalid_request", "有效授权数量已达上限", 409));
        }
        let gid = id();
        let code = token();
        ctx.conn.execute("INSERT INTO commerce_grants(id,client_id,shop_id,request_id,scopes,issuer_role,product_ids,card_limits,created,expires,code_digest,code_expires,redirect_uri,code_challenge) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",params![gid,text(&req,"client_id"),own,rid,json!(scopes).to_string(),if text(&s,"shop_id").is_empty(){"root"}else{"shop"},json!(ids).to_string(),json!(approved).to_string(),now(),expires,digest(&code),now()+120.0,text(&req,"redirect_uri"),text(&req,"code_challenge")])?;
        ctx.conn.execute(
            "UPDATE commerce_requests SET status='approved' WHERE id=?",
            [rid],
        )?;
        audit(
            &ctx.conn,
            &actor(&s),
            "commerce.grant.approve",
            &gid,
            Some(own),
        )?;
        return Ok(Some(Reply::json(
            json!({"ok":true,"redirect_uri":redirect(ctx,&req,"code",&code)?}),
        )));
    }
    Ok(None)
}

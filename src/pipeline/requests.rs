use super::*;
use auth::cli;
use rand::Rng;
use subtle::ConstantTimeEq;
const ALPHABET: &[u8] = b"ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
fn same(a: &str, b: &str) -> bool {
    bool::from(a.as_bytes().ct_eq(b.as_bytes()))
}
fn canonical(v: &Value) -> Result<String> {
    cli::ascii_json(v)
}
fn uuid(v: &str) -> Result<()> {
    if uuid::Uuid::parse_str(v)
        .ok()
        .is_none_or(|u| u.to_string() != v)
    {
        Err(invalid())
    } else {
        Ok(())
    }
}
fn code(value: &str) -> Option<String> {
    let value = value.trim().to_ascii_uppercase().replace('-', "");
    if value.len() != 12 || !value.bytes().all(|b| ALPHABET.contains(&b)) {
        return None;
    }
    Some(
        value
            .as_bytes()
            .chunks(4)
            .map(|b| std::str::from_utf8(b).unwrap())
            .collect::<Vec<_>>()
            .join("-"),
    )
}
fn request(ctx: &Context, rid: &str) -> Result<Value> {
    row(
        &ctx.conn,
        "SELECT * FROM cli_scope_requests WHERE id=?",
        [rid],
    )?
    .ok_or_else(|| ApiError::new(401, "设备授权请求无效"))
}
fn owner(ctx: &Context, s: &Value, sid: &str) -> Result<Value> {
    if text(s, "role") != "admin" || s["shop_id"].as_str().is_some_and(|own| own != sid) {
        return Err(ApiError::new(403, "此设备申请需要对应店铺的店长批准"));
    }
    auth::shop(ctx, sid)
}
fn browser(ctx: &Context, bucket: &str) -> Result<Value> {
    if ctx.headers.contains_key("authorization") {
        return Err(ApiError::new(401, "请使用已登录的浏览器批准设备"));
    }
    let s = auth::session(ctx, &["admin", "staff"], None)?;
    if text(&s, "channel") != "browser" {
        return Err(ApiError::new(401, "请使用已登录的浏览器批准设备"));
    }
    auth::throttle(ctx, bucket, 20, 60.0, Some(text(&s, "id")))?;
    Ok(s)
}
fn pending(ctx: &Context, s: &Value) -> Result<Value> {
    if let Some(code) = code(ctx.b("user_code")) {
        if let Some(r) = row(
            &ctx.conn,
            "SELECT * FROM cli_scope_requests WHERE user_code=? AND expires>? AND state='pending'",
            params![code, now()],
        )? {
            return Ok(r);
        }
    }
    auth::throttle(ctx, "device-invalid-code", 5, 60.0, Some(text(s, "id")))?;
    Err(ApiError::new(401, "设备码无效、已过期或已处理"))
}
fn request_view(ctx: &Context, r: &Value) -> Value {
    json!({"request_id":r["id"],"user_code":r["user_code"],"approval_url":format!("{}/cli/device",ctx.app.origin),"challenge":r["challenge"],"expires":r["expires"],"expires_in":(number(r,"expires")-now()+0.999).max(0.0) as u64,"interval":r["poll_interval"],"fingerprint":r["fingerprint"],"flow":"scope"})
}
fn request_current(ctx: &Context, r: &Value) -> Result<Option<Value>> {
    let Some(aid) = r["current_authorization_id"].as_str() else {
        return Ok(None);
    };
    let raw = current(ctx, aid)?;
    let value = view(ctx, &raw)?;
    if value["revision"] != r["current_revision"]
        || !same(text(&raw, "public_key"), text(r, "public_key"))
        || value["shop_id"] != r["shop_id"]
        || value["kind"] != r["kind"]
    {
        return Err(ApiError::new(409, "原授权已变化，请重新申请追加权限"));
    }
    bindings(ctx, aid)?;
    Ok(Some(value))
}
fn selected(ctx: &Context, r: &Value, body: &Value) -> Result<(Value, Option<Value>)> {
    let current = request_current(ctx, r)?;
    let requested_products = strings(&json_field(r, "requested_product_ids", Value::Null))?;
    let requested_permissions = strings(&json_field(r, "requested_permissions", Value::Null))?;
    let pids = body
        .get("product_ids")
        .filter(|v| !v.is_null())
        .map(strings)
        .transpose()?
        .unwrap_or(requested_products.clone());
    let perms = body
        .get("permissions")
        .filter(|v| !v.is_null())
        .map(strings)
        .transpose()?
        .unwrap_or(requested_permissions.clone());
    if pids.is_empty()
        || pids.iter().collect::<BTreeSet<_>>().len() != pids.len()
        || pids.iter().any(|p| !requested_products.contains(p))
        || perms.iter().any(|p| !requested_permissions.contains(p))
    {
        return Err(ApiError::new(403, "批准范围不能超过本次设备申请"));
    }
    let perms = permissions(&json!(perms), text(r, "kind"))?;
    let expires = match body.get("expires").filter(|v| !v.is_null()) {
        Some(v) => v.as_f64().ok_or_else(invalid)?,
        None => number(r, "grant_expires"),
    };
    if !expires.is_finite() || expires <= now() || expires > number(r, "grant_expires") {
        return Err(ApiError::bad("批准的有效期超出申请范围"));
    }
    let old = current
        .as_ref()
        .map(|v| super::current(ctx, text(v, "id")))
        .transpose()?;
    let value = draft(
        ctx,
        &json!({"shop_id":r["shop_id"],"kind":r["kind"],"product_ids":pids,"permissions":perms,"expires":expires}),
        old.as_ref(),
    )?;
    Ok((value, current))
}
fn snapshot(ctx: &Context, r: &Value, draft: &Value) -> Result<String> {
    let current = request_current(ctx, r)?;
    let shop = auth::shop(ctx, text(r, "shop_id"))?;
    let mut products = vec![];
    for pid in strings(&json_field(r, "requested_product_ids", Value::Null))? {
        let p = product(ctx, &pid, text(r, "shop_id"), text(r, "kind"))?;
        products
            .push(json!({"id":pid,"shop_id":p["shop_id"],"config_hash":digest(text(&p,"config"))}));
    }
    let mut history = Value::Null;
    if let Some(current) = current {
        let raw = super::current(ctx, text(&current, "id"))?;
        let mut scope = json!({});
        for key in [
            "id",
            "shop_id",
            "kind",
            "public_key",
            "fingerprint",
            "client_name",
            "permissions",
            "expires",
            "revision",
            "revoked",
            "issuer_role",
            "issuer_shop_id",
            "approved_actor",
        ] {
            scope[key] = raw[key].clone();
        }
        if !raw["agent_type"].is_null() {
            scope["agent_type"] = raw["agent_type"].clone();
        }
        let bindings = rows(
            &ctx.conn,
            "SELECT b.product_id,b.staff_id,b.device_id,s.permissions,s.expires,s.revoked,s.parent_id,d.public_key,d.fingerprint,d.revoked AS device_revoked FROM pipeline_bindings b JOIN staff s ON s.id=b.staff_id JOIN cli_devices d ON d.id=b.device_id WHERE b.authorization_id=? ORDER BY b.product_id",
            [text(&current, "id")],
        )?;
        history = json!({"scope":scope,"bindings":bindings});
    }
    let mut request = json!({});
    for key in [
        "id",
        "public_key",
        "client_name",
        "fingerprint",
        "nonce",
        "challenge",
        "request_json",
        "kind",
        "shop_id",
        "requested_product_ids",
        "requested_permissions",
        "grant_expires",
        "current_authorization_id",
        "current_revision",
        "expires",
    ] {
        request[key] = r[key].clone();
    }
    if !r["agent_type"].is_null() {
        request["agent_type"] = r["agent_type"].clone();
    }
    canonical(
        &json!({"request":request,"shop":{"id":shop["id"],"name":shop["name"],"enabled":shop["enabled"]},"products":products,"current":history,"selected":draft}),
    )
}
fn review_digest(snapshot: &str, s: &Value) -> Result<String> {
    let mut browser = json!({});
    for k in ["digest", "role", "shop_id", "staff_id"] {
        browser[k] = s[k].clone();
    }
    Ok(digest(&canonical(
        &json!({"scope_snapshot":snapshot,"browser":browser}),
    )?))
}
fn create_request(ctx: &Context) -> Result<Reply> {
    cli::handshake(ctx)?;
    auth::throttle(ctx, "scope-request", 10, 60.0, None)?;
    cli::fields(
        ctx,
        &[
            "public_key",
            "client_name",
            "agent_type",
            "nonce",
            "kind",
            "shop_id",
            "product_ids",
            "permissions",
            "authorization_id",
            "expected_revision",
            "reason",
            "signature",
        ],
    )?;
    let key = cli::input(ctx, "public_key", 43, 43)?;
    let nonce = cli::input(ctx, "nonce", 43, 43)?;
    let signature = cli::input(ctx, "signature", 86, 86)?;
    let (name, agent) = cli::identity(
        cli::input(ctx, "client_name", 1, 100)?,
        ctx.body["agent_type"].as_str(),
    )?;
    if name != ctx.b("client_name")
        || ctx
            .body
            .get("agent_type")
            .is_some_and(|v| !v.is_null() && !v.is_string())
    {
        return Err(invalid());
    }
    let reason = ctx.body.get("reason").cloned().unwrap_or(json!(""));
    let reason = reason.as_str().ok_or_else(invalid)?;
    if reason.chars().count() > 1000
        || reason
            .chars()
            .any(|c| cli::hidden(c) && !['\n', '\t'].contains(&c))
    {
        return Err(ApiError::bad("申请原因无效"));
    }
    let kind = cli::input(ctx, "kind", 1, 30)?;
    let mut pids = strings(&ctx.body["product_ids"])?;
    if pids.len() > 500 || pids.iter().collect::<BTreeSet<_>>().len() != pids.len() {
        return Err(invalid());
    }
    for pid in &pids {
        uuid(pid)?;
    }
    for k in ["shop_id", "authorization_id"] {
        if let Some(v) = ctx.body.get(k).filter(|v| !v.is_null()) {
            uuid(v.as_str().ok_or_else(invalid)?)?;
        }
    }
    let aid = ctx.body["authorization_id"].as_str();
    let revision = ctx
        .body
        .get("expected_revision")
        .filter(|v| !v.is_null())
        .map(|v| v.as_i64().filter(|n| *n > 0).ok_or_else(invalid))
        .transpose()?;
    let perms = permissions(&ctx.body["permissions"], kind)?;
    let mut unsigned = ctx.body.clone();
    unsigned.as_object_mut().unwrap().remove("signature");
    for (k, v) in [
        ("shop_id", Value::Null),
        ("authorization_id", Value::Null),
        ("expected_revision", Value::Null),
        ("reason", json!("")),
    ] {
        unsigned.as_object_mut().unwrap().entry(k).or_insert(v);
    }
    if agent.is_none() {
        unsigned.as_object_mut().unwrap().remove("agent_type");
    }
    let canonical = canonical(&unsigned)?;
    let raw = cli::verify(
        key,
        signature,
        &format!(
            "extore-cli-scope-request-v1\n{}\n{canonical}",
            ctx.app.origin
        ),
    )?;
    if aid.is_some() != revision.is_some() {
        return Err(ApiError::bad("追加授权版本无效"));
    }
    let issued = u64::from_be_bytes(
        cli::decode(nonce, 32)?[..8]
            .try_into()
            .map_err(|_| invalid())?,
    ) as f64;
    if let Some(found) = row(
        &ctx.conn,
        "SELECT * FROM cli_scope_requests WHERE public_key=? AND nonce=?",
        params![key, nonce],
    )? {
        if text(&found, "request_json") != canonical {
            return Err(ApiError::new(401, "设备授权申请不匹配"));
        }
        return Ok(Reply::json(request_view(ctx, &found)));
    }
    if issued > now() + 60.0 || issued + 600.0 <= now() {
        return Err(ApiError::new(401, "设备申请签名已过期，请生成新的设备码"));
    }
    ctx.conn.execute("DELETE FROM cli_scope_requests WHERE id IN (SELECT id FROM cli_scope_requests WHERE expires<? ORDER BY expires,id LIMIT 200)",[now()-300.0])?;
    let all = required(
        &ctx.conn,
        "SELECT COUNT(*) AS n FROM cli_scope_requests",
        [],
        "请求计数无效",
    )?;
    let own = required(
        &ctx.conn,
        "SELECT COUNT(*) AS n FROM cli_scope_requests WHERE public_key=? AND expires>? AND state IN ('pending','approved')",
        params![key, now()],
        "请求计数无效",
    )?;
    if integer(&all, "n", 0) >= 10000 || integer(&own, "n", 0) >= 10 {
        return Err(ApiError::new(429, "设备授权申请过多，请稍后重试"));
    }
    let mut sid = ctx.b("shop_id").to_owned();
    if kind == "product" {
        if pids.len() != 1 {
            return Err(ApiError::bad("单商品申请必须指定一个商品"));
        }
        let p = required(
            &ctx.conn,
            "SELECT shop_id FROM products WHERE id=?",
            [&pids[0]],
            "商品不可用",
        )?;
        if !sid.is_empty() && sid != text(&p, "shop_id") {
            return Err(ApiError::new(403, "商品不属于申请的店铺"));
        }
        sid = text(&p, "shop_id").into();
    } else if sid.is_empty() {
        return Err(ApiError::bad("全店流水线申请必须指定店铺"));
    }
    auth::shop(ctx, &sid)?;
    if kind == "shop.pipeline" && pids.is_empty() {
        pids=rows(&ctx.conn,"SELECT id,config FROM products WHERE shop_id=? AND NOT EXISTS (SELECT 1 FROM product_lifecycle l WHERE l.product_id=products.id AND l.deleted_at IS NOT NULL) ORDER BY id",[&sid])?.iter().filter_map(|p|parse(text(p,"config")).ok().filter(|c|text(c,"mode")=="manual").map(|_|text(p,"id").to_owned())).collect();
    }
    if pids.is_empty() || pids.len() > 500 {
        return Err(ApiError::bad("本次可申请的队列商品数量无效"));
    }
    for pid in &pids {
        product(ctx, pid, &sid, kind)?;
    }
    let existing = if let Some(aid) = aid {
        let raw = current(ctx, aid)?;
        let v = view(ctx, &raw)?;
        if !same(text(&raw, "public_key"), key) {
            return Err(ApiError::new(401, "只能用原设备密钥申请追加授权"));
        }
        let mut sorted = pids.clone();
        sorted.sort();
        if integer(&v, "revision", 1) != revision.unwrap()
            && (integer(&v, "revision", 1) < revision.unwrap()
                || v["product_ids"] != json!(sorted)
                || v["permissions"] != json!(perms))
        {
            return Err(ApiError::new(409, "原授权版本已变化，请刷新后再申请"));
        }
        Some(v)
    } else {
        recovery(ctx, &sid, kind, key, &pids, &perms)?
    };
    if let Some(current) = &existing {
        if kind == "shop.pipeline" {
            pids.extend(strings(&current["product_ids"])?);
            pids.sort();
            pids.dedup();
        }
    }
    let grant_expires = existing
        .as_ref()
        .map_or(now() + 7.0 * 86400.0, |r| number(r, "expires"));
    let raw_existing = existing
        .as_ref()
        .map(|r| current(ctx, text(r, "id")))
        .transpose()?;
    draft(
        ctx,
        &json!({"shop_id":sid,"kind":kind,"product_ids":pids,"permissions":perms,"expires":grant_expires}),
        raw_existing.as_ref(),
    )?;
    if let Some(e) = &existing {
        bindings(ctx, text(e, "id"))?;
    }
    pids.sort();
    let rid = id();
    let mut rng = rand::rngs::OsRng;
    let mut created = false;
    for _ in 0..8 {
        let raw_code = (0..12)
            .map(|_| ALPHABET[rng.gen_range(0..ALPHABET.len())] as char)
            .collect::<String>();
        let code = code(&raw_code).unwrap();
        if row(&ctx.conn,"SELECT 1 FROM cli_device_requests WHERE user_code=? UNION ALL SELECT 1 FROM cli_scope_requests WHERE user_code=? LIMIT 1",params![code,code])?.is_some(){continue;}
        ctx.conn.execute("INSERT INTO cli_scope_requests(id,public_key,client_name,fingerprint,nonce,user_code,challenge,request_json,kind,shop_id,requested_product_ids,requested_permissions,grant_expires,current_authorization_id,current_revision,expires,created,agent_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",params![rid,key,name,cli::fingerprint(&raw),nonce,code,token(),canonical,kind,sid,serde_json::to_string(&pids)?,serde_json::to_string(&perms)?,grant_expires,existing.as_ref().and_then(|v|v["id"].as_str()),existing.as_ref().map(|v|integer(v,"revision",1)),(now()+600.0).min(issued+600.0),now(),agent])?;
        created = true;
        break;
    }
    if !created {
        return Err(ApiError::new(503, "暂时无法生成设备码，请稍后重试"));
    }
    audit(&ctx.conn, "pending", "cli.scope.request", &rid, Some(&sid))?;
    Ok(Reply::json(request_view(ctx, &request(ctx, &rid)?)))
}
fn status(ctx: &Context) -> Result<Reply> {
    cli::handshake(ctx)?;
    auth::throttle(ctx, "scope-poll", 120, 60.0, None)?;
    cli::fields(ctx, &["public_key", "signature", "request_id"])?;
    let key = cli::input(ctx, "public_key", 43, 43)?;
    let rid = cli::input(ctx, "request_id", 1, 100)?;
    cli::verify(
        key,
        cli::input(ctx, "signature", 86, 86)?,
        &format!(
            "extore-cli-scope-status-v1\n{}\n{rid}\n{key}",
            ctx.app.origin
        ),
    )?;
    let r = request(ctx, rid)?;
    if !same(text(&r, "public_key"), key) {
        return Err(ApiError::new(401, "设备授权申请不匹配"));
    }
    let state = if number(&r, "expires") <= now() {
        "expired"
    } else {
        text(&r, "state")
    };
    let interval = integer(&r, "poll_interval", 5);
    let mut result = json!({"status":state,"expires":r["expires"],"interval":interval});
    if ["pending", "approved"].contains(&state) {
        if number(&r, "poll_after") > now() {
            let interval = (interval + 5).min(60);
            ctx.conn.execute(
                "UPDATE cli_scope_requests SET poll_interval=?,poll_after=? WHERE id=?",
                params![interval, now() + interval as f64, rid],
            )?;
            result = json!({"status":"slow_down","expires":r["expires"],"interval":interval,"retry_after":interval});
        } else {
            ctx.conn.execute(
                "UPDATE cli_scope_requests SET poll_after=? WHERE id=?",
                params![now() + interval as f64, rid],
            )?;
        }
    }
    Ok(Reply::json(result))
}
fn approval(ctx: &Context, action: &str) -> Result<Reply> {
    let s = browser(ctx, &format!("scope-{action}"))?;
    let allowed = match action {
        "options" => vec!["user_code", "product_ids", "permissions", "expires"],
        "approve" => vec![
            "user_code",
            "product_ids",
            "permissions",
            "expires",
            "review_digest",
        ],
        _ => vec!["user_code"],
    };
    cli::fields(ctx, &allowed)?;
    let r = pending(ctx, &s)?;
    let shop = owner(ctx, &s, text(&r, "shop_id"))?;
    if action == "deny" {
        ctx.conn.execute("UPDATE cli_scope_requests SET state='denied',approved_session_digest=?,approved_role=?,approved_shop_id=? WHERE id=? AND state='pending'",params![text(&s,"digest"),text(&s,"role"),s["shop_id"].as_str(),text(&r,"id")])?;
        audit(
            &ctx.conn,
            &actor(&s),
            "cli.scope.deny",
            text(&r, "id"),
            Some(text(&r, "shop_id")),
        )?;
        return Ok(Reply::json(json!({"ok":true,"status":"denied"})));
    }
    let (draft, current) = selected(ctx, &r, &ctx.body)?;
    let snapshot = snapshot(ctx, &r, &draft)?;
    let review = review_digest(&snapshot, &s)?;
    if action == "approve" {
        auth::recent(&s)?;
        if !ctx.body["product_ids"].is_array()
            || !ctx.body["permissions"].is_array()
            || !ctx.body["expires"].is_number()
            || !same(&review, cli::input(ctx, "review_digest", 64, 64)?)
        {
            return Err(ApiError::new(409, "申请或商品授权范围已变化，请重新核对"));
        }
        ctx.conn.execute("UPDATE cli_scope_requests SET state='approved',approved_draft=?,approved_snapshot=?,approved_session_digest=?,approved_role=?,approved_shop_id=? WHERE id=? AND state='pending'",params![canonical(&draft)?,snapshot,text(&s,"digest"),text(&s,"role"),s["shop_id"].as_str(),text(&r,"id")])?;
        audit(
            &ctx.conn,
            &actor(&s),
            "cli.scope.approve",
            text(&r, "id"),
            Some(text(&r, "shop_id")),
        )?;
        return Ok(Reply::json(json!({"ok":true,"status":"approved"})));
    }
    let signed = parse(text(&r, "request_json"))?;
    let mut products = vec![];
    for pid in strings(&json_field(&r, "requested_product_ids", Value::Null))? {
        let p = product(ctx, &pid, text(&r, "shop_id"), text(&r, "kind"))?;
        let config = parse(text(&p, "config"))?;
        products.push(json!({"id":pid,"name":config["name"],"mode":config["mode"]}));
    }
    Ok(Reply::json(
        json!({"flow":"scope","request":{"request_id":r["id"],"user_code":r["user_code"],"client_name":r["client_name"],"agent_type":r["agent_type"],"fingerprint":r["fingerprint"],"expires":r["expires"],"kind":r["kind"],"shop_id":r["shop_id"],"requested_product_ids":json_field(&r,"requested_product_ids",json!([])),"requested_permissions":json_field(&r,"requested_permissions",json!([])),"grant_expires":r["grant_expires"],"authorization_id":signed["authorization_id"],"expected_revision":if signed["authorization_id"].is_null(){Value::Null}else{r["current_revision"].clone()},"reason":signed["reason"]},"shop":{"id":shop["id"],"name":shop["name"]},"products":products,"current":current,"selected":draft,"snapshot_digest":digest(&snapshot),"review_digest":review}),
    ))
}
fn claim(ctx: &Context) -> Result<Reply> {
    cli::handshake(ctx)?;
    auth::throttle(ctx, "scope-claim", 30, 60.0, None)?;
    cli::fields(ctx, &["public_key", "signature", "request_id"])?;
    let rid = cli::input(ctx, "request_id", 1, 100)?;
    let r = request(ctx, rid)?;
    let key = cli::input(ctx, "public_key", 43, 43)?;
    cli::verify(
        key,
        cli::input(ctx, "signature", 86, 86)?,
        &format!(
            "extore-cli-scope-claim-v1\n{}\n{rid}\n{}\n{key}",
            ctx.app.origin,
            text(&r, "challenge")
        ),
    )?;
    if !same(text(&r, "public_key"), key)
        || number(&r, "expires") <= now()
        || !["approved", "claimed"].contains(&text(&r, "state"))
    {
        return Err(ApiError::new(401, "设备授权尚未批准或已失效"));
    }
    if text(&r, "state") == "claimed" {
        let raw = current(ctx, text(&r, "authorization_id"))?;
        if raw["revision"] != r["claimed_revision"] || !same(text(&raw, "public_key"), key) {
            return Err(ApiError::new(409, "已领取的授权发生变化，请刷新授权信息"));
        }
        return Ok(Reply::json(
            json!({"authorization":view(ctx,&raw)?,"bindings":bindings(ctx,text(&raw,"id"))?}),
        ));
    }
    let s = row(
        &ctx.conn,
        "SELECT * FROM sessions WHERE digest=? AND revoked=0 AND expires>? AND channel='browser'",
        params![text(&r, "approved_session_digest"), now()],
    )?
    .ok_or_else(|| ApiError::new(401, "浏览器设备审批已失效，请重新申请"))?;
    if s["role"] != r["approved_role"] || s["shop_id"] != r["approved_shop_id"] {
        return Err(ApiError::new(401, "浏览器设备审批已失效，请重新申请"));
    }
    owner(ctx, &s, text(&r, "shop_id"))?;
    let draft = parse(text(&r, "approved_draft"))?;
    if !same(text(&r, "approved_snapshot"), &snapshot(ctx, &r, &draft)?) {
        return Err(ApiError::new(409, "已批准的范围发生变化，请重新申请"));
    }
    let result = materialize(ctx, &draft, &r, &s)?;
    ctx.conn.execute("UPDATE cli_scope_requests SET state='claimed',authorization_id=?,claimed_revision=? WHERE id=?",params![text(&result["authorization"],"id"),integer(&result["authorization"],"revision",1),rid])?;
    audit(
        &ctx.conn,
        &actor(&s),
        "cli.scope.claim",
        text(&result["authorization"], "id"),
        Some(text(&r, "shop_id")),
    )?;
    Ok(Reply::json(result))
}
fn list(ctx: &Context) -> Result<Reply> {
    let s = auth::session(ctx, &["admin"], None)?;
    let view = if ctx.q("view").is_empty() {
        "active"
    } else {
        ctx.q("view")
    };
    if !["active", "revoked", "all"].contains(&view) {
        return Err(invalid());
    }
    let limit = if ctx.q("limit").is_empty() {
        200
    } else {
        ctx.q("limit").parse::<i64>().map_err(|_| invalid())?
    };
    if !(1..=200).contains(&limit) {
        return Err(invalid());
    }
    let sid = if ctx.q("shop_id").is_empty() {
        s["shop_id"].as_str()
    } else {
        Some(ctx.q("shop_id"))
    };
    if s["shop_id"].as_str().is_some_and(|own| sid != Some(own)) {
        return Err(ApiError::new(403, "没有此店铺的管理权限"));
    }
    if let Some(sid) = sid {
        uuid(sid)?;
    }
    let rows = rows(
        &ctx.conn,
        "SELECT * FROM pipeline_authorizations WHERE (?1 IS NULL OR shop_id=?1) AND (?2='all' OR (?2='active' AND revoked=0 AND expires>?3) OR (?2='revoked' AND (revoked=1 OR expires<=?3))) ORDER BY created DESC,id LIMIT ?4",
        params![sid, view, now(), limit],
    )?;
    let mut results = vec![];
    for r in rows {
        let mut v = super::view(ctx, &r)?;
        v["revoked"] = r["revoked"].clone();
        let shop = row(
            &ctx.conn,
            "SELECT name FROM shops WHERE id=?",
            [text(&r, "shop_id")],
        )?;
        v["shop_name"] = json!(shop.as_ref().map_or("", |s| text(s, "name")));
        let pids = strings(&v["product_ids"])?;
        let mut products = vec![];
        for pid in &pids {
            let p = row(
                &ctx.conn,
                "SELECT config,shop_id FROM products WHERE id=?",
                [pid],
            )?;
            let config = if let Some(p) = p.filter(|p| p["shop_id"] == r["shop_id"]) {
                parse(text(&p, "config"))?
            } else {
                json!({"name":"商品已不可用","mode":"unavailable"})
            };
            products.push(json!({"id":pid,"name":config["name"],"mode":config["mode"]}));
        }
        v["products"] = json!(products);
        v["bindings_count"] = json!(pids.len());
        results.push(v);
    }
    Ok(Reply::json(json!(results)))
}
pub(super) fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    if ctx.is("POST", "/api/cli/scopes/request") {
        return Ok(Some(create_request(ctx)?));
    }
    if ctx.is("POST", "/api/cli/scopes/status") {
        return Ok(Some(status(ctx)?));
    }
    if ctx.is("POST", "/api/cli/scopes/claim") {
        return Ok(Some(claim(ctx)?));
    }
    if ctx.method == "POST" {
        for action in ["options", "approve", "deny"] {
            if ctx.path == format!("/api/manage/device/{action}") {
                if code(ctx.b("user_code")).is_some_and(|code| {
                    row(
                        &ctx.conn,
                        "SELECT 1 FROM cli_scope_requests WHERE user_code=?",
                        [code],
                    )
                    .ok()
                    .flatten()
                    .is_some()
                }) {
                    return Ok(Some(approval(ctx, action)?));
                }
                return Ok(None);
            }
        }
    }
    if ctx.is("GET", "/api/admin/pipeline-authorizations") {
        return Ok(Some(list(ctx)?));
    }
    if ctx.method == "DELETE" {
        if let Some(aid) = ctx.path.strip_prefix("/api/admin/pipeline-authorizations/") {
            uuid(aid)?;
            cli::fields(ctx, &["expected_revision"])?;
            let expected = ctx.body["expected_revision"]
                .as_i64()
                .filter(|v| *v >= 1)
                .ok_or_else(invalid)?;
            let s = auth::session(ctx, &["admin"], None)?;
            auth::recent(&s)?;
            let r = required(
                &ctx.conn,
                "SELECT * FROM pipeline_authorizations WHERE id=?",
                [aid],
                "授权不存在",
            )?;
            if s["shop_id"]
                .as_str()
                .is_some_and(|own| own != text(&r, "shop_id"))
            {
                return Err(ApiError::new(403, "没有此店铺的管理权限"));
            }
            if integer(&r, "revision", 1) != expected {
                return Err(ApiError::new(409, "授权范围已变化，请重新核对后撤销"));
            }
            let result = revoke(ctx, aid, &actor(&s))?;
            return Ok(Some(Reply::json(
                json!({"ok":true,"id":aid,"revoked_bindings":result["bindings"],"released_jobs":result["jobs"]}),
            )));
        }
    }
    Ok(None)
}

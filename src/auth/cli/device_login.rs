use super::*;
use rand::Rng;

const ALPHABET: &[u8] = b"ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
fn request(ctx: &Context, rid: &str) -> Result<Value> {
    row(
        &ctx.conn,
        "SELECT * FROM cli_device_requests WHERE id=?",
        [rid],
    )?
    .ok_or_else(|| ApiError::new(401, "设备授权请求无效"))
}
fn normalized_code(code: &str) -> Option<String> {
    let raw = code.trim().to_ascii_uppercase().replace('-', "");
    if raw.len() != 12 || !raw.bytes().all(|b| ALPHABET.contains(&b)) {
        return None;
    }
    Some(
        raw.as_bytes()
            .chunks(4)
            .map(|s| std::str::from_utf8(s).unwrap())
            .collect::<Vec<_>>()
            .join("-"),
    )
}
fn browser(ctx: &Context, purpose: &str) -> Result<Value> {
    if ctx.headers.contains_key("authorization") {
        return Err(ApiError::new(401, "请使用已登录的浏览器批准设备"));
    }
    let s = session(ctx, &["admin", "staff"], None)?;
    if text(&s, "channel") != "browser" {
        return Err(ApiError::new(401, "请使用已登录的浏览器批准设备"));
    }
    throttle(
        ctx,
        &format!("device-{purpose}"),
        20,
        60.0,
        Some(text(&s, "id")),
    )?;
    Ok(s)
}
fn pending(ctx: &Context, s: &Value) -> Result<Value> {
    let raw = input(ctx, "user_code", 1, 64)?;
    let found = if let Some(code) = normalized_code(raw) {
        row(
            &ctx.conn,
            "SELECT * FROM cli_device_requests WHERE user_code=? AND expires>? AND state='pending'",
            params![code, now()],
        )?
    } else {
        None
    };
    if let Some(r) = found {
        return Ok(r);
    }
    throttle(ctx, "device-invalid-code", 5, 60.0, Some(text(s, "id")))?;
    Err(ApiError::new(401, "设备码无效、已过期或已处理"))
}
fn scope(ctx: &Context, staff: &Value, key: Option<&str>) -> Result<Value> {
    let p = required(
        &ctx.conn,
        "SELECT config FROM products WHERE id=?",
        [text(staff, "product_id")],
        "商品不存在",
    )?;
    let own = shop(ctx, text(staff, "shop_id"))?;
    let bound = if let Some(key) = key {
        row(
            &ctx.conn,
            "SELECT 1 FROM cli_devices WHERE staff_id=? AND public_key=? AND revoked=0",
            params![text(staff, "id"), key],
        )?
        .is_some()
    } else {
        false
    };
    Ok(
        json!({"staff_id":staff["id"],"link_name":staff["name"],"product_id":staff["product_id"],"product_name":parse(text(&p,"config"))?["name"],"shop_id":staff["shop_id"],"shop_name":own["name"],"permissions":staff["permissions"],"expires":staff["expires"],"remaining_cli_uses":(integer(staff,"max_cli_uses",0)-integer(staff,"cli_uses",0)).max(0),"already_bound":bound}),
    )
}
fn select(ctx: &Context, s: &Value, r: &Value, lid: &str, capacity: bool) -> Result<Value> {
    if text(s, "role") == "staff" && text(s, "staff_id") != lid {
        return Err(ApiError::new(403, "只能批准当前商品授权的 CLI 设备"));
    }
    let staff = staff_authorization(ctx, lid)?;
    authorize_product(ctx, s, text(&staff, "product_id"))?;
    if !r["requested_product_id"].is_null() && r["requested_product_id"] != staff["product_id"] {
        return Err(ApiError::new(403, "设备申请与所选商品不匹配"));
    }
    let existing = row(
        &ctx.conn,
        "SELECT revoked FROM cli_devices WHERE staff_id=? AND public_key=?",
        params![lid, text(r, "public_key")],
    )?;
    if existing.as_ref().is_some_and(|d| flag(d, "revoked")) {
        return Err(ApiError::new(401, "CLI 设备授权已撤销，请使用新的设备密钥"));
    }
    if capacity
        && existing.is_none()
        && integer(&staff, "cli_uses", 0) >= integer(&staff, "max_cli_uses", 0)
    {
        return Err(ApiError::new(409, "此商品授权已达到 CLI 设备绑定次数上限"));
    }
    Ok(staff)
}
fn snapshot(ctx: &Context, r: &Value, staff: &Value) -> Result<String> {
    let mut scope = scope(ctx, staff, None)?;
    scope.as_object_mut().unwrap().remove("remaining_cli_uses");
    scope.as_object_mut().unwrap().remove("already_bound");
    let mut chain = Vec::new();
    let mut current = text(staff, "id").to_owned();
    let mut seen = HashSet::new();
    while !current.is_empty() {
        if !seen.insert(current.clone()) {
            return Err(denied());
        }
        let link = required(
            &ctx.conn,
            "SELECT * FROM staff WHERE id=?",
            [&current],
            "商品管理链接失效",
        )?;
        let mut part = json!({});
        for k in [
            "id",
            "product_id",
            "name",
            "parent_id",
            "permissions",
            "expires",
            "revoked",
            "archived",
            "max_cli_uses",
        ] {
            part[k] = link[k].clone();
        }
        chain.push(part);
        current = text(&link, "parent_id").to_owned();
    }
    let mut req = json!({});
    for k in [
        "id",
        "public_key",
        "client_name",
        "fingerprint",
        "nonce",
        "challenge",
        "requested_product_id",
        "expires",
    ] {
        req[k] = r[k].clone();
    }
    if !r["agent_type"].is_null() {
        req["agent_type"] = r["agent_type"].clone();
    }
    ascii_json(&json!({"request":req,"scope":scope,"chain":chain}))
}
fn review_digest(snapshot: &str, s: &Value) -> Result<String> {
    let mut browser = json!({});
    for k in ["digest", "role", "shop_id", "staff_id"] {
        browser[k] = s[k].clone();
    }
    Ok(digest(&ascii_json(
        &json!({"scope_snapshot":snapshot,"browser":browser}),
    )?))
}
fn public_request(r: &Value) -> Value {
    json!({"request_id":r["id"],"user_code":r["user_code"],"client_name":r["client_name"],"agent_type":r["agent_type"],"fingerprint":r["fingerprint"],"product_id":r["requested_product_id"],"expires":r["expires"]})
}
fn request_view(ctx: &Context, r: &Value) -> Value {
    let mut result = public_request(r);
    result["approval_url"] = json!(format!("{}/cli/device", ctx.app.origin));
    result["challenge"] = r["challenge"].clone();
    result["expires_in"] = json!((number(r, "expires") - now() + 0.999).max(0.0) as i64);
    result["interval"] = r["poll_interval"].clone();
    result
}
fn approval_actor(ctx: &Context, r: &Value) -> Result<Value> {
    let mut s = row(
        &ctx.conn,
        "SELECT * FROM sessions WHERE digest=? AND revoked=0 AND expires>? AND channel='browser'",
        params![text(r, "approved_session_digest"), now()],
    )?
    .ok_or_else(|| ApiError::new(401, "浏览器设备审批已失效，请重新申请"))?;
    if s["role"] != r["approved_role"]
        || s["shop_id"] != r["approved_shop_id"]
        || !["admin", "staff"].contains(&text(&s, "role"))
    {
        return Err(ApiError::new(401, "浏览器设备审批已失效，请重新申请"));
    }
    if text(&s, "role") == "staff" {
        let staff = staff_authorization(ctx, text(&s, "staff_id"))?;
        if !s["shop_id"].is_null() && s["shop_id"] != staff["shop_id"] {
            return Err(denied());
        }
        for k in ["shop_id", "product_id", "permissions"] {
            s[k] = staff[k].clone();
        }
    } else if let Some(sid) = s["shop_id"].as_str() {
        shop(ctx, sid)?;
    }
    Ok(s)
}
pub(super) fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    let path = ctx.path.clone();
    if path.starts_with("/api/manage/device/") {
        // Persistent pipeline authorization shares this UI; its module handles those codes.
        if let Some(code) = normalized_code(ctx.b("user_code")) {
            if row(
                &ctx.conn,
                "SELECT 1 FROM cli_scope_requests WHERE user_code=?",
                [code],
            )?
            .is_some()
            {
                return Ok(None);
            }
        }
    }
    if ctx.is("POST", "/api/cli/device/request") {
        handshake(ctx)?;
        fields(
            ctx,
            &[
                "public_key",
                "signature",
                "client_name",
                "agent_type",
                "nonce",
                "product_id",
            ],
        )?;
        throttle(ctx, "device-request", 10, 60.0, None)?;
        let (name, agent) = identity(
            input(ctx, "client_name", 1, 100)?,
            ctx.body["agent_type"].as_str(),
        )?;
        if name != ctx.b("client_name") {
            return Err(ApiError::bad("处理端名称或类型须去除首尾空格"));
        }
        let nonce = input(ctx, "nonce", 43, 43)?;
        let raw_nonce = decode(nonce, 32)?;
        let issued = u64::from_be_bytes(raw_nonce[..8].try_into().map_err(|_| invalid())?) as f64;
        let key = input(ctx, "public_key", 43, 43)?;
        let product = if ctx.body["product_id"].is_null() {
            None
        } else {
            Some(input(ctx, "product_id", 1, 100)?)
        };
        let raw = verify(
            key,
            input(ctx, "signature", 86, 86)?,
            &format!(
                "extore-cli-device-request-v1\n{}\n{key}\n{name}\n{nonce}\n{}{}",
                ctx.app.origin,
                product.unwrap_or(""),
                agent.as_ref().map(|a| format!("\n{a}")).unwrap_or_default()
            ),
        )?;
        if let Some(r) = row(
            &ctx.conn,
            "SELECT * FROM cli_device_requests WHERE public_key=? AND nonce=?",
            params![key, nonce],
        )? {
            if text(&r, "client_name") != name
                || r["agent_type"].as_str() != agent.as_deref()
                || r["requested_product_id"].as_str() != product
            {
                return Err(ApiError::new(401, "设备授权申请不匹配"));
            }
            return Ok(Some(Reply::json(request_view(ctx, &r))));
        }
        if issued > now() + 60.0 || issued + 600.0 <= now() {
            return Err(ApiError::new(401, "设备申请签名已过期，请生成新的设备码"));
        }
        ctx.conn.execute("DELETE FROM cli_device_requests WHERE id IN (SELECT id FROM cli_device_requests WHERE expires<? ORDER BY expires,id LIMIT 200)",[now()-300.0])?;
        let all = required(
            &ctx.conn,
            "SELECT count(*) n FROM cli_device_requests",
            [],
            "设备请求不可用",
        )?;
        let own = required(
            &ctx.conn,
            "SELECT count(*) n FROM cli_device_requests WHERE public_key=? AND expires>? AND state IN ('pending','approved')",
            params![key, now()],
            "设备请求不可用",
        )?;
        if integer(&all, "n", 0) >= 10000 || integer(&own, "n", 0) >= 10 {
            return Err(ApiError::new(429, "设备授权申请过多，请稍后重试"));
        }
        let rid = id();
        let mut created = false;
        let mut rng = rand::rngs::OsRng;
        for _ in 0..8 {
            let raw_code = (0..12)
                .map(|_| ALPHABET[rng.gen_range(0..ALPHABET.len())] as char)
                .collect::<String>();
            let code = raw_code
                .as_bytes()
                .chunks(4)
                .map(|s| std::str::from_utf8(s).unwrap())
                .collect::<Vec<_>>()
                .join("-");
            if row(&ctx.conn,"SELECT 1 FROM cli_scope_requests WHERE user_code=? UNION ALL SELECT 1 FROM cli_device_requests WHERE user_code=? LIMIT 1",params![code,code])?.is_some(){continue;}
            ctx.conn.execute("INSERT INTO cli_device_requests(id,public_key,client_name,fingerprint,nonce,user_code,challenge,requested_product_id,expires,created,agent_type) VALUES (?,?,?,?,?,?,?,?,?,?,?)",params![rid,key,name,fingerprint(&raw),nonce,code,token(),product,(now()+600.0).min(issued+600.0),now(),agent])?;
            created = true;
            break;
        }
        if !created {
            return Err(ApiError::new(503, "暂时无法生成设备码，请稍后重试"));
        }
        audit(&ctx.conn, "pending", "cli.product.request", &rid, None)?;
        return Ok(Some(Reply::json(request_view(ctx, &request(ctx, &rid)?))));
    }
    if ctx.is("POST", "/api/cli/device/status") {
        handshake(ctx)?;
        fields(ctx, &["request_id", "public_key", "signature"])?;
        throttle(ctx, "device-poll", 120, 60.0, None)?;
        let rid = input(ctx, "request_id", 1, 100)?;
        let key = input(ctx, "public_key", 43, 43)?;
        verify(
            key,
            input(ctx, "signature", 86, 86)?,
            &format!(
                "extore-cli-device-status-v1\n{}\n{rid}\n{key}",
                ctx.app.origin
            ),
        )?;
        let r = request(ctx, rid)?;
        if !bool::from(text(&r, "public_key").as_bytes().ct_eq(key.as_bytes())) {
            return Err(ApiError::new(401, "设备授权申请不匹配"));
        }
        let state = if number(&r, "expires") <= now() {
            "expired"
        } else {
            text(&r, "state")
        };
        let result = json!({"status":state,"expires":r["expires"],"interval":r["poll_interval"]});
        if !["pending", "approved"].contains(&state) {
            return Ok(Some(Reply::json(result)));
        }
        if number(&r, "poll_after") > now() {
            let interval = (integer(&r, "poll_interval", 5) + 5).min(60);
            ctx.conn.execute(
                "UPDATE cli_device_requests SET poll_interval=?,poll_after=? WHERE id=?",
                params![interval, now() + interval as f64, rid],
            )?;
            return Ok(Some(Reply::json(
                json!({"status":"slow_down","expires":r["expires"],"interval":interval,"retry_after":interval}),
            )));
        }
        ctx.conn.execute(
            "UPDATE cli_device_requests SET poll_after=? WHERE id=?",
            params![now() + number(&r, "poll_interval"), rid],
        )?;
        return Ok(Some(Reply::json(result)));
    }
    if ctx.is("POST", "/api/manage/device/options") {
        fields(ctx, &["user_code", "staff_id"])?;
        let s = browser(ctx, "options")?;
        let r = pending(ctx, &s)?;
        let selected = if ctx.b("staff_id").is_empty() {
            None
        } else {
            Some(select(ctx, &s, &r, input(ctx, "staff_id", 1, 100)?, true)?)
        };
        let ids = rows(
            &ctx.conn,
            "SELECT st.id FROM staff st JOIN products p ON p.id=st.product_id WHERE st.revoked=0 AND st.archived=0 AND st.expires>? AND NOT EXISTS(SELECT 1 FROM pipeline_bindings b WHERE b.staff_id=st.id) AND (st.cli_uses<st.max_cli_uses OR EXISTS(SELECT 1 FROM cli_devices d WHERE d.staff_id=st.id AND d.public_key=? AND d.revoked=0)) AND (?!='staff' OR st.id=?) AND (? IS NULL OR p.shop_id=?) AND (? IS NULL OR st.product_id=?) ORDER BY st.created DESC,st.id LIMIT 201",
            params![
                now(),
                text(&r, "public_key"),
                text(&s, "role"),
                text(&s, "staff_id"),
                s["shop_id"].as_str(),
                s["shop_id"].as_str(),
                r["requested_product_id"].as_str(),
                r["requested_product_id"].as_str()
            ],
        )?;
        let mut candidates = Vec::new();
        for value in ids.iter().take(200) {
            match select(ctx, &s, &r, text(value, "id"), true) {
                Ok(staff) => candidates.push(scope(ctx, &staff, r["public_key"].as_str())?),
                Err(e) if e.status < 500 => {}
                Err(e) => return Err(e),
            }
        }
        let mut result = json!({"request":public_request(&r),"candidates":candidates,"candidates_truncated":ids.len()>200});
        if let Some(staff) = selected {
            let snapshot = snapshot(ctx, &r, &staff)?;
            result["selected"] = scope(ctx, &staff, r["public_key"].as_str())?;
            result["snapshot_digest"] = json!(digest(&snapshot));
            result["review_digest"] = json!(review_digest(&snapshot, &s)?);
        }
        return Ok(Some(Reply::json(result)));
    }
    if ctx.is("POST", "/api/manage/device/approve") {
        fields(ctx, &["user_code", "staff_id", "review_digest"])?;
        let s = browser(ctx, "approve")?;
        let r = pending(ctx, &s)?;
        let staff = select(ctx, &s, &r, input(ctx, "staff_id", 1, 100)?, true)?;
        if text(&s, "role") == "admin" {
            recent(&s)?;
        }
        let snapshot = snapshot(ctx, &r, &staff)?;
        let expected = review_digest(&snapshot, &s)?;
        if !bool::from(
            expected
                .as_bytes()
                .ct_eq(input(ctx, "review_digest", 64, 64)?.as_bytes()),
        ) {
            return Err(ApiError::new(
                409,
                "商品授权范围已变化，请重新核对设备与权限",
            ));
        }
        ctx.conn.execute("UPDATE cli_device_requests SET state='approved',approved_staff_id=?,approved_snapshot=?,approved_session_digest=?,approved_role=?,approved_shop_id=? WHERE id=? AND state='pending'",params![text(&staff,"id"),snapshot,text(&s,"digest"),text(&s,"role"),s["shop_id"].as_str(),text(&r,"id")])?;
        audit(
            &ctx.conn,
            &actor(&s),
            "cli.product.approve",
            text(&r, "id"),
            staff["shop_id"].as_str(),
        )?;
        return Ok(Some(Reply::json(json!({"ok":true,"status":"approved"}))));
    }
    if ctx.is("POST", "/api/manage/device/deny") {
        fields(ctx, &["user_code"])?;
        let s = browser(ctx, "deny")?;
        let r = pending(ctx, &s)?;
        if text(&s, "role") == "staff"
            && !r["requested_product_id"].is_null()
            && r["requested_product_id"] != s["product_id"]
        {
            return Err(ApiError::new(403, "设备申请与当前商品不匹配"));
        }
        if text(&s, "role") == "admin" && !r["requested_product_id"].is_null() {
            authorize_product(ctx, &s, text(&r, "requested_product_id"))?;
        }
        ctx.conn.execute("UPDATE cli_device_requests SET state='denied',approved_role=?,approved_shop_id=?,approved_staff_id=?,approved_session_digest=? WHERE id=? AND state='pending'",params![text(&s,"role"),s["shop_id"].as_str(),s["staff_id"].as_str(),text(&s,"digest"),text(&r,"id")])?;
        audit(
            &ctx.conn,
            &actor(&s),
            "cli.product.deny",
            text(&r, "id"),
            s["shop_id"].as_str(),
        )?;
        return Ok(Some(Reply::json(json!({"ok":true,"status":"denied"}))));
    }
    if ctx.is("POST", "/api/cli/device/claim") {
        handshake(ctx)?;
        fields(ctx, &["request_id", "public_key", "signature"])?;
        throttle(ctx, "device-claim", 30, 60.0, None)?;
        let r = request(ctx, input(ctx, "request_id", 1, 100)?)?;
        let key = input(ctx, "public_key", 43, 43)?;
        verify(
            key,
            input(ctx, "signature", 86, 86)?,
            &format!(
                "extore-cli-device-claim-v1\n{}\n{}\n{}\n{key}",
                ctx.app.origin,
                text(&r, "id"),
                text(&r, "challenge")
            ),
        )?;
        if !bool::from(text(&r, "public_key").as_bytes().ct_eq(key.as_bytes()))
            || number(&r, "expires") <= now()
            || !["approved", "claimed"].contains(&text(&r, "state"))
            || r["approved_staff_id"].is_null()
        {
            return Err(ApiError::new(401, "设备授权尚未批准或已失效"));
        }
        let mut staff = staff_authorization(ctx, text(&r, "approved_staff_id"))?;
        if text(&r, "approved_snapshot") != snapshot(ctx, &r, &staff)? {
            return Err(ApiError::new(
                401,
                "已批准的商品授权范围发生变化，请重新申请",
            ));
        }
        let existing = row(
            &ctx.conn,
            "SELECT * FROM cli_devices WHERE staff_id=? AND public_key=?",
            params![text(&staff, "id"), key],
        )?;
        if existing.as_ref().is_some_and(|d| flag(d, "revoked")) {
            return Err(ApiError::new(401, "CLI 设备授权已撤销，请使用新的设备密钥"));
        }
        let already = text(&r, "state") == "claimed";
        let already_bound = existing.is_some();
        let d = if already {
            let d = existing.ok_or_else(invalid)?;
            if d["id"] != r["device_id"]
                || d["client_name"] != r["client_name"]
                || (!r["agent_type"].is_null() && r["agent_type"] != d["agent_type"])
            {
                return Err(ApiError::new(401, "CLI 设备授权已失效"));
            }
            d
        } else {
            let s = approval_actor(ctx, &r)?;
            select(ctx, &s, &r, text(&staff, "id"), existing.is_none())?;
            let d = if let Some(d) = existing {
                let agent = r["agent_type"].as_str().or(d["agent_type"].as_str());
                ctx.conn.execute(
                    "UPDATE cli_devices SET client_name=?,agent_type=? WHERE id=?",
                    params![text(&r, "client_name"), agent, text(&d, "id")],
                )?;
                required(
                    &ctx.conn,
                    "SELECT * FROM cli_devices WHERE id=?",
                    [text(&d, "id")],
                    "CLI 设备不存在",
                )?
            } else {
                let (d, new_staff) = create_staff_device(
                    ctx,
                    &staff,
                    key,
                    text(&r, "client_name"),
                    r["agent_type"].as_str(),
                )?;
                staff = new_staff;
                d
            };
            ctx.conn.execute(
                "UPDATE cli_device_requests SET state='claimed',device_id=? WHERE id=?",
                params![text(&d, "id"), text(&r, "id")],
            )?;
            audit(
                &ctx.conn,
                &actor(&s),
                "cli.product.claim",
                text(&d, "id"),
                staff["shop_id"].as_str(),
            )?;
            d
        };
        // Revalidate mapped pipeline devices before returning a reusable grant.
        device(ctx, text(&d, "id"))?;
        return Ok(Some(Reply::json(bind_view(&d, &staff, already_bound))));
    }
    Ok(None)
}

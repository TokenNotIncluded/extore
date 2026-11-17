use super::*;
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use ed25519_dalek::{Signature, VerifyingKey};
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;
use unicode_general_category::{GeneralCategory, get_general_category};
mod device_login;
mod owner;

pub fn invalid() -> ApiError {
    ApiError::new(401, "设备签名无效")
}
pub fn fields(ctx: &Context, allowed: &[&str]) -> Result<()> {
    let o = ctx
        .body
        .as_object()
        .ok_or_else(|| ApiError::bad("授权参数无效"))?;
    if o.keys().any(|k| !allowed.contains(&k.as_str())) {
        return Err(ApiError::bad("授权参数无效"));
    }
    Ok(())
}
pub fn input<'a>(ctx: &'a Context, name: &str, min: usize, max: usize) -> Result<&'a str> {
    let v = ctx.body[name]
        .as_str()
        .filter(|v| (min..=max).contains(&v.chars().count()))
        .ok_or_else(|| ApiError::bad("授权参数无效"))?;
    Ok(v)
}
pub fn hidden(c: char) -> bool {
    matches!(
        get_general_category(c),
        GeneralCategory::Control
            | GeneralCategory::Format
            | GeneralCategory::Surrogate
            | GeneralCategory::PrivateUse
            | GeneralCategory::Unassigned
    )
}
pub fn identity(name: &str, agent: Option<&str>) -> Result<(String, Option<String>)> {
    let clean = |s: &str, max: usize, exact: bool| {
        let trimmed = s.trim();
        if trimmed.is_empty()
            || trimmed.chars().count() > max
            || s.chars().any(hidden)
            || (exact && s != trimmed)
            || s.chars().any(|c| {
                matches!(
                    get_general_category(c),
                    GeneralCategory::LineSeparator | GeneralCategory::ParagraphSeparator
                ) || (c != ' ' && get_general_category(c) == GeneralCategory::SpaceSeparator)
            })
        {
            Err(ApiError::bad("处理端名称或类型无效"))
        } else {
            Ok(trimmed.to_owned())
        }
    };
    Ok((
        clean(name, 100, agent.is_some())?,
        agent.map(|v| clean(v, 64, true)).transpose()?,
    ))
}
pub fn decode(value: &str, size: usize) -> Result<Vec<u8>> {
    let raw = URL_SAFE_NO_PAD.decode(value).map_err(|_| invalid())?;
    if raw.len() != size || URL_SAFE_NO_PAD.encode(&raw) != value {
        return Err(invalid());
    }
    Ok(raw)
}
pub fn verify(public_key: &str, signature: &str, proof: &str) -> Result<Vec<u8>> {
    let raw = decode(public_key, 32)?;
    let key = VerifyingKey::from_bytes(&raw.clone().try_into().map_err(|_| invalid())?)
        .map_err(|_| invalid())?;
    let signature = Signature::from_slice(&decode(signature, 64)?).map_err(|_| invalid())?;
    key.verify_strict(proof.as_bytes(), &signature)
        .map_err(|_| invalid())?;
    Ok(raw)
}
pub fn handshake(ctx: &Context) -> Result<()> {
    if ctx.headers.contains_key("authorization") || !ctx.cookie("extore_session").is_empty() {
        return Err(ApiError::bad("CLI 授权不能混用浏览器登录凭证"));
    }
    Ok(())
}
pub fn fingerprint(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
pub fn ascii_json(value: &Value) -> Result<String> {
    let json = serde_json::to_string(value)?;
    let mut out = String::with_capacity(json.len());
    for ch in json.chars() {
        if ch.is_ascii() {
            out.push(ch)
        } else {
            let mut units = [0u16; 2];
            for unit in ch.encode_utf16(&mut units) {
                out.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    Ok(out)
}
fn link_token(ctx: &Context, value: &str) -> Result<(String, bool)> {
    let mut ticket = false;
    let token = if let Some(value) = value.strip_prefix('#') {
        value.to_owned()
    } else if value.contains("://") {
        let parsed = url::Url::parse(value).map_err(|_| invalid())?;
        let origin = url::Url::parse(&ctx.app.origin).map_err(|_| invalid())?;
        if parsed.origin() != origin.origin()
            || !["/staff", "/cli"].contains(&parsed.path())
            || parsed.query().is_some()
            || !parsed.username().is_empty()
            || parsed.password().is_some()
        {
            return Err(ApiError::new(401, "商品管理链接无效"));
        }
        ticket = parsed.path() == "/cli";
        parsed.fragment().unwrap_or("").to_owned()
    } else {
        value.to_owned()
    };
    if token.is_empty()
        || !token
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
    {
        return Err(ApiError::new(401, "商品管理链接无效"));
    }
    Ok((token, ticket))
}
pub fn device(ctx: &Context, did: &str) -> Result<(Value, Value)> {
    let d = row(
        &ctx.conn,
        "SELECT * FROM cli_devices WHERE id=? AND revoked=0",
        [did],
    )?
    .ok_or_else(|| ApiError::new(401, "CLI 设备授权已失效"))?;
    let mut staff = staff_authorization(ctx, text(&d, "staff_id"))?;
    let mappings = rows(
        &ctx.conn,
        "SELECT * FROM pipeline_bindings WHERE device_id=? OR staff_id=?",
        params![did, text(&staff, "id")],
    )?;
    if !mappings.is_empty() {
        if mappings.len() != 1
            || text(&mappings[0], "device_id") != did
            || mappings[0]["staff_id"] != staff["id"]
        {
            return Err(denied());
        }
        let scope = validate_pipeline(ctx, &staff)?.ok_or_else(denied)?;
        staff["authorization_id"] = scope["id"].clone();
        staff["authorization_revision"] = scope["revision"].clone();
        staff["scope"] = scope["kind"].clone();
    }
    Ok((d, staff))
}
fn scope_metadata(source: &Value, target: &mut Value) {
    for k in ["authorization_id", "authorization_revision", "scope"] {
        if !source[k].is_null() {
            target[k] = source[k].clone();
        }
    }
}
pub fn bind_view(d: &Value, staff: &Value, already: bool) -> Value {
    let mut value = json!({"device_id":d["id"],"product_id":staff["product_id"],"shop_id":staff["shop_id"],"client_name":d["client_name"],"agent_type":d["agent_type"],"fingerprint":d["fingerprint"],"already_authorized":already,"expires":staff["expires"],"permissions":staff["permissions"],"max_cli_uses":staff["max_cli_uses"],"cli_uses":staff["cli_uses"],"remaining_cli_uses":(integer(staff,"max_cli_uses",0)-integer(staff,"cli_uses",0)).max(0)});
    scope_metadata(staff, &mut value);
    value
}
pub fn create_staff_device(
    ctx: &Context,
    staff: &Value,
    key: &str,
    name: &str,
    agent: Option<&str>,
) -> Result<(Value, Value)> {
    if integer(staff, "cli_uses", 0) >= integer(staff, "max_cli_uses", 0) {
        return Err(ApiError::new(
            409,
            "此商品管理链接已达到 CLI 设备授权次数上限",
        ));
    }
    let did = id();
    let raw = decode(key, 32)?;
    consume_link(ctx, text(staff, "id"), "cli")?;
    ctx.conn.execute("INSERT INTO cli_devices(id,staff_id,public_key,fingerprint,client_name,created,last_seen,agent_type) VALUES (?,?,?,?,?,?,?,?)",params![did,text(staff,"id"),key,fingerprint(&raw),name,now(),now(),agent])?;
    audit(
        &ctx.conn,
        text(staff, "id"),
        "cli.device.create",
        &did,
        staff["shop_id"].as_str(),
    )?;
    device(ctx, &did)
}
pub fn owner_device(ctx: &Context, did: &str) -> Result<Value> {
    owner::owner_device(ctx, did)
}
pub fn invalidate_pending_owner_approvals(ctx: &Context, sid: Option<&str>) -> Result<()> {
    let pending = rows(
        &ctx.conn,
        "SELECT id FROM owner_cli_requests WHERE scope_bound=1 AND shop_id IS ? AND state IN ('pending','approved')",
        [sid],
    )?;
    ctx.conn.execute(
        "DELETE FROM owner_cli_password_challenges WHERE shop_id IS ?",
        [sid],
    )?;
    ctx.conn.execute("DELETE FROM owner_cli_approval_challenges WHERE request_id IN (SELECT id FROM owner_cli_requests WHERE scope_bound=1 AND shop_id IS ?)",[sid])?;
    ctx.conn.execute("UPDATE owner_cli_requests SET state='denied' WHERE scope_bound=1 AND shop_id IS ? AND state IN ('pending','approved')",[sid])?;
    let who = sid.map(|s| format!("shop:{s}")).unwrap_or("owner".into());
    for p in pending {
        audit(
            &ctx.conn,
            &who,
            "cli.owner.approval.invalidate",
            text(&p, "id"),
            sid,
        )?;
    }
    Ok(())
}
pub fn verify_action(ctx: &Context) -> Result<()> {
    if !["POST", "PUT", "PATCH", "DELETE"].contains(&ctx.method.as_str())
        || ctx.path == "/api/cli/owner/action-challenge"
        || !ctx.headers.contains_key("authorization")
    {
        return Ok(());
    }
    let auth = ctx.header("authorization");
    let Some((scheme, raw)) = auth.split_once(' ') else {
        return Ok(());
    };
    if !scheme.eq_ignore_ascii_case("bearer") {
        return Ok(());
    }
    if row(
        &ctx.conn,
        "SELECT 1 FROM sessions WHERE digest=? AND channel='cli' AND role='admin'",
        [digest(raw)],
    )?
    .is_none()
    {
        return Ok(());
    }
    let s = session(ctx, &["admin"], None)?;
    let cid = ctx.header("x-extore-cli-challenge");
    let sig = ctx.header("x-extore-cli-signature");
    if cid.is_empty() || cid.len() > 100 || sig.len() != 86 {
        return Err(ApiError::new(401, "此操作需要新的商家设备签名"));
    }
    if !ctx.raw_target.is_ascii() {
        return Err(ApiError::bad("操作路径编码无效"));
    }
    let ch = row(
        &ctx.conn,
        "SELECT * FROM owner_cli_action_challenges WHERE id=? AND expires>?",
        params![cid, now()],
    )?
    .ok_or_else(invalid)?;
    let hash = fingerprint(&ctx.raw_body);
    if ch["device_id"] != s["owner_device_id"]
        || ch["session_id"] != s["id"]
        || text(&ch, "method") != ctx.method.as_str()
        || text(&ch, "path") != ctx.raw_target
        || text(&ch, "body_sha256") != hash
    {
        return Err(ApiError::new(401, "商家操作签名无效或已使用"));
    }
    let d = owner_device(ctx, text(&s, "owner_device_id"))?;
    verify(
        text(&d, "public_key"),
        sig,
        &format!(
            "extore-cli-owner-action-v1\n{}\n{}\n{}\n{}\n{}\n{}\n{}\n{}",
            ctx.app.origin,
            text(&d, "id"),
            text(&s, "id"),
            text(&ch, "id"),
            text(&ch, "challenge"),
            ctx.method,
            ctx.raw_target,
            hash
        ),
    )?;
    ctx.conn
        .execute("DELETE FROM owner_cli_action_challenges WHERE id=?", [cid])?;
    audit(
        &ctx.conn,
        &actor(&s),
        "cli.owner.action",
        text(&s, "id"),
        s["shop_id"].as_str(),
    )?;
    // A validated signature authorizes exactly one dispatch, even if it later fails.
    ctx.conn.execute_batch("COMMIT; BEGIN IMMEDIATE")?;
    Ok(())
}
fn cli_session(ctx: &Context, role: &str) -> Result<Value> {
    let s = session(ctx, &[role], None)?;
    if text(&s, "channel") != "cli" {
        return Err(ApiError::new(401, "此接口需要 CLI 授权"));
    }
    Ok(s)
}
pub fn link_visible(ctx: &Context, s: &Value, link: &str) -> Result<bool> {
    Ok(access::link_scope(ctx, s)?.is_none_or(|set| set.contains(link)))
}

fn revoke_staff_device(ctx: &Context, did: &str, who: &str, sid: Option<&str>) -> Result<usize> {
    ctx.conn
        .execute("UPDATE cli_devices SET revoked=1 WHERE id=?", [did])?;
    ctx.conn
        .execute("DELETE FROM cli_challenges WHERE device_id=?", [did])?;
    let active = rows(
        &ctx.conn,
        "SELECT digest FROM sessions WHERE device_id=? AND revoked=0",
        [did],
    )?;
    for s in &active {
        revoke_session(ctx, text(s, "digest"), who, "session.revoke")?;
    }
    audit(&ctx.conn, who, "cli.device.revoke", did, sid)?;
    Ok(active.len())
}
pub fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    if let Some(reply) = owner::handle(ctx)? {
        return Ok(Some(reply));
    }
    if let Some(reply) = device_login::handle(ctx)? {
        return Ok(Some(reply));
    }
    let path = ctx.path.clone();
    if ctx.is("POST", "/api/cli/authorize") {
        handshake(ctx)?;
        fields(
            ctx,
            &[
                "token",
                "public_key",
                "client_name",
                "agent_type",
                "signature",
            ],
        )?;
        throttle(ctx, "cli-bind", 20, 60.0, None)?;
        let original = input(ctx, "token", 1, 2048)?;
        let key = input(ctx, "public_key", 43, 43)?;
        let (name, agent) = identity(
            input(ctx, "client_name", 1, 100)?,
            ctx.body["agent_type"].as_str(),
        )?;
        let proof = format!(
            "extore-cli-bind-v1\n{}\n{original}\n{key}{}",
            ctx.app.origin,
            agent
                .as_ref()
                .map(|a| format!("\n{name}\n{a}"))
                .unwrap_or_default()
        );
        verify(key, input(ctx, "signature", 86, 86)?, &proof)?;
        let (value, ticket_only) = link_token(ctx, original)?;
        let ticket = row(
            &ctx.conn,
            "SELECT * FROM cli_bind_tickets WHERE digest=?",
            [digest(&value)],
        )?;
        let staff = if let Some(ticket) = &ticket {
            if number(ticket, "expires") <= now() {
                return Err(ApiError::new(401, "CLI 授权口令已失效"));
            }
            staff_authorization(ctx, text(ticket, "staff_id"))?
        } else {
            if ticket_only {
                return Err(ApiError::new(401, "CLI 授权口令已失效"));
            }
            let staff = row(
                &ctx.conn,
                "SELECT id FROM staff WHERE digest=?",
                [digest(&value)],
            )?
            .ok_or_else(|| ApiError::new(401, "商品管理链接无效"))?;
            staff_authorization(ctx, text(&staff, "id"))?
        };
        let existing = row(
            &ctx.conn,
            "SELECT * FROM cli_devices WHERE staff_id=? AND public_key=?",
            params![text(&staff, "id"), key],
        )?;
        if ticket.as_ref().is_some_and(|t| {
            flag(t, "consumed")
                && existing
                    .as_ref()
                    .is_none_or(|d| d["id"] != t["bound_device_id"])
        }) {
            return Err(ApiError::new(401, "CLI 授权口令已使用"));
        }
        let already = existing.is_some();
        let (d, staff) = if let Some(d) = existing {
            if flag(&d, "revoked") {
                return Err(ApiError::new(401, "CLI 设备授权已撤销，请申请新的管理链接"));
            }
            if agent.is_some()
                && (text(&d, "client_name") != name || d["agent_type"].as_str() != agent.as_deref())
            {
                return Err(ApiError::new(
                    409,
                    "更改已绑定处理端身份须重新申请设备码并由商家批准",
                ));
            }
            device(ctx, text(&d, "id"))?
        } else {
            create_staff_device(ctx, &staff, key, &name, agent.as_deref())?
        };
        if let Some(ticket) = ticket {
            ctx.conn.execute(
                "UPDATE cli_bind_tickets SET consumed=1,bound_device_id=? WHERE digest=?",
                params![text(&d, "id"), text(&ticket, "digest")],
            )?;
        }
        return Ok(Some(Reply::json(bind_view(&d, &staff, already))));
    }
    if ctx.is("POST", "/api/cli/challenge") {
        handshake(ctx)?;
        fields(ctx, &["device_id"])?;
        throttle(ctx, "cli-challenge", 60, 60.0, None)?;
        let did = input(ctx, "device_id", 1, 100)?;
        let (_, staff) = device(ctx, did)?;
        ctx.conn
            .execute("DELETE FROM cli_challenges WHERE expires<=?", [now()])?;
        let count = required(
            &ctx.conn,
            "SELECT count(*) n FROM cli_challenges WHERE device_id=?",
            [did],
            "CLI 设备不存在",
        )?;
        if integer(&count, "n", 0) >= 20 {
            return Err(ApiError::new(429, "设备请求过于频繁，请稍后重试"));
        }
        let cid = id();
        let challenge = token();
        let expires = (now() + 300.0).min(number(&staff, "expires"));
        ctx.conn.execute(
            "INSERT INTO cli_challenges VALUES (?,?,?,?,?)",
            params![cid, did, challenge, expires, now()],
        )?;
        return Ok(Some(Reply::json(
            json!({"challenge_id":cid,"challenge":challenge,"expires":expires,"expires_in":(expires-now()).ceil().max(0.0) as i64}),
        )));
    }
    if ctx.is("POST", "/api/cli/session") {
        handshake(ctx)?;
        fields(ctx, &["device_id", "challenge_id", "signature"])?;
        throttle(ctx, "cli-session", 60, 60.0, None)?;
        let (d, staff) = device(ctx, input(ctx, "device_id", 1, 100)?)?;
        let ch = row(
            &ctx.conn,
            "SELECT * FROM cli_challenges WHERE id=? AND device_id=? AND expires>?",
            params![input(ctx, "challenge_id", 1, 100)?, text(&d, "id"), now()],
        )?
        .ok_or_else(|| ApiError::new(401, "设备登录验证已失效"))?;
        verify(
            text(&d, "public_key"),
            input(ctx, "signature", 86, 86)?,
            &format!(
                "extore-cli-session-v1\n{}\n{}\n{}\n{}",
                ctx.app.origin,
                text(&d, "id"),
                text(&ch, "id"),
                text(&ch, "challenge")
            ),
        )?;
        let sid = id();
        let access = token();
        let expires = (now() + 28800.0).min(number(&staff, "expires"));
        ctx.conn
            .execute("DELETE FROM cli_challenges WHERE id=?", [text(&ch, "id")])?;
        let ua = ctx
            .header("user-agent")
            .chars()
            .filter(|c| !c.is_control())
            .take(300)
            .collect::<String>();
        ctx.conn.execute("INSERT INTO sessions(digest,role,staff_id,expires,created,id,last_seen,ip,ua,revoked,channel,device_id,client_name,shop_id,auth_at,auth_method) VALUES (?,'staff',?,?,?,?,?,?,?,0,'cli',?,?,?,?,?)",params![digest(&access),text(&staff,"id"),expires,now(),sid,now(),ctx.peer,ua,text(&d,"id"),text(&d,"client_name"),text(&staff,"shop_id"),now(),"device_key"])?;
        ctx.conn.execute(
            "UPDATE cli_devices SET last_seen=? WHERE id=?",
            params![now(), text(&d, "id")],
        )?;
        audit(
            &ctx.conn,
            text(&staff, "id"),
            "session.create",
            &sid,
            staff["shop_id"].as_str(),
        )?;
        let mut value = json!({"access_token":access,"token_type":"Bearer","expires":expires,"expires_in":(expires-now()).ceil().max(0.0) as i64,"device_id":d["id"],"session_id":sid,"client_name":d["client_name"],"agent_type":d["agent_type"],"product_id":staff["product_id"],"shop_id":staff["shop_id"],"permissions":staff["permissions"]});
        scope_metadata(&staff, &mut value);
        return Ok(Some(Reply::json(value)));
    }
    if ctx.is("GET", "/api/cli/status") {
        let s = cli_session(ctx, "staff")?;
        let p = authorize_product(ctx, &s, text(&s, "product_id"))?;
        let mut value = json!({"role":"staff","channel":"cli","origin":ctx.app.origin,"product_name":parse(text(&p,"config"))?["name"],"link_id":s["staff_id"],"link_name":s["name"],"session_id":s["id"]});
        for k in [
            "product_id",
            "shop_id",
            "permissions",
            "link_expires",
            "device_id",
            "client_name",
            "agent_type",
            "expires",
            "max_uses",
            "uses",
            "remaining_uses",
            "max_cli_uses",
            "cli_uses",
            "remaining_cli_uses",
        ] {
            value[k] = s[k].clone();
        }
        scope_metadata(&s, &mut value);
        return Ok(Some(Reply::json(value)));
    }
    if ctx.is("DELETE", "/api/cli/session") {
        let s = cli_session(ctx, "staff")?;
        revoke_session(ctx, text(&s, "digest"), &actor(&s), "session.logout")?;
        return Ok(Some(Reply::json(
            json!({"ok":true,"id":s["id"],"current":true}),
        )));
    }
    if ctx.is("POST", "/api/manage/cli-ticket") {
        fields(ctx, &["staff_id"])?;
        let s = session(ctx, &["admin", "staff"], None)?;
        if text(&s, "channel") != "browser" {
            return Err(ApiError::new(403, "请在浏览器登录后创建 CLI 授权口令"));
        }
        throttle(ctx, "cli-ticket", 20, 60.0, None)?;
        let lid = if text(&s, "role") == "staff" {
            if !ctx.b("staff_id").is_empty() && ctx.b("staff_id") != text(&s, "staff_id") {
                return Err(ApiError::new(
                    403,
                    "不能为其它商品管理链接创建 CLI 授权口令",
                ));
            }
            text(&s, "staff_id")
        } else {
            input(ctx, "staff_id", 1, 100)?
        };
        let staff = staff_authorization(ctx, lid)?;
        authorize_product(ctx, &s, text(&staff, "product_id"))?;
        if integer(&staff, "cli_uses", 0) >= integer(&staff, "max_cli_uses", 0) {
            return Err(ApiError::new(
                409,
                "此商品管理链接已达到 CLI 设备授权次数上限",
            ));
        }
        let value = format!("cli1_{}", token());
        let expires = (now() + 300.0).min(number(&staff, "expires"));
        ctx.conn.execute(
            "DELETE FROM cli_bind_tickets WHERE expires<?",
            [now() - 86400.0],
        )?;
        ctx.conn.execute(
            "INSERT INTO cli_bind_tickets(digest,staff_id,expires,created) VALUES (?,?,?,?)",
            params![digest(&value), text(&staff, "id"), expires, now()],
        )?;
        audit(
            &ctx.conn,
            &actor(&s),
            "cli.ticket.create",
            text(&staff, "id"),
            staff["shop_id"].as_str(),
        )?;
        return Ok(Some(Reply::json(
            json!({"token":value,"origin":ctx.app.origin,"expires":expires,"expires_in":(expires-now()).ceil().max(0.0) as i64,"staff_id":staff["id"],"product_id":staff["product_id"],"shop_id":staff["shop_id"]}),
        )));
    }
    let prefix = if path.starts_with("/api/admin/cli-devices") {
        Some("/api/admin/cli-devices")
    } else if path.starts_with("/api/manage/cli-devices") {
        Some("/api/manage/cli-devices")
    } else {
        None
    };
    if let Some(prefix) = prefix {
        let s = session(
            ctx,
            if prefix.starts_with("/api/admin") {
                &["admin"]
            } else {
                &["staff"]
            },
            None,
        )?;
        if ctx.method.as_str() == "GET" && path == prefix {
            let mut result = Vec::new();
            for d in rows(
                &ctx.conn,
                "SELECT d.*,st.name link_name,st.product_id,p.config,p.shop_id product_shop_id FROM cli_devices d JOIN staff st ON st.id=d.staff_id JOIN products p ON p.id=st.product_id ORDER BY d.created DESC",
                [],
            )? {
                if !link_visible(ctx, &s, text(&d, "staff_id"))? {
                    continue;
                }
                let mut value = json!({"id":d["id"],"link_id":d["staff_id"],"link_name":d["link_name"],"product_id":d["product_id"],"product_name":parse(text(&d,"config"))?["name"],"client_name":d["client_name"],"agent_type":d["agent_type"],"fingerprint":d["fingerprint"],"created":d["created"],"last_seen":d["last_seen"],"revoked":flag(&d,"revoked"),"active":device(ctx,text(&d,"id")).is_ok()});
                if let Some(mapped) = row(
                    &ctx.conn,
                    "SELECT a.id,a.revision,a.kind,a.shop_id FROM pipeline_bindings b JOIN pipeline_authorizations a ON a.id=b.authorization_id WHERE b.device_id=?",
                    [text(&d, "id")],
                )? {
                    value["authorization_id"] = mapped["id"].clone();
                    value["authorization_revision"] = mapped["revision"].clone();
                    value["scope"] = mapped["kind"].clone();
                    if mapped["shop_id"] != d["product_shop_id"] {
                        value["product_name"] = Value::Null;
                    }
                }
                result.push(value);
            }
            return Ok(Some(Reply::json(json!(result))));
        }
        if ctx.method.as_str() == "DELETE" {
            let did = path
                .strip_prefix(&format!("{prefix}/"))
                .filter(|v| !v.is_empty() && !v.contains('/'))
                .ok_or_else(|| ApiError::new(404, "CLI 设备不存在"))?;
            let d = required(
                &ctx.conn,
                "SELECT * FROM cli_devices WHERE id=?",
                [did],
                "CLI 设备不存在",
            )?;
            let mapping = row(
                &ctx.conn,
                "SELECT a.id,a.shop_id FROM pipeline_bindings b JOIN pipeline_authorizations a ON a.id=b.authorization_id WHERE b.device_id=?",
                [did],
            )?;
            if let Some(mapping) = mapping {
                if (text(&s, "role") == "admin"
                    && !s["shop_id"].is_null()
                    && s["shop_id"] != mapping["shop_id"])
                    || (text(&s, "role") == "staff"
                        && !link_visible(ctx, &s, text(&d, "staff_id"))?)
                {
                    return Err(ApiError::new(404, "CLI 设备不存在"));
                }
                ctx.conn.execute(
                    "UPDATE pipeline_authorizations SET revoked=1 WHERE id=?",
                    [text(&mapping, "id")],
                )?;
                let mut count = 0;
                for item in rows(
                    &ctx.conn,
                    "SELECT device_id FROM pipeline_bindings WHERE authorization_id=?",
                    [text(&mapping, "id")],
                )? {
                    count += revoke_staff_device(
                        ctx,
                        text(&item, "device_id"),
                        &actor(&s),
                        mapping["shop_id"].as_str(),
                    )?;
                }
                return Ok(Some(Reply::json(
                    json!({"ok":true,"id":did,"authorization_id":mapping["id"],"revoked_sessions":count,"released_jobs":0}),
                )));
            }
            if !link_visible(ctx, &s, text(&d, "staff_id"))? {
                return Err(ApiError::new(404, "CLI 设备不存在"));
            }
            let count = revoke_staff_device(ctx, did, &actor(&s), s["shop_id"].as_str())?;
            return Ok(Some(Reply::json(
                json!({"ok":true,"id":did,"revoked_sessions":count}),
            )));
        }
    }
    Ok(None)
}

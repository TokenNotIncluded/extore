use super::*;
use rand::Rng;

fn owner_error() -> ApiError {
    ApiError::new(401, "商家设备授权已失效")
}
fn authority(ctx: &Context, sid: Option<&str>) -> Result<Value> {
    let name = if let Some(sid) = sid {
        shop(ctx, sid)?["name"].clone()
    } else {
        json!("平台管理")
    };
    Ok(
        json!({"role":"admin","scope":"shop.owner","superadmin":sid.is_none(),"shop_id":sid,"shop_name":name}),
    )
}
fn approval_valid(ctx: &Context, sid: Option<&str>, cid: &str) -> Result<()> {
    if let Some(sid) = sid {
        let own = shop(ctx, sid)?;
        if cid == format!("password:{sid}") || cid == format!("password-totp:{sid}") {
            if text(&own, "password_hash").is_empty() {
                return Err(owner_error());
            }
            return Ok(());
        }
    }
    if row(
        &ctx.conn,
        "SELECT 1 FROM credentials WHERE id=? AND shop_id IS ?",
        params![cid, sid],
    )?
    .is_none()
    {
        return Err(ApiError::new(401, "商家 CLI 设备批准身份已失效"));
    }
    Ok(())
}
pub(super) fn owner_device(ctx: &Context, did: &str) -> Result<Value> {
    let d = row(
        &ctx.conn,
        "SELECT * FROM owner_cli_devices WHERE id=? AND revoked=0 AND expires>?",
        params![did, now()],
    )?
    .ok_or_else(owner_error)?;
    approval_valid(
        ctx,
        d["shop_id"].as_str(),
        text(&d, "approved_credential_id"),
    )?;
    Ok(d)
}
fn snapshot(value: &Value) -> Result<String> {
    let mut object = json!({});
    for k in [
        "id",
        "public_key",
        "client_name",
        "scope",
        "grant_expires",
        "fingerprint",
        "nonce",
        "device_code",
        "challenge",
        "expires",
        "shop_id",
        "target_email",
        "scope_bound",
    ] {
        object[k] = value[k].clone();
    }
    ascii_json(&object)
}
fn request(ctx: &Context, rid: &str) -> Result<Value> {
    row(
        &ctx.conn,
        "SELECT * FROM owner_cli_requests WHERE id=?",
        [rid],
    )?
    .ok_or_else(|| ApiError::new(401, "商家设备授权请求无效"))
}
fn browser_actor(ctx: &Context) -> Result<Option<Value>> {
    if ctx.headers.contains_key("authorization") {
        return Err(ApiError::new(401, "请使用新的浏览器认证批准设备"));
    }
    if ctx.cookie("extore_session").is_empty() {
        Ok(None)
    } else {
        Ok(Some(session(ctx, &["admin"], None)?))
    }
}
fn pending_request(ctx: &Context) -> Result<Value> {
    let r = request(ctx, input(ctx, "request_id", 1, 100)?)?;
    let code = input(ctx, "device_code", 1, 64)?
        .trim()
        .to_ascii_uppercase()
        .replace('-', "");
    if code.len() != 12
        || !code
            .bytes()
            .all(|b| b.is_ascii_uppercase() || (b'2'..=b'9').contains(&b))
        || !bool::from(
            text(&r, "device_code")
                .replace('-', "")
                .as_bytes()
                .ct_eq(code.as_bytes()),
        )
    {
        return Err(ApiError::new(401, "设备码不匹配"));
    }
    if number(&r, "expires") <= now() || text(&r, "state") != "pending" {
        return Err(ApiError::new(409, "商家设备授权请求已过期或处理"));
    }
    Ok(r)
}
fn bind_shop(ctx: &Context, r: &Value, sid: Option<&str>) -> Result<Value> {
    let own = sid.map(|id| shop(ctx, id)).transpose()?;
    if flag(r, "scope_bound") && r["shop_id"].as_str() != sid {
        return Err(ApiError::new(403, "此设备请求已绑定其他管理范围"));
    }
    if !text(r, "target_email").is_empty()
        && own.as_ref().is_none_or(|s| s["email"] != r["target_email"])
    {
        return Err(ApiError::new(401, "设备请求与店铺账号不匹配"));
    }
    if !flag(r, "scope_bound") {
        ctx.conn.execute(
            "UPDATE owner_cli_requests SET shop_id=?,scope_bound=1 WHERE id=?",
            params![sid, text(r, "id")],
        )?;
    }
    request(ctx, text(r, "id"))
}
fn approval_view(ctx: &Context, r: &Value) -> Result<Value> {
    let mut result = authority(ctx, r["shop_id"].as_str())?;
    result["request_id"] = r["id"].clone();
    for k in [
        "device_code",
        "client_name",
        "fingerprint",
        "grant_expires",
        "expires",
    ] {
        result[k] = r[k].clone();
    }
    Ok(result)
}
fn request_view(ctx: &Context, r: &Value) -> Value {
    json!({"request_id":r["id"],"device_code":r["device_code"],"approval_url":format!("{}/cli/owner#{}",ctx.app.origin,text(r,"id")),"challenge":r["challenge"],"expires":r["expires"],"expires_in":(number(r,"expires")-now()+0.999).max(0.0) as i64,"interval":5,"fingerprint":r["fingerprint"]})
}
fn same_actor(actor: Option<&Value>, sid: Option<&str>) -> Result<()> {
    if actor.is_some_and(|s| !s["shop_id"].is_null() && s["shop_id"].as_str() != sid) {
        return Err(ApiError::new(403, "此批准请求属于其他店铺"));
    }
    Ok(())
}
fn approved(ctx: &Context, r: &Value, cid: &str) -> Result<()> {
    ctx.conn.execute("UPDATE owner_cli_requests SET state='approved',approved_credential_id=?,approved_snapshot=? WHERE id=?",params![cid,snapshot(r)?,text(r,"id")])?;
    ctx.conn.execute(
        "DELETE FROM owner_cli_password_challenges WHERE request_id=?",
        [text(r, "id")],
    )?;
    ctx.conn.execute(
        "DELETE FROM owner_cli_approval_challenges WHERE request_id=?",
        [text(r, "id")],
    )?;
    let sid = r["shop_id"].as_str();
    let who = sid.map(|s| format!("shop:{s}")).unwrap_or("owner".into());
    audit(&ctx.conn, &who, "cli.owner.approve", text(r, "id"), sid)?;
    Ok(())
}
pub(super) fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    let path = ctx.path.clone();
    if ctx.is("POST", "/api/cli/owner/request") {
        handshake(ctx)?;
        fields(
            ctx,
            &[
                "public_key",
                "signature",
                "client_name",
                "nonce",
                "target_email",
            ],
        )?;
        throttle(ctx, "owner-cli-request", 10, 60.0, None)?;
        let name = input(ctx, "client_name", 1, 100)?;
        if name != name.trim() || name.chars().any(hidden) {
            return Err(ApiError::bad("设备名称无效"));
        }
        let nonce = input(ctx, "nonce", 43, 43)?;
        decode(nonce, 32)?;
        let key = input(ctx, "public_key", 43, 43)?;
        let email = ctx.body["target_email"]
            .as_str()
            .map(account::normalize_email)
            .transpose()?;
        let proof = if let Some(email) = &email {
            format!(
                "extore-cli-owner-request-v2\n{}\n{key}\n{name}\n{nonce}\n{email}",
                ctx.app.origin
            )
        } else {
            format!(
                "extore-cli-owner-request-v1\n{}\n{key}\n{name}\n{nonce}",
                ctx.app.origin
            )
        };
        let raw = verify(key, input(ctx, "signature", 86, 86)?, &proof)?;
        ctx.conn.execute(
            "DELETE FROM owner_cli_requests WHERE expires<?",
            [now() - 90.0 * 86400.0],
        )?;
        if let Some(r) = row(
            &ctx.conn,
            "SELECT * FROM owner_cli_requests WHERE public_key=? AND nonce=?",
            params![key, nonce],
        )? {
            if text(&r, "client_name") != name || r["target_email"].as_str() != email.as_deref() {
                return Err(ApiError::new(401, "商家设备授权请求不匹配"));
            }
            return Ok(Some(Reply::json(request_view(ctx, &r))));
        }
        const ALPHABET: &[u8] = b"ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
        let mut rng = rand::rngs::OsRng;
        let raw_code = (0..12)
            .map(|_| ALPHABET[rng.gen_range(0..ALPHABET.len())] as char)
            .collect::<String>();
        let code = raw_code
            .as_bytes()
            .chunks(4)
            .map(|s| std::str::from_utf8(s).unwrap())
            .collect::<Vec<_>>()
            .join("-");
        let rid = id();
        ctx.conn.execute("INSERT INTO owner_cli_requests(id,public_key,client_name,fingerprint,nonce,device_code,challenge,scope,expires,grant_expires,created,target_email) VALUES (?,?,?,?,?,?,?,'shop.owner',?,?,?,?)",params![rid,key,name,fingerprint(&raw),nonce,code,token(),now()+600.0,now()+30.0*86400.0,now(),email])?;
        audit(&ctx.conn, "pending", "cli.owner.request", &rid, None)?;
        return Ok(Some(Reply::json(request_view(ctx, &request(ctx, &rid)?))));
    }
    if ctx.is("POST", "/api/cli/owner/status") {
        handshake(ctx)?;
        fields(ctx, &["public_key", "signature", "request_id"])?;
        throttle(ctx, "owner-cli-poll", 120, 60.0, None)?;
        let rid = input(ctx, "request_id", 1, 100)?;
        let key = input(ctx, "public_key", 43, 43)?;
        verify(
            key,
            input(ctx, "signature", 86, 86)?,
            &format!(
                "extore-cli-owner-status-v1\n{}\n{rid}\n{key}",
                ctx.app.origin
            ),
        )?;
        let r = request(ctx, rid)?;
        if !bool::from(text(&r, "public_key").as_bytes().ct_eq(key.as_bytes())) {
            return Err(owner_error());
        }
        let state = if number(&r, "expires") <= now() {
            "expired"
        } else {
            text(&r, "state")
        };
        let mut result =
            json!({"status":if state=="claimed"{"approved"}else{state},"expires":r["expires"]});
        if ["approved", "claimed"].contains(&state) {
            if !flag(&r, "scope_bound") || text(&r, "approved_snapshot") != snapshot(&r)? {
                return Err(owner_error());
            }
            approval_valid(
                ctx,
                r["shop_id"].as_str(),
                text(&r, "approved_credential_id"),
            )?;
            for (k, v) in authority(ctx, r["shop_id"].as_str())?.as_object().unwrap() {
                result[k] = v.clone();
            }
        }
        return Ok(Some(Reply::json(result)));
    }
    if ctx.is("POST", "/api/auth/cli-owner/options") {
        fields(ctx, &["request_id", "device_code", "shop_id"])?;
        let actor = browser_actor(ctx)?;
        throttle(ctx, "owner-cli-approval", 20, 60.0, None)?;
        let own = actor.as_ref().and_then(|s| s["shop_id"].as_str());
        let selected = if ctx.body.get("shop_id").is_some() {
            ctx.body["shop_id"].as_str()
        } else {
            own
        };
        same_actor(actor.as_ref(), selected)?;
        let r = bind_shop(ctx, &pending_request(ctx)?, selected)?;
        let keys = rows(
            &ctx.conn,
            "SELECT id FROM credentials WHERE shop_id IS ?",
            [selected],
        )?;
        let own = selected.map(|sid| shop(ctx, sid)).transpose()?;
        let mut methods = Vec::new();
        if !keys.is_empty() {
            methods.push("passkey");
        }
        let has_totp = own
            .as_ref()
            .is_some_and(|s| !text(s, "totp_secret").is_empty());
        if own
            .as_ref()
            .is_some_and(|s| !text(s, "password_hash").is_empty())
        {
            methods.push(if has_totp {
                "password_totp"
            } else {
                "password"
            });
        }
        if methods.is_empty() {
            return Err(ApiError::new(409, "请先注册商家 Passkey"));
        }
        let mut result = approval_view(ctx, &r)?;
        result["options"] = Value::Null;
        result["approval_methods"] = json!(methods);
        result["mfa_required"] = json!(has_totp);
        ctx.conn.execute(
            "DELETE FROM owner_cli_approval_challenges WHERE expires<=? OR digest=?",
            params![now(), digest(ctx.cookie("extore_owner_approval"))],
        )?;
        if keys.is_empty() {
            ctx.set_cookie("extore_owner_approval=; Path=/api/auth/cli-owner; Max-Age=0; HttpOnly; SameSite=Strict")?;
        } else {
            let (options, state) = passkeys::authentication_options(ctx, selected)?;
            let raw = token();
            let expires = (now() + 300.0).min(number(&r, "expires"));
            ctx.conn.execute(
                "INSERT INTO owner_cli_approval_challenges VALUES (?,?,?,?,?,?)",
                params![
                    digest(&raw),
                    text(&r, "id"),
                    state,
                    snapshot(&r)?,
                    expires,
                    now()
                ],
            )?;
            ctx.set_cookie(&format!("extore_owner_approval={raw}; Path=/api/auth/cli-owner; HttpOnly; SameSite=Strict; Max-Age=300{}",if ctx.app.origin.starts_with("https:"){"; Secure"}else{""}))?;
            result["options"] = options;
        }
        return Ok(Some(Reply::json(result)));
    }
    if ctx.is("POST", "/api/auth/cli-owner/verify") {
        fields(ctx, &["request_id", "credential"])?;
        let actor = browser_actor(ctx)?;
        throttle(ctx, "owner-cli-approval-verify", 20, 60.0, None)?;
        let r = request(ctx, input(ctx, "request_id", 1, 100)?)?;
        same_actor(actor.as_ref(), r["shop_id"].as_str())?;
        authority(ctx, r["shop_id"].as_str())?;
        let ch = row(
            &ctx.conn,
            "SELECT * FROM owner_cli_approval_challenges WHERE digest=? AND expires>?",
            params![digest(ctx.cookie("extore_owner_approval")), now()],
        )?
        .ok_or_else(owner_error)?;
        if ch["request_id"] != r["id"]
            || text(&ch, "snapshot") != snapshot(&r)?
            || text(&r, "state") != "pending"
            || number(&r, "expires") <= now()
            || !flag(&r, "scope_bound")
        {
            return Err(owner_error());
        }
        ctx.conn.execute(
            "DELETE FROM owner_cli_approval_challenges WHERE digest=?",
            [text(&ch, "digest")],
        )?;
        ctx.conn.execute_batch("COMMIT; BEGIN IMMEDIATE")?;
        let state = URL_SAFE_NO_PAD
            .decode(text(&ch, "challenge"))
            .map_err(|_| owner_error())?;
        let cid = passkeys::verify_authentication(
            ctx,
            &ctx.body["credential"],
            &state,
            r["shop_id"].as_str(),
        )?;
        approved(ctx, &r, &cid)?;
        ctx.set_cookie("extore_owner_approval=; Path=/api/auth/cli-owner; Max-Age=0; HttpOnly; SameSite=Strict")?;
        return Ok(Some(Reply::json(json!({"ok":true,"status":"approved"}))));
    }
    if ctx.is("POST", "/api/auth/cli-owner/password-options") {
        fields(
            ctx,
            &[
                "request_id",
                "device_code",
                "shop_id",
                "email",
                "password",
                "code",
                "backup_code",
            ],
        )?;
        let actor = browser_actor(ctx)?;
        let email = account::normalize_email(input(ctx, "email", 3, 254)?)?;
        input(ctx, "password", 1, 200)?;
        throttle(ctx, "owner-cli-password", 5, 300.0, Some(&email))?;
        let r = pending_request(ctx)?;
        let own =
            row(&ctx.conn, "SELECT * FROM shops WHERE email=?", [&email])?.unwrap_or(Value::Null);
        same_actor(actor.as_ref(), own["id"].as_str())?;
        if !ctx.b("shop_id").is_empty() && ctx.b("shop_id") != text(&own, "id") {
            return Err(owner_error());
        }
        account::verify_shop_password(
            ctx,
            &own,
            ctx.b("password"),
            ctx.b("code"),
            ctx.b("backup_code"),
        )?;
        let r = bind_shop(ctx, &r, own["id"].as_str())?;
        let raw = token();
        let expires = (now() + 300.0).min(number(&r, "expires"));
        ctx.conn.execute(
            "DELETE FROM owner_cli_password_challenges WHERE expires<=? OR request_id=?",
            params![now(), text(&r, "id")],
        )?;
        ctx.conn.execute(
            "INSERT INTO owner_cli_password_challenges VALUES (?,?,?,?,?,?)",
            params![
                digest(&raw),
                text(&r, "id"),
                text(&own, "id"),
                snapshot(&r)?,
                expires,
                now()
            ],
        )?;
        let mut result = approval_view(ctx, &r)?;
        let mfa = !text(&own, "totp_secret").is_empty();
        result["options"] = Value::Null;
        result["approval_methods"] = json!([if mfa { "password_totp" } else { "password" }]);
        result["mfa_required"] = json!(mfa);
        result["approval_token"] = json!(raw);
        result["approval_expires"] = json!(expires);
        return Ok(Some(Reply::json(result)));
    }
    if ctx.is("POST", "/api/auth/cli-owner/password-approve") {
        fields(
            ctx,
            &["request_id", "device_code", "shop_id", "approval_token"],
        )?;
        let actor = browser_actor(ctx)?;
        throttle(ctx, "owner-cli-password-approve", 20, 60.0, None)?;
        let r = pending_request(ctx)?;
        let proof = row(
            &ctx.conn,
            "SELECT * FROM owner_cli_password_challenges WHERE digest=? AND expires>?",
            params![digest(input(ctx, "approval_token", 43, 43)?), now()],
        )?
        .ok_or_else(owner_error)?;
        if proof["request_id"] != r["id"]
            || proof["shop_id"] != r["shop_id"]
            || text(&proof, "snapshot") != snapshot(&r)?
            || r["shop_id"].is_null()
            || !flag(&r, "scope_bound")
        {
            return Err(ApiError::new(401, "新的密码批准验证已失效"));
        }
        same_actor(actor.as_ref(), r["shop_id"].as_str())?;
        let marker = format!("password:{}", text(&r, "shop_id"));
        approval_valid(ctx, r["shop_id"].as_str(), &marker)?;
        approved(ctx, &r, &marker)?;
        let mut result = authority(ctx, r["shop_id"].as_str())?;
        result["ok"] = json!(true);
        result["status"] = json!("approved");
        return Ok(Some(Reply::json(result)));
    }
    if ctx.is("POST", "/api/cli/owner/claim") {
        handshake(ctx)?;
        fields(ctx, &["public_key", "signature", "request_id"])?;
        throttle(ctx, "owner-cli-claim", 30, 60.0, None)?;
        let r = request(ctx, input(ctx, "request_id", 1, 100)?)?;
        let key = input(ctx, "public_key", 43, 43)?;
        if text(&r, "public_key") != key
            || number(&r, "expires") <= now()
            || !["approved", "claimed"].contains(&text(&r, "state"))
            || text(&r, "approved_snapshot") != snapshot(&r)?
            || !flag(&r, "scope_bound")
        {
            return Err(ApiError::new(401, "商家设备授权尚未批准或已失效"));
        }
        verify(
            key,
            input(ctx, "signature", 86, 86)?,
            &format!(
                "extore-cli-owner-claim-v1\n{}\n{}\n{}\n{key}",
                ctx.app.origin,
                text(&r, "id"),
                text(&r, "challenge")
            ),
        )?;
        approval_valid(
            ctx,
            r["shop_id"].as_str(),
            text(&r, "approved_credential_id"),
        )?;
        let existing = row(
            &ctx.conn,
            "SELECT * FROM owner_cli_devices WHERE public_key=?",
            [key],
        )?;
        let already = text(&r, "state") == "claimed";
        if existing
            .as_ref()
            .is_some_and(|d| d["shop_id"] != r["shop_id"])
        {
            return Err(ApiError::new(
                403,
                "此设备密钥已绑定其他管理范围，请使用新密钥",
            ));
        }
        if existing.as_ref().is_some_and(|d| flag(d, "revoked")) {
            return Err(ApiError::new(401, "此商家设备已撤销，请使用新的设备密钥"));
        }
        let did = if let Some(d) = existing {
            if already && d["id"] != r["device_id"] {
                return Err(owner_error());
            }
            if !already {
                ctx.conn.execute(
                    "UPDATE owner_cli_devices SET expires=?,approved_credential_id=? WHERE id=?",
                    params![
                        number(&r, "grant_expires"),
                        text(&r, "approved_credential_id"),
                        text(&d, "id")
                    ],
                )?;
            }
            text(&d, "id").to_owned()
        } else {
            if already {
                return Err(owner_error());
            }
            let did = id();
            ctx.conn.execute("INSERT INTO owner_cli_devices(id,public_key,client_name,fingerprint,expires,revoked,approved_credential_id,created,last_seen,shop_id) VALUES (?,?,?,?,?,0,?,?,?,?)",params![did,text(&r,"public_key"),text(&r,"client_name"),text(&r,"fingerprint"),number(&r,"grant_expires"),text(&r,"approved_credential_id"),now(),now(),r["shop_id"].as_str()])?;
            audit(
                &ctx.conn,
                &r["shop_id"]
                    .as_str()
                    .map(|s| format!("shop:{s}"))
                    .unwrap_or("owner".into()),
                "cli.owner.device.create",
                &did,
                r["shop_id"].as_str(),
            )?;
            did
        };
        ctx.conn.execute(
            "UPDATE owner_cli_requests SET state='claimed',device_id=? WHERE id=?",
            params![did, text(&r, "id")],
        )?;
        let d = owner_device(ctx, &did)?;
        let mut result = authority(ctx, d["shop_id"].as_str())?;
        result["device_id"] = d["id"].clone();
        result["already_authorized"] = json!(already);
        for k in ["client_name", "fingerprint", "expires"] {
            result[k] = d[k].clone();
        }
        return Ok(Some(Reply::json(result)));
    }
    if ctx.is("POST", "/api/cli/owner/challenge") {
        handshake(ctx)?;
        fields(ctx, &["device_id"])?;
        throttle(ctx, "owner-cli-challenge", 60, 60.0, None)?;
        let d = owner_device(ctx, input(ctx, "device_id", 1, 100)?)?;
        ctx.conn
            .execute("DELETE FROM owner_cli_challenges WHERE expires<=?", [now()])?;
        let pending = required(
            &ctx.conn,
            "SELECT count(*) n FROM owner_cli_challenges WHERE device_id=?",
            [text(&d, "id")],
            "商家设备不存在",
        )?;
        if integer(&pending, "n", 0) >= 20 {
            return Err(ApiError::new(429, "设备请求过于频繁"));
        }
        let cid = id();
        let raw = token();
        let expires = (now() + 300.0).min(number(&d, "expires"));
        ctx.conn.execute(
            "INSERT INTO owner_cli_challenges VALUES (?,?,?,?,?)",
            params![cid, text(&d, "id"), raw, expires, now()],
        )?;
        return Ok(Some(Reply::json(
            json!({"challenge_id":cid,"challenge":raw,"expires":expires,"expires_in":(expires-now()).ceil().max(0.0) as i64}),
        )));
    }
    if ctx.is("POST", "/api/cli/owner/session") {
        handshake(ctx)?;
        fields(ctx, &["device_id", "challenge_id", "signature"])?;
        throttle(ctx, "owner-cli-session", 60, 60.0, None)?;
        let d = owner_device(ctx, input(ctx, "device_id", 1, 100)?)?;
        let ch = row(
            &ctx.conn,
            "SELECT * FROM owner_cli_challenges WHERE id=? AND device_id=? AND expires>?",
            params![input(ctx, "challenge_id", 1, 100)?, text(&d, "id"), now()],
        )?
        .ok_or_else(owner_error)?;
        verify(
            text(&d, "public_key"),
            input(ctx, "signature", 86, 86)?,
            &format!(
                "extore-cli-owner-session-v1\n{}\n{}\n{}\n{}",
                ctx.app.origin,
                text(&d, "id"),
                text(&ch, "id"),
                text(&ch, "challenge")
            ),
        )?;
        let sid = id();
        let raw = token();
        let expires = (now() + 28800.0).min(number(&d, "expires"));
        ctx.conn.execute(
            "DELETE FROM owner_cli_challenges WHERE id=?",
            [text(&ch, "id")],
        )?;
        let ua = ctx
            .header("user-agent")
            .chars()
            .filter(|c| !c.is_control())
            .take(300)
            .collect::<String>();
        ctx.conn.execute("INSERT INTO sessions(digest,role,expires,created,id,last_seen,ip,ua,revoked,channel,owner_device_id,client_name,shop_id,auth_at,auth_method) VALUES (?,'admin',?,?,?,?,?,?,0,'cli',?,?,?,?,?)",params![digest(&raw),expires,now(),sid,now(),ctx.peer,ua,text(&d,"id"),text(&d,"client_name"),d["shop_id"].as_str(),now(),"device_key"])?;
        ctx.conn.execute(
            "UPDATE owner_cli_devices SET last_seen=? WHERE id=?",
            params![now(), text(&d, "id")],
        )?;
        audit(
            &ctx.conn,
            &d["shop_id"]
                .as_str()
                .map(|s| format!("shop:{s}"))
                .unwrap_or("owner".into()),
            "session.create",
            &sid,
            d["shop_id"].as_str(),
        )?;
        let mut result = authority(ctx, d["shop_id"].as_str())?;
        for(k,v)in json!({"access_token":raw,"token_type":"Bearer","expires":expires,"expires_in":(expires-now()).ceil().max(0.0) as i64,"device_id":d["id"],"session_id":sid}).as_object().unwrap(){result[k]=v.clone();}
        return Ok(Some(Reply::json(result)));
    }
    if ctx.is("GET", "/api/cli/owner/status") {
        let s = cli_session(ctx, "admin")?;
        let mut result = authority(ctx, s["shop_id"].as_str())?;
        for k in ["client_name", "fingerprint", "expires", "grant_expires"] {
            result[k] = s[k].clone();
        }
        result["channel"] = json!("cli");
        result["origin"] = json!(ctx.app.origin);
        result["device_id"] = s["owner_device_id"].clone();
        result["session_id"] = s["id"].clone();
        return Ok(Some(Reply::json(result)));
    }
    if ctx.is("DELETE", "/api/cli/owner/session") {
        let s = cli_session(ctx, "admin")?;
        revoke_session(ctx, text(&s, "digest"), &actor(&s), "session.logout")?;
        return Ok(Some(Reply::json(
            json!({"ok":true,"id":s["id"],"current":true}),
        )));
    }
    if ctx.is("POST", "/api/cli/owner/action-challenge") {
        fields(ctx, &["method", "path", "body_sha256"])?;
        let s = cli_session(ctx, "admin")?;
        throttle(ctx, "owner-cli-action", 120, 60.0, None)?;
        let method = input(ctx, "method", 3, 6)?;
        let target = input(ctx, "path", 1, 2048)?;
        let hash = input(ctx, "body_sha256", 64, 64)?;
        if !["POST", "PUT", "PATCH", "DELETE"].contains(&method)
            || !target.starts_with("/api/")
            || target.contains('#')
            || target.chars().any(hidden)
            || !hash
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(ApiError::bad("操作路径无效"));
        }
        ctx.conn.execute(
            "DELETE FROM owner_cli_action_challenges WHERE expires<=?",
            [now()],
        )?;
        let cid = id();
        let raw = token();
        let expires = (now() + 300.0)
            .min(number(&s, "expires"))
            .min(number(&s, "grant_expires"));
        ctx.conn.execute(
            "INSERT INTO owner_cli_action_challenges VALUES (?,?,?,?,?,?,?,?,?)",
            params![
                cid,
                text(&s, "owner_device_id"),
                text(&s, "id"),
                raw,
                method,
                target,
                hash,
                expires,
                now()
            ],
        )?;
        return Ok(Some(Reply::json(
            json!({"challenge_id":cid,"challenge":raw,"expires":expires,"expires_in":(expires-now()).ceil().max(0.0) as i64}),
        )));
    }
    if ctx.is("GET", "/api/admin/cli-owner-devices") {
        let s = session(ctx, &["admin"], None)?;
        let mut result = Vec::new();
        for d in rows(
            &ctx.conn,
            "SELECT d.*,shops.name shop_name,shops.enabled shop_enabled FROM owner_cli_devices d LEFT JOIN shops ON shops.id=d.shop_id WHERE (? IS NULL OR d.shop_id=?) ORDER BY d.created DESC",
            params![s["shop_id"].as_str(), s["shop_id"].as_str()],
        )? {
            let mut v = json!({"shop_id":d["shop_id"],"shop_name":if d["shop_name"].is_null(){json!("平台管理")}else{d["shop_name"].clone()},"superadmin":d["shop_id"].is_null(),"role":"admin","scope":"shop.owner","revoked":flag(&d,"revoked"),"active":owner_device(ctx,text(&d,"id")).is_ok()});
            for k in [
                "id",
                "client_name",
                "fingerprint",
                "created",
                "last_seen",
                "expires",
            ] {
                v[k] = d[k].clone();
            }
            result.push(v);
        }
        return Ok(Some(Reply::json(json!(result))));
    }
    if ctx.method.as_str() == "DELETE" && path.starts_with("/api/admin/cli-owner-devices/") {
        let s = session(ctx, &["admin"], None)?;
        let did = path.trim_start_matches("/api/admin/cli-owner-devices/");
        let d = required(
            &ctx.conn,
            "SELECT * FROM owner_cli_devices WHERE id=?",
            [did],
            "商家 CLI 设备不存在",
        )?;
        if !s["shop_id"].is_null() && s["shop_id"] != d["shop_id"] {
            return Err(ApiError::new(403, "无权撤销其他店铺的 CLI 设备"));
        }
        ctx.conn
            .execute("UPDATE owner_cli_devices SET revoked=1 WHERE id=?", [did])?;
        ctx.conn
            .execute("DELETE FROM owner_cli_challenges WHERE device_id=?", [did])?;
        ctx.conn.execute(
            "DELETE FROM owner_cli_action_challenges WHERE device_id=?",
            [did],
        )?;
        let active = rows(
            &ctx.conn,
            "SELECT digest FROM sessions WHERE owner_device_id=? AND revoked=0",
            [did],
        )?;
        for v in &active {
            revoke_session(ctx, text(v, "digest"), &actor(&s), "session.revoke")?;
        }
        audit(
            &ctx.conn,
            &actor(&s),
            "cli.owner.device.revoke",
            did,
            s["shop_id"].as_str(),
        )?;
        return Ok(Some(Reply::json(
            json!({"ok":true,"id":did,"revoked_sessions":active.len()}),
        )));
    }
    Ok(None)
}

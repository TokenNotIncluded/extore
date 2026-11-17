use super::*;
use data_encoding::BASE32_NOPAD;
use hmac::{Hmac, Mac};
use rand::RngCore;
use sha1::Sha1;
use sha2::{Digest, Sha256};
use std::sync::OnceLock;
use subtle::ConstantTimeEq;

fn login_error() -> ApiError {
    ApiError::new(401, "登录信息或 2FA 验证码错误")
}
fn invalid_input() -> ApiError {
    ApiError::bad("输入信息无效")
}
fn fields(ctx: &Context, allowed: &[&str]) -> Result<()> {
    let o = ctx.body.as_object().ok_or_else(invalid_input)?;
    if o.keys().any(|k| !allowed.contains(&k.as_str())) {
        return Err(invalid_input());
    }
    Ok(())
}
fn input_text<'a>(ctx: &'a Context, key: &str, min: usize, max: usize) -> Result<&'a str> {
    let v = ctx
        .body
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(invalid_input)?;
    let n = v.chars().count();
    if n < min || n > max {
        return Err(invalid_input());
    }
    Ok(v)
}
fn clean_text(value: &str, min: usize, max: usize) -> Result<String> {
    let v = value.trim();
    if v.chars().count() < min
        || v.chars().count() > max
        || v.chars().any(|c| c < ' ' || c == '\u{7f}')
    {
        return Err(invalid_input());
    }
    Ok(v.into())
}
fn label(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 63
        && s.as_bytes()[0].is_ascii_alphanumeric()
        && s.as_bytes()[s.len() - 1].is_ascii_alphanumeric()
        && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
}
pub fn normalize_email(value: &str) -> Result<String> {
    let value = clean_text(value, 3, 254)?;
    if !value.is_ascii() {
        return Err(invalid_input());
    }
    let Some((local, domain)) = value.split_once('@') else {
        return Err(invalid_input());
    };
    if local.is_empty()
        || local.len() > 64
        || local.starts_with('.')
        || local.ends_with('.')
        || local.contains("..")
        || !local
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b".!#$%&'*+/=?^_\x60{|}~-".contains(&b))
        || domain.len() > 253
        || !domain.contains('.')
        || !domain.split('.').all(label)
    {
        return Err(invalid_input());
    }
    Ok(value.to_ascii_lowercase())
}
fn superadmin(ctx: &Context) -> Result<Value> {
    let s = session(ctx, &["admin"], None)?;
    if !s["shop_id"].is_null() {
        return Err(ApiError::new(403, "仅平台管理员可以执行此操作"));
    }
    Ok(s)
}
fn owner(ctx: &Context) -> Result<Value> {
    let s = session(ctx, &["admin"], None)?;
    if s["shop_id"].is_null() {
        return Err(ApiError::new(403, "请登录店铺账号"));
    }
    Ok(s)
}
fn checked_password(hashed: &str, password: &str) -> bool {
    static DUMMY: OnceLock<String> = OnceLock::new();
    let dummy = DUMMY.get_or_init(|| {
        password_hash("extore-password-timing-placeholder-not-an-account")
            .expect("valid Argon2 parameters")
    });
    password_valid(if hashed.is_empty() { dummy } else { hashed }, password) && !hashed.is_empty()
}
pub fn totp_counter(secret: &str, code: &str, timestamp: f64, last: i64) -> Option<i64> {
    if code.len() != 6
        || !code.bytes().all(|b| b.is_ascii_digit())
        || !timestamp.is_finite()
        || timestamp < 0.0
        || last < -1
        || secret.len() < 32
        || secret.len() > 104
        || !secret.is_ascii()
    {
        return None;
    }
    let upper = secret.to_ascii_uppercase();
    let raw = upper.trim_end_matches('=');
    let padded = format!("{}{}", raw, "=".repeat((8 - raw.len() % 8) % 8));
    if upper != raw && upper != padded {
        return None;
    }
    let key = BASE32_NOPAD.decode(raw.as_bytes()).ok()?;
    if !(20..=64).contains(&key.len()) || BASE32_NOPAD.encode(&key) != raw {
        return None;
    }
    let current = (timestamp / 30.0).floor();
    if current >= i64::MAX as f64 {
        return None;
    }
    let current = current as i64;
    let mut matched = None;
    for n in [current - 1, current, current + 1] {
        if n < 0 {
            continue;
        }
        let mut mac = <Hmac<Sha1> as Mac>::new_from_slice(&key).ok()?;
        mac.update(&(n as u64).to_be_bytes());
        let data = mac.finalize().into_bytes();
        let offset = (data[19] & 15) as usize;
        let value = (u32::from_be_bytes(data[offset..offset + 4].try_into().ok()?) & 0x7fffffff)
            % 1_000_000;
        let expected = format!("{value:06}");
        let equal = bool::from(expected.as_bytes().ct_eq(code.as_bytes()));
        if equal && n > last {
            matched = Some(n);
        }
    }
    matched
}
pub fn backup_digest(code: &str) -> Result<String> {
    if code.len() > 96 {
        return Err(login_error());
    }
    let normalized = code.replace(['-', ' '], "").to_ascii_uppercase();
    if normalized.len() != 32 || !normalized.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(login_error());
    }
    let mut h = Sha256::new();
    h.update(b"extore-account-recovery-code-v1\0");
    h.update(normalized.as_bytes());
    Ok(format!("{:x}", h.finalize()))
}
fn backup_codes() -> Result<(Vec<String>, Vec<String>)> {
    let mut raw = Vec::new();
    let mut hashes = Vec::new();
    while raw.len() < 10 {
        let mut bytes = [0u8; 16];
        rand::rngs::OsRng.fill_bytes(&mut bytes);
        let compact = bytes.iter().map(|b| format!("{b:02X}")).collect::<String>();
        let code = compact
            .as_bytes()
            .chunks(8)
            .map(|s| std::str::from_utf8(s).unwrap())
            .collect::<Vec<_>>()
            .join("-");
        if raw.contains(&code) {
            continue;
        }
        hashes.push(backup_digest(&code)?);
        raw.push(code);
    }
    Ok((raw, hashes))
}
pub fn verify_second_factor(ctx: &Context, own: &Value, code: &str, backup: &str) -> Result<()> {
    if text(own, "totp_secret").is_empty() {
        return Ok(());
    }
    if code.is_empty() == backup.is_empty() {
        return Err(ApiError::new(401, "请输入 2FA 验证码或恢复码"));
    }
    let sid = text(own, "id");
    if !code.is_empty() {
        let secret =
            secret::open_secret(&ctx.app, Some(sid), "totp", sid, text(own, "totp_secret"))?;
        let count = totp_counter(
            secret.as_str().ok_or_else(login_error)?,
            code,
            now(),
            integer(own, "last_totp_counter", -1),
        )
        .ok_or_else(login_error)?;
        ctx.conn.execute(
            "UPDATE shops SET last_totp_counter=? WHERE id=?",
            params![count, sid],
        )?;
    } else {
        let wanted = backup_digest(backup)?;
        let mut hashes: Vec<String> =
            serde_json::from_str(text(own, "totp_backup_digests")).map_err(|_| login_error())?;
        let index = hashes
            .iter()
            .position(|h| bool::from(h.as_bytes().ct_eq(wanted.as_bytes())))
            .ok_or_else(login_error)?;
        hashes.remove(index);
        ctx.conn.execute(
            "UPDATE shops SET totp_backup_digests=? WHERE id=?",
            params![serde_json::to_string(&hashes)?, sid],
        )?;
        audit(
            &ctx.conn,
            &format!("shop:{sid}"),
            "account.backup_code.consume",
            sid,
            Some(sid),
        )?;
    }
    Ok(())
}
pub fn verify_shop_password(
    ctx: &Context,
    own: &Value,
    password: &str,
    code: &str,
    backup: &str,
) -> Result<()> {
    let valid = checked_password(text(own, "password_hash"), password);
    if password.is_empty()
        || password.chars().count() > 200
        || !flag(own, "enabled")
        || !flag(own, "verified")
        || !valid
    {
        return Err(login_error());
    }
    verify_second_factor(ctx, own, code, backup)
}
fn view(ctx: &Context, own: &Value) -> Result<Value> {
    let count = required(
        &ctx.conn,
        "SELECT count(*) n FROM credentials WHERE shop_id=?",
        [text(own, "id")],
        "店铺不存在",
    )?;
    Ok(
        json!({"id":own["id"],"name":own["name"],"email":own["email"],"enabled":flag(own,"enabled"),"verified":flag(own,"verified"),"created":own["created"],"legacy":flag(own,"legacy"),"totp_enabled":!text(own,"totp_secret").is_empty(),"storage_limit_bytes":own["storage_limit_bytes"],"passkeys":count["n"]}),
    )
}
pub fn smtp_config(ctx: &Context) -> Result<Value> {
    let value = setting(&ctx.conn, "smtp_config", "")?;
    if value.is_empty() {
        return Ok(
            json!({"enabled":false,"host":"","port":587,"mode":"starttls","sender":"","from_name":"","username":"","password":""}),
        );
    }
    let config = secret::open_secret(&ctx.app, None, "smtp-config", "global", &value)?;
    validate_smtp(config)
}
fn validate_smtp(mut v: Value) -> Result<Value> {
    const KEYS: &[&str] = &[
        "enabled",
        "host",
        "port",
        "mode",
        "sender",
        "from_name",
        "username",
        "password",
    ];
    let obj = v.as_object().ok_or_else(invalid_input)?;
    if obj.len() != KEYS.len()
        || obj.keys().any(|k| !KEYS.contains(&k.as_str()))
        || v["enabled"].as_bool().is_none()
        || v["port"].as_u64().is_none_or(|p| !(1..=65535).contains(&p))
        || !["ssl", "starttls"].contains(&text(&v, "mode"))
    {
        return Err(ApiError::bad("邮箱服务器配置无效"));
    }
    for (k, max) in [
        ("host", 253),
        ("sender", 254),
        ("from_name", 120),
        ("username", 320),
        ("password", 4096),
    ] {
        let s = v[k].as_str().ok_or_else(invalid_input)?;
        if s.chars().count() > max || s.chars().any(|c| c < ' ' || c == '\u{7f}') {
            return Err(invalid_input());
        }
    }
    let host = text(&v, "host").trim();
    if !host.is_empty() {
        let parsed = url::Host::parse(host).map_err(|_| invalid_input())?;
        let normalized = match parsed {
            url::Host::Domain(domain) => {
                if !domain.split('.').all(label) {
                    return Err(invalid_input());
                }
                domain
            }
            url::Host::Ipv4(ip) => {
                if ip.is_unspecified() || ip.is_multicast() {
                    return Err(invalid_input());
                }
                ip.to_string()
            }
            url::Host::Ipv6(ip) => {
                if ip.is_unspecified() || ip.is_multicast() {
                    return Err(invalid_input());
                }
                ip.to_string()
            }
        };
        v["host"] = json!(normalized);
    } else {
        v["host"] = json!("");
    }
    let sender = text(&v, "sender").trim();
    if !sender.is_empty() {
        v["sender"] = json!(normalize_email(sender)?);
    } else {
        v["sender"] = json!("");
    }
    if text(&v, "password") == "[redacted]"
        || text(&v, "username").is_empty() != text(&v, "password").is_empty()
        || (flag(&v, "enabled") && (text(&v, "host").is_empty() || text(&v, "sender").is_empty()))
    {
        return Err(ApiError::bad("邮箱服务器配置无效"));
    }
    Ok(v)
}
fn smtp_metadata(config: &Value) -> Value {
    json!({"configured":!text(config,"host").is_empty()&&!text(config,"sender").is_empty(),"enabled":config["enabled"],"host":config["host"],"port":config["port"],"mode":config["mode"],"sender":config["sender"],"from_name":config["from_name"],"has_username":!text(config,"username").is_empty(),"has_password":!text(config,"password").is_empty()})
}
fn configure_smtp(ctx: &Context, patch: &Value) -> Result<()> {
    let mut config = smtp_config(ctx)?;
    let fields = patch.as_object().ok_or_else(invalid_input)?;
    for (k, v) in fields {
        config[k] = v.clone();
    }
    let config = validate_smtp(config)?;
    let encrypted = secret::store_secret(&ctx.app, None, "smtp-config", "global", &config)?;
    set_setting(&ctx.conn, "smtp_config", &encrypted)?;
    audit(&ctx.conn, "superadmin", "smtp.configure", "global", None)?;
    Ok(())
}
pub fn enqueue_mail(
    ctx: &Context,
    to: &str,
    subject: &str,
    body: &str,
    html: Option<&str>,
    expires: f64,
    sid: Option<&str>,
) -> Result<String> {
    if !flag(&smtp_config(ctx)?, "enabled") {
        return Err(ApiError::new(503, "邮箱服务尚未启用，请联系管理者"));
    }
    let recipient = normalize_email(to)?;
    let subject = clean_text(subject, 1, 200)?;
    if body.trim().is_empty()
        || body.contains('\0')
        || html.is_some_and(|s| s.trim().is_empty() || s.contains('\0'))
        || body.len() + html.map(str::len).unwrap_or(0) > 65536
        || !expires.is_finite()
        || expires <= now()
        || expires > now() + 7.0 * 86400.0
    {
        return Err(invalid_input());
    }
    let count = required(
        &ctx.conn,
        "SELECT count(*) n FROM mail_outbox WHERE state IN ('pending','sending') AND expires>?",
        [now()],
        "邮件队列不可用",
    )?;
    if integer(&count, "n", 0) >= 1000 {
        return Err(ApiError::new(503, "邮件队列已满，请稍后重试"));
    }
    let mid = id();
    let mut data = json!({"to":recipient,"subject":subject,"body":body});
    if let Some(html) = html {
        data["html"] = json!(html);
    }
    let payload = secret::store_secret(&ctx.app, sid, "mail", &mid, &data)?;
    ctx.conn.execute(
        "INSERT INTO mail_outbox(id,tenant_id,payload,due,created,expires) VALUES (?,?,?,?,?,?)",
        params![mid, sid, payload, now(), now(), expires],
    )?;
    Ok(mid)
}
fn escaped(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
}
fn mail_token(
    ctx: &Context,
    kind: &str,
    email: &str,
    sid: Option<&str>,
    hash: Option<&str>,
    name: Option<&str>,
) -> Result<f64> {
    if !flag(&smtp_config(ctx)?, "enabled") {
        return Err(ApiError::new(503, "邮箱服务尚未启用，请联系管理者"));
    }
    let raw = token();
    let expires = now() + if kind == "invite" { 86400.0 } else { 1800.0 };
    ctx.conn.execute(
        "DELETE FROM account_tokens WHERE expires<? OR (kind=? AND email=? AND used IS NULL)",
        params![now(), kind, email],
    )?;
    ctx.conn.execute("INSERT INTO account_tokens(digest,kind,shop_id,email,password_hash,name,created,expires) VALUES (?,?,?,?,?,?,?,?)",params![digest(&raw),kind,sid,email,hash,name,now(),expires])?;
    let title = match kind {
        "invite" => "Extore 店铺邀请",
        "register" => "Extore 邮箱确认",
        _ => "Extore 密码重置",
    };
    let url = format!(
        "{}/account/{kind}#{raw}",
        ctx.app.origin.trim_end_matches('/')
    );
    let body = format!(
        "{title}\n\n请打开以下链接完成操作：\n{url}\n\n链接仅可使用一次，{} 小时后失效。如果不是你请求的，请忽略。",
        if kind == "invite" { "24" } else { "0.5" }
    );
    let html = format!(
        "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\"><body style=\"margin:0;background:#f8f9fc;color:#202124;font:16px/1.7 system-ui,sans-serif\"><main style=\"max-width:560px;margin:48px auto;padding:40px;background:white;border:1px solid #e8eaed;border-radius:20px\"><p style=\"font-size:13px;letter-spacing:2px;color:#5f6368\">EXTORE</p><h1 style=\"font-size:26px;font-weight:500\">{}</h1><p>请确认本次账号操作。链接仅可使用一次。</p><p style=\"margin:32px 0\"><a href=\"{}\" style=\"display:inline-block;padding:12px 24px;border-radius:24px;background:#1a73e8;color:white;text-decoration:none\">继续操作</a></p><p style=\"font-size:13px;color:#5f6368\">如果不是你请求的，请忽略此邮件。</p></main></body></html>",
        escaped(title),
        escaped(&url)
    );
    enqueue_mail(ctx, email, title, &body, Some(&html), expires, sid)?;
    Ok(expires)
}
fn consume_token(ctx: &Context, raw: &str, kind: &str) -> Result<Value> {
    if raw.len() < 20 || raw.len() > 100 {
        return Err(invalid_input());
    }
    let token = row(
        &ctx.conn,
        "SELECT * FROM account_tokens WHERE digest=? AND kind=? AND used IS NULL AND expires>?",
        params![digest(raw), kind, now()],
    )?
    .ok_or_else(|| ApiError::bad("链接已失效或已使用"))?;
    ctx.conn.execute(
        "UPDATE account_tokens SET used=? WHERE digest=? AND used IS NULL",
        params![now(), text(&token, "digest")],
    )?;
    Ok(token)
}
fn default_storage() -> i64 {
    std::env::var("EXTORE_UPLOAD_SHOP_BYTES")
        .ok()
        .and_then(|v| v.parse::<i64>().ok())
        .filter(|v| *v > 0)
        .unwrap_or(1073741824)
}
fn auth_method(own: &Value) -> &'static str {
    if text(own, "totp_secret").is_empty() {
        "email_password"
    } else {
        "email_password_totp"
    }
}
fn account_factors(ctx: &Context, own: &Value) -> Result<()> {
    input_text(ctx, "password", 1, 200)?;
    for k in ["code", "backup_code"] {
        if !ctx.body[k].is_null() {
            input_text(ctx, k, 0, 100)?;
        }
    }
    verify_shop_password(
        ctx,
        own,
        ctx.b("password"),
        ctx.b("code"),
        ctx.b("backup_code"),
    )
}
pub fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    let path = ctx.path.clone();
    let method = ctx.method.as_str();
    if path == "/api/platform/settings" && (method == "GET" || method == "PUT") {
        let s = superadmin(ctx)?;
        if method == "PUT" {
            fields(ctx, &["registration_enabled", "smtp"])?;
            recent(&s)?;
            if !ctx.body["smtp"].is_null() {
                configure_smtp(ctx, &ctx.body["smtp"])?;
            }
            if !ctx.body["registration_enabled"].is_null() {
                let enabled = ctx.body["registration_enabled"]
                    .as_bool()
                    .ok_or_else(invalid_input)?;
                if enabled && !flag(&smtp_config(ctx)?, "enabled") {
                    return Err(ApiError::new(409, "启用注册前请配置并启用邮箱服务"));
                }
                set_setting(
                    &ctx.conn,
                    "registration_enabled",
                    if enabled { "true" } else { "false" },
                )?;
                audit(
                    &ctx.conn,
                    "superadmin",
                    "platform.registration.configure",
                    if enabled { "True" } else { "False" },
                    None,
                )?;
            }
            if !flag(&smtp_config(ctx)?, "enabled") {
                set_setting(&ctx.conn, "registration_enabled", "false")?;
            }
        }
        return Ok(Some(Reply::json(
            json!({"registration_enabled":setting(&ctx.conn,"registration_enabled","false")?=="true","smtp":smtp_metadata(&smtp_config(ctx)?)}),
        )));
    }
    if path == "/api/platform/shops" && (method == "GET" || method == "POST") {
        let s = superadmin(ctx)?;
        if method == "GET" {
            let data = rows(&ctx.conn, "SELECT * FROM shops ORDER BY created DESC", [])?
                .iter()
                .map(|v| view(ctx, v))
                .collect::<Result<Vec<_>>>()?;
            return Ok(Some(Reply::json(json!(data))));
        }
        fields(ctx, &["email", "name"])?;
        recent(&s)?;
        let email = normalize_email(input_text(ctx, "email", 3, 254)?)?;
        let name = clean_text(input_text(ctx, "name", 1, 100)?, 1, 100)?;
        if row(&ctx.conn, "SELECT 1 FROM shops WHERE email=?", [&email])?.is_some() {
            return Err(ApiError::new(409, "此邮箱已有店铺账号"));
        }
        let sid = id();
        ctx.conn.execute(
            "INSERT INTO shops(id,name,email,created,storage_limit_bytes) VALUES (?,?,?,?,?)",
            params![sid, name, email, now(), default_storage()],
        )?;
        crate::catalog::ensure_shop_issuer(ctx, &sid)?;
        let expires = mail_token(ctx, "invite", &email, Some(&sid), None, None)?;
        audit(&ctx.conn, "superadmin", "shop.create", &sid, Some(&sid))?;
        return Ok(Some(Reply::json(
            json!({"ok":true,"shop":view(ctx,&shop(ctx,&sid)?)?,"invite_expires":expires}),
        )));
    }
    if let Some(rest) = path.strip_prefix("/api/platform/shops/") {
        if method == "POST" && rest.ends_with("/invite") {
            let sid = rest.trim_end_matches("/invite");
            let s = superadmin(ctx)?;
            recent(&s)?;
            let own = shop(ctx, sid)?;
            if flag(&own, "verified") || text(&own, "email").is_empty() {
                return Err(ApiError::new(409, "此店铺无需领取邀请"));
            }
            let expires = mail_token(ctx, "invite", text(&own, "email"), Some(sid), None, None)?;
            audit(&ctx.conn, "superadmin", "shop.invite", sid, Some(sid))?;
            return Ok(Some(Reply::json(
                json!({"ok":true,"invite_expires":expires}),
            )));
        }
        if method == "PATCH" && !rest.contains('/') {
            fields(ctx, &["enabled", "storage_limit_bytes"])?;
            let s = superadmin(ctx)?;
            recent(&s)?;
            required(
                &ctx.conn,
                "SELECT id FROM shops WHERE id=?",
                [rest],
                "店铺不存在",
            )?;
            if !ctx.body["enabled"].is_null() {
                let enabled = ctx.body["enabled"].as_bool().ok_or_else(invalid_input)?;
                ctx.conn.execute(
                    "UPDATE shops SET enabled=? WHERE id=?",
                    params![enabled, rest],
                )?;
                if !enabled {
                    revoke_shop_auth(ctx, Some(rest))?;
                }
                audit(
                    &ctx.conn,
                    "superadmin",
                    if enabled {
                        "shop.enable"
                    } else {
                        "shop.disable"
                    },
                    rest,
                    Some(rest),
                )?;
            }
            if !ctx.body["storage_limit_bytes"].is_null() {
                let limit = ctx.body["storage_limit_bytes"]
                    .as_i64()
                    .filter(|x| *x > 0)
                    .ok_or_else(invalid_input)?;
                crate::files::set_shop_storage_limit(ctx, &s, rest, limit as u64)?;
                audit(
                    &ctx.conn,
                    "superadmin",
                    "shop.storage_limit.configure",
                    rest,
                    Some(rest),
                )?;
            }
            let own = required(
                &ctx.conn,
                "SELECT * FROM shops WHERE id=?",
                [rest],
                "店铺不存在",
            )?;
            return Ok(Some(Reply::json(view(ctx, &own)?)));
        }
    }
    if path == "/api/shop/account" && (method == "GET" || method == "PATCH") {
        let s = owner(ctx)?;
        let sid = text(&s, "shop_id");
        if method == "PATCH" {
            fields(ctx, &["name"])?;
            let name = clean_text(input_text(ctx, "name", 1, 100)?, 1, 100)?;
            ctx.conn
                .execute("UPDATE shops SET name=? WHERE id=?", params![name, sid])?;
            audit(&ctx.conn, &actor(&s), "shop.rename", sid, Some(sid))?;
        }
        return Ok(Some(Reply::json(view(ctx, &shop(ctx, sid)?)?)));
    }
    if ctx.is("POST", "/api/auth/email/login") {
        fields(ctx, &["email", "password", "code", "backup_code"])?;
        let email = normalize_email(input_text(ctx, "email", 3, 254)?)?;
        input_text(ctx, "password", 1, 200)?;
        throttle(ctx, "email-login", 5, 300.0, Some(&email))?;
        let own =
            row(&ctx.conn, "SELECT * FROM shops WHERE email=?", [&email])?.unwrap_or(Value::Null);
        account_factors(ctx, &own)?;
        create_session(ctx, "admin", own["id"].as_str(), None, auth_method(&own))?;
        audit(
            &ctx.conn,
            &format!("shop:{}", text(&own, "id")),
            "account.login",
            text(&own, "id"),
            own["id"].as_str(),
        )?;
        return Ok(Some(Reply::json(
            json!({"role":"admin","shop_id":own["id"],"superadmin":false}),
        )));
    }
    if ctx.is("POST", "/api/auth/invite/claim") {
        fields(ctx, &["token", "password", "code", "backup_code"])?;
        input_text(ctx, "password", 12, 200)?;
        throttle(ctx, "invite-claim", 10, 300.0, None)?;
        let token = consume_token(ctx, ctx.b("token"), "invite")?;
        let own = shop(ctx, text(&token, "shop_id"))?;
        if flag(&own, "verified") || own["email"] != token["email"] {
            return Err(ApiError::bad("链接已失效或已使用"));
        }
        let sid = text(&own, "id");
        ctx.conn.execute(
            "UPDATE shops SET password_hash=?,verified=1 WHERE id=?",
            params![password_hash(ctx.b("password"))?, sid],
        )?;
        create_session(ctx, "admin", Some(sid), None, "email_password")?;
        audit(
            &ctx.conn,
            &format!("shop:{sid}"),
            "account.invite.claim",
            sid,
            Some(sid),
        )?;
        return Ok(Some(Reply::json(
            json!({"ok":true,"role":"admin","shop_id":sid}),
        )));
    }
    if ctx.is("POST", "/api/auth/register/email/request") {
        fields(ctx, &["email", "name", "password"])?;
        let email = normalize_email(input_text(ctx, "email", 3, 254)?)?;
        let name = clean_text(input_text(ctx, "name", 1, 100)?, 1, 100)?;
        input_text(ctx, "password", 12, 200)?;
        throttle(ctx, "register-email", 3, 600.0, Some(&email))?;
        if setting(&ctx.conn, "registration_enabled", "false")? != "true" {
            return Err(ApiError::new(
                403,
                "当前不允许自行注册，请联系管理者获取邀请",
            ));
        }
        if row(&ctx.conn, "SELECT 1 FROM shops WHERE email=?", [&email])?.is_none() {
            mail_token(
                ctx,
                "register",
                &email,
                None,
                Some(&password_hash(ctx.b("password"))?),
                Some(&name),
            )?;
        }
        return Ok(Some(Reply::json(
            json!({"ok":true,"message":"如果此邮箱可以注册，确认邮件将发送至该邮箱"}),
        )));
    }
    if ctx.is("POST", "/api/auth/register/email/confirm") {
        fields(ctx, &["token"])?;
        throttle(ctx, "register-confirm", 10, 300.0, None)?;
        if setting(&ctx.conn, "registration_enabled", "false")? != "true" {
            return Err(ApiError::new(403, "当前不允许自行注册"));
        }
        let token = consume_token(ctx, ctx.b("token"), "register")?;
        if row(
            &ctx.conn,
            "SELECT 1 FROM shops WHERE email=?",
            [text(&token, "email")],
        )?
        .is_some()
        {
            return Err(ApiError::bad("链接已失效或已使用"));
        }
        let sid = id();
        ctx.conn.execute("INSERT INTO shops(id,name,email,password_hash,created,verified,storage_limit_bytes) VALUES (?,?,?,?,?,1,?)",params![sid,text(&token,"name"),text(&token,"email"),text(&token,"password_hash"),now(),default_storage()])?;
        crate::catalog::ensure_shop_issuer(ctx, &sid)?;
        create_session(
            ctx,
            "admin",
            Some(&sid),
            None,
            "email_verified_registration",
        )?;
        audit(
            &ctx.conn,
            &format!("shop:{sid}"),
            "account.register",
            &sid,
            Some(&sid),
        )?;
        return Ok(Some(Reply::json(
            json!({"ok":true,"role":"admin","shop_id":sid}),
        )));
    }
    if ctx.is("POST", "/api/auth/password/reset/request") {
        fields(ctx, &["email"])?;
        let email = normalize_email(input_text(ctx, "email", 3, 254)?)?;
        throttle(ctx, "password-reset", 3, 600.0, Some(&email))?;
        if let Some(own) = row(
            &ctx.conn,
            "SELECT * FROM shops WHERE email=? AND enabled=1 AND verified=1",
            [&email],
        )? {
            if flag(&smtp_config(ctx)?, "enabled") {
                mail_token(ctx, "reset", &email, own["id"].as_str(), None, None)?;
            }
        }
        return Ok(Some(Reply::json(
            json!({"ok":true,"message":"如果此邮箱有可用账号，重置邮件将发送至该邮箱"}),
        )));
    }
    if ctx.is("POST", "/api/auth/password/reset/confirm") {
        fields(ctx, &["token", "password", "code", "backup_code"])?;
        input_text(ctx, "password", 12, 200)?;
        throttle(ctx, "password-reset-confirm", 5, 300.0, None)?;
        let token = consume_token(ctx, ctx.b("token"), "reset")?;
        let own = shop(ctx, text(&token, "shop_id"))?;
        if own["email"] != token["email"] || !flag(&own, "verified") {
            return Err(ApiError::bad("链接已失效或已使用"));
        }
        verify_second_factor(ctx, &own, ctx.b("code"), ctx.b("backup_code"))?;
        let sid = text(&own, "id");
        ctx.conn.execute(
            "UPDATE shops SET password_hash=? WHERE id=?",
            params![password_hash(ctx.b("password"))?, sid],
        )?;
        revoke_shop_auth(ctx, Some(sid))?;
        audit(
            &ctx.conn,
            &format!("shop:{sid}"),
            "account.password.reset",
            sid,
            Some(sid),
        )?;
        ctx.set_cookie("extore_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")?;
        return Ok(Some(Reply::json(
            json!({"ok":true,"message":"密码已重置，请重新登录"}),
        )));
    }
    if matches!(
        path.as_str(),
        "/api/auth/reauth/password"
            | "/api/auth/password/change"
            | "/api/auth/totp/setup"
            | "/api/auth/totp/confirm"
            | "/api/auth/totp/disable"
            | "/api/auth/totp/backup-codes"
    ) && ctx.method.as_str() == "POST"
    {
        let s = owner(ctx)?;
        let sid = text(&s, "shop_id");
        let own = shop(ctx, sid)?;
        let purpose = match path.as_str() {
            "/api/auth/reauth/password" => "account-reauth",
            "/api/auth/password/change" => "password-change",
            "/api/auth/totp/setup" => "totp-setup",
            "/api/auth/totp/confirm" => "totp-confirm",
            "/api/auth/totp/disable" => "totp-disable",
            _ => "totp-backup-rotate",
        };
        throttle(ctx, purpose, 5, 300.0, Some(sid))?;
        if path.ends_with("/confirm") {
            fields(ctx, &["code"])?;
            recent(&s)?;
            input_text(ctx, "code", 6, 6)?;
            if !text(&own, "totp_secret").is_empty()
                || text(&own, "pending_totp_secret").is_empty()
                || number(&own, "pending_totp_expires") < now()
            {
                return Err(ApiError::bad("验证设置已过期，请重新开始"));
            }
            let secret = secret::open_secret(
                &ctx.app,
                Some(sid),
                "totp-pending",
                sid,
                text(&own, "pending_totp_secret"),
            )?;
            let counter = totp_counter(
                secret.as_str().ok_or_else(login_error)?,
                ctx.b("code"),
                now(),
                integer(&own, "last_totp_counter", -1),
            )
            .ok_or_else(|| ApiError::new(401, "2FA 验证码错误"))?;
            let (raw, hashes) = backup_codes()?;
            let cipher = secret::store_secret(&ctx.app, Some(sid), "totp", sid, &secret)?;
            ctx.conn.execute("UPDATE shops SET totp_secret=?,pending_totp_secret=NULL,pending_totp_expires=NULL,last_totp_counter=?,totp_backup_digests=? WHERE id=?",params![cipher,counter,serde_json::to_string(&hashes)?,sid])?;
            cli::invalidate_pending_owner_approvals(ctx, Some(sid))?;
            audit(&ctx.conn, &actor(&s), "account.totp.enable", sid, Some(sid))?;
            return Ok(Some(Reply::json(json!({"ok":true,"backup_codes":raw}))));
        }
        fields(
            ctx,
            if path.ends_with("/change") {
                &["password", "new_password", "code", "backup_code"]
            } else {
                &["password", "code", "backup_code"]
            },
        )?;
        if path.ends_with("/setup") && !text(&own, "totp_secret").is_empty() {
            return Err(ApiError::new(409, "请先关闭现有 2FA 双因素认证"));
        }
        if matches!(
            path.as_str(),
            "/api/auth/totp/disable" | "/api/auth/totp/backup-codes"
        ) && text(&own, "totp_secret").is_empty()
        {
            return Err(login_error());
        }
        account_factors(ctx, &own)?;
        ctx.conn.execute(
            "UPDATE sessions SET auth_at=?,auth_method=? WHERE digest=?",
            params![now(), auth_method(&own), text(&s, "digest")],
        )?;
        if path == "/api/auth/reauth/password" {
            audit(
                &ctx.conn,
                &actor(&s),
                "account.reauthenticate",
                sid,
                Some(sid),
            )?;
            return Ok(Some(Reply::json(json!({"ok":true}))));
        }
        if path.ends_with("/change") {
            input_text(ctx, "new_password", 12, 200)?;
            ctx.conn.execute(
                "UPDATE shops SET password_hash=? WHERE id=?",
                params![password_hash(ctx.b("new_password"))?, sid],
            )?;
            revoke_shop_auth(ctx, Some(sid))?;
            if text(&s, "channel") != "cli" {
                create_session(ctx, "admin", Some(sid), None, auth_method(&own))?;
            }
            audit(
                &ctx.conn,
                &actor(&s),
                "account.password.change",
                sid,
                Some(sid),
            )?;
            return Ok(Some(Reply::json(json!({"ok":true}))));
        }
        if path.ends_with("/setup") {
            let mut bytes = [0u8; 20];
            rand::rngs::OsRng.fill_bytes(&mut bytes);
            let raw = BASE32_NOPAD.encode(&bytes);
            let expires = now() + 600.0;
            let cipher =
                secret::store_secret(&ctx.app, Some(sid), "totp-pending", sid, &json!(raw))?;
            ctx.conn.execute(
                "UPDATE shops SET pending_totp_secret=?,pending_totp_expires=? WHERE id=?",
                params![cipher, expires, sid],
            )?;
            let mut uri = url::Url::parse("otpauth://totp/").map_err(|_| invalid_input())?;
            uri.set_path(&format!("Extore:{}", text(&own, "email")));
            uri.query_pairs_mut()
                .append_pair("secret", &raw)
                .append_pair("issuer", "Extore")
                .append_pair("algorithm", "SHA1")
                .append_pair("digits", "6")
                .append_pair("period", "30");
            audit(&ctx.conn, &actor(&s), "account.totp.setup", sid, Some(sid))?;
            return Ok(Some(Reply::json(
                json!({"secret":raw,"uri":uri.as_str(),"expires":expires}),
            )));
        }
        cli::invalidate_pending_owner_approvals(ctx, Some(sid))?;
        if path.ends_with("/disable") {
            ctx.conn.execute("UPDATE shops SET totp_secret=NULL,pending_totp_secret=NULL,pending_totp_expires=NULL,totp_backup_digests='[]',last_totp_counter=-1 WHERE id=?",[sid])?;
            audit(
                &ctx.conn,
                &actor(&s),
                "account.totp.disable",
                sid,
                Some(sid),
            )?;
            return Ok(Some(Reply::json(json!({"ok":true}))));
        }
        let (raw, hashes) = backup_codes()?;
        ctx.conn.execute(
            "UPDATE shops SET totp_backup_digests=? WHERE id=?",
            params![serde_json::to_string(&hashes)?, sid],
        )?;
        audit(
            &ctx.conn,
            &actor(&s),
            "account.totp.backup.rotate",
            sid,
            Some(sid),
        )?;
        return Ok(Some(Reply::json(json!({"ok":true,"backup_codes":raw}))));
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rfc6238_sha1_and_replay() {
        let secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ";
        assert_eq!(totp_counter(secret, "287082", 59.0, -1), Some(1));
        assert_eq!(totp_counter(secret, "287082", 59.0, 1), None);
        assert_eq!(totp_counter(secret, "287082", 91.0, -1), None);
        assert_eq!(totp_counter(secret, "０１２３４５", 59.0, -1), None);
    }
    #[test]
    fn recovery_code_normalization() {
        assert_eq!(
            backup_digest("01234567-89ABCDEF-01234567-89ABCDEF").unwrap(),
            backup_digest("0123456789abcdef0123456789abcdef").unwrap()
        );
        assert!(backup_digest("01234567").is_err());
    }
    #[test]
    fn mailbox_rejects_header_injection() {
        assert_eq!(
            normalize_email(" Name+tag@Example.COM ").unwrap(),
            "name+tag@example.com"
        );
        for v in [
            "a@b",
            "a@b.com\nBcc:x@y.com",
            "Name <a@b.com>",
            "a..b@c.com",
            "a@-b.com",
        ] {
            assert!(normalize_email(v).is_err(), "{v}");
        }
    }
}

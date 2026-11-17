//! Tenant-owned settings: readable fields are explicit; credentials never leave encrypted revisions.
use crate::{auth, common::*};
use rusqlite::params;
use serde_json::{Value, json};
use std::collections::BTreeMap;
pub mod workflow;

fn invalid() -> ApiError {
    ApiError::new(422, "配置不符合商品处理器定义")
}
pub fn catalog() -> Value {
    serde_json::from_str(include_str!("processor-catalog.json"))
        .expect("checked-in reviewed catalog")
}
pub fn specification(pid: &str) -> Result<Value> {
    catalog()
        .as_array()
        .unwrap()
        .iter()
        .find(|p| text(p, "id") == pid)
        .cloned()
        .ok_or_else(|| ApiError::new(422, "请选择已审核的商品处理器"))
}
fn fields(pid: &str) -> Result<BTreeMap<String, Value>> {
    let mut result = BTreeMap::new();
    for f in specification(pid)?["configuration"]
        .as_array()
        .ok_or_else(invalid)?
    {
        let key = text(f, "key");
        if key.is_empty() {
            continue;
        }
        let mut f = f.clone();
        if result.contains_key(key) {
            f["secret"] = json!(true);
        }
        result.insert(key.to_owned(), f);
    }
    Ok(result)
}
fn validate_template(value: &str, allowed: &[&str]) -> Result<()> {
    let bytes = value.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] != b'$' {
            i += 1;
            continue;
        }
        i += 1;
        if i < bytes.len() && bytes[i] == b'$' {
            i += 1;
            continue;
        }
        let brace = i < bytes.len() && bytes[i] == b'{';
        if brace {
            i += 1;
        }
        let start = i;
        if i >= bytes.len() || !(bytes[i].is_ascii_alphabetic() || bytes[i] == b'_') {
            return Err(invalid());
        }
        i += 1;
        while i < bytes.len() && (bytes[i].is_ascii_alphanumeric() || bytes[i] == b'_') {
            i += 1;
        }
        if !allowed.contains(&&value[start..i]) {
            return Err(invalid());
        }
        if brace {
            if i >= bytes.len() || bytes[i] != b'}' {
                return Err(invalid());
            }
            i += 1;
        }
    }
    Ok(())
}
pub fn configuration(pid: &str, values: &Value, allow_incomplete: bool) -> Result<Value> {
    let values = values.as_object().ok_or_else(invalid)?;
    let fields = fields(pid)?;
    if values.len() > 30 || values.keys().any(|k| !fields.contains_key(k)) {
        return Err(invalid());
    }
    let mut result = json!({});
    for (key, field) in fields {
        let default = field.get("default").and_then(Value::as_str).unwrap_or("");
        let mut value = match values.get(&key) {
            Some(v) => v.as_str().ok_or_else(invalid)?,
            None => default,
        };
        if matches!(pid, "resource_link" | "personalized_text")
            && text(&field, "type") != "textarea"
        {
            value = value.trim();
        }
        if (!allow_incomplete && flag(&field, "required") && value.trim().is_empty())
            || value.chars().count() > integer(&field, "max_length", 10000) as usize
        {
            return Err(invalid());
        }
        if text(&field, "type") == "select"
            && !value.is_empty()
            && field["options"]
                .as_array()
                .is_none_or(|a| !a.iter().any(|o| text(o, "value") == value))
        {
            return Err(invalid());
        }
        result[&key] = json!(value);
    }
    if pid == "resource_link" && !text(&result, "resource_url").is_empty() {
        let value = text(&result, "resource_url");
        let u = url::Url::parse(value).map_err(|_| invalid())?;
        if u.scheme() != "https"
            || u.host_str().is_none()
            || !u.username().is_empty()
            || u.password().is_some()
            || value.contains('\\')
            || value.chars().any(|c| c.is_whitespace() || c.is_control())
        {
            return Err(invalid());
        }
    }
    if pid == "personalized_text" {
        validate_template(text(&result, "template"), &["name"])?;
    }
    if pid == "document_template" {
        validate_template(text(&result, "template"), &["title", "name", "body"])?;
    }
    Ok(result)
}
fn normalized_fields(value: &Value) -> Result<Value> {
    let mut result = value.as_array().ok_or_else(invalid)?.clone();
    for field in &mut result {
        let f = field.as_object_mut().ok_or_else(invalid)?;
        for (k, v) in [
            ("description", json!({})),
            ("collapsed", json!(true)),
            ("required", json!(true)),
            ("type", json!("text")),
        ] {
            f.entry(k).or_insert(v);
        }
        let kind = f
            .get("type")
            .and_then(Value::as_str)
            .unwrap_or("text")
            .to_owned();
        if kind != "select" {
            f.remove("options");
        }
        if kind == "images" {
            f.entry("max_items").or_insert(json!(10));
        } else {
            f.remove("max_items");
        }
        if f.get("sensitive").is_some_and(|v| v == &json!(true)) {
            f.entry("sensitive_ttl_seconds").or_insert(json!(120));
        } else {
            f.remove("sensitive");
            f.remove("sensitive_ttl_seconds");
        }
    }
    Ok(json!(result))
}
pub fn normalize_product(p: &mut Value) -> Result<()> {
    if text(p, "mode") != "script" {
        return Ok(());
    }
    let pid = text(p, "processor_id").to_owned();
    let spec = specification(&pid)?;
    for name in ["parameters", "outputs"] {
        if let Some(declared) = p.get(name) {
            if declared.as_array().is_none_or(|a| !a.is_empty())
                && normalized_fields(declared)? != normalized_fields(&spec[name])?
            {
                return Err(ApiError::new(
                    422,
                    "顾客填写项和输出字段由商品处理器代码定义",
                ));
            }
        }
        p[name] = spec[name].clone();
    }
    let supplied = p.get("processor_config").cloned().unwrap_or(json!({}));
    let config = configuration(&pid, &supplied, true)?;
    p["processor_config"] = json!({});
    for key in supplied.as_object().ok_or_else(invalid)?.keys() {
        p["processor_config"][key] = config[key].clone();
    }
    p["script"] = json!("");
    p["delivery"] = spec["delivery"].clone();
    Ok(())
}
fn configuration_patch(pid: &str, old: &Value, patch: &Value) -> Result<Value> {
    let fields = fields(pid)?;
    let patch = patch.as_object().ok_or_else(invalid)?;
    let mut result = old.clone();
    for (key, v) in patch {
        let f = fields.get(key).ok_or_else(invalid)?;
        let value = v.as_str().ok_or_else(invalid)?;
        if f["secret"] == json!(false) || !value.trim().is_empty() {
            result[key] = v.clone();
        }
    }
    Ok(result)
}
fn profile(ctx: &Context, pid: &str, owner: Option<&Value>) -> Result<Value> {
    let p = required(
        &ctx.conn,
        "SELECT * FROM processor_profiles WHERE id=?",
        [pid],
        "商品处理器配置不存在",
    )?;
    if let Some(owner) = owner {
        if text(owner, "role") != "admin" {
            return Err(ApiError::new(403, "此操作需要店铺管理权限"));
        }
        if owner["shop_id"]
            .as_str()
            .is_some_and(|s| s != text(&p, "shop_id"))
        {
            return Err(ApiError::new(404, "商品处理器配置不存在"));
        }
    }
    auth::shop(ctx, text(&p, "shop_id"))?;
    Ok(p)
}
fn seal(
    ctx: &Context,
    profile_id: &str,
    shop_id: &str,
    processor_id: &str,
    revision: i64,
    config: &Value,
    flow: &Value,
) -> Result<(i64, String)> {
    let version = integer(&specification(processor_id)?, "schema_version", 1);
    let envelope = json!({"format":"extore.processor-profile.v2","configuration":config,"workflow":workflow::validate(flow)?});
    if envelope.to_string().len() > 200000 {
        return Err(ApiError::new(422, "工作流配置超过存储限制"));
    }
    let cipher = auth::secret::store_secret(
        &ctx.app,
        Some(shop_id),
        &format!("processor-config:{processor_id}:v{version}"),
        &format!("{profile_id}:{revision}"),
        &envelope,
    )?;
    Ok((version, cipher))
}
fn open_revision(
    ctx: &Context,
    p: &Value,
    revision: i64,
    incomplete: bool,
) -> Result<(Value, Value)> {
    if flag(p, "disabled") {
        return Err(ApiError::new(409, "商品处理器配置已撤销或不可用"));
    }
    let r = required(
        &ctx.conn,
        "SELECT * FROM processor_profile_revisions WHERE profile_id=? AND revision=?",
        params![text(p, "id"), revision],
        "商品处理器配置不可用",
    )?;
    let version = integer(&r, "schema_version", 0);
    if version
        != integer(
            &specification(text(p, "processor_id"))?,
            "schema_version",
            1,
        )
    {
        return Err(ApiError::new(409, "商品处理器配置需要重新确认"));
    }
    let payload = auth::secret::open_secret(
        &ctx.app,
        Some(text(p, "shop_id")),
        &format!("processor-config:{}:v{version}", text(p, "processor_id")),
        &format!("{}:{revision}", text(p, "id")),
        text(&r, "ciphertext"),
    )?;
    let (config, flow) = if text(&payload, "format") == "extore.processor-profile.v2" {
        if payload.as_object().is_none_or(|v| v.len() != 3) || !payload["workflow"].is_object() {
            return Err(ApiError::new(409, "商品处理器配置格式不可用"));
        }
        (
            payload["configuration"].clone(),
            workflow::validate(&payload["workflow"])?,
        )
    } else {
        (payload, workflow::defaults())
    };
    Ok((
        configuration(text(p, "processor_id"), &config, incomplete)?,
        flow,
    ))
}
fn create(
    ctx: &Context,
    shop: &str,
    pid: &str,
    name: &str,
    config: &Value,
    flow: &Value,
) -> Result<Value> {
    if name.trim().is_empty() || name.chars().count() > 120 {
        return Err(ApiError::new(422, "请输入有效配置名称"));
    }
    let key = id();
    let (version, cipher) = seal(ctx, &key, shop, pid, 1, config, flow)?;
    let now = now();
    ctx.conn.execute("INSERT INTO processor_profiles(id,shop_id,processor_id,name,revision,created,updated) VALUES (?,?,?,?,1,?,?)",params![key,shop,pid,name.trim(),now,now])?;
    ctx.conn.execute(
        "INSERT INTO processor_profile_revisions VALUES (?,?,?,?,?)",
        params![key, 1, version, cipher, now],
    )?;
    profile(ctx, &key, None)
}
fn view(ctx: &Context, p: &Value, owner: &Value, revision: Option<i64>) -> Result<Value> {
    let current = profile(ctx, text(p, "id"), Some(owner))?;
    let mut result = current.clone();
    result["configuration"] = json!({});
    result["configured_fields"] = json!([]);
    result["workflow"] = workflow::view(&workflow::defaults());
    if flag(&current, "disabled") {
        return Ok(result);
    }
    let (config, flow) = open_revision(
        ctx,
        &current,
        revision.unwrap_or(integer(&current, "revision", 1)),
        true,
    )?;
    let mut configured = vec![];
    for (key, f) in fields(text(p, "processor_id"))? {
        if let Some(value) = config.get(&key).and_then(Value::as_str) {
            if !value.trim().is_empty() {
                configured.push(key.clone());
            }
            if f["secret"] == json!(false) {
                result["configuration"][&key] = json!(value);
            }
        }
    }
    result["configured_fields"] = json!(configured);
    result["workflow"] = workflow::view(&flow);
    Ok(result)
}
fn binding(ctx: &Context, pid: &str) -> Result<Option<Value>> {
    row(
        &ctx.conn,
        "SELECT profiles.*,binding.revision AS bound_revision FROM processor_product_bindings binding JOIN processor_profiles profiles ON profiles.id=binding.profile_id WHERE product_id=?",
        [pid],
    )
}
fn binding_view(ctx: &Context, pid: &str, s: &Value) -> Result<Value> {
    let product = auth::authorize_product(ctx, s, pid)?;
    let result = if let Some(p) = binding(ctx, pid)? {
        if text(&product, "shop_id") != text(&p, "shop_id") {
            return Err(ApiError::new(409, "商品处理器配置的店铺不匹配"));
        }
        let mut v = view(ctx, &p, s, Some(integer(&p, "bound_revision", 1)))?;
        v["bound_revision"] = p["bound_revision"].clone();
        v
    } else {
        Value::Null
    };
    Ok(json!({"product_id":pid,"profile":result}))
}
pub fn decorate(ctx: &Context, p: &mut Value, s: &Value) -> Result<()> {
    p["processor_config"] = json!({});
    p["configured_fields"] = json!([]);
    if text(s, "role") == "admin" && text(p, "mode") == "script" {
        let status = binding_view(ctx, text(p, "id"), s)?;
        if !status["profile"].is_null() {
            p["processor_config"] = status["profile"]["configuration"].clone();
            p["configured_fields"] = status["profile"]["configured_fields"].clone();
        }
    }
    Ok(())
}
pub fn save_inline(ctx: &Context, pid: &str, supplied: &Value, s: &Value) -> Result<()> {
    auth::authorize_product(ctx, s, pid)?;
    if text(s, "role") != "admin" {
        if supplied.as_object().is_none_or(|v| !v.is_empty()) {
            return Err(ApiError::new(403, "管理链接不能配置店铺处理器账户"));
        }
        return Ok(());
    }
    let p = super::product(ctx, pid)?;
    if text(&p, "mode") != "script" {
        ctx.conn.execute(
            "DELETE FROM processor_product_bindings WHERE product_id=?",
            [pid],
        )?;
        return Ok(());
    }
    let processor = text(&p, "processor_id");
    let mut values = supplied.clone();
    let mut flow = workflow::defaults();
    let mut previous = None;
    if let Some(current) = binding(ctx, pid)? {
        if text(&current, "processor_id") == processor {
            if text(&current, "shop_id") != text(&p, "shop_id") {
                return Err(ApiError::new(409, "商品处理器配置的店铺不匹配"));
            }
            if supplied.as_object().is_some_and(|v| v.is_empty()) {
                return Ok(());
            }
            let (old, w) =
                open_revision(ctx, &current, integer(&current, "bound_revision", 1), true)?;
            values = configuration_patch(processor, &old, supplied)?;
            previous = Some(old);
            flow = w;
        }
    }
    let config = configuration(processor, &values, true)?;
    if previous.as_ref() == Some(&config) {
        return Ok(());
    }
    let profile = create(
        ctx,
        text(&p, "shop_id"),
        processor,
        text(&p, "name"),
        &config,
        &flow,
    )?;
    ctx.conn.execute("INSERT INTO processor_product_bindings VALUES (?,?,?) ON CONFLICT(product_id) DO UPDATE SET profile_id=excluded.profile_id,revision=excluded.revision",params![pid,text(&profile,"id"),integer(&profile,"revision",1)])?;
    Ok(())
}
pub fn issue_configuration(ctx: &Context, pid: &str) -> Result<Value> {
    let p = super::require_active(ctx, pid)?;
    let b = required(
        &ctx.conn,
        "SELECT * FROM processor_product_bindings WHERE product_id=?",
        [pid],
        "请先完成商品处理器配置",
    )?;
    let profile = profile(ctx, text(&b, "profile_id"), None)?;
    if text(&profile, "shop_id") != text(&p, "shop_id")
        || text(&profile, "processor_id") != text(&p, "processor_id")
    {
        return Err(ApiError::new(409, "商品处理器配置的店铺或处理器不匹配"));
    }
    open_revision(ctx, &profile, integer(&b, "revision", 1), false)?;
    Ok(b)
}
pub fn freeze_card_binding(ctx: &Context, card_id: &str, pid: &str, b: &Value) -> Result<()> {
    let p = super::product(ctx, pid)?;
    ctx.conn.execute(
        "INSERT INTO processor_card_bindings VALUES (?,?,?,?,?,?)",
        params![
            card_id,
            pid,
            text(&p, "shop_id"),
            text(&p, "processor_id"),
            text(b, "profile_id"),
            integer(b, "revision", 1)
        ],
    )?;
    Ok(())
}
pub fn runtime_execution(
    ctx: &Context,
    job: &Value,
    processor_id: &str,
) -> Result<(Value, Value, Value)> {
    let p = super::product(ctx, text(job, "product_id"))?;
    auth::shop(ctx, text(&p, "shop_id"))?;
    let b = required(
        &ctx.conn,
        "SELECT * FROM processor_card_bindings WHERE card_id=?",
        [text(job, "card_id")],
        "此卡密没有已确认的商品处理器配置",
    )?;
    let profile = profile(ctx, text(&b, "profile_id"), None)?;
    if text(&b, "product_id") != text(job, "product_id")
        || text(&b, "shop_id") != text(&p, "shop_id")
        || text(&profile, "shop_id") != text(&p, "shop_id")
        || text(&b, "processor_id") != processor_id
        || text(&profile, "processor_id") != processor_id
    {
        return Err(ApiError::new(409, "商品处理器配置的店铺或处理器不匹配"));
    }
    let revision = integer(&b, "revision", 1);
    let (config, flow) = open_revision(ctx, &profile, revision, false)?;
    Ok((
        config,
        json!({"shop_id":p["shop_id"],"profile_id":profile["id"],"revision":revision}),
        flow,
    ))
}
fn only_fields(v: &Value, allowed: &[&str]) -> Result<()> {
    if v.as_object()
        .is_none_or(|o| o.keys().any(|k| !allowed.contains(&k.as_str())))
    {
        Err(invalid())
    } else {
        Ok(())
    }
}
pub fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    if ctx.is("GET", "/api/admin/processors") {
        auth::session(ctx, &["admin"], None)?;
        return Ok(Some(Reply::json(catalog())));
    }
    if ctx.is("GET", "/api/manage/processors") {
        auth::session(ctx, &["admin", "staff"], Some("product.edit"))?;
        return Ok(Some(Reply::json(catalog())));
    }
    let base = "/api/admin/processor-profiles";
    if ctx.path != base && !ctx.path.starts_with(&format!("{base}/")) {
        return Ok(None);
    }
    let owner = auth::session(ctx, &["admin"], None)?;
    let suffix = ctx
        .path
        .strip_prefix(base)
        .unwrap()
        .trim_start_matches('/')
        .to_owned();
    if suffix.is_empty() {
        let shop = super::selected_shop(
            ctx,
            &owner,
            if ctx.method == "GET" {
                ctx.q("shop_id")
            } else {
                ctx.b("shop_id")
            },
        )?;
        if ctx.method == "GET" {
            let result = rows(
                &ctx.conn,
                "SELECT * FROM processor_profiles WHERE shop_id=? ORDER BY created,id",
                [&shop],
            )?
            .iter()
            .map(|p| view(ctx, p, &owner, None))
            .collect::<Result<Vec<_>>>()?;
            return Ok(Some(Reply::json(json!(result))));
        }
        if ctx.method == "POST" {
            only_fields(
                &ctx.body,
                &[
                    "processor_id",
                    "name",
                    "configuration",
                    "workflow",
                    "shop_id",
                ],
            )?;
            let config = configuration(
                ctx.b("processor_id"),
                ctx.body.get("configuration").unwrap_or(&json!({})),
                false,
            )?;
            let flow = workflow::validate(ctx.body.get("workflow").unwrap_or(&json!({})))?;
            let p = create(
                ctx,
                &shop,
                ctx.b("processor_id"),
                ctx.b("name"),
                &config,
                &flow,
            )?;
            audit(
                &ctx.conn,
                &actor(&owner),
                "processor_profile.create",
                text(&p, "id"),
                Some(&shop),
            )?;
            return Ok(Some(Reply::json(view(ctx, &p, &owner, None)?)));
        }
    } else if let Some(pid) = suffix.strip_prefix("bindings/") {
        auth::authorize_product(ctx, &owner, pid)?;
        if ctx.method == "GET" {
            return Ok(Some(Reply::json(binding_view(ctx, pid, &owner)?)));
        }
        let p = super::require_active(ctx, pid)?;
        if ctx.method == "PUT" {
            only_fields(&ctx.body, &["profile_id"])?;
            let profile = profile(ctx, ctx.b("profile_id"), Some(&owner))?;
            if text(&p, "mode") != "script"
                || flag(&profile, "disabled")
                || profile["shop_id"] != p["shop_id"]
                || profile["processor_id"] != p["processor_id"]
            {
                return Err(ApiError::new(409, "配置必须属于此店铺及商品处理器"));
            }
            let revision = integer(&profile, "revision", 1);
            open_revision(ctx, &profile, revision, false)?;
            ctx.conn.execute("INSERT INTO processor_product_bindings VALUES (?,?,?) ON CONFLICT(product_id) DO UPDATE SET profile_id=excluded.profile_id,revision=excluded.revision",params![pid,text(&profile,"id"),revision])?;
            audit(
                &ctx.conn,
                &actor(&owner),
                "processor_profile.bind",
                pid,
                Some(text(&p, "shop_id")),
            )?;
            return Ok(Some(Reply::json(binding_view(ctx, pid, &owner)?)));
        }
        if ctx.method == "DELETE" {
            ctx.conn.execute(
                "DELETE FROM processor_product_bindings WHERE product_id=?",
                [pid],
            )?;
            audit(
                &ctx.conn,
                &actor(&owner),
                "processor_profile.unbind",
                pid,
                Some(text(&p, "shop_id")),
            )?;
            return Ok(Some(Reply::json(json!({"ok":true}))));
        }
    } else if !suffix.contains('/') {
        let p = profile(ctx, &suffix, Some(&owner))?;
        if ctx.method == "GET" {
            return Ok(Some(Reply::json(view(ctx, &p, &owner, None)?)));
        }
        if ctx.method == "DELETE" {
            ctx.conn.execute(
                "UPDATE processor_profiles SET disabled=1,updated=? WHERE id=?",
                params![now(), suffix],
            )?;
            audit(
                &ctx.conn,
                &actor(&owner),
                "processor_profile.revoke",
                &suffix,
                Some(text(&p, "shop_id")),
            )?;
            return Ok(Some(Reply::json(json!({"ok":true}))));
        }
        if ctx.method == "PUT" {
            only_fields(&ctx.body, &["name", "configuration", "workflow"])?;
            if flag(&p, "disabled") {
                return Err(ApiError::new(409, "已撤销的配置不能恢复"));
            }
            if ctx.body.get("workflow").is_some_and(Value::is_null) {
                return Err(ApiError::new(422, "工作流配置不符合安全限制"));
            }
            if ctx.body.get("configuration").is_some_and(|v| !v.is_null())
                || ctx.body.get("workflow").is_some()
            {
                let (old, old_flow) = open_revision(ctx, &p, integer(&p, "revision", 1), true)?;
                let values = if let Some(v) = ctx.body.get("configuration").filter(|v| !v.is_null())
                {
                    configuration(
                        text(&p, "processor_id"),
                        &configuration_patch(text(&p, "processor_id"), &old, v)?,
                        false,
                    )?
                } else {
                    old
                };
                let flow = if let Some(v) = ctx.body.get("workflow") {
                    workflow::patch(&old_flow, v)?
                } else {
                    old_flow
                };
                let revision = integer(&p, "revision", 1) + 1;
                let (version, cipher) = seal(
                    ctx,
                    &suffix,
                    text(&p, "shop_id"),
                    text(&p, "processor_id"),
                    revision,
                    &values,
                    &flow,
                )?;
                ctx.conn.execute(
                    "INSERT INTO processor_profile_revisions VALUES (?,?,?,?,?)",
                    params![suffix, revision, version, cipher, now()],
                )?;
                ctx.conn.execute(
                    "UPDATE processor_profiles SET revision=?,updated=? WHERE id=?",
                    params![revision, now(), suffix],
                )?;
            }
            if let Some(name) = ctx.body.get("name").filter(|v| !v.is_null()) {
                let name = name.as_str().ok_or_else(invalid)?;
                if name.trim().is_empty() || name.chars().count() > 120 {
                    return Err(invalid());
                }
                ctx.conn.execute(
                    "UPDATE processor_profiles SET name=?,updated=? WHERE id=?",
                    params![name.trim(), now(), suffix],
                )?;
            }
            audit(
                &ctx.conn,
                &actor(&owner),
                "processor_profile.update",
                &suffix,
                Some(text(&p, "shop_id")),
            )?;
            return Ok(Some(Reply::json(view(ctx, &p, &owner, None)?)));
        }
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn unknown_fields_and_template_expressions_fail() {
        assert!(configuration("resource_link", &json!({"command":"sh"}), true).is_err());
        assert!(configuration("personalized_text", &json!({"template":"${HOME}"}), true).is_err());
        assert!(
            configuration(
                "resource_link",
                &json!({"resource_url":"https://a.example\\b"}),
                true
            )
            .is_err()
        );
    }
    #[test]
    fn config_drafts_still_validate_supplied_values() {
        assert!(configuration("resource_link", &json!({}), true).is_ok());
        assert!(configuration("resource_link", &json!({}), false).is_err());
        assert!(configuration("json_formatter", &json!({"indent":"17"}), true).is_err());
    }
    #[test]
    fn inline_blank_secret_preserves_but_readable_blank_clears() {
        let old = json!({"resource_url":"https://example.com/x","message":"read me"});
        let p = configuration_patch(
            "resource_link",
            &old,
            &json!({"resource_url":"","message":""}),
        )
        .unwrap();
        assert_eq!(p["resource_url"], old["resource_url"]);
        assert_eq!(p["message"], "");
    }
}

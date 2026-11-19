mod batches;
mod cards;
mod lifecycle;
mod links;
mod migration;
pub mod profiles;
mod proxy;
mod tracking;
mod validation;
pub use cards::{card_digest, card_product, card_summary, card_variant, issue_cards};
pub use proxy::{ensure_shop_issuer, public_ip, unwrap_code};

use crate::{auth, common::*};
use rand::RngCore;
use rusqlite::params;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    io::Write,
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
};

pub fn initialize(app: &App) -> Result<()> {
    std::fs::create_dir_all(&app.data)?;
    std::fs::set_permissions(&app.data, std::fs::Permissions::from_mode(0o700))?;
    let c = app.db()?;
    let version: i64 = c.query_row("PRAGMA user_version", [], |r| r.get(0))?;
    if version == 22 {
        validate_state_versions(app, &[22, 23])?;
        migration::upgrade22(&c)?;
        validate_state(app)?;
        println!("Extore schema 23 ready");
        return Ok(());
    }
    if version != 0 && version != 23 {
        return Err(ApiError::new(409, "请先将旧数据库升级到 schema 22 或 23"));
    }
    if version == 23 {
        validate_state(app)?;
    }
    c.execute_batch("BEGIN IMMEDIATE")?;
    let schema = include_str!("schema.sql")
        .replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ")
        .replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ")
        .replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ");
    c.execute_batch(&schema)?;
    let fresh = setting(&c, "legacy_shop_id", "")?.is_empty();
    if fresh {
        let shop = id();
        c.execute("INSERT INTO shops(id,name,enabled,created,verified,legacy) VALUES (?,'默认店铺',1,?,1,1)", params![shop, now()])?;
        set_setting(&c, "legacy_shop_id", &shop)?;
        set_setting(&c, "proxy_tracking_started", &now().to_string())?;
    }
    c.execute_batch("PRAGMA user_version=23; COMMIT; PRAGMA journal_mode=WAL;")?;
    for name in ["master-secrets.key", "issuance.key"] {
        if !app.data.join(name).exists() {
            if name == "master-secrets.key"
                && row(
                    &c,
                    "SELECT 1 AS found FROM secret_store_state WHERE id=1",
                    [],
                )?
                .is_some()
            {
                return Err(ApiError::new(500, "已有加密数据的主密钥缺失，请恢复原密钥"));
            }
            let mut options = std::fs::OpenOptions::new();
            options.write(true).create_new(true).mode(0o600);
            let mut file = options.open(app.data.join(name))?;
            if name == "master-secrets.key" {
                let mut key = [0_u8; 32];
                rand::rngs::OsRng.fill_bytes(&mut key);
                file.write_all(&key)?;
            } else {
                file.write_all(fernet::Fernet::generate_key().as_bytes())?;
            }
            file.sync_all()?;
        }
    }
    let key = std::fs::read(app.data.join("master-secrets.key"))?;
    if key.len() != 32 {
        return Err(ApiError::new(500, "主密钥必须为 32 字节"));
    }
    let fingerprint = format!(
        "{:x}",
        Sha256::digest([b"extore-secret-key-v1\0".as_slice(), key.as_slice()].concat())
    );
    if let Some(old) = row(
        &c,
        "SELECT fingerprint FROM secret_store_state WHERE id=1",
        [],
    )? {
        if text(&old, "fingerprint") != fingerprint {
            return Err(ApiError::new(500, "主密钥与数据库不匹配"));
        }
    } else {
        c.execute("INSERT INTO secret_store_state VALUES (1,?)", [fingerprint])?;
    }
    std::fs::set_permissions(
        app.data.join("extore.sqlite3"),
        std::fs::Permissions::from_mode(0o600),
    )?;
    if fresh {
        auth::reset_auth(app)?;
    }
    println!("Extore schema 23 ready");
    Ok(())
}

pub fn defaults() -> Value {
    json!({"name":"","description":"","workshop_slogan":"","logo":"","image":"","public":false,"support_email":"","progress_steps":[],"variants":[{"id":"default","name":"默认规格","description":"","price":null,"currency":"CNY","attributes":{},"enabled":true}],"mode":"manual","delivery":"content","view_policy":"repeat","allow_retry":true,"max_attempts":3,"parameters":[],"outputs":[{"key":"content","label":{"zh-CN":"交付内容","en":"Delivery content"},"description":{},"collapsed":true,"required":true,"type":"textarea"}],"webhook_url":"","webhook_secret":"","script":"","processor_id":"","processor_config":{},"task_flow":null,"revision_policy":null,"marketplace":marketplace_defaults()})
}
pub fn marketplace_defaults() -> Value {
    json!({"title":null,"description":null,"image_urls":null,"links":null,"contact":null,"category_id":null,"visibility":null,"delivery_strategy":"sequential","max_quantity_per_order":null,"max_quantity_per_buyer":null,"sale_limit":null,"test_mode":false,"purchase_login_required":true,"pickup_login_required":true,"pickup_code_required":false,"email_pickup_link":false,"payment_methods":["balance"]})
}
pub fn variant_marketplace_defaults() -> Value {
    json!({"price_quota":null,"template":"card-key","fixed_content":null})
}
pub fn product(ctx: &Context, pid: &str) -> Result<Value> {
    let stored = required(
        &ctx.conn,
        "SELECT id,config,shop_id FROM products WHERE id=?",
        [pid],
        "商品不存在",
    )?;
    let mut value = defaults();
    let config = parse(text(&stored, "config"))?;
    value.as_object_mut().unwrap().extend(
        config
            .as_object()
            .ok_or_else(|| ApiError::new(500, "商品配置无效"))?
            .clone(),
    );
    value["id"] = json!(pid);
    value["shop_id"] = stored["shop_id"].clone();
    if text(&value, "delivery") == "service" && config.get("outputs").is_none() {
        value["outputs"] = json!([]);
    }
    if text(&value, "mode") == "script" {
        value["processor_config"] = json!({});
    }
    if let Some(lifecycle) = row(
        &ctx.conn,
        "SELECT deleted_at FROM product_lifecycle WHERE product_id=? AND deleted_at IS NOT NULL",
        [pid],
    )? {
        value["deleted"] = json!(true);
        value["deleted_at"] = lifecycle["deleted_at"].clone();
    }
    if let Some(purge) = row(
        &ctx.conn,
        "SELECT purged_at FROM product_purges WHERE product_id=?",
        [pid],
    )? {
        value["purged"] = json!(true);
        value["purged_at"] = purge["purged_at"].clone();
    }
    Ok(value)
}
pub fn require_active(ctx: &Context, pid: &str) -> Result<Value> {
    let p = product(ctx, pid)?;
    if flag(&p, "purged") {
        return Err(ApiError::new(409, "商品已永久移出回收站，不能再次发行或修改"));
    }
    if flag(&p, "deleted") {
        return Err(ApiError::new(409, "商品已删除，请先从回收站恢复"));
    }
    let shop = required(
        &ctx.conn,
        "SELECT enabled FROM shops WHERE id=?",
        [text(&p, "shop_id")],
        "店铺不存在",
    )?;
    if !flag(&shop, "enabled") {
        return Err(ApiError::new(403, "店铺已停用"));
    }
    Ok(p)
}
pub fn public_product(p: &Value) -> Value {
    let mut result = p.clone();
    for name in [
        "webhook_url",
        "webhook_secret",
        "script",
        "processor_config",
        "shop_id",
        "profile_id",
        "processor_profile",
        "processor_profile_id",
        "processor_binding",
        "configured_fields",
        "task_flow",
        "workshop_slogan",
        "marketplace",
        "deleted",
        "deleted_at",
        "deleted_by",
        "purged",
        "purged_at",
        "purged_by",
    ] {
        result.as_object_mut().unwrap().remove(name);
    }
    if let Some(variants) = result["variants"].as_array_mut() {
        for v in variants {
            v.as_object_mut().unwrap().remove("marketplace");
        }
    }
    if !p["task_flow"].is_null() {
        result["task_flow_view"] = crate::fulfillment::preview_flow(p);
    }
    result
}
pub fn selected_shop(ctx: &Context, session: &Value, requested: &str) -> Result<String> {
    Ok(
        auth::selected_shop(ctx, session, requested)?.unwrap_or(setting(
            &ctx.conn,
            "legacy_shop_id",
            "",
        )?),
    )
}
fn field_id(s: &str, first_letter: bool) -> bool {
    !s.is_empty()
        && s.len() <= 40
        && s.bytes().enumerate().all(|(i, c)| {
            if i == 0 && first_letter {
                c.is_ascii_lowercase()
            } else {
                (i != 0 || c.is_ascii_alphanumeric())
                    && (c.is_ascii_lowercase()
                        || c.is_ascii_digit()
                        || c == b'_'
                        || (!first_letter && c == b'-'))
            }
        })
}
fn validate_fields(p: &mut Value, key: &str) -> Result<()> {
    let array = p[key]
        .as_array_mut()
        .ok_or_else(|| ApiError::bad("参数定义必须是列表"))?;
    if array.len() > 30 {
        return Err(ApiError::bad("最多 30 个参数"));
    }
    let mut names = BTreeSet::new();
    for f in array {
        if !field_id(text(f, "key"), true) || !names.insert(text(f, "key").to_owned()) {
            return Err(ApiError::bad("参数代码无效或重复"));
        }
        let labels = f["label"]
            .as_object()
            .ok_or_else(|| ApiError::bad("请输入参数显示名称"))?;
        if labels.is_empty()
            || labels.values().any(|v| {
                v.as_str()
                    .is_none_or(|s| s.trim().is_empty() || s.chars().count() > 200)
            })
        {
            return Err(ApiError::bad("参数显示名称无效"));
        }
        let object = f.as_object_mut().unwrap();
        object.entry("description").or_insert(json!({}));
        object.entry("required").or_insert(json!(true));
        object.entry("collapsed").or_insert(json!(true));
        object.entry("type").or_insert(json!("text"));
        if !matches!(
            text(f, "type"),
            "text"
                | "email"
                | "url"
                | "textarea"
                | "number"
                | "file"
                | "select"
                | "boolean"
                | "image"
                | "images"
        ) {
            return Err(ApiError::bad("参数类型无效"));
        }
        if flag(f, "sensitive") {
            return Err(ApiError::bad("敏感输入只能在流水线独立输入步骤中配置"));
        }
        if text(f, "type") == "select"
            && f["options"]
                .as_array()
                .is_none_or(|v| v.is_empty() || v.len() > 100)
        {
            return Err(ApiError::bad("请提供下拉选项"));
        }
        if text(f, "type") == "images" {
            let count = integer(f, "max_items", 10);
            if !(1..=20).contains(&count) {
                return Err(ApiError::bad("图片数量无效"));
            }
            f["max_items"] = json!(count);
        }
    }
    Ok(())
}
pub fn normalize(input: Value) -> Result<Value> {
    normalize_inner(input).map_err(|mut e| {
        if e.status == 400 {
            e.status = 422;
        }
        e
    })
}
fn normalize_inner(mut input: Value) -> Result<Value> {
    if !input.is_object() {
        return Err(ApiError::bad("商品配置必须是对象"));
    }
    profiles::normalize_product(&mut input)?;
    let explicit_outputs = input.get("outputs").is_some();
    let mut p = defaults();
    p.as_object_mut()
        .unwrap()
        .append(input.as_object_mut().unwrap());
    for (key, max) in [
        ("name", 120),
        ("description", 20000),
        ("workshop_slogan", 4000),
        ("support_email", 254),
        ("logo", 2000),
        ("image", 2000),
    ] {
        if !p[key].is_string() || text(&p, key).chars().count() > max {
            return Err(ApiError::bad(format!("{key} 字段长度无效")));
        }
    }
    if text(&p, "name").trim().is_empty() {
        return Err(ApiError::bad("请输入商品名称"));
    }
    if !matches!(text(&p, "mode"), "manual" | "webhook" | "script" | "stock")
        || !matches!(text(&p, "delivery"), "content" | "service")
        || !matches!(text(&p, "view_policy"), "once" | "repeat")
    {
        return Err(ApiError::bad("商品交付类型无效"));
    }
    if !text(&p, "script").is_empty() {
        return Err(ApiError::bad("请选择已审核的商品处理器"));
    }
    if !(1..=20).contains(&integer(&p, "max_attempts", 0)) {
        return Err(ApiError::bad("重试次数范围为 1–20"));
    }
    if text(&p, "delivery") == "service" && !explicit_outputs {
        p["outputs"] = json!([]);
    }
    validate_fields(&mut p, "parameters")?;
    validate_fields(&mut p, "outputs")?;
    if (text(&p, "delivery") == "service") != p["outputs"].as_array().unwrap().is_empty() {
        return Err(ApiError::bad("内容商品需要输出字段；服务商品只返回状态"));
    }
    for key in ["logo", "image", "webhook_url"] {
        let v = text(&p, key);
        if !v.is_empty() {
            let u = url::Url::parse(v).map_err(|_| ApiError::bad("请输入 HTTPS 地址"))?;
            if u.scheme() != "https"
                || u.host_str().is_none()
                || !u.username().is_empty()
                || u.password().is_some()
                || u.fragment().is_some()
            {
                return Err(ApiError::bad("请输入无凭据的 HTTPS 地址"));
            }
        }
    }
    if text(&p, "mode") == "webhook" && text(&p, "webhook_url").is_empty() {
        return Err(ApiError::bad("请配置 Webhook 地址"));
    }
    if !text(&p, "webhook_url").is_empty() && text(&p, "webhook_secret").len() < 32 {
        return Err(ApiError::bad("Webhook 密钥至少 32 字符"));
    }
    if text(&p, "mode") == "script" && text(&p, "processor_id").is_empty() {
        return Err(ApiError::bad("请选择商品处理器"));
    }
    if text(&p, "mode") != "script"
        && (!text(&p, "processor_id").is_empty()
            || p["processor_config"]
                .as_object()
                .is_none_or(|v| !v.is_empty()))
    {
        return Err(ApiError::bad("仅处理器模式允许处理器配置"));
    }
    if text(&p, "mode") == "stock"
        && (!p["parameters"].as_array().unwrap().is_empty()
            || text(&p, "delivery") != "content"
            || p["outputs"].as_array().unwrap().len() != 1
            || text(&p["outputs"][0], "key") != "content"
            || !matches!(text(&p["outputs"][0], "type"), "text" | "textarea"))
    {
        return Err(ApiError::bad("一卡一文本商品只允许 content 文本输出"));
    }
    let variants = p["variants"]
        .as_array_mut()
        .ok_or_else(|| ApiError::bad("商品规格必须是列表"))?;
    if variants.is_empty() || variants.len() > 100 {
        return Err(ApiError::bad("商品需有 1–100 个规格"));
    }
    let mut keys = BTreeSet::new();
    for v in variants {
        if !field_id(text(v, "id"), false)
            || !keys.insert(text(v, "id").to_owned())
            || text(v, "name").trim().is_empty()
            || text(v, "name").chars().count() > 120
        {
            return Err(ApiError::bad("规格编号或名称无效"));
        }
        let o = v.as_object_mut().ok_or_else(|| ApiError::bad("规格无效"))?;
        for (k, value) in [
            ("description", json!("")),
            ("price", Value::Null),
            ("currency", json!("CNY")),
            ("attributes", json!({})),
            ("enabled", json!(true)),
        ] {
            o.entry(k).or_insert(value);
        }
        if !v["enabled"].is_boolean() {
            return Err(ApiError::bad("规格启用状态无效"));
        }
        if !v["price"].is_null() {
            let value = text(v, "price");
            let parts = value.split('.').collect::<Vec<_>>();
            if value.len() > 100
                || parts.len() > 2
                || parts
                    .iter()
                    .any(|s| s.is_empty() || !s.bytes().all(|b| b.is_ascii_digit()))
                || parts[0].trim_start_matches('0').len() > 12
                || parts.get(1).is_some_and(|s| s.len() > 6)
            {
                return Err(ApiError::bad("参考价必须为非负十进制文本，最多六位小数"));
            }
            let whole = parts[0].trim_start_matches('0');
            let whole = if whole.is_empty() { "0" } else { whole };
            let fraction = parts.get(1).map(|v| v.trim_end_matches('0')).unwrap_or("");
            v["price"] = json!(if fraction.is_empty() {
                whole.to_owned()
            } else {
                format!("{whole}.{fraction}")
            });
        }
        let currency = text(v, "currency");
        if !(3..=5).contains(&currency.len()) || !currency.bytes().all(|b| b.is_ascii_uppercase()) {
            return Err(ApiError::bad("币种代码无效"));
        }
        validate_attributes(&v["attributes"])?;
        if let Some(m) = v.get_mut("marketplace") {
            let mut mp = variant_marketplace_defaults();
            mp.as_object_mut().unwrap().extend(
                m.as_object()
                    .ok_or_else(|| ApiError::bad("商城规格配置无效"))?
                    .clone(),
            );
            if !matches!(
                text(&mp, "template"),
                "card-key"
                    | "text"
                    | "custom-text"
                    | "fixed-content"
                    | "redemption-code"
                    | "license-key"
                    | "download-link"
                    | "account-details"
            ) {
                return Err(ApiError::bad("商城交付模板无效"));
            }
            if !mp["price_quota"].is_null()
                && mp["price_quota"]
                    .as_i64()
                    .is_none_or(|i| !(500000..=9007199254740991).contains(&i))
            {
                return Err(ApiError::bad("商城售价至少为 500000 Credits（1 美元）"));
            }
            if text(&mp, "template") == "fixed-content" {
                if text(&mp, "fixed_content").trim().is_empty()
                    || text(&mp, "fixed_content").len() > 16000
                {
                    return Err(ApiError::bad("请填写固定交付正文"));
                }
            } else if mp.get("fixed_content").is_some_and(|v| !v.is_null()) {
                return Err(ApiError::bad("仅固定正文模板允许设置私有正文"));
            }
            *m = mp;
        }
    }
    let mut mp = marketplace_defaults();
    mp.as_object_mut().unwrap().extend(
        p["marketplace"]
            .as_object()
            .ok_or_else(|| ApiError::bad("商城配置无效"))?
            .clone(),
    );
    p["marketplace"] = mp;
    if !p["revision_policy"].is_null()
        && (text(&p, "view_policy") != "repeat"
            || text(&p, "delivery") != "content"
            || text(&p, "mode") == "stock"
            || !p["task_flow"].is_null())
    {
        return Err(ApiError::bad("修改权益需要可重复领取的内容商品"));
    }
    validation::product(&mut p)?;
    if !p["task_flow"].is_null() {
        crate::fulfillment::validate_flow(&p["task_flow"], &p)?;
    }
    for k in [
        "id",
        "shop_id",
        "deleted",
        "purged",
        "deleted_at",
        "purged_at",
        "processor_profile",
        "processor_binding",
        "configured_fields",
    ] {
        p.as_object_mut().unwrap().remove(k);
    }
    Ok(p)
}
pub fn validate_attributes(v: &Value) -> Result<()> {
    let o = v
        .as_object()
        .ok_or_else(|| ApiError::bad("属性必须是对象"))?;
    if o.len() > 20
        || o.iter().any(|(k, v)| {
            k.trim().is_empty()
                || k.chars().count() > 100
                || v.is_array()
                || v.is_object()
                || v.as_str().is_some_and(|s| s.chars().count() > 1000)
                || v.as_f64().is_some_and(|n| n.abs() > 9007199254740991.0)
        })
    {
        return Err(ApiError::bad("规格属性无效"));
    }
    Ok(())
}

pub fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    if let Some(r) = lifecycle::handle(ctx)? {
        return Ok(Some(r));
    }
    if let Some(r) = proxy::handle(ctx)? {
        return Ok(Some(r));
    }
    if let Some(r) = batches::handle(ctx)? {
        return Ok(Some(r));
    }
    if let Some(r) = cards::handle(ctx)? {
        return Ok(Some(r));
    }
    if let Some(r) = links::handle(ctx)? {
        return Ok(Some(r));
    }
    if let Some(r) = profiles::handle(ctx)? {
        return Ok(Some(r));
    }
    if ctx.is("GET", "/api/products") {
        let mut result = Vec::new();
        for item in rows(
            &ctx.conn,
            "SELECT p.id FROM products p JOIN shops s ON s.id=p.shop_id WHERE s.enabled=1 AND (?='' OR p.shop_id=?) ORDER BY p.created",
            params![ctx.q("shop_id"), ctx.q("shop_id")],
        )? {
            let p = product(ctx, text(&item, "id"))?;
            if flag(&p, "public") && !flag(&p, "deleted") && !flag(&p, "purged") {
                result.push(public_product(&p));
            }
        }
        return Ok(Some(Reply::json(json!(result))));
    }
    let is_admin = ctx.path.starts_with("/api/admin/products");
    let is_managed = ctx.path == "/api/manage/product" || ctx.path == "/api/manage/products";
    if is_admin || is_managed {
        let session = auth::session(
            ctx,
            if is_admin {
                &["admin"]
            } else {
                &["admin", "staff"]
            },
            if ctx.method == axum::http::Method::GET {
                if ctx.path == "/api/manage/product" {
                    Some("product.edit")
                } else {
                    None
                }
            } else {
                Some("product.edit")
            },
        )?;
        let selected = text(&session, "product_id").to_owned();
        if !selected.is_empty()
            && !ctx.q("product_id").is_empty()
            && selected != ctx.q("product_id")
        {
            return Err(ApiError::new(403, "没有此商品权限"));
        }
        if ctx.is("GET", "/api/admin/products") || ctx.is("GET", "/api/manage/products") {
            let own = auth::selected_shop(ctx, &session, ctx.q("shop_id"))?;
            let view = ctx.q("view");
            if !["", "active", "deleted", "all", "history"].contains(&view) {
                return Err(ApiError::new(422, "商品列表范围无效"));
            }
            let mut result = Vec::new();
            for item in rows(
                &ctx.conn,
                "SELECT id FROM products WHERE (? IS NULL OR shop_id=?) AND (?='' OR id=?) ORDER BY created",
                params![own, own, selected, selected],
            )? {
                let p = product(ctx, text(&item, "id"))?;
                let deleted = flag(&p, "deleted");
                let purged = flag(&p, "purged");
                if view == "history"
                    || (!purged
                        && ((view == "deleted" && deleted)
                            || view == "all"
                            || ((view.is_empty() || view == "active") && !deleted)))
                {
                    result.push(
                        if ctx.path == "/api/manage/products"
                            || matches!(ctx.q("compact"), "true" | "1")
                        {
                            let shop_id = p["shop_id"].clone();
                            let mut summary = managed_summary(p);
                            if is_admin {
                                summary["shop_id"] = shop_id;
                            }
                            summary
                        } else {
                            owner_view(ctx, p, &session)?
                        },
                    );
                }
            }
            return Ok(Some(Reply::json(json!(result))));
        }
        if ctx.is("GET", "/api/manage/product") {
            let pid = if selected.is_empty() {
                ctx.q("product_id")
            } else {
                &selected
            };
            auth::authorize_product(ctx, &session, pid)?;
            return Ok(Some(Reply::json(owner_view(
                ctx,
                product(ctx, pid)?,
                &session,
            )?)));
        }
        if ctx.is("POST", "/api/admin/products") || ctx.is("POST", "/api/admin/products/quick") {
            let shop = selected_shop(ctx, &session, ctx.q("shop_id"))?;
            let quick = ctx.path.ends_with("/quick");
            let input = if quick {
                if ctx.b("template_id") == "existing_product" {
                    let source = auth::authorize_product(ctx, &session, ctx.b("from_product_id"))?;
                    if text(&source, "shop_id") != shop {
                        return Err(ApiError::new(403, "仅可复制本店商品"));
                    }
                    let mut p = require_active(ctx, ctx.b("from_product_id"))?;
                    p["name"] = json!(if ctx.b("name").is_empty() {
                        "未命名商品"
                    } else {
                        ctx.b("name")
                    });
                    p["public"] = json!(false);
                    p["processor_config"] = json!({});
                    if !text(&p, "webhook_secret").is_empty() {
                        p["webhook_secret"] = json!(token());
                    }
                    p
                } else {
                    json!({"name":if ctx.b("name").is_empty(){"未命名商品"}else{ctx.b("name")},"delivery":if ctx.b("template_id")=="manual_service"{"service"}else{"content"}})
                }
            } else {
                ctx.body.clone()
            };
            let mut p = normalize(input)?;
            let pid = id();
            let configuration = p["processor_config"].clone();
            p["processor_config"] = json!({});
            ctx.conn.execute(
                "INSERT INTO products(id,config,created,shop_id) VALUES (?,?,?,?)",
                params![pid, p.to_string(), now(), shop],
            )?;
            profiles::save_inline(ctx, &pid, &configuration, &session)?;
            audit(
                &ctx.conn,
                &actor(&session),
                "product.create",
                &pid,
                Some(&shop),
            )?;
            let view = owner_view(ctx, product(ctx, &pid)?, &session)?;
            return Ok(Some(Reply::json(if quick {
                json!({"product":view,"management_link":links::create(ctx,&session,&json!({"product_id":pid,"name":"AI 配置","days":7,"permissions":["product.edit","fulfillment.configure"]}))?})
            } else {
                view
            })));
        }
        let suffix = ctx
            .path
            .strip_prefix("/api/admin/products/")
            .unwrap_or("")
            .to_owned();
        let parts = suffix.split('/').collect::<Vec<_>>();
        let pid = if is_managed {
            if selected.is_empty() {
                ctx.q("product_id").to_owned()
            } else {
                selected.clone()
            }
        } else {
            parts[0].to_owned()
        };
        if !pid.is_empty() && ctx.method == axum::http::Method::PUT {
            auth::authorize_product(ctx, &session, &pid)?;
            let old = require_active(ctx, &pid)?;
            let mut incoming = ctx.body.clone();
            let can_configure = session["permissions"]
                .as_array()
                .is_some_and(|v| v.contains(&json!("fulfillment.configure")));
            if text(&session, "role") == "staff" && !can_configure {
                if let Some(variants) = incoming["variants"].as_array_mut() {
                    for variant in variants {
                        if variant.get("marketplace").is_none() {
                            if let Some(saved) = old["variants"]
                                .as_array()
                                .and_then(|vs| {
                                    vs.iter().find(|v| text(v, "id") == text(variant, "id"))
                                })
                                .and_then(|v| v.get("marketplace"))
                            {
                                variant["marketplace"] = saved.clone();
                            }
                        }
                    }
                }
            }
            if text(&incoming, "webhook_secret").is_empty() {
                incoming["webhook_secret"] = old["webhook_secret"].clone();
            }
            let mut p = normalize(incoming)?;
            let permission = session["permissions"]
                .as_array()
                .is_some_and(|v| v.contains(&json!("fulfillment.configure")));
            if text(&session, "role") == "staff" && !permission {
                for field in [
                    "mode",
                    "delivery",
                    "view_policy",
                    "webhook_url",
                    "webhook_secret",
                    "processor_id",
                    "processor_config",
                    "task_flow",
                    "revision_policy",
                    "allow_retry",
                    "max_attempts",
                ] {
                    if p[field] != old[field] {
                        return Err(ApiError::new(403, "此授权未允许配置发货"));
                    }
                }
                if p["marketplace"] != old["marketplace"] || private_skus(&p) != private_skus(&old)
                {
                    return Err(ApiError::new(403, "此授权未允许修改商城发货配置"));
                }
            }
            let issued = row(
                &ctx.conn,
                "SELECT 1 AS found FROM cards WHERE product_id=? LIMIT 1",
                [&pid],
            )?
            .is_some();
            let retained_variants = p["variants"].as_array().unwrap();
            for issued_variant in rows(
                &ctx.conn,
                "SELECT DISTINCT COALESCE(m.variant_id,'default') AS variant_id FROM cards c LEFT JOIN card_meta m ON m.card_id=c.id WHERE c.product_id=?",
                [&pid],
            )? {
                if !retained_variants
                    .iter()
                    .any(|v| text(v, "id") == text(&issued_variant, "variant_id"))
                {
                    return Err(ApiError::new(409, "已有卡密的规格不能删除，可将其停用"));
                }
            }
            if issued && old["task_flow"].is_object() && p["view_policy"] != old["view_policy"] {
                return Err(ApiError::new(
                    409,
                    "已有任务流卡密，不能修改已冻结的查看方式",
                ));
            }
            if issued && text(&old, "mode") != "manual" {
                for field in [
                    "mode",
                    "delivery",
                    "view_policy",
                    "parameters",
                    "outputs",
                    "processor_id",
                    "task_flow",
                    "webhook_secret",
                ] {
                    let changed = if matches!(field, "parameters" | "outputs") {
                        field_contract(&p[field]) != field_contract(&old[field])
                    } else {
                        p[field] != old[field]
                    };
                    if changed {
                        return Err(ApiError::new(409, "已有卡密，不能修改已冻结的交付结构"));
                    }
                }
            }
            ctx.conn.execute(
                "UPDATE jobs SET progress_plan=? WHERE product_id=? AND progress_plan IS NULL",
                params![old["progress_steps"].to_string(), pid],
            )?;
            ctx.conn.execute(
                "UPDATE jobs SET schema_snapshot=? WHERE product_id=? AND schema_snapshot IS NULL",
                params![
                    json!({"parameters":old["parameters"],"outputs":old["outputs"]}).to_string(),
                    pid
                ],
            )?;
            let config = p["processor_config"].clone();
            p["processor_config"] = json!({});
            ctx.conn.execute(
                "UPDATE products SET config=? WHERE id=?",
                params![p.to_string(), pid],
            )?;
            profiles::save_inline(ctx, &pid, &config, &session)?;
            audit(
                &ctx.conn,
                &actor(&session),
                "product.update",
                &pid,
                Some(text(&old, "shop_id")),
            )?;
            return Ok(Some(Reply::json(owner_view(
                ctx,
                product(ctx, &pid)?,
                &session,
            )?)));
        }
    }
    if ctx.is("GET", "/api/admin/product-templates") {
        auth::session(ctx, &["admin"], None)?;
        return Ok(Some(Reply::json(
            json!([{"id":"manual_content","name":"队列内容交付","description":"由人员或 AI 从队列领取任务并交付内容","mode":"manual","delivery":"content"},{"id":"manual_service","name":"队列服务办理","description":"由人员或 AI 办理服务并更新状态","mode":"manual","delivery":"service"}]),
        )));
    }
    Ok(None)
}
fn field_contract(v: &Value) -> Value {
    let mut v = v.clone();
    if let Some(fields) = v.as_array_mut() {
        for field in fields {
            if let Some(o) = field.as_object_mut() {
                for k in ["label", "description", "collapsed"] {
                    o.remove(k);
                }
            }
        }
    }
    v
}
fn managed_summary(p: Value) -> Value {
    let mut value = json!({});
    for k in [
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
        "deleted",
        "deleted_at",
        "purged",
        "purged_at",
    ] {
        if let Some(v) = p.get(k) {
            value[k] = v.clone();
        }
    }
    if let Some(variants) = value["variants"].as_array_mut() {
        for v in variants {
            v.as_object_mut().unwrap().remove("marketplace");
        }
    }
    value
}
fn owner_view(ctx: &Context, mut p: Value, s: &Value) -> Result<Value> {
    if text(s, "role") == "staff"
        && !s["permissions"]
            .as_array()
            .is_some_and(|v| v.contains(&json!("fulfillment.configure")))
    {
        p["webhook_secret"] = json!("");
        if let Some(variants) = p["variants"].as_array_mut() {
            for v in variants {
                v.as_object_mut().unwrap().remove("marketplace");
            }
        }
    }
    profiles::decorate(ctx, &mut p, s)?;
    Ok(p)
}

pub fn instructions(ctx: &Context, pid: &str) -> Result<Value> {
    let p = product(ctx, pid)?;
    let shop = required(
        &ctx.conn,
        "SELECT factory_slogan FROM shops WHERE id=? AND enabled=1",
        [text(&p, "shop_id")],
        "店铺不存在",
    )?;
    let mut v = json!({"schema":"extore.work-instructions.v1","shop_id":p["shop_id"],"product_id":pid,"factory_slogan":text(&shop,"factory_slogan"),"workshop_slogan":text(&p,"workshop_slogan")});
    let raw = v.to_string();
    let mut ascii = String::new();
    for c in raw.chars() {
        if c.is_ascii() {
            ascii.push(c);
        } else {
            let mut units = [0u16; 2];
            for unit in c.encode_utf16(&mut units) {
                use std::fmt::Write;
                write!(ascii, "\\u{unit:04x}").unwrap();
            }
        }
    }
    v["revision"] = json!(digest(&ascii));
    Ok(v)
}

fn private_skus(p: &Value) -> std::collections::BTreeMap<String, Value> {
    p["variants"].as_array().into_iter().flatten().map(|v|(text(v,"id").to_owned(),json!({"marketplace":v.get("marketplace").cloned().unwrap_or_else(variant_marketplace_defaults),"revision_allowance":p["revision_policy"]["attribute_key"].as_str().map(|k|v["attributes"].get(k).cloned().unwrap_or(json!(0)))}))).collect()
}
pub fn validate_state(app: &App) -> Result<()> {
    validate_state_versions(app, &[23])
}

fn validate_state_versions(app: &App, versions: &[i64]) -> Result<()> {
    if !app.data.join("extore.sqlite3").is_file() {
        return Err(ApiError::new(500, "数据库不存在，请先初始化"));
    }
    let c = app.existing_db()?;
    validate_state_connection_versions(app, &c, versions)
}

#[cfg(feature = "plugin")]
pub(crate) fn validate_state_connection(app: &App, c: &rusqlite::Connection) -> Result<()> {
    validate_state_connection_versions(app, c, &[23])
}

fn validate_state_connection_versions(
    app: &App,
    c: &rusqlite::Connection,
    versions: &[i64],
) -> Result<()> {
    let version: i64 = c.query_row("PRAGMA user_version", [], |r| r.get(0))?;
    if !versions.contains(&version) {
        return Err(ApiError::new(409, "此版本需要 schema 23 数据库"));
    }
    let key = std::fs::read(app.data.join("master-secrets.key"))?;
    if key.len() != 32 {
        return Err(ApiError::new(500, "主密钥必须为 32 字节"));
    }
    let fp = format!(
        "{:x}",
        Sha256::digest([b"extore-secret-key-v1\0".as_slice(), key.as_slice()].concat())
    );
    let saved = required(
        c,
        "SELECT fingerprint FROM secret_store_state WHERE id=1",
        [],
        "数据库主密钥校验记录缺失",
    )?;
    if text(&saved, "fingerprint") != fp {
        return Err(ApiError::new(500, "主密钥与数据库不匹配"));
    }
    if app.data.join("issuance.key").exists() {
        let issuance = std::fs::read_to_string(app.data.join("issuance.key"))?;
        if fernet::Fernet::new(issuance.trim()).is_none() {
            return Err(ApiError::new(500, "发行记录密钥无效"));
        }
    } else if row(c, "SELECT 1 FROM api_requests LIMIT 1", [])?.is_some() {
        return Err(ApiError::new(500, "历史发行记录密钥缺失"));
    }
    Ok(())
}

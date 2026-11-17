use super::{product, require_active, validate_attributes};
use crate::{auth, common::*};
use rand::RngCore;
use rusqlite::{named_params, params};
use serde_json::{Value, json};

pub const INVENTORY: &str = include_str!("inventory.sql");
pub(super) const STATUSES: [&str; 11] = [
    "unused",
    "needs_input",
    "queued",
    "processing",
    "succeeded",
    "destroyed",
    "failed_retryable",
    "failed_terminal",
    "revoked",
    "rejected",
    "expired",
];
pub fn card_digest(value: &str) -> String {
    digest(&value.trim().to_uppercase().replace(['-', ' '], ""))
}
pub fn new_card() -> String {
    let mut raw = [0u8; 20];
    rand::rngs::OsRng.fill_bytes(&mut raw);
    data_encoding::BASE32_NOPAD
        .encode(&raw)
        .as_bytes()
        .chunks(8)
        .map(|v| std::str::from_utf8(v).unwrap())
        .collect::<Vec<_>>()
        .join("-")
}
pub fn card_variant(ctx: &Context, card: &Value) -> Result<Value> {
    let cid = if text(card, "card_id").is_empty() {
        text(card, "id")
    } else {
        text(card, "card_id")
    };
    if let Some(meta) = row(
        &ctx.conn,
        "SELECT variant_id,variant_snapshot FROM card_meta WHERE card_id=?",
        [cid],
    )? {
        let v = json_field(&meta, "variant_snapshot", json!({}));
        if v.get("id").is_some() {
            return Ok(v);
        }
    }
    Ok(
        json!({"id":"default","name":"默认规格","description":"","price":null,"currency":"CNY","attributes":{},"enabled":true}),
    )
}
pub fn card_product(ctx: &Context, card: &Value) -> Result<Value> {
    let mut p = product(ctx, text(card, "product_id"))?;
    p["task_flow"] = Value::Null;
    p["revision_policy"] = Value::Null;
    if let Some(value) = crate::fulfillment::flow_card_snapshot(ctx, card)? {
        if let Some(o) = value["product"].as_object() {
            p.as_object_mut().unwrap().extend(o.clone());
        }
        p["task_flow"] = value["definition"].clone();
    }
    if let Some(frozen) = row(
        &ctx.conn,
        "SELECT definition FROM text_card_payloads WHERE card_id=?",
        [text(card, "id")],
    )? {
        let v = parse(text(&frozen, "definition"))?;
        if let Some(o) = v.as_object() {
            p.as_object_mut().unwrap().extend(o.clone());
        }
    }
    if let Some(frozen) = row(
        &ctx.conn,
        "SELECT policy FROM card_entitlements WHERE card_id=?",
        [text(card, "id")],
    )? {
        p["revision_policy"] = json_field(&frozen, "policy", Value::Null);
    }
    Ok(p)
}
pub fn issue_cards(ctx: &Context, pid: &str, body: &Value) -> Result<Value> {
    let p = require_active(ctx, pid)?;
    if body.get("count").is_some_and(|v| v.as_i64().is_none())
        || body
            .get("expires")
            .filter(|v| !v.is_null())
            .is_some_and(|v| v.as_f64().is_none())
        || body.get("attributes").is_some_and(|v| !v.is_object())
        || body
            .get("routed")
            .filter(|v| !v.is_null())
            .is_some_and(|v| !v.is_boolean())
    {
        return Err(ApiError::bad("发行字段类型无效"));
    }

    let stock = body.get("text").and_then(Value::as_str);
    if (text(&p, "mode") == "stock") != stock.is_some() {
        return Err(ApiError::new(409, "一卡一文本商品请通过文本导入生成卡密"));
    }
    let mut blanks = 0usize;
    let mut duplicates = 0usize;
    let lines = stock.map(|s| {
        let s = s.strip_prefix('\u{feff}').unwrap_or(s);
        let mut seen = std::collections::BTreeSet::new();
        let mut result = Vec::new();
        for line in s.lines() {
            if line.trim().is_empty() {
                blanks += 1;
            } else if !seen.insert(line.to_owned()) {
                duplicates += 1;
            } else {
                result.push(line.to_owned());
            }
        }
        result
    });
    let count = lines
        .as_ref()
        .map(|v| v.len() as i64)
        .unwrap_or(integer(body, "count", 1));
    if !(1..=1000).contains(&count) {
        return Err(ApiError::bad("单次发行数量为 1–1000"));
    }
    if stock.is_some_and(|s| s.len() > 4 * 1024 * 1024)
        || lines
            .as_ref()
            .is_some_and(|v| v.iter().any(|s| s.len() > 100000))
    {
        return Err(ApiError::new(413, "导入文本过大"));
    }
    let expires = body.get("expires").and_then(Value::as_f64);
    if expires.is_some_and(|e| e <= now()) {
        return Err(ApiError::bad("卡密到期时间必须在未来"));
    }
    let variant_id = if text(body, "variant_id").is_empty() {
        "default"
    } else {
        text(body, "variant_id")
    };
    let variant = p["variants"]
        .as_array()
        .and_then(|v| v.iter().find(|v| text(v, "id") == variant_id))
        .cloned()
        .ok_or_else(|| ApiError::bad("规格不存在"))?;
    if !flag(&variant, "enabled") {
        return Err(ApiError::new(409, "规格已停用"));
    }
    let mut attrs = variant["attributes"].clone();
    if let Some(o) = body["attributes"].as_object() {
        attrs.as_object_mut().unwrap().extend(o.clone());
    }
    validate_attributes(&attrs)?;
    if let Some(key) = p["revision_policy"]["attribute_key"].as_str() {
        if attrs
            .get(key)
            .is_some_and(|v| v.as_i64().is_none_or(|i| !(0..=1000).contains(&i)))
        {
            return Err(ApiError::bad("请提供有效的修改次数属性"));
        }
    }
    let mut safe_variant = variant.clone();
    safe_variant.as_object_mut().unwrap().remove("marketplace");
    let batch = id();
    let created = now();
    let label = text(body, "label").trim();
    if label.chars().count() > 100 {
        return Err(ApiError::bad("批次名称最多 100 字"));
    }
    let mut codes = Vec::new();
    let mut items = Vec::new();
    ctx.conn.execute("INSERT INTO card_batches(id,product_id,label,created,count,variant_id) VALUES (?,?,?,?,?,?)",params![batch,pid,label,created,count,variant_id])?;
    let binding = if text(&p, "mode") == "script" {
        Some(super::profiles::issue_configuration(ctx, pid)?)
    } else {
        None
    };
    for n in 0..count {
        let cid = id();
        let code = new_card();
        ctx.conn.execute(
            "INSERT INTO cards(id,digest,product_id,created) VALUES (?,?,?,?)",
            params![cid, card_digest(&code), pid, created],
        )?;
        let normalized = code.replace('-', "");
        let suffix = &normalized[normalized.len() - 6..];
        ctx.conn.execute("INSERT INTO card_meta(card_id,batch_id,code_suffix,expires,variant_id,variant_snapshot) VALUES (?,?,?,?,?,?)",params![cid,batch,suffix,expires,variant_id,safe_variant.to_string()])?;
        ctx.conn.execute(
            "INSERT INTO card_entitlements(card_id,attributes,policy,created) VALUES (?,?,?,?)",
            params![
                cid,
                attrs.to_string(),
                if p["revision_policy"].is_null() {
                    None
                } else {
                    Some(p["revision_policy"].to_string())
                },
                created
            ],
        )?;
        if let Some(b) = &binding {
            super::profiles::freeze_card_binding(ctx, &cid, pid, b)?;
        }
        if !p["task_flow"].is_null() {
            crate::fulfillment::freeze_flow_card(ctx, &cid, &p)?;
        }
        if let Some(lines) = &lines {
            let line = &lines[n as usize];
            let cipher = auth::secret::store_secret(
                &ctx.app,
                Some(text(&p, "shop_id")),
                "text-card",
                &cid,
                &json!(line),
            )?;
            let definition=json!({"mode":"stock","delivery":p["delivery"],"view_policy":p["view_policy"],"parameters":p["parameters"],"outputs":p["outputs"],"allow_retry":p["allow_retry"],"max_attempts":p["max_attempts"],"progress_steps":p["progress_steps"]}).to_string();
            crate::files::check_storage_quota(
                ctx,
                (line.len() * 2 + definition.len() + 128) as u64,
                pid,
            )?;
            ctx.conn.execute("INSERT INTO text_card_payloads(card_id,product_id,shop_id,ciphertext,definition,created,size) VALUES (?,?,?,?,?,?,?)",params![cid,pid,text(&p,"shop_id"),cipher,definition,created,(line.len()*2+definition.len()+128) as i64])?;
            items.push(json!({"code":code,"content":line}));
        }
        codes.push(code);
    }
    let codes =
        super::proxy::wrap_codes(ctx, text(&p, "shop_id"), codes, body["routed"].as_bool())?;
    for (item, code) in items.iter_mut().zip(&codes) {
        item["code"] = json!(code);
    }
    let mut result = json!({"codes":codes,"batch_id":batch});
    if let Some(lines) = &lines {
        result["items"] = json!(items);
        result["stats"] = json!({"created":lines.len(),"lines":stock.unwrap().lines().count(),"blank":blanks,"duplicates":duplicates});
    }
    Ok(result)
}
pub fn card_summary(ctx: &Context, pid: &str, variant: &str) -> Result<Value> {
    summary_for(ctx, pid, variant, "")
}
pub(super) fn empty_summary() -> Value {
    let mut result = json!({"total":0,"remaining":0,"available":0,"used":0,"verified":0,"viewed":0,"in_progress":0,"completed":0,"failed":0,"rejected":0,"states":{}});
    for s in STATUSES {
        result["states"][s] = json!(0);
    }
    result
}
pub(super) fn summary_for(ctx: &Context, pid: &str, variant: &str, shop: &str) -> Result<Value> {
    let sql = format!(
        "{INVENTORY} SELECT status,COUNT(*) AS n,SUM(job_id IS NOT NULL AND (job_state!='needs_input' OR EXISTS(SELECT 1 FROM job_delivery_versions v WHERE v.job_id=inventory.job_id))) AS used,SUM(status IN ('unused','needs_input') AND NOT EXISTS(SELECT 1 FROM job_delivery_versions v WHERE v.job_id=inventory.job_id)) AS remaining,SUM(status IN ('unused','needs_input','failed_retryable') AND NOT EXISTS(SELECT 1 FROM job_delivery_versions v WHERE v.job_id=inventory.job_id)) AS available,SUM(first_verified IS NOT NULL) AS verified,SUM(revealed!=0 OR EXISTS(SELECT 1 FROM job_delivery_versions v WHERE v.job_id=inventory.job_id AND v.revealed!=0)) AS viewed FROM inventory WHERE (:variant_id='' OR variant_id=:variant_id) GROUP BY status"
    );
    let mut result = json!({"total":0,"remaining":0,"available":0,"used":0,"verified":0,"viewed":0,"in_progress":0,"completed":0,"failed":0,"rejected":0,"states":{}});
    for s in STATUSES {
        result["states"][s] = json!(0);
    }
    for item in rows(
        &ctx.conn,
        &sql,
        named_params! {":product_id":pid,":shop_id":shop,":now":now(),":variant_id":variant},
    )? {
        result["states"][text(&item, "status")] = item["n"].clone();
        result["total"] = json!(integer(&result, "total", 0) + integer(&item, "n", 0));
        for k in ["remaining", "available", "used", "verified", "viewed"] {
            result[k] = json!(integer(&result, k, 0) + integer(&item, k, 0));
        }
    }
    for (k, a, b) in [
        ("in_progress", "queued", "processing"),
        ("completed", "succeeded", "destroyed"),
        ("failed", "failed_retryable", "failed_terminal"),
    ] {
        result[k] = json!(integer(&result["states"], a, 0) + integer(&result["states"], b, 0));
    }
    result["rejected"] = result["states"]["rejected"].clone();
    Ok(result)
}
pub(super) fn scope(ctx: &Context) -> Result<(Value, String)> {
    let admin = ctx.path.starts_with("/api/admin/");
    let s = auth::session(
        ctx,
        if admin {
            &["admin"]
        } else {
            &["admin", "staff"]
        },
        Some("cards.manage"),
    )?;
    let requested = if !ctx.b("product_id").is_empty() {
        ctx.b("product_id")
    } else {
        ctx.q("product_id")
    };
    let pid = if text(&s, "role") == "staff" {
        if !requested.is_empty() && requested != text(&s, "product_id") {
            return Err(ApiError::new(403, "没有此商品权限"));
        }
        text(&s, "product_id")
    } else {
        requested
    }
    .to_owned();
    if !pid.is_empty() {
        auth::authorize_product(ctx, &s, &pid)?;
    }
    Ok((s, pid))
}
pub fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    if !(ctx.path.starts_with("/api/admin/card") || ctx.path.starts_with("/api/manage/card")) {
        return Ok(None);
    }
    let (s, pid) = scope(ctx)?;
    if ctx.path.ends_with("/cards") && ctx.method == axum::http::Method::POST
        || ctx.path.ends_with("/cards/import-text") && ctx.method == axum::http::Method::POST
    {
        if ctx.b("product_id").is_empty() {
            return Err(ApiError::new(422, "请指定商品编号"));
        }
        let result = issue_cards(ctx, &pid, &ctx.body)?;
        audit(
            &ctx.conn,
            &actor(&s),
            "cards.issue",
            &pid,
            Some(text(&product(ctx, &pid)?, "shop_id")),
        )?;
        return Ok(Some(Reply::json(result)));
    }
    if ctx.path.ends_with("/cards") && ctx.method == axum::http::Method::GET {
        let own = auth::selected_shop(ctx, &s, ctx.q("shop_id"))?;
        return Ok(Some(Reply::json(json!(rows(
            &ctx.conn,
            "SELECT c.id,c.product_id,c.state,c.created FROM cards c JOIN products p ON p.id=c.product_id WHERE (?='' OR c.product_id=?) AND (? IS NULL OR p.shop_id=?) ORDER BY c.created DESC LIMIT 500",
            params![pid, pid, own, own]
        )?))));
    }
    if ctx.path.ends_with("/card-stats") && ctx.method.as_str() == "GET" {
        let own = auth::selected_shop(ctx, &s, ctx.q("shop_id"))?.unwrap_or_default();
        return Ok(Some(Reply::json(super::tracking::stats(ctx, &pid, &own)?)));
    }
    if ctx.path.ends_with("/card-inventory") {
        let own = auth::selected_shop(ctx, &s, ctx.q("shop_id"))?.unwrap_or_default();
        let offset = ctx.q("offset").parse::<i64>().unwrap_or(0).max(0);
        let limit = ctx.q("limit").parse::<i64>().unwrap_or(50).clamp(1, 200);
        let sql = format!(
            "{INVENTORY} SELECT * FROM inventory WHERE (:status='' OR status=:status) AND (:variant='' OR variant_id=:variant) AND (:batch='' OR batch_id=:batch OR (:batch='legacy' AND batch_id IS NULL AND id NOT IN(SELECT card_id FROM card_meta WHERE batch_deleted_at IS NOT NULL))) AND (:search='' OR instr(upper(id),upper(:search))>0 OR instr(upper(COALESCE(code_suffix,'')),upper(:search))>0) ORDER BY created DESC,id LIMIT :limit OFFSET :offset"
        );
        let items = rows(
            &ctx.conn,
            &sql,
            named_params! {":product_id":pid,":shop_id":own,":now":now(),":status":ctx.q("status"),":variant":ctx.q("variant_id"),":batch":ctx.q("batch_id"),":search":ctx.q("search"),":limit":limit,":offset":offset},
        )?;
        let total_sql = format!(
            "{INVENTORY} SELECT COUNT(*) AS total FROM inventory WHERE (:status='' OR status=:status) AND (:variant='' OR variant_id=:variant) AND (:batch='' OR batch_id=:batch OR (:batch='legacy' AND batch_id IS NULL AND id NOT IN(SELECT card_id FROM card_meta WHERE batch_deleted_at IS NOT NULL))) AND (:search='' OR instr(upper(id),upper(:search))>0 OR instr(upper(COALESCE(code_suffix,'')),upper(:search))>0)"
        );
        let total = integer(
            &required(
                &ctx.conn,
                &total_sql,
                named_params! {":product_id":pid,":shop_id":own,":now":now(),":status":ctx.q("status"),":variant":ctx.q("variant_id"),":batch":ctx.q("batch_id"),":search":ctx.q("search")},
                "统计不存在",
            )?,
            "total",
            0,
        );
        return Ok(Some(Reply::json(
            json!({"items":items,"total":total,"summary":summary_for(ctx,&pid,"",&own)?,"offset":offset,"limit":limit}),
        )));
    }
    if let Some(cid) = ctx
        .path
        .split("/cards/")
        .nth(1)
        .and_then(|s| s.split('/').next())
    {
        let c = required(
            &ctx.conn,
            "SELECT * FROM cards WHERE id=?",
            [cid],
            "卡密不存在",
        )?;
        auth::authorize_product(ctx, &s, text(&c, "product_id"))?;
        if !pid.is_empty() && pid != text(&c, "product_id") {
            return Err(ApiError::new(403, "没有此商品的卡密权限"));
        }
        if ctx.path.ends_with("/revoke") && ctx.method == axum::http::Method::POST {
            if text(&c, "state") != "ready" {
                return Err(ApiError::new(409, "只能撤销未兑换卡密"));
            }
            ctx.conn
                .execute("UPDATE cards SET state='revoked' WHERE id=?", [cid])?;
            ctx.conn
                .execute("DELETE FROM grants WHERE card_id=?", [cid])?;
            ctx.conn
                .execute("DELETE FROM receipt_batch_cards WHERE card_id=?", [cid])?;
            audit(
                &ctx.conn,
                &actor(&s),
                "card.revoke",
                cid,
                Some(text(&product(ctx, text(&c, "product_id"))?, "shop_id")),
            )?;
            return Ok(Some(Reply::json(json!({"ok":true}))));
        }
        if ctx.path.ends_with("/history") && ctx.method.as_str() == "GET" {
            return Ok(Some(Reply::json(super::tracking::history(
                ctx,
                cid,
                text(&c, "product_id"),
            )?)));
        }
    }
    Ok(None)
}

//! Native card redemption and fulfillment with transaction-bound ownership.
mod batch;
mod definition;
pub mod entitlements;
pub mod flow;
use crate::{auth, catalog, common::*, files};
use hmac::{Hmac, Mac};
use rusqlite::params;
use serde_json::{Value, json};
use sha2::Sha256;
use std::{collections::HashSet, sync::Arc};
use subtle::ConstantTimeEq;

pub use definition::{validate_flow, validate_revision_policy};
pub use flow::{flow_card_snapshot, freeze_flow_card, preview_flow, worker_authority, worker_once};

pub fn job(ctx: &Context, jid: &str) -> Result<Value> {
    required(
        &ctx.conn,
        "SELECT * FROM jobs WHERE id=?",
        [jid],
        "任务不存在",
    )
}
pub fn card(ctx: &Context, cid: &str) -> Result<Value> {
    required(
        &ctx.conn,
        "SELECT * FROM cards WHERE id=?",
        [cid],
        "卡密不存在",
    )
}
pub fn require_enabled(ctx: &Context, pid: &str) -> Result<Value> {
    let p = required(
        &ctx.conn,
        "SELECT p.*,s.enabled FROM products p JOIN shops s ON s.id=p.shop_id WHERE p.id=?",
        [pid],
        "商品不存在",
    )?;
    if !flag(&p, "enabled") {
        return Err(ApiError::new(403, "店铺不可用"));
    }
    Ok(p)
}
pub fn expired(ctx: &Context, cid: &str) -> Result<bool> {
    Ok(row(
        &ctx.conn,
        "SELECT 1 FROM card_meta WHERE card_id=? AND expires IS NOT NULL AND expires<=?",
        params![cid, now()],
    )?
    .is_some())
}
pub fn ensure_card(ctx: &Context, c: &Value) -> Result<()> {
    if matches!(text(c, "state"), "revoked" | "rejected") {
        return Err(ApiError::new(410, "卡密已撤销或拒绝"));
    }
    if expired(ctx, text(c, "id"))? {
        return Err(ApiError::new(410, "卡密已过期"));
    }
    Ok(())
}
pub fn grant(ctx: &Context, value: &str, card_id: Option<&str>) -> Result<Value> {
    if value.is_empty() {
        return Err(ApiError::new(404, "兑换凭证无效或已过期，请重新输入卡密"));
    }
    if let Some(c) = row(
        &ctx.conn,
        "SELECT c.* FROM grants g JOIN cards c ON c.id=g.card_id WHERE g.digest=? AND g.expires>?",
        params![digest(value), now()],
    )? {
        if card_id.is_some_and(|id| !id.is_empty() && id != text(&c, "id")) {
            return Err(ApiError::new(404, "这张卡密不在此领取链接中"));
        }
        return Ok(c);
    }
    let list = batch_cards(ctx, value)?;
    if list.is_empty() {
        return Err(ApiError::new(404, "兑换凭证无效或已过期，请重新输入卡密"));
    }
    let cid = card_id
        .filter(|x| !x.is_empty())
        .ok_or_else(|| ApiError::bad("请选择一张卡密"))?;
    list.into_iter()
        .find(|c| text(c, "id") == cid)
        .ok_or_else(|| ApiError::new(404, "这张卡密不在此领取链接中"))
}
pub fn batch_cards(ctx: &Context, value: &str) -> Result<Vec<Value>> {
    rows(
        &ctx.conn,
        "SELECT c.* FROM receipt_batches b JOIN receipt_batch_cards bc ON bc.digest=b.digest JOIN cards c ON c.id=bc.card_id WHERE b.digest=? AND b.expires>? ORDER BY bc.position",
        params![digest(value), now()],
    )
}
pub fn merge(value: &mut Value, extra: Value) {
    if let (Some(a), Some(b)) = (value.as_object_mut(), extra.as_object()) {
        a.extend(b.clone());
    }
}
pub fn job_product(ctx: &Context, j: &Value) -> Result<Value> {
    let c = card(ctx, text(j, "card_id"))?;
    let mut p = catalog::card_product(ctx, &c)?;
    let saved = row(
        &ctx.conn,
        "SELECT schema_snapshot FROM jobs WHERE id=?",
        [text(j, "id")],
    )?;
    let raw = saved.as_ref().and_then(|v| v["schema_snapshot"].as_str());
    if let Some(raw) = raw {
        merge(&mut p, parse(raw)?);
    } else {
        ctx.conn.execute(
            "UPDATE jobs SET schema_snapshot=? WHERE id=? AND schema_snapshot IS NULL",
            params![
                json!({"parameters":p["parameters"],"outputs":p["outputs"]}).to_string(),
                text(j, "id")
            ],
        )?;
    }
    if flow::is_flow(ctx, j)? {
        flow::schemas(ctx, j, &mut p)?;
    }
    Ok(p)
}
pub fn progress(ctx: &Context, j: &Value) -> Result<(Vec<Value>, Vec<Value>)> {
    let r = job(ctx, text(j, "id"))?;
    let plan = if r["progress_plan"].is_null() {
        let p = catalog::product(ctx, text(j, "product_id"))?;
        let plan = p["progress_steps"].as_array().cloned().unwrap_or_default();
        ctx.conn.execute(
            "UPDATE jobs SET progress_plan=? WHERE id=? AND progress_plan IS NULL",
            params![json!(plan).to_string(), text(j, "id")],
        )?;
        plan
    } else {
        json_field(&r, "progress_plan", json!([]))
            .as_array()
            .cloned()
            .unwrap_or_default()
    };
    let completed = json_field(&r, "completed_steps", json!([]))
        .as_array()
        .cloned()
        .unwrap_or_default();
    Ok((plan, completed))
}
pub fn instructions(ctx: &Context, pid: &str) -> Result<Value> {
    let owner = require_enabled(ctx, pid)?;
    let p = catalog::product(ctx, pid)?;
    let s = required(
        &ctx.conn,
        "SELECT factory_slogan FROM shops WHERE id=?",
        [text(&owner, "shop_id")],
        "店铺不存在",
    )?;
    let mut v = json!({"schema":"extore.work-instructions.v1","shop_id":owner["shop_id"],"product_id":pid,"factory_slogan":s["factory_slogan"],"workshop_slogan":text(&p,"workshop_slogan")});
    v["revision"] = json!(digest(&v.to_string()));
    Ok(v)
}
pub fn job_view(ctx: &Context, j: &Value, staff: bool) -> Result<Value> {
    let p = job_product(ctx, j)?;
    let c = card(ctx, text(j, "card_id"))?;
    let mut result = json!({});
    for key in [
        "id",
        "product_id",
        "state",
        "message",
        "progress",
        "attempt",
        "created",
        "updated",
        "revealed",
    ] {
        result[key] = j[key].clone();
    }
    let (steps, completed) = progress(ctx, j)?;
    let done: HashSet<_> = completed.iter().filter_map(Value::as_str).collect();
    let shown = steps
        .iter()
        .map(|s| {
            let mut s = s.clone();
            s["done"] = json!(done.contains(text(&s, "id")));
            s
        })
        .collect::<Vec<_>>();
    if !steps.is_empty() {
        result["progress"] = json!(if matches!(text(j, "state"), "succeeded" | "destroyed") {
            100
        } else {
            (completed.len() * 100 / steps.len()).min(99)
        });
    }
    result["steps"] = json!(shown);
    result["completed_steps"] = json!(completed);
    for key in ["support_email", "delivery", "view_policy"] {
        result[key] = p[key].clone();
    }
    result["product_name"] = p["name"].clone();
    result["variant"] = catalog::card_variant(ctx, &c)?;
    result["can_retry"] = json!(
        (text(j, "state") == "needs_input"
            || (text(j, "state") == "failed"
                && flag(j, "retryable")
                && flag(&p, "allow_retry")
                && entitlements::round_attempt(j) < integer(&p, "max_attempts", 3)))
            && !expired(ctx, text(&c, "id"))?
            && !matches!(text(&c, "state"), "revoked" | "rejected")
    );
    if text(j, "state") == "needs_input" {
        result["params"] = json_field(j, "params", json!({}));
        result["retry_mode"] = j["retry_mode"].clone();
        result["retry_reason_type"] = j["retry_reason_type"].clone();
    }
    let active = matches!(text(j, "state"), "queued" | "processing");
    let ahead = if active {
        integer(
            &required(
                &ctx.conn,
                "SELECT count(*) AS n FROM jobs WHERE product_id=? AND state IN ('queued','processing') AND (created<? OR (created=? AND id<?))",
                params![
                    text(j, "product_id"),
                    number(j, "created"),
                    number(j, "created"),
                    text(j, "id")
                ],
                "队列不存在",
            )?,
            "n",
            0,
        )
    } else {
        0
    };
    result["queue_ahead"] = json!(ahead);
    result["queue_position"] = json!(if active { ahead + 1 } else { 0 });
    merge(&mut result, entitlements::job_context(ctx, j, true)?);
    if staff {
        result["instructions"] = instructions(ctx, text(j, "product_id"))?;
        result["params"] = json_field(j, "params", json!({}));
        result["claimed_by"] = j["claimed_by"].clone();
        result["processing_worker"] = Value::Null;
        for key in ["mode", "parameters", "outputs"] {
            result[key] = p[key].clone();
        }
        result["files"] = json!(files::listfiles(ctx, j, None)?);
    }
    if flow::is_flow(ctx, j)? {
        let view = flow::view(ctx, j, staff)?;
        result["task_flow"] = view.clone();
        if staff && matches!(text(&view, "phase"), "queued" | "processing") {
            if let Some(mut execution) = flow::execution(ctx, j, false)? {
                let protected = execution["parameters"]
                    .as_array()
                    .unwrap_or(&Vec::new())
                    .iter()
                    .filter(|f| flag(f, "sensitive"))
                    .map(|f| text(f, "key").to_owned())
                    .collect::<Vec<_>>();
                if let Some(params) = execution["params"].as_object_mut() {
                    for key in &protected {
                        params.remove(key);
                    }
                }
                for key in ["params", "parameters", "outputs", "flow_epoch", "action_id"] {
                    result[key] = execution[key].clone();
                }
                result["protected_fields"] = json!(protected);
            }
        }
    }
    Ok(result)
}
pub fn validate_values(
    fields: &Value,
    values: &Value,
    output: bool,
    preserve: bool,
) -> Result<Value> {
    let input = values
        .as_object()
        .ok_or_else(|| ApiError::bad("字段必须是对象"))?;
    let fields = fields
        .as_array()
        .ok_or_else(|| ApiError::bad("字段定义无效"))?;
    if input
        .keys()
        .any(|k| !fields.iter().any(|f| text(f, "key") == k))
    {
        return Err(ApiError::bad("提交了未定义的字段"));
    }
    let mut clean = serde_json::Map::new();
    let mut length = 0usize;
    for f in fields {
        let key = text(f, "key");
        let raw = input.get(key).unwrap_or(&Value::Null);
        let raw = if raw.is_null() {
            ""
        } else {
            raw.as_str()
                .ok_or_else(|| ApiError::bad("字段必须是文本"))?
        };
        let kind = text(f, "type");
        let v = if (!output && !preserve)
            || matches!(
                kind,
                "email" | "number" | "url" | "select" | "boolean" | "image" | "images"
            ) {
            raw.trim()
        } else {
            raw
        };
        length += v.chars().count();
        if f.get("required")
            .map(|v| v.as_bool().unwrap_or(true))
            .unwrap_or(true)
            && v.trim().is_empty()
        {
            return Err(ApiError::bad(format!(
                "请填写 {}",
                f["label"]
                    .as_object()
                    .and_then(|o| o.values().next())
                    .and_then(Value::as_str)
                    .unwrap_or(key)
            )));
        }
        if (!output && v.chars().count() > 10000) || (output && length > 100000) {
            return Err(ApiError::new(413, "字段内容超过大小上限"));
        }
        if !v.is_empty() {
            let valid = match kind {
                "email" => {
                    v.split_once('@').is_some_and(|(a, b)| {
                        !a.is_empty()
                            && b.contains('.')
                            && !b.starts_with('.')
                            && !b.ends_with('.')
                            && !b.contains('@')
                    }) && !v.chars().any(char::is_whitespace)
                }
                "number" => v.parse::<f64>().is_ok_and(f64::is_finite),
                "url" => valid_url(v),
                "select" => f["options"]
                    .as_array()
                    .is_some_and(|a| a.iter().any(|o| text(o, "value") == v)),
                "boolean" => matches!(v, "true" | "false"),
                "images" => serde_json::from_str::<Vec<String>>(v).is_ok_and(|ids| {
                    ids.len() <= integer(f, "max_items", 10) as usize
                        && ids.iter().all(|id| uuid::Uuid::parse_str(id).is_ok())
                        && ids.iter().collect::<HashSet<_>>().len() == ids.len()
                }),
                "image" | "file" => uuid::Uuid::parse_str(v).is_ok(),
                _ => true,
            };
            if !valid {
                return Err(ApiError::bad(format!("字段 {key} 格式无效")));
            }
        }
        clean.insert(key.to_owned(), json!(v));
    }
    Ok(Value::Object(clean))
}
fn valid_url(v: &str) -> bool {
    !v.contains('\\')
        && !v.chars().any(|c| c.is_whitespace() || c.is_control())
        && url::Url::parse(v).is_ok_and(|u| {
            matches!(u.scheme(), "http" | "https")
                && u.host_str().is_some()
                && u.username().is_empty()
                && u.password().is_none()
        })
}
pub fn output_content(p: &Value, output: &Value) -> String {
    let fields = p["outputs"].as_array().cloned().unwrap_or_default();
    if fields.len() == 1 && text(&fields[0], "key") == "content" {
        return text(output, "content").to_owned();
    }
    fields
        .iter()
        .filter_map(|f| {
            let value = text(output, text(f, "key"));
            if value.is_empty() {
                None
            } else {
                Some(format!(
                    "{}：\n{}",
                    f["label"]["zh-CN"]
                        .as_str()
                        .or_else(|| f["label"]
                            .as_object()
                            .and_then(|o| o.values().next())
                            .and_then(Value::as_str))
                        .unwrap_or(text(f, "key")),
                    value
                ))
            }
        })
        .collect::<Vec<_>>()
        .join("\n\n")
}
pub fn submit(ctx: &Context, c: &Value, params: &Value) -> Result<Value> {
    require_enabled(ctx, text(c, "product_id"))?;
    ensure_card(ctx, c)?;
    if let Some(j) = flow::submit(ctx, c, params)? {
        return Ok(j);
    }
    let existing = row(
        &ctx.conn,
        "SELECT * FROM jobs WHERE card_id=?",
        [text(c, "id")],
    )?;
    if existing
        .as_ref()
        .is_some_and(|j| text(j, "state") == "rejected")
    {
        return Err(ApiError::new(409, "此任务已被拒绝，不能重新提交"));
    }
    let p = if let Some(j) = &existing {
        job_product(ctx, j)?
    } else {
        catalog::card_product(ctx, c)?
    };
    let clean = validate_values(
        &p["parameters"],
        params,
        false,
        text(&p, "mode") == "script",
    )?;
    if let Some(j) = &existing {
        if text(j, "state") == "needs_input"
            && text(j, "retry_mode") == "reuse"
            && clean != json_field(j, "params", json!({}))
        {
            return Err(ApiError::new(409, "本次重试必须沿用原有参数"));
        }
    }
    files::validate_input_files(ctx, c, &p, &clean)?;
    let jid = if let Some(j) = &existing {
        if !matches!(text(j, "state"), "failed" | "needs_input") {
            return Ok(j.clone());
        }
        if text(j, "state") == "failed"
            && (!flag(j, "retryable")
                || !flag(&p, "allow_retry")
                || entitlements::round_attempt(j) >= integer(&p, "max_attempts", 3))
        {
            return Err(ApiError::new(409, "此任务不能重试"));
        }
        progress(ctx, j)?;
        files::purge_job_outputs(ctx, j)?;
        ctx.conn.execute("UPDATE jobs SET state='queued',params=?,content=NULL,result_json=NULL,message='',progress=0,completed_steps='[]',attempt=attempt+1,retryable=0,claimed_by=NULL,lease=NULL,updated=? WHERE id=?",params![clean.to_string(),now(),text(j,"id")])?;
        ctx.conn.execute(
            "UPDATE cards SET state=? WHERE id=?",
            params![
                if integer(j, "revision_round", 0) > 0 {
                    "used"
                } else {
                    "reserved"
                },
                text(c, "id")
            ],
        )?;
        text(j, "id").to_owned()
    } else {
        if text(c, "state") != "ready" {
            return Err(ApiError::new(409, "卡密不可兑换"));
        }
        let jid = id();
        ctx.conn.execute("INSERT INTO jobs(id,card_id,product_id,state,params,created,updated,progress_plan,schema_snapshot) VALUES (?,?,?,'queued',?,?,?,?,?)",params![jid,text(c,"id"),text(c,"product_id"),clean.to_string(),now(),now(),p["progress_steps"].to_string(),json!({"parameters":p["parameters"],"outputs":p["outputs"]}).to_string()])?;
        ctx.conn.execute(
            "UPDATE cards SET state='reserved' WHERE id=?",
            [text(c, "id")],
        )?;
        jid
    };
    let j = job(ctx, &jid)?;
    files::bind_inputs(ctx, &j, &p, &clean)?;
    if text(&p, "mode") == "stock" {
        let assignment = required(
            &ctx.conn,
            "SELECT t.*,p.shop_id AS current_shop,c.product_id AS current_product FROM text_card_payloads t JOIN cards c ON c.id=t.card_id JOIN products p ON p.id=c.product_id WHERE t.card_id=?",
            [text(c, "id")],
            "此卡密没有可交付文本",
        )?;
        if assignment["shop_id"] != assignment["current_shop"]
            || assignment["product_id"] != assignment["current_product"]
            || assignment["ciphertext"].is_null()
        {
            return Err(ApiError::new(410, "此卡密没有可交付文本"));
        }
        let content = auth::secret::open_secret(
            &ctx.app,
            Some(text(&assignment, "shop_id")),
            "text-card",
            text(c, "id"),
            text(&assignment, "ciphertext"),
        )?;
        apply_simple_update(
            ctx,
            &jid,
            &json!({"state":"succeeded","attempt":j["attempt"],"output":{"content":content}}),
        )?;
        ctx.conn.execute(
            "UPDATE text_card_payloads SET ciphertext=NULL WHERE card_id=?",
            [text(c, "id")],
        )?;
    } else {
        event(ctx, "redemption.requested", text(c, "product_id"), Some(&j))?;
    }
    job(ctx, &jid)
}
pub fn apply_update(ctx: &Context, jid: &str, u: &Value) -> Result<Value> {
    let j = job(ctx, jid)?;
    if flow::is_flow(ctx, &j)? {
        flow::process_update(ctx, &j, u)
    } else {
        apply_simple_update(ctx, jid, u)
    }
}
pub fn apply_simple_update(ctx: &Context, jid: &str, u: &Value) -> Result<Value> {
    let j = job(ctx, jid)?;
    require_enabled(ctx, text(&j, "product_id"))?;
    let c = card(ctx, text(&j, "card_id"))?;
    if matches!(text(&c, "state"), "revoked" | "rejected") {
        return Err(ApiError::new(410, "卡密已撤销或拒绝"));
    }
    let state = text(u, "state");
    if !matches!(state, "processing" | "succeeded" | "failed") {
        return Err(ApiError::bad("任务状态无效"));
    }
    let attempt = integer(u, "attempt", 0);
    if attempt < 1 || attempt != integer(&j, "attempt", 1) {
        return Err(ApiError::new(409, "提交对应的任务尝试已失效"));
    }
    if state == text(&j, "state") && matches!(state, "succeeded" | "failed") {
        return Ok(j);
    }
    if !matches!(text(&j, "state"), "queued" | "processing") {
        return Err(ApiError::new(409, "任务已完成或当前不能更新"));
    }
    let p = job_product(ctx, &j)?;
    let message = text(u, "message");
    if message.chars().count() > 1000 {
        return Err(ApiError::bad("任务消息过长"));
    }
    let (plan, old_done) = progress(ctx, &j)?;
    let mut completed = if let Some(v) = u.get("completed_steps").filter(|v| !v.is_null()) {
        v.as_array()
            .cloned()
            .ok_or_else(|| ApiError::bad("完成步骤格式无效"))?
    } else {
        old_done.clone()
    };
    if completed.len() > 30
        || completed.iter().any(|v| !v.is_string())
        || completed.iter().collect::<HashSet<_>>().len() != completed.len()
    {
        return Err(ApiError::bad("完成步骤无效"));
    }
    let ids = plan
        .iter()
        .map(|s| json!(text(s, "id")))
        .collect::<Vec<_>>();
    if completed.iter().any(|id| !ids.contains(id))
        || old_done.iter().any(|id| !completed.contains(id))
    {
        return Err(ApiError::new(409, "处理步骤不能回退或引用未定义步骤"));
    }
    if state == "succeeded" {
        completed = ids.clone();
    } else {
        completed = ids
            .iter()
            .filter(|id| completed.contains(id))
            .cloned()
            .collect();
    }
    let supplied_progress = integer(u, "progress", 0);
    if !(0..=100).contains(&supplied_progress) {
        return Err(ApiError::bad("进度必须为 0 到 100"));
    }
    let progress = if state == "succeeded" {
        100
    } else if !plan.is_empty() {
        (completed.len() * 100 / plan.len()).min(99) as i64
    } else {
        if state == "processing" && supplied_progress < integer(&j, "progress", 0) {
            return Err(ApiError::new(409, "进度不能回退"));
        }
        supplied_progress.min(99)
    };
    let has_output = u
        .get("output")
        .is_some_and(|v| !v.is_null() && v.as_object().is_none_or(|o| !o.is_empty()))
        || u.get("content").is_some_and(|v| !v.is_null());
    if state != "succeeded" && has_output {
        return Err(ApiError::bad("只有成功结果可以交付内容"));
    }
    let mut output = Value::Null;
    let mut content = Value::Null;
    if state == "succeeded" {
        if text(&p, "delivery") == "service" {
            if has_output {
                return Err(ApiError::bad("服务交付不能包含内容"));
            }
        } else {
            let mut raw = u
                .get("output")
                .filter(|v| !v.is_null())
                .cloned()
                .unwrap_or(json!({}));
            if !raw.is_object() {
                return Err(ApiError::bad("输出字段必须是对象"));
            }
            if let Some(legacy) = u.get("content").filter(|v| !v.is_null()) {
                if p["outputs"]
                    .as_array()
                    .is_none_or(|a| a.len() != 1 || text(&a[0], "key") != "content")
                {
                    return Err(ApiError::bad("此商品需要结构化交付内容"));
                }
                if raw.get("content").is_some_and(|v| v != legacy) {
                    return Err(ApiError::bad("两种交付内容不一致"));
                }
                raw["content"] = legacy.clone();
            }
            output = validate_values(&p["outputs"], &raw, true, true)?;
            files::validate_output_files(ctx, &j, &p, &output)?;
            content = json!(output_content(&p, &output));
            if integer(&j, "revision_round", 0) > 0 {
                files::check_storage_quota(
                    ctx,
                    (content.as_str().unwrap_or("").len() + output.to_string().len()) as u64,
                    text(&j, "product_id"),
                )?;
            }
        }
    }
    ctx.conn.execute("UPDATE jobs SET state=?,progress=?,message=?,content=?,result_json=?,completed_steps=?,retryable=?,updated=?,lease=? WHERE id=?",params![state,progress,message,content.as_str(),if output.is_null(){None}else{Some(output.to_string())},json!(completed).to_string(),state=="failed"&&flag(u,"retryable"),now(),if state=="processing"{Some(now()+3600.0)}else{None},jid])?;
    let result = job(ctx, jid)?;
    if state == "succeeded" {
        ctx.conn.execute(
            "UPDATE cards SET state='used' WHERE id=?",
            [text(&j, "card_id")],
        )?;
        files::bind_outputs(ctx, &result, &p, &output)?;
    }
    event(
        ctx,
        &format!("fulfillment.{state}"),
        text(&j, "product_id"),
        Some(&result),
    )?;
    Ok(result)
}
pub fn event(ctx: &Context, kind: &str, pid: &str, j: Option<&Value>) -> Result<String> {
    let eid = id();
    let mut payload =
        json!({"id":eid,"type":kind,"version":1,"created_at":now(),"product_id":pid,"data":{}});
    if let Some(j) = j {
        let mut data = json!({});
        for k in ["id", "state", "attempt", "progress", "message"] {
            data[k] = j[k].clone();
        }
        if text(j, "state") == "needs_input" {
            data["retry_mode"] = j["retry_mode"].clone();
            data["retry_reason_type"] = j["retry_reason_type"].clone();
        }
        data["variant"] = catalog::card_variant(ctx, &card(ctx, text(j, "card_id"))?)?;
        merge(&mut data, entitlements::job_context(ctx, j, false)?);
        let request = matches!(kind, "redemption.requested" | "revision.requested");
        if !request {
            data.as_object_mut().unwrap().remove("card_attributes");
            data["revision"].as_object_mut().unwrap().remove("message");
        }
        let (steps, completed) = progress(ctx, j)?;
        data["steps"] = json!(
            steps
                .into_iter()
                .map(|mut s| {
                    s["done"] = json!(completed.contains(&s["id"]));
                    s
                })
                .collect::<Vec<_>>()
        );
        data["completed_steps"] = json!(completed);
        if request {
            data["instructions"] = instructions(ctx, pid)?;
            data["params"] = json_field(j, "params", json!({}));
        }
        payload["data"] = data;
    }
    ctx.conn.execute(
        "INSERT INTO events(id,type,job_id,product_id,payload,created) VALUES (?,?,?,?,?,?)",
        params![
            eid,
            kind,
            j.map(|j| text(j, "id")),
            pid,
            payload.to_string(),
            now()
        ],
    )?;
    let p = catalog::product(ctx, pid)?;
    if !text(&p, "webhook_url").is_empty() {
        ctx.conn.execute(
            "INSERT INTO outbox(id,url,secret,due) VALUES (?,?,?,?)",
            params![
                eid,
                text(&p, "webhook_url"),
                text(&p, "webhook_secret"),
                now()
            ],
        )?;
    }
    Ok(eid)
}

mod routes;
pub use routes::{handle, release_claims};

pub mod private_worker;

#[cfg(test)]
mod tests;

/// Applied before the request transaction so rejected code guesses still count.
pub fn admit(ctx: &Context) -> Result<()> {
    if ctx.method.as_str() != "POST" {
        return Ok(());
    }
    let (category, limit) = match ctx.path.as_str() {
        "/api/exchange" | "/api/batch/exchange" => ("exchange", 20),
        "/api/redeem" | "/api/batch/redeem" => ("redeem", 20),
        "/api/receipt/revisions" => ("revision", 20),
        "/api/retry" => ("retry", 20),
        path if path.starts_with("/api/task-flow/") => ("task-flow", 60),
        _ => return Ok(()),
    };
    let clock = now();
    let key = format!("public:{category}:{}", digest(&ctx.peer));
    let r = required(
        &ctx.conn,
        "INSERT INTO rate_limits(key,count,expires) VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET count=CASE WHEN rate_limits.expires<=? THEN 1 ELSE rate_limits.count+1 END,expires=CASE WHEN rate_limits.expires<=? THEN excluded.expires ELSE rate_limits.expires END RETURNING count",
        params![key, clock + 60.0, clock, clock],
        "限流状态不可用",
    )?;
    if integer(&r, "count", 0) > limit {
        return Err(ApiError::new(429, "请求过于频繁，请稍后再试"));
    }
    Ok(())
}

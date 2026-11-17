//! A metadata-only progress view: no customer parameters, output, or secret decryption.
use crate::{auth, common::*};
use rusqlite::{params, types::Value as Sql};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
const STATES: [&str; 8] = [
    "queued",
    "processing",
    "waiting",
    "failed",
    "needs_input",
    "succeeded",
    "rejected",
    "destroyed",
];
const PHASES: [&str; 6] = [
    "await_start",
    "input",
    "display",
    "queued",
    "processing",
    "ended",
];
const IDENTITY: &str = " LEFT JOIN job_worker_identities wi ON wi.job_id=j.id AND wi.attempt=j.attempt AND wi.actor=j.claimed_by ";
const WORKER: &str = "CASE WHEN wi.channel='cli' AND wi.device_ref IS NOT NULL THEN COALESCE(wi.worker_ref,'cli:'||wi.device_ref) ELSE j.claimed_by END";
const CLAUSE: &str = "p.shop_id=? AND (?='' OR p.id=?)";
fn counts() -> Value {
    let mut c = json!({});
    for state in STATES {
        c[state] = json!(0)
    }
    c
}
fn safe_name(v: &Value, fallback: &str) -> String {
    let s = v
        .as_str()
        .unwrap_or("")
        .chars()
        .filter(|c| !auth::cli::hidden(*c) && (!c.is_whitespace() || *c == ' '))
        .take(120)
        .collect::<String>();
    if s.is_empty() { fallback.to_owned() } else { s }
}
fn stamp(v: &Value, nullable: bool) -> Result<Value> {
    if v.as_f64().is_some_and(|n| n.is_finite() && n > 0.0) {
        Ok(v.clone())
    } else if nullable {
        Ok(Value::Null)
    } else {
        Err(ApiError::new(409, "任务时间信息无效，无法显示进度"))
    }
}
fn slogan(v: &Value) -> Result<Value> {
    let s = v
        .as_str()
        .ok_or_else(|| ApiError::new(409, "工作指令格式无效"))?;
    if s.chars().count() > 4000
        || s.chars()
            .any(|c| c.is_control() && !['\n', '\r', '\t'].contains(&c))
    {
        return Err(ApiError::new(409, "工作指令格式无效"));
    }
    Ok(json!(s))
}
fn params_scope(sid: &str, pid: &str) -> Vec<Sql> {
    vec![
        sid.to_owned().into(),
        pid.to_owned().into(),
        pid.to_owned().into(),
    ]
}
fn steps(ctx: &Context, j: &Value) -> Result<Vec<Value>> {
    if !j["flow_phase"].is_null() {
        let rows = rows(
            &ctx.conn,
            "SELECT flow_epoch,state FROM task_flow_steps WHERE job_id=? AND attempt=? ORDER BY flow_epoch LIMIT 257",
            params![text(j, "id"), integer(j, "attempt", 0)],
        )?;
        if rows.len() > 256 {
            return Err(ApiError::new(409, "流程步骤数量超出看板安全范围"));
        }
        return Ok(rows.iter().enumerate().map(|(i,s)|json!({"position":i+1,"state":if text(s,"state")=="completed"{"done"}else if text(s,"state")=="active"&&s["flow_epoch"]==j["flow_epoch"]&&PHASES[..5].contains(&text(j,"flow_phase")){"current"}else{"pending"}})).collect());
    }
    let rows = rows(
        &ctx.conn,
        "SELECT CAST(p.key AS INTEGER)+1 AS position, EXISTS(SELECT 1 FROM json_each(CASE WHEN json_valid(j.completed_steps) THEN j.completed_steps ELSE '[]' END) d WHERE d.type='text' AND d.value=CASE WHEN p.type='object' THEN json_extract(p.value,'$.id') END) AS done FROM jobs j,json_each(CASE WHEN json_valid(j.progress_plan) THEN CASE WHEN json_type(j.progress_plan)='array' THEN j.progress_plan ELSE '[]' END ELSE '[]' END) p WHERE j.id=? ORDER BY CAST(p.key AS INTEGER) LIMIT 31",
        [text(j, "id")],
    )?;
    if rows.len() > 30 {
        return Err(ApiError::new(409, "处理步骤数量超出看板安全范围"));
    }
    let current = rows
        .iter()
        .find(|s| !flag(s, "done"))
        .map(|s| s["position"].clone());
    Ok(rows.iter().map(|s|json!({"position":s["position"],"state":if flag(s,"done"){"done"}else if text(j,"state")=="processing"&&current.as_ref()==Some(&s["position"]){"current"}else{"pending"}})).collect())
}
pub fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    if !ctx.is("GET", "/api/manage/progress-board") {
        return Ok(None);
    }
    let s = auth::session(ctx, &["admin", "staff"], None)?;
    let generated = now();
    let limit = if ctx.q("limit").is_empty() {
        100
    } else {
        ctx.q("limit")
            .parse::<i64>()
            .map_err(|_| ApiError::new(422, "分页参数无效"))?
    };
    let offset = if ctx.q("offset").is_empty() {
        0
    } else {
        ctx.q("offset")
            .parse::<i64>()
            .map_err(|_| ApiError::new(422, "分页参数无效"))?
    };
    let view = if ctx.q("view").is_empty() {
        "active"
    } else {
        ctx.q("view")
    };
    if !(1..=200).contains(&limit)
        || !(0..=1_000_000).contains(&offset)
        || !["active", "processed"].contains(&view)
        || ctx.q("shop_id").len() > 100
        || ctx.q("product_id").len() > 100
    {
        return Err(ApiError::new(422, "进度看板参数无效"));
    }
    let mut pid = ctx.q("product_id").to_owned();
    if text(&s, "role") == "staff" {
        if s["permissions"]
            .as_array()
            .is_none_or(|a| !a.iter().any(|p| p == "queue.monitor" || p == "queue.view"))
        {
            return Err(ApiError::new(403, "此商品管理链接没有查看进度看板的权限"));
        }
        if !pid.is_empty() && pid != text(&s, "product_id") {
            return Err(ApiError::new(403, "没有此商品的进度查看权限"));
        }
        pid = text(&s, "product_id").to_owned();
    }
    let sid = auth::selected_shop(ctx, &s, ctx.q("shop_id"))?
        .ok_or_else(|| ApiError::bad("请先选择要查看进度的店铺"))?;
    let shop = required(
        &ctx.conn,
        "SELECT id,name,enabled,factory_slogan FROM shops WHERE id=?",
        [&sid],
        "店铺不存在",
    )?;
    if !flag(&shop, "enabled") {
        return Err(ApiError::new(401, "店铺不可用"));
    }
    if !pid.is_empty() {
        let p = required(
            &ctx.conn,
            "SELECT shop_id FROM products WHERE id=?",
            [&pid],
            "商品不存在",
        )?;
        if text(&p, "shop_id") != sid {
            return Err(ApiError::new(403, "商品不属于所选店铺"));
        }
    }
    let shop = json!({"id":sid,"name":safe_name(&shop["name"],"店铺"),"factory_slogan":slogan(&shop["factory_slogan"])?});
    let products = rows(
        &ctx.conn,
        &format!(
            "SELECT p.id, CASE WHEN json_valid(p.config) THEN CASE WHEN json_type(p.config,'$.name')='text' THEN json_extract(p.config,'$.name') END END name, CASE WHEN json_valid(p.config) THEN CASE WHEN json_type(p.config,'$.mode')='text' THEN json_extract(p.config,'$.mode') END END mode, CASE WHEN json_valid(p.config) THEN CASE WHEN json_type(p.config,'$.workshop_slogan')='text' THEN json_extract(p.config,'$.workshop_slogan') WHEN json_type(p.config,'$.workshop_slogan') IS NULL THEN '' END END workshop_slogan FROM products p WHERE {CLAUSE} ORDER BY p.created,p.id LIMIT 501"
        ),
        params![sid, pid, pid],
    )?;
    if products.len() > 500 {
        return Err(ApiError::new(409, "商品数量超过看板范围，请按商品查看"));
    }
    let order = products
        .iter()
        .map(|p| text(p, "id").to_owned())
        .collect::<Vec<_>>();
    let mut summaries = BTreeMap::new();
    let mut totals = counts();
    for p in products {
        summaries.insert(text(&p,"id").to_owned(),json!({"id":p["id"],"name":safe_name(&p["name"],"商品"),"mode":if ["manual","script","webhook","stock"].contains(&text(&p,"mode")){text(&p,"mode")}else{"unknown"},"workshop_slogan":slogan(&p["workshop_slogan"])? ,"counts":counts(),"jobs":[]}));
    }
    for c in rows(
        &ctx.conn,
        &format!(
            "SELECT j.product_id,j.state,COUNT(*) n FROM jobs j JOIN products p ON p.id=j.product_id WHERE {CLAUSE} GROUP BY j.product_id,j.state"
        ),
        params![sid, pid, pid],
    )? {
        let state = text(&c, "state");
        if STATES.contains(&state) {
            totals[state] = json!(integer(&totals, state, 0) + integer(&c, "n", 0));
            if let Some(p) = summaries.get_mut(text(&c, "product_id")) {
                p["counts"][state] = json!(integer(&p["counts"], state, 0) + integer(&c, "n", 0));
            }
        }
    }
    let selected = if view == "active" {
        &STATES[..5]
    } else {
        &STATES[5..]
    };
    let total = selected.iter().map(|s| integer(&totals, s, 0)).sum::<i64>();
    let slots = vec!["?"; selected.len()].join(",");
    let order_sql = if view == "processed" {
        "j.updated DESC,j.id"
    } else {
        "CASE j.state WHEN 'processing' THEN 0 WHEN 'waiting' THEN 1 WHEN 'needs_input' THEN 2 WHEN 'queued' THEN 3 ELSE 4 END,j.created,j.id"
    };
    let mut arguments = params_scope(&sid, &pid);
    arguments.extend(selected.iter().map(|s| Sql::Text(s.to_string())));
    arguments.push(limit.into());
    arguments.push(offset.into());
    let jobs = rows(
        &ctx.conn,
        &format!(
            "SELECT j.id,j.product_id,j.state,j.progress,j.attempt,j.created,j.updated,j.claimed_by,{WORKER} worker_key,t.phase flow_phase,t.flow_epoch,CASE WHEN j.state='queued' THEN (SELECT COUNT(*) FROM jobs q WHERE q.product_id=j.product_id AND q.state='queued' AND (q.created<j.created OR (q.created=j.created AND q.id<=j.id))) END queue_position FROM jobs j JOIN products p ON p.id=j.product_id LEFT JOIN task_flow_runs t ON t.job_id=j.id AND t.attempt=j.attempt {IDENTITY} WHERE {CLAUSE} AND j.state IN ({slots}) ORDER BY {order_sql} LIMIT ? OFFSET ?"
        ),
        rusqlite::params_from_iter(arguments),
    )?;
    let mut keys = jobs
        .iter()
        .filter_map(|j| {
            j["worker_key"]
                .as_str()
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
        })
        .collect::<BTreeSet<_>>();
    for key in rows(
        &ctx.conn,
        &format!(
            "SELECT DISTINCT {WORKER} worker_key FROM jobs j JOIN products p ON p.id=j.product_id {IDENTITY} WHERE {CLAUSE} AND j.state='processing' AND j.claimed_by IS NOT NULL AND j.claimed_by<>'' LIMIT 501"
        ),
        params![sid, pid, pid],
    )? {
        if !text(&key, "worker_key").is_empty() {
            keys.insert(text(&key, "worker_key").to_owned());
        }
    }
    if keys.len() > 500 {
        return Err(ApiError::new(409, "处理人员数量超过看板范围，请按商品查看"));
    }
    let mut workers = BTreeMap::<String, Value>::new();
    let mut identity_times = BTreeMap::<String, f64>::new();
    if !keys.is_empty() {
        let slots = vec!["?"; keys.len()].join(",");
        let mut arguments = params_scope(&sid, &pid);
        arguments.extend(keys.iter().cloned().map(Sql::Text));
        let names=rows(&ctx.conn,&format!("SELECT s.id,s.name FROM staff s JOIN products p ON p.id=s.product_id WHERE {CLAUSE} AND s.id IN ({slots})"),rusqlite::params_from_iter(arguments.clone()))?.into_iter().map(|r|(text(&r,"id").to_owned(),r["name"].clone())).collect::<BTreeMap<_,_>>();
        let aggregates = rows(
            &ctx.conn,
            &format!(
                "SELECT {WORKER} worker_key,j.claimed_by,j.state,wi.client_name,wi.agent_type,wi.channel,MAX(wi.claimed_at) identity_time,COUNT(*) n,MAX(j.updated) last_update FROM jobs j JOIN products p ON p.id=j.product_id {IDENTITY} WHERE {CLAUSE} AND ({WORKER}) IN ({slots}) GROUP BY worker_key,j.claimed_by,j.state,wi.client_name,wi.agent_type,wi.channel"
            ),
            rusqlite::params_from_iter(arguments),
        )?;
        for a in aggregates {
            let key = text(&a, "worker_key");
            let actor = text(&a, "claimed_by");
            let (name, kind, agent) = if actor == "worker" {
                ("商品处理器".to_owned(), "automatic", json!("processor"))
            } else if actor == "owner" || actor == format!("shop:{sid}") {
                ("店主".to_owned(), "merchant", json!("human"))
            } else {
                (
                    safe_name(names.get(actor).unwrap_or(&Value::Null), "处理人员"),
                    "unknown",
                    Value::Null,
                )
            };
            let w=workers.entry(key.to_owned()).or_insert_with(||json!({"id":digest(&format!("extore-progress-board-v1\n{sid}\n{key}")),"name":name,"kind":kind,"agent_type":agent,"active_jobs":0,"completed_jobs":0,"last_update":null}));
            if let Some(t) = stamp(&a["identity_time"], true)?.as_f64() {
                if t >= *identity_times.get(key).unwrap_or(&0.0) {
                    identity_times.insert(key.to_owned(), t);
                    w["name"] = json!(safe_name(&a["client_name"], &name));
                    w["kind"] = json!(match text(&a, "channel") {
                        "cli" => "cli",
                        "browser" => "human",
                        "automatic" => "automatic",
                        _ => "unknown",
                    });
                    w["agent_type"] = a["agent_type"].clone();
                }
            }
            if text(&a, "state") == "processing" {
                w["active_jobs"] = json!(integer(w, "active_jobs", 0) + integer(&a, "n", 0));
            }
            if STATES[5..].contains(&text(&a, "state")) {
                w["completed_jobs"] = json!(integer(w, "completed_jobs", 0) + integer(&a, "n", 0));
            }
            if let Some(t) = stamp(&a["last_update"], true)?.as_f64() {
                w["last_update"] = json!(number(w, "last_update").max(t));
            }
        }
    }
    for j in &jobs {
        let steps = steps(ctx, j)?;
        let done = steps.iter().filter(|v| text(v, "state") == "done").count();
        let mut progress = j["progress"].as_i64().unwrap_or(0);
        if (!j["flow_phase"].is_null() || steps.is_empty())
            && (j["progress"].as_i64().is_none() || !(0..=100).contains(&progress))
        {
            return Err(ApiError::new(409, "任务进度信息无效，无法显示进度"));
        }
        if j["flow_phase"].is_null() && !steps.is_empty() {
            progress = if ["succeeded", "destroyed"].contains(&text(j, "state")) {
                100
            } else {
                (done * 100 / steps.len()).min(99) as i64
            };
        }
        if j["attempt"].as_i64().is_none_or(|n| n < 1) {
            return Err(ApiError::new(409, "任务尝试次数无效，无法显示进度"));
        }
        let view = json!({"id":j["id"],"state":j["state"],"progress":progress,"attempt":j["attempt"],"created":stamp(&j["created"],false)?,"updated":stamp(&j["updated"],false)?,"queue_position":j["queue_position"],"worker_id":workers.get(text(j,"worker_key")).map(|w|w["id"].clone()),"step_count":steps.len(),"completed_step_count":done,"steps":steps,"flow_phase":if PHASES.contains(&text(j,"flow_phase")){j["flow_phase"].clone()}else{Value::Null}});
        if let Some(p) = summaries.get_mut(text(j, "product_id")) {
            p["jobs"].as_array_mut().unwrap().push(view);
        }
    }
    let products = order
        .iter()
        .filter_map(|id| summaries.remove(id))
        .collect::<Vec<_>>();
    Ok(Some(Reply::json(
        json!({"schema":"extore.progress-board.v1","generated_at":generated,"shop":shop,"totals":totals,"products":products,"workers":workers.into_values().collect::<Vec<_>>(),"pagination":{"limit":limit,"offset":offset,"total":total,"has_more":offset+(jobs.len() as i64)<total},"scope":{"product_ids":order}}),
    )))
}

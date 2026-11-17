//! Issued-card-bound task graphs. Private payloads remain encrypted at rest.
use super::*;
use crate::auth::secret::{open_secret, store_secret};
use hkdf::Hkdf;
mod engine;
mod worker;
pub use engine::{claim, destroy, handle, process_update, release_claim, stop, submit};
pub use worker::worker_once;

pub fn fingerprint(ctx: &Context, value: &Value, shop: &str, resource: &str) -> Result<String> {
    let master = std::fs::read(ctx.app.data.join("master-secrets.key"))?;
    if master.len() != 32 {
        return Err(ApiError::new(500, "加密密钥无效"));
    }
    let mut tenant = [0u8; 32];
    Hkdf::<Sha256>::new(Some(b"extore-tenant-secret-v1"), &master)
        .expand(&serde_json::to_vec(&json!(shop))?, &mut tenant)
        .map_err(|_| ApiError::new(500, "密钥派生失败"))?;
    let aad = serde_json::to_vec(&json!([
        "extore-secret-v1",
        shop,
        "task-flow-receipt",
        resource
    ]))?;
    let mut receipt = [0u8; 32];
    Hkdf::<Sha256>::new(Some(b"extore-task-flow-receipt-v1"), &tenant)
        .expand(&aad, &mut receipt)
        .map_err(|_| ApiError::new(500, "密钥派生失败"))?;
    let mut mac =
        Hmac::<Sha256>::new_from_slice(&receipt).map_err(|_| ApiError::new(500, "密钥派生失败"))?;
    mac.update(&serde_json::to_vec(value)?);
    Ok(mac
        .finalize()
        .into_bytes()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect())
}
pub fn freeze_flow_card(ctx: &Context, cid: &str, p: &Value) -> Result<()> {
    let definition = definition::normalize(&p["task_flow"], p)?;
    if definition.is_null() {
        return Ok(());
    }
    let c = card(ctx, cid)?;
    let owner = require_enabled(ctx, text(p, "id"))?;
    if text(&c, "product_id") != text(p, "id")
        || row(
            &ctx.conn,
            "SELECT 1 FROM card_task_flows WHERE card_id=?",
            [cid],
        )?
        .is_some()
    {
        return Err(ApiError::new(409, "任务流程发行范围不匹配或快照已经存在"));
    }
    let mut product = json!({});
    for key in [
        "id",
        "name",
        "mode",
        "delivery",
        "view_policy",
        "parameters",
        "outputs",
        "allow_retry",
        "max_attempts",
        "processor_id",
        "webhook_url",
        "webhook_secret",
    ] {
        if let Some(v) = p.get(key) {
            product[key] = v.clone();
        }
    }
    product["shop_id"] = owner["shop_id"].clone();
    let snapshot = json!({"definition":definition,"product":product});
    let sid = text(&owner, "shop_id");
    let hash = fingerprint(ctx, &snapshot, sid, &format!("card:{cid}"))?;
    let encrypted = store_secret(&ctx.app, Some(sid), "task-flow-card", cid, &snapshot)?;
    files::check_storage_quota(ctx, encrypted.len() as u64, text(p, "id"))?;
    ctx.conn.execute("INSERT INTO card_task_flows(card_id,product_id,shop_id,definition_hash,snapshot_ciphertext,created) VALUES (?,?,?,?,?,?)",params![cid,text(p,"id"),sid,hash,encrypted,now()])?;
    Ok(())
}
pub fn flow_card_snapshot(ctx: &Context, c: &Value) -> Result<Option<Value>> {
    let cid = if c.get("card_id").is_some() {
        text(c, "card_id")
    } else {
        text(c, "id")
    };
    let Some(binding) = row(
        &ctx.conn,
        "SELECT * FROM card_task_flows WHERE card_id=?",
        [cid],
    )?
    else {
        return Ok(None);
    };
    let mut snapshot = open_secret(
        &ctx.app,
        Some(text(&binding, "shop_id")),
        "task-flow-card",
        cid,
        text(&binding, "snapshot_ciphertext"),
    )?;
    let hash = fingerprint(
        ctx,
        &snapshot,
        text(&binding, "shop_id"),
        &format!("card:{cid}"),
    )?;
    if !bool::from(
        hash.as_bytes()
            .ct_eq(text(&binding, "definition_hash").as_bytes()),
    ) {
        return Err(ApiError::new(409, "任务流程快照无效"));
    }
    snapshot["definition_hash"] = binding["definition_hash"].clone();
    Ok(Some(snapshot))
}
pub fn is_flow(ctx: &Context, j: &Value) -> Result<bool> {
    Ok(row(
        &ctx.conn,
        "SELECT 1 FROM card_task_flows WHERE card_id=?",
        [text(j, "card_id")],
    )?
    .is_some())
}
pub fn snapshot(ctx: &Context, j: &Value) -> Result<Value> {
    flow_card_snapshot(ctx, j)?.ok_or_else(|| ApiError::new(409, "此卡密没有任务流程"))
}
fn run(ctx: &Context, j: &Value) -> Result<Value> {
    required(
        &ctx.conn,
        "SELECT * FROM task_flow_runs WHERE job_id=?",
        [text(j, "id")],
        "任务流程尚未开始",
    )
}
fn node(s: &Value, nid: &str) -> Result<Value> {
    s["definition"]["nodes"]
        .as_array()
        .and_then(|a| a.iter().find(|n| text(n, "id") == nid))
        .cloned()
        .ok_or_else(|| ApiError::new(409, "任务流程节点无效"))
}
fn step(ctx: &Context, r: &Value, epoch: Option<i64>) -> Result<Value> {
    required(
        &ctx.conn,
        "SELECT * FROM task_flow_steps WHERE job_id=? AND flow_epoch=?",
        params![
            text(r, "job_id"),
            epoch.unwrap_or(integer(r, "flow_epoch", 0))
        ],
        "流程步骤不存在",
    )
}
fn guard(ctx: &Context, j: &Value) -> Result<(Value, Value, Value)> {
    let j = job(ctx, text(j, "id"))?;
    let s = snapshot(ctx, &j)?;
    let owner = require_enabled(ctx, text(&j, "product_id"))?;
    let current = catalog::product(ctx, text(&j, "product_id"))?;
    let frozen = &s["product"];
    if owner["shop_id"] != frozen["shop_id"]
        || j["product_id"] != frozen["id"]
        || current["mode"] != frozen["mode"]
    {
        return Err(ApiError::new(403, "任务流程的店铺或商品授权已失效"));
    }
    if text(frozen, "mode") == "webhook"
        && (text(&current, "webhook_secret").is_empty()
            || !bool::from(
                text(&current, "webhook_secret")
                    .as_bytes()
                    .ct_eq(text(frozen, "webhook_secret").as_bytes()),
            ))
    {
        return Err(ApiError::new(403, "任务流程的回调授权已撤销"));
    }
    if text(frozen, "mode") == "script" && current["processor_id"] != frozen["processor_id"] {
        return Err(ApiError::new(403, "任务流程的处理器授权已失效"));
    }
    let c = card(ctx, text(&j, "card_id"))?;
    if matches!(text(&c, "state"), "revoked" | "rejected")
        || matches!(text(&j, "state"), "destroyed" | "rejected")
    {
        return Err(ApiError::new(410, "任务流程已撤销或销毁"));
    }
    let r = run(ctx, &j)?;
    if r["attempt"] != j["attempt"] {
        return Err(ApiError::new(409, "任务流程尝试不匹配"));
    }
    Ok((j, r, s))
}
fn mutate(ctx: &Context, r: &Value, changes: Value) -> Result<Value> {
    let mut changes = changes
        .as_object()
        .cloned()
        .ok_or_else(|| ApiError::new(500, "Invalid internal flow mutation"))?;
    changes.insert("revision".into(), json!(integer(r, "revision", 0) + 1));
    changes.insert("updated".into(), json!(now()));
    let allowed = [
        "attempt",
        "flow_epoch",
        "revision",
        "node_id",
        "phase",
        "entered_at",
        "started_at",
        "deadline",
        "input_expires_at",
        "transition_count",
        "updated",
    ];
    let mut assignments = Vec::new();
    let mut values = Vec::<rusqlite::types::Value>::new();
    for (key, value) in changes {
        if !allowed.contains(&key.as_str()) {
            return Err(ApiError::new(500, "Invalid flow column"));
        }
        assignments.push(format!("{key}=?"));
        values.push(match value {
            Value::Null => rusqlite::types::Value::Null,
            Value::String(v) => v.into(),
            Value::Number(v) => {
                if let Some(v) = v.as_i64() {
                    v.into()
                } else {
                    v.as_f64().unwrap().into()
                }
            }
            _ => return Err(ApiError::new(500, "Invalid flow column value")),
        });
    }
    values.extend([
        text(r, "job_id").to_owned().into(),
        integer(r, "revision", 0).into(),
        integer(r, "flow_epoch", 0).into(),
        integer(r, "attempt", 1).into(),
    ]);
    let changed=ctx.conn.execute(&format!("UPDATE task_flow_runs SET {} WHERE job_id=? AND revision=? AND flow_epoch=? AND attempt=?",assignments.join(",")),rusqlite::params_from_iter(values))?;
    if changed != 1 {
        return Err(ApiError::new(409, "任务流程状态已改变，请刷新"));
    }
    required(
        &ctx.conn,
        "SELECT * FROM task_flow_runs WHERE job_id=?",
        [text(r, "job_id")],
        "任务流程不存在",
    )
}
fn payload(ctx: &Context, s: &Value, step: &Value) -> Result<Value> {
    open_secret(
        &ctx.app,
        Some(text(&s["product"], "shop_id")),
        "task-flow-step",
        &format!(
            "{}:{}",
            text(step, "job_id"),
            integer(step, "flow_epoch", 0)
        ),
        text(step, "payload_ciphertext"),
    )
}
fn write_payload(ctx: &Context, s: &Value, step: &Value, v: &Value) -> Result<()> {
    if serde_json::to_vec(v)?.len() > 200000 {
        return Err(ApiError::new(413, "任务流程单步数据过大，请使用附件"));
    }
    let encrypted = store_secret(
        &ctx.app,
        Some(text(&s["product"], "shop_id")),
        "task-flow-step",
        &format!(
            "{}:{}",
            text(step, "job_id"),
            integer(step, "flow_epoch", 0)
        ),
        v,
    )?;
    let old = text(step, "payload_ciphertext").len();
    let extra = encrypted.len().saturating_sub(old);
    if extra > 0 {
        let bytes = required(
            &ctx.conn,
            "SELECT (SELECT COALESCE(SUM(length(payload_ciphertext)),0) FROM task_flow_steps WHERE job_id=?)+(SELECT length(snapshot_ciphertext) FROM card_task_flows WHERE card_id=(SELECT card_id FROM jobs WHERE id=?))+(SELECT COALESCE(SUM(ciphertext_bytes),0) FROM task_flow_dispatches WHERE job_id=?) AS n",
            params![
                text(step, "job_id"),
                text(step, "job_id"),
                text(step, "job_id")
            ],
            "流程容量读取失败",
        )?;
        if integer(&bytes, "n", 0) + extra as i64 > 2 * 1024 * 1024 {
            return Err(ApiError::new(413, "任务流程文字存储达到上限，请使用附件"));
        }
        files::check_storage_quota(ctx, extra as u64, text(&s["product"], "id"))?;
    }
    ctx.conn.execute(
        "UPDATE task_flow_steps SET payload_ciphertext=? WHERE job_id=? AND flow_epoch=?",
        params![
            encrypted,
            text(step, "job_id"),
            integer(step, "flow_epoch", 0)
        ],
    )?;
    Ok(())
}
fn source(ctx: &Context, r: &Value, s: &Value, reference: &Value) -> Result<(Value, Value, Value)> {
    let (origin, _) = definition::reference_origin(&s["definition"], reference)?;
    let step=row(&ctx.conn,"SELECT * FROM task_flow_steps WHERE job_id=? AND attempt=? AND node_id=? AND state='completed' ORDER BY flow_epoch DESC LIMIT 1",params![text(r,"job_id"),integer(r,"attempt",1),text(&origin,"node")])?.ok_or_else(||ApiError::new(409,"任务流程引用的步骤尚未完成"))?;
    let p = payload(ctx, s, &step)?;
    let value = p[if text(&step, "kind") == "input" {
        "input"
    } else {
        "output"
    }]
    .get(text(&origin, "field"))
    .cloned()
    .ok_or_else(|| ApiError::new(409, "任务流程引用的数据已失效"))?;
    Ok((value, step, p))
}
fn references(ctx: &Context, r: &Value, s: &Value, mapping: &Value) -> Result<(Value, Value)> {
    let mut values = json!({});
    let mut sources = json!({});
    for (key, reference) in mapping
        .as_object()
        .ok_or_else(|| ApiError::new(409, "流程映射无效"))?
    {
        let (origin, _) = definition::reference_origin(&s["definition"], reference)?;
        let (value, step, _) = source(ctx, r, s, &origin)?;
        values[key] = value;
        sources[key] = json!({"node":step["node_id"],"field":origin["field"],"flow_epoch":step["flow_epoch"],"kind":if text(&step,"kind")=="input"{"input"}else{"output"}});
    }
    Ok((values, sources))
}
fn next(ctx: &Context, r: &Value, s: &Value, transition: &Value) -> Result<String> {
    if let Some(target) = transition.as_str() {
        return Ok(target.to_owned());
    }
    for case in transition["cases"]
        .as_array()
        .ok_or_else(|| ApiError::new(409, "流程分支无效"))?
    {
        let when = &case["when"];
        let op = text(when, "op");
        let value = match source(ctx, r, s, &when["source"]) {
            Ok((v, _, _)) => v,
            Err(e) if op == "exists" && e.status == 409 => continue,
            Err(e) => return Err(e),
        };
        if (op == "exists" && value != json!(""))
            || (op == "eq" && value == when["value"])
            || (op == "in" && when["value"].as_array().is_some_and(|a| a.contains(&value)))
        {
            return Ok(text(case, "to").to_owned());
        }
    }
    Ok(text(transition, "default").to_owned())
}
fn clear_sensitive(ctx: &Context, r: &Value, s: &Value, process: Option<&Value>) -> Result<()> {
    let nodes = if let Some(n) = process {
        let mut set = HashSet::new();
        for reference in n["inputs"].as_object().unwrap().values() {
            set.insert(
                text(
                    &definition::reference_origin(&s["definition"], reference)?.0,
                    "node",
                )
                .to_owned(),
            );
        }
        Some(set)
    } else {
        None
    };
    for step in rows(
        &ctx.conn,
        "SELECT * FROM task_flow_steps WHERE job_id=? AND attempt=? AND kind='input'",
        params![text(r, "job_id"), integer(r, "attempt", 1)],
    )? {
        if nodes
            .as_ref()
            .is_some_and(|set| !set.contains(text(&step, "node_id")))
        {
            continue;
        }
        let mut value = payload(ctx, s, &step)?;
        let n = node(s, text(&step, "node_id"))?;
        let mut changed = false;
        for field in n["fields"].as_array().unwrap() {
            if flag(field, "sensitive") {
                for key in ["input", "sensitive_until", "consumed_by"] {
                    if let Some(map) = value[key].as_object_mut() {
                        changed |= map.remove(text(field, "key")).is_some();
                    }
                }
            }
        }
        if changed {
            write_payload(ctx, s, &step, &value)?;
        }
    }
    Ok(())
}
fn sensitive_expiry(ctx: &Context, r: &Value, s: &Value, erase: bool) -> Result<Option<f64>> {
    let mut earliest: Option<f64> = None;
    for step in rows(
        &ctx.conn,
        "SELECT * FROM task_flow_steps WHERE job_id=? AND attempt=? AND kind='input'",
        params![text(r, "job_id"), integer(r, "attempt", 1)],
    )? {
        let n = node(s, text(&step, "node_id"))?;
        let mut v = payload(ctx, s, &step)?;
        let mut changed = false;
        for f in n["fields"].as_array().unwrap() {
            let key = text(f, "key");
            if !flag(f, "sensitive") || text(&v["input"], key).is_empty() {
                continue;
            }
            let expiry = v["sensitive_until"][key].as_f64().unwrap_or(now());
            if erase && expiry <= now() {
                for category in ["input", "sensitive_until", "consumed_by"] {
                    if let Some(m) = v[category].as_object_mut() {
                        m.remove(key);
                    }
                }
                changed = true;
                continue;
            }
            earliest = Some(earliest.map(|e| e.min(expiry)).unwrap_or(expiry));
        }
        if changed {
            write_payload(ctx, s, &step, &v)?;
        }
    }
    Ok(earliest)
}
fn inputs(
    ctx: &Context,
    r: &Value,
    s: &Value,
    n: &Value,
    consume: bool,
) -> Result<(Value, Option<f64>)> {
    let mut values = json!({});
    let mut earliest: Option<f64> = None;
    let mut writes = std::collections::BTreeMap::<i64, (Value, Value)>::new();
    for (key, reference) in n["inputs"].as_object().unwrap() {
        let (origin, f) = definition::reference_origin(&s["definition"], reference)?;
        let (v, step, mut p) = source(ctx, r, s, &origin)?;
        if flag(&f, "sensitive") && !(v == json!("") && !flag(&f, "required")) {
            let expiry = p["sensitive_until"][text(&origin, "field")]
                .as_f64()
                .ok_or_else(|| ApiError::new(409, "敏感输入已过期或已使用，请重新填写"))?;
            let used = p["consumed_by"][text(&origin, "field")].as_i64();
            if expiry <= now() || used.is_some_and(|e| e != integer(r, "flow_epoch", 0)) {
                return Err(ApiError::new(409, "敏感输入已过期或已使用，请重新填写"));
            }
            earliest = Some(earliest.map(|e| e.min(expiry)).unwrap_or(expiry));
            if consume && used.is_none() {
                if p["consumed_by"].is_null() {
                    p["consumed_by"] = json!({});
                }
                let entry = writes
                    .entry(integer(&step, "flow_epoch", 0))
                    .or_insert((step, p));
                entry.1["consumed_by"][text(&origin, "field")] = r["flow_epoch"].clone();
            }
        }
        values[key] = v;
    }
    for (_, (step, p)) in writes {
        write_payload(ctx, s, &step, &p)?;
    }
    Ok((values, earliest))
}
pub fn schemas(ctx: &Context, j: &Value, p: &mut Value) -> Result<()> {
    let Some(r) = row(
        &ctx.conn,
        "SELECT * FROM task_flow_runs WHERE job_id=?",
        [text(j, "id")],
    )?
    else {
        return Ok(());
    };
    if matches!(text(&r, "phase"), "queued" | "processing") {
        let s = snapshot(ctx, j)?;
        let n = node(&s, text(&r, "node_id"))?;
        let mut fields = Vec::new();
        for (key, reference) in n["inputs"].as_object().unwrap() {
            let (_, mut f) = definition::reference_origin(&s["definition"], reference)?;
            f["key"] = json!(key);
            fields.push(f);
        }
        p["parameters"] = json!(fields);
        p["outputs"] = n["outputs"].clone();
    }
    Ok(())
}
pub fn execution(ctx: &Context, j: &Value, consume: bool) -> Result<Option<Value>> {
    if !is_flow(ctx, j)? {
        return Ok(None);
    }
    let Some(r) = row(
        &ctx.conn,
        "SELECT * FROM task_flow_runs WHERE job_id=?",
        [text(j, "id")],
    )?
    else {
        return Ok(None);
    };
    if !matches!(text(&r, "phase"), "queued" | "processing") {
        return Ok(None);
    }
    let (j, r, s) = guard(ctx, j)?;
    if r["deadline"].as_f64().is_some_and(|v| v <= now()) {
        return Ok(None);
    }
    let n = node(&s, text(&r, "node_id"))?;
    if text(&n, "kind") != "process" {
        return Ok(None);
    }
    let st = step(ctx, &r, None)?;
    if st["attempt"] != j["attempt"] {
        return Ok(None);
    }
    let (params, expiry) = match inputs(ctx, &r, &s, &n, consume) {
        Ok(v) => v,
        Err(e) if e.status == 409 => return Ok(None),
        Err(e) => return Err(e),
    };
    let mut parameters = Vec::new();
    for (key, reference) in n["inputs"].as_object().unwrap() {
        let (_, mut f) = definition::reference_origin(&s["definition"], reference)?;
        f["key"] = json!(key);
        parameters.push(f);
    }
    let p = &s["product"];
    Ok(Some(
        json!({"job_id":j["id"],"card_id":j["card_id"],"product_id":j["product_id"],"shop_id":p["shop_id"],"attempt":j["attempt"],"flow_epoch":r["flow_epoch"],"node_id":n["id"],"action_id":st["action_id"],"mode":p["mode"],"params":params,"parameters":parameters,"outputs":n["outputs"],"deadline":r["deadline"],"input_expires_at":expiry,"processor_id":text(p,"processor_id"),"webhook_url":text(p,"webhook_url"),"webhook_secret":text(p,"webhook_secret")}),
    ))
}
pub fn frozen_authority(
    ctx: &Context,
    j: &Value,
    epoch: i64,
    attempt: Option<i64>,
) -> Result<Value> {
    let (j, r, s) = guard(ctx, j)?;
    let st = step(ctx, &r, Some(epoch))?;
    if epoch < 1
        || text(&st, "kind") != "process"
        || attempt.is_some_and(|a| a != integer(&st, "attempt", 0))
    {
        return Err(ApiError::new(409, "处理步骤不存在或不匹配"));
    }
    let p = &s["product"];
    Ok(
        json!({"job_id":j["id"],"card_id":j["card_id"],"product_id":j["product_id"],"shop_id":p["shop_id"],"attempt":st["attempt"],"flow_epoch":epoch,"node_id":st["node_id"],"action_id":st["action_id"],"mode":p["mode"],"webhook_url":text(p,"webhook_url"),"webhook_secret":text(p,"webhook_secret"),"deadline":st["deadline"],"step_state":st["state"]}),
    )
}
pub fn check_identity(ctx: &Context, j: &Value, scope: &Value) -> Result<()> {
    let (_, r, _) = guard(ctx, j)?;
    let st = step(ctx, &r, None)?;
    if !matches!(text(&r, "phase"), "queued" | "processing")
        || scope["attempt"].as_i64() != j["attempt"].as_i64()
        || scope["flow_epoch"].as_i64() != r["flow_epoch"].as_i64()
        || scope
            .get("action_id")
            .filter(|v| !v.is_null())
            .is_some_and(|v| *v != st["action_id"])
    {
        return Err(ApiError::new(409, "提交对应的流程步骤已失效"));
    }
    Ok(())
}
pub fn check_scope(ctx: &Context, j: &Value, scope: &Value, require_input: bool) -> Result<()> {
    check_identity(ctx, j, scope)?;
    let r = run(ctx, j)?;
    if r["deadline"].as_f64().is_some_and(|v| v <= now()) {
        return Err(ApiError::new(409, "提交对应的流程步骤已失效"));
    }
    if require_input && execution(ctx, j, false)?.is_none() {
        return Err(ApiError::new(409, "处理步骤输入已失效"));
    }
    Ok(())
}
pub fn expire_current(ctx: &Context, j: &Value, scope: &Value) -> Result<bool> {
    check_identity(ctx, j, scope)?;
    let (j, r, s) = guard(ctx, j)?;
    if r["deadline"].as_f64().is_some_and(|v| v <= now()) {
        engine::timeout(ctx, &j, &r, &s)?;
        Ok(true)
    } else {
        Ok(false)
    }
}
pub fn worker_authority(
    ctx: &Context,
    j: &Value,
    execution: &Value,
    require_input: bool,
) -> Result<()> {
    let actual = job(ctx, text(j, "id"))?;
    check_scope(ctx, &actual, execution, require_input)?;
    let frozen = frozen_authority(
        ctx,
        &actual,
        integer(execution, "flow_epoch", 0),
        Some(integer(execution, "attempt", 0)),
    )?;
    for key in [
        "shop_id",
        "product_id",
        "job_id",
        "attempt",
        "node_id",
        "flow_epoch",
        "action_id",
    ] {
        if execution[key] != frozen[key] {
            return Err(ApiError::new(409, "处理步骤执行身份已改变"));
        }
    }
    if text(&actual, "state") != "processing" {
        return Err(ApiError::new(409, "处理步骤未被领取"));
    }
    Ok(())
}
pub fn preview_flow(p: &Value) -> Value {
    let def = &p["task_flow"];
    let Some(n) = def["nodes"]
        .as_array()
        .and_then(|a| a.iter().find(|n| n["id"] == def["entry"]))
    else {
        return Value::Null;
    };
    let mut current = json!({});
    for k in ["id", "kind", "label", "prompt", "start_policy", "content"] {
        if let Some(v) = n.get(k) {
            current[k] = v.clone();
        }
    }
    json!({"enabled":true,"flow_epoch":0,"revision":0,"phase":"await_start","current":current,"deadline":null,"server_time":now(),"shown":[],"history":[],"actions":["start"]})
}
pub fn view(ctx: &Context, j: &Value, _staff: bool) -> Result<Value> {
    let s = snapshot(ctx, j)?;
    let Some(r) = row(
        &ctx.conn,
        "SELECT * FROM task_flow_runs WHERE job_id=?",
        [text(j, "id")],
    )?
    else {
        let mut p = s["product"].clone();
        p["task_flow"] = s["definition"].clone();
        let mut v = preview_flow(&p);
        v["definition_hash"] = s["definition_hash"].clone();
        v["attempt"] = json!(1);
        v["version"] = json!(1);
        return Ok(v);
    };
    let n = node(&s, text(&r, "node_id"))?;
    let mut current = json!({});
    for k in ["id", "kind", "label"] {
        if let Some(v) = n.get(k) {
            current[k] = v.clone();
        }
    }
    let mut actions = Vec::<&str>::new();
    let mut shown = Vec::new();
    match text(&r, "phase") {
        "await_start" => {
            current["prompt"] = n
                .get("prompt")
                .or_else(|| n.get("content"))
                .cloned()
                .unwrap_or(json!({}));
            current["start_policy"] = json!(
                n.get("start_policy")
                    .and_then(Value::as_str)
                    .unwrap_or("confirm")
            );
            actions.push("start");
        }
        "input" => {
            current["question"] = n["question"].clone();
            current["fields"] = n["fields"].clone();
            current["start_policy"] = n["start_policy"].clone();
            actions.push("answer");
        }
        "display" => {
            current["content"] = n["content"].clone();
            actions.push("continue");
        }
        _ => (),
    }
    if matches!(text(&r, "phase"), "input" | "display") {
        if let Some(mapping) = n["show_from"].as_object() {
            for (key, reference) in mapping {
                let (_, f) = definition::reference_origin(&s["definition"], reference)?;
                if flag(&f, "sensitive") || matches!(text(&f, "type"), "file" | "image" | "images")
                {
                    continue;
                }
                match source(ctx, &r, &s, reference) {
                    Ok((value, _, _)) => shown
                        .push(json!({"key":key,"label":f["label"],"type":f["type"],"value":value})),
                    Err(e) if e.status == 409 => (),
                    Err(e) => return Err(e),
                }
            }
        }
    }
    let history = rows(
        &ctx.conn,
        "SELECT node_id,flow_epoch,kind,state,entered_at,started_at,completed_at FROM task_flow_steps WHERE job_id=? ORDER BY flow_epoch LIMIT 256",
        [text(j, "id")],
    )?;
    Ok(
        json!({"enabled":true,"version":1,"definition_hash":s["definition_hash"],"attempt":r["attempt"],"flow_epoch":r["flow_epoch"],"revision":r["revision"],"phase":r["phase"],"current":current,"deadline":r["deadline"],"server_time":now(),"shown":shown,"history":history,"actions":actions}),
    )
}
pub fn input_file_scope(
    ctx: &Context,
    c: &Value,
    field: &str,
    epoch: Option<i64>,
    revision: Option<i64>,
    node_id: Option<&str>,
) -> Result<Option<(Value, Option<Value>)>> {
    if flow_card_snapshot(ctx, c)?.is_none() {
        return Ok(None);
    }
    let j = row(
        &ctx.conn,
        "SELECT * FROM jobs WHERE card_id=?",
        [text(c, "id")],
    )?
    .ok_or_else(|| ApiError::new(409, "请先开始任务流程"))?;
    let (_, r, s) = guard(ctx, &j)?;
    if text(&r, "phase") != "input"
        || epoch != r["flow_epoch"].as_i64()
        || revision != r["revision"].as_i64()
        || node_id != r["node_id"].as_str()
        || r["deadline"].as_f64().is_some_and(|x| x <= now())
    {
        return Err(ApiError::new(409, "上传所属步骤已改变或超时，请刷新"));
    }
    let n = node(&s, text(&r, "node_id"))?;
    if !n["fields"]
        .as_array()
        .unwrap()
        .iter()
        .any(|f| text(f, "key") == field && matches!(text(f, "type"), "file" | "image" | "images"))
    {
        return Err(ApiError::bad("当前输入步骤没有此附件字段"));
    }
    let mut p = job_product(ctx, &j)?;
    p["parameters"] = n["fields"].clone();
    Ok(Some((p, Some(j))))
}
pub fn output_file_scope(
    ctx: &Context,
    j: &Value,
    epoch: Option<i64>,
    action_id: Option<&str>,
) -> Result<Option<Value>> {
    if !is_flow(ctx, j)? {
        return Ok(None);
    }
    let (_, r, s) = guard(ctx, j)?;
    let mut execution =
        frozen_authority(ctx, j, epoch.unwrap_or(0), Some(integer(j, "attempt", 1)))?;
    if text(&r, "phase") != "processing"
        || text(j, "state") != "processing"
        || epoch != r["flow_epoch"].as_i64()
        || action_id.is_some_and(|a| a != text(&execution, "action_id"))
        || r["deadline"].as_f64().is_some_and(|v| v <= now())
    {
        return Err(ApiError::new(409, "上传所属处理步骤已改变或超时"));
    }
    execution["outputs"] = node(&s, text(&r, "node_id"))?["outputs"].clone();
    Ok(Some(execution))
}
pub fn stage_values(ctx: &Context, j: &Value, epoch: i64, kind: &str) -> Result<Value> {
    let (_, r, s) = guard(ctx, j)?;
    let st = step(ctx, &r, Some(epoch))?;
    if st["attempt"] != j["attempt"] || !["input", "output"].contains(&kind) {
        return Err(ApiError::new(409, "附件步骤轮次已失效"));
    }
    let n = node(&s, text(&st, "node_id"))?;
    Ok(
        json!({"node_id":st["node_id"],"flow_epoch":epoch,"kind":kind,"fields":n[if kind=="input"{"fields"}else{"outputs"}],"values":payload(ctx,&s,&st)?.get(kind).cloned().unwrap_or(json!({}))}),
    )
}

pub fn private_worker_file_scope(
    ctx: &Context,
    j: &Value,
    execution_: &Value,
    field_key: &str,
    kind: &str,
    file_id: Option<&str>,
) -> Result<Value> {
    if kind == "output" {
        let current = output_file_scope(
            ctx,
            j,
            execution_["flow_epoch"].as_i64(),
            execution_["action_id"].as_str(),
        )?
        .ok_or_else(|| ApiError::new(409, "附件输出的处理动作已改变"))?;
        if current["attempt"] != execution_["attempt"]
            || current["node_id"] != execution_["node_id"]
        {
            return Err(ApiError::new(409, "附件输出的处理动作已改变"));
        }
        return current["outputs"]
            .as_array()
            .and_then(|a| {
                a.iter().find(|f| {
                    text(f, "key") == field_key
                        && matches!(text(f, "type"), "file" | "image" | "images")
                })
            })
            .cloned()
            .ok_or_else(|| ApiError::bad("当前处理步骤没有这个附件输出字段"));
    }
    if kind != "input" || file_id.is_none() {
        return Err(ApiError::bad("附件请求无效"));
    }
    let current = execution(ctx, j, false)?
        .ok_or_else(|| ApiError::new(409, "附件输入的处理步骤已改变或过期"))?;
    for key in [
        "product_id",
        "job_id",
        "attempt",
        "node_id",
        "flow_epoch",
        "action_id",
    ] {
        if current[key] != execution_[key] {
            return Err(ApiError::new(409, "附件输入的处理步骤已改变或过期"));
        }
    }
    let field = current["parameters"]
        .as_array()
        .and_then(|a| {
            a.iter().find(|f| {
                text(f, "key") == field_key
                    && matches!(text(f, "type"), "file" | "image" | "images")
            })
        })
        .cloned()
        .ok_or_else(|| ApiError::new(403, "文件不是当前处理步骤授权的输入"))?;
    let fid = file_id.unwrap();
    if !entitlements::attachment_ids(&field, text(&current["params"], field_key))
        .iter()
        .any(|id| id == fid)
    {
        return Err(ApiError::new(403, "文件不是当前处理步骤授权的输入"));
    }
    let f = required(
        &ctx.conn,
        "SELECT * FROM job_files WHERE id=?",
        [fid],
        "文件不存在",
    )?;
    if text(&f, "job_id") != text(j, "id")
        || f["card_id"] != j["card_id"]
        || f["product_id"] != j["product_id"]
        || f["attempt"] != j["attempt"]
        || !flag(&f, "bound")
        || f["content"].is_null()
        || flag(&f, "consumed")
    {
        return Err(ApiError::new(403, "文件不属于本次任务"));
    }
    Ok(field)
}
pub fn bind_private_worker_file(
    ctx: &Context,
    j: &Value,
    execution: &Value,
    field_key: &str,
    fid: &str,
) -> Result<()> {
    private_worker_file_scope(ctx, j, execution, field_key, "output", None)?;
    ctx.conn.execute("INSERT INTO task_flow_files(file_id,job_id,node_id,flow_epoch,attempt,kind) VALUES (?,?,?,?,?,'output')",params![fid,text(j,"id"),text(execution,"node_id"),integer(execution,"flow_epoch",0),integer(j,"attempt",1)])?;
    Ok(())
}

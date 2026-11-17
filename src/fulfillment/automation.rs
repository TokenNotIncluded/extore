//! Signed multi-product queue claims and bounded recovery receipts.
use super::*;
use std::{
    collections::BTreeMap,
    time::{Duration, Instant},
};

type Bindings = BTreeMap<String, Value>;

fn bounded_id(value: &Value, min: usize, max: usize) -> bool {
    value.as_str().is_some_and(|s| {
        (min..=max).contains(&s.len())
            && s.bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-'))
    })
}

fn validate(input: &Value) -> Result<Value> {
    let bad = || ApiError::new(422, "领取参数无效");
    let allowed = ["request_id", "issued_at", "wait_seconds", "limit", "grants"];
    if input
        .as_object()
        .is_none_or(|o| o.keys().any(|k| !allowed.contains(&k.as_str())))
        || !bounded_id(&input["request_id"], 16, 100)
        || input["issued_at"].as_i64().is_none()
    {
        return Err(bad());
    }
    let wait = input
        .get("wait_seconds")
        .unwrap_or(&json!(25))
        .as_i64()
        .filter(|v| (0..=25).contains(v))
        .ok_or_else(bad)?;
    let limit = input
        .get("limit")
        .unwrap_or(&json!(1))
        .as_i64()
        .filter(|v| (1..=10).contains(v))
        .ok_or_else(bad)?;
    let grants = input["grants"]
        .as_array()
        .filter(|a| (1..=500).contains(&a.len()))
        .ok_or_else(bad)?;
    let mut devices = HashSet::new();
    let mut products = HashSet::new();
    for grant in grants {
        if grant.as_object().is_none_or(|o| {
            o.len() != 3
                || o.keys()
                    .any(|k| !["device_id", "product_id", "signature"].contains(&k.as_str()))
        }) || !bounded_id(&grant["device_id"], 1, 100)
            || !bounded_id(&grant["product_id"], 1, 100)
            || grant["signature"].as_str().is_none_or(|s| s.len() != 86)
            || !devices.insert(text(grant, "device_id"))
            || !products.insert(text(grant, "product_id"))
        {
            return Err(bad());
        }
    }
    Ok(
        json!({"request_id":input["request_id"],"issued_at":input["issued_at"],"wait_seconds":wait,"limit":limit,"grants":grants}),
    )
}

fn canonical(body: &Value) -> Result<String> {
    let mut grants = body["grants"]
        .as_array()
        .unwrap()
        .iter()
        .map(|g| json!({"device_id":g["device_id"],"product_id":g["product_id"]}))
        .collect::<Vec<_>>();
    grants.sort_by(|a, b| {
        (text(a, "device_id"), text(a, "product_id"))
            .cmp(&(text(b, "device_id"), text(b, "product_id")))
    });
    auth::cli::ascii_json(
        &json!({"request_id":body["request_id"],"issued_at":body["issued_at"],"wait_seconds":body["wait_seconds"],"limit":body["limit"],"grants":grants}),
    )
}

fn authorization(ctx: &Context, body: &Value) -> Result<(Value, Bindings)> {
    // Every poll reloads the bearer, device, all parent links, pipeline bindings,
    // and current shop permissions. A revoked grant cannot finish a pending wait.
    if !ctx.headers.contains_key("authorization") {
        return Err(ApiError::new(401, "请使用 CLI 登录凭证"));
    }
    let s = auth::session(ctx, &["admin", "staff"], Some("queue.process"))?;
    if text(&s, "channel") != "cli" || text(&s, "role") != "staff" {
        return Err(ApiError::new(403, "请使用商品处理 CLI 设备授权"));
    }
    let grants = body["grants"].as_array().unwrap();
    if !grants.iter().any(|g| g["device_id"] == s["device_id"]) {
        return Err(ApiError::new(403, "登录设备不在本次领取范围内"));
    }
    let canonical = canonical(body)?;
    let mut bindings = Bindings::new();
    for grant in grants {
        let did = text(grant, "device_id");
        let pid = text(grant, "product_id");
        let (device, staff) = auth::cli::device(ctx, did)?;
        let proof = format!(
            "extore-automation-next-v1\n{}\nPOST\n/api/manage/next\n{did}\n{canonical}",
            ctx.app.origin
        );
        auth::cli::verify(
            text(&device, "public_key"),
            text(grant, "signature"),
            &proof,
        )?;
        if text(&staff, "product_id") != pid {
            return Err(ApiError::new(403, "设备授权不属于此商品"));
        }
        if !["queue.view", "queue.process"].iter().all(|p| {
            staff["permissions"]
                .as_array()
                .is_some_and(|a| a.iter().any(|v| v.as_str() == Some(*p)))
        }) {
            return Err(ApiError::new(403, "设备缺少查看和处理队列权限"));
        }
        require_enabled(ctx, pid)?;
        if text(&catalog::product(ctx, pid)?, "mode") != "manual" {
            return Err(ApiError::new(409, "此商品不是可领取的处理队列"));
        }
        bindings.insert(pid.to_owned(), json!({"device_id":did,"actor":staff["id"]}));
    }
    Ok((s, bindings))
}

fn phase(ctx: &Context, j: &Value, expected: &str) -> Result<bool> {
    Ok(row(
        &ctx.conn,
        "SELECT phase FROM task_flow_runs WHERE job_id=? AND attempt=?",
        params![text(j, "id"), integer(j, "attempt", 0)],
    )?
    .is_some_and(|r| text(&r, "phase") == expected))
}

fn work_item(ctx: &Context, j: &Value, binding: &Value) -> Result<Option<Value>> {
    let is_flow = flow::is_flow(ctx, j)?;
    if is_flow && !phase(ctx, j, "processing")? {
        return Ok(None);
    }
    let execution = if is_flow {
        flow::execution(ctx, j, true)?
    } else {
        None
    };
    if is_flow && execution.is_none() {
        return Ok(None);
    }
    let details = job_view_with_history(ctx, j, !is_flow, false)?;
    let mut execution = if let Some(value) = execution {
        let mut result = json!({});
        for key in [
            "params",
            "parameters",
            "outputs",
            "flow_epoch",
            "node_id",
            "action_id",
            "deadline",
            "attempt",
            "mode",
        ] {
            if let Some(value) = value.get(key) {
                result[key] = value.clone();
            }
        }
        result
    } else {
        json!({"params":details["params"],"parameters":details["parameters"],"outputs":details["outputs"],"flow_epoch":null,"action_id":null})
    };
    merge(&mut execution, entitlements::job_context(ctx, j, false)?);
    let mut safe_job = json!({});
    for key in [
        "id",
        "attempt",
        "state",
        "product_name",
        "variant",
        "message",
        "progress",
        "steps",
        "completed_steps",
        "card_attributes",
        "entitlements",
        "revision",
        "deliveries",
        "last_delivery",
    ] {
        if let Some(value) = details.get(key) {
            safe_job[key] = value.clone();
        }
    }
    Ok(Some(
        json!({"product_id":j["product_id"],"device_id":binding["device_id"],"instructions":instructions(ctx,text(j,"product_id"))?,"job":safe_job,"execution":execution}),
    ))
}

fn claim(ctx: &Context, j: &Value, binding: &Value) -> Result<Option<Value>> {
    let who = text(binding, "actor");
    if flow::is_flow(ctx, j)? {
        // An inconsistent phase or expired secret never falls back to the
        // classic queue path and never exposes inputs through a receipt replay.
        if !phase(ctx, j, "queued")? {
            return Ok(None);
        }
        let Some(execution) = flow::execution(ctx, j, false)? else {
            return Ok(None);
        };
        if number(&execution, "deadline") <= now() {
            return Ok(None);
        }
        flow::claim(ctx, j, who, integer(&execution, "flow_epoch", 0))?;
    } else {
        if ctx.conn.execute("UPDATE jobs SET claimed_by=? WHERE id=? AND state='queued' AND attempt=? AND claimed_by IS NULL",params![who,text(j,"id"),integer(j,"attempt",0)])?!=1 {return Ok(None);}
        apply_update(
            ctx,
            text(j, "id"),
            &json!({"state":"processing","attempt":j["attempt"],"message":"正在处理"}),
        )?;
    }
    let current = job(ctx, text(j, "id"))?;
    if text(&current, "state") != "processing" || text(&current, "claimed_by") != who {
        return Ok(None);
    }
    record_claim(ctx, &current, who, None, Some(text(binding, "device_id")))?;
    let Some(item) = work_item(ctx, &current, binding)? else {
        return Ok(None);
    };
    audit(&ctx.conn, who, "queue.next.claim", text(j, "id"), None)?;
    Ok(Some(item))
}

fn identity(item: &Value) -> Value {
    json!({"job_id":item["job"]["id"],"product_id":item["product_id"],"device_id":item["device_id"],"attempt":item["job"]["attempt"],"flow_epoch":item["execution"]["flow_epoch"],"action_id":item["execution"]["action_id"]})
}

fn receipt(ctx: &Context, record: &Value, body: &Value, bindings: &Bindings) -> Result<Value> {
    let identities = json_field(record, "items", json!([]));
    let mut items = Vec::new();
    let mut stale = false;
    for saved in identities
        .as_array()
        .ok_or_else(|| ApiError::new(409, "领取记录无效"))?
    {
        let binding = bindings.get(text(saved, "product_id"));
        let current = row(
            &ctx.conn,
            "SELECT * FROM jobs WHERE id=?",
            [text(saved, "job_id")],
        )?;
        let (Some(binding), Some(current)) = (binding, current) else {
            stale = true;
            continue;
        };
        if binding["device_id"] != saved["device_id"]
            || current["product_id"] != saved["product_id"]
            || current["attempt"] != saved["attempt"]
            || text(&current, "state") != "processing"
            || text(&current, "claimed_by") != text(binding, "actor")
            || number(&current, "lease") <= now()
        {
            stale = true;
            continue;
        }
        let Some(item) = work_item(ctx, &current, binding)? else {
            stale = true;
            continue;
        };
        if identity(&item) != *saved
            || item["execution"]["deadline"]
                .as_f64()
                .is_some_and(|d| d <= now())
        {
            stale = true;
            continue;
        }
        items.push(item);
    }
    Ok(
        json!({"idle":items.is_empty(),"items":items,"request_id":body["request_id"],"replayed":true,"stale":stale}),
    )
}

fn cleanup(ctx: &Context, clock: f64) -> Result<()> {
    ctx.conn.execute("DELETE FROM automation_requests WHERE id IN (SELECT id FROM automation_requests WHERE expires<=? LIMIT 200)",[clock])?;
    Ok(())
}

fn tick(ctx: &Context, body: &Value, force_idle: bool) -> Result<(Option<Value>, f64)> {
    let (session, bindings) = authorization(ctx, body)?;
    let clock = now();
    let mut devices = body["grants"]
        .as_array()
        .unwrap()
        .iter()
        .map(|g| text(g, "device_id"))
        .collect::<Vec<_>>();
    devices.sort();
    let key = digest(&format!(
        "{}:{}",
        devices.join(","),
        text(body, "request_id")
    ));
    let body_hash = digest(&canonical(body)?);
    let existing = row(
        &ctx.conn,
        "SELECT * FROM automation_requests WHERE id=?",
        [&key],
    )?;
    if let Some(record) = &existing {
        if number(record, "expires") <= clock {
            return Err(ApiError::new(410, "领取请求已过期，请使用新请求"));
        }
        if text(record, "body_hash") != body_hash {
            return Err(ApiError::new(409, "相同领取请求不能修改范围或参数"));
        }
    }
    let existed = existing.is_some();
    let record = if let Some(record) = existing {
        record
    } else {
        let issued = integer(body, "issued_at", 0) as f64;
        if issued < clock - 120.0 || issued > clock + 30.0 {
            return Err(ApiError::new(401, "领取签名已过期"));
        }
        cleanup(ctx, clock)?;
        let total = required(
            &ctx.conn,
            "SELECT count(*) AS n FROM automation_requests",
            [],
            "领取记录不可用",
        )?;
        let own = required(
            &ctx.conn,
            "SELECT count(*) AS n,COALESCE(sum(state='pending' AND deadline>?),0) AS pending FROM automation_requests WHERE device_id=? AND expires>?",
            params![clock, text(&session, "device_id"), clock],
            "领取记录不可用",
        )?;
        if integer(&total, "n", 0) >= 10000
            || integer(&own, "n", 0) >= 100
            || integer(&own, "pending", 0) >= 3
        {
            return Err(ApiError::new(429, "领取请求过多，请稍后重试"));
        }
        ctx.conn.execute("INSERT INTO automation_requests(id,device_id,body_hash,state,created,deadline,expires,items) VALUES (?,?,?,'pending',?,?,?,'[]')",params![key,text(&session,"device_id"),body_hash,clock,clock+integer(body,"wait_seconds",0) as f64,clock+600.0])?;
        required(
            &ctx.conn,
            "SELECT * FROM automation_requests WHERE id=?",
            [&key],
            "领取记录不可用",
        )?
    };
    if text(&record, "state") == "done" {
        return Ok((Some(receipt(ctx, &record, body, &bindings)?), 0.0));
    }
    if existed && clock >= number(&record, "deadline") {
        ctx.conn.execute(
            "UPDATE automation_requests SET state='done' WHERE id=?",
            [&key],
        )?;
        return Ok((
            Some(
                json!({"items":[],"idle":true,"request_id":body["request_id"],"replayed":true,"stale":false}),
            ),
            0.0,
        ));
    }
    let placeholders = std::iter::repeat_n("?", bindings.len())
        .collect::<Vec<_>>()
        .join(",");
    let candidates = rows(
        &ctx.conn,
        &format!(
            "SELECT * FROM jobs WHERE state='queued' AND claimed_by IS NULL AND product_id IN ({placeholders}) ORDER BY created,id LIMIT 500"
        ),
        rusqlite::params_from_iter(bindings.keys()),
    )?;
    let mut items = Vec::new();
    for candidate in candidates {
        if let Some(item) = claim(ctx, &candidate, &bindings[text(&candidate, "product_id")])? {
            items.push(item);
            if items.len() == integer(body, "limit", 1) as usize {
                break;
            }
        }
    }
    if !items.is_empty() || clock >= number(&record, "deadline") || force_idle {
        ctx.conn.execute(
            "UPDATE automation_requests SET state='done',items=? WHERE id=?",
            params![
                json!(items.iter().map(identity).collect::<Vec<_>>()).to_string(),
                key
            ],
        )?;
        return Ok((
            Some(
                json!({"idle":items.is_empty(),"items":items,"request_id":body["request_id"],"replayed":false,"stale":false}),
            ),
            0.0,
        ));
    }
    Ok((None, (number(&record, "deadline") - now()).max(0.0)))
}

pub fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    if !ctx.is("POST", "/api/manage/next") {
        return Ok(None);
    }
    if !ctx.headers.contains_key("authorization") {
        return Err(ApiError::new(401, "请使用 CLI 登录凭证"));
    }
    let body = validate(&ctx.body)?;
    let stop = Instant::now() + Duration::from_secs(integer(&body, "wait_seconds", 25) as u64);
    loop {
        let remaining = stop.saturating_duration_since(Instant::now());
        let (result, database_remaining) = tick(ctx, &body, remaining.is_zero())?;
        if let Some(value) = result {
            return Ok(Some(Reply::json(value)));
        }
        let wait = remaining
            .min(Duration::from_secs_f64(database_remaining))
            .min(Duration::from_millis(500));
        ctx.conn.execute_batch("COMMIT")?;
        if !wait.is_zero() {
            std::thread::sleep(wait);
        }
        // The HTTP dispatcher owns the final commit/rollback; every loop exits
        // with a live transaction and each sleep occurs after releasing its lock.
        ctx.conn.execute_batch("BEGIN IMMEDIATE")?;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn canonical_is_stable_sorted_and_excludes_signatures() {
        let input = json!({"request_id":"abcdefghijklmnop","issued_at":123,"wait_seconds":0,"limit":2,"grants":[{"device_id":"b","product_id":"second","signature":"b".repeat(86)},{"device_id":"a","product_id":"first","signature":"a".repeat(86)}]});
        assert_eq!(
            canonical(&validate(&input).unwrap()).unwrap(),
            r#"{"grants":[{"device_id":"a","product_id":"first"},{"device_id":"b","product_id":"second"}],"issued_at":123,"limit":2,"request_id":"abcdefghijklmnop","wait_seconds":0}"#
        );
        let mut invalid = input.clone();
        invalid["grants"][1]["device_id"] = json!("b");
        assert_eq!(validate(&invalid).unwrap_err().status, 422);
        let mut invalid = input.clone();
        invalid["issued_at"] = json!(true);
        assert_eq!(validate(&invalid).unwrap_err().status, 422);
        let mut invalid = input.clone();
        invalid["authority"] = json!("all-products");
        assert_eq!(validate(&invalid).unwrap_err().status, 422);
    }
}

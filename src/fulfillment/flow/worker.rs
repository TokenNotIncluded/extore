//! Encrypted durable dispatches and one action per native worker iteration.
use super::*;
const PRIVATE_FIELDS: &[&str] = &[
    "shop_id",
    "product_id",
    "job_id",
    "attempt",
    "node_id",
    "flow_epoch",
    "action_id",
    "mode",
    "webhook_url",
    "webhook_secret",
    "params",
    "parameters",
    "outputs",
    "deadline",
    "input_expires_at",
];
fn context(execution: &Value) -> Value {
    let mut v = json!({});
    for key in PRIVATE_FIELDS {
        v[*key] = execution[*key].clone();
    }
    v
}
fn finish_dispatch(
    ctx: &Context,
    d: &Value,
    state: &str,
    attempts: i64,
    error: &str,
) -> Result<()> {
    ctx.conn.execute("UPDATE task_flow_dispatches SET state=?,payload_ciphertext='',ciphertext_bytes=0,attempts=?,error=?,updated=?,finished_at=? WHERE id=?",params![state,attempts,error,now(),now(),text(d,"id")])?;
    Ok(())
}
pub(super) fn cancel_dispatches(ctx: &Context, jid: &str, keep: Option<&str>) -> Result<()> {
    for d in rows(
        &ctx.conn,
        "SELECT * FROM task_flow_dispatches WHERE job_id=? AND state IN ('pending','sending')",
        [jid],
    )? {
        if keep.is_none_or(|id| id != text(&d, "action_id")) {
            finish_dispatch(ctx, &d, "cancelled", integer(&d, "attempts", 0), "")?;
        }
    }
    Ok(())
}
fn active(ctx: &Context, j: &Value) -> Result<Option<Value>> {
    let execution = match execution(ctx, j, false) {
        Ok(Some(v)) => v,
        Ok(None) => return Ok(None),
        Err(e) if matches!(e.status, 401 | 403 | 409 | 410 | 404) => return Ok(None),
        Err(e) => return Err(e),
    };
    if text(&execution, "mode") != "webhook"
        || ["deadline", "input_expires_at"]
            .iter()
            .any(|k| execution[*k].as_f64().is_some_and(|v| v <= now()))
    {
        return Ok(None);
    }
    let p = catalog::product(ctx, text(j, "product_id"))?;
    if ["mode", "webhook_url", "webhook_secret"]
        .iter()
        .any(|k| p[*k] != execution[*k])
    {
        return Ok(None);
    }
    Ok(Some(execution))
}
pub(super) fn sync_dispatch(ctx: &Context, j: &Value) -> Result<()> {
    let Some(execution) = active(ctx, j)? else {
        return cancel_dispatches(ctx, text(j, "id"), None);
    };
    cancel_dispatches(ctx, text(j, "id"), Some(text(&execution, "action_id")))?;
    if row(
        &ctx.conn,
        "SELECT 1 FROM task_flow_dispatches WHERE action_id=?",
        [text(&execution, "action_id")],
    )?
    .is_some()
    {
        return Ok(());
    }
    let private = context(&execution);
    if private.to_string().len() > 200000 {
        return Err(ApiError::new(413, "流程回调内容过大，请使用附件"));
    }
    let did = id();
    let shop = text(&execution, "shop_id");
    let hash = fingerprint(ctx, &private, shop, &did)?;
    let ciphertext = store_secret(&ctx.app, Some(shop), "task-flow-dispatch", &did, &private)?;
    let size = ciphertext.len() as i64;
    let used = required(
        &ctx.conn,
        "SELECT (SELECT COALESCE(SUM(length(snapshot_ciphertext)),0) FROM card_task_flows WHERE card_id=?)+(SELECT COALESCE(SUM(length(t.payload_ciphertext)),0) FROM task_flow_steps t JOIN jobs j ON j.id=t.job_id WHERE j.card_id=?)+(SELECT COALESCE(SUM(d.ciphertext_bytes),0) FROM task_flow_dispatches d JOIN jobs j ON j.id=d.job_id WHERE j.card_id=?) AS n",
        params![text(j, "card_id"), text(j, "card_id"), text(j, "card_id")],
        "流程容量读取失败",
    )?;
    if integer(&used, "n", 0) + size > 2 * 1024 * 1024 {
        return Err(ApiError::new(
            413,
            "此卡密的流程存储额度已满，请减少文本并使用附件",
        ));
    }
    files::check_storage_quota(ctx, size as u64, text(j, "product_id"))?;
    ctx.conn.execute("INSERT INTO task_flow_dispatches(id,job_id,product_id,shop_id,attempt,flow_epoch,node_id,action_id,state,payload_ciphertext,ciphertext_bytes,payload_digest,deadline,input_expires_at,due,created,updated) VALUES (?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,?)",params![did,text(j,"id"),text(j,"product_id"),shop,integer(&execution,"attempt",1),integer(&execution,"flow_epoch",0),text(&execution,"node_id"),text(&execution,"action_id"),ciphertext,size,hash,execution["deadline"].as_f64(),execution["input_expires_at"].as_f64(),now(),now(),now()])?;
    Ok(())
}
fn validated_context(ctx: &Context, d: &Value, state: Option<&str>) -> Result<Value> {
    if state.is_some_and(|s| s != text(d, "state")) || text(d, "payload_ciphertext").is_empty() {
        return Err(ApiError::new(409, "流程回调已失效"));
    }
    let j = job(ctx, text(d, "job_id"))?;
    let execution = active(ctx, &j)?.ok_or_else(|| ApiError::new(409, "流程回调已失效"))?;
    let mut private = open_secret(
        &ctx.app,
        Some(text(d, "shop_id")),
        "task-flow-dispatch",
        text(d, "id"),
        text(d, "payload_ciphertext"),
    )?;
    if private != context(&execution)
        || [
            "shop_id",
            "product_id",
            "job_id",
            "attempt",
            "node_id",
            "flow_epoch",
            "action_id",
            "deadline",
            "input_expires_at",
        ]
        .iter()
        .any(|k| private[*k] != d[*k])
    {
        return Err(ApiError::new(409, "流程回调已失效"));
    }
    let hash = fingerprint(ctx, &private, text(d, "shop_id"), text(d, "id"))?;
    if !bool::from(hash.as_bytes().ct_eq(text(d, "payload_digest").as_bytes())) {
        return Err(ApiError::new(409, "流程回调已失效"));
    }
    private["instructions"] = instructions(ctx, text(d, "product_id"))?;
    merge(&mut private, entitlements::job_context(ctx, &j, false)?);
    Ok(private)
}
fn cancel_stale(ctx: &Context) -> Result<bool> {
    let list = rows(
        &ctx.conn,
        "SELECT * FROM task_flow_dispatches WHERE state IN ('pending','sending') ORDER BY CASE WHEN (deadline IS NOT NULL AND deadline<=?) OR (input_expires_at IS NOT NULL AND input_expires_at<=?) THEN 0 ELSE 1 END,updated,id LIMIT 100",
        params![now(), now()],
    )?;
    let mut changed = false;
    for d in list {
        if validated_context(ctx, &d, None).is_err() {
            finish_dispatch(ctx, &d, "cancelled", integer(&d, "attempts", 0), "")?;
            changed = true;
        } else {
            ctx.conn.execute(
                "UPDATE task_flow_dispatches SET updated=? WHERE id=?",
                params![now(), text(&d, "id")],
            )?;
        }
    }
    Ok(changed)
}
fn outbox_once(app: Arc<App>) -> Result<bool> {
    let selected = crate::worker::transaction(app.clone(), |ctx| {
        let Some(d) = row(
            &ctx.conn,
            "SELECT * FROM task_flow_dispatches WHERE state IN ('pending','sending') AND due<=? ORDER BY due,id LIMIT 1",
            [now()],
        )?
        else {
            return Ok(None);
        };
        if integer(&d, "attempts", 0) >= 8 {
            finish_dispatch(
                ctx,
                &d,
                "dead",
                integer(&d, "attempts", 0),
                "delivery_failed",
            )?;
            return Ok(Some((d, None)));
        }
        let private = match validated_context(ctx, &d, None) {
            Ok(v) => v,
            Err(_) => {
                finish_dispatch(ctx, &d, "cancelled", integer(&d, "attempts", 0), "")?;
                return Ok(Some((d, None)));
            }
        };
        ctx.conn.execute(
            "UPDATE task_flow_dispatches SET state='sending',due=?,updated=? WHERE id=?",
            params![now() + 60.0, now(), text(&d, "id")],
        )?;
        Ok(Some((d, Some(private))))
    })?;
    let Some((selected, private)) = selected else {
        return Ok(false);
    };
    let Some(private) = private else {
        return Ok(true);
    };
    let send = crate::fulfillment::private_worker::deliver_v2(&app, &private, || {
        let ctx = Context::background(app.clone())?;
        let d = required(
            &ctx.conn,
            "SELECT * FROM task_flow_dispatches WHERE id=?",
            [text(&selected, "id")],
            "流程回调已失效",
        )?;
        validated_context(&ctx, &d, Some("sending")).map(|_| ())
    });
    crate::worker::transaction(app, |ctx| {
        let Some(d) = row(
            &ctx.conn,
            "SELECT * FROM task_flow_dispatches WHERE id=?",
            [text(&selected, "id")],
        )?
        else {
            return Ok(());
        };
        if text(&d, "state") != "sending" {
            return Ok(());
        }
        let attempts = integer(&d, "attempts", 0) + 1;
        if send.is_ok() {
            finish_dispatch(ctx, &d, "sent", attempts, "")?;
        } else if validated_context(ctx, &d, Some("sending")).is_err() {
            finish_dispatch(ctx, &d, "cancelled", attempts, "")?;
        } else if attempts >= 8 {
            finish_dispatch(ctx, &d, "dead", attempts, "delivery_failed")?;
        } else {
            ctx.conn.execute("UPDATE task_flow_dispatches SET state='pending',attempts=?,due=?,updated=?,error='delivery_failed' WHERE id=?",params![attempts,now()+(2_i64.pow(attempts as u32)*5).min(3600) as f64,now(),text(&d,"id")])?;
        }
        Ok(())
    })?;
    Ok(true)
}
pub fn worker_once(app: Arc<App>) -> Result<bool> {
    let (busy, selected) = crate::worker::transaction(app.clone(), |ctx| {
        let mut busy = cancel_stale(ctx)?;
        busy |= engine::expire_due(ctx)?;
        let mut selected = None;
        let list = rows(
            &ctx.conn,
            "SELECT j.* FROM jobs j JOIN task_flow_runs r ON r.job_id=j.id WHERE r.phase IN ('queued','processing') AND j.state IN ('queued','processing') ORDER BY CASE WHEN j.state='queued' AND json_extract((SELECT config FROM products WHERE id=j.product_id),'$.mode')='script' THEN 0 ELSE 1 END,j.created,j.id LIMIT 100",
            [],
        )?;
        for j in list {
            let execution = match execution(ctx, &j, false) {
                Ok(Some(v)) => v,
                Ok(None) => {
                    cancel_dispatches(ctx, text(&j, "id"), None)?;
                    continue;
                }
                Err(e) if matches!(e.status, 401 | 403 | 404 | 409 | 410) => {
                    cancel_dispatches(ctx, text(&j, "id"), None)?;
                    continue;
                }
                Err(e) => return Err(e),
            };
            if text(&execution, "mode") == "webhook" {
                sync_dispatch(ctx, &j)?;
                continue;
            }
            if text(&execution, "mode") != "script"
                || text(&j, "state") != "queued"
                || selected.is_some()
            {
                continue;
            }
            let current = engine::claim(ctx, &j, "worker", integer(&execution, "flow_epoch", 0))?;
            busy = true;
            let Some(fresh) = super::execution(ctx, &current, true)? else {
                continue;
            };
            if fresh["flow_epoch"] != execution["flow_epoch"]
                || text(&current, "state") != "processing"
                || text(&current, "claimed_by") != "worker"
            {
                continue;
            }
            record_claim(ctx, &current, "worker", None, None)?;
            let p = snapshot(ctx, &current)?["product"].clone();
            selected = Some((current, p, fresh));
        }
        Ok((busy, selected))
    })?;
    if let Some((j, p, execution)) = selected {
        let mut result=crate::worker::execute_script(app.clone(),&j,&p,Some(&execution)).unwrap_or_else(|_|json!({"state":"failed","retryable":false,"message":"处理器未正常完成，待商家核实是否已交付"}));
        for key in ["attempt", "flow_epoch", "action_id"] {
            result[key] = execution[key].clone();
        }
        crate::worker::transaction(app.clone(), |ctx| {
            let current = job(ctx, text(&j, "id"))?;
            if worker_authority(ctx, &current, &execution, false).is_err()
                || text(&current, "claimed_by") != "worker"
            {
                return Ok(());
            }
            ctx.conn.execute_batch("SAVEPOINT flow_script_result")?;
            match engine::process_update(ctx, &current, &result) {
                Ok(_) => ctx
                    .conn
                    .execute_batch("RELEASE SAVEPOINT flow_script_result")?,
                Err(_) => {
                    ctx.conn.execute_batch("ROLLBACK TO SAVEPOINT flow_script_result; RELEASE SAVEPOINT flow_script_result")?;
                    let current = job(ctx, text(&j, "id"))?;
                    if worker_authority(ctx, &current, &execution, false).is_ok() {
                        engine::process_update(
                            ctx,
                            &current,
                            &json!({"state":"failed","attempt":execution["attempt"],"flow_epoch":execution["flow_epoch"],"action_id":execution["action_id"],"retryable":false,"message":"处理器返回的结果不符合步骤定义，待商家核实"}),
                        )?;
                    }
                }
            }
            Ok(())
        })?;
        return Ok(true);
    }
    Ok(outbox_once(app)? || busy)
}

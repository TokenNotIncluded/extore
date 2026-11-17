use super::*;
fn expect(r: &Value, epoch: i64, revision: Option<i64>) -> Result<()> {
    if epoch != integer(r, "flow_epoch", -1)
        || revision.is_some_and(|v| v != integer(r, "revision", -1))
    {
        return Err(ApiError::new(409, "任务流程状态已改变，请刷新"));
    }
    Ok(())
}
pub(super) fn validate_stage(
    ctx: &Context,
    j: &Value,
    n: &Value,
    epoch: i64,
    kind: &str,
    values: &Value,
) -> Result<()> {
    let fields = &n[if kind == "input" { "fields" } else { "outputs" }];
    for field in fields.as_array().unwrap() {
        for fid in entitlements::attachment_ids(field, text(values, text(field, "key"))) {
            let f = required(
                &ctx.conn,
                "SELECT f.*,t.node_id,t.flow_epoch,t.attempt AS scope_attempt,t.kind AS scope_kind,t.job_id AS scope_job FROM job_files f JOIN task_flow_files t ON t.file_id=f.id WHERE f.id=?",
                [&fid],
                "附件不存在",
            )?;
            if text(&f, "job_id") != text(j, "id")
                || text(&f, "scope_job") != text(j, "id")
                || f["card_id"] != j["card_id"]
                || f["product_id"] != j["product_id"]
                || f["attempt"] != j["attempt"]
                || f["scope_attempt"] != j["attempt"]
                || text(&f, "field_key") != text(field, "key")
                || text(&f, "node_id") != text(n, "id")
                || integer(&f, "flow_epoch", 0) != epoch
                || text(&f, "kind") != kind
                || text(&f, "scope_kind") != kind
                || f["content"].is_null()
                || flag(&f, "consumed")
            {
                return Err(ApiError::new(403, "附件不属于当前任务步骤或字段"));
            }
            ctx.conn
                .execute("UPDATE job_files SET bound=1 WHERE id=?", [fid])?;
        }
    }
    Ok(())
}
fn promote(ctx: &Context, j: &Value, s: &Value, output: &Value, sources: &Value) -> Result<()> {
    let mut seen = HashSet::new();
    for field in s["product"]["outputs"].as_array().unwrap() {
        for fid in entitlements::attachment_ids(field, text(output, text(field, "key"))) {
            if !seen.insert(fid.clone()) {
                return Err(ApiError::new(403, "最终交付附件不能重复绑定"));
            }
            let source = &sources[text(field, "key")];
            let f = required(
                &ctx.conn,
                "SELECT f.*,t.node_id,t.flow_epoch,t.attempt AS scope_attempt,t.kind AS scope_kind,t.job_id AS scope_job FROM job_files f JOIN task_flow_files t ON t.file_id=f.id WHERE f.id=?",
                [&fid],
                "附件不存在",
            )?;
            if source.is_null()
                || text(&source, "kind") != "output"
                || text(&f, "job_id") != text(j, "id")
                || text(&f, "scope_job") != text(j, "id")
                || f["card_id"] != j["card_id"]
                || f["product_id"] != j["product_id"]
                || f["attempt"] != j["attempt"]
                || f["scope_attempt"] != j["attempt"]
                || text(&f, "field_key") != text(source, "field")
                || f["node_id"] != source["node"]
                || f["flow_epoch"] != source["flow_epoch"]
                || text(&f, "kind") != "output"
                || text(&f, "scope_kind") != "output"
                || !flag(&f, "bound")
                || f["content"].is_null()
                || flag(&f, "consumed")
            {
                return Err(ApiError::new(403, "最终交付附件的步骤来源无效"));
            }
            ctx.conn.execute(
                "UPDATE job_files SET field_key=? WHERE id=?",
                params![text(field, "key"), fid],
            )?;
        }
    }
    Ok(())
}
fn finish(ctx: &Context, j: &Value) -> Result<Value> {
    let j = job(ctx, text(j, "id"))?;
    worker::sync_dispatch(ctx, &j)?;
    Ok(j)
}
pub(super) fn terminal(
    ctx: &Context,
    j: &Value,
    r: &Value,
    s: &Value,
    state: &str,
    output: &Value,
    sources: &Value,
    message: &Value,
    retryable: bool,
) -> Result<Value> {
    let actual = job(ctx, text(j, "id"))?;
    if matches!(
        text(&actual, "state"),
        "succeeded" | "failed" | "rejected" | "destroyed"
    ) {
        if text(&actual, "state") == state {
            return Ok(actual);
        }
        return Err(ApiError::new(409, "任务已经结束，不能覆盖结果"));
    }
    clear_sensitive(ctx, r, s, None)?;
    let message = message
        .as_str()
        .or_else(|| message["zh-CN"].as_str())
        .or_else(|| {
            message
                .as_object()
                .and_then(|o| o.values().next())
                .and_then(Value::as_str)
        })
        .unwrap_or("");
    if state == "rejected" {
        files::purge_job_outputs(ctx, &actual)?;
        ctx.conn.execute("UPDATE jobs SET state='rejected',message=?,content=NULL,result_json=NULL,lease=NULL,claimed_by=NULL,retryable=0,updated=? WHERE id=?",params![message,now(),text(j,"id")])?;
        ctx.conn.execute(
            "UPDATE cards SET state='rejected' WHERE id=?",
            [text(j, "card_id")],
        )?;
        event(
            ctx,
            "fulfillment.rejected",
            text(j, "product_id"),
            Some(&job(ctx, text(j, "id"))?),
        )?;
    } else {
        if state == "succeeded" {
            promote(ctx, j, s, output, sources)?;
        }
        ctx.conn.execute(
            "UPDATE jobs SET state='processing' WHERE id=?",
            [text(j, "id")],
        )?;
        let mut update =
            json!({"state":state,"attempt":j["attempt"],"message":message,"retryable":retryable});
        if state == "succeeded" && text(&s["product"], "delivery") == "content" {
            update["output"] = output.clone();
        }
        super::super::apply_simple_update(ctx, text(j, "id"), &update)?;
    }
    finish(ctx, j)
}
pub(super) fn activate(
    ctx: &Context,
    j: &Value,
    r: &Value,
    s: &Value,
    target: &str,
    initial: bool,
) -> Result<Value> {
    if integer(r, "transition_count", 0) >= 256 {
        let r = mutate(
            ctx,
            r,
            json!({"phase":"ended","deadline":null,"input_expires_at":null}),
        )?;
        return terminal(
            ctx,
            j,
            &r,
            s,
            "failed",
            &json!({}),
            &json!({}),
            &json!("任务流程超过执行次数上限，请联系商家"),
            false,
        );
    }
    let n = node(s, target)?;
    let kind = text(&n, "kind");
    let clock = now();
    let started = if matches!(kind, "process" | "end")
        || (!initial && (kind == "display" || text(&n, "start_policy") == "automatic"))
    {
        Some(clock)
    } else {
        None
    };
    let deadline = started.and_then(|started| {
        n["timeout_seconds"]
            .as_f64()
            .map(|seconds| started + seconds)
    });
    let phase = match kind {
        "input" => {
            if started.is_some() {
                "input"
            } else {
                "await_start"
            }
        }
        "display" => {
            if started.is_some() {
                "display"
            } else {
                "await_start"
            }
        }
        "process" => "queued",
        _ => "ended",
    };
    let epoch = integer(r, "flow_epoch", 0) + 1;
    let mut expires = sensitive_expiry(ctx, r, s, false)?;
    if kind == "process" {
        for reference in n["inputs"].as_object().unwrap().values() {
            let (origin, f) = definition::reference_origin(&s["definition"], reference)?;
            if !flag(&f, "sensitive") {
                continue;
            }
            let expiry = match source(ctx, r, s, &origin) {
                Ok((v, _, _p)) if v == json!("") && !flag(&f, "required") => continue,
                Ok((_, _, p)) => {
                    let e = p["sensitive_until"][text(&origin, "field")].as_f64();
                    let used = p["consumed_by"][text(&origin, "field")].as_i64();
                    if used.is_some_and(|used| used != epoch) {
                        Some(clock)
                    } else {
                        e.or(Some(clock))
                    }
                }
                Err(e) if e.status == 409 => Some(clock),
                Err(e) => return Err(e),
            };
            if let Some(e) = expiry {
                expires = Some(expires.map(|old| old.min(e)).unwrap_or(e));
            }
        }
    }
    let r = mutate(
        ctx,
        r,
        json!({"flow_epoch":epoch,"node_id":target,"phase":phase,"entered_at":clock,"started_at":started,"deadline":deadline,"input_expires_at":expires,"transition_count":integer(r,"transition_count",0)+1}),
    )?;
    let encrypted = store_secret(
        &ctx.app,
        Some(text(&s["product"], "shop_id")),
        "task-flow-step",
        &format!("{}:{epoch}", text(j, "id")),
        &json!({}),
    )?;
    files::check_storage_quota(ctx, encrypted.len() as u64, text(j, "product_id"))?;
    ctx.conn.execute("INSERT INTO task_flow_steps(job_id,flow_epoch,attempt,node_id,kind,state,action_id,entered_at,started_at,deadline,completed_at,payload_ciphertext,result_digest) VALUES (?,?,?,?,?,'active',?,?,?,?,NULL,?,NULL)",params![text(j,"id"),epoch,integer(&r,"attempt",1),target,kind,if kind=="process"{Some(id())}else{None},clock,started,deadline,encrypted])?;
    if kind == "end" {
        ctx.conn.execute("UPDATE task_flow_steps SET state='completed',completed_at=? WHERE job_id=? AND flow_epoch=?",params![clock,text(j,"id"),epoch])?;
        let state = text(&n, "state");
        let (output, sources) = if state == "succeeded" {
            references(ctx, &r, s, &n["result"])?
        } else {
            (json!({}), json!({}))
        };
        let output = if state == "succeeded" {
            validate_values(&s["product"]["outputs"], &output, true, true)?
        } else {
            output
        };
        terminal(
            ctx,
            j,
            &r,
            s,
            state,
            &output,
            &sources,
            &n["message"],
            flag(&n, "retryable"),
        )
    } else {
        ctx.conn.execute(
            "UPDATE jobs SET state=?,claimed_by=NULL,lease=NULL,progress=0,updated=? WHERE id=?",
            params![
                if kind == "process" {
                    "queued"
                } else {
                    "waiting"
                },
                clock,
                text(j, "id")
            ],
        )?;
        finish(ctx, j)
    }
}
pub(super) fn timeout(ctx: &Context, j: &Value, r: &Value, s: &Value) -> Result<Value> {
    let n = node(s, text(r, "node_id"))?;
    ctx.conn.execute("UPDATE task_flow_steps SET state='timed_out',completed_at=? WHERE job_id=? AND flow_epoch=?",params![now(),text(j,"id"),integer(r,"flow_epoch",0)])?;
    clear_sensitive(
        ctx,
        r,
        s,
        if text(&n, "kind") == "process" {
            Some(&n)
        } else {
            None
        },
    )?;
    if text(&n, "kind") == "process" {
        let r = mutate(
            ctx,
            r,
            json!({"flow_epoch":integer(r,"flow_epoch",0)+1,"phase":"ended","deadline":null,"input_expires_at":null}),
        )?;
        terminal(
            ctx,
            j,
            &r,
            s,
            "failed",
            &json!({}),
            &json!({}),
            &json!("处理超时或中断，待商家核实"),
            false,
        )
    } else {
        activate(ctx, j, r, s, text(&n, "timeout_next"), false)
    }
}
pub fn submit(ctx: &Context, c: &Value, params: &Value) -> Result<Option<Value>> {
    let Some(s) = flow_card_snapshot(ctx, c)? else {
        return Ok(None);
    };
    require_enabled(ctx, text(c, "product_id"))?;
    ensure_card(ctx, c)?;
    if params.as_object().is_none_or(|o| !o.is_empty()) {
        return Err(ApiError::new(409, "请在当前流程步骤提交信息"));
    }
    if let Some(j) = row(
        &ctx.conn,
        "SELECT * FROM jobs WHERE card_id=?",
        [text(c, "id")],
    )? {
        if text(&j, "state") == "rejected" {
            return Err(ApiError::new(409, "此任务已被拒绝，不能重新提交"));
        }
        if matches!(text(&j, "state"), "failed" | "needs_input") {
            if !flag(&job_view(ctx, &j, false)?, "can_retry") {
                return Err(ApiError::new(409, "此任务不能重试，请联系商家"));
            }
            let r = run(ctx, &j)?;
            clear_sensitive(ctx, &r, &s, None)?;
            files::purge_job_files(ctx, &j)?;
            ctx.conn.execute("UPDATE jobs SET attempt=attempt+1,state='waiting',params='{}',content=NULL,result_json=NULL,claimed_by=NULL,lease=NULL,progress=0,completed_steps='[]',retryable=0,updated=? WHERE id=?",params![now(),text(&j,"id")])?;
            ctx.conn.execute(
                "UPDATE cards SET state='reserved' WHERE id=?",
                [text(c, "id")],
            )?;
            ctx.conn.execute("UPDATE task_flow_steps SET state='cancelled',completed_at=? WHERE job_id=? AND state='active'",params![now(),text(&j,"id")])?;
            let j = job(ctx, text(&j, "id"))?;
            let r = mutate(
                ctx,
                &r,
                json!({"attempt":j["attempt"],"phase":"initializing","deadline":null,"input_expires_at":null}),
            )?;
            return Ok(Some(activate(
                ctx,
                &j,
                &r,
                &s,
                text(&s["definition"], "entry"),
                true,
            )?));
        }
        return Ok(Some(j));
    }
    if text(c, "state") != "ready" {
        return Err(ApiError::new(409, "此卡密无法兑换"));
    }
    let jid = id();
    let p = &s["product"];
    ctx.conn.execute("INSERT INTO jobs(id,card_id,product_id,state,params,created,updated,progress_plan,schema_snapshot) VALUES (?,?,?,'waiting','{}',?,?,?,?)",params![jid,text(c,"id"),text(c,"product_id"),now(),now(),p.get("progress_steps").cloned().unwrap_or(json!([])).to_string(),json!({"parameters":p["parameters"],"outputs":p["outputs"]}).to_string()])?;
    ctx.conn.execute(
        "UPDATE cards SET state='reserved' WHERE id=?",
        [text(c, "id")],
    )?;
    ctx.conn.execute("INSERT INTO task_flow_runs(job_id,attempt,phase,created,updated) VALUES (?,1,'initializing',?,?)",params![jid,now(),now()])?;
    let j = job(ctx, &jid)?;
    let r = run(ctx, &j)?;
    Ok(Some(activate(
        ctx,
        &j,
        &r,
        &s,
        text(&s["definition"], "entry"),
        true,
    )?))
}
pub fn claim(ctx: &Context, j: &Value, who: &str, epoch: i64) -> Result<Value> {
    let (j, r, s) = guard(ctx, j)?;
    expect(&r, epoch, None)?;
    if text(&r, "phase") != "queued" || text(&j, "state") != "queued" {
        return Err(ApiError::new(409, "处理步骤已被领取或不在队列"));
    }
    if r["deadline"].as_f64().is_some_and(|d| d <= now()) {
        return timeout(ctx, &j, &r, &s);
    }
    if execution(ctx, &j, false)?.is_none() {
        return Err(ApiError::new(409, "处理步骤输入已失效"));
    }
    mutate(ctx, &r, json!({"phase":"processing"}))?;
    let lease = r["deadline"]
        .as_f64()
        .map(|d| d.min(now() + 3600.0))
        .unwrap_or(now() + 3600.0);
    ctx.conn.execute("UPDATE jobs SET state='processing',claimed_by=?,lease=?,updated=? WHERE id=? AND state='queued'",params![who,lease,now(),text(&j,"id")])?;
    finish(ctx, &j)
}
pub fn release_claim(ctx: &Context, j: &Value, who: &str) -> Result<Value> {
    let (j, r, s) = guard(ctx, j)?;
    if text(&r, "phase") != "processing" || text(&j, "claimed_by") != who {
        return Err(ApiError::new(409, "处理步骤不属于当前处理者"));
    }
    if r["deadline"].as_f64().is_some_and(|d| d <= now()) {
        return timeout(ctx, &j, &r, &s);
    }
    mutate(ctx, &r, json!({"phase":"queued"}))?;
    ctx.conn.execute(
        "UPDATE jobs SET state='queued',claimed_by=NULL,lease=NULL,updated=? WHERE id=?",
        params![now(), text(&j, "id")],
    )?;
    finish(ctx, &j)
}
pub fn process_update(ctx: &Context, j: &Value, u: &Value) -> Result<Value> {
    let (j, r, s) = guard(ctx, j)?;
    check_identity(ctx, &j, u)?;
    let n = node(&s, text(&r, "node_id"))?;
    let state = text(u, "state");
    if text(&s["product"], "mode") == "manual"
        && (text(&r, "phase") != "processing"
            || text(&j, "claimed_by").is_empty()
            || j["lease"].as_f64().is_none_or(|d| d <= now()))
    {
        return Err(ApiError::new(409, "请先领取且只能处理自己的步骤"));
    }
    if r["deadline"].as_f64().is_some_and(|d| d <= now()) {
        return timeout(ctx, &j, &r, &s);
    }
    let message = text(u, "message");
    if message.chars().count() > 1000 {
        return Err(ApiError::bad("任务消息过长"));
    }
    if state == "processing" {
        if u.get("content").is_some_and(|v| !v.is_null())
            || u.get("output")
                .is_some_and(|v| !v.is_null() && v.as_object().is_none_or(|m| !m.is_empty()))
        {
            return Err(ApiError::bad("只有步骤完成可以包含结果"));
        }
        let progress = u["progress"].as_i64().unwrap_or(0);
        if !(0..=99).contains(&progress) || progress < integer(&j, "progress", 0) {
            return Err(ApiError::new(409, "步骤进度无效或倒退"));
        }
        mutate(ctx, &r, json!({"phase":"processing"}))?;
        ctx.conn.execute(
            "UPDATE jobs SET state='processing',progress=?,message=?,updated=? WHERE id=?",
            params![progress, message, now(), text(&j, "id")],
        )?;
        return finish(ctx, &j);
    }
    if !["succeeded", "failed"].contains(&state) {
        return Err(ApiError::bad("处理步骤状态无效"));
    }
    let mut raw = u
        .get("output")
        .filter(|v| !v.is_null())
        .cloned()
        .unwrap_or(json!({}));
    if !raw.is_object() {
        return Err(ApiError::bad("输出字段必须是对象"));
    }
    if let Some(content) = u.get("content").filter(|v| !v.is_null()) {
        if n["outputs"]
            .as_array()
            .is_none_or(|a| a.len() != 1 || text(&a[0], "key") != "content")
        {
            return Err(ApiError::bad("请按步骤定义提交结果字段"));
        }
        if raw.get("content").is_some_and(|v| v != content) {
            return Err(ApiError::bad("步骤内容与结果字段不一致"));
        }
        raw["content"] = content.clone();
    }
    if state == "failed" && raw.as_object().is_none_or(|m| !m.is_empty()) {
        return Err(ApiError::bad("失败步骤不能包含结果"));
    }
    let clean = if state == "succeeded" {
        validate_values(&n["outputs"], &raw, true, true)?
    } else {
        json!({})
    };
    let epoch = integer(&r, "flow_epoch", 0);
    if state == "succeeded" {
        validate_stage(ctx, &j, &n, epoch, "output", &clean)?;
    }
    let st = step(ctx, &r, None)?;
    write_payload(ctx, &s, &st, &json!({"output":clean,"message":message}))?;
    let receipt = fingerprint(
        ctx,
        u,
        text(&s["product"], "shop_id"),
        &format!("{}:{epoch}", text(&j, "id")),
    )?;
    ctx.conn.execute("UPDATE task_flow_steps SET state=?,completed_at=?,result_digest=? WHERE job_id=? AND flow_epoch=?",params![if state=="succeeded"{"completed"}else{"failed"},now(),receipt,text(&j,"id"),epoch])?;
    clear_sensitive(ctx, &r, &s, Some(&n))?;
    if state == "failed" && !flag(u, "retryable") {
        let r = mutate(
            ctx,
            &r,
            json!({"phase":"ended","deadline":null,"input_expires_at":null}),
        )?;
        return terminal(
            ctx,
            &j,
            &r,
            &s,
            "failed",
            &json!({}),
            &json!({}),
            &json!(if message.is_empty() {
                "处理失败，待商家核实"
            } else {
                message
            }),
            false,
        );
    }
    let target = if state == "succeeded" {
        next(ctx, &r, &s, &n["next"])?
    } else {
        text(&n, "failure_next").to_owned()
    };
    activate(ctx, &j, &r, &s, &target, false)
}
pub fn destroy(ctx: &Context, j: &Value) -> Result<()> {
    if !is_flow(ctx, j)? {
        return Ok(());
    }
    let Some(r) = row(
        &ctx.conn,
        "SELECT * FROM task_flow_runs WHERE job_id=?",
        [text(j, "id")],
    )?
    else {
        return Ok(());
    };
    let s = snapshot(ctx, j)?;
    for st in rows(
        &ctx.conn,
        "SELECT * FROM task_flow_steps WHERE job_id=?",
        [text(j, "id")],
    )? {
        write_payload(ctx, &s, &st, &json!({}))?;
    }
    mutate(
        ctx,
        &r,
        json!({"phase":"ended","deadline":null,"input_expires_at":null}),
    )?;
    worker::cancel_dispatches(ctx, text(j, "id"), None)?;
    Ok(())
}
pub fn stop(ctx: &Context, j: &Value, _state: &str) -> Result<()> {
    if !is_flow(ctx, j)? {
        return Ok(());
    }
    destroy(ctx, j)?;
    let r = run(ctx, j)?;
    ctx.conn.execute("UPDATE task_flow_steps SET state='cancelled',completed_at=? WHERE job_id=? AND state='active'",params![now(),text(j,"id")])?;
    mutate(
        ctx,
        &r,
        json!({"flow_epoch":integer(&r,"flow_epoch",0)+1,"phase":"ended","deadline":null,"input_expires_at":null}),
    )?;
    Ok(())
}
pub(super) fn expire_due(ctx: &Context) -> Result<bool> {
    let list = rows(
        &ctx.conn,
        "SELECT j.* FROM jobs j JOIN task_flow_runs r ON r.job_id=j.id WHERE r.phase IN ('await_start','input','display','queued','processing') AND ((r.deadline IS NOT NULL AND r.deadline<=?) OR (r.input_expires_at IS NOT NULL AND r.input_expires_at<=?)) ORDER BY COALESCE(r.input_expires_at,r.deadline),j.id LIMIT 100",
        params![now(), now()],
    )?;
    let mut changed = false;
    for j in list {
        let r = run(ctx, &j)?;
        let s = snapshot(ctx, &j)?;
        let ttl = r["input_expires_at"].as_f64().is_some_and(|v| v <= now());
        let remaining = if ttl {
            sensitive_expiry(ctx, &r, &s, true)?
        } else {
            None
        };
        let (j, r, s) = match guard(ctx, &j) {
            Ok(v) => v,
            Err(_) => {
                if ttl {
                    mutate(ctx, &r, json!({"input_expires_at":remaining}))?;
                    changed = true;
                }
                continue;
            }
        };
        if r["deadline"].as_f64().is_some_and(|v| v <= now()) {
            timeout(ctx, &j, &r, &s)?;
            changed = true;
            continue;
        }
        if ttl {
            if text(&r, "phase") == "queued" {
                let n = node(&s, text(&r, "node_id"))?;
                let mut bad_source = None;
                for reference in n["inputs"].as_object().unwrap().values() {
                    let (origin, f) = definition::reference_origin(&s["definition"], reference)?;
                    if !flag(&f, "sensitive") {
                        continue;
                    }
                    let invalid = match source(ctx, &r, &s, &origin) {
                        Ok((v, _, p)) => {
                            if v == json!("") && !flag(&f, "required") {
                                false
                            } else {
                                p["sensitive_until"][text(&origin, "field")]
                                    .as_f64()
                                    .is_none_or(|e| e <= now())
                                    || p["consumed_by"][text(&origin, "field")]
                                        .as_i64()
                                        .is_some_and(|e| e != integer(&r, "flow_epoch", 0))
                            }
                        }
                        Err(e) if e.status == 409 => true,
                        Err(e) => return Err(e),
                    };
                    if invalid {
                        bad_source = Some(text(&origin, "node").to_owned());
                        break;
                    }
                }
                if let Some(target) = bad_source {
                    ctx.conn.execute("UPDATE task_flow_steps SET state='cancelled',completed_at=? WHERE job_id=? AND flow_epoch=?",params![now(),text(&j,"id"),integer(&r,"flow_epoch",0)])?;
                    clear_sensitive(ctx, &r, &s, Some(&n))?;
                    activate(ctx, &j, &r, &s, &target, true)?;
                    ctx.conn.execute(
                        "UPDATE jobs SET message=? WHERE id=?",
                        params!["敏感输入已过期，请重新填写", text(&j, "id")],
                    )?;
                } else {
                    mutate(ctx, &r, json!({"input_expires_at":remaining}))?;
                }
            } else {
                mutate(ctx, &r, json!({"input_expires_at":remaining}))?;
            }
            changed = true;
            worker::sync_dispatch(ctx, &job(ctx, text(&j, "id"))?)?;
        }
    }
    Ok(changed)
}
pub fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    if ctx.method.as_str() != "POST" || !ctx.path.starts_with("/api/task-flow/") {
        return Ok(None);
    }
    let operation = ctx.path.trim_start_matches("/api/task-flow/").to_owned();
    if !["start", "answer", "continue", "cancel", "restart"].contains(&operation.as_str()) {
        return Ok(None);
    }
    if ctx.body.as_object().is_none_or(|o| {
        o.keys().any(|k| {
            ![
                "token",
                "card_id",
                "flow_epoch",
                "expected_revision",
                "values",
            ]
            .contains(&k.as_str())
        })
    }) {
        return Err(ApiError::bad("流程操作字段无效"));
    }
    let c = grant(ctx, ctx.b("token"), ctx.body["card_id"].as_str())?;
    require_enabled(ctx, text(&c, "product_id"))?;
    let mut epoch = ctx.body["flow_epoch"]
        .as_i64()
        .filter(|v| *v >= 0)
        .ok_or_else(|| ApiError::bad("流程轮次无效"))?;
    let mut revision = ctx.body["expected_revision"]
        .as_i64()
        .filter(|v| *v >= 0)
        .ok_or_else(|| ApiError::bad("流程版本无效"))?;
    let values = ctx.body.get("values").cloned().unwrap_or(json!({}));
    if operation != "answer" && values.as_object().is_none_or(|o| !o.is_empty()) {
        return Err(ApiError::bad("此操作不能提交字段值"));
    }
    let existing = row(
        &ctx.conn,
        "SELECT * FROM jobs WHERE card_id=?",
        [text(&c, "id")],
    )?;
    let j = if let Some(j) = existing {
        j
    } else {
        if operation != "start" || epoch != 0 || revision != 0 {
            return Err(ApiError::new(409, "请先开始任务流程"));
        }
        let j =
            submit(ctx, &c, &json!({}))?.ok_or_else(|| ApiError::new(409, "此卡密没有任务流程"))?;
        let r = run(ctx, &j)?;
        epoch = integer(&r, "flow_epoch", 0);
        revision = integer(&r, "revision", 0);
        j
    };
    if !is_flow(ctx, &j)? {
        return Err(ApiError::new(409, "此任务没有步骤流程"));
    }
    let (j, r, s) = guard(ctx, &j)?;
    if operation == "restart" {
        expect(&r, epoch, Some(revision))?;
        if !matches!(text(&j, "state"), "failed" | "needs_input") {
            return Err(ApiError::new(409, "只有允许重试的失败任务可以重新开始"));
        }
        let j = submit(ctx, &c, &json!({}))?.unwrap();
        audit(
            &ctx.conn,
            "customer",
            "task_flow.restart",
            text(&j, "id"),
            s["product"]["shop_id"].as_str(),
        )?;
        return Ok(Some(Reply::json(job_view(ctx, &j, false)?)));
    }
    if operation == "answer" {
        if let Some(old) = row(
            &ctx.conn,
            "SELECT * FROM task_flow_steps WHERE job_id=? AND flow_epoch=?",
            params![text(&j, "id"), epoch],
        )? {
            if old["attempt"] == j["attempt"]
                && text(&old, "kind") == "input"
                && text(&old, "state") == "completed"
            {
                let n = node(&s, text(&old, "node_id"))?;
                let clean = validate_values(&n["fields"], &values, false, false)?;
                let digest = fingerprint(
                    ctx,
                    &clean,
                    text(&s["product"], "shop_id"),
                    &format!("{}:{epoch}", text(&j, "id")),
                )?;
                if !bool::from(
                    digest
                        .as_bytes()
                        .ct_eq(text(&old, "result_digest").as_bytes()),
                ) {
                    return Err(ApiError::new(409, "同一次回答不能更换内容"));
                }
                return Ok(Some(Reply::json(job_view(ctx, &j, false)?)));
            }
        }
    }
    expect(
        &r,
        epoch,
        if operation == "start" && matches!(text(&r, "phase"), "input" | "display") {
            None
        } else {
            Some(revision)
        },
    )?;
    let mut expired = false;
    let updated = if r["deadline"].as_f64().is_some_and(|v| v <= now()) && operation != "cancel" {
        expired = true;
        timeout(ctx, &j, &r, &s)?
    } else {
        let n = node(&s, text(&r, "node_id"))?;
        match operation.as_str() {
            "start" => {
                if matches!(text(&r, "phase"), "input" | "display") {
                    j.clone()
                } else {
                    if text(&r, "phase") != "await_start" {
                        return Err(ApiError::new(409, "当前步骤不能开始答题"));
                    }
                    let clock = now();
                    let deadline = n["timeout_seconds"].as_f64().map(|v| clock + v);
                    mutate(
                        ctx,
                        &r,
                        json!({"phase":if text(&n,"kind")=="display"{"display"}else{"input"},"started_at":clock,"deadline":deadline}),
                    )?;
                    ctx.conn.execute("UPDATE task_flow_steps SET started_at=?,deadline=? WHERE job_id=? AND flow_epoch=?",params![clock,deadline,text(&j,"id"),epoch])?;
                    finish(ctx, &j)?
                }
            }
            "answer" => {
                if text(&r, "phase") != "input" {
                    return Err(ApiError::new(409, "当前步骤不接受回答"));
                }
                let clean = validate_values(&n["fields"], &values, false, false)?;
                validate_stage(ctx, &j, &n, epoch, "input", &clean)?;
                let mut until = json!({});
                for f in n["fields"].as_array().unwrap() {
                    let key = text(f, "key");
                    if flag(f, "sensitive") && !text(&clean, key).is_empty() {
                        let expiry = now() + integer(f, "sensitive_ttl_seconds", 120) as f64;
                        until[key] = json!(
                            r["deadline"]
                                .as_f64()
                                .map(|d| d.min(expiry))
                                .unwrap_or(expiry)
                        );
                    }
                }
                let st = step(ctx, &r, None)?;
                write_payload(
                    ctx,
                    &s,
                    &st,
                    &json!({"input":clean,"sensitive_until":until}),
                )?;
                let digest = fingerprint(
                    ctx,
                    &clean,
                    text(&s["product"], "shop_id"),
                    &format!("{}:{epoch}", text(&j, "id")),
                )?;
                ctx.conn.execute("UPDATE task_flow_steps SET state='completed',completed_at=?,result_digest=? WHERE job_id=? AND flow_epoch=?",params![now(),digest,text(&j,"id"),epoch])?;
                activate(ctx, &j, &r, &s, &next(ctx, &r, &s, &n["next"])?, false)?
            }
            "continue" => {
                if text(&r, "phase") != "display" {
                    return Err(ApiError::new(409, "当前步骤不能继续"));
                }
                ctx.conn.execute("UPDATE task_flow_steps SET state='completed',completed_at=? WHERE job_id=? AND flow_epoch=?",params![now(),text(&j,"id"),epoch])?;
                activate(ctx, &j, &r, &s, text(&n, "next"), false)?
            }
            "cancel" => {
                if text(&r, "phase") == "ended" {
                    return Err(ApiError::new(409, "已经结束的任务不能取消"));
                }
                let uncertain = text(&r, "phase") == "processing";
                ctx.conn.execute("UPDATE task_flow_steps SET state='cancelled',completed_at=? WHERE job_id=? AND state='active'",params![now(),text(&j,"id")])?;
                for st in rows(
                    &ctx.conn,
                    "SELECT * FROM task_flow_steps WHERE job_id=?",
                    [text(&j, "id")],
                )? {
                    write_payload(ctx, &s, &st, &json!({}))?;
                }
                let r = mutate(
                    ctx,
                    &r,
                    json!({"flow_epoch":integer(&r,"flow_epoch",0)+1,"phase":"ended","deadline":null,"input_expires_at":null}),
                )?;
                let result = terminal(
                    ctx,
                    &j,
                    &r,
                    &s,
                    "failed",
                    &json!({}),
                    &json!({}),
                    &json!("顾客已取消任务"),
                    !uncertain,
                )?;
                files::purge_job_files(ctx, &j)?;
                destroy(ctx, &result)?;
                result
            }
            _ => unreachable!(),
        }
    };
    audit(
        &ctx.conn,
        "customer",
        &format!("task_flow.{operation}"),
        text(&j, "id"),
        s["product"]["shop_id"].as_str(),
    )?;
    if expired {
        let mut reply = Reply::json(json!({"detail":"当前步骤已超时，请刷新查看后续步骤"}));
        reply.status = 409;
        Ok(Some(reply))
    } else {
        Ok(Some(Reply::json(job_view(ctx, &updated, false)?)))
    }
}

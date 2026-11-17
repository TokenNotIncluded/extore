use super::*;
pub fn screen(ctx: &Context, c: &Value) -> Result<(Option<Value>, Value)> {
    if c.is_null() || text(c, "state") == "revoked" {
        return Err(ApiError::new(404, "卡密不存在或已撤销"));
    }
    ensure_card(ctx, c)?;
    require_enabled(ctx, text(c, "product_id"))?;
    let j = row(
        &ctx.conn,
        "SELECT * FROM jobs WHERE card_id=?",
        [text(c, "id")],
    )?;
    let p = if let Some(j) = &j {
        job_product(ctx, j)?
    } else {
        catalog::card_product(ctx, c)?
    };
    if j.as_ref().is_some_and(|j| {
        text(j, "state") == "destroyed"
            || (text(j, "state") == "succeeded"
                && (text(&p, "view_policy") == "once" || text(&p, "delivery") == "service"))
    }) {
        return Err(ApiError::new(410, "卡密已使用，无法再次领取"));
    }
    Ok((j, p))
}
pub fn receipt_item(ctx: &Context, c: &Value) -> Result<Value> {
    let j = row(
        &ctx.conn,
        "SELECT * FROM jobs WHERE card_id=?",
        [text(c, "id")],
    )?;
    let p = if let Some(j) = &j {
        job_product(ctx, j)?
    } else {
        catalog::card_product(ctx, c)?
    };
    let meta = row(
        &ctx.conn,
        "SELECT code_suffix FROM card_meta WHERE card_id=?",
        [text(c, "id")],
    )?;
    let mut result = json!({"card_id":c["id"],"suffix":meta.as_ref().map(|r|text(r,"code_suffix")).unwrap_or(""),"product":catalog::public_product(&p),"variant":catalog::card_variant(ctx,c)?,"job":if let Some(j)=&j{job_view(ctx,j,false)?}else{Value::Null}});
    merge(&mut result, entitlements::card_view(ctx, c, j.as_ref())?);
    Ok(result)
}
pub fn batch_receipt(ctx: &Context, list: &[Value]) -> Result<Value> {
    let items = list
        .iter()
        .map(|c| receipt_item(ctx, c))
        .collect::<Result<Vec<_>>>()?;
    let product = list
        .first()
        .map(|card| {
            catalog::product(ctx, text(card, "product_id")).map(|p| catalog::public_product(&p))
        })
        .transpose()?
        .unwrap_or(Value::Null);
    Ok(json!({"batch":true,"product":product,"items":items}))
}
pub fn cleanup(ctx: &Context) -> Result<()> {
    ctx.conn
        .execute("DELETE FROM grants WHERE expires<?", [now()])?;
    ctx.conn.execute("DELETE FROM receipt_batch_cards WHERE digest IN (SELECT digest FROM receipt_batches WHERE expires<?)",[now()])?;
    ctx.conn
        .execute("DELETE FROM receipt_batches WHERE expires<?", [now()])?;
    Ok(())
}
pub fn codes(ctx: &Context, raw: &str) -> Result<Vec<String>> {
    if raw.is_empty() || raw.chars().count() > 8000 {
        return Err(ApiError::bad("请输入卡密，一次最多 8000 字符"));
    }
    if row(
        &ctx.conn,
        "SELECT 1 FROM cards WHERE digest=?",
        [catalog::card_digest(raw)],
    )?
    .is_some()
    {
        return Ok(vec![raw.to_owned()]);
    }
    let list = raw
        .split(|c: char| c.is_whitespace() || ",，;；".contains(c))
        .filter(|s| !s.is_empty())
        .map(String::from)
        .collect::<Vec<_>>();
    if list.is_empty() { return Err(ApiError::bad("请输入卡密")); }
    if list.len() > 30 { return Err(ApiError::bad("一次最多兑换 30 张卡密")); }
    Ok(list)
}
pub fn verified(ctx: &Context, c: &Value) -> Result<()> {
    ctx.conn.execute("INSERT OR IGNORE INTO card_meta(card_id,variant_id,variant_snapshot) VALUES (?,'default','{}')",[text(c,"id")])?;
    ctx.conn.execute(
        "UPDATE card_meta SET first_verified=COALESCE(first_verified,?) WHERE card_id=?",
        params![now(), text(c, "id")],
    )?;
    Ok(())
}
fn exchange(ctx: &Context) -> Result<Value> {
    let values = codes(ctx, ctx.b("code"))?;
    let mut list = Vec::new();
    let mut seen = HashSet::new();
    let multiple = values.len() > 1;
    for v in values {
        let unwrapped = catalog::unwrap_code(ctx, &v)?;
        let key = catalog::card_digest(&unwrapped);
        if !seen.insert(key.clone()) {
            continue;
        }
        let ordinal = list.len() + 1;
        let annotate = |e: ApiError| {
            if multiple {
                ApiError::new(e.status, format!("第 {ordinal} 张：{}", e.message))
            } else {
                e
            }
        };
        let c = required(
            &ctx.conn,
            "SELECT * FROM cards WHERE digest=?",
            [key],
            "卡密不存在",
        )
        .map_err(&annotate)?;
        screen(ctx, &c).map_err(annotate)?;
        list.push(c);
    }
    if list
        .iter()
        .any(|c| c["product_id"] != list[0]["product_id"])
    {
        return Err(ApiError::bad("这些卡密不是同一件商品，请分开兑换"));
    }
    cleanup(ctx)?;
    let value = token();
    let key = digest(&value);
    let mut result = if list.len() == 1 {
        ctx.conn.execute(
            "INSERT INTO grants(digest,card_id,expires) VALUES (?,?,?)",
            params![key, text(&list[0], "id"), now() + 30.0 * 86400.0],
        )?;
        let mut v = receipt_item(ctx, &list[0])?;
        v.as_object_mut().unwrap().remove("card_id");
        v.as_object_mut().unwrap().remove("suffix");
        v
    } else {
        ctx.conn.execute(
            "INSERT INTO receipt_batches(digest,expires,created) VALUES (?,?,?)",
            params![key, now() + 30.0 * 86400.0, now()],
        )?;
        for (i, c) in list.iter().enumerate() {
            ctx.conn.execute(
                "INSERT INTO receipt_batch_cards(digest,card_id,position) VALUES (?,?,?)",
                params![key, text(c, "id"), i as i64],
            )?;
        }
        batch_receipt(ctx, &list)?
    };
    for c in &list {
        verified(ctx, c)?;
    }
    result["token"] = json!(value);
    Ok(result)
}
fn scope(ctx: &Context, s: &Value, requested: &str) -> Result<String> {
    let pid = if text(s, "role") == "staff" {
        if !requested.is_empty() && requested != text(s, "product_id") {
            return Err(ApiError::new(403, "没有此商品的管理权限"));
        }
        text(s, "product_id")
    } else {
        requested
    };
    if pid.is_empty() {
        return Err(ApiError::bad("请选择一个商品"));
    }
    let owner = auth::authorize_product(ctx, s, pid)?;
    if let Some(shop) = auth::selected_shop(ctx, s, ctx.q("shop_id"))? {
        if text(&owner, "shop_id") != shop {
            return Err(ApiError::new(403, "商品不属于所选店铺"));
        }
    }
    Ok(pid.to_owned())
}
fn queue(ctx: &Context) -> Result<Value> {
    let s = auth::session(ctx, &["admin", "staff"], Some("queue.view"))?;
    let pid = scope(ctx, &s, ctx.q("product_id"))?;
    let state = ctx.q("state");
    let jid = ctx.q("job_id");
    let view = ctx.q("view");
    let limit = ctx.q("limit").parse::<i64>().unwrap_or(100).clamp(1, 500);
    let list = rows(
        &ctx.conn,
        "SELECT * FROM jobs WHERE product_id=? AND (?='' OR state=?) AND (?='' OR id=?) AND (?!='' OR ?!='' OR ?='all' OR (?='processed' AND state IN ('succeeded','destroyed','rejected')) OR (? IN ('','active') AND state IN ('queued','processing','waiting','failed','needs_input'))) ORDER BY created,id LIMIT ?",
        params![
            pid, state, state, jid, jid, state, jid, view, view, view, limit
        ],
    )?;
    let compact = matches!(ctx.q("compact"), "true" | "1");
    let mut result = Vec::new();
    for j in list {
        let mut v = job_view(ctx, &j, !compact)?;
        if compact {
            v["claimed_by"] = j["claimed_by"].clone();
            let mut stats = json!({});
            for kind in ["input", "output"] {
                stats[kind] = required(
                    &ctx.conn,
                    "SELECT COUNT(*) AS count,COALESCE(SUM(size),0) AS bytes FROM job_files WHERE job_id=? AND kind=? AND content IS NOT NULL",
                    params![text(&j, "id"), kind],
                    "文件统计失败",
                )?;
            }
            v["attachments"] = stats;
        }
        result.push(v);
    }
    Ok(json!(result))
}
fn queue_batch(ctx: &Context) -> Result<Value> {
    let action = ctx.b("action");
    let s = auth::session(
        ctx,
        &["admin", "staff"],
        Some(if action == "retry" {
            "queue.retry"
        } else {
            "queue.process"
        }),
    )?;
    let pid = scope(ctx, &s, ctx.b("product_id"))?;
    let who = actor(&s);
    let p = catalog::product(ctx, &pid)?;
    let ids = ctx.body["ids"]
        .as_array()
        .filter(|v| !v.is_empty() && v.len() <= 100)
        .ok_or_else(|| ApiError::bad("请至少选择一个任务，最多一百个"))?;
    let mut seen = HashSet::new();
    if let Some(scopes) = ctx.body["flow_scopes"].as_object() {
        if scopes
            .keys()
            .any(|k| !ids.iter().any(|id| id.as_str() == Some(k)))
        {
            return Err(ApiError::bad("流程身份包含未选择的任务"));
        }
    }
    for jid in ids {
        let jid = jid.as_str().ok_or_else(|| ApiError::bad("任务编号无效"))?;
        if !seen.insert(jid) {
            continue;
        }
        let mut j = job(ctx, jid)?;
        if text(&j, "product_id") != pid {
            return Err(ApiError::new(403, "批处理只能包含所选商品的任务"));
        }
        let scope = ctx.body["flow_scopes"]
            .get(jid)
            .cloned()
            .unwrap_or(json!({}));
        if scope.as_object().is_none_or(|o| {
            o.keys()
                .any(|k| !["attempt", "flow_epoch", "action_id"].contains(&k.as_str()))
        }) {
            return Err(ApiError::bad("流程身份字段无效"));
        }
        let attempt = scope
            .get("attempt")
            .filter(|v| !v.is_null())
            .or_else(|| ctx.body.get("attempt").filter(|v| !v.is_null()));
        if integer(&j, "revision_round", 0) > 0 && attempt.is_none() {
            return Err(ApiError::new(409, "修改轮次操作必须指定当前任务的 attempt"));
        }
        if attempt.is_some_and(|a| a.as_i64() != Some(integer(&j, "attempt", 1))) {
            return Err(ApiError::new(409, "提交对应的任务尝试已失效"));
        }
        let epoch = scope
            .get("flow_epoch")
            .filter(|v| !v.is_null())
            .or_else(|| ctx.body.get("flow_epoch"))
            .cloned()
            .unwrap_or(Value::Null);
        let aid = scope
            .get("action_id")
            .filter(|v| !v.is_null())
            .or_else(|| ctx.body.get("action_id"))
            .cloned()
            .unwrap_or(Value::Null);
        let is_flow = flow::is_flow(ctx, &j)?;
        if text(&p, "mode") != "manual" && action != "retry" {
            return Err(ApiError::new(409, "自动处理任务不能由队列处理覆盖"));
        }
        if is_flow && action != "retry" {
            flow::check_identity(
                ctx,
                &j,
                &json!({"attempt":j["attempt"],"flow_epoch":epoch,"action_id":aid}),
            )?;
        }
        if let Some(steps) = ctx.body.get("progress_steps").filter(|v| !v.is_null()) {
            if matches!(
                action,
                "request_changes" | "request_retry" | "reject" | "retry"
            ) {
                return Err(ApiError::bad("此操作不能修改处理步骤"));
            }
            let (plan, done) = progress(ctx, &j)?;
            if !matches!(text(&j, "state"), "queued" | "processing")
                || !plan.is_empty()
                || !done.is_empty()
            {
                return Err(ApiError::new(
                    409,
                    "只有尚未定义步骤的待处理任务可以设置处理步骤",
                ));
            }
            let steps = steps
                .as_array()
                .filter(|a| !a.is_empty() && a.len() <= 30)
                .ok_or_else(|| ApiError::bad("步骤无效"))?;
            let mut keys = HashSet::new();
            for step in steps {
                if text(step, "id").is_empty()
                    || !keys.insert(text(step, "id"))
                    || !step["label"].is_object()
                {
                    return Err(ApiError::bad("步骤无效或重复"));
                }
            }
            ctx.conn.execute(
                "UPDATE jobs SET progress_plan=?,completed_steps='[]',progress=0 WHERE id=?",
                params![json!(steps).to_string(), jid],
            )?;
            j = job(ctx, jid)?;
        }
        match action {
            "claim" => {
                if is_flow {
                    flow::claim(ctx, &j, &who, epoch.as_i64().unwrap_or(0))?;
                } else {
                    if text(&j, "state") != "queued" {
                        return Err(ApiError::new(409, "任务已被领取或完成，请刷新列表"));
                    }
                    ctx.conn
                        .execute("UPDATE jobs SET claimed_by=? WHERE id=?", params![who, jid])?;
                    let mut u = ctx.body.clone();
                    u["state"] = json!("processing");
                    u["attempt"] = j["attempt"].clone();
                    if text(&u, "message").is_empty() {
                        u["message"] = json!("正在处理");
                    }
                    apply_update(ctx, jid, &u)?;
                }
            }
            "progress" | "succeed" | "fail" => {
                if text(&j, "state") != "processing" || text(&j, "claimed_by") != who {
                    return Err(ApiError::new(409, "请先领取任务，且只能处理自己领取的任务"));
                }
                let mut u = ctx.body.clone();
                u["state"] = json!(match action {
                    "succeed" => "succeeded",
                    "fail" => "failed",
                    _ => "processing",
                });
                u["attempt"] = j["attempt"].clone();
                u["flow_epoch"] = epoch;
                u["action_id"] = aid;
                apply_update(ctx, jid, &u)?;
            }
            "request_changes" | "request_retry" | "reject" => {
                if text(&j, "state") != "processing" || text(&j, "claimed_by") != who {
                    return Err(ApiError::new(409, "请先领取任务，且只能处理自己领取的任务"));
                }
                ensure_card(ctx, &card(ctx, text(&j, "card_id"))?)?;
                let reason = ctx.b("message").trim();
                if reason.is_empty() || reason.chars().count() > 1000 {
                    return Err(ApiError::bad("要求重试或拒绝任务必须填写原因"));
                }
                if ctx.body.get("content").is_some_and(|v| !v.is_null())
                    || ctx.body.get("output").is_some_and(|v| !v.is_null())
                    || ctx
                        .body
                        .get("completed_steps")
                        .is_some_and(|v| !v.is_null())
                {
                    return Err(ApiError::bad(
                        "要求重试或拒绝任务不能同时交付结果或修改步骤",
                    ));
                }
                let mode = if ctx.b("retry_mode").is_empty() {
                    "revise"
                } else {
                    ctx.b("retry_mode")
                };
                let kind = if ctx.b("reason_type").is_empty() {
                    "customer_input"
                } else {
                    ctx.b("reason_type")
                };
                if !["revise", "reuse"].contains(&mode)
                    || !["customer_input", "external", "processor"].contains(&kind)
                {
                    return Err(ApiError::bad("重试方式或原因类型无效"));
                }
                progress(ctx, &j)?;
                files::purge_job_outputs(ctx, &j)?;
                let state = if action == "reject" {
                    "rejected"
                } else {
                    "needs_input"
                };
                ctx.conn.execute("UPDATE jobs SET state=?,message=?,content=NULL,result_json=NULL,retryable=0,claimed_by=NULL,lease=NULL,retry_mode=?,retry_reason_type=?,updated=? WHERE id=?",params![state,reason,mode,kind,now(),jid])?;
                ctx.conn.execute(
                    "UPDATE cards SET state=? WHERE id=?",
                    params![
                        if state == "rejected" {
                            "rejected"
                        } else if integer(&j, "revision_round", 0) > 0 {
                            "used"
                        } else {
                            "ready"
                        },
                        text(&j, "card_id")
                    ],
                )?;
                if is_flow {
                    flow::stop(ctx, &j, state)?;
                }
                event(
                    ctx,
                    &format!("fulfillment.{state}"),
                    &pid,
                    Some(&job(ctx, jid)?),
                )?;
            }
            "retry" => {
                if text(&j, "state") != "failed" {
                    return Err(ApiError::new(409, "只能放行失败的任务"));
                }
                ensure_card(ctx, &card(ctx, text(&j, "card_id"))?)?;
                ctx.conn
                    .execute("UPDATE jobs SET retryable=1 WHERE id=?", [jid])?;
            }
            _ => return Err(ApiError::bad("队列操作无效")),
        }
        if action == "claim" {
            let current = job(ctx, jid)?;
            if text(&current, "state") == "processing" {
                record_claim(ctx, &current, &who, Some(&s), None)?;
            }
        }
        audit(
            &ctx.conn,
            &who,
            &format!("job.{action}"),
            jid,
            p["shop_id"].as_str(),
        )?;
    }
    Ok(json!({"ok":true}))
}
fn events(ctx: &Context) -> Result<Value> {
    let manage = ctx.path.starts_with("/api/manage/");
    let s = auth::session(
        ctx,
        if manage {
            &["admin", "staff"]
        } else {
            &["admin"]
        },
        Some("events.manage"),
    )?;
    let pid = if manage {
        Some(scope(ctx, &s, ctx.q("product_id"))?)
    } else if !ctx.q("product_id").is_empty() {
        Some(scope(ctx, &s, ctx.q("product_id"))?)
    } else {
        None
    };
    let sid = auth::selected_shop(ctx, &s, ctx.q("shop_id"))?;
    if ctx.method.as_str() == "GET" {
        let limit = ctx.q("limit").parse::<i64>().unwrap_or(100).clamp(1, 500);
        return Ok(json!(rows(
            &ctx.conn,
            "SELECT e.id,e.type,e.job_id,e.product_id,e.created,o.state AS webhook_state,o.attempts,o.error FROM events e JOIN products p ON p.id=e.product_id LEFT JOIN outbox o ON o.id=e.id WHERE (? IS NULL OR e.product_id=?) AND (? IS NULL OR p.shop_id=?) ORDER BY e.created DESC LIMIT ?",
            params![pid, pid, sid, sid, limit]
        )?));
    }
    let eid = ctx
        .path
        .trim_end_matches("/retry")
        .rsplit('/')
        .next()
        .unwrap_or("");
    let e = required(
        &ctx.conn,
        "SELECT e.product_id,o.state FROM events e LEFT JOIN outbox o ON o.id=e.id WHERE e.id=?",
        [eid],
        "事件不存在",
    )?;
    auth::authorize_product(ctx, &s, text(&e, "product_id"))?;
    if pid.as_ref().is_some_and(|p| p != text(&e, "product_id")) {
        return Err(ApiError::new(403, "无权管理此商品的事件"));
    }
    if text(&e, "state") != "dead" {
        return Err(ApiError::new(409, "只能重试已停止投递的事件"));
    }
    ctx.conn.execute(
        "UPDATE outbox SET state='pending',attempts=0,due=?,finished_at=NULL WHERE id=?",
        params![now(), eid],
    )?;
    audit(&ctx.conn, &actor(&s), "webhook.retry", eid, sid.as_deref())?;
    Ok(json!({"ok":true}))
}
fn callback(ctx: &Context) -> Result<Value> {
    let parts = ctx
        .path
        .trim_start_matches("/api/callbacks/")
        .split('/')
        .collect::<Vec<_>>();
    if parts.len() != 2 {
        return Err(ApiError::new(404, "回调不存在"));
    }
    let (pid, jid) = (parts[0], parts[1]);
    let p = catalog::product(ctx, pid)?;
    if text(&p, "mode") != "webhook" {
        return Err(ApiError::new(403, "此商品不支持回调"));
    }
    let timestamp = ctx.header("x-extore-timestamp");
    let nonce = ctx.header("x-extore-nonce");
    let signature = ctx.header("x-extore-signature");
    if timestamp.parse::<i64>().is_err()
        || (now() - timestamp.parse::<i64>().unwrap_or(0) as f64).abs() > 300.0
        || nonce.is_empty()
        || nonce.len() > 128
    {
        return Err(ApiError::new(401, "回调签名无效或过期"));
    }
    let mut mac = Hmac::<Sha256>::new_from_slice(text(&p, "webhook_secret").as_bytes())
        .map_err(|_| ApiError::bad("回调密钥无效"))?;
    mac.update(timestamp.as_bytes());
    mac.update(b".");
    mac.update(nonce.as_bytes());
    mac.update(b".");
    mac.update(&ctx.raw_body);
    let expected = mac
        .finalize()
        .into_bytes()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>();
    if !bool::from(expected.as_bytes().ct_eq(signature.as_bytes())) {
        return Err(ApiError::new(401, "回调签名无效或过期"));
    }
    let j = job(ctx, jid)?;
    if text(&j, "product_id") != pid {
        return Err(ApiError::new(403, "任务不属于此商品"));
    }
    if flow::is_flow(ctx, &j)? {
        return Err(ApiError::new(
            409,
            "步骤流程必须使用绑定执行身份的私有 Worker v2 回调",
        ));
    }
    ctx.conn.execute(
        "DELETE FROM callback_nonces WHERE created<?",
        [now() - 600.0],
    )?;
    let key = digest(&format!("{pid}:{nonce}"));
    if row(
        &ctx.conn,
        "SELECT 1 FROM callback_nonces WHERE nonce=?",
        [&key],
    )?
    .is_some()
    {
        return Err(ApiError::new(409, "回调重复"));
    }
    let result = apply_update(ctx, jid, &ctx.body)?;
    ctx.conn.execute(
        "INSERT INTO callback_nonces VALUES (?,?)",
        params![key, now()],
    )?;
    job_view(ctx, &result, false)
}
pub fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    if let Some(r) = automation::handle(ctx)? {
        return Ok(Some(r));
    }
    if let Some(r) = private_worker::handle(ctx)? {
        return Ok(Some(r));
    }
    if let Some(r) = batch::handle(ctx)? {
        return Ok(Some(r));
    }
    if let Some(r) = flow::handle(ctx)? {
        return Ok(Some(r));
    }
    let result = if ctx.is("POST", "/api/exchange") {
        exchange(ctx)?
    } else if ctx.is("POST", "/api/receipt") {
        let list = batch_cards(ctx, ctx.b("token"))?;
        if !list.is_empty() {
            batch_receipt(ctx, &list)?
        } else {
            let c = grant(ctx, ctx.b("token"), None)?;
            let mut v = receipt_item(ctx, &c)?;
            v.as_object_mut().unwrap().remove("card_id");
            v.as_object_mut().unwrap().remove("suffix");
            v
        }
    } else if ctx.is("POST", "/api/redeem") {
        let list = batch_cards(ctx, ctx.b("token"))?;
        if !list.is_empty() {
            let items = ctx.body["items"]
                .as_array()
                .filter(|a| !a.is_empty() && a.len() <= 30)
                .ok_or_else(|| ApiError::bad("请为每张待兑换的卡密填写启动参数"))?;
            let mut seen = HashSet::new();
            for item in items {
                let cid = text(item, "card_id");
                if !seen.insert(cid) {
                    return Err(ApiError::bad("卡密重复提交"));
                }
                let c = list
                    .iter()
                    .find(|c| text(c, "id") == cid)
                    .ok_or_else(|| ApiError::new(404, "这张卡密不在此领取链接中"))?;
                submit(ctx, c, item.get("params").unwrap_or(&json!({})))?;
            }
            batch_receipt(ctx, &list)?
        } else {
            if ctx.body.get("items").is_some() {
                return Err(ApiError::bad("单张卡密请直接提交参数"));
            }
            let c = grant(ctx, ctx.b("token"), None)?;
            job_view(
                ctx,
                &submit(ctx, &c, ctx.body.get("params").unwrap_or(&json!({})))?,
                false,
            )?
        }
    } else if ctx.is("POST", "/api/receipt/revisions") {
        let c = grant(ctx, ctx.b("token"), ctx.body["card_id"].as_str())?;
        job_view(
            ctx,
            &entitlements::request_revision(ctx, &c, &ctx.body)?,
            false,
        )?
    } else if ctx.is("POST", "/api/retry") {
        let c = grant(ctx, ctx.b("token"), ctx.body["card_id"].as_str())?;
        let j = required(
            &ctx.conn,
            "SELECT * FROM jobs WHERE card_id=?",
            [text(&c, "id")],
            "任务不存在",
        )?;
        if text(&j, "state") != "needs_input" || text(&j, "retry_mode") != "reuse" {
            return Err(ApiError::new(409, "此任务不支持直接重试"));
        }
        job_view(
            ctx,
            &submit(ctx, &c, &json_field(&j, "params", json!({})))?,
            false,
        )?
    } else if ctx.is("POST", "/api/receipt/reveal") {
        let c = grant(ctx, ctx.b("token"), ctx.body["card_id"].as_str())?;
        let j = required(
            &ctx.conn,
            "SELECT * FROM jobs WHERE card_id=?",
            [text(&c, "id")],
            "尚无可领取内容",
        )?;
        let d = entitlements::select_delivery(ctx, &j, ctx.body["revision"].as_i64())?;
        let p = job_product(ctx, &j)?;
        if text(&p, "delivery") == "service" {
            json!({"content":null,"output":{}})
        } else {
            if text(&p, "view_policy") == "once" && flag(&d, "revealed") {
                return Err(ApiError::new(410, "内容已领取，无法再次查看"));
            }
            if d["content"].is_null() && d["result_json"].is_null() {
                return Err(ApiError::new(410, "内容已销毁"));
            }
            let output = if d["result_json"].is_null() {
                json!({"content":d["content"]})
            } else {
                json_field(&d, "result_json", json!({}))
            };
            let output_files =
                files::release_output_files(ctx, &entitlements::delivery_row(&j, &d), &p)?;
            if flag(&d, "current") {
                ctx.conn.execute("UPDATE jobs SET revealed=1,content=CASE WHEN ? THEN NULL ELSE content END,result_json=CASE WHEN ? THEN NULL ELSE result_json END WHERE id=?",params![text(&p,"view_policy")=="once",text(&p,"view_policy")=="once",text(&j,"id")])?;
            } else {
                ctx.conn.execute(
                    "UPDATE job_delivery_versions SET revealed=1 WHERE job_id=? AND revision=?",
                    params![text(&j, "id"), integer(&d, "revision", 0)],
                )?;
            }
            event(ctx, "delivery.viewed", text(&j, "product_id"), Some(&j))?;
            if text(&p, "view_policy") == "once" {
                flow::destroy(ctx, &j)?;
                ctx.conn.execute(
                    "UPDATE text_card_payloads SET ciphertext=NULL,size=0 WHERE card_id=?",
                    [text(&c, "id")],
                )?;
            }
            let mut v = json!({"content":d["content"],"output":output});
            if !entitlements::frozen_policy(ctx, text(&c, "id"))?.is_null() {
                v["revision"] = d["revision"].clone();
            }
            if !output_files.is_empty() {
                v["files"] = json!(output_files);
            }
            v
        }
    } else if ctx.is("POST", "/api/receipt/destroy") {
        let c = grant(ctx, ctx.b("token"), ctx.body["card_id"].as_str())?;
        let j = required(
            &ctx.conn,
            "SELECT * FROM jobs WHERE card_id=?",
            [text(&c, "id")],
            "任务不存在",
        )?;
        if !matches!(text(&j, "state"), "succeeded" | "destroyed")
            && !entitlements::has_previous(ctx, &j)?
        {
            return Err(ApiError::new(409, "尚无可销毁内容"));
        }
        if text(&j, "state") != "destroyed" {
            files::purge_job_files(ctx, &j)?;
            flow::destroy(ctx, &j)?;
            entitlements::destroy(ctx, &j)?;
            ctx.conn.execute(
                "UPDATE text_card_payloads SET ciphertext=NULL,size=0 WHERE card_id=?",
                [text(&c, "id")],
            )?;
            ctx.conn.execute("UPDATE jobs SET state='destroyed',content=NULL,result_json=NULL,params='{}',message='',claimed_by=NULL,lease=NULL,attempt=attempt+1,updated=? WHERE id=?",params![now(),text(&j,"id")])?;
            for e in rows(
                &ctx.conn,
                "SELECT id,payload FROM events WHERE job_id=?",
                [text(&j, "id")],
            )? {
                let mut payload = parse(text(&e, "payload"))?;
                if let Some(data) = payload["data"].as_object_mut() {
                    data.remove("params");
                    if let Some(revision) = data.get_mut("revision").and_then(Value::as_object_mut)
                    {
                        revision.remove("message");
                    }
                }
                ctx.conn.execute(
                    "UPDATE events SET payload=? WHERE id=?",
                    params![payload.to_string(), text(&e, "id")],
                )?;
            }
            event(
                ctx,
                "delivery.destroyed",
                text(&j, "product_id"),
                Some(&job(ctx, text(&j, "id"))?),
            )?;
            audit(
                &ctx.conn,
                "customer",
                "delivery.destroy",
                text(&j, "id"),
                None,
            )?;
        }
        json!({"ok":true})
    } else if ctx.is("GET", "/api/manage/jobs") {
        queue(ctx)?
    } else if ctx.is("POST", "/api/manage/batch") {
        queue_batch(ctx)?
    } else if (ctx.method.as_str() == "GET"
        && (ctx.path == "/api/manage/events" || ctx.path == "/api/admin/events"))
        || (ctx.method.as_str() == "POST"
            && (ctx.path.starts_with("/api/manage/events/")
                || ctx.path.starts_with("/api/admin/events/"))
            && ctx.path.ends_with("/retry"))
    {
        events(ctx)?
    } else if ctx.method.as_str() == "POST" && ctx.path.starts_with("/api/callbacks/") {
        callback(ctx)?
    } else {
        return Ok(None);
    };
    Ok(Some(Reply::json(result)))
}
pub fn release_claims(ctx: &Context, who: &str) -> Result<()> {
    for j in rows(
        &ctx.conn,
        "SELECT j.* FROM jobs j JOIN products p ON p.id=j.product_id WHERE j.state='processing' AND j.claimed_by=? AND json_extract(p.config,'$.mode')='manual'",
        [who],
    )? {
        if flow::is_flow(ctx, &j)? {
            flow::release_claim(ctx, &j, who)?;
        } else {
            ctx.conn.execute(
                "UPDATE jobs SET state='queued',claimed_by=NULL,lease=NULL,updated=? WHERE id=?",
                params![now(), text(&j, "id")],
            )?;
        }
    }
    Ok(())
}

//! Immutable issuance entitlements and version-specific delivery authority.
use super::*;
pub fn round_attempt(j: &Value) -> i64 {
    integer(j, "attempt", 1) - integer(j, "revision_start_attempt", 1) + 1
}
pub fn frozen_policy(ctx: &Context, cid: &str) -> Result<Value> {
    Ok(row(
        &ctx.conn,
        "SELECT policy FROM card_entitlements WHERE card_id=?",
        [cid],
    )?
    .map(|v| json_field(&v, "policy", Value::Null))
    .unwrap_or(Value::Null))
}
pub fn attributes(ctx: &Context, c: &Value) -> Result<Value> {
    if let Some(r) = row(
        &ctx.conn,
        "SELECT attributes FROM card_entitlements WHERE card_id=?",
        [text(c, "id")],
    )? {
        Ok(json_field(&r, "attributes", json!({})))
    } else {
        Ok(catalog::card_variant(ctx, c)?["attributes"].clone())
    }
}
pub fn card_view(ctx: &Context, c: &Value, j: Option<&Value>) -> Result<Value> {
    let policy = frozen_policy(ctx, text(c, "id"))?;
    let attrs = attributes(ctx, c)?;
    if policy.is_null() {
        return Ok(json!({"card_attributes":attrs,"entitlements":null}));
    }
    let total = attrs
        .get(text(&policy, "attribute_key"))
        .and_then(Value::as_i64)
        .unwrap_or(0);
    if !(0..=1000).contains(&total) {
        return Err(ApiError::new(409, "卡密的修改权益配置无效"));
    }
    let saved = if j.is_none() {
        row(
            &ctx.conn,
            "SELECT * FROM jobs WHERE card_id=?",
            [text(c, "id")],
        )?
    } else {
        None
    };
    let j = j.or(saved.as_ref());
    let used = if let Some(j) = j {
        integer(
            &required(
                &ctx.conn,
                "SELECT COUNT(*) AS n FROM job_revision_requests WHERE job_id=?",
                [text(j, "id")],
                "任务不存在",
            )?,
            "n",
            0,
        )
    } else {
        0
    };
    let reason = if text(c, "state") == "revoked" {
        Some("revoked")
    } else if text(c, "state") == "rejected" || j.is_some_and(|j| text(j, "state") == "rejected") {
        Some("rejected")
    } else if j.is_some_and(|j| text(j, "state") == "destroyed") {
        Some("destroyed")
    } else if expired(ctx, text(c, "id"))? {
        Some("expired")
    } else if row(
        &ctx.conn,
        "SELECT 1 FROM products p JOIN shops s ON s.id=p.shop_id WHERE p.id=? AND s.enabled=1",
        [text(c, "product_id")],
    )?
    .is_none()
    {
        Some("shop_disabled")
    } else if j.is_none() {
        Some("not_delivered")
    } else if j.is_some_and(|j| text(j, "state") != "succeeded") {
        if j.is_some_and(|j| matches!(text(j, "state"), "queued" | "processing")) {
            Some("in_progress")
        } else {
            Some("not_delivered")
        }
    } else if used >= total {
        Some("exhausted")
    } else {
        None
    };
    Ok(
        json!({"card_attributes":attrs,"entitlements":{"attribute_key":policy["attribute_key"],"label":policy["label"],"total":total,"used":used,"remaining":(total-used).max(0),"can_request":reason.is_none(),"reason":reason}}),
    )
}
pub fn current_delivery(j: &Value) -> Value {
    json!({"job_id":j["id"],"revision":integer(j,"revision_round",0),"attempt":j["attempt"],"content":j["content"],"result_json":j["result_json"],"revealed":j["revealed"],"created":j["updated"],"current":true})
}
pub fn select_delivery(ctx: &Context, j: &Value, revision: Option<i64>) -> Result<Value> {
    if text(j, "state") == "destroyed" {
        return Err(ApiError::new(409, "尚无可领取内容"));
    }
    if text(j, "state") == "succeeded"
        && revision.is_none_or(|v| v == integer(j, "revision_round", 0))
    {
        return Ok(current_delivery(j));
    }
    let mut d=row(&ctx.conn,"SELECT * FROM job_delivery_versions WHERE job_id=? AND (? IS NULL OR revision=?) ORDER BY revision DESC LIMIT 1",params![text(j,"id"),revision,revision])?.ok_or_else(||ApiError::new(if revision.is_some(){404}else{409},"尚无可领取内容或交付版本不存在"))?;
    d["current"] = json!(false);
    Ok(d)
}
pub fn delivery_row(j: &Value, d: &Value) -> Value {
    let mut r = j.clone();
    merge(&mut r, d.clone());
    r["id"] = j["id"].clone();
    r["state"] = json!("succeeded");
    r
}
pub fn has_previous(ctx: &Context, j: &Value) -> Result<bool> {
    Ok(row(
        &ctx.conn,
        "SELECT 1 FROM job_delivery_versions WHERE job_id=? LIMIT 1",
        [text(j, "id")],
    )?
    .is_some())
}
pub fn preserved_output_ids(ctx: &Context, j: &Value) -> Result<Vec<String>> {
    Ok(rows(&ctx.conn,"SELECT f.id FROM job_files f WHERE f.job_id=? AND f.kind='output' AND f.bound=1 AND EXISTS(SELECT 1 FROM job_delivery_versions v WHERE v.job_id=f.job_id AND v.attempt=f.attempt AND v.result_json IS NOT NULL)",[text(j,"id")])?.iter().map(|v|text(v,"id").to_owned()).collect())
}
pub fn attachment_ids(field: &Value, value: &str) -> Vec<String> {
    if matches!(text(field, "type"), "file" | "image") {
        if value.is_empty() {
            Vec::new()
        } else {
            vec![value.to_owned()]
        }
    } else if text(field, "type") == "images" {
        serde_json::from_str(value).unwrap_or_default()
    } else {
        Vec::new()
    }
}
pub fn file_delivery(
    ctx: &Context,
    j: &Value,
    file: &Value,
    revision: Option<i64>,
) -> Result<Option<Value>> {
    if text(j, "state") == "destroyed" {
        return Err(ApiError::new(410, "文件已销毁或无法领取"));
    }
    let mut versions = rows(
        &ctx.conn,
        "SELECT * FROM job_delivery_versions WHERE job_id=? AND attempt=?",
        params![text(j, "id"), integer(file, "attempt", 0)],
    )?;
    if text(j, "state") == "succeeded" {
        versions.push(current_delivery(j));
    }
    let p = job_product(ctx, j)?;
    for mut d in versions {
        if revision.is_some_and(|r| r != integer(&d, "revision", 0))
            || d["attempt"] != file["attempt"]
        {
            continue;
        }
        let output = json_field(&d, "result_json", json!({}));
        if p["outputs"].as_array().is_some_and(|fields| {
            fields.iter().any(|f| {
                text(f, "key") == text(file, "field_key")
                    && attachment_ids(f, text(&output, text(f, "key")))
                        .iter()
                        .any(|id| id == text(file, "id"))
            })
        }) {
            if d.get("current").is_none() {
                d["current"] = json!(false);
            }
            return Ok(Some(d));
        }
    }
    Ok(None)
}
pub fn job_context(ctx: &Context, j: &Value, deliveries: bool) -> Result<Value> {
    let c = card(ctx, text(j, "card_id"))?;
    let mut result = card_view(ctx, &c, Some(j))?;
    result["revision"] = json!({"current":integer(j,"revision_round",0),"message":text(j,"revision_message"),"is_revision":integer(j,"revision_round",0)>0});
    result["last_delivery"] = if text(j, "state") == "succeeded" {
        json!({"revision":integer(j,"revision_round",0),"attempt":j["attempt"],"created":j["updated"]})
    } else {
        row(&ctx.conn,"SELECT revision,attempt,created FROM job_delivery_versions WHERE job_id=? ORDER BY revision DESC LIMIT 1",[text(j,"id")])?.unwrap_or(Value::Null)
    };
    if deliveries {
        let mut list = rows(
            &ctx.conn,
            "SELECT v.revision,v.attempt,v.created,v.revealed,EXISTS(SELECT 1 FROM job_files f WHERE f.job_id=v.job_id AND f.attempt=v.attempt AND f.kind='output' AND f.bound=1 AND f.content IS NOT NULL) AS has_files FROM job_delivery_versions v WHERE v.job_id=? ORDER BY v.revision",
            [text(j, "id")],
        )?;
        for item in &mut list {
            item["revealed"] = json!(flag(item, "revealed"));
            item["has_files"] = json!(flag(item, "has_files"));
        }
        if text(j, "state") == "succeeded" {
            let p = job_product(ctx, j)?;
            let output = json_field(j, "result_json", json!({}));
            let has = p["outputs"].as_array().is_some_and(|a| {
                a.iter()
                    .any(|f| !attachment_ids(f, text(&output, text(f, "key"))).is_empty())
            });
            list.push(json!({"revision":integer(j,"revision_round",0),"attempt":j["attempt"],"created":j["updated"],"revealed":flag(j,"revealed"),"has_files":has}));
        }
        result["deliveries"] = json!(list);
    }
    Ok(result)
}
pub fn allocated_bytes(ctx: &Context, cid: &str) -> Result<i64> {
    let old = required(
        &ctx.conn,
        "SELECT COALESCE(SUM(COALESCE(length(CAST(v.content AS BLOB)),0)+COALESCE(length(CAST(v.result_json AS BLOB)),0)),0) AS n FROM job_delivery_versions v JOIN jobs j ON j.id=v.job_id WHERE j.card_id=? AND v.result_json IS NOT NULL",
        [cid],
        "容量读取失败",
    )?;
    let messages = required(
        &ctx.conn,
        "SELECT COALESCE(SUM(length(CAST(r.message AS BLOB))),0) AS n FROM job_revision_requests r JOIN jobs j ON j.id=r.job_id WHERE j.card_id=?",
        [cid],
        "容量读取失败",
    )?;
    let current = required(
        &ctx.conn,
        "SELECT COALESCE(SUM(COALESCE(length(CAST(content AS BLOB)),0)+COALESCE(length(CAST(result_json AS BLOB)),0)),0) AS n FROM jobs WHERE card_id=? AND revision_round>0 AND result_json IS NOT NULL",
        [cid],
        "容量读取失败",
    )?;
    Ok(integer(&old, "n", 0) + integer(&messages, "n", 0) + integer(&current, "n", 0))
}
pub fn request_revision(ctx: &Context, c: &Value, request: &Value) -> Result<Value> {
    let allowed = [
        "token",
        "card_id",
        "revision",
        "request_id",
        "expected_revision",
        "message",
    ];
    if request
        .as_object()
        .is_none_or(|o| o.keys().any(|k| !allowed.contains(&k.as_str())))
        || request["token"]
            .as_str()
            .is_none_or(|s| s.chars().count() > 100)
        || (!request["card_id"].is_null()
            && request["card_id"]
                .as_str()
                .is_none_or(|s| s.chars().count() > 80))
        || !request["revision"].is_null()
    {
        return Err(ApiError::new(422, "修改请求字段无效"));
    }
    let rid = text(request, "request_id");
    let uuid = uuid::Uuid::parse_str(rid)
        .map_err(|_| ApiError::new(422, "修改请求编号必须是随机 UUID v4"))?;
    if uuid.get_version_num() != 4 || uuid.to_string() != rid {
        return Err(ApiError::new(422, "修改请求编号必须是规范的随机 UUID v4"));
    }
    let expected = request["expected_revision"]
        .as_i64()
        .filter(|x| (0..=1000).contains(x))
        .ok_or_else(|| ApiError::new(422, "修改轮次无效"))?;
    let message = text(request, "message").trim();
    if message.is_empty() || message.chars().count() > 10000 {
        return Err(ApiError::new(
            422,
            "请填写有效的修改建议和 expected_revision",
        ));
    }
    require_enabled(ctx, text(c, "product_id"))?;
    let j = required(
        &ctx.conn,
        "SELECT * FROM jobs WHERE card_id=?",
        [text(c, "id")],
        "请先完成首次兑换再申请修改",
    )?;
    if text(&j, "state") == "destroyed" || matches!(text(c, "state"), "revoked" | "rejected") {
        return Err(ApiError::new(
            410,
            "卡密已撤销、拒绝或交付已销毁，不能申请修改",
        ));
    }
    let key = std::fs::read(ctx.app.data.join("issuance.key"))?;
    let mut mac =
        Hmac::<Sha256>::new_from_slice(&key).map_err(|_| ApiError::bad("权益密钥无效"))?;
    mac.update(serde_json::to_string(&json!([j["id"], expected, message]))?.as_bytes());
    let fingerprint = mac
        .finalize()
        .into_bytes()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>();
    if let Some(previous) = row(
        &ctx.conn,
        "SELECT request_digest FROM job_revision_requests WHERE job_id=? AND request_id=?",
        params![text(&j, "id"), rid],
    )? {
        if !bool::from(
            text(&previous, "request_digest")
                .as_bytes()
                .ct_eq(fingerprint.as_bytes()),
        ) {
            return Err(ApiError::new(409, "同一个修改请求编号不能用于不同内容"));
        }
        return Ok(j);
    }
    let state = card_view(ctx, c, Some(&j))?["entitlements"].clone();
    if state.is_null() {
        return Err(ApiError::new(409, "此卡密未配置交付后修改权益"));
    }
    if expected != integer(&j, "revision_round", 0) {
        return Err(ApiError::new(409, "交付轮次已改变，请刷新后再提交修改"));
    }
    if !flag(&state, "can_request") {
        return Err(ApiError::new(
            409,
            format!("当前不能申请修改：{}", text(&state, "reason")),
        ));
    }
    let p = job_product(ctx, &j)?;
    if text(&p, "view_policy") != "repeat"
        || text(&p, "delivery") != "content"
        || text(&p, "mode") == "stock"
        || !p["task_flow"].is_null()
    {
        return Err(ApiError::new(409, "此商品的处理方式不支持交付后修改"));
    }
    let mut extra = message.len() as i64;
    if expected == 0 {
        extra += (text(&j, "content").len() + text(&j, "result_json").len()) as i64;
    }
    let used = integer(
        &required(
            &ctx.conn,
            "SELECT COALESCE(SUM(size),0) AS n FROM job_files WHERE card_id=? AND content IS NOT NULL",
            [text(c, "id")],
            "容量读取失败",
        )?,
        "n",
        0,
    );
    if used + allocated_bytes(ctx, text(c, "id"))? + extra > 100 * 1024 * 1024 {
        return Err(ApiError::new(
            413,
            "此卡密的交付历史和文件总量已达到容量上限",
        ));
    }
    files::check_storage_quota(ctx, extra as u64, text(c, "product_id"))?;
    ctx.conn.execute("INSERT INTO job_delivery_versions(job_id,revision,attempt,content,result_json,revealed,created) VALUES (?,?,?,?,?,?,?)",params![text(&j,"id"),expected,integer(&j,"attempt",1),j["content"].as_str(),j["result_json"].as_str(),flag(&j,"revealed"),number(&j,"updated")])?;
    ctx.conn.execute("INSERT INTO job_revision_requests(job_id,request_id,request_digest,revision,message,created) VALUES (?,?,?,?,?,?)",params![text(&j,"id"),rid,fingerprint,expected+1,message,now()])?;
    ctx.conn.execute("UPDATE jobs SET revision_round=?,revision_message=?,revision_start_attempt=attempt+1,attempt=attempt+1,state='queued',content=NULL,result_json=NULL,revealed=0,message='',progress=0,completed_steps='[]',retryable=0,claimed_by=NULL,lease=NULL,updated=? WHERE id=?",params![expected+1,message,now(),text(&j,"id")])?;
    ctx.conn
        .execute("UPDATE cards SET state='used' WHERE id=?", [text(c, "id")])?;
    let j = job(ctx, text(&j, "id"))?;
    files::bind_inputs(ctx, &j, &p, &json_field(&j, "params", json!({})))?;
    event(ctx, "revision.requested", text(&j, "product_id"), Some(&j))?;
    audit(
        &ctx.conn,
        "customer",
        "revision.request",
        text(&j, "id"),
        p["shop_id"].as_str(),
    )?;
    Ok(j)
}
pub fn destroy(ctx: &Context, j: &Value) -> Result<()> {
    ctx.conn.execute(
        "UPDATE job_delivery_versions SET content=NULL,result_json=NULL WHERE job_id=?",
        [text(j, "id")],
    )?;
    ctx.conn.execute(
        "UPDATE job_revision_requests SET message='',request_digest='' WHERE job_id=?",
        [text(j, "id")],
    )?;
    ctx.conn.execute(
        "UPDATE jobs SET revision_message='' WHERE id=?",
        [text(j, "id")],
    )?;
    Ok(())
}

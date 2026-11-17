//! Partial batch API isolates each card with a SQLite savepoint.
use super::*;
fn isolated<T>(ctx: &Context, index: usize, f: impl FnOnce() -> Result<T>) -> Result<T> {
    let name = format!("redemption_item_{index}");
    ctx.conn.execute_batch(&format!("SAVEPOINT {name}"))?;
    match f() {
        Ok(v) => {
            ctx.conn
                .execute_batch(&format!("RELEASE SAVEPOINT {name}"))?;
            Ok(v)
        }
        Err(e) => {
            ctx.conn.execute_batch(&format!(
                "ROLLBACK TO SAVEPOINT {name}; RELEASE SAVEPOINT {name}"
            ))?;
            Err(e)
        }
    }
}
fn suffix(raw: &str) -> String {
    let s = raw
        .to_uppercase()
        .chars()
        .filter(char::is_ascii_alphanumeric)
        .collect::<String>();
    s.chars()
        .rev()
        .take(6)
        .collect::<String>()
        .chars()
        .rev()
        .collect()
}
fn base(index: usize, suffix_: &str, status: &str) -> Value {
    json!({"index":index,"suffix":suffix(suffix_),"status":status,"accepted":false,"error":null,"http_status":null})
}
fn rejected(c: Option<&Value>, index: usize, suffix_: &str, e: ApiError) -> Value {
    let used = c.is_some_and(|c| text(c, "state") == "used")
        && e.status == 410
        && e.message == "卡密已使用，无法再次领取";
    let mut v = base(index, suffix_, if used { "used" } else { "invalid" });
    v["error"] = json!(if e.status >= 500 {
        "这张卡密暂时无法处理，请稍后重试".to_owned()
    } else {
        e.message.chars().take(500).collect()
    });
    v["http_status"] = json!(e.status);
    v
}
fn view(ctx: &Context, c: &Value, index: usize, fallback: &str, screening: bool) -> Result<Value> {
    if screening {
        super::routes::screen(ctx, c)?;
    } else {
        if text(c, "state") == "revoked" {
            return Err(ApiError::new(410, "卡密已撤销"));
        }
        if row(
            &ctx.conn,
            "SELECT 1 FROM jobs WHERE card_id=?",
            [text(c, "id")],
        )?
        .is_none()
        {
            ensure_card(ctx, c)?;
            require_enabled(ctx, text(c, "product_id"))?;
        }
    }
    let mut v = super::routes::receipt_item(ctx, c)?;
    let state = if v["job"].is_null() {
        "valid"
    } else if flag(&v["job"], "can_retry") {
        "needs_retry"
    } else {
        "used"
    };
    let mut b = base(
        index,
        if text(&v, "suffix").is_empty() {
            fallback
        } else {
            text(&v, "suffix")
        },
        state,
    );
    b["accepted"] = json!(true);
    v.as_object_mut().unwrap().remove("suffix");
    merge(&mut b, v);
    Ok(b)
}
fn summary(items: &[Value]) -> Value {
    let mut s =
        json!({"total":items.len(),"accepted":items.iter().filter(|i|flag(i,"accepted")).count()});
    for state in ["valid", "used", "needs_retry", "invalid", "duplicate"] {
        s[state] = json!(items.iter().filter(|i| text(i, "status") == state).count());
    }
    s
}
fn load(ctx: &Context, value: &str) -> Result<Vec<Value>> {
    let list = rows(
        &ctx.conn,
        "SELECT c.*,bc.position FROM receipt_batches b JOIN receipt_batch_cards bc ON bc.digest=b.digest JOIN cards c ON c.id=bc.card_id WHERE b.digest=? AND b.expires>? ORDER BY bc.position",
        params![digest(value), now()],
    )?;
    if list.is_empty() {
        return Err(ApiError::new(404, "批量领取凭证无效或已过期"));
    }
    Ok(list)
}
fn snapshot(ctx: &Context, list: &[Value]) -> Result<Value> {
    let mut items = Vec::new();
    for c in list {
        let index = integer(c, "position", 0) as usize;
        let item = isolated(ctx, index, || view(ctx, c, index, "", false))
            .unwrap_or_else(|e| rejected(Some(c), index, "", e));
        items.push(item);
    }
    Ok(json!({"batch":true,"partial":true,"summary":summary(&items),"items":items}))
}
pub fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    if ctx.method.as_str() != "POST"
        || !matches!(
            ctx.path.as_str(),
            "/api/batch/exchange" | "/api/batch/receipt" | "/api/batch/redeem"
        )
    {
        return Ok(None);
    }
    let allowed: &[&str] = match ctx.path.as_str() {
        "/api/batch/exchange" => &["code"],
        "/api/batch/receipt" => &["token"],
        _ => &["token", "items"],
    };
    if ctx
        .body
        .as_object()
        .is_none_or(|o| o.keys().any(|k| !allowed.contains(&k.as_str())))
    {
        return Err(ApiError::bad("请求字段无效"));
    }
    let result = if ctx.path == "/api/batch/exchange" {
        let codes = super::routes::codes(ctx, ctx.b("code"))?;
        let mut seen = std::collections::HashMap::new();
        let mut items = Vec::new();
        let mut accepted = Vec::new();
        for (index, code) in codes.iter().enumerate() {
            let shown = suffix(code);
            let key = match catalog::unwrap_code(ctx, code) {
                Ok(code) => catalog::card_digest(&code),
                Err(_) => {
                    items.push(rejected(None, index, &shown, ApiError::bad("卡密格式无效")));
                    continue;
                }
            };
            if let Some(previous) = seen.get(&key) {
                let mut v = base(index, &shown, "duplicate");
                v["error"] = json!("卡密重复输入");
                v["duplicate_of"] = json!(previous);
                items.push(v);
                continue;
            }
            seen.insert(key.clone(), index);
            let c = row(&ctx.conn, "SELECT * FROM cards WHERE digest=?", [key])?;
            let result = isolated(ctx, index, || {
                let c = c.as_ref().ok_or_else(|| ApiError::new(404, "卡密不存在"))?;
                let v = view(ctx, c, index, &shown, true)?;
                super::routes::verified(ctx, c)?;
                Ok(v)
            });
            match result {
                Ok(v) => {
                    accepted.push((text(c.as_ref().unwrap(), "id").to_owned(), index));
                    items.push(v);
                }
                Err(e) => items.push(rejected(c.as_ref(), index, &shown, e)),
            }
        }
        let value = if accepted.is_empty() {
            None
        } else {
            let value = token();
            let key = digest(&value);
            super::routes::cleanup(ctx)?;
            ctx.conn.execute(
                "INSERT INTO receipt_batches(digest,expires,created) VALUES (?,?,?)",
                params![key, now() + 30.0 * 86400.0, now()],
            )?;
            for (cid, index) in accepted {
                ctx.conn.execute(
                    "INSERT INTO receipt_batch_cards(digest,card_id,position) VALUES (?,?,?)",
                    params![key, cid, index as i64],
                )?;
            }
            Some(value)
        };
        json!({"batch":true,"partial":true,"token":value,"summary":summary(&items),"items":items})
    } else {
        let value = ctx.b("token");
        if value.is_empty() || value.len() > 100 {
            return Err(ApiError::bad("请提供有效的领取凭证"));
        }
        let list = load(ctx, value)?;
        if ctx.path == "/api/batch/receipt" {
            snapshot(ctx, &list)?
        } else {
            let supplied = ctx.body["items"]
                .as_array()
                .filter(|a| !a.is_empty() && a.len() <= 30)
                .ok_or_else(|| ApiError::bad("请逐卡提交参数，一次最多提交 30 张"))?;
            let mut results = Vec::new();
            let mut seen = HashSet::new();
            for (index, item) in supplied.iter().enumerate() {
                let mut result = json!({"index":index,"status":"error","error":null});
                let submitted = isolated(ctx, index, || {
                    if item.as_object().is_none_or(|o| {
                        o.len() != 2 || !o.contains_key("card_id") || !o.contains_key("params")
                    }) {
                        return Err(ApiError::bad("每张卡密必须单独提供 card_id 和 params"));
                    }
                    let cid = text(item, "card_id");
                    let c = list
                        .iter()
                        .find(|c| text(c, "id") == cid)
                        .ok_or_else(|| ApiError::new(404, "这张卡密不在此领取链接中"))?;
                    result["card_id"] = json!(cid);
                    if !seen.insert(cid.to_owned()) {
                        result["status"] = json!("duplicate");
                        return Err(ApiError::bad("卡密重复提交"));
                    }
                    let params = &item["params"];
                    if params.as_object().is_none_or(|o| {
                        o.len() > 100
                            || o.iter().any(|(k, v)| {
                                k.is_empty()
                                    || k.len() > 100
                                    || v.as_str().is_none_or(|s| s.chars().count() > 10000)
                            })
                    }) {
                        return Err(ApiError::bad("每张卡密的 params 必须是独立的文本参数对象"));
                    }
                    view(ctx, c, integer(c, "position", 0) as usize, "", true)?;
                    let previous = row(&ctx.conn, "SELECT state FROM jobs WHERE card_id=?", [cid])?;
                    let j = submit(ctx, c, params)?;
                    result["job"] = job_view(ctx, &j, false)?;
                    result["status"] = json!(if previous
                        .as_ref()
                        .is_some_and(|p| !matches!(text(p, "state"), "failed" | "needs_input"))
                    {
                        "unchanged"
                    } else {
                        "submitted"
                    });
                    Ok(())
                });
                if let Err(e) = submitted {
                    result["error"] = json!(if e.status >= 500 {
                        "这张卡密暂时无法处理，请稍后重试".to_owned()
                    } else {
                        e.message.chars().take(500).collect()
                    });
                    result["http_status"] = json!(e.status);
                    result.as_object_mut().unwrap().remove("job");
                } else {
                    result["http_status"] = json!(200);
                }
                results.push(result);
            }
            let mut response = snapshot(ctx, &list)?;
            response["submission_summary"] = json!({"total":results.len(),"succeeded":results.iter().filter(|r|matches!(text(r,"status"),"submitted"|"unchanged")).count(),"failed":results.iter().filter(|r|matches!(text(r,"status"),"error"|"duplicate")).count()});
            response["results"] = json!(results);
            response
        }
    };
    Ok(Some(Reply::json(result)))
}

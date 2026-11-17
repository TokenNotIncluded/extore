use super::cards::{INVENTORY, scope};
use crate::{auth, common::*};
use rusqlite::{named_params, params};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

fn batch(ctx: &Context, pid: &str, bid: &str) -> Result<Value> {
    if bid == "legacy" {
        let mut b = required(
            &ctx.conn,
            "SELECT COUNT(*) AS count,MIN(c.created) AS created FROM cards c LEFT JOIN card_meta m ON m.card_id=c.id WHERE c.product_id=? AND m.batch_id IS NULL AND m.batch_deleted_at IS NULL",
            [pid],
            "批次不存在",
        )?;
        if integer(&b, "count", 0) == 0 {
            return Err(ApiError::new(404, "批次不存在"));
        }
        b["id"] = json!(bid);
        b["label"] = json!("历史卡密");
        b["product_id"] = json!(pid);
        b["deleted_at"] = Value::Null;
        return Ok(b);
    }
    let b = required(
        &ctx.conn,
        "SELECT id,label,created,count,product_id,deleted_at FROM card_batches WHERE id=? AND product_id=?",
        params![bid, pid],
        "批次不存在",
    )?;
    if row(&ctx.conn,"SELECT 1 AS found FROM card_meta m JOIN cards c ON c.id=m.card_id WHERE m.batch_id=? AND c.product_id!=? LIMIT 1",params![bid,pid])?.is_some(){return Err(ApiError::new(409,"批次关联的商品范围异常"));}
    Ok(b)
}
fn membership() -> &'static str {
    "c.product_id=:pid AND ((:bid='legacy' AND m.batch_id IS NULL AND m.batch_deleted_at IS NULL) OR m.batch_id=:bid)"
}
fn preview(ctx: &Context, pid: &str, bid: &str, action: &str) -> Result<Value> {
    let b = batch(ctx, pid, bid)?;
    let deleted = !b["deleted_at"].is_null();
    if (action == "purge") != deleted {
        return Err(ApiError::new(
            409,
            if action == "purge" {
                "请先把批次移入回收站，再清空"
            } else {
                "批次已经在回收站"
            },
        ));
    }
    let from = format!(
        "FROM cards c LEFT JOIN card_meta m ON m.card_id=c.id LEFT JOIN jobs j ON j.card_id=c.id WHERE {}",
        membership()
    );
    let stats = required(
        &ctx.conn,
        &format!(
            "SELECT COUNT(*) AS total,SUM(j.id IS NULL) AS unreferenced,SUM(c.state='ready' AND (j.id IS NULL OR j.state IN ('failed','needs_input'))) AS revocable,SUM(j.state IN ('queued','processing','waiting')) AS in_progress {from}"
        ),
        named_params! {":pid":pid,":bid":bid},
        "批次不存在",
    )?;
    let mut hash = Sha256::new();
    hash.update(
        json!(["extore.card-batch.v1", pid, bid, action, b["deleted_at"]])
            .to_string()
            .as_bytes(),
    );
    for r in rows(
        &ctx.conn,
        &format!(
            "SELECT c.id AS cid,c.state AS cs,j.id AS jid,j.state AS js,j.attempt,j.updated,m.first_verified,m.batch_deleted_at,m.batch_previous_state {from} ORDER BY c.id"
        ),
        named_params! {":pid":pid,":bid":bid},
    )? {
        hash.update(
            json!([
                r["cid"],
                r["cs"],
                r["jid"],
                r["js"],
                r["attempt"],
                r["updated"],
                r["first_verified"],
                r["batch_deleted_at"],
                r["batch_previous_state"]
            ])
            .to_string()
            .as_bytes(),
        );
    }
    for r in rows(
        &ctx.conn,
        &format!(
            "SELECT f.id,f.size,f.created,f.job_id,f.bound,f.released,f.consumed FROM job_files f JOIN cards c ON c.id=f.card_id LEFT JOIN card_meta m ON m.card_id=c.id WHERE {} ORDER BY f.id",
            membership()
        ),
        named_params! {":pid":pid,":bid":bid},
    )? {
        hash.update(
            json!([
                r["id"],
                r["size"],
                r["created"],
                r["job_id"],
                r["bound"],
                r["released"],
                r["consumed"]
            ])
            .to_string()
            .as_bytes(),
        );
    }
    let total = integer(&stats, "total", 0);
    let removable = if action == "purge" {
        integer(&stats, "unreferenced", 0)
    } else {
        0
    };
    Ok(
        json!({"action":action,"batch":{"id":bid,"label":b["label"],"total":total,"deleted":deleted},"delete_count":removable,"retain_count":total-removable,"revocable_count":integer(&stats,"revocable",0),"in_progress":integer(&stats,"in_progress",0),"revision":format!("{:x}",hash.finalize()),"explanation":if action=="purge"{"清空会永久删除未创建任务的卡密、预上传附件及文本库存；已创建任务和交付保持可领取。"}else{"批次移入回收站，未开始及可重试卡密停止兑换；正在处理的任务和已有交付不受影响。"}}),
    )
}
fn list(ctx: &Context, pid: &str) -> Result<Value> {
    let view = if ctx.q("view").is_empty() {
        "active"
    } else {
        ctx.q("view")
    };
    if !matches!(view, "active" | "deleted") {
        return Err(ApiError::bad("批次视图无效"));
    }
    let offset = ctx
        .q("offset")
        .parse::<i64>()
        .unwrap_or(0)
        .clamp(0, 1000000);
    let limit = ctx.q("limit").parse::<i64>().unwrap_or(50).clamp(1, 100);
    let grouped = format!(
        "{INVENTORY}, folders AS (SELECT COALESCE(i.batch_id,'legacy') AS id,i.product_id,COALESCE(b.label,'历史卡密') AS label,COALESCE(b.created,MIN(i.created)) AS created,b.deleted_at,MIN(i.variant_id) AS variant_id,COUNT(*) AS total,SUM(i.status IN ('unused','needs_input') AND NOT EXISTS(SELECT 1 FROM job_delivery_versions v WHERE v.job_id=i.job_id)) AS remaining,SUM(i.status IN ('unused','needs_input','failed_retryable') AND NOT EXISTS(SELECT 1 FROM job_delivery_versions v WHERE v.job_id=i.job_id)) AS available,SUM(i.job_id IS NOT NULL AND (i.job_state!='needs_input' OR EXISTS(SELECT 1 FROM job_delivery_versions v WHERE v.job_id=i.job_id))) AS used,SUM(i.status IN ('queued','processing')) AS in_progress,SUM(i.status IN ('succeeded','destroyed')) AS completed,SUM(i.status IN ('failed_retryable','failed_terminal')) AS failed FROM inventory i LEFT JOIN card_batches b ON b.id=i.batch_id LEFT JOIN card_meta m ON m.card_id=i.id WHERE (i.batch_id IS NOT NULL OR m.batch_deleted_at IS NULL) AND ((:view='active' AND b.deleted_at IS NULL) OR (:view='deleted' AND b.deleted_at IS NOT NULL)) AND (:variant_id='' OR i.variant_id=:variant_id) GROUP BY i.batch_id,i.product_id) "
    );
    let filter = "WHERE (:search='' OR instr(lower(label),lower(:search))>0 OR instr(lower(id),lower(:search))>0 OR id IN (SELECT COALESCE(batch_id,'legacy') FROM inventory WHERE (:variant_id='' OR variant_id=:variant_id) AND (instr(upper(id),upper(:search))>0 OR instr(upper(COALESCE(code_suffix,'')),upper(:search))>0)))";
    let total=required(&ctx.conn,&format!("{grouped} SELECT COUNT(*) AS total FROM folders {filter}"),named_params!{":product_id":pid,":shop_id":"",":now":now(),":view":view,":variant_id":ctx.q("variant_id"),":search":ctx.q("search").trim()},"批次不存在")?["total"].clone();
    let mut items = rows(
        &ctx.conn,
        &format!(
            "{grouped} SELECT * FROM folders {filter} ORDER BY created DESC,id LIMIT :limit OFFSET :offset"
        ),
        named_params! {":product_id":pid,":shop_id":"",":now":now(),":view":view,":variant_id":ctx.q("variant_id"),":search":ctx.q("search").trim(),":limit":limit,":offset":offset},
    )?;
    for item in &mut items {
        item["legacy"] = json!(text(item, "id") == "legacy");
        item["deleted"] = json!(!item["deleted_at"].is_null());
        item["states"] = json!({});
        for status in super::cards::STATUSES {
            item["states"][status] = json!(0);
        }
        for r in rows(
            &ctx.conn,
            &format!(
                "{INVENTORY} SELECT status,COUNT(*) AS n FROM inventory i LEFT JOIN card_meta m ON m.card_id=i.id WHERE ((:bid='legacy' AND i.batch_id IS NULL AND m.batch_deleted_at IS NULL) OR i.batch_id=:bid) AND (:variant_id='' OR i.variant_id=:variant_id) GROUP BY status"
            ),
            named_params! {":product_id":pid,":shop_id":"",":now":now(),":bid":text(item,"id"),":variant_id":ctx.q("variant_id")},
        )? {
            item["states"][text(&r, "status")] = r["n"].clone();
        }
    }
    Ok(json!({"items":items,"total":total,"offset":offset,"limit":limit,"view":view}))
}
pub fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    let prefix = if ctx.path.starts_with("/api/admin/card-batches") {
        "/api/admin/card-batches"
    } else if ctx.path.starts_with("/api/manage/card-batches") {
        "/api/manage/card-batches"
    } else {
        return Ok(None);
    };
    let suffix = ctx
        .path
        .strip_prefix(prefix)
        .unwrap()
        .trim_start_matches('/')
        .to_owned();
    let (session, pid) = scope(ctx)?;
    if pid.is_empty() {
        return Err(ApiError::bad("请先选择一个商品"));
    }
    let p = auth::authorize_product(ctx, &session, &pid)?;
    if suffix.is_empty() && ctx.method.as_str() == "GET" {
        return Ok(Some(Reply::json(list(ctx, &pid)?)));
    }
    let parts = suffix.split('/').collect::<Vec<_>>();
    let mut bid = parts[0].to_owned();
    if bid.is_empty() {
        return Ok(None);
    }
    let operation = parts.get(1).copied().unwrap_or("");
    if matches!(operation, "delete-preview" | "purge-preview") && ctx.method.as_str() == "POST" {
        return Ok(Some(Reply::json(preview(
            ctx,
            &pid,
            &bid,
            if operation == "delete-preview" {
                "delete"
            } else {
                "purge"
            },
        )?)));
    }
    if operation == "restore" && ctx.method.as_str() == "POST" {
        let b = batch(ctx, &pid, &bid)?;
        if b["deleted_at"].is_null() {
            return Err(ApiError::new(409, "批次不在回收站"));
        }
        ctx.conn.execute("UPDATE cards SET state='ready' WHERE state='revoked' AND product_id=? AND id IN(SELECT card_id FROM card_meta WHERE batch_id=? AND batch_previous_state='ready')",params![pid,bid])?;
        ctx.conn.execute("UPDATE card_meta SET batch_deleted_at=NULL,batch_previous_state=NULL WHERE batch_id=? AND card_id IN(SELECT id FROM cards WHERE product_id=?)",params![bid,pid])?;
        ctx.conn.execute(
            "UPDATE card_batches SET deleted_at=NULL WHERE id=? AND product_id=?",
            params![bid, pid],
        )?;
        audit(
            &ctx.conn,
            &actor(&session),
            "cards.batch.restore",
            &bid,
            Some(text(&p, "shop_id")),
        )?;
        return Ok(Some(Reply::json(
            json!({"ok":true,"batch_id":bid,"deleted":false}),
        )));
    }
    let action = if operation.is_empty() && ctx.method.as_str() == "DELETE" {
        "delete"
    } else if operation == "purge" && ctx.method.as_str() == "POST" {
        "purge"
    } else {
        return Ok(None);
    };
    if ctx.body["confirmed"] != json!(true)
        || ctx
            .body
            .as_object()
            .is_none_or(|o| o.keys().any(|k| k != "confirmed" && k != "revision"))
    {
        return Err(ApiError::new(422, "删除批次需要确认预览"));
    }
    let current = preview(ctx, &pid, &bid, action)?;
    if current["revision"] != ctx.body["revision"] {
        return Err(ApiError::new(409, "批次状态已变化，请重新查看预览"));
    }
    if action == "delete" {
        if bid == "legacy" {
            bid = id();
            ctx.conn.execute("INSERT INTO card_batches(id,product_id,label,created,count,variant_id) SELECT ?,?,'历史卡密',MIN(c.created),COUNT(*),'default' FROM cards c LEFT JOIN card_meta m ON m.card_id=c.id WHERE c.product_id=? AND m.batch_id IS NULL AND m.batch_deleted_at IS NULL",params![bid,pid,pid])?;
            ctx.conn.execute("INSERT INTO card_meta(card_id,batch_id) SELECT c.id,? FROM cards c LEFT JOIN card_meta m ON m.card_id=c.id WHERE c.product_id=? AND m.card_id IS NULL",params![bid,pid])?;
            ctx.conn.execute("UPDATE card_meta SET batch_id=? WHERE batch_id IS NULL AND batch_deleted_at IS NULL AND card_id IN(SELECT id FROM cards WHERE product_id=?)",params![bid,pid])?;
        }
        ctx.conn.execute("UPDATE card_meta SET batch_deleted_at=?,batch_previous_state=CASE WHEN card_id IN(SELECT c.id FROM cards c LEFT JOIN jobs j ON j.card_id=c.id WHERE c.state='ready' AND (j.id IS NULL OR j.state IN ('failed','needs_input'))) THEN 'ready' ELSE NULL END WHERE batch_id=? AND card_id IN(SELECT id FROM cards WHERE product_id=?)",params![now(),bid,pid])?;
        ctx.conn.execute("UPDATE cards SET state='revoked' WHERE id IN(SELECT card_id FROM card_meta WHERE batch_id=? AND batch_previous_state='ready') AND product_id=?",params![bid,pid])?;
        ctx.conn.execute(
            "UPDATE card_batches SET deleted_at=? WHERE id=? AND product_id=?",
            params![now(), bid, pid],
        )?;
        audit(
            &ctx.conn,
            &actor(&session),
            "cards.batch.delete",
            &bid,
            Some(text(&p, "shop_id")),
        )?;
        return Ok(Some(Reply::json(
            json!({"ok":true,"batch_id":bid,"deleted":true,"revoked":current["revocable_count"],"retained":current["retain_count"]}),
        )));
    }
    let removable = "SELECT c.id FROM cards c JOIN card_meta m ON m.card_id=c.id WHERE c.product_id=? AND m.batch_id=? AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.card_id=c.id)";
    for table in ["grants", "receipt_batch_cards"] {
        ctx.conn.execute(
            &format!("DELETE FROM {table} WHERE card_id IN ({removable})"),
            params![pid, bid],
        )?;
    }
    ctx.conn.execute(
        &format!("DELETE FROM cards WHERE id IN ({removable})"),
        params![pid, bid],
    )?;
    ctx.conn.execute(
        "DELETE FROM card_batches WHERE id=? AND product_id=?",
        params![bid, pid],
    )?;
    audit(
        &ctx.conn,
        &actor(&session),
        "cards.batch.purge",
        &bid,
        Some(text(&p, "shop_id")),
    )?;
    Ok(Some(Reply::json(
        json!({"ok":true,"batch_id":bid,"purged":true,"deleted_cards":current["delete_count"],"retained_cards":current["retain_count"],"preserved_fulfillment":true}),
    )))
}

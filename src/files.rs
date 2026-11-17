//! Bounded attachment storage with card, shop, field and immutable attempt authorization.
use crate::{auth, catalog, common::*, fulfillment};
use axum::body::Bytes;
use rusqlite::params;
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
mod images;
pub mod storage;
pub use storage::{check_storage_quota, maintenance_once, set_shop_storage_limit, storage_usage};

pub fn max_file_bytes() -> usize {
    storage::env_limit(
        "EXTORE_UPLOAD_FILE_BYTES",
        20 * 1024 * 1024,
        20 * 1024 * 1024,
    ) as usize
}
pub fn max_card_bytes() -> u64 {
    storage::env_limit(
        "EXTORE_UPLOAD_JOB_BYTES",
        100 * 1024 * 1024,
        i64::MAX as u64,
    )
}
pub fn max_card_files() -> u64 {
    storage::env_limit("EXTORE_UPLOAD_JOB_FILES", 100, i64::MAX as u64)
}
pub fn max_multipart_bytes() -> usize {
    max_file_bytes() + 64 * 1024
}
fn multipart_error() -> ApiError {
    ApiError::bad("上传表单无效，请先提交领取凭据和字段，再上传一个文件")
}
fn is_attachment(f: &Value) -> bool {
    matches!(text(f, "type"), "file" | "image" | "images")
}
fn field<'a>(p: &'a Value, key: &str, kind: &str) -> Result<&'a Value> {
    p[if kind == "input" {
        "parameters"
    } else {
        "outputs"
    }]
    .as_array()
    .and_then(|a| a.iter().find(|f| text(f, "key") == key && is_attachment(f)))
    .ok_or_else(|| ApiError::new(400, "此字段不是文件或图片字段"))
}
pub fn attachment_ids(f: &Value, value: &Value) -> Result<Vec<String>> {
    if !is_attachment(f) {
        return Ok(vec![]);
    }
    let s = value
        .as_str()
        .ok_or_else(|| ApiError::new(422, "附件字段必须是文本"))?;
    if s.is_empty() {
        return Ok(vec![]);
    }
    if text(f, "type") != "images" {
        return Ok(vec![s.to_owned()]);
    }
    let values: Vec<String> = serde_json::from_str(s)
        .map_err(|_| ApiError::new(422, "图片集合必须是文件 ID 的 JSON 数组"))?;
    let max = integer(f, "max_items", 10);
    let unique = values.iter().collect::<BTreeSet<_>>();
    if !(1..=20).contains(&max)
        || values.len() > max as usize
        || values.len() != unique.len()
        || values.iter().any(|s| {
            s.len() != 36
                || uuid::Uuid::parse_str(s).is_err()
                || s.chars()
                    .any(|c| !c.is_ascii_digit() && !('a'..='f').contains(&c) && c != '-')
        })
    {
        return Err(ApiError::new(422, "图片集合包含无效、重复或过多的文件 ID"));
    }
    Ok(values)
}
fn meta(ctx: &Context, fid: &str) -> Result<Value> {
    required(
        &ctx.conn,
        "SELECT f.id,f.card_id,f.product_id,f.job_id,f.field_key,f.kind,f.attempt,f.filename,f.content_type,f.size,f.bound,f.released,f.consumed,f.created,f.content IS NOT NULL AS available,t.flow_epoch,t.node_id FROM job_files f LEFT JOIN task_flow_files t ON t.file_id=f.id WHERE f.id=?",
        [fid],
        "文件不存在",
    )
}
fn descriptor(file: &Value) -> Value {
    let mut r = json!({});
    for k in [
        "id",
        "job_id",
        "field_key",
        "kind",
        "filename",
        "content_type",
        "size",
        "created",
        "consumed",
        "attempt",
    ] {
        r[k] = file[k].clone();
    }
    r["available"] = json!(flag(file, "available"));
    for k in ["flow_epoch", "node_id"] {
        if !file[k].is_null() {
            r[k] = file[k].clone();
        }
    }
    r
}
pub fn listfiles(ctx: &Context, job: &Value, kind: Option<&str>) -> Result<Vec<Value>> {
    let all = rows(
        &ctx.conn,
        "SELECT f.id,f.job_id,f.field_key,f.kind,f.attempt,f.filename,f.content_type,f.size,f.consumed,f.created,f.content IS NOT NULL AS available,t.flow_epoch,t.node_id FROM job_files f LEFT JOIN task_flow_files t ON t.file_id=f.id WHERE f.job_id=? AND f.product_id=? AND f.card_id=? AND (? IS NULL OR f.kind=?) ORDER BY f.created,f.id",
        params![
            text(job, "id"),
            text(job, "product_id"),
            text(job, "card_id"),
            kind,
            kind
        ],
    )?;
    Ok(all.iter().map(descriptor).collect())
}
fn validate_image(ctx: &Context, item: &Value, f: &Value) -> Result<()> {
    if matches!(text(f, "type"), "image" | "images") {
        let bytes = ctx.conn.query_row(
            "SELECT content FROM job_files WHERE id=?",
            [text(item, "id")],
            |r| r.get::<_, Vec<u8>>(0),
        )?;
        let content_type = images::content_type(&bytes)?;
        if text(item, "content_type") != content_type {
            ctx.conn.execute(
                "UPDATE job_files SET content_type=? WHERE id=?",
                params![content_type, text(item, "id")],
            )?;
        }
    }
    Ok(())
}
pub fn validate_input_files(ctx: &Context, card: &Value, p: &Value, values: &Value) -> Result<()> {
    let existing = row(
        &ctx.conn,
        "SELECT id,attempt FROM jobs WHERE card_id=?",
        [text(card, "id")],
    )?;
    for f in p["parameters"]
        .as_array()
        .ok_or_else(|| ApiError::bad("商品输入字段无效"))?
    {
        for fid in attachment_ids(f, values.get(text(f, "key")).unwrap_or(&json!("")))? {
            let item = meta(ctx, &fid)?;
            let attempt = existing.as_ref().map_or(1, |v| integer(v, "attempt", 1));
            if item["card_id"] != card["id"]
                || item["product_id"] != p["id"]
                || item["field_key"] != f["key"]
                || text(&item, "kind") != "input"
                || integer(&item, "attempt", 1) != attempt
                || !flag(&item, "available")
                || (!item["job_id"].is_null()
                    && existing.as_ref().is_none_or(|v| item["job_id"] != v["id"]))
            {
                return Err(ApiError::new(403, "输入文件不属于此卡密、尝试或字段"));
            }
            validate_image(ctx, &item, f)?;
        }
    }
    Ok(())
}
pub fn validate_output_files(ctx: &Context, job: &Value, p: &Value, values: &Value) -> Result<()> {
    for f in p["outputs"]
        .as_array()
        .ok_or_else(|| ApiError::bad("商品输出字段无效"))?
    {
        for fid in attachment_ids(f, values.get(text(f, "key")).unwrap_or(&json!("")))? {
            let item = meta(ctx, &fid)?;
            if item["card_id"] != job["card_id"]
                || item["product_id"] != job["product_id"]
                || item["job_id"] != job["id"]
                || item["attempt"] != job["attempt"]
                || item["field_key"] != f["key"]
                || text(&item, "kind") != "output"
                || !flag(&item, "available")
            {
                return Err(ApiError::new(403, "交付文件不属于此任务、尝试或字段"));
            }
            validate_image(ctx, &item, f)?;
        }
    }
    Ok(())
}
fn selected(p: &Value, values: &Value, kind: &str) -> Result<BTreeSet<String>> {
    Ok(selected_ordered(p, values, kind)?.into_iter().collect())
}
fn selected_ordered(p: &Value, values: &Value, kind: &str) -> Result<Vec<String>> {
    let mut result = Vec::new();
    let mut seen = BTreeSet::new();
    for f in p[if kind == "input" {
        "parameters"
    } else {
        "outputs"
    }]
    .as_array()
    .ok_or_else(|| ApiError::bad("商品字段无效"))?
    {
        for file_id in attachment_ids(f, values.get(text(f, "key")).unwrap_or(&json!("")))? {
            if seen.insert(file_id.clone()) {
                result.push(file_id);
            }
        }
    }
    Ok(result)
}
pub fn bind_inputs(ctx: &Context, job: &Value, p: &Value, values: &Value) -> Result<()> {
    let selected = selected(p, values, "input")?;
    for f in rows(
        &ctx.conn,
        "SELECT id FROM job_files WHERE card_id=? AND kind='input'",
        [text(job, "card_id")],
    )? {
        let fid = text(&f, "id");
        if selected.contains(fid) {
            ctx.conn.execute(
                "UPDATE job_files SET job_id=?,attempt=?,bound=1 WHERE id=?",
                params![text(job, "id"), integer(job, "attempt", 1), fid],
            )?;
        } else {
            ctx.conn
                .execute("DELETE FROM job_files WHERE id=?", [fid])?;
        }
    }
    Ok(())
}
pub fn bind_outputs(ctx: &Context, job: &Value, p: &Value, values: &Value) -> Result<()> {
    let selected = selected(p, values, "output")?;
    let preserved = fulfillment::entitlements::preserved_output_ids(ctx, job)?;
    for f in rows(
        &ctx.conn,
        "SELECT id FROM job_files WHERE job_id=? AND kind='output'",
        [text(job, "id")],
    )? {
        let fid = text(&f, "id");
        if selected.contains(fid) {
            ctx.conn
                .execute("UPDATE job_files SET bound=1 WHERE id=?", [fid])?;
        } else if !preserved.iter().any(|v| v == fid) {
            ctx.conn
                .execute("DELETE FROM job_files WHERE id=?", [fid])?;
        }
    }
    Ok(())
}
pub fn release_output_files(ctx: &Context, job: &Value, p: &Value) -> Result<Vec<Value>> {
    let values = json_field(job, "result_json", json!({}));
    validate_output_files(ctx, job, p, &values)?;
    let mut result = vec![];
    for fid in selected_ordered(p, &values, "output")? {
        let f = meta(ctx, &fid)?;
        if !flag(&f, "bound") {
            return Err(ApiError::new(409, "交付文件尚未完成"));
        }
        ctx.conn
            .execute("UPDATE job_files SET released=1 WHERE id=?", [&fid])?;
        result.push(descriptor(&f));
    }
    Ok(result)
}
pub fn purge_job_outputs(ctx: &Context, job: &Value) -> Result<()> {
    let preserved = fulfillment::entitlements::preserved_output_ids(ctx, job)?;
    for f in rows(
        &ctx.conn,
        "SELECT id FROM job_files WHERE job_id=? AND kind='output'",
        [text(job, "id")],
    )? {
        let fid = text(&f, "id");
        if !preserved.iter().any(|v| v == fid) {
            ctx.conn
                .execute("DELETE FROM job_files WHERE id=?", [fid])?;
        }
    }
    Ok(())
}
pub fn purge_job_files(ctx: &Context, job: &Value) -> Result<()> {
    ctx.conn.execute(
        "DELETE FROM job_files WHERE card_id=?",
        [text(job, "card_id")],
    )?;
    Ok(())
}
struct Upload {
    fields: BTreeMap<String, String>,
    filename: String,
    content_type: String,
    content: Vec<u8>,
}
async fn parse_multipart(body: Vec<u8>, content_type: String, output: bool) -> Result<Upload> {
    if body.len() > max_multipart_bytes() {
        return Err(ApiError::new(413, "文件超过上传大小限制"));
    }
    let boundary = multer::parse_boundary(content_type).map_err(|_| multipart_error())?;
    if boundary.len() > 200 {
        return Err(multipart_error());
    }
    let stream =
        futures_util::stream::once(async move { Ok::<Bytes, std::io::Error>(Bytes::from(body)) });
    let mut multipart = multer::Multipart::new(stream, boundary);
    let mut fields = BTreeMap::new();
    let mut upload = None;
    let required = if output {
        vec!["job_id", "field_key"]
    } else {
        vec!["token", "field_key"]
    };
    let optional = if output {
        vec!["flow_epoch", "action_id", "attempt"]
    } else {
        vec!["card_id", "flow_epoch", "expected_revision", "node_id"]
    };
    while let Some(mut part) = multipart
        .next_field()
        .await
        .map_err(|_| multipart_error())?
    {
        if part
            .headers()
            .iter()
            .map(|(k, v)| k.as_str().len() + v.as_bytes().len())
            .sum::<usize>()
            > 4096
        {
            return Err(multipart_error());
        }
        let name = part.name().ok_or_else(multipart_error)?.to_owned();
        if upload.is_some() {
            return Err(multipart_error());
        }
        if let Some(filename) = part.file_name() {
            if name != "file" || required.iter().any(|k| !fields.contains_key(*k)) {
                return Err(multipart_error());
            }
            let filename = filename.to_owned();
            let content_type = part
                .content_type()
                .map(|m| m.to_string())
                .unwrap_or_default();
            let mut content = Vec::new();
            while let Some(chunk) = part.chunk().await.map_err(|_| multipart_error())? {
                if content.len() + chunk.len() > max_file_bytes() {
                    return Err(ApiError::new(413, "文件超过上传大小限制"));
                }
                content.extend_from_slice(&chunk);
            }
            upload = Some((filename, content_type, content));
        } else {
            if fields.contains_key(&name)
                || !required.contains(&name.as_str()) && !optional.contains(&name.as_str())
            {
                return Err(multipart_error());
            }
            let data = part.bytes().await.map_err(|_| multipart_error())?;
            if data.len() > 512 {
                return Err(multipart_error());
            }
            fields.insert(
                name,
                String::from_utf8(data.to_vec()).map_err(|_| multipart_error())?,
            );
        }
    }
    let (filename, content_type, content) = upload.ok_or_else(multipart_error)?;
    Ok(Upload {
        fields,
        filename,
        content_type,
        content,
    })
}
fn upload_number(fields: &BTreeMap<String, String>, name: &str, min: i64) -> Result<Option<i64>> {
    fields
        .get(name)
        .map(|v| {
            if v.is_empty() || v.len() > 9 || !v.bytes().all(|b| b.is_ascii_digit()) {
                return Err(ApiError::new(422, "上传步骤标识无效"));
            }
            let n = v.parse::<i64>().map_err(|_| multipart_error())?;
            if n < min {
                return Err(ApiError::new(422, "上传步骤标识无效"));
            }
            Ok(n)
        })
        .transpose()
}
fn filename(value: &str) -> String {
    let value = value
        .rsplit(['/', '\\'])
        .next()
        .unwrap_or("")
        .chars()
        .filter(|c| !c.is_control())
        .take(180)
        .collect::<String>();
    let value = value.trim();
    if value.is_empty() {
        "attachment".into()
    } else {
        value.into()
    }
}
fn mime(value: &str) -> String {
    if value.len() <= 100
        && value.contains('/')
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"!#$&^_.+-/".contains(&b))
    {
        value.into()
    } else {
        "application/octet-stream".into()
    }
}
fn bind_flow(ctx: &Context, job: &Value, fid: &str, kind: &str) -> Result<()> {
    if let Some(run) = row(
        &ctx.conn,
        "SELECT * FROM task_flow_runs WHERE job_id=?",
        [text(job, "id")],
    )? {
        ctx.conn.execute("INSERT INTO task_flow_files(file_id,job_id,node_id,flow_epoch,attempt,kind) VALUES (?,?,?,?,?,?)",params![fid,text(job,"id"),text(&run,"node_id"),integer(&run,"flow_epoch",0),integer(job,"attempt",1),kind])?;
    }
    Ok(())
}
fn store(
    ctx: &Context,
    card_id: &str,
    p: &Value,
    job: Option<&Value>,
    kind: &str,
    key: &str,
    upload: Upload,
    flow: bool,
) -> Result<Value> {
    let field = field(p, key, kind)?;
    let content_type = if matches!(text(field, "type"), "image" | "images") {
        images::content_type(&upload.content)?.to_owned()
    } else {
        mime(&upload.content_type)
    };
    let size = upload.content.len() as u64;
    if size > max_file_bytes() as u64 {
        return Err(ApiError::new(413, "文件超过上传大小限制"));
    }
    let count = required(
        &ctx.conn,
        "SELECT COALESCE(SUM(size),0) AS size,COUNT(*) AS count FROM job_files WHERE card_id=? AND content IS NOT NULL",
        [card_id],
        "文件计数不可用",
    )?;
    if integer(&count, "size", 0) as u64
        + storage::revision_bytes(&ctx.conn, None, Some(card_id))?
        + size
        > max_card_bytes()
    {
        return Err(ApiError::new(413, "此卡密的文件总量超过限制"));
    }
    if integer(&count, "count", 0) as u64 >= max_card_files() {
        return Err(ApiError::new(413, "此卡密的文件数量超过限制"));
    }
    check_storage_quota(ctx, size, text(p, "id"))?;
    let fid = id();
    let jid = job
        .filter(|_| kind == "output" || flow)
        .map(|j| text(j, "id"));
    let attempt = job.map_or(1, |j| integer(j, "attempt", 1));
    ctx.conn.execute("INSERT INTO job_files(id,card_id,product_id,job_id,field_key,kind,attempt,filename,content_type,size,content,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",params![fid,card_id,text(p,"id"),jid,key,kind,attempt,filename(&upload.filename),content_type,size as i64,upload.content,now()])?;
    if let Some(job) = job.filter(|_| flow) {
        bind_flow(ctx, job, &fid, kind)?;
    }
    Ok(descriptor(&meta(ctx, &fid)?))
}
fn job(ctx: &Context, jid: &str) -> Result<Value> {
    required(
        &ctx.conn,
        "SELECT * FROM jobs WHERE id=?",
        [jid],
        "任务不存在",
    )
}
fn download(ctx: &Context, item: &Value) -> Result<Reply> {
    let bytes = ctx.conn.query_row(
        "SELECT content FROM job_files WHERE id=? AND content IS NOT NULL",
        [text(item, "id")],
        |r| r.get::<_, Vec<u8>>(0),
    )?;
    let encoded = text(item, "filename")
        .as_bytes()
        .iter()
        .map(|b| {
            if b.is_ascii_alphanumeric() || b"-._~".contains(b) {
                (*b as char).to_string()
            } else {
                format!("%{b:02X}")
            }
        })
        .collect::<String>();
    let mut reply = Reply::bytes("application/octet-stream", bytes)?;
    for (k, v) in [
        (
            "content-disposition",
            format!("attachment; filename=\"download\"; filename*=UTF-8''{encoded}"),
        ),
        ("x-content-type-options", "nosniff".into()),
        ("cache-control", "no-store".into()),
        ("pragma", "no-cache".into()),
        (
            "content-security-policy",
            "default-src 'none'; sandbox".into(),
        ),
    ] {
        reply.headers.insert(
            axum::http::HeaderName::from_bytes(k.as_bytes()).unwrap(),
            axum::http::HeaderValue::from_str(&v).map_err(|_| ApiError::bad("下载文件名无效"))?,
        );
    }
    Ok(reply)
}
pub fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    if let Some(reply) = storage::handle(ctx)? {
        return Ok(Some(reply));
    }
    if ctx.is("GET", "/api/upload-limits") {
        return Ok(Some(Reply::json(
            json!({"max_file_bytes":max_file_bytes(),"max_card_bytes":max_card_bytes(),"max_card_files":max_card_files()}),
        )));
    }
    if ctx.is("POST", "/api/files/upload") || ctx.is("POST", "/api/manage/files/upload") {
        let output = ctx.path.starts_with("/api/manage/");
        auth::throttle(
            ctx,
            "files-upload",
            if output { 20 } else { 60 },
            60.0,
            None,
        )?;
        let session = if output {
            Some(auth::session(
                ctx,
                &["admin", "staff"],
                Some("queue.process"),
            )?)
        } else {
            None
        };
        let ct = ctx.header("content-type").to_owned();
        let bytes = std::mem::take(&mut ctx.raw_body);
        let u = tokio::runtime::Handle::current().block_on(parse_multipart(bytes, ct, output))?;
        let f = u.fields.clone();
        let key = f.get("field_key").ok_or_else(multipart_error)?;
        let epoch = upload_number(&f, "flow_epoch", 0)?;
        if output {
            let s = session.unwrap();
            let j = job(ctx, &f["job_id"])?;
            auth::authorize_product(ctx, &s, text(&j, "product_id"))?;
            let p = fulfillment::job_product(ctx, &j)?;
            if text(&p, "mode") != "manual"
                || text(&j, "state") != "processing"
                || text(&j, "claimed_by") != actor(&s)
            {
                return Err(ApiError::new(
                    409,
                    "请先领取任务，且只能处理自己领取的队列任务",
                ));
            }
            let attempt = upload_number(&f, "attempt", 1)?;
            if integer(&j, "revision_round", 0) > 0 && attempt.is_none()
                || attempt.is_some_and(|a| a != integer(&j, "attempt", 1))
            {
                return Err(ApiError::new(
                    409,
                    "上传必须绑定当前任务尝试，请刷新任务后重新上传",
                ));
            }
            if f.get("action_id")
                .is_some_and(|v| v.is_empty() || v.len() > 100)
            {
                return Err(ApiError::new(422, "上传处理动作无效"));
            }
            let execution = fulfillment::flow::output_file_scope(
                ctx,
                &j,
                epoch,
                f.get("action_id").map(String::as_str),
            )?;
            let result = store(
                ctx,
                text(&j, "card_id"),
                &p,
                Some(&j),
                "output",
                key,
                u,
                execution.is_some(),
            )?;
            return Ok(Some(Reply::json(result)));
        }
        let card = fulfillment::grant(
            ctx,
            &f["token"],
            f.get("card_id")
                .filter(|v| !v.is_empty())
                .map(String::as_str),
        )?;
        if matches!(text(&card, "state"), "revoked" | "rejected") {
            return Err(ApiError::new(410, "此卡密已撤销或拒绝"));
        }
        let p = catalog::product(ctx, text(&card, "product_id"))?;
        auth::shop(ctx, text(&p, "shop_id")).map_err(|error| {
            if error.status == 401 {
                ApiError::new(404, "商品不存在或店铺不可用")
            } else {
                error
            }
        })?;
        let flow = fulfillment::flow::input_file_scope(
            ctx,
            &card,
            key,
            epoch,
            upload_number(&f, "expected_revision", 0)?,
            f.get("node_id").map(String::as_str),
        )?;
        let (p, existing, flow) = if let Some((p, j)) = flow {
            (p, j, true)
        } else {
            let existing = row(
                &ctx.conn,
                "SELECT * FROM jobs WHERE card_id=?",
                [text(&card, "id")],
            )?;
            let p = if let Some(j) = &existing {
                if !flag(&fulfillment::job_view(ctx, j, false)?, "can_retry") {
                    return Err(ApiError::new(409, "提交后的任务不能替换输入文件"));
                }
                fulfillment::job_product(ctx, j)?
            } else {
                if text(&card, "state") != "ready" {
                    return Err(ApiError::new(409, "此卡密不能上传输入文件"));
                }
                p
            };
            (p, existing, false)
        };
        let result = store(
            ctx,
            text(&card, "id"),
            &p,
            existing.as_ref(),
            "input",
            key,
            u,
            flow,
        )?;
        return Ok(Some(Reply::json(result)));
    }
    if ctx.is("GET", "/api/manage/files") {
        let s = auth::session(ctx, &["admin", "staff"], Some("queue.view"))?;
        let j = job(ctx, ctx.q("job_id"))?;
        auth::authorize_product(ctx, &s, text(&j, "product_id"))?;
        return Ok(Some(Reply::json(json!(listfiles(ctx, &j, None)?))));
    }
    if ctx.method == "GET"
        && ctx.path.starts_with("/api/manage/files/")
        && ctx.path.ends_with("/download")
    {
        auth::throttle(ctx, "files-download", 60, 60.0, None)?;
        let s = auth::session(ctx, &["admin", "staff"], Some("queue.view"))?;
        let fid = ctx
            .path
            .strip_prefix("/api/manage/files/")
            .unwrap()
            .strip_suffix("/download")
            .unwrap();
        let f = meta(ctx, fid)?;
        if f["job_id"].is_null() {
            return Err(ApiError::new(404, "文件尚未提交到任务"));
        }
        let j = job(ctx, text(&f, "job_id"))?;
        auth::authorize_product(ctx, &s, text(&j, "product_id"))?;
        if f["product_id"] != j["product_id"] || f["card_id"] != j["card_id"] {
            return Err(ApiError::new(403, "文件不属于此任务"));
        }
        if text(&j, "state") == "destroyed" || !flag(&f, "available") {
            return Err(ApiError::new(410, "文件已销毁或领取"));
        }
        return Ok(Some(download(ctx, &f)?));
    }
    if ctx.is("POST", "/api/files/download") {
        auth::throttle(ctx, "files-download", 60, 60.0, None)?;
        let revision = ctx
            .body
            .get("revision")
            .filter(|v| !v.is_null())
            .map(|v| {
                v.as_i64()
                    .filter(|n| (0..=1000).contains(n))
                    .ok_or_else(|| ApiError::new(422, "交付版本无效"))
            })
            .transpose()?;
        let card = fulfillment::grant(
            ctx,
            ctx.b("token"),
            ctx.body.get("card_id").and_then(Value::as_str),
        )?;
        let f = meta(ctx, ctx.b("file_id"))?;
        if f["card_id"] != card["id"] || text(&f, "kind") != "output" || f["job_id"].is_null() {
            return Err(ApiError::new(403, "文件不属于此次领取"));
        }
        let j = job(ctx, text(&f, "job_id"))?;
        if j["card_id"] != card["id"] || f["product_id"] != j["product_id"] {
            return Err(ApiError::new(403, "文件不属于此次领取"));
        }
        if !flag(&f, "available") || flag(&f, "consumed") {
            return Err(ApiError::new(410, "文件已领取或销毁"));
        }
        let p = fulfillment::job_product(ctx, &j)?;
        field(&p, text(&f, "field_key"), "output")?;
        let delivery = fulfillment::entitlements::file_delivery(ctx, &j, &f, revision)?;
        let once = text(&p, "view_policy") == "once";
        let once_current = once
            && text(&j, "state") == "succeeded"
            && f["attempt"] == j["attempt"]
            && revision.is_none_or(|r| r == 0);
        if delivery.is_none() && !once_current {
            return Err(ApiError::new(409, "文件未出现在已完成的交付版本中"));
        }
        if !flag(&f, "bound")
            || !flag(&f, "released")
            || !delivery
                .as_ref()
                .map_or(flag(&j, "revealed"), |d| flag(d, "revealed"))
        {
            return Err(ApiError::new(409, "请先领取商品后下载文件"));
        }
        let reply = download(ctx, &f)?;
        if once {
            ctx.conn.execute(
                "UPDATE job_files SET content=NULL,consumed=1 WHERE id=? AND consumed=0",
                [text(&f, "id")],
            )?;
        }
        return Ok(Some(reply));
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ids_and_names_are_bounded() {
        let f = json!({"type":"images","max_items":2});
        assert!(attachment_ids(&f, &json!("[\"https://example.com\"]")).is_err());
        let id = "a0000000-0000-4000-a000-000000000000";
        assert!(attachment_ids(&f, &json!(format!("[\"{id}\",\"{id}\"]"))).is_err());
        assert_eq!(filename("../a\\bad\r\n.txt"), "bad.txt");
        assert_eq!(mime("text/html;evil"), "application/octet-stream");
    }
    #[tokio::test]
    async fn multipart_requires_scope_before_file_and_one_payload() {
        let body=b"--x\r\nContent-Disposition: form-data; name=\"file\"; filename=\"x\"\r\n\r\nx\r\n--x--\r\n".to_vec();
        assert!(
            parse_multipart(body, "multipart/form-data; boundary=x".into(), false)
                .await
                .is_err()
        );
        let body=b"--x\r\nContent-Disposition: form-data; name=\"token\"\r\n\r\nt\r\n--x\r\nContent-Disposition: form-data; name=\"field_key\"\r\n\r\nf\r\n--x\r\nContent-Disposition: form-data; name=\"file\"; filename=\"x\"\r\n\r\nx\r\n--x--\r\n".to_vec();
        let u = parse_multipart(body, "multipart/form-data; boundary=x".into(), false)
            .await
            .unwrap();
        assert_eq!(u.content, b"x");
    }
}

pub fn scoped_file(ctx: &Context, job: &Value, fid: &str, kind: &str) -> Result<Value> {
    let f = meta(ctx, fid)?;
    if f["job_id"] != job["id"]
        || f["card_id"] != job["card_id"]
        || f["product_id"] != job["product_id"]
        || f["attempt"] != job["attempt"]
        || text(&f, "kind") != kind
    {
        return Err(ApiError::new(403, "文件不属于此任务、尝试或字段"));
    }
    if text(job, "state") == "destroyed" || !flag(&f, "available") {
        return Err(ApiError::new(410, "文件已销毁或领取"));
    }
    Ok(f)
}
pub fn download_scoped(ctx: &Context, job: &Value, fid: &str, kind: &str) -> Result<Reply> {
    let f = scoped_file(ctx, job, fid, kind)?;
    download(ctx, &f)
}
pub fn store_output(
    ctx: &Context,
    job: &Value,
    p: &Value,
    key: &str,
    name: &str,
    content_type: &str,
    bytes: &[u8],
) -> Result<Value> {
    if job["product_id"] != p["id"] || text(job, "state") != "processing" {
        return Err(ApiError::new(409, "任务已改变，请刷新任务后重新上传"));
    }
    let upload = Upload {
        fields: BTreeMap::new(),
        filename: name.into(),
        content_type: content_type.into(),
        content: bytes.to_vec(),
    };
    let flow = row(
        &ctx.conn,
        "SELECT 1 FROM task_flow_runs WHERE job_id=?",
        [text(job, "id")],
    )?
    .is_some();
    store(
        ctx,
        text(job, "card_id"),
        p,
        Some(job),
        "output",
        key,
        upload,
        flow,
    )
}

pub fn download_worker_input(
    ctx: &Context,
    job: &Value,
    execution: &Value,
    key: &str,
    fid: &str,
) -> Result<Reply> {
    fulfillment::flow::private_worker_file_scope(ctx, job, execution, key, "input", Some(fid))?;
    download(ctx, &meta(ctx, fid)?)
}
pub fn store_worker_output(
    ctx: &Context,
    job: &Value,
    execution: &Value,
    key: &str,
    name: &str,
    content_type: &str,
    bytes: &[u8],
) -> Result<Value> {
    if text(job, "state") != "processing" {
        return Err(ApiError::new(409, "任务已改变，请刷新任务后重新上传"));
    }

    let field =
        fulfillment::flow::private_worker_file_scope(ctx, job, execution, key, "output", None)?;
    let product = json!({"id":job["product_id"],"outputs":[field]});
    let upload = Upload {
        fields: BTreeMap::new(),
        filename: name.into(),
        content_type: content_type.into(),
        content: bytes.to_vec(),
    };
    let result = store(
        ctx,
        text(job, "card_id"),
        &product,
        Some(job),
        "output",
        key,
        upload,
        false,
    )?;
    fulfillment::flow::bind_private_worker_file(ctx, job, execution, key, text(&result, "id"))?;
    Ok(descriptor(&meta(ctx, text(&result, "id"))?))
}

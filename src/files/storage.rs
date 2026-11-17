use crate::{auth, common::*};
use rusqlite::{Connection, params};
use serde_json::{Value, json};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::{
    fs::OpenOptions,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
};

pub fn env_limit(name: &str, default: u64, max: u64) -> u64 {
    std::env::var(name)
        .ok()
        .and_then(|v| v.parse::<u64>().ok())
        .filter(|v| *v > 0)
        .unwrap_or(default)
        .min(max)
}
pub fn total_limit() -> u64 {
    env_limit(
        "EXTORE_UPLOAD_TOTAL_BYTES",
        5 * 1024 * 1024 * 1024,
        i64::MAX as u64,
    )
}
pub fn reserve() -> u64 {
    env_limit(
        "EXTORE_UPLOAD_DISK_RESERVE_BYTES",
        512 * 1024 * 1024,
        i64::MAX as u64,
    )
}
fn sum(c: &Connection, sql: &str, sid: Option<&str>) -> Result<u64> {
    Ok(c.query_row(sql, params![sid, sid], |r| r.get::<_, i64>(0))?
        .max(0) as u64)
}
pub fn revision_bytes(c: &Connection, sid: Option<&str>, cid: Option<&str>) -> Result<u64> {
    let queries = [
        "SELECT COALESCE(SUM(COALESCE(length(CAST(v.content AS BLOB)),0)+COALESCE(length(CAST(v.result_json AS BLOB)),0)),0) FROM job_delivery_versions v JOIN jobs j ON j.id=v.job_id JOIN products p ON p.id=j.product_id WHERE v.result_json IS NOT NULL AND (? IS NULL OR p.shop_id=?) AND (? IS NULL OR j.card_id=?)",
        "SELECT COALESCE(SUM(length(CAST(r.message AS BLOB))),0) FROM job_revision_requests r JOIN jobs j ON j.id=r.job_id JOIN products p ON p.id=j.product_id WHERE (? IS NULL OR p.shop_id=?) AND (? IS NULL OR j.card_id=?)",
        "SELECT COALESCE(SUM(COALESCE(length(CAST(j.content AS BLOB)),0)+COALESCE(length(CAST(j.result_json AS BLOB)),0)),0) FROM jobs j JOIN products p ON p.id=j.product_id WHERE j.revision_round>0 AND j.result_json IS NOT NULL AND (? IS NULL OR p.shop_id=?) AND (? IS NULL OR j.card_id=?)",
    ];
    let mut n = 0u64;
    for sql in queries {
        n = n.saturating_add(
            c.query_row(sql, params![sid, sid, cid, cid], |r| r.get::<_, i64>(0))?
                .max(0) as u64,
        );
    }
    Ok(n)
}
fn totals(c: &Connection, sid: Option<&str>) -> Result<(u64, u64)> {
    let queries = [
        "SELECT COALESCE(SUM(f.size),0) FROM job_files f JOIN products p ON p.id=f.product_id WHERE f.content IS NOT NULL AND (? IS NULL OR p.shop_id=?)",
        "SELECT COALESCE(SUM(size),0) FROM text_card_payloads WHERE (? IS NULL OR shop_id=?)",
        "SELECT COALESCE(SUM(length(snapshot_ciphertext)),0) FROM card_task_flows WHERE (? IS NULL OR shop_id=?)",
        "SELECT COALESCE(SUM(length(s.payload_ciphertext)),0) FROM task_flow_steps s JOIN jobs j ON j.id=s.job_id JOIN card_task_flows f ON f.card_id=j.card_id WHERE (? IS NULL OR f.shop_id=?)",
        "SELECT COALESCE(SUM(length(payload_ciphertext)),0) FROM task_flow_dispatches WHERE (? IS NULL OR shop_id=?)",
    ];
    let mut stored = revision_bytes(c, sid, None)?;
    for sql in queries {
        stored = stored.saturating_add(sum(c, sql, sid)?);
    }
    let pending = sum(
        c,
        "SELECT COALESCE(SUM(size),0) FROM upload_reservations WHERE (? IS NULL OR shop_id=?)",
        sid,
    )?;
    Ok((stored, pending))
}
pub fn check_storage_quota(ctx: &Context, extra: u64, pid: &str) -> Result<()> {
    let (stored, pending) = totals(&ctx.conn, None)?;
    if stored.saturating_add(pending).saturating_add(extra) > total_limit() {
        return Err(ApiError::new(507, "服务器文件存储额度已满，请联系商家"));
    }
    if fs2::available_space(&ctx.app.data)?
        < reserve().saturating_add(4u64.saturating_mul(extra.saturating_add(pending)))
    {
        return Err(ApiError::new(507, "服务器文件空间不足，请联系商家"));
    }
    let shop = required(
        &ctx.conn,
        "SELECT shops.id,shops.storage_limit_bytes,shops.enabled FROM products JOIN shops ON shops.id=products.shop_id WHERE products.id=?",
        [pid],
        "商品不存在",
    )?;
    if !flag(&shop, "enabled") {
        return Err(ApiError::new(404, "商品不存在"));
    }
    let (own, pending) = totals(&ctx.conn, Some(text(&shop, "id")))?;
    if own.saturating_add(pending).saturating_add(extra)
        > integer(&shop, "storage_limit_bytes", 1024 * 1024 * 1024).max(0) as u64
    {
        return Err(ApiError::new(507, "此店铺的文件存储额度已满，请联系商家"));
    }
    Ok(())
}
pub fn storage_usage(ctx: &Context, sid: Option<&str>) -> Result<Value> {
    let (stored, pending) = totals(&ctx.conn, sid)?;
    let limit = if let Some(sid) = sid {
        integer(
            &required(
                &ctx.conn,
                "SELECT storage_limit_bytes FROM shops WHERE id=?",
                [sid],
                "店铺不存在",
            )?,
            "storage_limit_bytes",
            0,
        ) as u64
    } else {
        total_limit()
    };
    let active = ctx.conn.query_row(
        "SELECT COUNT(*) FROM upload_reservations WHERE (? IS NULL OR shop_id=?)",
        params![sid, sid],
        |r| r.get::<_, i64>(0),
    )?;
    let mut result = json!({"stored_bytes":stored,"uploading_bytes":pending,"limit_bytes":limit,"active_uploads":active,"upload_concurrency":4});
    if sid.is_none() {
        result["disk_free_bytes"] = json!(fs2::available_space(&ctx.app.data)?);
        result["disk_reserve_bytes"] = json!(reserve());
    }
    Ok(result)
}
pub fn set_shop_storage_limit(
    ctx: &Context,
    session: &Value,
    sid: &str,
    limit: u64,
) -> Result<Value> {
    if text(session, "role") != "admin" || !session["shop_id"].is_null() {
        return Err(ApiError::new(403, "此操作需要平台管理权限"));
    }
    if limit == 0 || limit > total_limit() {
        return Err(ApiError::bad(
            "店铺文件额度必须为正整数且不超过全站文件额度",
        ));
    }
    required(
        &ctx.conn,
        "SELECT id FROM shops WHERE id=?",
        [sid],
        "店铺不存在",
    )?;
    let (stored, pending) = totals(&ctx.conn, Some(sid))?;
    if limit < stored.saturating_add(pending) {
        return Err(ApiError::new(
            409,
            "店铺文件额度不能小于已存储和正在上传的文件总量",
        ));
    }
    ctx.conn.execute(
        "UPDATE shops SET storage_limit_bytes=? WHERE id=?",
        params![limit as i64, sid],
    )?;
    storage_usage(ctx, Some(sid))
}
pub fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    if !ctx.is("GET", "/api/admin/storage") {
        return Ok(None);
    }
    let s = auth::session(ctx, &["admin"], None)?;
    let scope = auth::selected_shop(ctx, &s, ctx.q("shop_id"))?;
    Ok(Some(Reply::json(storage_usage(ctx, scope.as_deref())?)))
}
fn valid_lock(name: &str) -> bool {
    name.strip_suffix(".lock").is_some_and(|v| {
        uuid::Uuid::parse_str(v).is_ok()
            && v.len() == 36
            && v.chars()
                .all(|c| c.is_ascii_digit() || ('a'..='f').contains(&c) || c == '-')
    })
}
fn remove_dead_lock(path: &std::path::Path) -> Result<bool> {
    let f = match OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(0x20000)
        .open(path)
    {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(true),
        Err(_) => return Ok(false),
    };
    if !f.metadata()?.is_file() || fs2::FileExt::try_lock_exclusive(&f).is_err() {
        return Ok(false);
    }
    match std::fs::remove_file(path) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(e.into()),
    };
    Ok(true)
}
pub fn maintenance_once(app: Arc<App>) -> Result<bool> {
    static LAST: AtomicU64 = AtomicU64::new(0);
    let n = now() as u64;
    let prev = LAST.load(Ordering::Relaxed);
    if n.saturating_sub(prev) < 60
        || LAST
            .compare_exchange(prev, n, Ordering::SeqCst, Ordering::Relaxed)
            .is_err()
    {
        return Ok(false);
    }
    let c = app.db()?;
    c.execute_batch("BEGIN IMMEDIATE")?;
    let result = (|| -> Result<bool> {
        let dir = app.data.join("upload-tmp");
        std::fs::create_dir_all(&dir)?;
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700))?;
        let mut changed = false;
        for r in rows(&c, "SELECT id FROM upload_reservations", [])? {
            let name = format!("{}.lock", text(&r, "id"));
            if valid_lock(&name) && remove_dead_lock(&dir.join(name))? {
                c.execute(
                    "DELETE FROM upload_reservations WHERE id=?",
                    [text(&r, "id")],
                )?;
                changed = true;
            }
        }
        for entry in std::fs::read_dir(&dir)? {
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().to_string();
            if !valid_lock(&name) {
                continue;
            }
            let m = entry.metadata()?;
            if m.modified()
                .ok()
                .and_then(|t| t.elapsed().ok())
                .is_none_or(|d| d.as_secs() < 3600)
            {
                continue;
            }
            let key = name.trim_end_matches(".lock");
            if row(&c, "SELECT id FROM upload_reservations WHERE id=?", [key])?.is_none() {
                remove_dead_lock(&entry.path())?;
            }
        }
        let ttl = env_limit("EXTORE_UPLOAD_DRAFT_TTL_SECONDS", 86400, u64::MAX) as f64;
        let mut erased = 0u64;
        for r in rows(
            &c,
            "SELECT id,size,content IS NOT NULL AS available FROM job_files WHERE bound=0 AND created<? AND (kind='input' OR NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.id=job_files.job_id AND jobs.state='processing' AND jobs.attempt=job_files.attempt)) ORDER BY created,id LIMIT 100",
            [now() - ttl],
        )? {
            let size = if flag(&r, "available") {
                integer(&r, "size", 0).max(0) as u64
            } else {
                0
            };
            if erased + size > 20 * 1024 * 1024
                || size > 0 && fs2::available_space(&app.data)? < reserve() + 4 * (erased + size)
            {
                break;
            }
            c.execute("DELETE FROM job_files WHERE id=?", [text(&r, "id")])?;
            erased += size;
            changed = true;
        }
        Ok(changed)
    })();
    match result {
        Ok(changed) => {
            c.execute_batch("COMMIT")?;
            c.busy_timeout(std::time::Duration::from_millis(100))?;
            let _ = c.execute_batch("PRAGMA wal_checkpoint(TRUNCATE)");
            Ok(changed)
        }
        Err(e) => {
            let _ = c.execute_batch("ROLLBACK");
            Err(e)
        }
    }
}

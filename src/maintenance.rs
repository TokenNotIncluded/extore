//! Bounded retention preserves pending delivery and authorization ancestry.
use crate::{auth, common::*};
use rusqlite::params;
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
};
const DAY: f64 = 86400.0;
fn policy(ctx: &Context, sid: Option<&str>) -> Result<Value> {
    let mut v = json!({"enabled":true,"event_retention_days":30,"dead_letter_retention_days":90,"audit_retention_days":180,"link_retention_days":90});
    if let Ok(Value::Object(o)) = parse(&setting(&ctx.conn, "maintenance_policy:platform", "{}")?) {
        v.as_object_mut().unwrap().extend(o);
    }
    let enabled = flag(&v, "enabled");
    if let Some(sid) = sid {
        if let Ok(Value::Object(o)) = parse(&setting(
            &ctx.conn,
            &format!("maintenance_policy:{sid}"),
            "{}",
        )?) {
            v.as_object_mut().unwrap().extend(o);
        }
    }
    validate_policy(&v)?;
    v["enabled"] = json!(enabled && flag(&v, "enabled"));
    Ok(v)
}
fn validate_policy(v: &Value) -> Result<()> {
    if !v["enabled"].is_boolean() {
        return Err(ApiError::bad("保留策略启用标记无效"));
    }
    for (key, min) in [
        ("event_retention_days", 1),
        ("dead_letter_retention_days", 1),
        ("audit_retention_days", 90),
        ("link_retention_days", 1),
    ] {
        if v[key].as_i64().is_none_or(|n| !(min..=3650).contains(&n)) {
            return Err(ApiError::bad("记录保留天数无效"));
        }
    }
    Ok(())
}
fn chain(ctx: &Context, sid: &str) -> Result<Vec<Value>> {
    let mut id = Some(sid.to_owned());
    let mut seen = BTreeSet::new();
    let mut pid = None;
    let mut list = Vec::new();
    while let Some(sid) = id {
        if !seen.insert(sid.clone()) {
            break;
        }
        let Some(r) = row(&ctx.conn, "SELECT * FROM staff WHERE id=?", [sid])? else {
            break;
        };
        if pid
            .as_ref()
            .is_some_and(|p: &String| p != text(&r, "product_id"))
        {
            break;
        }
        pid = Some(text(&r, "product_id").to_owned());
        id = r["parent_id"].as_str().map(str::to_owned);
        list.push(r);
    }
    Ok(list)
}
fn link_state(ctx: &Context, r: &Value) -> &'static str {
    if flag(r, "archived") {
        "archived"
    } else if auth::staff_authorization(ctx, text(r, "id")).is_ok() {
        "active"
    } else {
        "history"
    }
}
fn inactive_since(ctx: &Context, r: &Value) -> Result<Option<f64>> {
    let mut times = Vec::new();
    for n in chain(ctx, text(r, "id"))? {
        if let Some(t) = n["expires"]
            .as_f64()
            .filter(|t| t.is_finite() && *t <= now())
        {
            times.push(t);
        }
        if flag(&n, "revoked") || flag(&n, "archived") {
            if let Some(v)=row(&ctx.conn,"SELECT MIN(created) AS t FROM audit WHERE target=? AND action IN ('staff.revoke','staff.archive') AND created<=?",params![text(&n,"id"),now()])?.and_then(|v|v["t"].as_f64()){times.push(v);}
        }
    }
    Ok(times.into_iter().min_by(f64::total_cmp))
}
fn links(
    ctx: &Context,
    sid: Option<&str>,
    pid: Option<&str>,
    staff: Option<&str>,
    before: Option<f64>,
    limit: usize,
) -> Result<Vec<Value>> {
    let mut selected = Vec::new();
    for r in rows(
        &ctx.conn,
        "SELECT s.* FROM staff s JOIN products p ON p.id=s.product_id WHERE s.archived=0 AND (? IS NULL OR p.shop_id=?) AND (? IS NULL OR s.product_id=?) ORDER BY s.created,s.id",
        params![sid, sid, pid, pid],
    )? {
        if let Some(staff) = staff {
            if text(&r, "id") == staff
                || !chain(ctx, text(&r, "id"))?
                    .iter()
                    .skip(1)
                    .any(|r| text(r, "id") == staff)
            {
                continue;
            }
        }
        if link_state(ctx, &r) != "history" {
            continue;
        }
        if let Some(before) = before {
            if inactive_since(ctx, &r)?.is_none_or(|t| t > before) {
                continue;
            }
        }
        selected.push(json!(text(&r, "id")));
        if selected.len() == limit {
            break;
        }
    }
    Ok(selected)
}
fn archive(ctx: &Context, sid: &str, who: &str, shop: Option<&str>) -> Result<()> {
    let r = required(
        &ctx.conn,
        "SELECT * FROM staff WHERE id=?",
        [sid],
        "管理链接不存在",
    )?;
    if link_state(ctx, &r) == "active" {
        return Err(ApiError::new(409, "仍然有效的链接不能清理"));
    }
    if flag(&r, "archived") {
        return Ok(());
    }
    ctx.conn.execute("UPDATE staff SET archived=1,revoked=1,digest=?,name='已清理链接',permissions='[\"queue.view\"]',uses=max_uses,cli_uses=max_cli_uses WHERE id=? AND archived=0",params![digest(&token()),sid])?;
    for d in rows(
        &ctx.conn,
        "WITH RECURSIVE descendants(id) AS(SELECT id FROM staff WHERE id=? UNION SELECT child.id FROM staff child JOIN descendants d ON child.parent_id=d.id WHERE child.product_id=?) SELECT id FROM descendants ORDER BY id",
        params![sid, text(&r, "product_id")],
    )? {
        for session in rows(
            &ctx.conn,
            "SELECT digest FROM sessions WHERE staff_id=? AND revoked=0",
            [text(&d, "id")],
        )? {
            auth::revoke_session(ctx, text(&session, "digest"), who, "session.revoke")?;
        }
        crate::fulfillment::release_claims(ctx, text(&d, "id"))?;
    }
    audit(&ctx.conn, who, "staff.archive", sid, shop)?;
    Ok(())
}
pub fn cleanup(
    ctx: &Context,
    sid: Option<&str>,
    pid: Option<&str>,
    staff: Option<&str>,
    areas: &[&str],
    dry: bool,
    limit: usize,
    automatic: bool,
    platform_only: bool,
    who: &str,
) -> Result<Value> {
    if !(1..=500).contains(&limit)
        || areas.is_empty()
        || areas
            .iter()
            .any(|a| !matches!(*a, "links" | "events" | "audit"))
    {
        return Err(ApiError::bad("清理类别或批次大小无效"));
    }
    let pol = policy(ctx, sid)?;
    let mut selected = std::collections::BTreeMap::<&str, Vec<Value>>::from([
        ("links", vec![]),
        ("events", vec![]),
        ("audit", vec![]),
    ]);
    if areas.contains(&"links") {
        selected.insert(
            "links",
            links(
                ctx,
                sid,
                pid,
                staff,
                if automatic {
                    Some(now() - number(&pol, "link_retention_days") * DAY)
                } else {
                    None
                },
                limit,
            )?,
        );
    }
    if areas.contains(&"events") {
        selected.insert("events",rows(&ctx.conn,"SELECT e.id FROM events e LEFT JOIN products p ON p.id=e.product_id LEFT JOIN outbox o ON o.id=e.id WHERE (? IS NULL OR p.shop_id=?) AND (? IS NULL OR e.product_id=?) AND ((o.id IS NULL AND e.created<?) OR (o.state IN ('delivered','cancelled') AND o.finished_at<?) OR (o.state='dead' AND o.finished_at<?)) ORDER BY e.created,e.id LIMIT ?",params![sid,sid,pid,pid,now()-number(&pol,"event_retention_days")*DAY,now()-number(&pol,"event_retention_days")*DAY,now()-number(&pol,"dead_letter_retention_days")*DAY,limit as i64+1])?.into_iter().map(|v|v["id"].clone()).collect());
    }
    if areas.contains(&"audit") {
        selected.insert("audit",rows(&ctx.conn,"SELECT id FROM audit WHERE created<? AND (? IS NULL OR shop_id=?) AND (?=0 OR shop_id IS NULL) AND (? IS NULL OR target=? OR target IN(SELECT id FROM jobs WHERE product_id=?) OR target IN(SELECT id FROM cards WHERE product_id=?) OR target IN(SELECT id FROM staff WHERE product_id=?) OR target IN(SELECT id FROM events WHERE product_id=?) OR actor IN(SELECT id FROM staff WHERE product_id=?)) ORDER BY created,id LIMIT ?",params![now()-number(&pol,"audit_retention_days")*DAY,sid,sid,platform_only,pid,pid,pid,pid,pid,pid,pid,limit as i64+1])?.into_iter().map(|v|v["id"].clone()).collect());
    }
    let mut eligible = json!({});
    let mut changed = json!({});
    let mut has_more = json!({});
    for area in ["links", "events", "audit"] {
        let ids = &selected[area];
        eligible[area] = json!(ids.len().min(limit));
        has_more[area] = json!(if area == "links" {
            ids.len() == limit
        } else {
            ids.len() > limit
        });
        let mut n = 0;
        if !dry {
            if area == "audit" && !ids.is_empty() {
                let highest: i64 =
                    ctx.conn
                        .query_row("SELECT COALESCE(MAX(id),0) FROM audit", [], |r| r.get(0))?;
                let recorded = setting(&ctx.conn, "audit_id_sequence", "0")?
                    .parse::<i64>()
                    .unwrap_or(0);
                set_setting(
                    &ctx.conn,
                    "audit_id_sequence",
                    &highest.max(recorded).to_string(),
                )?;
            }
            for id in ids.iter().take(limit) {
                if area == "links" {
                    archive(ctx, id.as_str().unwrap(), who, sid)?;
                    n += 1;
                } else if area == "events" {
                    ctx.conn
                        .execute("DELETE FROM outbox WHERE id=?", [id.as_str().unwrap()])?;
                    n += ctx
                        .conn
                        .execute("DELETE FROM events WHERE id=?", [id.as_str().unwrap()])?;
                } else {
                    n += ctx
                        .conn
                        .execute("DELETE FROM audit WHERE id=?", [id.as_i64().unwrap()])?;
                }
            }
        }
        changed[area] = json!(n);
    }
    if changed
        .as_object()
        .unwrap()
        .values()
        .any(|v| v.as_i64().unwrap_or(0) > 0)
    {
        audit(
            &ctx.conn,
            who,
            "maintenance.cleanup",
            sid.unwrap_or("platform"),
            sid,
        )?;
    }
    Ok(
        json!({"ok":true,"dry_run":dry,"shop_id":sid,"product_id":pid,"eligible":eligible,"changed":changed,"has_more":has_more,"policy":pol}),
    )
}
fn scope(ctx: &Context, s: &Value, pid: Option<&str>, requested: &str) -> Result<Option<String>> {
    let mut sid = if text(s, "role") == "staff" {
        Some(text(s, "shop_id").to_owned())
    } else {
        auth::selected_shop(ctx, s, requested)?
    };
    if let Some(pid) = pid {
        let p = auth::authorize_product(ctx, s, pid)?;
        if sid.as_ref().is_some_and(|s| s != text(&p, "shop_id")) {
            return Err(ApiError::new(403, "商品不属于此店铺"));
        }
        sid = Some(text(&p, "shop_id").to_owned());
    }
    Ok(sid)
}
pub fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    let delegated = ctx.path == "/api/manage/links/cleanup";
    if !ctx.path.starts_with("/api/admin/maintenance") && !delegated {
        return Ok(None);
    }
    let session = auth::session(
        ctx,
        if delegated {
            &["admin", "staff"]
        } else {
            &["admin"]
        },
        if delegated {
            Some("links.delegate")
        } else {
            None
        },
    )?;
    let pid = if !ctx.b("product_id").is_empty() {
        Some(ctx.b("product_id"))
    } else if delegated && !text(&session, "product_id").is_empty() {
        Some(text(&session, "product_id"))
    } else {
        None
    };
    let sid = scope(
        ctx,
        &session,
        pid,
        if ctx.b("shop_id").is_empty() {
            ctx.q("shop_id")
        } else {
            ctx.b("shop_id")
        },
    )?;
    if ctx.is("GET", "/api/admin/maintenance") {
        let mut counts = json!({"links_active":0,"links_history":0,"links_archived":0});
        for r in rows(
            &ctx.conn,
            "SELECT s.* FROM staff s JOIN products p ON p.id=s.product_id WHERE (? IS NULL OR p.shop_id=?)",
            params![sid, sid],
        )? {
            let key = format!("links_{}", link_state(ctx, &r));
            counts[&key] = json!(integer(&counts, &key, 0) + 1);
        }
        let events = required(
            &ctx.conn,
            "SELECT COUNT(*) AS total,SUM(o.state='pending') AS pending FROM events e LEFT JOIN products p ON p.id=e.product_id LEFT JOIN outbox o ON o.id=e.id WHERE (? IS NULL OR p.shop_id=?)",
            params![sid, sid],
            "统计不可用",
        )?;
        counts["events_total"] = events["total"].clone();
        counts["events_pending"] = json!(integer(&events, "pending", 0));
        counts["audit_total"] = required(
            &ctx.conn,
            "SELECT COUNT(*) AS n FROM audit WHERE (? IS NULL OR shop_id=?)",
            params![sid, sid],
            "统计不可用",
        )?["n"]
            .clone();
        return Ok(Some(Reply::json(
            json!({"ok":true,"shop_id":sid,"policy":policy(ctx,sid.as_deref())?,"counts":counts}),
        )));
    }
    if ctx.is("PUT", "/api/admin/maintenance/policy") {
        auth::require_recent(&session)?;
        let mut pol = policy(ctx, None)?;
        let input = ctx
            .body
            .as_object()
            .ok_or_else(|| ApiError::bad("保留策略无效"))?;
        for (k, v) in input {
            if k != "shop_id" {
                if !pol.as_object().unwrap().contains_key(k) {
                    return Err(ApiError::bad("保留策略字段无效"));
                }
                pol[k] = v.clone();
            }
        }
        validate_policy(&pol)?;
        set_setting(
            &ctx.conn,
            &format!(
                "maintenance_policy:{}",
                sid.as_deref().unwrap_or("platform")
            ),
            &pol.to_string(),
        )?;
        audit(
            &ctx.conn,
            &actor(&session),
            "maintenance.policy",
            sid.as_deref().unwrap_or("platform"),
            sid.as_deref(),
        )?;
        return Ok(Some(Reply::json(
            json!({"ok":true,"shop_id":sid,"policy":policy(ctx,sid.as_deref())?}),
        )));
    }
    if ctx.method.as_str() == "POST" && (delegated || ctx.path == "/api/admin/maintenance/cleanup")
    {
        let dry = ctx
            .body
            .get("dry_run")
            .map(|v| {
                v.as_bool()
                    .ok_or_else(|| ApiError::bad("预览标记必须是布尔值"))
            })
            .transpose()?
            .unwrap_or(true);
        if !dry && text(&session, "role") == "admin" {
            auth::require_recent(&session)?;
        }
        let raw = ctx.body["areas"].as_array();
        let areas = if delegated {
            vec!["links"]
        } else if let Some(raw) = raw {
            raw.iter()
                .map(|v| v.as_str().ok_or_else(|| ApiError::bad("清理类别无效")))
                .collect::<Result<Vec<_>>>()?
        } else {
            vec!["links", "events", "audit"]
        };
        let limit = ctx
            .body
            .get("limit")
            .map(|v| v.as_u64().ok_or_else(|| ApiError::bad("清理批次数量无效")))
            .transpose()?
            .unwrap_or(100);
        let staff = if text(&session, "role") == "staff" {
            Some(text(&session, "staff_id"))
        } else {
            None
        };
        return Ok(Some(Reply::json(cleanup(
            ctx,
            sid.as_deref(),
            pid,
            staff,
            &areas,
            dry,
            limit as usize,
            false,
            false,
            &actor(&session),
        )?)));
    }
    Ok(None)
}
pub fn once(app: Arc<App>) -> Result<bool> {
    static LAST: AtomicU64 = AtomicU64::new(0);
    let time = now() as u64;
    let old = LAST.load(Ordering::Relaxed);
    if time.saturating_sub(old) < 60
        || LAST
            .compare_exchange(old, time, Ordering::SeqCst, Ordering::Relaxed)
            .is_err()
    {
        return Ok(false);
    }
    crate::worker::transaction(app, |ctx| {
        let cursor = setting(&ctx.conn, "maintenance_cursor", "")?;
        let shops = rows(
            &ctx.conn,
            "SELECT id FROM shops WHERE id>? ORDER BY id LIMIT 21",
            [cursor],
        )?;
        for shop in shops.iter().take(20) {
            let sid = text(shop, "id");
            if flag(&policy(ctx, Some(sid))?, "enabled") {
                cleanup(
                    ctx,
                    Some(sid),
                    None,
                    None,
                    &["links", "events", "audit"],
                    false,
                    100,
                    true,
                    false,
                    "system",
                )?;
            }
        }
        set_setting(
            &ctx.conn,
            "maintenance_cursor",
            if shops.len() > 20 {
                text(&shops[19], "id")
            } else {
                ""
            },
        )?;
        if flag(&policy(ctx, None)?, "enabled") {
            cleanup(
                ctx,
                None,
                None,
                None,
                &["audit"],
                false,
                100,
                true,
                true,
                "system",
            )?;
        }
        Ok(false)
    })
}

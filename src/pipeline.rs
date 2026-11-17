//! Explicit, key-bound product snapshots approved in the merchant's browser.
use crate::{auth, catalog, common::*};
use rusqlite::params;
use serde_json::{Value, json};
use std::collections::BTreeSet;
mod instructions;
mod requests;

fn invalid() -> ApiError {
    ApiError::bad("商品流水线授权参数无效")
}
fn invalid_scope() -> ApiError {
    ApiError::new(401, "商品流水线授权已过期或撤销")
}
pub fn permissions(value: &Value, kind: &str) -> Result<Vec<String>> {
    if !["product", "shop.pipeline"].contains(&kind) {
        return Err(invalid());
    }
    let list = value.as_array().ok_or_else(invalid)?;
    let unique = list
        .iter()
        .filter_map(Value::as_str)
        .collect::<BTreeSet<_>>();
    if list.is_empty()
        || unique.len() != list.len()
        || unique.iter().any(|p| {
            !auth::PERMISSIONS.contains(p)
                || kind == "shop.pipeline"
                    && ![
                        "queue.view",
                        "queue.process",
                        "queue.retry",
                        "queue.monitor",
                    ]
                    .contains(p)
        })
        || unique.contains("queue.process") && !unique.contains("queue.view")
        || unique.contains("queue.retry") && !unique.contains("queue.view")
        || unique.contains("fulfillment.configure") && !unique.contains("product.edit")
    {
        return Err(invalid());
    }
    Ok(auth::PERMISSIONS
        .iter()
        .filter(|p| unique.contains(*p))
        .map(|p| p.to_string())
        .collect())
}
fn stored_permissions(r: &Value) -> Result<Vec<String>> {
    permissions(&json_field(r, "permissions", Value::Null), text(r, "kind"))
}
pub fn current(ctx: &Context, aid: &str) -> Result<Value> {
    let r = row(
        &ctx.conn,
        "SELECT * FROM pipeline_authorizations WHERE id=? AND revoked=0 AND expires>?",
        params![aid, now()],
    )?
    .ok_or_else(invalid_scope)?;
    if !number(&r, "expires").is_finite() {
        return Err(invalid_scope());
    }
    auth::shop(ctx, text(&r, "shop_id"))?;
    stored_permissions(&r)?;
    Ok(r)
}
fn product(ctx: &Context, pid: &str, sid: &str, kind: &str) -> Result<Value> {
    let p = required(
        &ctx.conn,
        "SELECT shop_id,config FROM products WHERE id=?",
        [pid],
        "商品不存在",
    )?;
    if text(&p, "shop_id") != sid {
        return Err(ApiError::new(403, "商品不属于本次授权的店铺"));
    }
    if kind == "shop.pipeline" && text(&parse(text(&p, "config"))?, "mode") != "manual" {
        return Err(ApiError::new(403, "店铺流水线授权只能处理队列商品"));
    }
    Ok(p)
}
pub fn view(ctx: &Context, r: &Value) -> Result<Value> {
    let mut v = json!({});
    for key in [
        "id",
        "shop_id",
        "kind",
        "expires",
        "revision",
        "created",
        "last_seen",
        "client_name",
        "agent_type",
        "fingerprint",
        "approved_actor",
        "issuer_role",
        "issuer_shop_id",
    ] {
        v[key] = r[key].clone();
    }
    v["permissions"] = json!(stored_permissions(r)?);
    v["product_ids"] = json!(
        rows(
            &ctx.conn,
            "SELECT product_id FROM pipeline_bindings WHERE authorization_id=? ORDER BY product_id",
            [text(r, "id")]
        )?
        .iter()
        .map(|v| text(v, "product_id").to_owned())
        .collect::<Vec<_>>()
    );
    Ok(v)
}
pub fn authorization(ctx: &Context, aid: &str) -> Result<Value> {
    view(ctx, &current(ctx, aid)?)
}
pub fn bindings(ctx: &Context, aid: &str) -> Result<Vec<Value>> {
    let scope = current(ctx, aid)?;
    let mut result = vec![];
    for binding in rows(
        &ctx.conn,
        "SELECT * FROM pipeline_bindings WHERE authorization_id=? ORDER BY product_id",
        [aid],
    )? {
        let (d, staff) = auth::cli::device(ctx, text(&binding, "device_id"))?;
        if binding["staff_id"] != staff["id"]
            || binding["product_id"] != staff["product_id"]
            || staff["shop_id"] != scope["shop_id"]
            || d["public_key"] != scope["public_key"]
            || d["fingerprint"] != scope["fingerprint"]
        {
            return Err(invalid_scope());
        }
        let mut v = auth::cli::bind_view(&d, &staff, true);
        v["staff_id"] = staff["id"].clone();
        v["authorization_id"] = json!(aid);
        v["authorization_revision"] = scope["revision"].clone();
        result.push(v);
    }
    Ok(result)
}
fn strings(v: &Value) -> Result<Vec<String>> {
    v.as_array()
        .ok_or_else(invalid)?
        .iter()
        .map(|v| {
            v.as_str()
                .filter(|s| !s.is_empty() && s.len() <= 100)
                .map(str::to_owned)
                .ok_or_else(invalid)
        })
        .collect()
}
fn draft(ctx: &Context, value: &Value, existing: Option<&Value>) -> Result<Value> {
    let o = value.as_object().ok_or_else(invalid)?;
    if o.keys().any(|k| {
        !["shop_id", "kind", "product_ids", "permissions", "expires"].contains(&k.as_str())
    }) {
        return Err(invalid());
    }
    let sid = text(value, "shop_id");
    let kind = text(value, "kind");
    auth::shop(ctx, sid)?;
    let mut pids = strings(&value["product_ids"])?;
    let unique = pids.iter().collect::<BTreeSet<_>>();
    if pids.is_empty()
        || pids.len() > 500
        || unique.len() != pids.len()
        || kind == "product" && pids.len() != 1
    {
        return Err(ApiError::bad("请选择本次授权的商品"));
    }
    let perms = permissions(&value["permissions"], kind)?;
    let expires = value
        .get("expires")
        .filter(|v| !v.is_null())
        .and_then(Value::as_f64)
        .unwrap_or_else(|| existing.map_or(now() + 7.0 * 86400.0, |r| number(r, "expires")));
    if !expires.is_finite() || expires <= now() || expires > now() + 90.0 * 86400.0 {
        return Err(ApiError::bad("商品流水线授权期限无效"));
    }
    let mut old = BTreeSet::new();
    if let Some(existing) = existing {
        if text(existing, "shop_id") != sid
            || text(existing, "kind") != kind
            || number(existing, "expires") != expires
        {
            return Err(ApiError::new(403, "追加授权不能更改店铺、类型或有效期"));
        }
        for p in rows(
            &ctx.conn,
            "SELECT product_id FROM pipeline_bindings WHERE authorization_id=?",
            [text(existing, "id")],
        )? {
            old.insert(text(&p, "product_id").to_owned());
        }
        if stored_permissions(existing)?
            .iter()
            .any(|p| !perms.contains(p))
            || old.iter().any(|p| !pids.contains(p))
        {
            return Err(ApiError::new(403, "追加授权不能缩减或替换原来的商品及权限"));
        }
    }
    for pid in &pids {
        product(ctx, pid, sid, kind)?;
        if !old.contains(pid) {
            catalog::require_active(ctx, pid)?;
        }
    }
    pids.sort();
    Ok(json!({"shop_id":sid,"kind":kind,"product_ids":pids,"permissions":perms,"expires":expires}))
}
fn recovery(
    ctx: &Context,
    sid: &str,
    kind: &str,
    key: &str,
    pids: &[String],
    perms: &[String],
) -> Result<Option<Value>> {
    let mut wanted = pids.to_vec();
    wanted.sort();
    for candidate in rows(
        &ctx.conn,
        "SELECT * FROM pipeline_authorizations WHERE shop_id=? AND kind=? AND public_key=? AND revoked=0 AND expires>? ORDER BY created,id",
        params![sid, kind, key, now()],
    )? {
        let v = view(ctx, &candidate)?;
        if v["permissions"] == json!(perms) && v["product_ids"] == json!(wanted) {
            current(ctx, text(&v, "id"))?;
            bindings(ctx, text(&v, "id"))?;
            return Ok(Some(v));
        }
    }
    Ok(None)
}
fn materialize(ctx: &Context, value: &Value, request: &Value, owner: &Value) -> Result<Value> {
    let key = text(request, "public_key");
    let fingerprint = auth::cli::fingerprint(&auth::cli::decode(key, 32)?);
    if fingerprint != text(request, "fingerprint") {
        return Err(invalid());
    }
    let (name, agent) =
        auth::cli::identity(text(request, "client_name"), request["agent_type"].as_str())?;
    let existing = if let Some(aid) = request["current_authorization_id"].as_str() {
        Some(current(ctx, aid)?)
    } else {
        None
    };
    if let Some(e) = &existing {
        if e["public_key"] != request["public_key"]
            || e["fingerprint"] != request["fingerprint"]
            || e["revision"] != request["current_revision"]
        {
            return Err(ApiError::new(409, "流水线授权已变化，请重新申请追加权限"));
        }
        bindings(ctx, text(e, "id"))?;
    }
    let value = draft(ctx, value, existing.as_ref())?;
    let sid = text(&value, "shop_id");
    let kind = text(&value, "kind");
    let pids = strings(&value["product_ids"])?;
    let perms = permissions(&value["permissions"], kind)?;
    let expires = number(&value, "expires");
    let issuer = if owner["shop_id"].is_null() {
        "root"
    } else {
        "shop"
    };
    if issuer == "shop" && text(owner, "shop_id") != sid {
        return Err(ApiError::new(403, "流水线授权批准者与店铺不匹配"));
    }
    if existing.is_none() {
        if recovery(ctx, sid, kind, key, &pids, &perms)?.is_some() {
            return Err(ApiError::new(
                409,
                "已有相同流水线授权，请重新申请并确认恢复范围",
            ));
        }
        for (sql, limit) in [
            (
                "SELECT COUNT(*) AS n FROM pipeline_authorizations WHERE revoked=0 AND expires>?1 AND (?2 IS NOT NULL)",
                10000,
            ),
            (
                "SELECT COUNT(*) AS n FROM pipeline_authorizations WHERE public_key=?2 AND revoked=0 AND expires>?1",
                20,
            ),
            (
                "SELECT COUNT(*) AS n FROM pipeline_authorizations WHERE ?1 IS NOT NULL AND ?2 IS NOT NULL",
                50000,
            ),
            (
                "SELECT COUNT(*) AS n FROM pipeline_authorizations WHERE public_key=?2 AND ?1 IS NOT NULL",
                100,
            ),
        ] {
            if integer(
                &required(&ctx.conn, sql, params![now(), key], "授权计数无效")?,
                "n",
                0,
            ) >= limit
            {
                return Err(ApiError::new(
                    409,
                    "流水线授权已达到上限，请撤销不再使用的授权",
                ));
            }
        }
    }
    let aid = existing
        .as_ref()
        .map(|e| text(e, "id").to_owned())
        .unwrap_or_else(id);
    let old = rows(
        &ctx.conn,
        "SELECT product_id FROM pipeline_bindings WHERE authorization_id=?",
        [&aid],
    )?
    .iter()
    .map(|p| text(p, "product_id").to_owned())
    .collect::<BTreeSet<_>>();
    let encoded = serde_json::to_string(&perms)?;
    let who = actor(owner);
    let changed = existing.as_ref().is_none_or(|e| {
        stored_permissions(e).ok() != Some(perms.clone())
            || pids.iter().cloned().collect::<BTreeSet<_>>() != old
            || text(e, "issuer_role") != issuer
            || e["issuer_shop_id"] != owner["shop_id"]
            || text(e, "client_name") != name
            || e["agent_type"].as_str() != agent.as_deref()
    });
    if let Some(e) = &existing {
        if changed {
            ctx.conn.execute("UPDATE pipeline_authorizations SET permissions=?,revision=revision+1,last_seen=?,approved_actor=?,issuer_role=?,issuer_shop_id=?,client_name=?,agent_type=? WHERE id=? AND revision=? AND revoked=0",params![encoded,now(),who,issuer,owner["shop_id"].as_str(),name,agent,aid,integer(e,"revision",1)])?;
            ctx.conn.execute("UPDATE staff SET permissions=? WHERE id IN (SELECT staff_id FROM pipeline_bindings WHERE authorization_id=?)",params![encoded,aid])?;
            ctx.conn.execute("UPDATE cli_devices SET client_name=?,agent_type=? WHERE id IN (SELECT device_id FROM pipeline_bindings WHERE authorization_id=?)",params![name,agent,aid])?;
            if text(e, "issuer_role") != issuer {
                audit(
                    &ctx.conn,
                    &who,
                    &format!("cli.scope.issuer.{}_to_{issuer}", text(e, "issuer_role")),
                    &aid,
                    Some(sid),
                )?;
            }
        }
    } else {
        ctx.conn.execute("INSERT INTO pipeline_authorizations(id,shop_id,public_key,fingerprint,client_name,kind,permissions,expires,created,last_seen,approved_actor,issuer_role,issuer_shop_id,agent_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",params![aid,sid,key,fingerprint,name,kind,encoded,expires,now(),now(),who,issuer,owner["shop_id"].as_str(),agent])?;
    }
    for pid in &pids {
        if old.contains(pid) {
            continue;
        }
        let staffid = id();
        ctx.conn.execute("INSERT INTO staff(id,digest,product_id,name,expires,permissions,created,max_uses,uses,max_cli_uses,cli_uses) VALUES (?,?,?,?,?,?,?,0,0,1,0)",params![staffid,digest(&token()),pid,format!("CLI · {name}"),expires,encoded,now()])?;
        let staff = auth::staff_authorization(ctx, &staffid)?;
        let (device, _) =
            auth::cli::create_staff_device(ctx, &staff, key, &name, agent.as_deref())?;
        ctx.conn.execute(
            "INSERT INTO pipeline_bindings VALUES (?,?,?,?,?)",
            params![aid, pid, staffid, text(&device, "id"), now()],
        )?;
    }
    audit(
        &ctx.conn,
        &who,
        if existing.is_none() {
            "cli.scope.create"
        } else if changed {
            "cli.scope.upgrade"
        } else {
            "cli.scope.recover"
        },
        &aid,
        Some(sid),
    )?;
    let mut bindings = bindings(ctx, &aid)?;
    for binding in &mut bindings {
        binding["already_authorized"] = json!(old.contains(text(binding, "product_id")));
    }
    Ok(json!({"authorization":authorization(ctx,&aid)?,"bindings":bindings}))
}
pub fn revoke(ctx: &Context, aid: &str, who: &str) -> Result<Value> {
    let Some(scope) = row(
        &ctx.conn,
        "SELECT * FROM pipeline_authorizations WHERE id=?",
        [aid],
    )?
    else {
        return Ok(json!({"authorizations":0,"bindings":0,"jobs":0}));
    };
    if flag(&scope, "revoked") {
        return Ok(json!({"authorizations":0,"bindings":0,"jobs":0}));
    }
    ctx.conn.execute(
        "UPDATE pipeline_authorizations SET revoked=1 WHERE id=?",
        [aid],
    )?;
    let bindings = rows(
        &ctx.conn,
        "SELECT * FROM pipeline_bindings WHERE authorization_id=?",
        [aid],
    )?;
    let mut jobs = 0;
    for binding in &bindings {
        let descendants = rows(
            &ctx.conn,
            "WITH RECURSIVE tree(id) AS (SELECT id FROM staff WHERE id=? UNION SELECT s.id FROM staff s JOIN tree t ON s.parent_id=t.id WHERE s.product_id=?) SELECT id FROM tree",
            params![text(binding, "staff_id"), text(binding, "product_id")],
        )?;
        for staff in descendants {
            let sid = text(&staff, "id");
            jobs += integer(
                &required(
                    &ctx.conn,
                    "SELECT COUNT(*) AS n FROM jobs WHERE product_id=? AND claimed_by=? AND state='processing'",
                    params![text(binding, "product_id"), sid],
                    "任务计数无效",
                )?,
                "n",
                0,
            );
            ctx.conn
                .execute("UPDATE staff SET revoked=1 WHERE id=?", [sid])?;
            ctx.conn
                .execute("UPDATE cli_devices SET revoked=1 WHERE staff_id=?", [sid])?;
            crate::fulfillment::release_claims(ctx, sid)?;
            for session in rows(
                &ctx.conn,
                "SELECT digest FROM sessions WHERE staff_id=? AND revoked=0",
                [sid],
            )? {
                auth::revoke_session(ctx, text(&session, "digest"), who, "session.revoke")?;
            }
            ctx.conn.execute("UPDATE jobs SET state='queued',claimed_by=NULL,lease=NULL,updated=? WHERE product_id=? AND claimed_by=? AND state='processing'",params![now(),text(binding,"product_id"),sid])?;
        }
    }
    audit(
        &ctx.conn,
        who,
        "cli.scope.revoke",
        aid,
        Some(text(&scope, "shop_id")),
    )?;
    Ok(json!({"authorizations":1,"bindings":bindings.len(),"jobs":jobs}))
}
pub fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    if let Some(r) = instructions::handle(ctx)? {
        return Ok(Some(r));
    }
    requests::handle(ctx)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn pipeline_scope_never_grants_admin_capabilities() {
        assert!(permissions(&json!(["queue.view", "product.edit"]), "shop.pipeline").is_err());
        assert!(permissions(&json!(["queue.view", "queue.view"]), "product").is_err());
        assert!(permissions(&json!(["queue.process"]), "shop.pipeline").is_err());
        assert_eq!(
            permissions(&json!(["queue.retry", "queue.view"]), "shop.pipeline").unwrap(),
            vec!["queue.view", "queue.retry"]
        );
    }
}

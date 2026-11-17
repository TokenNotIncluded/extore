use super::*;
use rusqlite::types::Value as SqlValue;
use subtle::ConstantTimeEq;

pub fn link_scope(ctx: &Context, s: &Value) -> Result<Option<HashSet<String>>> {
    if text(s, "role") == "admin" {
        let Some(sid) = s["shop_id"].as_str() else {
            return Ok(None);
        };
        let values = rows(
            &ctx.conn,
            "WITH RECURSIVE scoped_links(id,shop_id) AS (SELECT b.staff_id,a.shop_id FROM pipeline_bindings b JOIN pipeline_authorizations a ON a.id=b.authorization_id UNION SELECT child.id,scoped_links.shop_id FROM staff child JOIN scoped_links ON child.parent_id=scoped_links.id) SELECT st.id FROM staff st JOIN products p ON p.id=st.product_id WHERE (p.shop_id=? AND NOT EXISTS(SELECT 1 FROM scoped_links WHERE id=st.id)) OR (EXISTS(SELECT 1 FROM scoped_links WHERE id=st.id AND shop_id=?) AND NOT EXISTS(SELECT 1 FROM scoped_links WHERE id=st.id AND shop_id!=?))",
            params![sid, sid, sid],
        )?;
        return Ok(Some(
            values
                .into_iter()
                .map(|v| text(&v, "id").to_owned())
                .collect(),
        ));
    }
    if text(s, "role") != "staff" {
        return Err(denied());
    }
    let delegate = s["permissions"]
        .as_array()
        .is_some_and(|a| a.iter().any(|v| v == "links.delegate"));
    let values = if delegate {
        rows(
            &ctx.conn,
            "WITH RECURSIVE descendants(id) AS (SELECT ? UNION SELECT st.id FROM staff st JOIN descendants d ON st.parent_id=d.id) SELECT st.id FROM staff st JOIN descendants d ON st.id=d.id WHERE st.product_id=?",
            params![text(s, "staff_id"), text(s, "product_id")],
        )?
    } else {
        vec![json!({"id":s["staff_id"]})]
    };
    Ok(Some(
        values
            .into_iter()
            .map(|v| text(&v, "id").to_owned())
            .collect(),
    ))
}
fn visible(s: &Value, scope: &Option<HashSet<String>>, item: &Value) -> bool {
    scope.as_ref().is_none_or(|set| {
        (text(item, "role") == "staff" && set.contains(text(item, "staff_id")))
            || (text(s, "role") == "admin"
                && !s["shop_id"].is_null()
                && text(item, "role") == "admin"
                && s["shop_id"] == item["shop_id"])
    })
}
fn session_rows(ctx: &Context, s: &Value, scope: &Option<HashSet<String>>) -> Result<Vec<Value>> {
    Ok(rows(&ctx.conn,"SELECT se.*,st.name link_name,st.product_id,p.config product_config,p.shop_id product_shop_id,COALESCE(d.fingerprint,o.fingerprint) device_fingerprint,COALESCE(d.revoked,o.revoked) device_revoked,o.expires owner_grant_expires FROM sessions se LEFT JOIN staff st ON st.id=se.staff_id LEFT JOIN products p ON p.id=st.product_id LEFT JOIN cli_devices d ON d.id=se.device_id AND d.staff_id=se.staff_id LEFT JOIN owner_cli_devices o ON o.id=se.owner_device_id AND o.shop_id IS se.shop_id ORDER BY se.created DESC,se.id",[])?.into_iter().filter(|v|visible(s,scope,v)).collect())
}
fn session_view(ctx: &Context, value: &Value, current: &str) -> Result<Value> {
    let mut active = !flag(value, "revoked") && number(value, "expires") > now();
    if text(value, "channel") == "cli"
        && (value["device_revoked"].is_null() || flag(value, "device_revoked"))
    {
        active = false;
    }
    if !text(value, "owner_device_id").is_empty() {
        active = active
            && cli::owner_device(ctx, text(value, "owner_device_id"))
                .is_ok_and(|d| d["shop_id"] == value["shop_id"]);
    }
    if active && text(value, "role") == "staff" {
        active = if text(value, "channel") == "cli" {
            cli::device(ctx, text(value, "device_id")).is_ok()
        } else {
            staff_authorization(ctx, text(value, "staff_id")).is_ok()
        };
    }
    if active {
        if let Some(sid) = value["shop_id"].as_str() {
            active = shop(ctx, sid).is_ok();
        }
    }
    let name = if !text(value, "product_config").is_empty()
        && (value["shop_id"].is_null() || value["shop_id"] == value["product_shop_id"])
    {
        parse(text(value, "product_config"))
            .ok()
            .map(|v| v["name"].clone())
            .unwrap_or(Value::Null)
    } else {
        Value::Null
    };
    let mut result = json!({"link_id":value["staff_id"],"product_name":name,"revoked":flag(value,"revoked"),"current":bool::from(text(value,"digest").as_bytes().ct_eq(current.as_bytes())),"active":active,"ip":text(value,"ip").chars().take(100).collect::<String>(),"ua":text(value,"ua").chars().take(300).collect::<String>(),"device_id":if value["device_id"].is_null(){value["owner_device_id"].clone()}else{value["device_id"].clone()},"fingerprint":value["device_fingerprint"]});
    for k in [
        "id",
        "role",
        "shop_id",
        "link_name",
        "product_id",
        "created",
        "last_seen",
        "expires",
        "channel",
        "client_name",
        "owner_device_id",
    ] {
        result[k] = value[k].clone();
    }
    Ok(result)
}
const ACTIONS: &[&str] = &[
    "session.create",
    "session.replace",
    "session.logout",
    "session.revoke",
    "session.link_revoke",
    "session.bootstrap_complete",
    "session.auth_reset",
    "link.consume",
    "link.cli_consume",
    "cli.device.create",
    "cli.device.revoke",
    "cli.ticket.create",
    "cli.product.request",
    "cli.product.approve",
    "cli.product.deny",
    "cli.product.claim",
    "cli.scope.request",
    "cli.scope.approve",
    "cli.scope.deny",
    "cli.scope.claim",
    "cli.scope.create",
    "cli.scope.upgrade",
    "cli.scope.recover",
    "cli.scope.revoke",
    "cli.scope.issuer.root_to_shop",
    "cli.scope.issuer.shop_to_root",
    "cli.owner.request",
    "cli.owner.approve",
    "cli.owner.device.create",
    "cli.owner.device.revoke",
    "cli.owner.action",
    "staff.create",
    "staff.revoke",
    "staff.archive",
];
fn ids(ctx: &Context, table: &str) -> Result<HashSet<String>> {
    Ok(rows(&ctx.conn, &format!("SELECT id FROM {table}"), [])?
        .iter()
        .map(|v| text(v, "id").to_owned())
        .collect())
}
fn audit_metadata(ctx: &Context, value: &Value) -> Result<Value> {
    let target = text(value, "target");
    for table in [
        "cli_scope_requests",
        "cli_device_requests",
        "pipeline_authorizations",
    ] {
        if let Some(mut metadata) = row(
            &ctx.conn,
            &format!("SELECT client_name,fingerprint FROM {table} WHERE id=?"),
            [target],
        )? {
            metadata["channel"] = json!("cli");
            return Ok(metadata);
        }
    }
    if let Some(metadata) = row(
        &ctx.conn,
        "SELECT se.channel,se.client_name,COALESCE(d.fingerprint,o.fingerprint) fingerprint FROM sessions se LEFT JOIN cli_devices d ON d.id=se.device_id AND d.staff_id=se.staff_id LEFT JOIN owner_cli_devices o ON o.id=se.owner_device_id AND o.shop_id IS se.shop_id WHERE se.id=?",
        [target],
    )? {
        return Ok(metadata);
    }
    for table in ["cli_devices", "owner_cli_devices", "owner_cli_requests"] {
        if let Some(mut metadata) = row(
            &ctx.conn,
            &format!("SELECT client_name,fingerprint FROM {table} WHERE id=?"),
            [target],
        )? {
            metadata["channel"] = json!("cli");
            return Ok(metadata);
        }
    }
    Ok(
        json!({"channel":if ["link.cli_consume","cli.ticket.create"].contains(&text(value,"action")){"cli"}else{"browser"},"client_name":null,"fingerprint":null}),
    )
}
fn list_audit(
    ctx: &Context,
    s: &Value,
    scope: &Option<HashSet<String>>,
    limit: i64,
) -> Result<Value> {
    let mut safe = HashSet::new();
    for table in [
        "sessions",
        "staff",
        "cli_devices",
        "owner_cli_devices",
        "owner_cli_requests",
        "cli_device_requests",
        "pipeline_authorizations",
        "cli_scope_requests",
    ] {
        safe.extend(ids(ctx, table)?);
    }
    let targets = if let Some(links) = scope {
        let mut allowed = links.clone();
        for row in session_rows(ctx, s, scope)? {
            allowed.insert(text(&row, "id").to_owned());
        }
        for d in rows(&ctx.conn, "SELECT id,staff_id FROM cli_devices", [])? {
            if links.contains(text(&d, "staff_id")) {
                allowed.insert(text(&d, "id").to_owned());
            }
        }
        let sid = if text(s, "role") == "admin" {
            s["shop_id"].as_str()
        } else {
            None
        };
        if let Some(sid) = sid {
            for table in [
                "owner_cli_devices",
                "owner_cli_requests",
                "pipeline_authorizations",
            ] {
                for d in rows(
                    &ctx.conn,
                    &format!("SELECT id FROM {table} WHERE shop_id=?"),
                    [sid],
                )? {
                    allowed.insert(text(&d, "id").to_owned());
                }
            }
            for d in rows(
                &ctx.conn,
                "SELECT id FROM cli_scope_requests WHERE approved_shop_id=? OR authorization_id IN (SELECT id FROM pipeline_authorizations WHERE shop_id=?)",
                params![sid, sid],
            )? {
                allowed.insert(text(&d, "id").to_owned());
            }
        }
        for d in rows(
            &ctx.conn,
            "SELECT id,approved_staff_id,approved_shop_id FROM cli_device_requests",
            [],
        )? {
            if links.contains(text(&d, "approved_staff_id"))
                || sid.is_some_and(|sid| d["approved_shop_id"].as_str() == Some(sid))
            {
                allowed.insert(text(&d, "id").to_owned());
            }
        }
        Some(allowed)
    } else {
        None
    };
    if targets.as_ref().is_some_and(HashSet::is_empty) {
        return Ok(json!([]));
    }
    let mut parameters = ACTIONS
        .iter()
        .map(|s| SqlValue::Text(s.to_string()))
        .collect::<Vec<_>>();
    let mut sql = format!(
        "SELECT id,actor,action,target,created FROM audit WHERE action IN ({})",
        vec!["?"; ACTIONS.len()].join(",")
    );
    if let Some(targets) = targets {
        sql.push_str(&format!(
            " AND target IN ({})",
            vec!["?"; targets.len()].join(",")
        ));
        parameters.extend(targets.into_iter().map(SqlValue::Text));
    }
    sql.push_str(" ORDER BY created DESC,id DESC LIMIT ?");
    parameters.push(SqlValue::Integer(limit));
    let mut result = Vec::new();
    for mut value in rows(&ctx.conn, &sql, rusqlite::params_from_iter(parameters))? {
        if !safe.contains(text(&value, "target")) {
            continue;
        }
        let actor = text(&value, "actor");
        let valid = ["owner", "bootstrap", "ssh", "pending", "system"].contains(&actor)
            || if let Some(sid) = actor.strip_prefix("shop:") {
                row(&ctx.conn, "SELECT 1 FROM shops WHERE id=?", [sid])?.is_some()
            } else {
                row(&ctx.conn, "SELECT 1 FROM staff WHERE id=?", [actor])?.is_some()
            };
        if !valid {
            continue;
        }
        let metadata = audit_metadata(ctx, &value)?;
        value
            .as_object_mut()
            .unwrap()
            .extend(metadata.as_object().unwrap().clone());
        result.push(value);
    }
    Ok(json!(result))
}
pub fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    let path = ctx.path.clone();
    let (prefix, roles) = if path.starts_with("/api/admin/sessions") || path == "/api/admin/audit" {
        ("/api/admin", &["admin"][..])
    } else if path.starts_with("/api/manage/sessions") || path == "/api/manage/audit" {
        ("/api/manage", &["staff"][..])
    } else {
        return Ok(None);
    };
    let s = session(ctx, roles, None)?;
    let scope = link_scope(ctx, &s)?;
    if ctx.method.as_str() == "GET" && path == format!("{prefix}/sessions") {
        let result = session_rows(ctx, &s, &scope)?
            .iter()
            .map(|v| session_view(ctx, v, text(&s, "digest")))
            .collect::<Result<Vec<_>>>()?;
        return Ok(Some(Reply::json(json!(result))));
    }
    if ctx.method.as_str() == "DELETE" {
        if let Some(sid) = path.strip_prefix(&format!("{prefix}/sessions/")) {
            let value = required(
                &ctx.conn,
                "SELECT * FROM sessions WHERE id=?",
                [sid],
                "登录会话不存在",
            )?;
            if !visible(&s, &scope, &value) {
                return Err(ApiError::new(404, "登录会话不存在"));
            }
            let current = bool::from(
                text(&value, "digest")
                    .as_bytes()
                    .ct_eq(text(&s, "digest").as_bytes()),
            );
            revoke_session(ctx, text(&value, "digest"), &actor(&s), "session.revoke")?;
            return Ok(Some(Reply::json(
                json!({"ok":true,"id":value["id"],"current":current}),
            )));
        }
    }
    if ctx.method.as_str() == "GET" && path == format!("{prefix}/audit") {
        let limit = if ctx.q("limit").is_empty() {
            200
        } else {
            ctx.q("limit")
                .parse::<i64>()
                .ok()
                .filter(|v| (1..=200).contains(v))
                .ok_or_else(|| ApiError::bad("审计条数必须为 1–200"))?
        };
        return Ok(Some(Reply::json(list_audit(ctx, &s, &scope, limit)?)));
    }
    Ok(None)
}

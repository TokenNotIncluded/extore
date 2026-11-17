//! Public machine-readable contracts and side-effect-free draft validation.
use crate::{auth, common::*, fulfillment};
use serde_json::{Value, json};
use std::sync::OnceLock;

pub fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    if ctx.path == "/api/processor-contributions" && ctx.method.as_str() != "GET" {
        return Err(ApiError::new(405, "此接口只提供贡献规范"));
    }
    if ctx.is("GET", "/api/source") {
        return Ok(Some(Reply::json(
            json!({"url":"https://github.com/TokenNotIncluded/extore","stars":null}),
        )));
    }
    if ctx.is("GET", "/api/task-flows/schema") {
        return Ok(Some(Reply::bytes(
            "application/schema+json",
            include_bytes!("api/task-flow-schema.json").to_vec(),
        )?));
    }
    if ctx.is("GET", "/api/processor-contributions") {
        return Ok(Some(Reply::bytes(
            "application/json",
            include_bytes!("api/processor-contributions.json").to_vec(),
        )?));
    }
    if ctx.is("GET", "/openapi.json") {
        static SCHEMA: OnceLock<Value> = OnceLock::new();
        let v = SCHEMA.get_or_init(|| {
            let mut v: Value = serde_json::from_str(include_str!("api/openapi.json"))
                .expect("checked-in OpenAPI schema");
            v["info"]["version"] = json!(env!("CARGO_PKG_VERSION"));
            v["info"]["x-runtime"] = json!("rust");
            v
        });
        return Ok(Some(Reply::json(v.clone())));
    }
    if ctx.is("GET", "/docs") || ctx.is("GET", "/redoc") {
        return Ok(Some(Reply::bytes("text/html; charset=utf-8",br#"<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Extore API</title><link rel="stylesheet" href="/static/native-theme.css"><main><h1>Extore API</h1><p>Native Rust redemption and fulfillment service.</p><ul><li><a href="/openapi.json">OpenAPI contract</a></li><li><a href="/api/task-flows/schema">Task-flow JSON Schema</a></li><li><a href="/api/integrations/commerce/schema">Commerce import contract</a></li><li><a href="https://github.com/TokenNotIncluded/extore/tree/main/docs">Integration documentation</a></li></ul><p><a href="/">Back to Extore</a></p></main></html>"#.to_vec())?));
    }
    if ctx.method.as_str() == "POST"
        && [
            "/api/admin/task-flows/validate",
            "/api/manage/task-flows/validate",
        ]
        .contains(&ctx.path.as_str())
    {
        auth::session(
            ctx,
            if ctx.path.starts_with("/api/admin/") {
                &["admin"]
            } else {
                &["admin", "staff"]
            },
            Some("fulfillment.configure"),
        )?;
        let invalid = || ApiError::new(422, "工作流草稿格式无效");
        let body = ctx.body.as_object().ok_or_else(invalid)?;
        if body.len() != 2 || !body.contains_key("definition") || !body.contains_key("product") {
            return Err(invalid());
        }
        let obj = ctx.body["product"].as_object().ok_or_else(invalid)?;
        if obj
            .keys()
            .any(|k| !["mode", "parameters", "outputs"].contains(&k.as_str()))
        {
            return Err(invalid());
        }
        let mut p = json!({"mode":"manual","parameters":[],"outputs":[]});
        p.as_object_mut().unwrap().extend(obj.clone());
        if !["manual", "script", "webhook"].contains(&text(&p, "mode")) {
            return Err(invalid());
        }
        for k in ["parameters", "outputs"] {
            if p[k]
                .as_array()
                .is_none_or(|a| a.len() > 30 || a.iter().any(|v| !v.is_object()))
            {
                return Err(invalid());
            }
        }
        let definition =
            fulfillment::normalize_flow(&ctx.body["definition"], &p).map_err(|mut e| {
                e.status = 422;
                e
            })?;
        let nodes = definition["nodes"].as_array().cloned().unwrap_or_default();
        let mut kinds = json!({});
        for kind in ["input", "process", "display", "end"] {
            kinds[kind] = json!(nodes.iter().filter(|n| text(n, "kind") == kind).count());
        }
        return Ok(Some(Reply::json(
            json!({"ok":true,"definition":definition,"summary":{"version":definition["version"],"entry":definition["entry"],"node_count":nodes.len(),"nodes":kinds,"transition_limit":256}}),
        )));
    }
    Ok(None)
}

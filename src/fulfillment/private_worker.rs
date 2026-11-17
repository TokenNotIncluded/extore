//! Version-two private-worker signatures and exact action-scoped files.
use super::{flow, job};
use crate::{auth, catalog, common::*, files};
use hmac::{Hmac, Mac};
use rusqlite::params;
use serde::{
    Deserialize,
    de::{self, MapAccess, SeqAccess, Visitor},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{fmt, net::ToSocketAddrs, time::Duration};
use subtle::ConstantTimeEq;

const FIELDS: [&str; 7] = [
    "shop_id",
    "product_id",
    "job_id",
    "attempt",
    "node_id",
    "flow_epoch",
    "action_id",
];
const META: [&str; 7] = [
    "version",
    "audience",
    "direction",
    "timestamp",
    "nonce",
    "body-sha256",
    "signature",
];
const MAX_RESULT: usize = 256_000;
const MAX_FILE: usize = 20 * 1024 * 1024;
const INBOUND: &str = "worker-to-extore";
const OUTBOUND: &str = "extore-to-worker";

fn invalid() -> ApiError {
    ApiError::new(401, "私有处理器签名无效或过期")
}
fn valid_id(value: &str) -> bool {
    (1..=100).contains(&value.len())
        && value.as_bytes()[0].is_ascii_alphanumeric()
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_.:-".contains(&b))
}
fn valid_field(value: &str) -> bool {
    (1..=40).contains(&value.len())
        && value.as_bytes()[0].is_ascii_lowercase()
        && value
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
}
fn hex(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn hash(raw: &[u8]) -> String {
    format!("{:x}", Sha256::digest(raw))
}
fn canonical(value: &Value) -> Result<Vec<u8>> {
    Ok(auth::cli::ascii_json(value)?.into_bytes())
}
fn same(a: &str, b: &str) -> bool {
    bool::from(a.as_bytes().ct_eq(b.as_bytes()))
}

fn scope(context: &Value) -> Result<Value> {
    let mut result = json!({});
    for key in FIELDS {
        let value = &context[key];
        if matches!(key, "attempt" | "flow_epoch") {
            if value
                .as_i64()
                .is_none_or(|n| !(1..=9_007_199_254_740_991).contains(&n))
            {
                return Err(invalid());
            }
        } else if value.as_str().is_none_or(|s| !valid_id(s)) {
            return Err(invalid());
        }
        result[key] = value.clone();
    }
    Ok(result)
}

fn signature(
    secret: &str,
    scope: &Value,
    direction: &str,
    audience: &str,
    method: &str,
    path: &str,
    timestamp: &str,
    nonce: &str,
    body_digest: &str,
) -> Result<String> {
    if secret.is_empty()
        || ![INBOUND, OUTBOUND].contains(&direction)
        || audience.is_empty()
        || audience.len() > 2000
        || !audience.bytes().all(|b| (33..=126).contains(&b))
        || !["GET", "POST"].contains(&method)
        || !path.starts_with('/')
        || path.len() > 4096
        || !path.bytes().all(|b| (33..=126).contains(&b))
        || timestamp.is_empty()
        || timestamp.len() > 12
        || !timestamp.bytes().all(|b| b.is_ascii_digit())
        || !(16..=128).contains(&nonce.len())
        || !nonce
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
        || !hex(body_digest)
    {
        return Err(invalid());
    }
    let scope = self::scope(scope)?;
    let mut derivation =
        Hmac::<Sha256>::new_from_slice(secret.as_bytes()).map_err(|_| invalid())?;
    derivation.update(b"extore-private-worker-key-v2\0");
    derivation.update(direction.as_bytes());
    let mut mac = Hmac::<Sha256>::new_from_slice(&derivation.finalize().into_bytes())
        .map_err(|_| invalid())?;
    let mut values = vec![
        json!("extore-private-worker-signature-v2"),
        json!(direction),
        json!(audience),
        json!(method),
        json!(path),
        json!(timestamp),
        json!(nonce),
        json!(body_digest),
    ];
    values.extend(FIELDS.iter().map(|key| scope[*key].clone()));
    mac.update(&canonical(&json!(values))?);
    Ok(mac
        .finalize()
        .into_bytes()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect())
}

fn request_scope(ctx: &Context, pid: &str, jid: &str) -> Result<Value> {
    for key in FIELDS
        .iter()
        .map(|k| k.replace('_', "-"))
        .chain(META.iter().map(|k| k.to_string()))
    {
        if ctx
            .headers
            .get_all(format!("x-extore-{key}"))
            .iter()
            .count()
            != 1
        {
            return Err(invalid());
        }
    }
    if ctx.raw_target != ctx.path
        || ctx.path.contains('%')
        || !ctx.path.is_ascii()
        || !ctx.query.is_empty()
        || ctx.headers.contains_key("authorization")
        || ctx.header("x-extore-version") != "2"
        || ctx.header("x-extore-audience") != ctx.app.origin
        || ctx.header("x-extore-direction") != INBOUND
    {
        return Err(invalid());
    }
    let mut value = json!({});
    for key in FIELDS {
        let h = format!("x-extore-{}", key.replace('_', "-"));
        let raw = ctx.header(&h);
        value[key] = if matches!(key, "attempt" | "flow_epoch") {
            if raw.starts_with('0')
                || raw.is_empty()
                || raw.len() > 16
                || !raw.bytes().all(|b| b.is_ascii_digit())
            {
                return Err(invalid());
            }
            json!(raw.parse::<i64>().map_err(|_| invalid())?)
        } else {
            json!(raw)
        };
    }
    let value = scope(&value)?;
    if text(&value, "product_id") != pid || text(&value, "job_id") != jid {
        return Err(invalid());
    }
    Ok(value)
}

fn authority(ctx: &Context, s: &Value) -> Result<(Value, Value)> {
    let j = job(ctx, text(s, "job_id"))?;
    let a = flow::frozen_authority(
        ctx,
        &j,
        integer(s, "flow_epoch", 0),
        Some(integer(s, "attempt", 0)),
    )?;
    if scope(&a)? != *s || text(&a, "mode") != "webhook" {
        return Err(ApiError::new(409, "私有处理器执行范围已失效"));
    }
    Ok((j, a))
}
fn verify(ctx: &Context, s: &Value, body_digest: &str) -> Result<(Value, Value)> {
    let (j, a) = authority(ctx, s)?;
    let timestamp = ctx.header("x-extore-timestamp");
    let nonce = ctx.header("x-extore-nonce");
    let expected = signature(
        text(&a, "webhook_secret"),
        s,
        INBOUND,
        &ctx.app.origin,
        ctx.method.as_str(),
        &ctx.path,
        timestamp,
        nonce,
        body_digest,
    )?;
    let claimed = ctx.header("x-extore-body-sha256");
    let supplied = ctx.header("x-extore-signature");
    if timestamp
        .parse::<f64>()
        .map_or(true, |t| (now() - t).abs() > 300.0)
        || !hex(claimed)
        || !hex(supplied)
        || !same(claimed, body_digest)
        || !same(supplied, &expected)
    {
        return Err(invalid());
    }
    ctx.conn.execute(
        "DELETE FROM private_worker_nonces WHERE created<?",
        [now() - 900.0],
    )?;
    let nd = hash(&canonical(&json!([s["product_id"], INBOUND, nonce]))?);
    let inserted = ctx.conn.execute(
        "INSERT OR IGNORE INTO private_worker_nonces(digest,product_id,created) VALUES (?,?,?)",
        params![nd, text(s, "product_id"), now()],
    )?;
    if inserted != 1 {
        return Err(ApiError::new(409, "私有处理器请求已使用"));
    }
    Ok((j, a))
}
fn receipt(ctx: &Context, s: &Value, rid: &str, bd: &str, kind: &str) -> Result<Option<Value>> {
    let sd = hash(&canonical(&json!([s, kind]))?);
    let Some(r) = row(
        &ctx.conn,
        "SELECT body_digest,response FROM private_worker_receipts WHERE scope_digest=? AND result_id=?",
        params![sd, rid],
    )?
    else {
        return Ok(None);
    };
    if !same(text(&r, "body_digest"), bd) {
        return Err(ApiError::new(409, "同一结果编号不能更换内容"));
    }
    Ok(Some(parse(text(&r, "response"))?))
}
fn save_receipt(
    ctx: &Context,
    s: &Value,
    rid: &str,
    bd: &str,
    kind: &str,
    response: &Value,
) -> Result<()> {
    ctx.conn.execute("INSERT INTO private_worker_receipts(scope_digest,result_id,job_id,body_digest,response,created) VALUES (?,?,?,?,?,?)",params![hash(&canonical(&json!([s,kind]))?),rid,text(s,"job_id"),bd,auth::cli::ascii_json(response)?,now()])?;
    Ok(())
}
fn expired_reply() -> Reply {
    let mut r = Reply::json(json!({"detail":"此处理步骤已超时，请刷新任务状态"}));
    r.status = 409;
    r
}
fn active(ctx: &Context, j: &Value, s: &Value, output: bool) -> Result<Option<Value>> {
    if flow::expire_current(ctx, j, s)? {
        return Ok(None);
    }
    let v = if output {
        flow::output_file_scope(
            ctx,
            j,
            Some(integer(s, "flow_epoch", 0)),
            Some(text(s, "action_id")),
        )?
    } else {
        flow::execution(ctx, j, false)?
    };
    let v = v.ok_or_else(|| ApiError::new(409, "此处理步骤或尝试已失效"))?;
    if scope(&v)? != *s || text(&v, "mode") != "webhook" {
        return Err(ApiError::new(409, "此处理步骤或尝试已失效"));
    }
    Ok(Some(v))
}

// serde_json::Value normally accepts duplicate object keys. Signed requests do not.
struct Strict(Value);
impl<'de> Deserialize<'de> for Strict {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = Strict;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("strict JSON")
            }
            fn visit_unit<E: de::Error>(self) -> std::result::Result<Strict, E> {
                Ok(Strict(Value::Null))
            }
            fn visit_bool<E: de::Error>(self, v: bool) -> std::result::Result<Strict, E> {
                Ok(Strict(json!(v)))
            }
            fn visit_i64<E: de::Error>(self, v: i64) -> std::result::Result<Strict, E> {
                Ok(Strict(json!(v)))
            }
            fn visit_u64<E: de::Error>(self, v: u64) -> std::result::Result<Strict, E> {
                Ok(Strict(json!(v)))
            }
            fn visit_f64<E: de::Error>(self, v: f64) -> std::result::Result<Strict, E> {
                serde_json::Number::from_f64(v)
                    .map(|n| Strict(Value::Number(n)))
                    .ok_or_else(|| E::custom("nonfinite JSON"))
            }
            fn visit_str<E: de::Error>(self, v: &str) -> std::result::Result<Strict, E> {
                Ok(Strict(json!(v)))
            }
            fn visit_string<E: de::Error>(self, v: String) -> std::result::Result<Strict, E> {
                Ok(Strict(json!(v)))
            }
            fn visit_seq<A: SeqAccess<'de>>(
                self,
                mut a: A,
            ) -> std::result::Result<Strict, A::Error> {
                let mut v = Vec::new();
                while let Some(Strict(x)) = a.next_element::<Strict>()? {
                    v.push(x)
                }
                Ok(Strict(json!(v)))
            }
            fn visit_map<A: MapAccess<'de>>(
                self,
                mut a: A,
            ) -> std::result::Result<Strict, A::Error> {
                let mut v = serde_json::Map::new();
                while let Some(k) = a.next_key::<String>()? {
                    if v.contains_key(&k) {
                        return Err(de::Error::custom("duplicate JSON key"));
                    }
                    let Strict(x) = a.next_value::<Strict>()?;
                    v.insert(k, x);
                }
                Ok(Strict(Value::Object(v)))
            }
        }
        d.deserialize_any(V)
    }
}
fn strict_json(raw: &[u8]) -> Result<Value> {
    let Strict(v) = serde_json::from_slice::<Strict>(raw)
        .map_err(|_| ApiError::new(422, "私有处理器 JSON 格式无效"))?;
    if !v.is_object() {
        return Err(ApiError::new(422, "私有处理器 JSON 必须是对象"));
    }
    Ok(v)
}
fn validate_update(u: &Value, s: &Value) -> Result<()> {
    let bad = || ApiError::new(422, "私有处理器更新字段无效");
    let o = u.as_object().ok_or_else(bad)?;
    if o.keys().any(|k| {
        ![
            "state",
            "progress",
            "completed_steps",
            "message",
            "content",
            "output",
            "retryable",
            "attempt",
            "flow_epoch",
            "action_id",
        ]
        .contains(&k.as_str())
    }) || !["processing", "succeeded", "failed"].contains(&text(u, "state"))
        || u["attempt"].as_i64().is_none_or(|n| n < 1)
        || u.get("progress")
            .is_some_and(|v| v.as_i64().is_none_or(|n| !(0..=100).contains(&n)))
        || u.get("message")
            .is_some_and(|v| v.as_str().is_none_or(|t| t.chars().count() > 1000))
        || u.get("content")
            .filter(|v| !v.is_null())
            .is_some_and(|v| v.as_str().is_none_or(|t| t.chars().count() > 100_000))
        || u.get("output").filter(|v| !v.is_null()).is_some_and(|v| {
            v.as_object()
                .is_none_or(|m| m.values().any(|v| !v.is_string()))
        })
        || u.get("retryable").is_some_and(|v| !v.is_boolean())
        || u.get("completed_steps")
            .filter(|v| !v.is_null())
            .is_some_and(|v| {
                v.as_array().is_none_or(|a| {
                    a.len() > 30 || a.iter().any(|v| v.as_str().is_none_or(|s| !valid_id(s)))
                })
            })
        || u.get("flow_epoch")
            .filter(|v| !v.is_null())
            .is_some_and(|v| v.as_i64().is_none_or(|n| n < 1))
        || u.get("action_id")
            .filter(|v| !v.is_null())
            .is_some_and(|v| v.as_str().is_none_or(|t| t.chars().count() > 100))
    {
        return Err(bad());
    }
    if u["attempt"] != s["attempt"]
        || ["flow_epoch", "action_id"].iter().any(|k| {
            u.get(*k)
                .filter(|v| !v.is_null())
                .is_some_and(|v| *v != s[*k])
        })
    {
        return Err(ApiError::new(409, "处理步骤或尝试与签名不一致"));
    }
    Ok(())
}

pub fn is_upload_path(path: &str) -> bool {
    let p = path.split('/').collect::<Vec<_>>();
    p.len() == 10
        && p[1..4] == ["api", "callbacks", "v2"]
        && valid_id(p[4])
        && valid_id(p[5])
        && p[6] == "files"
        && valid_field(p[7])
        && valid_id(p[8])
        && p[9] == "upload"
}
pub fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    if !ctx.path.starts_with("/api/callbacks/v2/") {
        return Ok(None);
    }
    let path = ctx.path.clone();
    let p = path.split('/').collect::<Vec<_>>();
    if p.len() < 7 || !valid_id(p[4]) || !valid_id(p[5]) {
        return Err(ApiError::new(404, "接口不存在"));
    }
    let s = request_scope(ctx, p[4], p[5])?;
    if p.len() == 7 && p[6] == "result" && ctx.method == axum::http::Method::POST {
        auth::throttle(ctx, "private-worker-result", 120, 60.0, None)?;
        if ctx.raw_body.len() > MAX_RESULT {
            return Err(ApiError::new(413, "私有处理器结果过大"));
        }
        let body = strict_json(&ctx.raw_body)?;
        let o = body.as_object().unwrap();
        if o.len() != 3
            || !["version", "result_id", "update"]
                .iter()
                .all(|k| o.contains_key(*k))
            || body["version"].as_i64() != Some(2)
            || !valid_id(text(&body, "result_id"))
        {
            return Err(ApiError::new(422, "私有处理器结果字段无效"));
        }
        validate_update(&body["update"], &s)?;
        let bd = hash(&ctx.raw_body);
        let (j, _) = verify(ctx, &s, &bd)?;
        let rid = text(&body, "result_id");
        if let Some(r) = receipt(ctx, &s, rid, &bd, "result")? {
            return Ok(Some(Reply::json(r)));
        }
        if flow::expire_current(ctx, &j, &s)? {
            return Ok(Some(expired_reply()));
        }
        if row(
            &ctx.conn,
            "SELECT phase FROM task_flow_runs WHERE job_id=?",
            [p[5]],
        )?
        .is_some_and(|r| text(&r, "phase") == "queued")
            && active(ctx, &j, &s, false)?.is_none()
        {
            return Ok(Some(expired_reply()));
        }
        let mut update = body["update"].clone();
        update["flow_epoch"] = s["flow_epoch"].clone();
        update["action_id"] = s["action_id"].clone();
        super::apply_update(ctx, p[5], &update)?;
        let r = json!({"ok":true,"version":2,"result_id":rid,"body_digest":bd,"scope":s});
        save_receipt(ctx, &s, rid, &bd, "result", &r)?;
        return Ok(Some(Reply::json(r)));
    }
    if p.len() == 10 && p[6] == "files" && valid_field(p[7]) && valid_id(p[8]) {
        let field = p[7];
        let fid = p[8];
        if p[9] == "upload" && ctx.method == axum::http::Method::POST {
            auth::throttle(ctx, "private-worker-upload", 20, 60.0, None)?;
            if ctx.raw_body.len() > MAX_FILE {
                return Err(ApiError::new(413, "文件超过 20 MiB 限制"));
            }
            if ctx.headers.contains_key("content-length")
                && ctx.header("content-length").parse::<usize>().ok() != Some(ctx.raw_body.len())
            {
                return Err(ApiError::new(400, "文件长度无效"));
            }
            let bd = hash(&ctx.raw_body);
            let (j, _) = verify(ctx, &s, &bd)?;
            let kind = format!("file:{field}");
            if let Some(r) = receipt(ctx, &s, fid, &bd, &kind)? {
                return Ok(Some(Reply::json(r)));
            }
            let Some(execution) = active(ctx, &j, &s, true)? else {
                return Ok(Some(expired_reply()));
            };
            let name = if ctx.header("x-extore-filename").is_empty() {
                "attachment"
            } else {
                ctx.header("x-extore-filename")
            };
            let mime = if ctx.header("content-type").is_empty() {
                "application/octet-stream"
            } else {
                ctx.header("content-type")
            };
            let file =
                files::store_worker_output(ctx, &j, &execution, field, name, mime, &ctx.raw_body)?;
            let r = json!({"ok":true,"version":2,"result_id":fid,"body_digest":bd,"scope":s,"file":file});
            save_receipt(ctx, &s, fid, &bd, &kind, &r)?;
            return Ok(Some(Reply::json(r)));
        }
        if p[9] == "download" && ctx.method == axum::http::Method::GET {
            auth::throttle(ctx, "private-worker-download", 60, 60.0, None)?;
            if !ctx.raw_body.is_empty()
                || ctx.headers.contains_key("transfer-encoding")
                || (ctx.headers.contains_key("content-length")
                    && ctx.header("content-length") != "0")
            {
                return Err(ApiError::bad("文件下载 GET 不能包含请求体"));
            }
            let (j, _) = verify(ctx, &s, &hash(b""))?;
            let Some(execution) = active(ctx, &j, &s, false)? else {
                return Ok(Some(expired_reply()));
            };
            return Ok(Some(files::download_worker_input(
                ctx, &j, &execution, field, fid,
            )?));
        }
    }
    Err(ApiError::new(404, "接口不存在"))
}

pub fn deliver_v2(app: &App, execution: &Value, recheck: impl Fn() -> Result<()>) -> Result<()> {
    let s = scope(execution)?;
    let u = url::Url::parse(text(execution, "webhook_url"))
        .map_err(|_| ApiError::bad("私有处理器地址无效"))?;
    if u.scheme() != "https"
        || u.host_str().is_none()
        || !u.username().is_empty()
        || u.password().is_some()
        || u.query().is_some()
        || u.fragment().is_some()
    {
        return Err(ApiError::bad(
            "私有处理器地址必须是 HTTPS 公网地址且不能含查询参数",
        ));
    }
    let host = u.host_str().unwrap();
    let addresses = (host, u.port_or_known_default().unwrap_or(443))
        .to_socket_addrs()?
        .collect::<Vec<_>>();
    if addresses.is_empty() || addresses.iter().any(|a| !catalog::public_ip(a.ip())) {
        return Err(ApiError::bad("私有处理器地址必须解析到公网 IP"));
    }
    if execution["deadline"].as_f64().is_none_or(|d| d <= now()) {
        return Err(ApiError::new(409, "私有处理器步骤已过期"));
    }
    recheck()?;
    let mut payload = json!({"version":2,"type":"flow.process.requested","scope":s,"params":execution["params"],"parameters":execution["parameters"],"outputs":execution["outputs"],"deadline":execution["deadline"],"callback_url":format!("{}/api/callbacks/v2/{}/{}/result",app.origin,text(&s,"product_id"),text(&s,"job_id"))});
    for k in [
        "instructions",
        "card_attributes",
        "entitlements",
        "revision",
        "last_delivery",
    ] {
        if let Some(v) = execution.get(k) {
            if k != "instructions" || !v.is_null() {
                payload[k] = v.clone();
            }
        }
    }
    let raw = canonical(&payload)?;
    if raw.len() > MAX_RESULT {
        return Err(ApiError::new(413, "私有处理器任务输入过大"));
    }
    let audience = u.origin().ascii_serialization();
    let timestamp = (now() as i64).to_string();
    let nonce = token();
    let bd = hash(&raw);
    let sig = signature(
        text(execution, "webhook_secret"),
        &s,
        OUTBOUND,
        &audience,
        "POST",
        u.path(),
        &timestamp,
        &nonce,
        &bd,
    )?;
    let client = reqwest::blocking::Client::builder()
        .no_proxy()
        .https_only(true)
        .redirect(reqwest::redirect::Policy::none())
        .timeout(Duration::from_secs(15))
        .resolve(host, addresses[0])
        .build()
        .map_err(|_| ApiError::new(502, "私有处理器 TLS 配置失败"))?;
    let mut request = client
        .post(u)
        .header("content-type", "application/json")
        .header("x-extore-version", "2")
        .header("x-extore-direction", OUTBOUND)
        .header("x-extore-audience", audience)
        .header("x-extore-timestamp", timestamp)
        .header("x-extore-nonce", nonce)
        .header("x-extore-body-sha256", bd)
        .header("x-extore-signature", sig);
    for k in FIELDS {
        request = request.header(
            format!("x-extore-{}", k.replace('_', "-")),
            if matches!(k, "attempt" | "flow_epoch") {
                s[k].to_string()
            } else {
                text(&s, k).to_owned()
            },
        );
    }
    recheck()?;
    let response = request
        .body(raw)
        .send()
        .map_err(|_| ApiError::new(502, "私有处理器连接失败"))?;
    if !response.status().is_success() {
        return Err(ApiError::new(
            502,
            format!("HTTP {}", response.status().as_u16()),
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn strict_objects_reject_duplicates_recursively() {
        assert!(strict_json(br#"{"a":1,"a":2}"#).is_err());
        assert!(strict_json(br#"{"a":[{"x":1,"x":2}]}"#).is_err());
        assert!(strict_json(br#"{"a":true,"b":[1,null,"ok"]}"#).is_ok());
    }
    #[test]
    fn upload_path_is_exact() {
        assert!(is_upload_path(
            "/api/callbacks/v2/p/j/files/output/r1/upload"
        ));
        assert!(!is_upload_path(
            "/api/callbacks/v2/p/j/files/Output/r1/upload"
        ));
        assert!(!is_upload_path(
            "/api/callbacks/v2/p/j/files/output/r1/upload/"
        ));
    }
    #[test]
    fn directions_and_action_are_bound() {
        let s = json!({"shop_id":"default","product_id":"p","job_id":"j","attempt":1,"node_id":"n","flow_epoch":2,"action_id":"a"});
        let bd = hash(b"{}");
        let a = signature(
            "secret",
            &s,
            INBOUND,
            "https://extore.example",
            "POST",
            "/result",
            "1700000000",
            "abcdefghijklmnop",
            &bd,
        )
        .unwrap();
        let b = signature(
            "secret",
            &s,
            OUTBOUND,
            "https://extore.example",
            "POST",
            "/result",
            "1700000000",
            "abcdefghijklmnop",
            &bd,
        )
        .unwrap();
        assert_ne!(a, b);
        let mut changed = s.clone();
        changed["action_id"] = json!("b");
        assert_ne!(
            a,
            signature(
                "secret",
                &changed,
                INBOUND,
                "https://extore.example",
                "POST",
                "/result",
                "1700000000",
                "abcdefghijklmnop",
                &bd
            )
            .unwrap()
        );
    }
}

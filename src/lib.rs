mod api;
mod auth;
mod board;
mod catalog;
mod commerce;
mod common;
mod files;
mod fulfillment;
mod maintenance;
mod pipeline;
#[cfg(feature = "plugin")]
pub mod plugin_runtime;
mod worker;

use axum::{
    Router,
    body::{Body, to_bytes},
    extract::{ConnectInfo, State},
    http::{HeaderMap, HeaderValue, Method, Request, StatusCode},
    response::{IntoResponse, Response},
    routing::any,
};
use common::*;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    net::SocketAddr,
    path::{Path, PathBuf},
    sync::{Arc, OnceLock},
};

fn response(reply: Reply) -> Response {
    let mut r = (
        StatusCode::from_u16(reply.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        reply.body,
    )
        .into_response();
    *r.headers_mut() = reply.headers;
    for (k, v) in [
        ("cache-control", "no-store"),
        ("referrer-policy", "no-referrer"),
        ("x-content-type-options", "nosniff"),
        ("x-frame-options", "DENY"),
        (
            "content-security-policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        ),
    ] {
        if !r.headers().contains_key(k) {
            r.headers_mut().insert(k, HeaderValue::from_static(v));
        }
    }
    r.headers_mut()
        .insert("x-extore-runtime", HeaderValue::from_static("rust"));
    r
}
fn failure(error: ApiError) -> Response {
    let mut reply = Reply::json(json!({"detail":error.message}));
    reply.status = error.status;
    response(reply)
}

async fn request(
    State(app): State<Arc<App>>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    request: Request<Body>,
) -> Response {
    let (parts, body) = request.into_parts();
    static UPLOADS: OnceLock<Arc<tokio::sync::Semaphore>> = OnceLock::new();
    static REQUESTS: OnceLock<Arc<tokio::sync::Semaphore>> = OnceLock::new();
    let multipart = parts
        .headers
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .is_some_and(|s| s.starts_with("multipart/form-data"));
    let upload = multipart || fulfillment::private_worker::is_upload_path(parts.uri.path());
    let pool = if upload {
        UPLOADS.get_or_init(|| Arc::new(tokio::sync::Semaphore::new(4)))
    } else {
        REQUESTS.get_or_init(|| Arc::new(tokio::sync::Semaphore::new(32)))
    };
    let permit = match pool.clone().try_acquire_owned() {
        Ok(p) => p,
        Err(_) => return failure(ApiError::new(429, "服务繁忙，请稍后再试")),
    };
    let max = if upload {
        files::max_multipart_bytes()
    } else {
        5 * 1024 * 1024
    };
    let raw = match tokio::time::timeout(
        std::time::Duration::from_secs(if upload { 300 } else { 30 }),
        to_bytes(body, max),
    )
    .await
    {
        Ok(Ok(v)) => v.to_vec(),
        Ok(Err(_)) => return failure(ApiError::new(413, "请求过大")),
        Err(_) => return failure(ApiError::new(408, "请求接收超时")),
    };
    let result = tokio::task::spawn_blocking(move || -> Result<Reply> {
        let _permit = permit;
        let query = url::form_urlencoded::parse(parts.uri.query().unwrap_or("").as_bytes())
            .into_owned()
            .collect::<BTreeMap<_, _>>();
        let content_type = parts
            .headers
            .get("content-type")
            .and_then(|h| h.to_str().ok())
            .unwrap_or("");
        let body = if raw.is_empty() || upload || content_type.starts_with("multipart/") {
            Value::Null
        } else if content_type.starts_with("application/x-www-form-urlencoded") {
            json!(
                url::form_urlencoded::parse(&raw)
                    .into_owned()
                    .collect::<BTreeMap<_, _>>()
            )
        } else {
            if parts.uri.path() == "/api/batch/exchange" {
                fulfillment::parse_exchange_body(&raw)?
            } else {
                serde_json::from_slice(&raw).map_err(|e| {
                    if parts.uri.path().starts_with("/api/callbacks/v2/") {
                        ApiError::new(422, "回调必须使用有效 JSON")
                    } else {
                        ApiError::from(e)
                    }
                })?
            }
        };
        let remote_ip = if peer.ip().is_loopback() {
            parts
                .headers
                .get("x-forwarded-for")
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.rsplit(',').next())
                .and_then(|v| v.trim().parse::<std::net::IpAddr>().ok())
                .unwrap_or(peer.ip())
        } else {
            peer.ip()
        };
        let conn = app.db()?;
        let mut ctx = Context {
            app,
            conn,
            method: parts.method,
            path: parts.uri.path().to_owned(),
            raw_target: parts
                .uri
                .path_and_query()
                .map(|v| v.as_str())
                .unwrap_or("/")
                .to_owned(),
            query,
            headers: parts.headers,
            body,
            raw_body: raw,
            peer: remote_ip.to_string(),
            response_headers: HeaderMap::new(),
        };
        let safe = matches!(ctx.method, Method::GET | Method::HEAD | Method::OPTIONS);
        let external = ctx.path.starts_with("/api/integrations/")
            || ctx.path.starts_with("/api/callbacks/")
            || ctx.path.starts_with("/api/cli/")
            || ctx.path == "/api/manage/next";
        if !safe && !external && ctx.header("authorization").is_empty() {
            if ctx.header("origin") != ctx.app.origin {
                return Err(ApiError::new(403, "请求来源无效"));
            }
            let legacy_batch_json = ctx.header("content-type").is_empty()
                && matches!(
                    ctx.path.as_str(),
                    "/api/batch/exchange" | "/api/batch/receipt"
                );
            if !ctx.raw_body.is_empty()
                && !legacy_batch_json
                && !ctx.header("content-type").starts_with("application/json")
                && !ctx
                    .header("content-type")
                    .starts_with("multipart/form-data")
            {
                return Err(ApiError::new(415, "请使用 JSON 请求"));
            }
        }
        dispatch_transaction(&mut ctx)
    })
    .await;
    match result {
        Ok(Ok(reply)) => response(reply),
        Ok(Err(error)) => failure(error),
        Err(e) => {
            tracing::error!(error=%e, "request task failed");
            failure(ApiError::new(500, "服务暂时不可用"))
        }
    }
}

fn dispatch_transaction(ctx: &mut Context) -> Result<Reply> {
    fulfillment::admit(ctx)?;
    ctx.conn.execute_batch("BEGIN IMMEDIATE")?;
    let outcome = dispatch(ctx);
    match outcome {
        Ok(mut r) => {
            ctx.conn.execute_batch("COMMIT")?;
            r.headers.extend(std::mem::take(&mut ctx.response_headers));
            if ctx.app.origin.starts_with("https:") {
                r.headers.insert(
                    "strict-transport-security",
                    HeaderValue::from_static("max-age=31536000"),
                );
            }
            if ctx.method == Method::HEAD {
                r.body.clear();
            }
            Ok(r)
        }
        Err(e) => {
            let _ = ctx.conn.execute_batch("ROLLBACK");
            Err(e)
        }
    }
}

fn dispatch(ctx: &mut Context) -> Result<Reply> {
    auth::cli::verify_action(ctx)?;
    if (ctx.method == Method::GET || ctx.method == Method::HEAD) && ctx.path == "/health" {
        return Ok(Reply::json(
            json!({"status":"ok","runtime":"rust","version":env!("CARGO_PKG_VERSION")}),
        ));
    }
    for handler in [
        auth::handle,
        api::handle,
        board::handle,
        pipeline::handle,
        maintenance::handle,
        commerce::handle,
        catalog::handle,
        fulfillment::handle,
        files::handle,
    ] {
        if let Some(reply) = handler(ctx)? {
            return Ok(reply);
        }
    }
    if ctx.method != Method::GET && ctx.method != Method::HEAD {
        return Err(ApiError::new(404, "接口不存在"));
    }
    if let Some(name) = ctx.path.strip_prefix("/static/") {
        return asset(&ctx.app.static_dir, name);
    }
    if ctx.path == "/AGENTS.md" {
        return asset(&ctx.app.static_dir, "AGENTS.md");
    }
    if matches!(
        ctx.path.as_str(),
        "/" | "/admin"
            | "/staff"
            | "/receipt"
            | "/cli/owner"
            | "/cli/device"
            | "/account"
            | "/account/login"
            | "/account/invite"
            | "/account/register"
            | "/account/reset"
            | "/account/security"
            | "/connect/authorize"
            | "/proxy"
    ) {
        return asset(&ctx.app.static_dir, "index.html");
    }
    Err(ApiError::new(404, "页面不存在"))
}
fn asset(root: &Path, name: &str) -> Result<Reply> {
    if name
        .split('/')
        .any(|s| matches!(s, ".." | ".") || s.contains('\\'))
    {
        return Err(ApiError::new(404, "文件不存在"));
    }
    let root = root.canonicalize()?;
    let file = root
        .join(name)
        .canonicalize()
        .map_err(|_| ApiError::new(404, "文件不存在"))?;
    if !file.starts_with(root) || !file.is_file() {
        return Err(ApiError::new(404, "文件不存在"));
    }
    Reply::bytes(
        mime_guess::from_path(&file)
            .first_or_octet_stream()
            .as_ref(),
        std::fs::read(file)?,
    )
}

#[tokio::main]
pub async fn run() -> std::result::Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "extore=info".into()),
        )
        .init();
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    let command = args.first().map(String::as_str).unwrap_or("serve");
    if args
        .iter()
        .skip(1)
        .any(|a| matches!(a.as_str(), "--help" | "-h"))
    {
        println!(
            "Extore {}\nCommands: init, bootstrap, serve, worker, reset-auth, integration-key, version\nConfigure the server with EXTORE_DATA, EXTORE_ORIGIN, EXTORE_STATIC and EXTORE_LISTEN.",
            env!("CARGO_PKG_VERSION")
        );
        return Ok(());
    }
    if args.len() > 1 {
        return Err(
            "Unexpected arguments; use environment variables to configure the native server".into(),
        );
    }
    if matches!(command, "--version" | "-V" | "version") {
        println!("extore {} (Rust)", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    let app = Arc::new(App {
        data: PathBuf::from(std::env::var("EXTORE_DATA").unwrap_or_else(|_| "data".into())),
        origin: std::env::var("EXTORE_ORIGIN")
            .unwrap_or_else(|_| "http://localhost:8000".into())
            .trim_end_matches('/')
            .to_owned(),
        static_dir: PathBuf::from(
            std::env::var("EXTORE_STATIC").unwrap_or_else(|_| "extore/static".into()),
        ),
    });
    let url = url::Url::parse(&app.origin)?;
    if !matches!(url.scheme(), "http" | "https")
        || url.host_str().is_none()
        || url.path() != "/"
        || url.query().is_some()
        || url.fragment().is_some()
        || !url.username().is_empty()
        || (url.scheme() != "https" && !matches!(url.host_str(), Some("localhost" | "127.0.0.1")))
    {
        return Err("EXTORE_ORIGIN must be HTTPS (or localhost for development)".into());
    }
    if matches!(command, "init" | "bootstrap") {
        catalog::initialize(&app)?;
        return Ok(());
    }
    if command == "worker" {
        catalog::validate_state(&app)?;
        return worker::run(app).await.map_err(Into::into);
    }
    if command == "integration-key" {
        catalog::validate_state(&app)?;
        let c = app.db()?;
        let key = token();
        c.execute_batch("BEGIN IMMEDIATE")?;
        set_setting(&c, "integration_key", &digest(&key))?;
        audit(&c, "owner", "integration_key.rotate", "platform", None)?;
        c.execute_batch("COMMIT")?;
        println!("{key}");
        return Ok(());
    }
    if command == "reset-auth" {
        auth::reset_auth(&app)?;
        return Ok(());
    }
    if matches!(command, "--help" | "-h" | "help") {
        println!(
            "Extore {}\n\nCommands: init, bootstrap, serve, worker, reset-auth, integration-key, version\n\nEnvironment: EXTORE_DATA, EXTORE_ORIGIN, EXTORE_STATIC, EXTORE_LISTEN",
            env!("CARGO_PKG_VERSION")
        );
        return Ok(());
    }
    if command != "serve" {
        return Err(format!("Unknown command: {command}").into());
    }
    if !app.data.join("extore.sqlite3").exists() {
        return Err("Database missing; run extore init before serving".into());
    }
    catalog::validate_state(&app)?;
    let listen = std::env::var("EXTORE_LISTEN").unwrap_or_else(|_| "127.0.0.1:8000".into());
    let listener = tokio::net::TcpListener::bind(&listen).await?;
    tracing::info!(%listen, "Extore Rust listening");
    axum::serve(
        listener,
        Router::new()
            .fallback(any(request))
            .with_state(app)
            .into_make_service_with_connect_info::<SocketAddr>(),
    )
    .with_graceful_shutdown(shutdown())
    .await?;
    Ok(())
}

pub async fn shutdown() {
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        .expect("register SIGTERM");
    tokio::select! { _=tokio::signal::ctrl_c()=>{},_=terminate.recv()=>{} }
}

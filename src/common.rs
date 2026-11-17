use axum::http::{HeaderMap, HeaderValue, Method};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use rand::RngCore;
use rusqlite::{Connection, Params, types::ValueRef};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, path::PathBuf, sync::Arc, time::{Duration, SystemTime, UNIX_EPOCH}};

pub type Result<T> = std::result::Result<T, ApiError>;

#[derive(Debug)]
pub struct ApiError { pub status: u16, pub message: String }
impl ApiError {
    pub fn new(status: u16, message: impl Into<String>) -> Self { Self { status, message: message.into() } }
    pub fn bad(message: impl Into<String>) -> Self { Self::new(400, message) }
}
impl std::fmt::Display for ApiError { fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result { write!(f, "{}", self.message) } }
impl std::error::Error for ApiError {}
impl From<rusqlite::Error> for ApiError { fn from(e: rusqlite::Error) -> Self { tracing::error!(error=%e, "database operation failed"); Self::new(500, "数据库操作失败") } }
impl From<serde_json::Error> for ApiError { fn from(_: serde_json::Error) -> Self { Self::bad("JSON 格式无效") } }
impl From<std::io::Error> for ApiError { fn from(e: std::io::Error) -> Self { tracing::error!(error=%e, "file operation failed"); Self::new(500, "文件操作失败") } }

#[derive(Clone)]
pub struct App { pub data: PathBuf, pub origin: String, pub static_dir: PathBuf }
impl App {
    pub fn db(&self) -> Result<Connection> {
        let c = Connection::open(self.data.join("extore.sqlite3"))?;
        c.busy_timeout(Duration::from_secs(20))?;
        c.execute_batch("PRAGMA foreign_keys=ON; PRAGMA secure_delete=ON;")?;
        Ok(c)
    }
}

pub struct Context {
    pub app: Arc<App>, pub conn: Connection, pub method: Method, pub path: String,
    pub query: BTreeMap<String, String>, pub headers: HeaderMap, pub body: Value,
    pub raw_body: Vec<u8>, pub raw_target: String, pub peer: String, pub response_headers: HeaderMap,
}
impl Context {
    pub fn background(app: Arc<App>) -> Result<Self> { let conn=app.db()?; Ok(Self { app,conn,method:Method::GET,path:String::new(),raw_target:String::new(),query:BTreeMap::new(),headers:HeaderMap::new(),body:Value::Null,raw_body:Vec::new(),peer:"worker".to_owned(),response_headers:HeaderMap::new() }) }
    pub fn is(&self, method: &str, path: &str) -> bool { self.method.as_str() == method && self.path == path }
    pub fn q(&self, key: &str) -> &str { self.query.get(key).map(String::as_str).unwrap_or("") }
    pub fn header(&self, key: &str) -> &str { self.headers.get(key).and_then(|h| h.to_str().ok()).unwrap_or("") }
    pub fn cookie(&self, name: &str) -> &str {
        self.header("cookie").split(';').find_map(|p| p.trim().split_once('=').filter(|(k, _)| *k == name).map(|(_, v)| v)).unwrap_or("")
    }
    pub fn set_cookie(&mut self, value: &str) -> Result<()> {
        self.response_headers.append("set-cookie", HeaderValue::from_str(value).map_err(|_| ApiError::new(500, "Invalid cookie"))?); Ok(())
    }
    pub fn b(&self, key: &str) -> &str { text(&self.body, key) }
}

pub struct Reply { pub status: u16, pub headers: HeaderMap, pub body: Vec<u8> }
impl Reply {
    pub fn json(value: Value) -> Self {
        let mut headers = HeaderMap::new(); headers.insert("content-type", HeaderValue::from_static("application/json; charset=utf-8"));
        Self { status: 200, headers, body: serde_json::to_vec(&value).expect("JSON value is serializable") }
    }
    pub fn redirect(url: &str) -> Result<Self> {
        let mut headers = HeaderMap::new(); headers.insert("location", HeaderValue::from_str(url).map_err(|_| ApiError::bad("跳转地址无效"))?);
        Ok(Self { status: 303, headers, body: Vec::new() })
    }
    pub fn bytes(content_type: &str, body: Vec<u8>) -> Result<Self> {
        let mut headers = HeaderMap::new(); headers.insert("content-type", HeaderValue::from_str(content_type).map_err(|_| ApiError::bad("文件类型无效"))?);
        Ok(Self { status: 200, headers, body })
    }
}

pub fn rows<P: Params>(c: &Connection, sql: &str, params: P) -> Result<Vec<Value>> {
    let mut statement = c.prepare(sql)?;
    let names = statement.column_names().into_iter().map(String::from).collect::<Vec<_>>();
    let mut cursor = statement.query(params)?; let mut values = Vec::new();
    while let Some(row) = cursor.next()? {
        let mut object = serde_json::Map::new();
        for (i, name) in names.iter().enumerate() {
            let value = match row.get_ref(i)? {
                ValueRef::Null => Value::Null, ValueRef::Integer(v) => json!(v), ValueRef::Real(v) => json!(v),
                ValueRef::Text(v) => json!(String::from_utf8_lossy(v)), ValueRef::Blob(v) => json!(URL_SAFE_NO_PAD.encode(v)),
            }; object.insert(name.clone(), value);
        }
        values.push(Value::Object(object));
    }
    Ok(values)
}
pub fn row<P: Params>(c: &Connection, sql: &str, params: P) -> Result<Option<Value>> { Ok(rows(c, sql, params)?.into_iter().next()) }
pub fn required<P: Params>(c: &Connection, sql: &str, params: P, message: &str) -> Result<Value> { row(c, sql, params)?.ok_or_else(|| ApiError::new(404, message)) }
pub fn setting(c: &Connection, key: &str, default: &str) -> Result<String> { Ok(row(c, "SELECT value FROM settings WHERE key=?", [key])?.map(|v| text(&v, "value").to_owned()).unwrap_or_else(|| default.to_owned())) }
pub fn set_setting(c: &Connection, key: &str, value: &str) -> Result<()> { c.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", [key, value])?; Ok(()) }
pub fn audit(c: &Connection, actor: &str, action: &str, target: &str, shop_id: Option<&str>) -> Result<()> {
    c.execute("INSERT INTO audit(actor,action,target,created,shop_id) VALUES (?,?,?,?,?)", rusqlite::params![actor, action, target, now(), shop_id])?; Ok(())
}
pub fn now() -> f64 { SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs_f64() }
pub fn id() -> String { uuid::Uuid::new_v4().to_string() }
pub fn token() -> String { let mut bytes = [0_u8; 32]; rand::rngs::OsRng.fill_bytes(&mut bytes); URL_SAFE_NO_PAD.encode(bytes) }
pub fn digest(value: &str) -> String { format!("{:x}", Sha256::digest(value.as_bytes())) }
pub fn text<'a>(value: &'a Value, key: &str) -> &'a str { value.get(key).and_then(Value::as_str).unwrap_or("") }
pub fn number(value: &Value, key: &str) -> f64 { value.get(key).and_then(Value::as_f64).unwrap_or(0.0) }
pub fn integer(value: &Value, key: &str, default: i64) -> i64 { value.get(key).and_then(Value::as_i64).unwrap_or(default) }
pub fn flag(value: &Value, key: &str) -> bool { value.get(key).is_some_and(|v| v == &Value::Bool(true) || v.as_i64().is_some_and(|i| i != 0)) }
pub fn parse(value: &str) -> Result<Value> { Ok(serde_json::from_str(value)?) }
pub fn json_field(value: &Value, key: &str, default: Value) -> Value { value.get(key).map(|v| if let Some(s) = v.as_str() { serde_json::from_str(s).unwrap_or(default.clone()) } else { v.clone() }).unwrap_or(default) }
pub fn actor(session: &Value) -> String { if text(session, "role") == "staff" { text(session, "staff_id").to_owned() } else if !text(session, "shop_id").is_empty() { format!("shop:{}", text(session, "shop_id")) } else { "owner".to_owned() } }
pub fn encrypt(app: &App, key: &str, value: &[u8]) -> Result<String> {
    let secret = std::fs::read_to_string(app.data.join(key))?;
    fernet::Fernet::new(secret.trim()).ok_or_else(|| ApiError::new(500, "加密密钥无效")).map(|f| f.encrypt(value))
}
pub fn decrypt(app: &App, key: &str, value: &str) -> Result<Vec<u8>> {
    let secret = std::fs::read_to_string(app.data.join(key))?;
    fernet::Fernet::new(secret.trim()).ok_or_else(|| ApiError::new(500, "加密密钥无效"))?.decrypt(value).map_err(|_| ApiError::new(500, "加密数据校验失败"))
}

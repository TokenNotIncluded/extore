use crate::common::*;
use aes_gcm::{
    Aes256Gcm, KeyInit, Nonce,
    aead::{Aead, Payload},
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use hkdf::Hkdf;
use rand::RngCore;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;

fn ascii_json(value: &Value) -> Result<Vec<u8>> {
    let source = serde_json::to_string(value)?;
    let mut encoded = String::new();
    for ch in source.chars() {
        if ch.is_ascii() {
            encoded.push(ch);
        } else {
            let mut units = [0; 2];
            for unit in ch.encode_utf16(&mut units) {
                use std::fmt::Write;
                write!(encoded, "\\u{unit:04x}").expect("writing String");
            }
        }
    }
    Ok(encoded.into_bytes())
}
fn context(
    app: &App,
    tenant: Option<&str>,
    kind: &str,
    resource: &str,
) -> Result<(Aes256Gcm, Vec<u8>)> {
    for value in [Some(kind), Some(resource), tenant].into_iter().flatten() {
        if value.is_empty() || value.chars().count() > 200 || value.chars().any(|c| c < ' ') {
            return Err(ApiError::bad("加密资源范围无效"));
        }
    }
    let path = app.data.join("master-secrets.key");
    let key = std::fs::read(&path)?;
    if key.len() != 32 {
        return Err(ApiError::new(500, "加密密钥无效"));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if std::fs::metadata(path)?.permissions().mode() & 0o777 != 0o600 {
            return Err(ApiError::new(500, "加密密钥权限无效"));
        }
    }
    let mut material = b"extore-secret-key-v1\0".to_vec();
    material.extend_from_slice(&key);
    let fingerprint = format!("{:x}", Sha256::digest(material));
    let c = app.db()?;
    if let Some(existing) = row(
        &c,
        "SELECT fingerprint FROM secret_store_state WHERE id=1",
        [],
    )? {
        if !bool::from(
            text(&existing, "fingerprint")
                .as_bytes()
                .ct_eq(fingerprint.as_bytes()),
        ) {
            return Err(ApiError::new(500, "加密密钥与数据库不匹配"));
        }
    }
    let hk = Hkdf::<Sha256>::new(Some(b"extore-tenant-secret-v1"), &key);
    let mut derived = [0u8; 32];
    hk.expand(&ascii_json(&json!(tenant))?, &mut derived)
        .map_err(|_| ApiError::new(500, "加密密钥派生失败"))?;
    let cipher =
        Aes256Gcm::new_from_slice(&derived).map_err(|_| ApiError::new(500, "加密密钥无效"))?;
    Ok((
        cipher,
        ascii_json(&json!(["extore-secret-v1", tenant, kind, resource]))?,
    ))
}
pub fn store_secret(
    app: &App,
    tenant: Option<&str>,
    kind: &str,
    resource: &str,
    value: &Value,
) -> Result<String> {
    if !value.is_string() && !value.is_object() {
        return Err(ApiError::bad("密钥必须是文本或对象"));
    }
    let bytes = serde_json::to_vec(value)?;
    if bytes.len() > 200_000 {
        return Err(ApiError::bad("密钥超过存储上限"));
    }
    let (cipher, aad) = context(app, tenant, kind, resource)?;
    let mut nonce = [0u8; 12];
    rand::rngs::OsRng.fill_bytes(&mut nonce);
    let encrypted = cipher
        .encrypt(
            Nonce::from_slice(&nonce),
            Payload {
                msg: &bytes,
                aad: &aad,
            },
        )
        .map_err(|_| ApiError::new(500, "加密失败"))?;
    let mut data = nonce.to_vec();
    data.extend(encrypted);
    Ok(format!("v1.{}", URL_SAFE_NO_PAD.encode(data)))
}
pub fn open_secret(
    app: &App,
    tenant: Option<&str>,
    kind: &str,
    resource: &str,
    ciphertext: &str,
) -> Result<Value> {
    let (cipher, aad) = context(app, tenant, kind, resource)?;
    let invalid = || ApiError::new(500, "加密数据校验失败");
    let encoded = ciphertext.strip_prefix("v1.").ok_or_else(invalid)?;
    if encoded.len() > 400_056 {
        return Err(invalid());
    }
    let bytes = URL_SAFE_NO_PAD.decode(encoded).map_err(|_| invalid())?;
    if bytes.len() < 28 || URL_SAFE_NO_PAD.encode(&bytes) != encoded {
        return Err(invalid());
    }
    let plain = cipher
        .decrypt(
            Nonce::from_slice(&bytes[..12]),
            Payload {
                msg: &bytes[12..],
                aad: &aad,
            },
        )
        .map_err(|_| invalid())?;
    let value: Value = serde_json::from_slice(&plain).map_err(|_| invalid())?;
    if !value.is_string() && !value.is_object() {
        return Err(invalid());
    }
    Ok(value)
}

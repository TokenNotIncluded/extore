use super::*;
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use serde::{
    Deserialize, Deserializer,
    de::{self, MapAccess, Visitor},
};
use std::fmt;
use subtle::ConstantTimeEq;

#[derive(Default)]
struct AttributesOrder(Vec<(String, Value)>);
impl<'de> Deserialize<'de> for AttributesOrder {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        struct MapVisitor;
        impl<'de> Visitor<'de> for MapVisitor {
            type Value = AttributesOrder;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                write!(f, "attribute object")
            }
            fn visit_map<M: MapAccess<'de>>(
                self,
                mut map: M,
            ) -> std::result::Result<Self::Value, M::Error> {
                let mut values: Vec<(String, Value)> = Vec::new();
                while let Some((key, value)) = map.next_entry::<String, Value>()? {
                    if let Some((_, old)) = values.iter_mut().find(|(k, _)| k == &key) {
                        *old = value;
                    } else {
                        values.push((key, value));
                    }
                }
                Ok(AttributesOrder(values))
            }
        }
        deserializer.deserialize_map(MapVisitor)
    }
}
impl AttributesOrder {
    fn from_body(bytes: &[u8]) -> Result<Self> {
        struct Outer;
        impl<'de> Visitor<'de> for Outer {
            type Value = AttributesOrder;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                write!(f, "request object")
            }
            fn visit_map<M: MapAccess<'de>>(
                self,
                mut map: M,
            ) -> std::result::Result<Self::Value, M::Error> {
                let mut attrs = AttributesOrder::default();
                while let Some(key) = map.next_key::<String>()? {
                    if key == "attributes" {
                        attrs = map.next_value()?;
                    } else {
                        map.next_value::<de::IgnoredAny>()?;
                    }
                }
                Ok(attrs)
            }
        }
        let mut reader = serde_json::Deserializer::from_slice(bytes);
        reader
            .deserialize_map(Outer)
            .map_err(|_| ApiError::bad("发行请求无效"))
    }
    fn encoded(&self) -> Result<String> {
        let parts = self
            .0
            .iter()
            .map(|(k, v)| {
                Ok(format!(
                    "{}:{}",
                    serde_json::to_string(k)?,
                    serde_json::to_string(v)?
                ))
            })
            .collect::<Result<Vec<_>>>()?;
        Ok(format!("{{{}}}", parts.join(",")))
    }
}
fn normalized(ctx: &Context) -> Result<(Value, String)> {
    let pid = ctx.body["product_id"]
        .as_str()
        .ok_or_else(|| ApiError::bad("请提供商品编号"))?;
    let variant = if let Some(v) = ctx.body.get("variant_id") {
        v.as_str().ok_or_else(|| ApiError::bad("规格编号无效"))?
    } else {
        "default"
    };
    if variant.is_empty()
        || variant.len() > 40
        || !variant.bytes().enumerate().all(|(i, b)| {
            b.is_ascii_lowercase() || b.is_ascii_digit() || (i > 0 && (b == b'_' || b == b'-'))
        })
    {
        return Err(ApiError::bad("规格编号无效"));
    }
    let count = match ctx.body.get("count") {
        None => 1,
        Some(v) => v
            .as_i64()
            .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
            .filter(|v| (1..=1000).contains(v))
            .ok_or_else(|| ApiError::bad("单次发行数量为 1–1000"))?,
    };
    let label = match ctx.body.get("label") {
        None => "",
        Some(v) => v
            .as_str()
            .filter(|v| v.chars().count() <= 100)
            .ok_or_else(|| ApiError::bad("批次名称最多 100 字"))?,
    };
    let expiry = if ctx.body["expires"].is_null() {
        None
    } else {
        Some(
            ctx.body["expires"]
                .as_f64()
                .or_else(|| ctx.body["expires"].as_str().and_then(|s| s.parse().ok()))
                .filter(|v| v.is_finite())
                .ok_or_else(|| ApiError::bad("卡密到期时间无效"))?,
        )
    };
    let routed = if ctx.body["routed"].is_null() {
        None
    } else {
        Some(
            ctx.body["routed"]
                .as_bool()
                .ok_or_else(|| ApiError::bad("路由参数无效"))?,
        )
    };
    let attrs = ctx.body.get("attributes").cloned().unwrap_or(json!({}));
    crate::catalog::validate_attributes(&attrs)?;
    let body = json!({"product_id":pid,"variant_id":variant,"count":count,"label":label,"expires":expiry,"routed":routed,"attributes":attrs});
    // Pydantic's historical fingerprint uses declaration order and preserves flat attribute order.
    let mut parts = vec![format!("\"product_id\":{}", serde_json::to_string(pid)?)];
    if variant != "default" {
        parts.push(format!(
            "\"variant_id\":{}",
            serde_json::to_string(variant)?
        ));
    }
    parts.push(format!("\"count\":{count}"));
    if !label.is_empty() || expiry.is_some() {
        parts.push(format!("\"label\":{}", serde_json::to_string(label)?));
        parts.push(format!("\"expires\":{}", serde_json::to_string(&expiry)?));
    }
    if let Some(routed) = routed {
        parts.push(format!("\"routed\":{routed}"));
    }
    if attrs.as_object().is_some_and(|o| !o.is_empty()) {
        let order = if ctx.raw_body.is_empty() {
            AttributesOrder(
                attrs
                    .as_object()
                    .unwrap()
                    .iter()
                    .map(|(k, v)| (k.clone(), v.clone()))
                    .collect(),
            )
        } else {
            AttributesOrder::from_body(&ctx.raw_body)?
        };
        parts.push(format!("\"attributes\":{}", order.encoded()?));
    }
    let hash = digest(&format!("{{{}}}", parts.join(",")));
    Ok((body, hash))
}
pub fn handle(ctx: &mut Context) -> Result<Option<Reply>> {
    if ctx.path == "/api/shop/integration-key"
        && ["GET", "POST", "DELETE"].contains(&ctx.method.as_str())
    {
        let s = session(ctx, &["admin"], None)?;
        let sid = crate::catalog::selected_shop(ctx, &s, ctx.q("shop_id"))?;
        if ctx.method.as_str() == "GET" {
            let saved = row(
                &ctx.conn,
                "SELECT created FROM shop_integration_keys WHERE shop_id=?",
                [&sid],
            )?;
            return Ok(Some(Reply::json(
                json!({"shop_id":sid,"configured":saved.is_some(),"created":saved.map(|v|v["created"].clone())}),
            )));
        }
        recent(&s)?;
        if ctx.method.as_str() == "POST" {
            let raw = token();
            ctx.conn.execute("INSERT INTO shop_integration_keys(shop_id,key_digest,created) VALUES (?,?,?) ON CONFLICT(shop_id) DO UPDATE SET key_digest=excluded.key_digest,created=excluded.created",params![sid,digest(&raw),now()])?;
            audit(
                &ctx.conn,
                &actor(&s),
                "integration_key.rotate",
                &sid,
                Some(&sid),
            )?;
            return Ok(Some(Reply::json(json!({"shop_id":sid,"key":raw}))));
        }
        ctx.conn
            .execute("DELETE FROM shop_integration_keys WHERE shop_id=?", [&sid])?;
        audit(
            &ctx.conn,
            &actor(&s),
            "integration_key.revoke",
            &sid,
            Some(&sid),
        )?;
        return Ok(Some(Reply::json(json!({"ok":true}))));
    }
    if ctx.is("POST", "/api/integrations/cards") {
        throttle(ctx, "integration", 60, 60.0, None)?;
        let (body, hash) = normalized(ctx)?;
        let auth = ctx.header("authorization");
        let raw = auth.strip_prefix("Bearer ").unwrap_or(auth);
        let secret_digest = digest(raw);
        if raw.is_empty() {
            return Err(ApiError::new(401, "平台密钥无效"));
        }
        let scoped = row(
            &ctx.conn,
            "SELECT shop_id FROM shop_integration_keys WHERE key_digest=?",
            [&secret_digest],
        )?;
        let legacy = scoped.is_none();
        let sid = if let Some(scoped) = scoped {
            text(&scoped, "shop_id").to_owned()
        } else {
            let expected = setting(&ctx.conn, "integration_key", "")?;
            if expected.is_empty()
                || !bool::from(expected.as_bytes().ct_eq(secret_digest.as_bytes()))
            {
                return Err(ApiError::new(401, "平台密钥无效"));
            }
            setting(&ctx.conn, "legacy_shop_id", "")?
        };
        shop(ctx, &sid)?;
        let pid = text(&body, "product_id");
        let p = crate::catalog::require_active(ctx, pid)?;
        if text(&p, "shop_id") != sid {
            return Err(ApiError::new(403, "平台密钥无权发行此店铺的卡密"));
        }
        let key = ctx.header("idempotency-key");
        if !(8..=200).contains(&key.chars().count()) {
            return Err(ApiError::bad("请提供 8–200 字符的 Idempotency-Key"));
        }
        let request_key = if legacy {
            digest(key)
        } else {
            digest(&format!("shop:{sid}:{secret_digest}:{key}"))
        };
        if let Some(previous) = row(
            &ctx.conn,
            "SELECT * FROM api_requests WHERE key=?",
            [&request_key],
        )? {
            if text(&previous, "fingerprint") != hash {
                return Err(ApiError::new(409, "同一个幂等键不能用于不同请求"));
            }
            let stored = text(&previous, "response");
            let ciphertext = if stored.starts_with("gAAAA") {
                stored.to_owned()
            } else {
                String::from_utf8(
                    URL_SAFE_NO_PAD
                        .decode(stored)
                        .map_err(|_| ApiError::new(500, "历史发行记录无效"))?,
                )
                .map_err(|_| ApiError::new(500, "历史发行记录无效"))?
            };
            let plain = decrypt(&ctx.app, "issuance.key", &ciphertext)?;
            return Ok(Some(Reply::json(serde_json::from_slice(&plain)?)));
        }
        let issued = crate::catalog::issue_cards(ctx, pid, &body)?;
        let result = json!({"codes":issued["codes"]});
        let ciphertext = encrypt(&ctx.app, "issuance.key", &serde_json::to_vec(&result)?)?;
        ctx.conn.execute(
            "INSERT INTO api_requests(key,fingerprint,response,created) VALUES (?,?,?,?)",
            params![request_key, hash, ciphertext.as_bytes(), now()],
        )?;
        audit(
            &ctx.conn,
            &if legacy {
                "platform".to_owned()
            } else {
                format!("shop:{sid}")
            },
            "cards.issue",
            &format!("{pid}:{}", integer(&body, "count", 1)),
            Some(&sid),
        )?;
        return Ok(Some(Reply::json(result)));
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn context(body: &str) -> (tempfile::TempDir, Context) {
        let dir = tempfile::tempdir().unwrap();
        let app = std::sync::Arc::new(App {
            data: dir.path().into(),
            origin: "https://example.test".into(),
            static_dir: dir.path().into(),
        });
        let mut ctx = Context::background(app).unwrap();
        ctx.body = serde_json::from_str(body).unwrap();
        ctx.raw_body = body.as_bytes().to_vec();
        (dir, ctx)
    }
    #[test]
    fn pre_sku_idempotency_fingerprint_is_unchanged() {
        let (_dir, ctx) = context(
            r#"{"count":1,"product_id":"x","variant_id":"default","attributes":{},"routed":null,"label":"","expires":null}"#,
        );
        assert_eq!(
            normalized(&ctx).unwrap().1,
            digest(r#"{"product_id":"x","count":1}"#)
        );
    }
    #[test]
    fn attribute_order_matches_legacy_pydantic_serialization() {
        let (_dir, ctx) =
            context(r#"{"product_id":"x","count":2,"attributes":{"z":"中文","a":2}}"#);
        assert_eq!(
            normalized(&ctx).unwrap().1,
            digest(r#"{"product_id":"x","count":2,"attributes":{"z":"中文","a":2}}"#)
        );
    }
}

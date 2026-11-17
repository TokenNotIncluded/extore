use super::*;

fn fail() -> ApiError {
    ApiError::bad("商品字段格式无效")
}
fn bounded(v: &Value, max: usize) -> bool {
    v.as_str().is_some_and(|s| s.chars().count() <= max)
}
fn localized(v: &Value, max: usize, required: bool) -> bool {
    v.as_object().is_some_and(|m| {
        (!required || !m.is_empty())
            && m.len() <= 20
            && m.iter().all(|(k, v)| {
                !k.trim().is_empty()
                    && k.chars().count() <= 40
                    && bounded(v, max)
                    && (!required || !v.as_str().unwrap().trim().is_empty())
            })
    })
}
pub fn fields(p: &mut Value, key: &str) -> Result<()> {
    for f in p[key].as_array_mut().ok_or_else(fail)? {
        if !localized(&f["label"], 200, true)
            || !localized(&f["description"], 10000, false)
            || !f["required"].is_boolean()
            || !f["collapsed"].is_boolean()
        {
            return Err(fail());
        }
        if f.get("sensitive").is_some_and(|v| !v.is_boolean())
            || f.get("sensitive_ttl_seconds")
                .is_some_and(|v| v.as_i64().is_none_or(|n| !(1..=600).contains(&n)))
        {
            return Err(fail());
        }
        if text(f, "type") == "select" {
            let options = f["options"].as_array().ok_or_else(fail)?;
            let mut seen = BTreeSet::new();
            for o in options {
                let value = text(o, "value");
                if o.as_object()
                    .is_none_or(|m| m.len() != 2 || !m.contains_key("label"))
                    || value.is_empty()
                    || value.len() > 100
                    || !value.as_bytes()[0].is_ascii_alphanumeric()
                    || !value
                        .bytes()
                        .all(|c| c.is_ascii_alphanumeric() || b"_.-".contains(&c))
                    || !seen.insert(value)
                    || !localized(&o["label"], 200, true)
                {
                    return Err(ApiError::bad("下拉选项字段无效或重复"));
                }
            }
        } else {
            if f.get("options")
                .filter(|v| !v.is_null())
                .is_some_and(|v| v.as_array().is_none_or(|a| !a.is_empty()))
            {
                return Err(ApiError::bad("只有下拉字段可以配置选项"));
            }
            f.as_object_mut().unwrap().remove("options");
        }
        if text(f, "type") != "images" {
            if f.get("max_items").is_some_and(|v| !v.is_null()) {
                return Err(ApiError::bad("只有图片集合可以配置图片数量"));
            }
            f.as_object_mut().unwrap().remove("max_items");
        }
        f.as_object_mut().unwrap().remove("sensitive");
        f.as_object_mut().unwrap().remove("sensitive_ttl_seconds");
    }
    Ok(())
}
pub fn marketplace(mp: &Value) -> Result<()> {
    let defaults = marketplace_defaults();
    let obj = mp.as_object().ok_or_else(fail)?;
    if obj.keys().any(|k| defaults.get(k).is_none()) {
        return Err(ApiError::bad("商城配置包含未定义字段"));
    }
    for (k, max) in [
        ("title", 200),
        ("description", 131072),
        ("contact", 4096),
        ("category_id", 36),
    ] {
        if !mp[k].is_null() && !bounded(&mp[k], max) {
            return Err(ApiError::bad(format!("商城 {k} 字段无效")));
        }
    }
    if !mp["visibility"].is_null()
        && !["public", "private", "unlisted"].contains(&text(mp, "visibility"))
    {
        return Err(ApiError::bad("商城可见范围无效"));
    }
    if !["sequential", "random"].contains(&text(mp, "delivery_strategy")) {
        return Err(ApiError::bad("商城交付顺序无效"));
    }
    for k in [
        "test_mode",
        "purchase_login_required",
        "pickup_login_required",
        "pickup_code_required",
        "email_pickup_link",
    ] {
        if !mp[k].is_boolean() {
            return Err(ApiError::bad(format!("商城 {k} 必须为布尔值")));
        }
    }
    for k in [
        "max_quantity_per_order",
        "max_quantity_per_buyer",
        "sale_limit",
    ] {
        if !mp[k].is_null()
            && mp[k]
                .as_i64()
                .is_none_or(|n| !(1..=2147483647).contains(&n))
        {
            return Err(ApiError::bad("商城数量限制必须为正整数"));
        }
    }
    if !mp["image_urls"].is_null()
        && mp["image_urls"]
            .as_array()
            .is_none_or(|a| a.len() > 32 || a.iter().any(|v| !v.is_string()))
    {
        return Err(ApiError::bad("商城图片列表无效"));
    }
    if !mp["links"].is_null() {
        let links = mp["links"].as_array().ok_or_else(fail)?;
        if links.len() > 2000 {
            return Err(ApiError::bad("商城链接过多"));
        }
        for link in links {
            let o = link.as_object().ok_or_else(fail)?;
            if o.keys()
                .any(|k| !["title", "url", "description"].contains(&k.as_str()))
                || !bounded(&link["title"], 200)
                || !bounded(&link["url"], 2048)
                || link.get("description").is_some_and(|v| !bounded(v, 10000))
            {
                return Err(ApiError::bad("商城链接字段无效"));
            }
        }
    }
    let payments = mp["payment_methods"].as_array().ok_or_else(fail)?;
    if payments.len() > 5
        || payments.iter().any(|v| {
            v.as_str().is_none_or(|s| {
                ![
                    "balance",
                    "platform:waffo_pancake",
                    "platform:linuxdo",
                    "external:epay",
                    "external:waffo_pancake",
                ]
                .contains(&s)
            })
        })
    {
        return Err(ApiError::bad("商城支付方式无效"));
    }
    Ok(())
}
pub fn product(p: &mut Value) -> Result<()> {
    for k in ["public", "allow_retry"] {
        if !p[k].is_boolean() {
            return Err(fail());
        }
    }
    if p["max_attempts"].as_i64().is_none() {
        return Err(fail());
    }
    for k in ["webhook_url", "webhook_secret", "processor_id", "script"] {
        if !p[k].is_string() {
            return Err(fail());
        }
    }
    if text(p, "webhook_url").chars().count() > 2000
        || text(p, "webhook_secret").chars().count() > 200
        || text(p, "processor_id").len() > 100
        || !text(p, "processor_id")
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
    {
        return Err(fail());
    }
    let email = text(p, "support_email").trim();
    if !email.is_empty() {
        let (local, domain) = email
            .rsplit_once('@')
            .ok_or_else(|| ApiError::bad("联系邮箱格式不正确"))?;
        if local.is_empty()
            || local.len() > 64
            || local.starts_with('.')
            || local.ends_with('.')
            || local.contains("..")
            || !local
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b".!#$%&'*+/=?^_`{|}~-".contains(&b))
            || !domain.contains('.')
            || domain.split('.').any(|s| {
                s.is_empty()
                    || s.len() > 63
                    || !s.as_bytes()[0].is_ascii_alphanumeric()
                    || !s.as_bytes()[s.len() - 1].is_ascii_alphanumeric()
                    || !s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
            })
        {
            return Err(ApiError::bad("联系邮箱格式不正确"));
        }
    }
    p["support_email"] = json!(email);
    let steps = p["progress_steps"].as_array().ok_or_else(fail)?;
    let mut seen = BTreeSet::new();
    if steps.len() > 30 {
        return Err(ApiError::bad("最多 30 个处理步骤"));
    }
    for step in steps {
        let sid = text(step, "id");
        if !field_id(sid, false)
            || !seen.insert(sid)
            || step.as_object().is_none_or(|m| m.len() != 2)
            || !localized(&step["label"], 200, true)
        {
            return Err(ApiError::bad("处理步骤无效或重复"));
        }
    }
    if text(p, "mode") == "stock" && (!steps.is_empty() || !flag(&p["outputs"][0], "required")) {
        return Err(ApiError::bad(
            "一卡一文本商品不能配置处理步骤，且 content 必须必填",
        ));
    }
    for v in p["variants"].as_array().unwrap() {
        if v.as_object().unwrap().keys().any(|k| {
            ![
                "id",
                "name",
                "description",
                "price",
                "currency",
                "attributes",
                "enabled",
                "marketplace",
            ]
            .contains(&k.as_str())
        }) || !bounded(&v["description"], 10000)
        {
            return Err(ApiError::bad("规格字段无效"));
        }
        if let Some(mp) = v.get("marketplace") {
            if mp.as_object().is_none_or(|o| {
                o.keys()
                    .any(|k| !["template", "price_quota", "fixed_content"].contains(&k.as_str()))
            }) {
                return Err(ApiError::bad("商城规格包含未定义字段"));
            }
        }
    }
    fields(p, "parameters")?;
    fields(p, "outputs")?;
    marketplace(&p["marketplace"])?;
    if !p["revision_policy"].is_null() {
        crate::fulfillment::validate_revision_policy(&p["revision_policy"])?;
        for v in p["variants"].as_array().unwrap() {
            let key = text(&p["revision_policy"], "attribute_key");
            if v["attributes"]
                .get(key)
                .is_some_and(|v| v.as_i64().is_none_or(|n| !(0..=1000).contains(&n)))
            {
                return Err(ApiError::bad("修改额度必须为 0–1000 的整数"));
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn marketplace_types_and_limits() {
        let mut m = marketplace_defaults();
        assert!(marketplace(&m).is_ok());
        m["sale_limit"] = json!(true);
        assert!(marketplace(&m).is_err());
        m["sale_limit"] = json!(0);
        assert!(marketplace(&m).is_err());
        m["sale_limit"] = json!(1);
        m["payment_methods"] = json!(["unknown"]);
        assert!(marketplace(&m).is_err());
    }
    #[test]
    fn unknown_private_config_is_rejected() {
        let mut m = marketplace_defaults();
        m["unknown"] = json!("value");
        assert!(marketplace(&m).is_err());
    }
}

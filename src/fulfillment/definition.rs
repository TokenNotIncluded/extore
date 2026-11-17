//! Bounded task graph validation; graphs never select executable code or credentials.
use super::*;
use std::collections::{BTreeMap, HashSet};
fn invalid(message: &str) -> ApiError {
    ApiError::new(422, format!("任务流程{message}"))
}
fn keys(v: &Value, allowed: &[&str], required: &[&str]) -> Result<()> {
    if v.as_object().is_none_or(|o| {
        o.keys().any(|k| !allowed.contains(&k.as_str()))
            || required.iter().any(|k| !o.contains_key(*k))
    }) {
        return Err(invalid("字段无效或缺少必填项"));
    }
    Ok(())
}
fn ident(value: &str, max: usize, hyphen: bool) -> bool {
    !value.is_empty()
        && value.len() <= max
        && value.as_bytes()[0].is_ascii_lowercase()
        && value.bytes().all(|b| {
            b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_' || (hyphen && b == b'-')
        })
}
fn target(v: &Value) -> Result<String> {
    v.as_str()
        .filter(|s| ident(s, 64, true))
        .map(String::from)
        .ok_or_else(|| invalid("节点目标代码无效"))
}
fn i18n(v: &Value, limit: usize, required: bool) -> Result<Value> {
    let o = v
        .as_object()
        .filter(|o| o.len() <= 20)
        .ok_or_else(|| invalid("多语言文本无效"))?;
    let mut out = serde_json::Map::new();
    for (k, v) in o {
        let s = v.as_str().ok_or_else(|| invalid("多语言文本无效"))?;
        if k.trim().is_empty()
            || k.chars().count() > 40
            || k.chars().any(char::is_control)
            || s.chars().count() > limit
            || s.contains('\0')
        {
            return Err(invalid("多语言文本无效或过长"));
        }
        if !s.trim().is_empty() {
            out.insert(k.clone(), v.clone());
        }
    }
    if required && out.is_empty() {
        return Err(invalid("多语言文本必须至少有一项非空文本"));
    }
    Ok(Value::Object(out))
}
fn boolean(v: &Value, key: &str, default: bool) -> Result<bool> {
    match v.get(key) {
        Some(v) => v.as_bool().ok_or_else(|| invalid("布尔配置必须是布尔值")),
        None => Ok(default),
    }
}
pub fn fields(v: &Value, sensitive: bool) -> Result<Value> {
    let list = v
        .as_array()
        .filter(|a| a.len() <= 30)
        .ok_or_else(|| invalid("字段数量无效"))?;
    let mut seen = HashSet::new();
    let mut result = Vec::new();
    for f in list {
        keys(
            f,
            &[
                "key",
                "label",
                "description",
                "collapsed",
                "required",
                "type",
                "options",
                "max_items",
                "sensitive",
                "sensitive_ttl_seconds",
            ],
            &["key", "label"],
        )?;
        let key = text(f, "key");
        let kind = f.get("type").and_then(Value::as_str).unwrap_or("text");
        if !ident(key, 40, false)
            || !seen.insert(key)
            || ![
                "text", "textarea", "email", "url", "number", "file", "image", "images", "select",
                "boolean",
            ]
            .contains(&kind)
        {
            return Err(invalid("字段代码或类型无效或重复"));
        }
        let mut normalized = json!({"key":key,"label":i18n(&f["label"],200,true)?,"description":i18n(f.get("description").unwrap_or(&json!({})),10000,false)?,"collapsed":boolean(f,"collapsed",true)?,"required":boolean(f,"required",true)?,"type":kind});
        if kind == "select" {
            let options = f["options"]
                .as_array()
                .filter(|a| !a.is_empty() && a.len() <= 50)
                .ok_or_else(|| invalid("选择项数量无效"))?;
            let mut choices = Vec::new();
            let mut values = HashSet::new();
            for o in options {
                keys(o, &["value", "label"], &["value", "label"])?;
                let v = text(o, "value");
                if v.is_empty()
                    || v.trim() != v
                    || v.chars().count() > 100
                    || v.chars().any(char::is_control)
                    || !values.insert(v)
                {
                    return Err(invalid("选择项代码无效或重复"));
                }
                choices.push(json!({"value":v,"label":i18n(&o["label"],200,true)?}));
            }
            normalized["options"] = json!(choices);
        } else if f.get("options").is_some() {
            return Err(invalid("只有选择字段允许选择项"));
        }
        if kind == "images" {
            let max = integer(f, "max_items", 10);
            if !(1..=20).contains(&max) || f.get("max_items").is_some_and(|v| v.as_i64().is_none())
            {
                return Err(invalid("图片数量上限无效"));
            }
            normalized["max_items"] = json!(max);
        } else if f.get("max_items").is_some() {
            return Err(invalid("只有图片列表允许数量上限"));
        }
        if boolean(f, "sensitive", false)? {
            let ttl = integer(f, "sensitive_ttl_seconds", 120);
            if !sensitive
                || kind != "text"
                || !(1..=600).contains(&ttl)
                || f.get("sensitive_ttl_seconds")
                    .is_some_and(|v| v.as_i64().is_none())
            {
                return Err(invalid("敏感字段只能是输入文本且有效期在一至六百秒内"));
            }
            normalized["sensitive"] = json!(true);
            normalized["sensitive_ttl_seconds"] = json!(ttl);
        } else if f.get("sensitive_ttl_seconds").is_some() {
            return Err(invalid("只有敏感字段允许有效期"));
        }
        result.push(normalized);
    }
    Ok(json!(result))
}
fn reference(v: &Value) -> Result<Value> {
    keys(v, &["node", "field"], &["node", "field"])?;
    let node = target(&v["node"])?;
    if !ident(text(v, "field"), 40, false) {
        return Err(invalid("引用字段代码无效"));
    }
    Ok(json!({"node":node,"field":v["field"]}))
}
fn mapping(v: &Value) -> Result<Value> {
    let o = v
        .as_object()
        .filter(|a| a.len() <= 30)
        .ok_or_else(|| invalid("字段映射无效"))?;
    let mut m = serde_json::Map::new();
    for (key, r) in o {
        if !ident(key, 40, false) {
            return Err(invalid("映射字段代码无效"));
        }
        m.insert(key.clone(), reference(r)?);
    }
    Ok(Value::Object(m))
}
fn next(v: &Value) -> Result<Value> {
    if v.is_string() {
        return Ok(json!(target(v)?));
    }
    keys(v, &["cases", "default"], &["cases", "default"])?;
    let cases = v["cases"]
        .as_array()
        .filter(|a| !a.is_empty() && a.len() <= 32)
        .ok_or_else(|| invalid("条件分支数量无效"))?;
    let mut out = Vec::new();
    for case in cases {
        keys(case, &["when", "to"], &["when", "to"])?;
        let w = &case["when"];
        keys(w, &["source", "op", "value"], &["source", "op"])?;
        let op = text(w, "op");
        let mut when = json!({"source":reference(&w["source"])?,"op":op});
        match op {
            "exists" => {
                if w.get("value").is_some() {
                    return Err(invalid("存在性条件不接受比较值"));
                }
            }
            "eq" => {
                if !w["value"].is_string() {
                    return Err(invalid("相等条件必须有文本比较值"));
                }
                when["value"] = w["value"].clone();
            }
            "in" => {
                if w["value"].as_array().is_none_or(|a| {
                    a.is_empty() || a.len() > 50 || a.iter().any(|v| !v.is_string())
                }) {
                    return Err(invalid("集合条件必须有文本比较值列表"));
                }
                when["value"] = w["value"].clone();
            }
            _ => return Err(invalid("条件操作无效")),
        }
        out.push(json!({"when":when,"to":target(&case["to"])?}));
    }
    Ok(json!({"cases":out,"default":target(&v["default"])?}))
}
fn timeout(node: &Value, process: bool) -> Result<Value> {
    let seconds = node.get("timeout_seconds").cloned().unwrap_or(if process {
        json!(3600)
    } else {
        Value::Null
    });
    if seconds.is_null() {
        if process || node.get("timeout_next").is_some() {
            return Err(invalid("超时路径配置无效"));
        }
        return Ok(json!({"timeout_seconds":null}));
    }
    if seconds.as_i64().is_none_or(|s| !(1..=86400).contains(&s)) {
        return Err(invalid("超时时限无效"));
    }
    Ok(json!({"timeout_seconds":seconds,"timeout_next":target(&node["timeout_next"])?}))
}
pub fn reference_origin(def: &Value, r: &Value) -> Result<(Value, Value)> {
    let nodes = def["nodes"].as_array().ok_or_else(|| invalid("节点无效"))?;
    let mut r = r.clone();
    let mut seen = HashSet::new();
    for _ in 0..=64 * 30 {
        let node = text(&r, "node");
        let field = text(&r, "field");
        if !seen.insert((node.to_owned(), field.to_owned())) {
            return Err(invalid("字段引用不存在或形成无来源的循环"));
        }
        let n = nodes
            .iter()
            .find(|n| text(n, "id") == node)
            .ok_or_else(|| invalid("字段引用不存在"))?;
        if matches!(text(n, "kind"), "input" | "process") {
            let fs = if text(n, "kind") == "input" {
                &n["fields"]
            } else {
                &n["outputs"]
            };
            let f = fs
                .as_array()
                .and_then(|fs| fs.iter().find(|f| text(f, "key") == field))
                .ok_or_else(|| invalid("字段引用不存在"))?;
            return Ok((r.clone(), f.clone()));
        }
        r = n[if text(n, "kind") == "display" {
            "show_from"
        } else {
            "result"
        }]
        .get(field)
        .cloned()
        .ok_or_else(|| invalid("字段引用不存在"))?;
    }
    Err(invalid("字段引用超过上限"))
}
fn compatible(a: &Value, b: &Value) -> bool {
    if a["type"] != b["type"] {
        return false;
    }
    if text(a, "type") == "images" {
        return integer(a, "max_items", 10) <= integer(b, "max_items", 10);
    }
    if text(a, "type") == "select" {
        return a["options"].as_array().is_some_and(|list| {
            list.iter().all(|o| {
                b["options"]
                    .as_array()
                    .is_some_and(|list| list.iter().any(|t| t["value"] == o["value"]))
            })
        });
    }
    true
}
pub fn edges(n: &Value) -> Vec<String> {
    let mut out = Vec::new();
    if text(n, "kind") != "end" {
        if let Some(s) = n["next"].as_str() {
            out.push(s.to_owned());
        } else {
            if let Some(a) = n["next"]["cases"].as_array() {
                out.extend(a.iter().map(|c| text(c, "to").to_owned()));
            }
            out.push(text(&n["next"], "default").to_owned());
        }
    }
    for k in ["timeout_next", "failure_next"] {
        if let Some(s) = n[k].as_str() {
            out.push(s.to_owned());
        }
    }
    out
}
pub fn normalize(value: &Value, p: &Value) -> Result<Value> {
    if value.is_null() {
        return Ok(Value::Null);
    }
    keys(
        value,
        &["version", "entry", "nodes"],
        &["version", "entry", "nodes"],
    )?;
    if value.to_string().len() > 100000 || value["version"].as_i64() != Some(1) {
        return Err(invalid("版本无效或定义超过大小上限"));
    }
    if !["manual", "script", "webhook"].contains(&text(p, "mode")) {
        return Err(invalid("商品处理方式无效"));
    }
    let supplied = value["nodes"]
        .as_array()
        .filter(|a| !a.is_empty() && a.len() <= 64)
        .ok_or_else(|| invalid("节点数量无效"))?;
    let parameters = fields(p.get("parameters").unwrap_or(&json!([])), true)?;
    let outputs = fields(p.get("outputs").unwrap_or(&json!([])), false)?;
    let mut ids = HashSet::new();
    let mut nodes = Vec::new();
    for node in supplied {
        let kind = text(node, "kind");
        let allowed: &[&str] = match kind {
            "input" => &[
                "id",
                "kind",
                "label",
                "prompt",
                "question",
                "fields",
                "start_policy",
                "show_from",
                "timeout_seconds",
                "timeout_next",
                "next",
            ],
            "process" => &[
                "id",
                "kind",
                "label",
                "inputs",
                "outputs",
                "timeout_seconds",
                "timeout_next",
                "failure_next",
                "next",
            ],
            "display" => &[
                "id",
                "kind",
                "label",
                "content",
                "show_from",
                "result",
                "timeout_seconds",
                "timeout_next",
                "next",
            ],
            "end" => &[
                "id",
                "kind",
                "label",
                "state",
                "result",
                "message",
                "retryable",
                "needs_review",
            ],
            _ => return Err(invalid("节点类型无效")),
        };
        keys(node, allowed, &["id", "kind"])?;
        let nid = target(&node["id"])?;
        if !ids.insert(nid.clone()) {
            return Err(invalid("节点代码重复"));
        }
        let mut n = json!({"id":nid,"kind":kind});
        if let Some(label) = node.get("label") {
            n["label"] = i18n(label, 200, false)?;
        }
        match kind {
            "input" => {
                let policy = node
                    .get("start_policy")
                    .and_then(Value::as_str)
                    .unwrap_or("confirm");
                if !["confirm", "automatic"].contains(&policy) {
                    return Err(invalid("开始策略无效"));
                }
                for key in ["prompt", "question"] {
                    n[key] = i18n(node.get(key).unwrap_or(&json!({})), 10000, false)?;
                }
                n["fields"] = fields(&node["fields"], true)?;
                n["start_policy"] = json!(policy);
                n["show_from"] = mapping(node.get("show_from").unwrap_or(&json!({})))?;
                n["next"] = next(&node["next"])?;
                merge(&mut n, timeout(node, false)?);
            }
            "process" => {
                n["inputs"] = mapping(&node["inputs"])?;
                n["outputs"] = fields(&node["outputs"], false)?;
                n["next"] = next(&node["next"])?;
                n["failure_next"] = json!(target(&node["failure_next"])?);
                merge(&mut n, timeout(node, true)?);
            }
            "display" => {
                if node.get("result").is_some() && node.get("show_from").is_some() {
                    return Err(invalid("展示字段映射存在歧义"));
                }
                n["content"] = i18n(node.get("content").unwrap_or(&json!({})), 10000, false)?;
                n["show_from"] = mapping(
                    node.get("show_from")
                        .or_else(|| node.get("result"))
                        .unwrap_or(&json!({})),
                )?;
                n["next"] = json!(target(&node["next"])?);
                merge(&mut n, timeout(node, false)?);
            }
            "end" => {
                let state = text(node, "state");
                if !["succeeded", "failed", "rejected"].contains(&state) {
                    return Err(invalid("终态无效"));
                }
                let result = mapping(node.get("result").unwrap_or(&json!({})))?;
                if state != "succeeded" && !result.as_object().unwrap().is_empty() {
                    return Err(invalid("失败或拒绝终态不能保留结果映射"));
                }
                if state == "succeeded" {
                    let fields = outputs.as_array().unwrap();
                    if result
                        .as_object()
                        .unwrap()
                        .keys()
                        .any(|k| !fields.iter().any(|f| text(f, "key") == k))
                        || fields
                            .iter()
                            .filter(|f| flag(f, "required"))
                            .any(|f| result.get(text(f, "key")).is_none())
                    {
                        return Err(invalid("成功终态输出不符合商品定义"));
                    }
                }
                n["state"] = json!(state);
                n["result"] = result;
                n["message"] = i18n(node.get("message").unwrap_or(&json!({})), 10000, false)?;
                n["retryable"] = json!(boolean(node, "retryable", false)?);
                n["needs_review"] = json!(boolean(node, "needs_review", false)?);
            }
            _ => unreachable!(),
        }
        nodes.push(n);
    }
    let entry = target(&value["entry"])?;
    if !nodes
        .iter()
        .any(|n| text(n, "id") == entry && matches!(text(n, "kind"), "input" | "display"))
    {
        return Err(invalid("入口必须是输入或展示节点"));
    }
    for n in &nodes {
        if edges(n).iter().any(|t| !ids.contains(t)) {
            return Err(invalid("引用了不存在的节点目标"));
        }
    }
    let mut canonical = json!({"version":1,"entry":entry,"nodes":nodes});
    let copy = canonical.clone();
    for n in canonical["nodes"].as_array_mut().unwrap() {
        let kind = text(n, "kind").to_owned();
        if matches!(kind.as_str(), "input" | "display") {
            for r in n["show_from"].as_object().unwrap().values() {
                let (_, f) = reference_origin(&copy, r)?;
                if flag(&f, "sensitive") {
                    return Err(invalid("敏感字段只能传入处理节点"));
                }
            }
        }
        if kind == "process" {
            for (key, r) in n["inputs"].as_object().unwrap() {
                let (_, f) = reference_origin(&copy, r)?;
                if text(p, "mode") == "script"
                    && !parameters
                        .as_array()
                        .unwrap()
                        .iter()
                        .any(|dest| text(dest, "key") == key && compatible(&f, dest))
                {
                    return Err(invalid("处理器输入与商品定义不兼容"));
                }
            }
            if text(p, "mode") == "script" {
                if parameters
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|f| flag(f, "required") && n["inputs"].get(text(f, "key")).is_none())
                {
                    return Err(invalid("处理器缺少商品必填输入"));
                }
                let fs = n["outputs"].as_array().unwrap();
                let dest = outputs.as_array().unwrap();
                if fs.len() != dest.len()
                    || fs.iter().any(|f| {
                        !dest.iter().any(|d| {
                            text(f, "key") == text(d, "key")
                                && compatible(f, d)
                                && compatible(d, f)
                                && f["required"] == d["required"]
                        })
                    })
                {
                    return Err(invalid("处理器输出与商品定义不兼容"));
                }
            }
        }
        if let Some(cases) = n
            .get_mut("next")
            .and_then(|v| v.get_mut("cases"))
            .and_then(Value::as_array_mut)
        {
            for case in cases {
                let when = &mut case["when"];
                let (_, mut f) = reference_origin(&copy, &when["source"])?;
                if flag(&f, "sensitive") {
                    return Err(invalid("条件分支不能引用敏感字段"));
                }
                f["required"] = json!(false);
                if text(when, "op") != "exists" {
                    let values = if text(when, "op") == "eq" {
                        vec![when["value"].clone()]
                    } else {
                        when["value"].as_array().unwrap().clone()
                    };
                    let mut cleaned = Vec::new();
                    for v in values {
                        let mut input = json!({});
                        input[text(&f, "key")] = v;
                        let clean = super::validate_values(&json!([f]), &input, false, false)?;
                        cleaned.push(clean[text(&f, "key")].clone());
                    }
                    when["value"] = if text(when, "op") == "eq" {
                        cleaned.remove(0)
                    } else {
                        json!(cleaned)
                    };
                }
            }
        }
        if kind == "end" && text(n, "state") == "succeeded" {
            for (key, r) in n["result"].as_object().unwrap() {
                let (origin, f) = reference_origin(&copy, r)?;
                if flag(&f, "sensitive") {
                    return Err(invalid("最终输出不能引用敏感字段"));
                }
                let out = outputs
                    .as_array()
                    .unwrap()
                    .iter()
                    .find(|f| text(f, "key") == key)
                    .unwrap();
                let source = copy["nodes"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .find(|n| n["id"] == origin["node"])
                    .unwrap();
                if (matches!(text(out, "type"), "file" | "image" | "images")
                    && text(source, "kind") != "process")
                    || !compatible(&f, out)
                {
                    return Err(invalid("最终输出字段类型或附件来源不兼容"));
                }
            }
        }
    }
    let map = canonical["nodes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|n| (text(n, "id").to_owned(), n))
        .collect::<BTreeMap<_, _>>();
    let mut visited = HashSet::new();
    let mut pending = vec![entry];
    while let Some(nid) = pending.pop() {
        if visited.insert(nid.clone()) {
            pending.extend(edges(map[&nid]));
        }
    }
    if !visited.iter().any(|id| text(map[id], "kind") == "end") {
        return Err(invalid("必须存在从入口可达的结束节点"));
    }
    if canonical.to_string().len() > 100000 {
        return Err(invalid("规范化定义超过大小上限"));
    }
    let mut frozen = json!({});
    for key in [
        "id",
        "name",
        "mode",
        "delivery",
        "view_policy",
        "parameters",
        "outputs",
        "allow_retry",
        "max_attempts",
        "processor_id",
        "webhook_url",
        "webhook_secret",
        "shop_id",
    ] {
        if let Some(v) = p.get(key) {
            frozen[key] = v.clone();
        }
    }
    if json!({"definition":canonical,"product":frozen})
        .to_string()
        .len()
        + 128
        > 200000
    {
        return Err(invalid("商品与流程快照超过大小上限"));
    }
    Ok(canonical)
}
pub fn validate_flow(def: &Value, p: &Value) -> Result<()> {
    normalize(def, p).map(|_| ())
}
pub fn validate_revision_policy(v: &Value) -> Result<()> {
    if v.is_null() {
        return Ok(());
    }
    keys(v, &["attribute_key", "label"], &["attribute_key", "label"])?;
    let k = text(v, "attribute_key");
    if k.is_empty() || k.trim() != k || k.chars().count() > 100 {
        return Err(ApiError::bad("修改额度属性名不能为空或包含首尾空白"));
    }
    i18n(&v["label"], 200, true)?;
    Ok(())
}

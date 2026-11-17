use crate::common::*;
use serde_json::{Value, json};
use std::collections::BTreeSet;

fn invalid() -> ApiError {
    ApiError::new(422, "工作流配置不符合安全限制")
}
pub fn defaults() -> Value {
    json!({"variables":{},"secrets":{},"runtime":{"timeout_seconds":120,"memory_mb":256,"cpu_seconds":120,"max_output_bytes":1000000}})
}
fn valid_name(name: &str) -> bool {
    const RESERVED: &[&str] = &[
        "PATH",
        "LANG",
        "LANGUAGE",
        "HOME",
        "SHELL",
        "ENV",
        "IFS",
        "CDPATH",
        "FPATH",
        "PWD",
        "OLDPWD",
        "LIBRARY_PATH",
        "CPATH",
        "C_INCLUDE_PATH",
        "CPLUS_INCLUDE_PATH",
        "OBJC_INCLUDE_PATH",
        "LOCPATH",
        "TZ",
        "TZDIR",
        "RES_OPTIONS",
        "LOCALDOMAIN",
        "HOSTALIASES",
        "DISPLAY",
        "PROMPT_COMMAND",
        "PS4",
        "SHELLOPTS",
        "BASHOPTS",
        "PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    ];
    const PREFIXES: &[&str] = &[
        "EXTORE_",
        "PYTHON",
        "LD",
        "DYLD",
        "BWRAP_",
        "TMP",
        "TEMP",
        "SSL",
        "OPENSSL",
        "BASH",
        "ZSH",
        "GLIBC",
        "GCONV",
        "XDG_",
        "DBUS_",
        "LC_",
        "VIRTUAL_ENV",
        "CONDA",
        "PIP",
        "UV_",
        "GIT_",
        "NODE",
        "RUBY",
        "PERL",
        "JAVA",
        "SSH_",
        "GPG_",
    ];
    !name.is_empty()
        && name.len() <= 64
        && name
            .bytes()
            .enumerate()
            .all(|(i, b)| b.is_ascii_uppercase() || i > 0 && (b.is_ascii_digit() || b == b'_'))
        && !RESERVED.contains(&name)
        && !PREFIXES.iter().any(|p| name.starts_with(p))
        && !name.ends_with("_PROXY")
}
pub fn validate(value: &Value) -> Result<Value> {
    let object = value.as_object().ok_or_else(invalid)?;
    if object
        .keys()
        .any(|k| !["variables", "secrets", "runtime"].contains(&k.as_str()))
    {
        return Err(invalid());
    }
    let mut result = defaults();
    let mut bytes = 0usize;
    for group in ["variables", "secrets"] {
        if let Some(values) = object.get(group) {
            let values = values.as_object().ok_or_else(invalid)?;
            if values.len() > 64 {
                return Err(invalid());
            }
            for (key, value) in values {
                let s = value.as_str().ok_or_else(invalid)?;
                if !valid_name(key) || s.contains('\0') || s.len() > 8192 {
                    return Err(invalid());
                }
                bytes += s.len();
                result[group][key] = json!(s);
            }
        }
    }
    if bytes > 65536
        || result["variables"]
            .as_object()
            .unwrap()
            .keys()
            .any(|k| result["secrets"].get(k).is_some())
    {
        return Err(invalid());
    }
    if let Some(runtime) = object.get("runtime") {
        let runtime = runtime.as_object().ok_or_else(invalid)?;
        if runtime.keys().any(|k| result["runtime"].get(k).is_none()) {
            return Err(invalid());
        }
        result["runtime"]
            .as_object_mut()
            .unwrap()
            .extend(runtime.clone());
    }
    for (key, min, max) in [
        ("timeout_seconds", 10, 120),
        ("memory_mb", 64, 512),
        ("cpu_seconds", 1, 120),
        ("max_output_bytes", 65536, 1000000),
    ] {
        if result["runtime"][key]
            .as_i64()
            .is_none_or(|v| v < min || v > max)
        {
            return Err(invalid());
        }
    }
    result["secrets"]
        .as_object_mut()
        .unwrap()
        .retain(|_, v| !v.as_str().unwrap().trim().is_empty());
    Ok(result)
}
pub fn patch(old: &Value, patch: &Value) -> Result<Value> {
    let object = patch.as_object().ok_or_else(invalid)?;
    if object.keys().any(|k| {
        ![
            "variables",
            "secrets",
            "runtime",
            "delete_variables",
            "delete_secrets",
        ]
        .contains(&k.as_str())
    }) {
        return Err(invalid());
    }
    let mut merged = old.clone();
    for group in ["variables", "secrets"] {
        let supplied = object.get(group).cloned().unwrap_or(json!({}));
        let fields = supplied.as_object().ok_or_else(invalid)?;
        let delete = object
            .get(&format!("delete_{group}"))
            .cloned()
            .unwrap_or(json!([]));
        let delete = delete.as_array().ok_or_else(invalid)?;
        let mut seen = BTreeSet::new();
        if delete.len() > 64 {
            return Err(invalid());
        }
        for name in delete {
            let name = name.as_str().ok_or_else(invalid)?;
            if !valid_name(name) || !seen.insert(name) || fields.contains_key(name) {
                return Err(invalid());
            }
            merged[group]
                .as_object_mut()
                .ok_or_else(invalid)?
                .remove(name);
        }
        let normalized = validate(&json!({group:supplied}))?;
        merged[group]
            .as_object_mut()
            .ok_or_else(invalid)?
            .extend(normalized[group].as_object().unwrap().clone());
    }
    if let Some(runtime) = object.get("runtime") {
        merged["runtime"]
            .as_object_mut()
            .ok_or_else(invalid)?
            .extend(runtime.as_object().ok_or_else(invalid)?.clone());
    }
    validate(&merged)
}
pub fn view(value: &Value) -> Value {
    json!({"variables":value["variables"],"runtime":value["runtime"],"configured_secret_names":value["secrets"].as_object().map(|v|v.keys().cloned().collect::<Vec<_>>()).unwrap_or_default()})
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn reserved_and_loader_names_fail() {
        for name in [
            "PATH",
            "LD_PRELOAD",
            "EXTORE_TOKEN",
            "HTTP_PROXY",
            "PYTHONPATH",
            "A_PROXY",
            "TMPFILE",
            "HOME",
            "lower",
        ] {
            assert!(validate(&json!({"secrets":{name:"x"}})).is_err(), "{name}");
        }
    }
    #[test]
    fn blank_secret_preserves_and_explicit_delete_removes() {
        let old =
            validate(&json!({"variables":{"REGION":"cn"},"secrets":{"TOKEN":"secret"}})).unwrap();
        let merged = patch(&old, &json!({"secrets":{"TOKEN":" "}})).unwrap();
        assert_eq!(merged["secrets"]["TOKEN"], "secret");
        let removed = patch(&merged, &json!({"delete_secrets":["TOKEN"]})).unwrap();
        assert!(removed["secrets"].as_object().unwrap().is_empty());
        assert!(
            patch(
                &old,
                &json!({"delete_secrets":["TOKEN"],"secrets":{"TOKEN":"a"}})
            )
            .is_err()
        );
    }
    #[test]
    fn bounds_and_group_overlap_fail() {
        assert!(validate(&json!({"variables":{"KEY":"a"},"secrets":{"KEY":"b"}})).is_err());
        assert!(validate(&json!({"runtime":{"memory_mb":513}})).is_err());
        assert!(validate(&json!({"runtime":{"timeout_seconds":10.0}})).is_err());
        assert!(validate(&json!({"variables":{"KEY":"a".repeat(8193)}})).is_err());
    }
}

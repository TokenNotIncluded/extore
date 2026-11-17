//! Execute the reviewed Python processor SDK inside the existing offline sandbox.
//! The Rust worker owns authority, leases, input/output limits, and all database writes.
use super::*;
use std::{
    collections::BTreeMap,
    io::{BufRead, BufReader, Read, Write},
    os::unix::process::CommandExt,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::mpsc,
    time::Instant,
};

fn runtime_error() -> ApiError {
    ApiError::new(500, "处理器沙箱不可用")
}
fn output(program: &Path, args: &[&str]) -> Result<Vec<u8>> {
    let out = Command::new(program)
        .args(args)
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("LC_ALL", "C")
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()?;
    if !out.status.success() || out.stdout.len() > 1024 * 1024 {
        return Err(runtime_error());
    }
    Ok(out.stdout)
}
fn dependencies(path: &Path, mounts: &mut BTreeMap<String, String>) -> Result<()> {
    let data = output(
        Path::new("/usr/bin/ldd"),
        &[path.to_str().ok_or_else(runtime_error)?],
    )?;
    let text = std::str::from_utf8(&data).map_err(|_| runtime_error())?;
    if text.contains("not found") {
        return Err(runtime_error());
    }
    for line in text.lines() {
        let words = line.split_whitespace().collect::<Vec<_>>();
        for (i, w) in words.iter().enumerate() {
            if w.starts_with('/') && words.get(i + 1).is_some_and(|n| n.starts_with('(')) {
                let source = Path::new(w).canonicalize()?;
                if !source.is_file() {
                    return Err(runtime_error());
                }
                let target = if w.starts_with("/lib/") || w.starts_with("/lib64/") {
                    format!("/usr{w}")
                } else {
                    (*w).to_owned()
                };
                mounts.insert(target, source.to_string_lossy().into_owned());
            }
        }
    }
    Ok(())
}
fn sandbox(app: &App, processor_id: &str) -> Result<(PathBuf, PathBuf, Vec<String>)> {
    if processor_id.is_empty()
        || processor_id.len() > 100
        || !processor_id.as_bytes()[0].is_ascii_lowercase()
        || !processor_id
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
    {
        return Err(runtime_error());
    }
    let version = output(Path::new("/usr/bin/bwrap"), &["--version"])?;
    let value = std::str::from_utf8(&version)
        .map_err(|_| runtime_error())?
        .trim()
        .strip_prefix("bubblewrap ")
        .ok_or_else(runtime_error)?;
    let parts = value
        .split('.')
        .map(|p| p.parse::<u32>())
        .collect::<std::result::Result<Vec<_>, _>>()
        .map_err(|_| runtime_error())?;
    if parts.len() < 2 || (parts[0], parts[1]) < (0, 12) {
        return Err(runtime_error());
    }
    let python = PathBuf::from(
        std::env::var("EXTORE_PROCESSOR_PYTHON").unwrap_or_else(|_| "/usr/bin/python3".to_owned()),
    );
    let metadata = output(
        &python,
        &[
            "-I",
            "-S",
            "-c",
            "import json,sys,sysconfig;print(json.dumps({'executable':sys.executable,'stdlib':sysconfig.get_path('stdlib'),'version':f'python{sys.version_info.major}.{sys.version_info.minor}'}))",
        ],
    )?;
    let metadata: Value = serde_json::from_slice(&metadata)?;
    let executable = PathBuf::from(text(&metadata, "executable")).canonicalize()?;
    let stdlib = PathBuf::from(text(&metadata, "stdlib")).canonicalize()?;
    let version = text(&metadata, "version");
    let ldconfig = [
        "/usr/sbin/ldconfig",
        "/usr/bin/ldconfig",
        "/sbin/ldconfig",
        "/bin/ldconfig",
    ]
    .iter()
    .map(Path::new)
    .find(|p| p.is_file())
    .ok_or_else(runtime_error)?;
    let cache = output(ldconfig, &["-p"])?;
    let cache = std::str::from_utf8(&cache).map_err(|_| runtime_error())?;
    let seccomp = cache
        .lines()
        .find(|l| l.trim_start().starts_with("libseccomp.so.2 "))
        .and_then(|l| l.split("=>").nth(1))
        .map(|s| PathBuf::from(s.trim()))
        .ok_or_else(runtime_error)?
        .canonicalize()?;
    let mut mounts = BTreeMap::new();
    dependencies(&executable, &mut mounts)?;
    dependencies(&seccomp, &mut mounts)?;
    let extensions = [
        "_ctypes",
        "_sqlite3",
        "_ssl",
        "_hashlib",
        "_socket",
        "_struct",
        "_json",
        "_datetime",
        "_random",
        "_sha2",
        "_blake2",
        "unicodedata",
        "math",
        "array",
        "binascii",
        "zlib",
        "select",
        "_posixsubprocess",
        "fcntl",
    ];
    for p in std::fs::read_dir(stdlib.join("lib-dynload"))? {
        let p = p?.path();
        let name = p.file_name().and_then(|s| s.to_str()).unwrap_or("");
        if name.ends_with(".so") && extensions.contains(&name.split('.').next().unwrap_or("")) {
            dependencies(&p, &mut mounts)?;
        }
    }
    mounts.insert(
        "/usr/lib/libseccomp.so.2".to_owned(),
        seccomp.to_string_lossy().into_owned(),
    );
    let root = app
        .static_dir
        .canonicalize()?
        .parent()
        .ok_or_else(runtime_error)?
        .to_path_buf();
    let catalog = root
        .parent()
        .ok_or_else(runtime_error)?
        .join("processors/official/extore_processors")
        .canonicalize()?;
    let runner = root.join("processor_runner.py");
    if !runner.is_file() || !catalog.join("__main__.py").is_file() {
        return Err(runtime_error());
    }
    let mut args = [
        "--unshare-user",
        "--unshare-ipc",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-uts",
        "--unshare-cgroup",
        "--uid",
        "1000",
        "--gid",
        "1000",
        "--disable-userns",
        "--assert-userns-disabled",
        "--cap-drop",
        "ALL",
        "--die-with-parent",
        "--new-session",
        "--hostname",
        "extore-processor",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect::<Vec<_>>();
    let mut mount = |source: &Path, target: String| {
        args.extend([
            "--ro-bind".to_owned(),
            source.to_string_lossy().into_owned(),
            target,
        ]);
    };
    mount(&executable, "/python/bin/python3".to_owned());
    mount(&stdlib, format!("/python/lib/{version}"));
    if stdlib.join("site-packages").exists() {
        args.extend([
            "--tmpfs".to_owned(),
            format!("/python/lib/{version}/site-packages"),
            "--remount-ro".to_owned(),
            format!("/python/lib/{version}/site-packages"),
        ]);
    }
    for (destination, source) in mounts {
        args.extend(["--ro-bind".to_owned(), source, destination]);
    }
    for (source, target) in [
        (catalog, "/code/extore_processors"),
        (root.join("__init__.py"), "/code/extore/__init__.py"),
        (root.join("variants.py"), "/code/extore/variants.py"),
        (
            root.join("task_flow_definition.py"),
            "/code/extore/task_flow_definition.py",
        ),
        (
            root.join("task_flow_schema.py"),
            "/code/extore/task_flow_schema.py",
        ),
        (root.join("sdk"), "/code/extore/sdk"),
        (runner.clone(), "/runner.py"),
    ] {
        args.extend([
            "--ro-bind".to_owned(),
            source.to_string_lossy().into_owned(),
            target.to_owned(),
        ]);
    }
    args.extend(
        [
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--remount-ro",
            "/dev",
            "--remount-ro",
            "/proc",
            "--size",
            "16777216",
            "--tmpfs",
            "/work",
            "--size",
            "16777216",
            "--tmpfs",
            "/tmp",
            "--chmod",
            "0700",
            "/work",
            "--chmod",
            "0700",
            "/tmp",
            "--chdir",
            "/work",
            "--remount-ro",
            "/",
            "--",
            "/python/bin/python3",
            "-I",
            "-S",
            "-B",
            "-u",
            "-X",
            "utf8",
            "/runner.py",
            "--execute",
            processor_id,
        ]
        .iter()
        .map(|s| s.to_string()),
    );
    Ok((executable, runner, args))
}
fn workflow(value: &Value) -> Result<Value> {
    let mut v = value.clone();
    if !v.is_object() {
        return Err(runtime_error());
    }
    let mut total = 0;
    let mut seen = std::collections::BTreeSet::new();
    for group in ["variables", "secrets"] {
        if v.get(group).is_none() {
            v[group] = json!({});
        }
        let o = v[group].as_object().ok_or_else(runtime_error)?;
        if o.len() > 64 {
            return Err(runtime_error());
        }
        for (key, value) in o {
            let data = value.as_str().ok_or_else(runtime_error)?;
            if key.is_empty()
                || key.len() > 64
                || !key.as_bytes()[0].is_ascii_uppercase()
                || !key
                    .bytes()
                    .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'_')
                || !seen.insert(key.to_owned())
                || data.contains('\0')
                || data.len() > 8192
            {
                return Err(runtime_error());
            }
            total += data.len();
        }
    }
    if total > 65536 {
        return Err(runtime_error());
    }
    if v.get("runtime").is_none() {
        v["runtime"] = json!({});
    }
    for (k, default, min, max) in [
        ("timeout_seconds", 120, 10, 120),
        ("memory_mb", 256, 64, 512),
        ("cpu_seconds", 120, 1, 120),
        ("max_output_bytes", 1000000, 65536, 1000000),
    ] {
        if v["runtime"].get(k).is_none() {
            v["runtime"][k] = json!(default);
        }
        if v["runtime"][k].as_i64().is_none_or(|n| n < min || n > max) {
            return Err(runtime_error());
        }
    }
    if v["runtime"].as_object().is_none_or(|v| v.len() != 4) {
        return Err(runtime_error());
    }
    Ok(v)
}
fn authority(
    app: Arc<App>,
    job: &Value,
    processor_id: &str,
    context: &Value,
    execution: Option<&Value>,
    require_input: bool,
) -> Result<()> {
    let ctx = Context::background(app)?;
    let j = required(
        &ctx.conn,
        "SELECT * FROM jobs WHERE id=?",
        [text(job, "id")],
        "任务不存在",
    )?;
    if j["product_id"] != job["product_id"]
        || j["card_id"] != job["card_id"]
        || j["attempt"] != job["attempt"]
        || text(&j, "state") != "processing"
        || (!j["lease"].is_null() && number(&j, "lease") <= now())
    {
        return Err(ApiError::new(409, "处理任务已失效"));
    }
    let b = required(
        &ctx.conn,
        "SELECT b.*,f.disabled,f.shop_id AS profile_shop_id,f.processor_id AS profile_processor_id,s.enabled,p.shop_id AS product_shop_id,p.config,c.state AS card_state FROM processor_card_bindings b JOIN processor_profiles f ON f.id=b.profile_id JOIN cards c ON c.id=b.card_id JOIN products p ON p.id=c.product_id JOIN shops s ON s.id=p.shop_id WHERE b.card_id=?",
        [text(&j, "card_id")],
        "处理器授权不存在",
    )?;
    let p = parse(text(&b, "config"))?;
    if !flag(&b, "enabled")
        || flag(&b, "disabled")
        || matches!(text(&b, "card_state"), "revoked" | "rejected")
        || b["product_id"] != j["product_id"]
        || ["shop_id", "profile_shop_id", "product_shop_id"]
            .iter()
            .any(|k| b[*k] != context["shop_id"])
        || b["profile_id"] != context["profile_id"]
        || b["revision"] != context["revision"]
        || text(&b, "processor_id") != processor_id
        || text(&b, "profile_processor_id") != processor_id
        || text(&p, "mode") != "script"
        || text(&p, "processor_id") != processor_id
    {
        return Err(ApiError::new(409, "商品处理器授权已失效"));
    }
    catalog::product(&ctx, text(&j, "product_id"))?;
    if let Some(execution) = execution {
        fulfillment::worker_authority(&ctx, &j, execution, require_input)?;
    }
    Ok(())
}
struct Process(Child);
impl Drop for Process {
    fn drop(&mut self) {
        if self.0.try_wait().ok().flatten().is_none() {
            unsafe {
                libc::kill(-(self.0.id() as i32), libc::SIGKILL);
            };
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
}
pub fn execute_script(
    app: Arc<App>,
    job: &Value,
    product: &Value,
    execution: Option<&Value>,
) -> Result<Value> {
    let ctx = Context::background(app.clone())?;
    let processor_id = text(product, "processor_id");
    let current = required(
        &ctx.conn,
        "SELECT * FROM jobs WHERE id=?",
        [text(job, "id")],
        "任务不存在",
    )?;
    let (configuration, shop_context, flow) =
        catalog::profiles::runtime_execution(&ctx, &current, processor_id)?;
    let workflow = workflow(&flow)?;
    authority(
        app.clone(),
        job,
        processor_id,
        &shop_context,
        execution,
        true,
    )?;
    let parameters =
        execution
            .map(|v| v["params"].clone())
            .unwrap_or(json_field(&current, "params", json!({})));
    let mut environment = json!({});
    for k in ["variables", "secrets"] {
        environment
            .as_object_mut()
            .unwrap()
            .extend(workflow[k].as_object().unwrap().clone());
    }
    let mut payload = json!({"job_id":job["id"],"product_id":job["product_id"],"attempt":job["attempt"],"params":parameters,"configuration":configuration,"variant":catalog::card_variant(&ctx,&current)?,"steps":json_field(&current,"progress_plan",json!([])),"completed_steps":json_field(&current,"completed_steps",json!([])),"shop_context":shop_context,"environment":environment});
    if let Some(entitlements) = row(
        &ctx.conn,
        "SELECT attributes,policy FROM card_entitlements WHERE card_id=?",
        [text(&current, "card_id")],
    )? {
        payload["card_attributes"] = json_field(&entitlements, "attributes", json!({}));
    }
    payload["instructions"] = catalog::instructions(&ctx, text(&current, "product_id"))?;
    let encoded = serde_json::to_vec(&payload)?;
    if encoded.len() > 200000 {
        return Err(ApiError::bad("处理器任务输入超过限制"));
    }
    let (python, runner, args) = sandbox(&app, processor_id)?;
    authority(
        app.clone(),
        job,
        processor_id,
        &shop_context,
        execution,
        true,
    )?;
    let mut command = Command::new(python);
    command
        .arg("-I")
        .arg(runner)
        .arg("--launch")
        .arg(workflow["runtime"].to_string())
        .arg("/usr/bin/bwrap")
        .args(args)
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("LANG", "C.UTF-8")
        .env("HOME", "/work")
        .env("TMPDIR", "/tmp")
        .current_dir("/")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .process_group(0);
    for (key, value) in environment.as_object().unwrap() {
        command.env(format!("EXTORE_WORKFLOW_{key}"), value.as_str().unwrap());
    }
    let mut process = Process(command.spawn()?);
    let mut stdin = process.0.stdin.take().ok_or_else(runtime_error)?;
    let stdout = process.0.stdout.take().ok_or_else(runtime_error)?;
    let sender = std::thread::spawn(move || {
        let result = stdin.write_all(&encoded);
        drop(stdin);
        result
    });
    let (tx, rx) = mpsc::sync_channel::<std::result::Result<Vec<u8>, ()>>(8);
    let reader = std::thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        loop {
            let mut line = Vec::new();
            match (&mut reader).take(131073).read_until(b'\n', &mut line) {
                Ok(0) => break,
                Ok(_) => {
                    if line.len() > 131072 {
                        let _ = tx.send(Err(()));
                        break;
                    }
                    if tx.send(Ok(line)).is_err() {
                        break;
                    }
                }
                Err(_) => {
                    let _ = tx.send(Err(()));
                    break;
                }
            }
        }
    });
    let start = Instant::now();
    let timeout = Duration::from_secs(integer(&workflow["runtime"], "timeout_seconds", 120) as u64);
    let mut result = None;
    let mut size = 0;
    let mut updates = 0;
    loop {
        authority(
            app.clone(),
            job,
            processor_id,
            &shop_context,
            execution,
            false,
        )?;
        if start.elapsed() > timeout {
            return Err(ApiError::new(408, "处理器运行超时"));
        }
        match rx.recv_timeout(Duration::from_millis(250)) {
            Ok(Ok(line)) => {
                size += line.len();
                if size > integer(&workflow["runtime"], "max_output_bytes", 1000000) as usize
                    || result.is_some()
                {
                    return Err(ApiError::bad("处理器输出超过限制或顺序无效"));
                }
                let mut value: Value = serde_json::from_slice(&line)?;
                if !value.is_object() {
                    return Err(ApiError::bad("处理器输出格式错误"));
                }
                value["attempt"] = job["attempt"].clone();
                if let Some(e) = execution {
                    value["flow_epoch"] = e["flow_epoch"].clone();
                    value["action_id"] = e["action_id"].clone();
                }
                match text(&value, "kind") {
                    "progress" => {
                        updates += 1;
                        if updates > 100 {
                            return Err(ApiError::bad("处理器进度更新超过限制"));
                        }
                        value["state"] = json!("processing");
                        transaction(app.clone(), |ctx| {
                            fulfillment::apply_update(ctx, text(job, "id"), &value)?;
                            Ok(())
                        })?;
                    }
                    "result" if matches!(text(&value, "state"), "succeeded" | "failed") => {
                        result = Some(value)
                    }
                    _ => return Err(ApiError::bad("处理器输出格式错误")),
                }
            }
            Ok(Err(())) => return Err(ApiError::bad("处理器输出无效")),
            Err(mpsc::RecvTimeoutError::Timeout) => continue,
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
        }
    }
    let _ = reader.join();
    while !sender.is_finished() {
        authority(
            app.clone(),
            job,
            processor_id,
            &shop_context,
            execution,
            false,
        )?;
        if start.elapsed() > timeout {
            return Err(ApiError::new(408, "处理器输入超时"));
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    sender.join().map_err(|_| runtime_error())??;
    while process.0.try_wait()?.is_none() {
        authority(
            app.clone(),
            job,
            processor_id,
            &shop_context,
            execution,
            false,
        )?;
        if start.elapsed() > timeout {
            return Err(ApiError::new(408, "处理器运行超时"));
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    if !process.0.wait()?.success() {
        return Err(runtime_error());
    }
    authority(app, job, processor_id, &shop_context, execution, false)?;
    result.ok_or_else(runtime_error)
}

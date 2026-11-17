use super::*;
use axum::http::{HeaderValue, Method};
use std::os::unix::fs::PermissionsExt;
struct Fixture {
    _dir: tempfile::TempDir,
    ctx: Context,
}
impl Fixture {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let app = Arc::new(App {
            data: dir.path().to_path_buf(),
            origin: "http://localhost:8000".into(),
            static_dir: dir.path().to_path_buf(),
        });
        let ctx = Context::background(app).unwrap();
        ctx.conn
            .execute_batch(include_str!("../schema.sql"))
            .unwrap();
        let master = [42_u8; 32];
        std::fs::write(ctx.app.data.join("master-secrets.key"), master).unwrap();
        std::fs::set_permissions(
            ctx.app.data.join("master-secrets.key"),
            std::fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        std::fs::write(
            ctx.app.data.join("issuance.key"),
            fernet::Fernet::generate_key(),
        )
        .unwrap();
        for sid in ["shop-a", "shop-b"] {
            ctx.conn
                .execute(
                    "INSERT INTO shops(id,name,enabled,created,verified) VALUES (?,?,1,?,1)",
                    params![sid, sid, now()],
                )
                .unwrap();
        }
        set_setting(&ctx.conn, "legacy_shop_id", "shop-a").unwrap();
        ctx.conn.execute("INSERT INTO sessions(digest,role,expires,created,id,shop_id,auth_at) VALUES (?,'admin',?,?,?,'shop-a',?)",params![digest("admin-a"),now()+3600.0,now(),id(),now()]).unwrap();
        let mut fixture = Self { _dir: dir, ctx };
        fixture
            .ctx
            .headers
            .insert("cookie", HeaderValue::from_static("extore_session=admin-a"));
        fixture
    }
    fn product(&self, pid: &str, extra: Value) {
        let mut p = json!({"name":"Native fulfillment","mode":"manual","delivery":"content","view_policy":"repeat","allow_retry":true,"max_attempts":3,"parameters":[{"key":"email","label":{"en":"Email"},"type":"email","required":true}],"outputs":[{"key":"content","label":{"en":"Delivery"},"type":"textarea","required":true}]});
        merge(&mut p, extra);
        let p = catalog::normalize(p).unwrap();
        self.ctx
            .conn
            .execute(
                "INSERT INTO products(id,config,created,shop_id) VALUES (?,?,?,'shop-a')",
                params![pid, p.to_string(), now()],
            )
            .unwrap();
    }
    fn issue(&self, pid: &str, extra: Value) -> (String, Value) {
        let mut body = json!({"count":1,"routed":false});
        merge(&mut body, extra);
        let result = catalog::issue_cards(&self.ctx, pid, &body).unwrap();
        let code = result["codes"][0].as_str().unwrap().to_owned();
        let c = required(
            &self.ctx.conn,
            "SELECT * FROM cards WHERE digest=?",
            [catalog::card_digest(&code)],
            "missing",
        )
        .unwrap();
        (code, c)
    }
    fn call(&mut self, path: &str, body: Value) -> Result<Value> {
        self.ctx.path = path.into();
        self.ctx.raw_target = path.into();
        self.ctx.method = Method::POST;
        self.ctx.body = body;
        self.ctx.raw_body = serde_json::to_vec(&self.ctx.body).unwrap();
        self.ctx.conn.execute_batch("BEGIN IMMEDIATE")?;
        match handle(&mut self.ctx) {
            Ok(Some(reply)) => {
                self.ctx.conn.execute_batch("COMMIT")?;
                let value: Value = serde_json::from_slice(&reply.body)?;
                if reply.status >= 400 {
                    Err(ApiError::new(reply.status, text(&value, "detail")))
                } else {
                    Ok(value)
                }
            }
            Ok(None) => {
                self.ctx.conn.execute_batch("ROLLBACK")?;
                Err(ApiError::new(404, "not handled"))
            }
            Err(e) => {
                self.ctx.conn.execute_batch("ROLLBACK")?;
                Err(e)
            }
        }
    }
    fn token(&mut self, code: &str) -> String {
        self.call("/api/exchange", json!({"code":code})).unwrap()["token"]
            .as_str()
            .unwrap()
            .to_owned()
    }
    fn queue(&mut self, pid: &str, j: &Value, action: &str, extra: Value) -> Result<Value> {
        let mut b =
            json!({"product_id":pid,"ids":[j["id"]],"action":action,"attempt":j["attempt"]});
        merge(&mut b, extra);
        self.call("/api/manage/batch", b)
    }
}
fn graph() -> Value {
    json!({"version":1,"entry":"input","nodes":[
        {"id":"input","kind":"input","fields":[{"key":"email","label":{"en":"Email"},"type":"email"},{"key":"otp","label":{"en":"Code"},"sensitive":true,"sensitive_ttl_seconds":120}],"next":"process"},
        {"id":"process","kind":"process","inputs":{"email":{"node":"input","field":"email"},"otp":{"node":"input","field":"otp"}},"outputs":[{"key":"content","label":{"en":"Delivery"},"type":"textarea"}],"next":"end","failure_next":"failed","timeout_next":"failed","timeout_seconds":60},
        {"id":"end","kind":"end","state":"succeeded","result":{"content":{"node":"process","field":"content"}}},
        {"id":"failed","kind":"end","state":"failed"}
    ]})
}
fn flow_action(token: &str, j: &Value, values: Value) -> Value {
    json!({"token":token,"flow_epoch":j["task_flow"]["flow_epoch"],"expected_revision":j["task_flow"]["revision"],"values":values})
}

#[test]
fn manual_receipts_revisions_replay_and_destroy() {
    let mut f = Fixture::new();
    f.product("manual",json!({"revision_policy":{"attribute_key":"revisions","label":{"en":"Revisions"}},"variants":[{"id":"default","name":"Standard","attributes":{"revisions":2},"enabled":true}]}));
    let (code, c) = f.issue("manual", json!({}));
    let token = f.token(&code);
    let j = f
        .call(
            "/api/redeem",
            json!({"token":token,"params":{"email":"buyer@example.com"}}),
        )
        .unwrap();
    f.queue("manual", &j, "claim", json!({})).unwrap();
    f.queue(
        "manual",
        &j,
        "succeed",
        json!({"output":{"content":"First delivery"}}),
    )
    .unwrap();
    let initial = job(&f.ctx, text(&j, "id")).unwrap();
    let repeat = apply_update(
        &f.ctx,
        text(&j, "id"),
        &json!({"attempt":1,"state":"succeeded","output":{"content":"must not overwrite"}}),
    )
    .unwrap();
    assert_eq!(repeat["content"], initial["content"]);
    let receipt = f.call("/api/receipt", json!({"token":token})).unwrap();
    assert_eq!(receipt["job"]["entitlements"]["remaining"], 2);
    assert_eq!(receipt["job"]["deliveries"].as_array().unwrap().len(), 1);
    let request = json!({"token":token,"request_id":id(),"expected_revision":0,"message":"Please revise the wording"});
    let revised = f.call("/api/receipt/revisions", request.clone()).unwrap();
    assert_eq!(revised["attempt"], 2);
    assert_eq!(revised["state"], "queued");
    let duplicate = f.call("/api/receipt/revisions", request.clone()).unwrap();
    assert_eq!(duplicate["attempt"], 2);
    let mut changed = request;
    changed["message"] = json!("Different text");
    assert_eq!(
        f.call("/api/receipt/revisions", changed)
            .unwrap_err()
            .status,
        409
    );
    assert_eq!(
        f.call("/api/receipt/reveal", json!({"token":token,"revision":0}))
            .unwrap()["content"],
        "First delivery"
    );
    assert_eq!(
        f.queue("manual", &j, "claim", json!({}))
            .unwrap_err()
            .status,
        409
    );
    f.queue("manual", &revised, "claim", json!({})).unwrap();
    // Archiving a product must not invalidate an already accepted paid job.
    f.ctx
        .conn
        .execute(
            "UPDATE products SET config=json_set(config,'$.deleted',1) WHERE id='manual'",
            [],
        )
        .unwrap();
    f.queue(
        "manual",
        &revised,
        "succeed",
        json!({"output":{"content":"Second delivery"}}),
    )
    .unwrap();
    assert_eq!(
        f.call("/api/receipt/reveal", json!({"token":token}))
            .unwrap()["content"],
        "Second delivery"
    );
    let receipt = f.call("/api/receipt", json!({"token":token})).unwrap();
    assert_eq!(receipt["job"]["entitlements"]["used"], 1);
    assert_eq!(receipt["job"]["deliveries"].as_array().unwrap().len(), 2);
    assert_eq!(
        text(&card(&f.ctx, text(&c, "id")).unwrap(), "state"),
        "used"
    );
    f.call("/api/receipt/destroy", json!({"token":token}))
        .unwrap();
    assert_eq!(
        f.call("/api/receipt/reveal", json!({"token":token,"revision":0}))
            .unwrap_err()
            .status,
        409
    );
    let j = job(&f.ctx, text(&j, "id")).unwrap();
    assert_eq!(j["state"], "destroyed");
    assert_eq!(json_field(&j, "params", Value::Null), json!({}));
    let version = required(
        &f.ctx.conn,
        "SELECT * FROM job_delivery_versions WHERE job_id=?",
        [text(&j, "id")],
        "missing",
    )
    .unwrap();
    assert!(version["content"].is_null() && version["result_json"].is_null());
    let events = rows(
        &f.ctx.conn,
        "SELECT payload FROM events WHERE job_id=?",
        [text(&j, "id")],
    )
    .unwrap();
    assert!(
        !events
            .iter()
            .any(|e| text(e, "payload").contains("buyer@example.com"))
    );
}

#[test]
fn stock_once_retains_first_receipt_and_erases_assignment() {
    let mut f = Fixture::new();
    f.product(
        "stock",
        json!({"mode":"stock","parameters":[],"view_policy":"once"}),
    );
    let (code, c) = f.issue("stock", json!({"text":"One private delivery"}));
    let token = f.token(&code);
    let j = f
        .call("/api/redeem", json!({"token":token,"params":{}}))
        .unwrap();
    assert_eq!(j["state"], "succeeded");
    let assignment = required(
        &f.ctx.conn,
        "SELECT * FROM text_card_payloads WHERE card_id=?",
        [text(&c, "id")],
        "missing",
    )
    .unwrap();
    assert!(assignment["ciphertext"].is_null());
    assert_eq!(
        f.call("/api/exchange", json!({"code":code}))
            .unwrap_err()
            .status,
        410
    );
    assert_eq!(
        f.call("/api/receipt/reveal", json!({"token":token}))
            .unwrap()["content"],
        "One private delivery"
    );
    assert_eq!(
        f.call("/api/receipt/reveal", json!({"token":token}))
            .unwrap_err()
            .status,
        410
    );
    let saved = job(&f.ctx, text(&j, "id")).unwrap();
    assert!(saved["content"].is_null() && saved["result_json"].is_null());
    assert_eq!(
        required(
            &f.ctx.conn,
            "SELECT size FROM text_card_payloads WHERE card_id=?",
            [text(&c, "id")],
            "missing"
        )
        .unwrap()["size"],
        0
    );
}

#[test]
fn partial_batch_isolates_validation_and_duplicate_cards() {
    let mut f = Fixture::new();
    f.product("first", json!({}));
    f.product("second", json!({}));
    let (a, ca) = f.issue("first", json!({}));
    let (b, cb) = f.issue("second", json!({}));
    let response = f
        .call(
            "/api/batch/exchange",
            json!({"code":format!("{a},invalid-card,{a},{b}")}),
        )
        .unwrap();
    assert_eq!(response["summary"]["accepted"], 2);
    assert_eq!(response["summary"]["invalid"], 1);
    assert_eq!(response["summary"]["duplicate"], 1);
    let value = &response["token"];
    let result=f.call("/api/batch/redeem",json!({"token":value,"items":[{"card_id":ca["id"],"params":{"email":"bad"}},{"card_id":cb["id"],"params":{"email":"valid@example.com"}}]})).unwrap();
    assert_eq!(result["submission_summary"]["succeeded"], 1);
    assert_eq!(result["submission_summary"]["failed"], 1);
    assert!(
        row(
            &f.ctx.conn,
            "SELECT * FROM jobs WHERE card_id=?",
            [text(&ca, "id")]
        )
        .unwrap()
        .is_none()
    );
    assert!(
        row(
            &f.ctx.conn,
            "SELECT * FROM jobs WHERE card_id=?",
            [text(&cb, "id")]
        )
        .unwrap()
        .is_some()
    );
    let (other, _) = f.issue("first", json!({}));
    let single = f.token(&other);
    assert_eq!(
        grant(&f.ctx, &single, Some(text(&cb, "id")))
            .unwrap_err()
            .status,
        404
    );
}

#[test]
fn queue_scope_and_attempt_fences_reject_other_shop() {
    let mut f = Fixture::new();
    f.product("owned", json!({}));
    let (code, _) = f.issue("owned", json!({}));
    let token = f.token(&code);
    let j = f
        .call(
            "/api/redeem",
            json!({"token":token,"params":{"email":"a@example.com"}}),
        )
        .unwrap();
    f.ctx
        .conn
        .execute("UPDATE products SET shop_id='shop-b' WHERE id='owned'", [])
        .unwrap();
    assert_eq!(
        f.queue("owned", &j, "claim", json!({})).unwrap_err().status,
        403
    );
    assert_eq!(job(&f.ctx, text(&j, "id")).unwrap()["state"], "queued");
}

#[test]
fn issued_flow_snapshot_sensitive_lifetime_and_final_output() {
    let mut f = Fixture::new();
    f.product("flow", json!({"parameters":[],"task_flow":graph()}));
    let (code, c) = f.issue("flow", json!({}));
    let snapshot = flow_card_snapshot(&f.ctx, &c).unwrap().unwrap();
    f.ctx
        .conn
        .execute(
            "UPDATE products SET config=json_set(config,'$.task_flow',NULL) WHERE id='flow'",
            [],
        )
        .unwrap();
    assert_eq!(
        flow_card_snapshot(&f.ctx, &c).unwrap().unwrap()["definition_hash"],
        snapshot["definition_hash"]
    );
    let token = f.token(&code);
    let started = f
        .call(
            "/api/task-flow/start",
            json!({"token":token,"flow_epoch":0,"expected_revision":0}),
        )
        .unwrap();
    assert_eq!(started["task_flow"]["phase"], "input");
    let answer = flow_action(
        &token,
        &started,
        json!({"email":"buyer@example.com","otp":"654321"}),
    );
    let queued = f.call("/api/task-flow/answer", answer.clone()).unwrap();
    assert_eq!(queued["state"], "queued");
    assert!(!queued.to_string().contains("654321"));
    let duplicate = f.call("/api/task-flow/answer", answer).unwrap();
    assert_eq!(
        duplicate["task_flow"]["flow_epoch"],
        queued["task_flow"]["flow_epoch"]
    );
    let j = job(&f.ctx, text(&queued, "id")).unwrap();
    let execution = flow::execution(&f.ctx, &j, false).unwrap().unwrap();
    f.queue(
        "flow",
        &queued,
        "claim",
        json!({"flow_epoch":execution["flow_epoch"],"action_id":execution["action_id"]}),
    )
    .unwrap();
    let current = job(&f.ctx, text(&queued, "id")).unwrap();
    let consumed = flow::execution(&f.ctx, &current, true).unwrap().unwrap();
    assert_eq!(consumed["params"]["otp"], "654321");
    // The stored job/events contain no raw OTP. Only the tenant-scoped step vault does.
    assert_eq!(json_field(&current, "params", Value::Null), json!({}));
    let input = required(
        &f.ctx.conn,
        "SELECT * FROM task_flow_steps WHERE job_id=? AND kind='input'",
        [text(&current, "id")],
        "missing",
    )
    .unwrap();
    assert!(!text(&input, "payload_ciphertext").contains("654321"));
    let resource = format!(
        "{}:{}",
        text(&current, "id"),
        integer(&input, "flow_epoch", 0)
    );
    let mut payload = auth::secret::open_secret(
        &f.ctx.app,
        Some("shop-a"),
        "task-flow-step",
        &resource,
        text(&input, "payload_ciphertext"),
    )
    .unwrap();
    payload["sensitive_until"]["otp"] = json!(now() - 1.0);
    let ciphertext = auth::secret::store_secret(
        &f.ctx.app,
        Some("shop-a"),
        "task-flow-step",
        &resource,
        &payload,
    )
    .unwrap();
    f.ctx
        .conn
        .execute(
            "UPDATE task_flow_steps SET payload_ciphertext=? WHERE job_id=? AND flow_epoch=?",
            params![
                ciphertext,
                text(&current, "id"),
                integer(&input, "flow_epoch", 0)
            ],
        )
        .unwrap();
    f.ctx
        .conn
        .execute(
            "UPDATE task_flow_runs SET input_expires_at=? WHERE job_id=?",
            params![now() - 1.0, text(&current, "id")],
        )
        .unwrap();
    worker_once(f.ctx.app.clone()).unwrap();
    let live = job(&f.ctx, text(&current, "id")).unwrap();
    assert_eq!(live["state"], "processing");
    assert!(flow::execution(&f.ctx, &live, false).unwrap().is_none());
    // An already started action may complete after its OTP has been erased.
    f.queue("flow",&queued,"succeed",json!({"flow_epoch":execution["flow_epoch"],"action_id":execution["action_id"],"output":{"content":"Finished"}})).unwrap();
    let receipt = f
        .call("/api/receipt/reveal", json!({"token":token}))
        .unwrap();
    assert_eq!(receipt["content"], "Finished");
    let step = required(
        &f.ctx.conn,
        "SELECT payload_ciphertext FROM task_flow_steps WHERE job_id=? AND kind='input'",
        [text(&live, "id")],
        "missing",
    )
    .unwrap();
    let payload = auth::secret::open_secret(
        &f.ctx.app,
        Some("shop-a"),
        "task-flow-step",
        &resource,
        text(&step, "payload_ciphertext"),
    )
    .unwrap();
    assert!(payload["input"].get("otp").is_none());
    let done = job(&f.ctx, text(&live, "id")).unwrap();
    assert_eq!(apply_update(&f.ctx,text(&done,"id"),&json!({"state":"succeeded","attempt":1,"flow_epoch":execution["flow_epoch"],"action_id":execution["action_id"],"output":{"content":"overwrite"}})).unwrap_err().status,409);
}

#[test]
fn flow_timeout_checks_identity_before_committing_terminal_state() {
    let mut f = Fixture::new();
    f.product("flow", json!({"parameters":[],"task_flow":graph()}));
    let (code, _) = f.issue("flow", json!({}));
    let token = f.token(&code);
    let started = f
        .call(
            "/api/task-flow/start",
            json!({"token":token,"flow_epoch":0,"expected_revision":0}),
        )
        .unwrap();
    let queued = f
        .call(
            "/api/task-flow/answer",
            flow_action(
                &token,
                &started,
                json!({"email":"buyer@example.com","otp":"123456"}),
            ),
        )
        .unwrap();
    let j = job(&f.ctx, text(&queued, "id")).unwrap();
    let mut scope = flow::execution(&f.ctx, &j, false).unwrap().unwrap();
    f.ctx
        .conn
        .execute(
            "UPDATE task_flow_runs SET deadline=? WHERE job_id=?",
            params![now() - 1.0, text(&j, "id")],
        )
        .unwrap();
    let original = scope["flow_epoch"].clone();
    scope["flow_epoch"] = json!(999);
    assert_eq!(
        flow::expire_current(&f.ctx, &j, &scope).unwrap_err().status,
        409
    );
    assert_eq!(job(&f.ctx, text(&j, "id")).unwrap()["state"], "queued");
    scope["flow_epoch"] = original;
    assert!(flow::expire_current(&f.ctx, &j, &scope).unwrap());
    let ended = job(&f.ctx, text(&j, "id")).unwrap();
    assert_eq!(ended["state"], "failed");
    assert!(!flag(&ended, "retryable"));
    assert!(
        row(
            &f.ctx.conn,
            "SELECT 1 FROM task_flow_steps WHERE job_id=? AND kind='process' AND state='timed_out'",
            [text(&j, "id")]
        )
        .unwrap()
        .is_some()
    );
}

#[test]
fn graph_validation_rejects_secret_projection_and_cycles() {
    let mut p = catalog::defaults();
    p["name"] = json!("Graph");
    let good = graph();
    validate_flow(&good, &p).unwrap();
    let mut bad = good.clone();
    bad["nodes"][2]["result"]["content"] = json!({"node":"input","field":"otp"});
    assert!(validate_flow(&bad, &p).is_err());
    let cyclic = json!({"version":1,"entry":"view","nodes":[{"id":"view","kind":"display","show_from":{"x":{"node":"view","field":"x"}},"next":"end"},{"id":"end","kind":"end","state":"failed"}]});
    assert!(validate_flow(&cyclic, &p).is_err());
    let mut bad = good.clone();
    bad["nodes"][0]["timeout_seconds"] = json!(true);
    bad["nodes"][0]["timeout_next"] = json!("failed");
    assert!(validate_flow(&bad, &p).is_err());
    assert!(
        validate_revision_policy(&json!({"attribute_key":" ","label":{"en":"Revisions"}})).is_err()
    );
}

#[test]
fn old_cards_do_not_inherit_a_new_flow_and_tampered_binding_is_rejected() {
    let f = Fixture::new();
    f.product("old", json!({"parameters":[]}));
    let (_, old) = f.issue("old", json!({}));
    let mut p = catalog::product(&f.ctx, "old").unwrap();
    p["task_flow"] = graph();
    f.ctx
        .conn
        .execute(
            "UPDATE products SET config=? WHERE id='old'",
            [p.to_string()],
        )
        .unwrap();
    assert!(catalog::card_product(&f.ctx, &old).unwrap()["task_flow"].is_null());
    let (_, new) = f.issue("old", json!({}));
    assert!(flow_card_snapshot(&f.ctx, &new).unwrap().is_some());
    f.ctx
        .conn
        .execute(
            "UPDATE card_task_flows SET definition_hash=? WHERE card_id=?",
            params!["f".repeat(64), text(&new, "id")],
        )
        .unwrap();
    assert_eq!(flow_card_snapshot(&f.ctx, &new).unwrap_err().status, 409);
}

#[test]
fn rejected_exchange_attempts_count_outside_business_transaction() {
    let mut f = Fixture::new();
    f.ctx.path = "/api/exchange".into();
    f.ctx.method = Method::POST;
    f.ctx.peer = "127.0.0.8".into();
    for _ in 0..20 {
        admit(&f.ctx).unwrap();
        assert!(
            f.call("/api/exchange", json!({"code":"invalid-card"}))
                .is_err()
        );
    }
    assert_eq!(admit(&f.ctx).unwrap_err().status, 429);
    f.ctx.peer = "127.0.0.9".into();
    admit(&f.ctx).unwrap();
}

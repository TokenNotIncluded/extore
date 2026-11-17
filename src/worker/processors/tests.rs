use super::*;
use tempfile::TempDir;

fn fixture() -> (TempDir, Context) {
    let dir = tempfile::tempdir().unwrap();
    let app = Arc::new(App {
        data: dir.path().into(),
        origin: "https://example.test".into(),
        static_dir: dir.path().into(),
    });
    let ctx = Context::background(app).unwrap();
    ctx.conn
        .execute_batch(include_str!("../../schema.sql"))
        .unwrap();
    ctx.conn
        .execute(
            "INSERT INTO shops(id,name,created) VALUES ('shop','Shop',1)",
            [],
        )
        .unwrap();
    ctx.conn
        .execute(
            "INSERT INTO products(id,config,created,shop_id) VALUES ('product','{}',1,'shop')",
            [],
        )
        .unwrap();
    ctx.conn.execute("INSERT INTO cards(id,digest,product_id,state,created) VALUES ('card','fixture-card-digest','product','redeemed',1)", []).unwrap();
    ctx.conn.execute("INSERT INTO jobs(id,card_id,product_id,state,params,attempt,created,updated,progress_plan,revision_round,revision_start_attempt,revision_message) VALUES ('job','card','product','processing','{}',4,1,30,'[]',2,4,'Please refine the title')", []).unwrap();
    ctx.conn.execute(
        "INSERT INTO card_entitlements(card_id,attributes,policy,created) VALUES ('card',?,?,1)",
        params![json!({"edits":3,"format":"DOCX"}).to_string(),json!({"attribute_key":"edits","label":{"en":"Included revisions"}}).to_string()],
    ).unwrap();
    for revision in [1_i64, 2] {
        ctx.conn.execute(
            "INSERT INTO job_revision_requests(job_id,request_id,request_digest,revision,message,created) VALUES ('job',?,?,?,?,?)",
            params![format!("request-{revision}"),format!("digest-{revision}"),revision,"Review request",revision as f64*10.0],
        ).unwrap();
    }
    for (revision, attempt, created) in [(0_i64, 1_i64, 10.0), (1, 3, 20.0)] {
        ctx.conn.execute(
            "INSERT INTO job_delivery_versions(job_id,revision,attempt,content,result_json,created) VALUES ('job',?,?,?,?,?)",
            params![revision,attempt,"PRIVATE_ARCHIVED_BODY",r#"{"content":"PRIVATE_ARCHIVED_JSON"}"#,created],
        ).unwrap();
    }
    (dir, ctx)
}

#[test]
fn processor_revision_context_contains_authorized_metadata_without_history_bodies() {
    let (_dir, ctx) = fixture();
    let job = fulfillment::job(&ctx, "job").unwrap();
    let mut payload = json!({"job_id":"job","attempt":4,"params":{}});
    add_delivery_context(&ctx, &job, &mut payload).unwrap();
    assert_eq!(
        payload["revision"],
        json!({"current":2,"message":"Please refine the title","is_revision":true})
    );
    assert_eq!(
        payload["card_attributes"],
        json!({"edits":3,"format":"DOCX"})
    );
    assert_eq!(payload["entitlements"]["total"], 3);
    assert_eq!(payload["entitlements"]["used"], 2);
    assert_eq!(payload["entitlements"]["remaining"], 1);
    assert_eq!(payload["entitlements"]["reason"], "in_progress");
    assert_eq!(
        payload["last_delivery"],
        json!({"revision":1,"attempt":3,"created":20.0})
    );
    assert!(payload.get("deliveries").is_none());
    assert!(!payload.to_string().contains("PRIVATE_ARCHIVED"));
}

#[test]
fn processor_initial_context_keeps_null_entitlements_and_no_previous_delivery() {
    let (_dir, ctx) = fixture();
    ctx.conn
        .execute("DELETE FROM job_revision_requests", [])
        .unwrap();
    ctx.conn
        .execute("DELETE FROM job_delivery_versions", [])
        .unwrap();
    ctx.conn.execute("UPDATE jobs SET revision_round=0,revision_message='',revision_start_attempt=1,attempt=1", []).unwrap();
    ctx.conn
        .execute("UPDATE card_entitlements SET policy=NULL", [])
        .unwrap();
    let job = fulfillment::job(&ctx, "job").unwrap();
    let mut payload = json!({});
    add_delivery_context(&ctx, &job, &mut payload).unwrap();
    assert_eq!(
        payload["revision"],
        json!({"current":0,"message":"","is_revision":false})
    );
    assert!(payload["entitlements"].is_null());
    assert!(payload["last_delivery"].is_null());
    assert!(payload.get("deliveries").is_none());
    assert_eq!(payload["card_attributes"]["format"], "DOCX");
}

#[test]
fn processor_progress_plan_rejects_replacement_and_invalid_steps() {
    let (_dir, ctx) = fixture();
    let job = fulfillment::job(&ctx, "job").unwrap();
    for invalid in [
        json!([]),
        json!([{"id":"Bad ID","label":{"en":"Name"}}]),
        json!([{"id":"one","label":{"en":"Name"},"extra":true}]),
        json!([{"id":"one","label":{"en":" "}}]),
        json!([{"id":"one","label":{"en":"One"}},{"id":"one","label":{"en":"Duplicate"}}]),
    ] {
        assert_eq!(
            bootstrap_progress(&ctx, &job, &invalid).unwrap_err().status,
            400
        );
    }
    let steps = json!([{"id":"review","label":{"en":"Review","zh-CN":"复核"}}]);
    bootstrap_progress(&ctx, &job, &steps).unwrap();
    assert_eq!(
        bootstrap_progress(&ctx, &job, &steps).unwrap_err().status,
        409
    );
    let (frozen, completed) = fulfillment::progress(&ctx, &job).unwrap();
    assert_eq!(json!(frozen), steps);
    assert!(completed.is_empty());
}

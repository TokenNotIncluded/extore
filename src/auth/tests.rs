use super::*;
use axum::http::{HeaderMap, HeaderValue, Method};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use ed25519_dalek::{Signer, SigningKey};
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, sync::Arc};
use tempfile::TempDir;

struct Fixture {
    _dir: TempDir,
    ctx: Context,
}
impl Fixture {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let app = Arc::new(App {
            data: dir.path().into(),
            origin: "https://example.test".into(),
            static_dir: dir.path().into(),
        });
        let conn = app.db().unwrap();
        conn.execute_batch(include_str!("../schema.sql")).unwrap();
        conn.execute_batch("PRAGMA journal_mode=WAL").unwrap();
        let master = (0..32u8).collect::<Vec<_>>();
        std::fs::write(dir.path().join("master-secrets.key"), &master).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(
                dir.path().join("master-secrets.key"),
                std::fs::Permissions::from_mode(0o600),
            )
            .unwrap();
        }
        let mut material = b"extore-secret-key-v1\0".to_vec();
        material.extend(master);
        conn.execute(
            "INSERT INTO secret_store_state VALUES (1,?)",
            [format!("{:x}", Sha256::digest(material))],
        )
        .unwrap();
        for sid in ["shop-a", "shop-b"] {
            conn.execute("INSERT INTO shops(id,name,email,created,verified,password_hash) VALUES (?,?,?,?,1,?)",params![sid,sid,format!("{sid}@example.test"),now(),"fixture-password-hash"]).unwrap();
        }
        set_setting(&conn, "legacy_shop_id", "shop-a").unwrap();
        conn.execute(
            "INSERT INTO products(id,config,created,shop_id) VALUES (?,?,?,?)",
            params![
                "product-a",
                json!({"id":"product-a","name":"测试商品","mode":"manual"}).to_string(),
                now(),
                "shop-a"
            ],
        )
        .unwrap();
        conn.execute("INSERT INTO staff(id,digest,product_id,name,expires,permissions,max_uses,max_cli_uses,created) VALUES (?,?,?,?,?,?,?,?,?)",params!["link-a",digest("fixture-link-token"),"product-a","Fixture",now()+3600.0,json!(["queue.view","queue.process"]).to_string(),2,1,now()]).unwrap();
        let ctx = Context {
            app,
            conn,
            method: Method::GET,
            path: "".into(),
            query: BTreeMap::new(),
            headers: HeaderMap::new(),
            body: Value::Null,
            raw_body: Vec::new(),
            raw_target: "".into(),
            peer: "127.0.0.1".into(),
            response_headers: HeaderMap::new(),
        };
        Self { _dir: dir, ctx }
    }
    fn prepare(&mut self, method: &str, target: &str, body: Value, cookie: &str, bearer: &str) {
        self.ctx.method = Method::from_bytes(method.as_bytes()).unwrap();
        self.ctx.path = target.split('?').next().unwrap().into();
        self.ctx.raw_target = target.into();
        self.ctx.body = body;
        self.ctx.raw_body = serde_json::to_vec(&self.ctx.body).unwrap();
        self.ctx.headers = HeaderMap::new();
        self.ctx.response_headers = HeaderMap::new();
        self.ctx.query = BTreeMap::new();
        if let Some((_, q)) = target.split_once('?') {
            self.ctx.query = url::form_urlencoded::parse(q.as_bytes())
                .into_owned()
                .collect();
        }
        if !cookie.is_empty() {
            self.ctx
                .headers
                .insert("cookie", HeaderValue::from_str(cookie).unwrap());
        }
        if !bearer.is_empty() {
            self.ctx
                .headers
                .insert("authorization", HeaderValue::from_str(bearer).unwrap());
        }
    }
    fn dispatch(&mut self) -> Result<Value> {
        self.ctx.conn.execute_batch("BEGIN IMMEDIATE")?;
        let result = (|| {
            cli::verify_action(&self.ctx)?;
            let reply =
                handle(&mut self.ctx)?.ok_or_else(|| ApiError::new(404, "No auth handler"))?;
            Ok(serde_json::from_slice(&reply.body)?)
        })();
        self.ctx
            .conn
            .execute_batch(if result.is_ok() { "COMMIT" } else { "ROLLBACK" })?;
        result
    }
    fn call(
        &mut self,
        method: &str,
        path: &str,
        body: Value,
        cookie: &str,
        bearer: &str,
    ) -> Result<Value> {
        self.prepare(method, path, body, cookie, bearer);
        self.dispatch()
    }
    fn cookie(&self, name: &str) -> String {
        self.ctx
            .response_headers
            .get_all("set-cookie")
            .iter()
            .filter_map(|v| v.to_str().ok())
            .find(|v| v.starts_with(&format!("{name}=")))
            .unwrap()
            .split(';')
            .next()
            .unwrap()
            .to_owned()
    }
    fn browser(&mut self, sid: Option<&str>, role: &str, staff: Option<&str>) -> String {
        self.ctx.response_headers = HeaderMap::new();
        self.ctx.headers = HeaderMap::new();
        self.ctx.conn.execute_batch("BEGIN IMMEDIATE").unwrap();
        create_session(&mut self.ctx, role, sid, staff, "fixture").unwrap();
        self.ctx.conn.execute_batch("COMMIT").unwrap();
        self.cookie("extore_session")
    }
}
fn signed(key: &SigningKey, proof: &str) -> String {
    URL_SAFE_NO_PAD.encode(key.sign(proof.as_bytes()).to_bytes())
}
fn staff_bind(f: &mut Fixture, key: &SigningKey) -> Value {
    let public = URL_SAFE_NO_PAD.encode(key.verifying_key().to_bytes());
    let proof = format!("extore-cli-bind-v1\nhttps://example.test\nfixture-link-token\n{public}");
    f.call("POST","/api/cli/authorize",json!({"token":"fixture-link-token","public_key":public,"client_name":"Fixture CLI","signature":signed(key,&proof)}),"","").unwrap()
}
fn staff_login(f: &mut Fixture, key: &SigningKey, did: &str) -> Value {
    let ch = f
        .call(
            "POST",
            "/api/cli/challenge",
            json!({"device_id":did}),
            "",
            "",
        )
        .unwrap();
    let proof = format!(
        "extore-cli-session-v1\nhttps://example.test\n{did}\n{}\n{}",
        text(&ch, "challenge_id"),
        text(&ch, "challenge")
    );
    f.call(
        "POST",
        "/api/cli/session",
        json!({"device_id":did,"challenge_id":ch["challenge_id"],"signature":signed(key,&proof)}),
        "",
        "",
    )
    .unwrap()
}
#[test]
fn legacy_aesgcm_fixture_and_tenant_binding() {
    let f = Fixture::new();
    let encoded =
        "v1.AAECAwQFBgcICQoL_e3UbX5eqbQH21vXRtrK2vc4R1I7H_b2DFLx6V56l-xbkcqEuLLaF5X7lm-QFBwavQVjDA";
    assert_eq!(
        secret::open_secret(&f.ctx.app, Some("测试店"), "totp", "fixture-shop", encoded).unwrap(),
        json!({"message":"兼容🙂","safe":true})
    );
    assert!(
        secret::open_secret(&f.ctx.app, Some("other"), "totp", "fixture-shop", encoded).is_err()
    );
    assert!(
        secret::open_secret(
            &f.ctx.app,
            Some("测试店"),
            "totp-pending",
            "fixture-shop",
            encoded
        )
        .is_err()
    );
    let cipher = secret::store_secret(
        &f.ctx.app,
        Some("shop-a"),
        "mail",
        "message",
        &json!({"body":"value"}),
    )
    .unwrap();
    assert_eq!(
        secret::open_secret(&f.ctx.app, Some("shop-a"), "mail", "message", &cipher).unwrap(),
        json!({"body":"value"})
    );
}
#[test]
fn historical_recovery_digest_matches_python() {
    assert_eq!(
        account::backup_digest("01234567-89ABCDEF-01234567-89ABCDEF").unwrap(),
        "9021b1297253fb1299e994cc59d513046fb3d7ff154a87b3b6324ec8dd0d4ba0"
    );
}
#[test]
fn staff_key_binding_is_idempotent_and_cookie_is_not_bearer() {
    let mut f = Fixture::new();
    let key = SigningKey::from_bytes(&[9; 32]);
    let grant = staff_bind(&mut f, &key);
    let again = staff_bind(&mut f, &key);
    assert_eq!(grant["device_id"], again["device_id"]);
    assert_eq!(again["already_authorized"], true);
    assert_eq!(
        integer(
            &row(
                &f.ctx.conn,
                "SELECT cli_uses FROM staff WHERE id='link-a'",
                []
            )
            .unwrap()
            .unwrap(),
            "cli_uses",
            0
        ),
        1
    );
    let auth = staff_login(&mut f, &key, text(&grant, "device_id"));
    let bearer = format!("Bearer {}", text(&auth, "access_token"));
    let status = f
        .call("GET", "/api/cli/status", json!({}), "", &bearer)
        .unwrap();
    assert_eq!(status["role"], "staff");
    assert_eq!(status["shop_id"], "shop-a");
    let cookie = f.browser(Some("shop-a"), "admin", None);
    assert_eq!(
        f.call("GET", "/api/cli/status", json!({}), &cookie, &bearer)
            .unwrap_err()
            .status,
        401
    );
    assert_eq!(
        f.call(
            "GET",
            "/api/cli/status",
            json!({}),
            &format!("extore_session={}", text(&auth, "access_token")),
            ""
        )
        .unwrap_err()
        .status,
        401
    );
    assert_eq!(
        f.call("GET", "/api/admin/sessions", json!({}), "", &bearer)
            .unwrap_err()
            .status,
        401
    );
}
#[test]
fn signed_session_challenge_cannot_be_replayed() {
    let mut f = Fixture::new();
    let key = SigningKey::from_bytes(&[7; 32]);
    let grant = staff_bind(&mut f, &key);
    let did = text(&grant, "device_id");
    let ch = f
        .call(
            "POST",
            "/api/cli/challenge",
            json!({"device_id":did}),
            "",
            "",
        )
        .unwrap();
    let proof = format!(
        "extore-cli-session-v1\nhttps://example.test\n{did}\n{}\n{}",
        text(&ch, "challenge_id"),
        text(&ch, "challenge")
    );
    let body =
        json!({"device_id":did,"challenge_id":ch["challenge_id"],"signature":signed(&key,&proof)});
    f.call("POST", "/api/cli/session", body.clone(), "", "")
        .unwrap();
    assert_eq!(
        f.call("POST", "/api/cli/session", body, "", "")
            .unwrap_err()
            .status,
        401
    );
}
#[test]
fn revoked_ancestor_invalidates_existing_device_and_session() {
    let mut f = Fixture::new();
    f.ctx
        .conn
        .execute(
            "INSERT INTO staff(id,digest,product_id,name,expires,permissions) VALUES (?,?,?,?,?,?)",
            params![
                "parent",
                digest("parent"),
                "product-a",
                "Parent",
                now() + 3600.0,
                json!(["queue.view", "queue.process"]).to_string()
            ],
        )
        .unwrap();
    f.ctx
        .conn
        .execute("UPDATE staff SET parent_id='parent' WHERE id='link-a'", [])
        .unwrap();
    let key = SigningKey::from_bytes(&[3; 32]);
    let grant = staff_bind(&mut f, &key);
    let auth = staff_login(&mut f, &key, text(&grant, "device_id"));
    f.ctx
        .conn
        .execute("UPDATE staff SET revoked=1 WHERE id='parent'", [])
        .unwrap();
    assert_eq!(
        f.call(
            "GET",
            "/api/cli/status",
            json!({}),
            "",
            &format!("Bearer {}", text(&auth, "access_token"))
        )
        .unwrap_err()
        .status,
        401
    );
    assert_eq!(
        f.call(
            "POST",
            "/api/cli/challenge",
            json!({"device_id":grant["device_id"]}),
            "",
            ""
        )
        .unwrap_err()
        .status,
        401
    );
}
#[test]
fn browser_device_review_and_claim_preserve_scope() {
    let mut f = Fixture::new();
    let browser = f.browser(Some("shop-a"), "admin", None);
    let key = SigningKey::from_bytes(&[4; 32]);
    let public = URL_SAFE_NO_PAD.encode(key.verifying_key().to_bytes());
    let mut nonce = [4u8; 32];
    nonce[..8].copy_from_slice(&(now() as u64).to_be_bytes());
    let nonce = URL_SAFE_NO_PAD.encode(nonce);
    let proof = format!(
        "extore-cli-device-request-v1\nhttps://example.test\n{public}\nFixture CLI\n{nonce}\nproduct-a\nagent"
    );
    let req=f.call("POST","/api/cli/device/request",json!({"public_key":public,"client_name":"Fixture CLI","agent_type":"agent","nonce":nonce,"product_id":"product-a","signature":signed(&key,&proof)}),"","").unwrap();
    let options = f
        .call(
            "POST",
            "/api/manage/device/options",
            json!({"user_code":req["user_code"],"staff_id":"link-a"}),
            &browser,
            "",
        )
        .unwrap();
    assert_eq!(options["selected"]["shop_id"], "shop-a");
    let approve = json!({"user_code":req["user_code"],"staff_id":"link-a","review_digest":options["review_digest"]});
    f.call("POST", "/api/manage/device/approve", approve, &browser, "")
        .unwrap();
    let proof = format!(
        "extore-cli-device-claim-v1\nhttps://example.test\n{}\n{}\n{public}",
        text(&req, "request_id"),
        text(&req, "challenge")
    );
    let claimed=f.call("POST","/api/cli/device/claim",json!({"request_id":req["request_id"],"public_key":public,"signature":signed(&key,&proof)}),"","").unwrap();
    assert_eq!(claimed["product_id"], "product-a");
    assert_eq!(claimed["agent_type"], "agent");
    assert_eq!(claimed["cli_uses"], 1);
}
#[test]
fn device_review_rejects_a_changed_link() {
    let mut f = Fixture::new();
    let browser = f.browser(Some("shop-a"), "admin", None);
    let key = SigningKey::from_bytes(&[5; 32]);
    let public = URL_SAFE_NO_PAD.encode(key.verifying_key().to_bytes());
    let mut nonce = [5u8; 32];
    nonce[..8].copy_from_slice(&(now() as u64).to_be_bytes());
    let nonce = URL_SAFE_NO_PAD.encode(nonce);
    let proof = format!(
        "extore-cli-device-request-v1\nhttps://example.test\n{public}\nFixture CLI\n{nonce}\nproduct-a"
    );
    let req=f.call("POST","/api/cli/device/request",json!({"public_key":public,"client_name":"Fixture CLI","nonce":nonce,"product_id":"product-a","signature":signed(&key,&proof)}),"","").unwrap();
    let options = f
        .call(
            "POST",
            "/api/manage/device/options",
            json!({"user_code":req["user_code"],"staff_id":"link-a"}),
            &browser,
            "",
        )
        .unwrap();
    f.ctx
        .conn
        .execute(
            "UPDATE staff SET permissions='[\"queue.view\"]' WHERE id='link-a'",
            [],
        )
        .unwrap();
    assert_eq!(f.call("POST","/api/manage/device/approve",json!({"user_code":req["user_code"],"staff_id":"link-a","review_digest":options["review_digest"]}),&browser,"").unwrap_err().status,409);
}
#[test]
fn owner_signature_covers_raw_target_body_and_is_single_use() {
    let mut f = Fixture::new();
    let key = SigningKey::from_bytes(&[8; 32]);
    let public = URL_SAFE_NO_PAD.encode(key.verifying_key().to_bytes());
    f.ctx.conn.execute("INSERT INTO owner_cli_devices(id,public_key,client_name,fingerprint,expires,approved_credential_id,created,last_seen,shop_id) VALUES (?,?,?,?,?,?,?,?,?)",params!["owner-device",public,"Owner CLI",cli::fingerprint(&key.verifying_key().to_bytes()),now()+3600.0,"password:shop-a",now(),now(),"shop-a"]).unwrap();
    let ch = f
        .call(
            "POST",
            "/api/cli/owner/challenge",
            json!({"device_id":"owner-device"}),
            "",
            "",
        )
        .unwrap();
    let proof = format!(
        "extore-cli-owner-session-v1\nhttps://example.test\nowner-device\n{}\n{}",
        text(&ch, "challenge_id"),
        text(&ch, "challenge")
    );
    let logged=f.call("POST","/api/cli/owner/session",json!({"device_id":"owner-device","challenge_id":ch["challenge_id"],"signature":signed(&key,&proof)}),"","").unwrap();
    let bearer = format!("bEaReR {}", text(&logged, "access_token"));
    let target = "/api/shop/account?b=2&a=%2F";
    let body = json!({"name":"更新店铺"});
    let raw = serde_json::to_vec(&body).unwrap();
    let hash = cli::fingerprint(&raw);
    let ch = f
        .call(
            "POST",
            "/api/cli/owner/action-challenge",
            json!({"method":"PATCH","path":target,"body_sha256":hash}),
            "",
            &bearer,
        )
        .unwrap();
    let proof = format!(
        "extore-cli-owner-action-v1\nhttps://example.test\nowner-device\n{}\n{}\n{}\nPATCH\n{target}\n{hash}",
        text(&logged, "session_id"),
        text(&ch, "challenge_id"),
        text(&ch, "challenge")
    );
    let sig = signed(&key, &proof);
    f.prepare("PATCH", target, body.clone(), "", &bearer);
    f.ctx.headers.insert(
        "x-extore-cli-challenge",
        HeaderValue::from_str(text(&ch, "challenge_id")).unwrap(),
    );
    f.ctx.headers.insert(
        "x-extore-cli-signature",
        HeaderValue::from_str(&sig).unwrap(),
    );
    f.ctx.raw_target = "/api/shop/account?a=%2F&b=2".into();
    assert_eq!(f.dispatch().unwrap_err().status, 401);
    f.ctx.raw_target = target.into();
    let renamed = f.dispatch().unwrap();
    assert_eq!(renamed["name"], "更新店铺");
    assert_eq!(f.dispatch().unwrap_err().status, 401);
}
#[test]
fn sessions_and_audit_do_not_cross_shop_scope() {
    let mut f = Fixture::new();
    let a = f.browser(Some("shop-a"), "admin", None);
    let b = f.browser(Some("shop-b"), "admin", None);
    let mine = f
        .call("GET", "/api/admin/sessions", json!({}), &a, "")
        .unwrap();
    assert!(
        mine.as_array()
            .unwrap()
            .iter()
            .all(|r| r["shop_id"] == "shop-a")
    );
    let theirs = f
        .call("GET", "/api/admin/sessions", json!({}), &b, "")
        .unwrap();
    let sid = theirs[0]["id"].as_str().unwrap();
    assert_eq!(
        f.call(
            "DELETE",
            &format!("/api/admin/sessions/{sid}"),
            json!({}),
            &a,
            ""
        )
        .unwrap_err()
        .status,
        404
    );
    f.ctx.conn.execute("INSERT INTO audit(actor,action,target,created,shop_id) VALUES ('owner','smtp.configure','secret-token',?,'shop-a')",[now()]).unwrap();
    let audit = f
        .call("GET", "/api/admin/audit", json!({}), &a, "")
        .unwrap();
    assert!(
        audit
            .as_array()
            .unwrap()
            .iter()
            .all(|v| v["target"] != "secret-token")
    );
}
#[test]
fn bootstrap_reset_uses_private_file_and_argon2_compatibility() {
    let f = Fixture::new();
    reset_auth(&f.ctx.app).unwrap();
    let hash = setting(&f.ctx.conn, "bootstrap_password", "").unwrap();
    let password = std::fs::read_to_string(f.ctx.app.data.join("bootstrap-password.txt")).unwrap();
    assert!(hash.starts_with("$argon2id$v=19$m=65536,t=3,p=4$"));
    assert!(password_valid(&hash, password.trim()));
    assert!(!password_valid(&hash, "wrong"));
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(f.ctx.app.data.join("bootstrap-password.txt"))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
    }
}

fn add_passkey(f: &Fixture) -> (p256::ecdsa::SigningKey, String) {
    use serde_cbor_2::Value as Cbor;
    let signing = p256::ecdsa::SigningKey::random(&mut rand::rngs::OsRng);
    let point = signing.verifying_key().to_encoded_point(false);
    let mut fields = BTreeMap::new();
    for (k, v) in [(1, 2), (3, -7), (-1, 1)] {
        fields.insert(Cbor::Integer(k), Cbor::Integer(v));
    }
    fields.insert(Cbor::Integer(-2), Cbor::Bytes(point.x().unwrap().to_vec()));
    fields.insert(Cbor::Integer(-3), Cbor::Bytes(point.y().unwrap().to_vec()));
    let key = serde_cbor_2::to_vec(&Cbor::Map(fields)).unwrap();
    let cid = URL_SAFE_NO_PAD.encode(b"fixture-passkey");
    f.ctx.conn.execute("INSERT INTO credentials(id,public_key,sign_count,name,created,shop_id) VALUES (?,?,0,'Fixture Passkey',?,NULL)",params![cid,key,now()]).unwrap();
    (signing, cid)
}
fn assertion(
    key: &p256::ecdsa::SigningKey,
    cid: &str,
    challenge: &Value,
    origin: &str,
    rp: &str,
    count: u32,
    flags: u8,
) -> Value {
    let client=serde_json::to_vec(&json!({"type":"webauthn.get","challenge":challenge["challenge"],"origin":origin,"crossOrigin":false})).unwrap();
    let mut authdata = Sha256::digest(rp.as_bytes()).to_vec();
    authdata.push(flags);
    authdata.extend(count.to_be_bytes());
    let mut payload = authdata.clone();
    payload.extend(Sha256::digest(&client));
    let signature: p256::ecdsa::Signature = key.sign(&payload);
    json!({"id":cid,"rawId":cid,"type":"public-key","clientExtensionResults":{},"response":{"authenticatorData":URL_SAFE_NO_PAD.encode(&authdata),"clientDataJSON":URL_SAFE_NO_PAD.encode(client),"signature":URL_SAFE_NO_PAD.encode(signature.to_der().as_bytes()),"userHandle":URL_SAFE_NO_PAD.encode(b"extore-owner")}})
}
#[test]
fn legacy_cose_passkey_login_verifies_real_es256_and_rejects_replay() {
    let mut f = Fixture::new();
    let (key, cid) = add_passkey(&f);
    let options = f
        .call("POST", "/api/auth/login/options", json!({}), "", "")
        .unwrap();
    assert_eq!(options["userVerification"], "required");
    let cookie = f.cookie("extore_challenge");
    let credential = assertion(
        &key,
        &cid,
        &options,
        "https://example.test",
        "example.test",
        1,
        5,
    );
    let auth = f
        .call(
            "POST",
            "/api/auth/login/verify",
            json!({"credential":credential.clone()}),
            &cookie,
            "",
        )
        .unwrap();
    assert_eq!(auth["role"], "admin");
    assert_eq!(auth["superadmin"], true);
    let session = f.cookie("extore_session");
    assert_eq!(
        f.call("GET", "/api/auth/status", json!({}), &session, "")
            .unwrap()["actor"],
        "owner"
    );
    assert_eq!(
        f.call(
            "POST",
            "/api/auth/login/verify",
            json!({"credential":credential}),
            &cookie,
            ""
        )
        .unwrap_err()
        .status,
        400
    );
    let options = f
        .call("POST", "/api/auth/login/options", json!({}), "", "")
        .unwrap();
    let cookie = f.cookie("extore_challenge");
    let duplicate_counter = assertion(
        &key,
        &cid,
        &options,
        "https://example.test",
        "example.test",
        1,
        5,
    );
    assert_eq!(
        f.call(
            "POST",
            "/api/auth/login/verify",
            json!({"credential":duplicate_counter}),
            &cookie,
            ""
        )
        .unwrap_err()
        .status,
        401
    );
}
#[test]
fn passkey_origin_rp_and_user_verification_are_enforced() {
    let mut f = Fixture::new();
    let (key, cid) = add_passkey(&f);
    for (origin, rp, flags) in [
        ("https://evil.example", "example.test", 5),
        ("https://example.test", "evil.example", 5),
        ("https://example.test", "example.test", 1),
    ] {
        let options = f
            .call("POST", "/api/auth/login/options", json!({}), "", "")
            .unwrap();
        let cookie = f.cookie("extore_challenge");
        let credential = assertion(&key, &cid, &options, origin, rp, 1, flags);
        assert_eq!(
            f.call(
                "POST",
                "/api/auth/login/verify",
                json!({"credential":credential}),
                &cookie,
                ""
            )
            .unwrap_err()
            .status,
            401
        );
    }
    assert_eq!(
        integer(
            &row(&f.ctx.conn, "SELECT count(*) n FROM sessions", [])
                .unwrap()
                .unwrap(),
            "n",
            0
        ),
        0
    );
}

#[test]
fn bootstrap_enrollment_creates_legacy_compatible_cose_and_removes_password() {
    use serde_cbor_2::Value as Cbor;
    let mut f = Fixture::new();
    reset_auth(&f.ctx.app).unwrap();
    let password = std::fs::read_to_string(f.ctx.app.data.join("bootstrap-password.txt")).unwrap();
    f.call(
        "POST",
        "/api/auth/password",
        json!({"password":password.trim()}),
        "",
        "",
    )
    .unwrap();
    let session = f.cookie("extore_session");
    let options = f
        .call(
            "POST",
            "/api/auth/register/options",
            json!({}),
            &session,
            "",
        )
        .unwrap();
    let challenge = f.cookie("extore_challenge");
    let key = p256::ecdsa::SigningKey::random(&mut rand::rngs::OsRng);
    let point = key.verifying_key().to_encoded_point(false);
    let mut cose = BTreeMap::new();
    for (k, v) in [(1, 2), (3, -7), (-1, 1)] {
        cose.insert(Cbor::Integer(k), Cbor::Integer(v));
    }
    cose.insert(Cbor::Integer(-2), Cbor::Bytes(point.x().unwrap().to_vec()));
    cose.insert(Cbor::Integer(-3), Cbor::Bytes(point.y().unwrap().to_vec()));
    let cose = serde_cbor_2::to_vec(&Cbor::Map(cose)).unwrap();
    let raw_id = b"new-native-passkey";
    let cid = URL_SAFE_NO_PAD.encode(raw_id);
    let mut authdata = Sha256::digest(b"example.test").to_vec();
    authdata.push(0x45);
    authdata.extend(0u32.to_be_bytes());
    authdata.extend([0u8; 16]);
    authdata.extend((raw_id.len() as u16).to_be_bytes());
    authdata.extend(raw_id);
    authdata.extend(&cose);
    let mut attestation = BTreeMap::new();
    attestation.insert(Cbor::Text("fmt".into()), Cbor::Text("none".into()));
    attestation.insert(Cbor::Text("attStmt".into()), Cbor::Map(BTreeMap::new()));
    attestation.insert(Cbor::Text("authData".into()), Cbor::Bytes(authdata));
    let client=serde_json::to_vec(&json!({"type":"webauthn.create","challenge":options["challenge"],"origin":"https://example.test","crossOrigin":false})).unwrap();
    let credential = json!({"id":cid,"rawId":cid,"type":"public-key","clientExtensionResults":{},"response":{"clientDataJSON":URL_SAFE_NO_PAD.encode(client),"attestationObject":URL_SAFE_NO_PAD.encode(serde_cbor_2::to_vec(&Cbor::Map(attestation)).unwrap()),"transports":["internal"]}});
    f.call(
        "POST",
        "/api/auth/register/verify",
        json!({"credential":credential,"name":"Native Passkey"}),
        &format!("{session}; {challenge}"),
        "",
    )
    .unwrap();
    let cookie = f.cookie("extore_session");
    assert_eq!(
        f.call("GET", "/api/auth/status", json!({}), &cookie, "")
            .unwrap()["role"],
        "admin"
    );
    assert_eq!(
        setting(&f.ctx.conn, "bootstrap_password", "missing").unwrap(),
        ""
    );
    assert!(!f.ctx.app.data.join("bootstrap-password.txt").exists());
    let stored = row(&f.ctx.conn, "SELECT * FROM credentials WHERE id=?", [&cid])
        .unwrap()
        .unwrap();
    let bytes = URL_SAFE_NO_PAD.decode(text(&stored, "public_key")).unwrap();
    assert_eq!(
        serde_cbor_2::from_slice::<Cbor>(&bytes).unwrap(),
        serde_cbor_2::from_slice::<Cbor>(&cose).unwrap()
    );
    assert_eq!(
        f.call(
            "POST",
            "/api/auth/password",
            json!({"password":password.trim()}),
            "",
            ""
        )
        .unwrap_err()
        .status,
        403
    );
}

-- Extore SQLite schema version 23. Existing rows and credential formats are retained.
CREATE TABLE account_tokens (digest TEXT PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('invite','register','reset')),shop_id TEXT REFERENCES shops(id) ON DELETE CASCADE,email TEXT NOT NULL,password_hash TEXT,name TEXT,created REAL NOT NULL,expires REAL NOT NULL,used REAL);
CREATE TABLE api_requests (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response BLOB NOT NULL, created REAL NOT NULL);
CREATE TABLE audit (id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, created REAL NOT NULL, shop_id TEXT REFERENCES shops(id));
CREATE TABLE automation_requests (id TEXT PRIMARY KEY,device_id TEXT NOT NULL,body_hash TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('pending','done')),created REAL NOT NULL,deadline REAL NOT NULL,expires REAL NOT NULL,items TEXT NOT NULL DEFAULT '[]');
CREATE TABLE callback_nonces (nonce TEXT PRIMARY KEY, created REAL NOT NULL);
CREATE TABLE card_batches (id TEXT PRIMARY KEY, product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE, label TEXT NOT NULL DEFAULT '', created REAL NOT NULL, count INTEGER NOT NULL, variant_id TEXT NOT NULL DEFAULT 'default', deleted_at REAL);
CREATE TABLE card_entitlements (card_id TEXT PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,attributes TEXT NOT NULL,policy TEXT,created REAL NOT NULL);
CREATE TABLE card_meta (card_id TEXT PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE, batch_id TEXT REFERENCES card_batches(id) ON DELETE SET NULL, code_suffix TEXT, expires REAL, first_verified REAL, variant_id TEXT NOT NULL DEFAULT 'default', variant_snapshot TEXT NOT NULL DEFAULT '{}', batch_deleted_at REAL, batch_previous_state TEXT);
CREATE TABLE card_task_flows (card_id TEXT PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,shop_id TEXT NOT NULL REFERENCES shops(id),definition_hash TEXT NOT NULL,snapshot_ciphertext TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE cards (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, product_id TEXT NOT NULL REFERENCES products(id), state TEXT NOT NULL DEFAULT 'ready', created REAL NOT NULL);
CREATE TABLE challenges (digest TEXT PRIMARY KEY, challenge BLOB NOT NULL, kind TEXT NOT NULL, session_digest TEXT, expires REAL NOT NULL);
CREATE TABLE cli_bind_tickets (digest TEXT PRIMARY KEY,staff_id TEXT NOT NULL REFERENCES staff(id),expires REAL NOT NULL,created REAL NOT NULL,consumed INTEGER NOT NULL DEFAULT 0 CHECK(consumed IN (0,1)),bound_device_id TEXT REFERENCES cli_devices(id));
CREATE TABLE cli_challenges (id TEXT PRIMARY KEY,device_id TEXT NOT NULL REFERENCES cli_devices(id),challenge TEXT NOT NULL,expires REAL NOT NULL,created REAL NOT NULL);
CREATE TABLE cli_device_requests (id TEXT PRIMARY KEY,public_key TEXT NOT NULL,client_name TEXT NOT NULL,fingerprint TEXT NOT NULL,nonce TEXT NOT NULL,user_code TEXT UNIQUE NOT NULL,challenge TEXT NOT NULL,requested_product_id TEXT,expires REAL NOT NULL,created REAL NOT NULL,state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','approved','claimed','denied')),approved_staff_id TEXT REFERENCES staff(id) ON DELETE SET NULL,approved_snapshot TEXT,approved_session_digest TEXT REFERENCES sessions(digest) ON DELETE SET NULL,approved_role TEXT,approved_shop_id TEXT,device_id TEXT REFERENCES cli_devices(id) ON DELETE SET NULL,poll_after REAL NOT NULL DEFAULT 0,poll_interval INTEGER NOT NULL DEFAULT 5, agent_type TEXT,UNIQUE(public_key,nonce));
CREATE TABLE cli_devices (id TEXT PRIMARY KEY,staff_id TEXT NOT NULL REFERENCES staff(id),public_key TEXT NOT NULL,fingerprint TEXT NOT NULL,client_name TEXT NOT NULL,created REAL NOT NULL,last_seen REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)), agent_type TEXT,UNIQUE(staff_id,public_key));
CREATE TABLE cli_scope_requests (id TEXT PRIMARY KEY,public_key TEXT NOT NULL,client_name TEXT NOT NULL,fingerprint TEXT NOT NULL,nonce TEXT NOT NULL,user_code TEXT UNIQUE NOT NULL,challenge TEXT NOT NULL,request_json TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('product','shop.pipeline')),shop_id TEXT NOT NULL REFERENCES shops(id),requested_product_ids TEXT NOT NULL,requested_permissions TEXT NOT NULL,grant_expires REAL NOT NULL,current_authorization_id TEXT REFERENCES pipeline_authorizations(id),current_revision INTEGER,expires REAL NOT NULL,created REAL NOT NULL,state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','approved','claimed','denied')),approved_draft TEXT,approved_snapshot TEXT,approved_session_digest TEXT REFERENCES sessions(digest) ON DELETE SET NULL,approved_role TEXT,approved_shop_id TEXT,authorization_id TEXT REFERENCES pipeline_authorizations(id),claimed_revision INTEGER,poll_after REAL NOT NULL DEFAULT 0,poll_interval INTEGER NOT NULL DEFAULT 5, agent_type TEXT,UNIQUE(public_key,nonce));
CREATE TABLE commerce_bindings (id TEXT PRIMARY KEY,shop_id TEXT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,key_digest TEXT UNIQUE NOT NULL,label TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)));
CREATE TABLE commerce_clients (id TEXT PRIMARY KEY,shop_id TEXT NOT NULL REFERENCES shops(id),metadata TEXT NOT NULL,created REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)));
CREATE TABLE commerce_grants (id TEXT PRIMARY KEY,client_id TEXT NOT NULL REFERENCES commerce_clients(id),shop_id TEXT NOT NULL REFERENCES shops(id),request_id TEXT UNIQUE REFERENCES commerce_requests(id) ON DELETE SET NULL,scopes TEXT NOT NULL,issuer_role TEXT NOT NULL CHECK(issuer_role IN ('root','shop')),product_ids TEXT NOT NULL,card_limits TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),last_used REAL,code_digest TEXT UNIQUE NOT NULL,code_expires REAL NOT NULL,code_used REAL,redirect_uri TEXT NOT NULL,code_challenge TEXT NOT NULL);
CREATE TABLE commerce_issuances (id TEXT PRIMARY KEY,grant_id TEXT NOT NULL REFERENCES commerce_grants(id) ON DELETE CASCADE,key_digest TEXT NOT NULL,fingerprint TEXT NOT NULL,product_id TEXT NOT NULL REFERENCES products(id),variant_id TEXT NOT NULL,batch_id TEXT NOT NULL,created REAL NOT NULL,recovery_expires REAL NOT NULL,response_ciphertext TEXT,UNIQUE(grant_id,key_digest));
CREATE TABLE commerce_requests (id TEXT PRIMARY KEY,client_id TEXT NOT NULL REFERENCES commerce_clients(id),redirect_uri TEXT NOT NULL,state TEXT NOT NULL,code_challenge TEXT NOT NULL,scopes TEXT NOT NULL,product_ids TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','denied')));
CREATE TABLE commerce_tokens (digest TEXT PRIMARY KEY,grant_id TEXT NOT NULL REFERENCES commerce_grants(id) ON DELETE CASCADE,kind TEXT NOT NULL CHECK(kind IN ('access','refresh')),created REAL NOT NULL,expires REAL NOT NULL,used REAL);
CREATE TABLE credentials (id TEXT PRIMARY KEY, public_key BLOB NOT NULL, sign_count INTEGER NOT NULL, name TEXT NOT NULL, created REAL NOT NULL, shop_id TEXT REFERENCES shops(id));
CREATE TABLE events (id TEXT PRIMARY KEY, type TEXT NOT NULL, job_id TEXT, product_id TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE grants (digest TEXT PRIMARY KEY, card_id TEXT NOT NULL REFERENCES cards(id), expires REAL NOT NULL);
CREATE TABLE job_delivery_versions (job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,revision INTEGER NOT NULL CHECK(revision>=0),attempt INTEGER NOT NULL,content TEXT,result_json TEXT,revealed INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,PRIMARY KEY(job_id,revision));
CREATE TABLE job_files (id TEXT PRIMARY KEY, card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE, product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE, job_id TEXT REFERENCES jobs(id) ON DELETE CASCADE, field_key TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('input','output')), attempt INTEGER NOT NULL DEFAULT 1, filename TEXT NOT NULL, content_type TEXT NOT NULL, size INTEGER NOT NULL, content BLOB, bound INTEGER NOT NULL DEFAULT 0, released INTEGER NOT NULL DEFAULT 0, consumed INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, CHECK(size>=0 AND size<=20971520));
CREATE TABLE job_revision_requests (job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,request_id TEXT NOT NULL,request_digest TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>0),message TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(job_id,request_id),UNIQUE(job_id,revision));
CREATE TABLE job_worker_identities (job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,attempt INTEGER NOT NULL,actor TEXT NOT NULL,device_ref TEXT,worker_ref TEXT,client_name TEXT NOT NULL,agent_type TEXT,channel TEXT NOT NULL CHECK(channel IN ('cli','browser','automatic')),claimed_at REAL NOT NULL,PRIMARY KEY(job_id,attempt));
CREATE TABLE jobs (id TEXT PRIMARY KEY, card_id TEXT UNIQUE NOT NULL REFERENCES cards(id), product_id TEXT NOT NULL REFERENCES products(id), state TEXT NOT NULL, params TEXT NOT NULL, content TEXT, message TEXT NOT NULL DEFAULT '', progress INTEGER NOT NULL DEFAULT 0, attempt INTEGER NOT NULL DEFAULT 1, retryable INTEGER NOT NULL DEFAULT 0, claimed_by TEXT, lease REAL, created REAL NOT NULL, updated REAL NOT NULL, revealed INTEGER NOT NULL DEFAULT 0, result_json TEXT, progress_plan TEXT, completed_steps TEXT NOT NULL DEFAULT '[]', schema_snapshot TEXT, retry_mode TEXT NOT NULL DEFAULT 'revise', retry_reason_type TEXT NOT NULL DEFAULT 'customer_input', revision_round INTEGER NOT NULL DEFAULT 0, revision_start_attempt INTEGER NOT NULL DEFAULT 1, revision_message TEXT NOT NULL DEFAULT '');
CREATE TABLE mail_outbox (
    id TEXT PRIMARY KEY,
    tenant_id TEXT,
    payload TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending', 'sending', 'delivered', 'dead')),
    attempts INTEGER NOT NULL DEFAULT 0,
    due REAL NOT NULL,
    created REAL NOT NULL,
    expires REAL NOT NULL,
    lease_until REAL,
    lease_token TEXT,
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE outbox (id TEXT PRIMARY KEY REFERENCES events(id), url TEXT NOT NULL, secret TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '', finished_at REAL);
CREATE TABLE owner_cli_action_challenges (id TEXT PRIMARY KEY,device_id TEXT NOT NULL REFERENCES owner_cli_devices(id),session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,challenge TEXT NOT NULL,method TEXT NOT NULL,path TEXT NOT NULL,body_sha256 TEXT NOT NULL,expires REAL NOT NULL,created REAL NOT NULL);
CREATE TABLE owner_cli_approval_challenges (digest TEXT PRIMARY KEY,request_id TEXT NOT NULL REFERENCES owner_cli_requests(id) ON DELETE CASCADE,challenge BLOB NOT NULL,snapshot TEXT NOT NULL,expires REAL NOT NULL,created REAL NOT NULL);
CREATE TABLE owner_cli_challenges (id TEXT PRIMARY KEY,device_id TEXT NOT NULL REFERENCES owner_cli_devices(id),challenge TEXT NOT NULL,expires REAL NOT NULL,created REAL NOT NULL);
CREATE TABLE owner_cli_devices (id TEXT PRIMARY KEY,public_key TEXT UNIQUE NOT NULL,client_name TEXT NOT NULL,fingerprint TEXT NOT NULL,expires REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),approved_credential_id TEXT NOT NULL,created REAL NOT NULL,last_seen REAL NOT NULL, shop_id TEXT REFERENCES shops(id));
CREATE TABLE owner_cli_password_challenges (digest TEXT PRIMARY KEY,request_id TEXT NOT NULL REFERENCES owner_cli_requests(id) ON DELETE CASCADE,shop_id TEXT NOT NULL REFERENCES shops(id),snapshot TEXT NOT NULL,expires REAL NOT NULL,created REAL NOT NULL);
CREATE TABLE owner_cli_requests (id TEXT PRIMARY KEY,public_key TEXT NOT NULL,client_name TEXT NOT NULL,fingerprint TEXT NOT NULL,nonce TEXT NOT NULL,device_code TEXT NOT NULL,challenge TEXT NOT NULL,scope TEXT NOT NULL,expires REAL NOT NULL,grant_expires REAL NOT NULL,state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','approved','claimed','denied')),approved_credential_id TEXT,approved_snapshot TEXT,device_id TEXT REFERENCES owner_cli_devices(id),created REAL NOT NULL, target_email TEXT, scope_bound INTEGER NOT NULL DEFAULT 0 CHECK(scope_bound IN (0,1)), shop_id TEXT REFERENCES shops(id),UNIQUE(public_key,nonce));
CREATE TABLE pipeline_authorizations (id TEXT PRIMARY KEY,shop_id TEXT NOT NULL REFERENCES shops(id),public_key TEXT NOT NULL,fingerprint TEXT NOT NULL,client_name TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('product','shop.pipeline')),permissions TEXT NOT NULL,expires REAL NOT NULL,revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),created REAL NOT NULL,last_seen REAL NOT NULL,approved_actor TEXT NOT NULL,issuer_role TEXT NOT NULL CHECK(issuer_role IN ('root','shop')),issuer_shop_id TEXT REFERENCES shops(id), agent_type TEXT,CHECK((issuer_role='root' AND issuer_shop_id IS NULL) OR (issuer_role='shop' AND issuer_shop_id=shop_id)));
CREATE TABLE pipeline_bindings (authorization_id TEXT NOT NULL REFERENCES pipeline_authorizations(id),product_id TEXT NOT NULL REFERENCES products(id),staff_id TEXT NOT NULL UNIQUE REFERENCES staff(id),device_id TEXT NOT NULL UNIQUE REFERENCES cli_devices(id),created REAL NOT NULL,PRIMARY KEY(authorization_id,product_id));
CREATE TABLE private_worker_nonces (digest TEXT PRIMARY KEY,product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,created REAL NOT NULL);
CREATE TABLE private_worker_receipts (scope_digest TEXT NOT NULL,result_id TEXT NOT NULL,job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,body_digest TEXT NOT NULL,response TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(scope_digest,result_id));
CREATE TABLE processor_card_bindings (card_id TEXT PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,product_id TEXT NOT NULL REFERENCES products(id),shop_id TEXT NOT NULL REFERENCES shops(id),processor_id TEXT NOT NULL,profile_id TEXT NOT NULL,revision INTEGER NOT NULL,FOREIGN KEY(profile_id,revision) REFERENCES processor_profile_revisions(profile_id,revision));
CREATE TABLE processor_product_bindings (product_id TEXT PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,profile_id TEXT NOT NULL,revision INTEGER NOT NULL,FOREIGN KEY(profile_id,revision) REFERENCES processor_profile_revisions(profile_id,revision));
CREATE TABLE processor_profile_revisions (profile_id TEXT NOT NULL REFERENCES processor_profiles(id),revision INTEGER NOT NULL,schema_version INTEGER NOT NULL,ciphertext TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(profile_id,revision));
CREATE TABLE processor_profiles (id TEXT PRIMARY KEY,shop_id TEXT NOT NULL REFERENCES shops(id),processor_id TEXT NOT NULL,name TEXT NOT NULL,revision INTEGER NOT NULL,disabled INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN(0,1)),created REAL NOT NULL,updated REAL NOT NULL);
CREATE TABLE product_lifecycle (product_id TEXT PRIMARY KEY REFERENCES products(id),deleted_at REAL,deleted_by TEXT);
CREATE TABLE product_purges (product_id TEXT PRIMARY KEY REFERENCES products(id),purged_at REAL NOT NULL,purged_by TEXT NOT NULL);
CREATE TABLE products (id TEXT PRIMARY KEY, config TEXT NOT NULL, created REAL NOT NULL, shop_id TEXT REFERENCES shops(id));
CREATE TABLE proxy_identities (id TEXT PRIMARY KEY,shop_id TEXT NOT NULL REFERENCES shops(id),name TEXT NOT NULL,public_key TEXT NOT NULL,private_key TEXT NOT NULL,created REAL NOT NULL, current INTEGER NOT NULL DEFAULT 0 CHECK(current IN(0,1)));
CREATE TABLE proxy_issued_cards (card_id TEXT PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,shop_id TEXT NOT NULL REFERENCES shops(id),route_id TEXT NOT NULL,issued REAL NOT NULL,FOREIGN KEY(shop_id,route_id) REFERENCES proxy_routes(shop_id,route_id) ON DELETE CASCADE);
CREATE TABLE proxy_routes (route_id TEXT NOT NULL,shop_id TEXT NOT NULL REFERENCES shops(id),name TEXT NOT NULL,identity_id TEXT REFERENCES proxy_identities(id),issuer_id TEXT NOT NULL,public_key TEXT NOT NULL,origin TEXT NOT NULL,path TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN(0,1)),default_issuer INTEGER NOT NULL DEFAULT 0 CHECK(default_issuer IN(0,1)),created REAL NOT NULL,updated REAL NOT NULL, archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN(0,1)),PRIMARY KEY(shop_id,route_id));
CREATE TABLE rate_limits (key TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL);
CREATE TABLE receipt_batch_cards (digest TEXT NOT NULL REFERENCES receipt_batches(digest), card_id TEXT NOT NULL REFERENCES cards(id), position INTEGER NOT NULL, PRIMARY KEY (digest, card_id));
CREATE TABLE receipt_batches (digest TEXT PRIMARY KEY, expires REAL NOT NULL, created REAL NOT NULL);
CREATE TABLE secret_store_state (id INTEGER PRIMARY KEY CHECK(id=1),fingerprint TEXT NOT NULL);
CREATE TABLE sessions (digest TEXT PRIMARY KEY, role TEXT NOT NULL, staff_id TEXT REFERENCES staff(id), expires REAL NOT NULL, created REAL NOT NULL, id TEXT, last_seen REAL NOT NULL DEFAULT 0, ip TEXT NOT NULL DEFAULT '', ua TEXT NOT NULL DEFAULT '', revoked INTEGER NOT NULL DEFAULT 0, channel TEXT NOT NULL DEFAULT 'browser' CHECK(channel IN ('browser','cli')), device_id TEXT REFERENCES cli_devices(id), client_name TEXT NOT NULL DEFAULT '', owner_device_id TEXT REFERENCES owner_cli_devices(id), shop_id TEXT REFERENCES shops(id), auth_at REAL NOT NULL DEFAULT 0, auth_method TEXT NOT NULL DEFAULT 'legacy');
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE shop_integration_keys (shop_id TEXT PRIMARY KEY REFERENCES shops(id) ON DELETE CASCADE,key_digest TEXT UNIQUE NOT NULL,created REAL NOT NULL);
CREATE TABLE shops (id TEXT PRIMARY KEY,name TEXT NOT NULL,email TEXT UNIQUE,password_hash TEXT,enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),created REAL NOT NULL,verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1)),legacy INTEGER NOT NULL DEFAULT 0 CHECK(legacy IN (0,1)),totp_secret TEXT,pending_totp_secret TEXT,pending_totp_expires REAL,last_totp_counter INTEGER NOT NULL DEFAULT -1,totp_backup_digests TEXT NOT NULL DEFAULT '[]', storage_limit_bytes INTEGER NOT NULL DEFAULT 1073741824 CHECK(storage_limit_bytes>0), factory_slogan TEXT NOT NULL DEFAULT '');
CREATE TABLE staff (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, product_id TEXT NOT NULL REFERENCES products(id), name TEXT NOT NULL, expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, permissions TEXT NOT NULL DEFAULT '["queue.view","queue.process"]', parent_id TEXT REFERENCES staff(id), created REAL NOT NULL DEFAULT 0, max_uses INTEGER NOT NULL DEFAULT 1, uses INTEGER NOT NULL DEFAULT 0, max_cli_uses INTEGER NOT NULL DEFAULT 1, cli_uses INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)));
CREATE TABLE task_flow_dispatches (id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,shop_id TEXT NOT NULL REFERENCES shops(id),attempt INTEGER NOT NULL,flow_epoch INTEGER NOT NULL,node_id TEXT NOT NULL,action_id TEXT NOT NULL UNIQUE,state TEXT NOT NULL,payload_ciphertext TEXT NOT NULL,ciphertext_bytes INTEGER NOT NULL,payload_digest TEXT NOT NULL,deadline REAL,input_expires_at REAL,due REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL,finished_at REAL,error TEXT NOT NULL DEFAULT '');
CREATE TABLE task_flow_files (file_id TEXT PRIMARY KEY REFERENCES job_files(id) ON DELETE CASCADE,job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,node_id TEXT NOT NULL,flow_epoch INTEGER NOT NULL,attempt INTEGER NOT NULL,kind TEXT NOT NULL);
CREATE TABLE task_flow_runs (job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,attempt INTEGER NOT NULL,flow_epoch INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 0,node_id TEXT NOT NULL DEFAULT '',phase TEXT NOT NULL,entered_at REAL,started_at REAL,deadline REAL,input_expires_at REAL,transition_count INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL);
CREATE TABLE task_flow_steps (job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,flow_epoch INTEGER NOT NULL,attempt INTEGER NOT NULL,node_id TEXT NOT NULL,kind TEXT NOT NULL,state TEXT NOT NULL,action_id TEXT,entered_at REAL NOT NULL,started_at REAL,deadline REAL,completed_at REAL,payload_ciphertext TEXT NOT NULL,result_digest TEXT,PRIMARY KEY(job_id,flow_epoch));
CREATE TABLE text_card_payloads (card_id TEXT PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,shop_id TEXT NOT NULL REFERENCES shops(id),ciphertext TEXT,definition TEXT NOT NULL,created REAL NOT NULL,size INTEGER NOT NULL DEFAULT 0 CHECK(size>=0));
CREATE TABLE upload_reservations (id TEXT PRIMARY KEY, size INTEGER NOT NULL DEFAULT 0 CHECK(size>=0), created REAL NOT NULL, shop_id TEXT REFERENCES shops(id), product_id TEXT REFERENCES products(id));
CREATE INDEX account_tokens_expiry ON account_tokens(expires);
CREATE INDEX audit_retention ON audit(shop_id,created,id);
CREATE INDEX automation_requests_device ON automation_requests(device_id,expires);
CREATE INDEX automation_requests_expiry ON automation_requests(expires);
CREATE INDEX card_meta_batch ON card_meta(batch_id);
CREATE INDEX card_meta_expiry ON card_meta(expires);
CREATE INDEX card_meta_variant ON card_meta(variant_id);
CREATE INDEX cards_product_created ON cards(product_id,created);
CREATE INDEX cli_device_link ON cli_devices(staff_id);
CREATE INDEX cli_device_request_expiry ON cli_device_requests(expires);
CREATE INDEX cli_device_request_staff ON cli_device_requests(approved_staff_id);
CREATE INDEX cli_scope_request_expiry ON cli_scope_requests(expires);
CREATE INDEX cli_scope_request_shop ON cli_scope_requests(shop_id,state);
CREATE INDEX cli_session_device ON sessions(device_id);
CREATE INDEX commerce_bindings_shop ON commerce_bindings(shop_id,revoked);
CREATE INDEX commerce_clients_shop ON commerce_clients(shop_id,revoked);
CREATE INDEX commerce_grants_shop ON commerce_grants(shop_id,revoked,expires);
CREATE INDEX commerce_issuances_recovery ON commerce_issuances(recovery_expires);
CREATE INDEX commerce_requests_client ON commerce_requests(client_id,status);
CREATE INDEX commerce_requests_expiry ON commerce_requests(expires);
CREATE INDEX commerce_tokens_expiry ON commerce_tokens(expires);
CREATE INDEX commerce_tokens_grant ON commerce_tokens(grant_id,kind);
CREATE INDEX credentials_shop ON credentials(shop_id);
CREATE INDEX delivery_versions_attempt ON job_delivery_versions(job_id,attempt);
CREATE INDEX events_job_created ON events(job_id,created);
CREATE INDEX events_retention ON events(created,product_id);
CREATE INDEX job_files_card ON job_files(card_id);
CREATE INDEX job_files_job ON job_files(job_id,kind);
CREATE INDEX jobs_queue ON jobs(state, created);
CREATE INDEX mail_outbox_due ON mail_outbox(state,due);
CREATE INDEX outbox_due ON outbox(state, due);
CREATE INDEX outbox_finished ON outbox(state,finished_at);
CREATE INDEX owner_cli_request_expiry ON owner_cli_requests(expires);
CREATE INDEX pipeline_authorizations_shop ON pipeline_authorizations(shop_id,revoked,expires);
CREATE INDEX private_worker_nonce_expiry ON private_worker_nonces(created);
CREATE INDEX processor_profiles_shop ON processor_profiles(shop_id);
CREATE INDEX products_shop ON products(shop_id);
CREATE UNIQUE INDEX proxy_current_identity ON proxy_identities(shop_id) WHERE current=1;
CREATE UNIQUE INDEX proxy_default_issuer ON proxy_routes(shop_id) WHERE default_issuer=1;
CREATE INDEX proxy_issued_route ON proxy_issued_cards(shop_id,route_id);
CREATE INDEX proxy_routes_shop ON proxy_routes(shop_id);
CREATE UNIQUE INDEX sessions_id ON sessions(id);
CREATE INDEX sessions_link ON sessions(staff_id,revoked,expires);
CREATE INDEX sessions_owner_device ON sessions(owner_device_id);
CREATE INDEX sessions_shop ON sessions(shop_id);
CREATE INDEX staff_cleanup ON staff(product_id,archived,created,id);
CREATE INDEX staff_parent ON staff(parent_id);
CREATE INDEX task_flow_deadlines ON task_flow_runs(deadline,phase);
CREATE INDEX task_flow_dispatch_due ON task_flow_dispatches(state,due);
CREATE INDEX task_flow_dispatch_job ON task_flow_dispatches(job_id,attempt,flow_epoch);
CREATE INDEX task_flow_files_scope ON task_flow_files(job_id,attempt,flow_epoch,kind);
CREATE INDEX task_flow_input_expiry ON task_flow_runs(input_expires_at,phase);
CREATE INDEX task_flow_step_lookup ON task_flow_steps(job_id,attempt,node_id,flow_epoch DESC);
CREATE INDEX text_card_payloads_shop ON text_card_payloads(shop_id);
CREATE INDEX upload_reservations_shop ON upload_reservations(shop_id);
PRAGMA user_version=23;

//! Executes the existing commerce catalog handler for an in-process host.
//!
//! The host supplies an operator-owned Extore state directory. No server, worker,
//! migration or Core database connection is started by this adapter.

use crate::{catalog, common::*, dispatch_transaction};
use axum::http::{HeaderMap, HeaderValue, Method};
use extore_plugin_api::{
    CONFIG_SCHEMA_VERSION, Config, INVALID_CONFIG, MAX_CONFIG_BYTES, MAX_RESPONSE_BYTES,
    RESPONSE_SCHEMA, RESPONSE_TOO_LARGE, Response, STATE_UNAVAILABLE, zplugin,
};
use serde_json::{Value, json};
use std::{collections::BTreeMap, sync::Arc};

const CATALOG_PATH: &str = "/api/integrations/commerce/products";

/// An independently configured Extore store. Authorization is read for every call.
pub struct CommerceRuntime {
    app: Arc<App>,
}

impl CommerceRuntime {
    /// Open existing Extore state without initializing or migrating it.
    pub fn configure(bytes: &[u8]) -> zplugin::Result<Self> {
        if bytes.len() > MAX_CONFIG_BYTES {
            return Err(zplugin::Error::PluginStatus(INVALID_CONFIG));
        }
        let config: Config = serde_json::from_slice(bytes)
            .map_err(|_| zplugin::Error::PluginStatus(INVALID_CONFIG))?;
        if config.schema_version != CONFIG_SCHEMA_VERSION || !config.data_dir.is_absolute() {
            return Err(zplugin::Error::PluginStatus(INVALID_CONFIG));
        }
        let origin = url::Url::parse(&config.origin)
            .map_err(|_| zplugin::Error::PluginStatus(INVALID_CONFIG))?;
        if !matches!(origin.scheme(), "http" | "https")
            || origin.host_str().is_none()
            || origin.path() != "/"
            || origin.query().is_some()
            || origin.fragment().is_some()
            || !origin.username().is_empty()
            || origin.password().is_some()
            || (origin.scheme() != "https"
                && !matches!(origin.host_str(), Some("localhost" | "127.0.0.1")))
            || config.origin.ends_with('/')
        {
            return Err(zplugin::Error::PluginStatus(INVALID_CONFIG));
        }
        let data = config
            .data_dir
            .canonicalize()
            .map_err(|_| zplugin::Error::PluginStatus(STATE_UNAVAILABLE))?;
        let app = Arc::new(App {
            data,
            origin: config.origin,
            static_dir: std::path::PathBuf::new(),
        });
        // Validate the same already-opened, non-creating connection. A path check
        // followed by Connection::open could recreate a concurrently removed DB.
        let conn = app
            .existing_db()
            .map_err(|_| zplugin::Error::PluginStatus(STATE_UNAVAILABLE))?;
        catalog::validate_state_connection(&app, &conn)
            .map_err(|_| zplugin::Error::PluginStatus(STATE_UNAVAILABLE))?;
        Ok(Self { app })
    }

    /// Read the approved catalog through the same transaction and handler as HTTP.
    /// Failures of the existing authorization checks retain their HTTP status/body.
    pub fn catalog(&self, access_token: &str) -> zplugin::Result<Vec<u8>> {
        // Deliberately omit CREATE. Removing a configured database must fail closed.
        let conn = self
            .app
            .existing_db()
            .map_err(|_| zplugin::Error::PluginStatus(STATE_UNAVAILABLE))?;

        let mut headers = HeaderMap::new();
        // Invalid tokens reach the ordinary commerce authorization rejection.
        // Cap the header before allocation; an Extore access token is 43 bytes.
        if access_token.len() == 43 {
            if let Ok(value) = HeaderValue::from_str(&format!("Bearer {access_token}")) {
                headers.insert("authorization", value);
            }
        }
        let mut ctx = Context {
            app: self.app.clone(),
            conn,
            method: Method::GET,
            path: CATALOG_PATH.to_owned(),
            raw_target: CATALOG_PATH.to_owned(),
            query: BTreeMap::new(),
            headers,
            body: Value::Null,
            raw_body: Vec::new(),
            peer: "extore-plugin".to_owned(),
            response_headers: HeaderMap::new(),
        };
        let reply = match dispatch_transaction(&mut ctx) {
            Ok(reply) => reply,
            Err(error) => {
                let mut reply = Reply::json(json!({"detail": error.message}));
                reply.status = error.status;
                reply
            }
        };
        if reply.body.len() > MAX_RESPONSE_BYTES {
            return Err(zplugin::Error::PluginStatus(RESPONSE_TOO_LARGE));
        }
        let mut headers = BTreeMap::from([("cache-control".to_owned(), "no-store".to_owned())]);
        for name in ["content-type", "www-authenticate"] {
            if let Some(value) = reply
                .headers
                .get(name)
                .and_then(|value| value.to_str().ok())
            {
                headers.insert(name.to_owned(), value.to_owned());
            }
        }
        let response = Response {
            schema: RESPONSE_SCHEMA.to_owned(),
            status: reply.status,
            headers,
            body: serde_json::from_slice(&reply.body)
                .map_err(|_| zplugin::Error::PluginStatus(STATE_UNAVAILABLE))?,
        };
        let bytes = serde_json::to_vec(&response)
            .map_err(|_| zplugin::Error::PluginStatus(STATE_UNAVAILABLE))?;
        if bytes.len() > MAX_RESPONSE_BYTES {
            return Err(zplugin::Error::PluginStatus(RESPONSE_TOO_LARGE));
        }
        Ok(bytes)
    }
}

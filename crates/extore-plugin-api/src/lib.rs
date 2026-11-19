//! The versioned native interface between Extore and an application host.
//!
//! Native loading requires a trusted Extore artifact on the same target ABI.
//! This crate has no dependency on the Extore database or application runtime.

use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, path::PathBuf};

pub use zplugin;

pub const CONFIG_SCHEMA_VERSION: u32 = 1;
pub const RESPONSE_SCHEMA: &str = "extore.plugin-response.v1";
pub const MAX_CONFIG_BYTES: usize = 16 * 1024;
pub const MAX_RESPONSE_BYTES: usize = 16 * 1024 * 1024;

pub const NOT_CONFIGURED: i32 = 1001;
pub const INVALID_CONFIG: i32 = 1002;
pub const STATE_UNAVAILABLE: i32 = 1003;
pub const RESPONSE_TOO_LARGE: i32 = 1004;
pub const ALREADY_CONFIGURED: i32 = 1005;

/// Operator-owned configuration for one existing, independently owned Extore store.
/// This must never contain a Core database URL or be populated from browser input.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub schema_version: u32,
    pub data_dir: PathBuf,
    pub origin: String,
}

/// The existing commerce response, including its authorization failure status.
/// The body preserves Extore's existing catalog and error JSON contracts.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Response {
    pub schema: String,
    pub status: u16,
    pub headers: BTreeMap<String, String>,
    pub body: serde_json::Value,
}

/// Version 1 only reads the catalog approved by an existing commerce grant.
/// OAuth consent, refresh, revocation, card issuance and funds stay at their
/// existing application boundaries. Returned bytes release in the plugin image.
#[zplugin::interface(id = "extore.commerce", version = 1)]
pub trait Commerce {
    fn configure(&mut self, config: &[u8]) -> zplugin::Result<()>;
    fn catalog(&mut self, access_token: &str) -> zplugin::Result<zplugin::OwnedBytes>;
}

//! A trusted native Extore catalog plugin. OAuth, shop authorization and SQLite
//! remain in the Extore service. This crate neither opens a database nor issues cards.

use extore_plugin_api::{
    ALREADY_CONFIGURED, Commerce, CommerceExport, DENIED, MAX_CATALOG_BYTES, MAX_CERTIFICATE_BYTES,
    NOT_CONFIGURED, PROTOCOL, UNAVAILABLE,
};
use reqwest::{Certificate, Url, blocking::Client, redirect::Policy};
use std::{io::Read, time::Duration};
use zplugin::{Error, OwnedBytes, Result};

pub struct CommerceClient {
    origin: String,
    http: Client,
}

impl CommerceClient {
    pub fn new(origin: &str, root_certificate_pem: &[u8]) -> Result<Self> {
        let url = Url::parse(origin).map_err(|_| Error::InvalidArgument)?;
        let host = url.host_str().ok_or(Error::InvalidArgument)?;
        if url.scheme() != "https"
            || !url.username().is_empty()
            || url.password().is_some()
            || url.query().is_some()
            || url.fragment().is_some()
            || url.path() != "/"
            || url.port_or_known_default() != Some(443)
            || host.parse::<std::net::IpAddr>().is_ok()
            || !host.contains('.')
            || host.ends_with('.')
            || [
                ".localhost",
                ".local",
                ".internal",
                ".lan",
                ".home",
                ".onion",
            ]
            .iter()
            .any(|suffix| host.ends_with(suffix))
            || root_certificate_pem.len() > MAX_CERTIFICATE_BYTES
        {
            return Err(Error::InvalidArgument);
        }
        let normalized = format!("https://{host}");
        if normalized != origin {
            return Err(Error::InvalidArgument);
        }
        let mut builder = Client::builder()
            .timeout(Duration::from_secs(20))
            .connect_timeout(Duration::from_secs(5))
            .redirect(Policy::none())
            .no_proxy();
        if !root_certificate_pem.is_empty() {
            let certificate =
                Certificate::from_pem(root_certificate_pem).map_err(|_| Error::InvalidArgument)?;
            builder = builder.add_root_certificate(certificate);
        }
        let http = builder
            .build()
            .map_err(|_| Error::PluginStatus(UNAVAILABLE))?;
        Ok(Self {
            origin: normalized,
            http,
        })
    }

    pub fn read_catalog(&self, access_token: &str) -> Result<Vec<u8>> {
        if access_token.is_empty()
            || access_token.len() > 512
            || !access_token
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
        {
            return Err(Error::InvalidArgument);
        }
        let response = self
            .http
            .get(format!(
                "{}/api/integrations/commerce/products",
                self.origin
            ))
            .header(reqwest::header::ACCEPT, "application/json")
            .bearer_auth(access_token)
            .send()
            .map_err(|_| Error::PluginStatus(UNAVAILABLE))?;
        if matches!(response.status().as_u16(), 401 | 403) {
            return Err(Error::PluginStatus(DENIED));
        }
        if response.status() != reqwest::StatusCode::OK {
            return Err(Error::PluginStatus(UNAVAILABLE));
        }
        if response
            .content_length()
            .is_some_and(|length| length > MAX_CATALOG_BYTES as u64)
        {
            return Err(Error::PluginStatus(PROTOCOL));
        }
        let mut bytes = Vec::new();
        response
            .take((MAX_CATALOG_BYTES + 1) as u64)
            .read_to_end(&mut bytes)
            .map_err(|_| Error::PluginStatus(UNAVAILABLE))?;
        if bytes.len() > MAX_CATALOG_BYTES {
            return Err(Error::PluginStatus(PROTOCOL));
        }
        // Keep JSON integers intact. The LMM consumer also applies its existing
        // issuer, grant, shop, product and source-listing validation.
        let value: serde_json::Value =
            serde_json::from_slice(&bytes).map_err(|_| Error::PluginStatus(PROTOCOL))?;
        if value.get("schema").and_then(|value| value.as_str())
            != Some("extore.commerce-catalog.v1")
            || value.get("issuer").and_then(|value| value.as_str()) != Some(self.origin.as_str())
        {
            return Err(Error::PluginStatus(PROTOCOL));
        }
        Ok(bytes)
    }
}

#[derive(Default)]
struct Implementation {
    client: Option<CommerceClient>,
}

impl Commerce for Implementation {
    fn configure(&mut self, origin: &str, root_certificate_pem: &[u8]) -> Result<()> {
        if self.client.is_some() {
            return Err(Error::PluginStatus(ALREADY_CONFIGURED));
        }
        self.client = Some(CommerceClient::new(origin, root_certificate_pem)?);
        Ok(())
    }

    fn catalog(&mut self, access_token: &str) -> Result<OwnedBytes> {
        let client = self
            .client
            .as_ref()
            .ok_or(Error::PluginStatus(NOT_CONFIGURED))?;
        Ok(OwnedBytes::from_vec(client.read_catalog(access_token)?))
    }
}

fn create() -> Result<Implementation> {
    Ok(Implementation::default())
}

// Reqwest owns runtime/TLS resources. Keep the image pinned for this process;
// object and response allocations are still released through their owners.
zplugin::export!(
    CommerceExport<Implementation>,
    constructor = create,
    version = (1, 0, 0),
    unload = pinned
);

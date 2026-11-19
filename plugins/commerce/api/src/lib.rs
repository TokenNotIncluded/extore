#![no_std]

//! Extore owns the HTTP/OAuth commerce authority; this plugin reads its catalog.
//! Method order and signatures are the application v1 ABI.

pub const MAX_CATALOG_BYTES: usize = 16 * 1024 * 1024;
pub const MAX_CERTIFICATE_BYTES: usize = 256 * 1024;
pub const DENIED: i32 = 1001;
pub const UNAVAILABLE: i32 = 1002;
pub const PROTOCOL: i32 = 1003;
pub const NOT_CONFIGURED: i32 = 1004;
pub const ALREADY_CONFIGURED: i32 = 1005;

#[zplugin::interface(id = "extore.commerce", version = 1)]
pub trait Commerce {
    fn configure(&mut self, origin: &str, root_certificate_pem: &[u8]) -> zplugin::Result<()>;
    fn catalog(&mut self, access_token: &str) -> zplugin::Result<zplugin::OwnedBytes>;
}

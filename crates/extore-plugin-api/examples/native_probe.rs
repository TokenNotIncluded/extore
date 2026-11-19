//! A separately linked host used by scripts/test-native-plugin.py.
//! Credentials arrive only over stdin; they never appear in process arguments.
use extore_plugin_api::{CommerceApi, Config, MAX_CONFIG_BYTES, Response, zplugin};
use serde::Deserialize;
use serde_json::json;
use std::io::{self, BufRead, Read, Write};
use zplugin::{Plugin, UnloadPolicy};

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
enum Request {
    Configure { config: Config },
    Catalog { access_token: String },
}

#[zplugin::interface(id = "extore.commerce", version = 2)]
trait FutureCommerce {
    fn configure(&mut self, config: &[u8]) -> zplugin::Result<()>;
    fn catalog(&mut self, access_token: &str) -> zplugin::Result<zplugin::OwnedBytes>;
}

#[zplugin::interface(id = "extore.commerce", version = 1)]
trait WrongSignature {
    fn configure(&mut self, config: &[u8]) -> zplugin::Result<()>;
    fn catalog(&mut self, access_token: &[u8]) -> zplugin::Result<zplugin::OwnedBytes>;
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let path = std::env::args_os()
        .nth(1)
        .ok_or("provide a trusted Extore plugin path")?;
    // SAFETY: the verification script supplies the independently built Extore
    // artifact from this checkout on the same Linux GNU target.
    let library = unsafe { Plugin::load_path(path, UnloadPolicy::Pinned)? };
    if std::env::args_os().nth(2).as_deref() == Some(std::ffi::OsStr::new("--check-abi")) {
        let version_rejected = matches!(
            FutureCommerceApi::create(&library),
            Err(zplugin::Error::InterfaceVersion { .. })
        );
        let signature_rejected = matches!(
            WrongSignatureApi::create(&library),
            Err(zplugin::Error::SignatureMismatch { .. })
        );
        if !version_rejected || !signature_rejected {
            return Err("incompatible application interface was accepted".into());
        }
        println!(
            "{}",
            json!({"wrong_version_rejected": true, "wrong_signature_rejected": true})
        );
        return Ok(());
    }
    let mut commerce = CommerceApi::create(&library)?;
    let stdin = io::stdin();
    let mut input = stdin.lock();
    let stdout = io::stdout();
    let mut output = stdout.lock();
    loop {
        let mut line = Vec::new();
        let count = Read::by_ref(&mut input)
            .take((MAX_CONFIG_BYTES + 1024) as u64)
            .read_until(b'\n', &mut line)?;
        if count == 0 {
            break;
        }
        if line.last() != Some(&b'\n') {
            return Err("probe input exceeds the request limit or is incomplete".into());
        }
        let result = match serde_json::from_slice::<Request>(&line)? {
            Request::Configure { config } => {
                match commerce.configure(&serde_json::to_vec(&config)?) {
                    Ok(()) => json!({"ok": true}),
                    Err(error) => json!({"plugin_status": error.status()}),
                }
            }
            Request::Catalog { access_token } => match commerce.catalog(&access_token) {
                Ok(buffer) => {
                    let response: Response = serde_json::from_slice(&buffer)?;
                    serde_json::to_value(response)?
                }
                Err(error) => json!({"plugin_status": error.status()}),
            },
        };
        serde_json::to_writer(&mut output, &result)?;
        output.write_all(b"\n")?;
        output.flush()?;
    }
    Ok(())
}

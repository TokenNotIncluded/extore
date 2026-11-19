use extore::plugin_runtime::CommerceRuntime;
use extore_plugin_api::{ALREADY_CONFIGURED, Commerce, CommerceExport, NOT_CONFIGURED, zplugin};

#[derive(Default)]
struct ExtoreCommerce {
    runtime: Option<CommerceRuntime>,
}

impl Commerce for ExtoreCommerce {
    fn configure(&mut self, config: &[u8]) -> zplugin::Result<()> {
        if self.runtime.is_some() {
            return Err(zplugin::Error::PluginStatus(ALREADY_CONFIGURED));
        }
        self.runtime = Some(CommerceRuntime::configure(config)?);
        Ok(())
    }

    fn catalog(&mut self, access_token: &str) -> zplugin::Result<zplugin::OwnedBytes> {
        let runtime = self
            .runtime
            .as_ref()
            .ok_or(zplugin::Error::PluginStatus(NOT_CONFIGURED))?;
        Ok(zplugin::OwnedBytes::from_vec(
            runtime.catalog(access_token)?,
        ))
    }
}

fn create() -> zplugin::Result<ExtoreCommerce> {
    Ok(ExtoreCommerce::default())
}

// No unload-safe promise is made for Extore and its native dependencies.
zplugin::export!(
    CommerceExport<ExtoreCommerce>,
    constructor = create,
    version = (1, 0, 0),
    unload = pinned
);

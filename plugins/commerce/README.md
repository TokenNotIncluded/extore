# Extore Commerce plugin

This workspace exports Extore's catalog client through the published Project Z
zplugin 0.3.1 ABI. The Extore HTTP service keeps OAuth authorization, shop and
product scope, SQLite and issuance authority. The plugin does not open a database
or issue cards.

The interface crate defines extore.commerce v1. The plugin performs a bounded
HTTPS catalog request with redirects and environment proxies disabled. An
optional operator-provided root certificate supports private installations and
isolated real-service tests; certificate verification stays enabled.

The host bridge calls zplugin's actual loader and generated typed proxy. A Go
consumer must create, call and close it on one OS thread. Plugin response buffers
are released through their originating allocator after a single catalog call;
the bridge's copied output must also be released exactly once. The native image
uses zplugin's pinned policy and remains resident until process exit. No hot
reload or successful image unload is claimed.

Build on the developer or official build machine:

```sh
cargo build --manifest-path plugins/commerce/Cargo.toml --release --locked
```

The resulting libextore_plugin_host.so and libextore_commerce_plugin.so are
separate artifacts. The host and plugin must be independently built and tested
through the actual consumer before production activation. A library file, build
or ABI handshake alone does not prove authorized catalog behavior.

zplugin is MIT OR Apache-2.0; this Extore workspace remains MIT. zplugin 0.3.1
reuses zsys 0.1.1 for native loading. The zcall and zmcpio protocol domains are
not needed by this catalog interface.

## Scope and instance lifetime

Extore already has a Rust HTTP runtime. This additional library workspace exposes
its commerce catalog client; it does not embed or move the full server, replace
OAuth authorization, or open its SQLite database. No existing data is migrated.
The service remains the authority for grant scope, shop ownership and revocation;
the consumer still validates the issuer, grant, shop and listing contracts.

The bridge retains one verified plugin image per OS thread. Reopening that same
path creates a new independently configured instance, including for a different
origin. Selecting a different plugin path on that thread is an error. Each
request can therefore close its client and proxy without accumulating pinned
loader references or retaining a cache of user origins. The C header documents
the bridge's numeric errors; an enabled plugin failure must be visible to callers.

See [THIRD_PARTY.md](THIRD_PARTY.md) for the pinned dependency record.

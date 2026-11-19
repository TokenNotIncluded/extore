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

## Development verification

Use the same fixed Rust toolchain for the server and the libraries:

```sh
cargo +1.100.0 build --locked
cargo +1.100.0 build --manifest-path plugins/commerce/Cargo.toml --release --locked
python3 tests/native_plugin_probe.py \
  --binary target/debug/extore \
  --host-library plugins/commerce/target/release/libextore_plugin_host.so \
  --plugin-library plugins/commerce/target/release/libextore_commerce_plugin.so \
  --report output/native-plugin-component.json
```

The probe needs Linux user/network namespaces, `ip` and `openssl`. It creates a
separate network namespace, binds its TLS bridge there, and uses a private CA with
a server leaf certificate. Certificate checks stay enabled. The bridge forwards
every business request to the real Rust service. The fixture seeds only shop
identities and browser sessions; products, consent, tokens and revocation use
HTTP. It never reads an existing merchant database.

The component probe checks real loading and interface rejection, independent
instances, one HTTP catalog read per native call, explicit output release and
instance close, an invalid and revoked token, approved product filtering, and
another shop's denied detail read. Its report does not claim that the pinned
image was unloaded, that Core settlement was tested, or that a production
deployment was accepted.

A precompiled LMM consumer test may be passed after `--consumer`. It receives
`EXTORE_PLUGIN_PROBE_CONFIG`, a temporary private JSON file, while the real fixture
is running. The consumer must exercise LMM's own catalog and Store authorization
boundary, then write secret-free facts to the supplied `report_path`. Consumer
results remain separate in the report. A native component pass alone is
insufficient for accepting the LMM integration.

## Distribution notices

Generate notices from the exact locked graph for the binary target. For example:

```sh
cargo +1.100.0 metadata --locked --format-version 1 \
  --filter-platform x86_64-unknown-linux-gnu \
  --manifest-path plugins/commerce/Cargo.toml > plugin-metadata.json
python3 plugins/commerce/scripts/collect_notices.py \
  --metadata plugin-metadata.json --output-dir output/licenses/extore
```

The collector does not compile code or fetch packages. It copies original
LICENSE, COPYING and NOTICE text, including nested notices, from the downloaded
packages reachable in that resolved graph. Missing texts or registry checksums
fail the operation. Its two fixed output names are `THIRD-PARTY-NATIVE.txt` and
`THIRD-PARTY-NATIVE.json`; the latter records package versions, licenses,
checksums and source notice hashes without local registry paths. Include both
alongside the distributed libraries.

## Alpine development image

GNU and musl libraries belong to separate artifact groups. Rust 1.100.0's
[x86_64 musl target](https://github.com/rust-lang/rust/blob/1.100.0/compiler/rustc_target/src/spec/targets/x86_64_unknown_linux_musl.rs)
defaults to a static C runtime. The test image selects a dynamic musl runtime for
the shared libraries and uses Alpine's native C linker:

```text
-C target-feature=-crt-static -C link-self-contained=no -C linker=cc
```


The test-only [Alpine Dockerfile](../../tests/native_plugin_alpine.Dockerfile)
pins Rust 1.100.0 / Alpine 3.22 and Alpine 3.22 image digests. Its two targets share
the native library build. For an amd64 builder:

```sh
docker build --platform linux/amd64 \
  -f tests/native_plugin_alpine.Dockerfile \
  --build-arg EXTORE_RUST_TARGET=x86_64-unknown-linux-musl \
  --target native-export --output type=local,dest=output/native-musl .
docker build --platform linux/amd64 \
  -f tests/native_plugin_alpine.Dockerfile \
  --build-arg EXTORE_RUST_TARGET=x86_64-unknown-linux-musl \
  --target fixture -t extore-plugin-fixture:review .
```


For an arm64 builder, select `linux/arm64` and
`aarch64-unknown-linux-musl`. A target's successful build does not
establish that it passed runtime acceptance.

The `native-export` target emits the two libraries and
`licenses/extore/THIRD-PARTY-NATIVE.{txt,json}`.
The `fixture` target also builds the existing Extore server and provides
Python, OpenSSL and the network utility used by the real-service probe. No
application source or database format is changed.

Run the fixture in a fresh container network namespace with no published ports.
The `--inside-netns` entry point requires the caller's observed parent
network namespace identifier and refuses an identical namespace. Only the
container's network administration capability is needed to bring up its own
loopback device; a privileged container is unnecessary. Use a container-only
`localhost.localdomain` host mapping, keep both library paths explicit,
and supply the prebuilt musl Go consumer after `--consumer`.
When testing an APK, install it in that temporary container and select its actual
installed library paths. The private consumer configuration includes both
library hashes so the Go host can verify the exact files before loading them.

Image creation, installation, native loading and successful business calls are
distinct validation steps. Record the image ID, binary/library hashes and real
probe result for each target before accepting that target.

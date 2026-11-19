# Dependency record

This workspace uses published crates. `Cargo.lock` records the complete dependency
graph and registry checksums; the versions below are exact direct or Project Z
dependencies. License identifiers were read from the downloaded packages with
`cargo +1.100.0 metadata --locked --format-version 1` on 2026-11-20.

| Package | Version | Purpose | License |
| --- | --- | --- | --- |
| [zplugin](https://github.com/LIghtJUNction/zplugin/tree/00d566eedcff7b2932e90b9dfeb44910c783d305) | 0.3.1 | Native loader, interface checks, typed proxy and allocation ownership | MIT OR Apache-2.0 |
| [zplugin-macros](https://github.com/LIghtJUNction/zplugin/tree/00d566eedcff7b2932e90b9dfeb44910c783d305/crates/zplugin-macros) | 0.3.1 | Compile-time interface and export generation | MIT OR Apache-2.0 |
| [zsys](https://github.com/LIghtJUNction/zsys) | 0.1.1 | zplugin's platform library loader | MIT OR Apache-2.0 |
| [reqwest](https://github.com/seanmonstar/reqwest) | 0.12.28 | Bounded, certificate-verified HTTPS catalog transport | MIT OR Apache-2.0 |
| [serde_json](https://github.com/serde-rs/json) | 1.0.154 | Validate the returned catalog envelope without changing its bytes | MIT OR Apache-2.0 |

The zplugin 0.3.1 registry checksum is
`094cd57c6b7759cbbf7834e96aed75c9d025558a7d2cc4b742e78b33f8e7a448`.
Its published source commit is
`00d566eedcff7b2932e90b9dfeb44910c783d305`; the workspace consumes the
registry release, rather than a local Project Z checkout.

Extore's source remains MIT-licensed. Package distributors must include the
license texts and notices for the dependencies actually bundled in their
artifacts. The locked graph also contains platform and build dependencies, so this
short table is not a complete binary notice bundle. Neither zcall nor zmcpio is
added: this catalog boundary performs HTTPS requests and exposes no AI or MCP API.

Resolving these packages and checking their metadata does not establish that the
plugin, the Go host, or a production deployment has passed runtime acceptance.

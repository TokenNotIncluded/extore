# Test-only image: the existing Rust service and native catalog libraries.
# Native code uses the same musl C library as the Alpine Go consumer.
FROM rust:1.100.0-alpine3.22@sha256:f8ddfd4b265d9a9e7fd7c36821af4a55771aee2ac3894294687a66c709319be9 AS native-build
ARG EXTORE_RUST_TARGET
RUN apk add --no-cache build-base openssl-dev python3
WORKDIR /work
COPY . .
ENV CARGO_TARGET_DIR=/target
ENV CARGO_BUILD_JOBS=8
ENV RUSTFLAGS="-C target-feature=-crt-static -C link-self-contained=no -C linker=cc"
RUN test -n "$EXTORE_RUST_TARGET" && rustup target add "$EXTORE_RUST_TARGET" --toolchain 1.100.0
RUN rustc +1.100.0 -vV && cc -dumpmachine
RUN cargo +1.100.0 build --locked --release --target "$EXTORE_RUST_TARGET" --manifest-path plugins/commerce/Cargo.toml
RUN mkdir -p /native-out/licenses/extore && cp "/target/$EXTORE_RUST_TARGET/release/libextore_plugin_host.so" "/target/$EXTORE_RUST_TARGET/release/libextore_commerce_plugin.so" /native-out/
RUN cargo +1.100.0 metadata --locked --format-version 1 --filter-platform "$EXTORE_RUST_TARGET" --manifest-path plugins/commerce/Cargo.toml > /tmp/extore-plugin-metadata.json
RUN python3 plugins/commerce/scripts/collect_notices.py --metadata /tmp/extore-plugin-metadata.json --output-dir /native-out/licenses/extore

FROM scratch AS native-export
COPY --from=native-build /native-out/ /

FROM native-build AS fixture-build
ARG EXTORE_RUST_TARGET
RUN cargo +1.100.0 build --locked --release --target "$EXTORE_RUST_TARGET" --bin extore
RUN cp "/target/$EXTORE_RUST_TARGET/release/extore" /native-out/extore-server

FROM alpine:3.22@sha256:49340b346798875b5592e5d420241f2f9cfba5f47e62ebb1446a1b34734e898f AS fixture
RUN apk add --no-cache python3 openssl iproute2 libgcc libssl3 libcrypto3
COPY --from=fixture-build /work/ /work/
COPY --from=fixture-build /native-out/ /opt/extore/
WORKDIR /work
ENTRYPOINT ["python3", "tests/native_plugin_probe.py"]

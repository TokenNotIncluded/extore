# Rust migration checkpoint

This branch is an implementation checkpoint. It is not a production-ready release.

## Implemented in this checkpoint

- Axum HTTP entry point, SQLite schema 23 access, bounded requests, and graceful shutdown.
- Native shop authentication, encryption, passkeys, account management, product/catalog management, and OAuth commerce protocol.
- EXR1 Ed25519 card issuance and redemption routing using the existing issuer pins and tenant-bound encryption.
- Card inventory, batches, product retirement, and preservation of accepted fulfillment.
- Rust background delivery for mail, signed webhooks, and isolated reviewed processor execution.
- LMM storefront callback registration for both the existing and new architecture domains.

The reviewed Python processor SDK remains an isolated extension. The HTTP service and worker orchestration are being replaced by Rust.

## Verification status

The dependency build completed. The first `cargo check` stopped at four modules still being implemented: CLI authorization, processor profiles, file handling, and fulfillment. No successful Rust build or production cutover is claimed.

Existing Python contract tests and the new UI checks were run in the earlier recovery/UI checkpoints. Those results do not establish Rust API compatibility.

## Required before production activation

Complete the remaining modules, run a native build, exercise independent-database migration and end-to-end authorization/redemption/store-sync cases, verify backup restoration, and update the deployment packaging to the Rust binary. Keep the current production service until these gates pass.

#!/usr/bin/env python3
"""Exercise the real native ABI and Extore authorization on isolated state."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import selectors
import subprocess
import time
from uuid import UUID

from native_plugin_fixture import ExtoreFixture, digest


class Probe:
    def __init__(self, executable: Path, plugin: Path):
        self.process = subprocess.Popen(
            [str(executable), str(plugin)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def call(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        if not self.selector.select(timeout=30):
            raise TimeoutError("native probe did not respond")
        line = self.process.stdout.readline(16 * 1024 * 1024 + 1024)
        if not line.endswith("\n"):
            raise AssertionError("native probe returned no complete bounded response")
        return json.loads(line)

    def catalog(self, token):
        return self.call({"op": "catalog", "access_token": token})

    def configure(self, config):
        return self.call({"op": "configure", "config": config})

    def close(self):
        self.selector.close()
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        if self.process.stdout:
            self.process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def sha256(path):
    result = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    for name in ["binary", "plugin", "probe"]:
        setattr(args, name, getattr(args, name).resolve(strict=True))
    started = time.monotonic()
    passed = []

    abi = subprocess.run([str(args.probe), str(args.plugin), "--check-abi"],
                         text=True, capture_output=True, check=True, timeout=20)
    assert json.loads(abi.stdout) == {
        "wrong_version_rejected": True, "wrong_signature_rejected": True}
    passed += ["independent_native_load", "interface_version_rejected", "method_signature_rejected"]

    with ExtoreFixture(args.binary) as fixture:
        status, _, shops = fixture.request("GET", "/api/platform/shops", admin=True)
        assert status == 200
        assert {fixture.shop_a, fixture.shop_b}.issubset({shop["id"] for shop in shops})
        assert all(str(UUID(shop["id"])) == shop["id"] for shop in shops)
        passed += ["browser_shop_identifier_contract"]

        authorization = fixture.authorize()
        limited = fixture.authorize(scopes=["marketplace.sync"])
        expired = fixture.authorize()
        other_shop = fixture.authorize(fixture.shop_b)
        config = fixture.config()

        with Probe(args.probe, args.plugin) as probe:
            assert probe.catalog(authorization["access_token"]) == {"plugin_status": 1001}
            assert probe.configure({**config, "schema_version": 2}) == {"plugin_status": 1002}
            assert probe.configure({**config, "data_dir": "relative-state"}) == {"plugin_status": 1002}
            absent = fixture.root / "absent-state"
            assert probe.configure({**config, "data_dir": str(absent)}) == {"plugin_status": 1003}
            assert not absent.exists()
            empty_state = fixture.root / "empty-state"
            empty_state.mkdir()
            assert probe.configure({**config, "data_dir": str(empty_state)}) == {"plugin_status": 1003}
            assert not (empty_state / "extore.sqlite3").exists()
            with fixture.connection() as db:
                db.execute("PRAGMA user_version=22")
            try:
                assert probe.configure(config) == {"plugin_status": 1003}
                with fixture.connection() as db:
                    assert db.execute("PRAGMA user_version").fetchone()[0] == 22
            finally:
                with fixture.connection() as db:
                    db.execute("PRAGMA user_version=23")
            key_path = fixture.data_dir / "master-secrets.key"
            original_key = key_path.read_bytes()
            key_path.write_bytes(b"x" * 32)
            try:
                assert probe.configure(config) == {"plugin_status": 1003}
            finally:
                key_path.write_bytes(original_key)
            assert probe.configure(config) == {"ok": True}
            assert probe.configure(config) == {"plugin_status": 1005}
            passed += ["configure_required", "invalid_configuration_rejected",
                       "missing_state_not_created", "existing_directory_without_database_rejected",
                       "unsupported_schema_not_migrated", "master_key_fingerprint_checked",
                       "configured_store_is_immutable"]

            first = probe.catalog(authorization["access_token"])
            status, _, http = fixture.request("GET", "/api/integrations/commerce/products",
                                              bearer=authorization["access_token"])
            assert first["schema"] == "extore.plugin-response.v1"
            assert first["status"] == status == 200 and first["body"] == http
            assert first["headers"]["cache-control"] == "no-store"
            assert http["shop"]["id"] == fixture.shop_a
            assert [p["id"] for p in http["products"]] == [fixture.products[fixture.shop_a]]
            assert http["products"][0]["variants"][0]["price"] == "12.3400"
            assert "PRIVATE_" not in json.dumps(first)
            repeated = probe.catalog(authorization["access_token"])
            assert repeated["body"] == first["body"]
            assert probe.catalog(other_shop["access_token"])["body"]["shop"]["id"] == fixture.shop_b
            passed += ["http_catalog_parity", "grant_product_subset", "shop_catalog_isolation",
                       "private_fields_filtered", "decimal_price_preserved", "stable_revision"]

            with fixture.connection() as db:
                raw = db.execute("SELECT config FROM products WHERE id=?",
                                 (fixture.products[fixture.shop_a],)).fetchone()[0]
                product = json.loads(raw)
                product["description"] = "catalog version two"
                db.execute("UPDATE products SET config=? WHERE id=?",
                           (json.dumps(product), fixture.products[fixture.shop_a]))
            changed = probe.catalog(authorization["access_token"])
            before = first["body"]["products"][0]
            after = changed["body"]["products"][0]
            assert after["revision"] != before["revision"]
            assert after["sync_revision"] != before["sync_revision"]
            status, _, http = fixture.request("GET", "/api/integrations/commerce/products",
                                              bearer=authorization["access_token"])
            assert status == 200 and changed["body"] == http
            passed += ["live_revision_change"]

            insufficient = probe.catalog(limited["access_token"])
            assert insufficient["status"] == 403
            assert insufficient["body"]["error"] == "insufficient_scope"
            assert 'insufficient_scope' in insufficient["headers"]["www-authenticate"]
            assert probe.catalog("invalid")["status"] == 401
            assert probe.catalog("x" * 43)["status"] == 401
            with fixture.connection() as db:
                db.execute("UPDATE commerce_tokens SET expires=0 WHERE digest=?",
                           (digest(expired["access_token"]),))
            assert probe.catalog(expired["access_token"])["status"] == 401
            passed += ["scope_enforced", "invalid_token_rejected", "expired_token_rejected"]

            # Simulate a product moved to another shop after the grant was approved.
            with fixture.connection() as db:
                db.execute("UPDATE products SET shop_id=? WHERE id=?",
                           (fixture.shop_b, fixture.products[fixture.shop_a]))
            cross_shop = probe.catalog(authorization["access_token"])
            assert cross_shop["status"] == 409
            assert cross_shop["body"]["error"] == "product_unavailable"
            with fixture.connection() as db:
                db.execute("UPDATE products SET shop_id=? WHERE id=?",
                           (fixture.shop_a, fixture.products[fixture.shop_a]))
            passed += ["changed_product_ownership_rejected"]

            fixture.revoke(authorization)
            revoked = probe.catalog(authorization["access_token"])
            assert revoked["status"] == 401
            assert revoked["body"]["error"] == "invalid_token"
            with fixture.connection() as db:
                assert db.execute("SELECT revoked FROM commerce_grants WHERE id=?",
                                  (authorization["grant_id"],)).fetchone()[0] == 1
                assert db.execute("SELECT COUNT(*) FROM commerce_tokens WHERE grant_id=? AND kind='access'",
                                  (authorization["grant_id"],)).fetchone()[0] == 0
            passed += ["real_http_revocation_visible_in_loaded_instance"]

            database = fixture.data_dir / "extore.sqlite3"
            database.rename(fixture.data_dir / "removed.sqlite3")
            assert probe.catalog(other_shop["access_token"]) == {"plugin_status": 1003}
            assert not database.exists()
            with Probe(args.probe, args.plugin) as fresh_probe:
                assert fresh_probe.configure(config) == {"plugin_status": 1003}
            assert not database.exists()
            passed += ["removed_database_not_recreated",
                       "configure_does_not_recreate_removed_database"]

    receipt = {
        "result": "pass", "checks": passed, "elapsed_seconds": round(time.monotonic() - started, 3),
        "artifacts": {name: {"path": str(getattr(args, name)), "sha256": sha256(getattr(args, name))}
                      for name in ["binary", "plugin", "probe"]},
        "scope": "Linux GNU native ABI, isolated SQLite state, real Rust HTTP PKCE and revoke",
        "fixtures": "seeded administrator session, shops and products; no production state",
    }
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()

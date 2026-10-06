"""Adding tenant boundaries must preserve legacy root credentials and rows."""

import sqlite3
import time

from extore.db import SCHEMA
from extore.shops import default_shop, init_schema


def legacy_connection():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(SCHEMA)
    c.execute("CREATE TABLE owner_cli_devices (id TEXT PRIMARY KEY,public_key TEXT)")
    c.execute("CREATE TABLE owner_cli_requests (id TEXT PRIMARY KEY,public_key TEXT)")
    return c


def test_tenant_migration_preserves_root_credentials_sessions_and_product_config():
    c = legacy_connection()
    config = '{"name":"Historical product","mode":"manual"}'
    c.execute(
        "INSERT INTO products(id,config,created) VALUES ('p',?,?)", (config, 123.0)
    )
    c.execute(
        "INSERT INTO credentials(id,public_key,sign_count,name,created) VALUES ('root-key',x'0102',7,'Root key',100)"
    )
    c.execute(
        "INSERT INTO sessions(digest,role,expires,created) VALUES ('root-session','admin',?,100)",
        (time.time() + 1000,),
    )
    c.execute("INSERT INTO owner_cli_devices VALUES ('root-device','device-key')")
    c.execute("INSERT INTO owner_cli_requests VALUES ('root-request','request-key')")
    init_schema(c)
    product = c.execute("SELECT * FROM products WHERE id='p'").fetchone()
    root = c.execute("SELECT * FROM credentials WHERE id='root-key'").fetchone()
    assert product["config"] == config and product["created"] == 123
    assert product["shop_id"] == default_shop(c)
    assert tuple(
        root[name] for name in ("id", "public_key", "sign_count", "name", "created")
    ) == ("root-key", b"\x01\x02", 7, "Root key", 100)
    assert root["shop_id"] is None
    assert (
        c.execute(
            "SELECT shop_id FROM sessions WHERE digest='root-session'"
        ).fetchone()[0]
        is None
    )
    assert (
        c.execute(
            "SELECT shop_id FROM owner_cli_devices WHERE id='root-device'"
        ).fetchone()[0]
        is None
    )
    assert (
        c.execute(
            "SELECT shop_id FROM owner_cli_requests WHERE id='root-request'"
        ).fetchone()[0]
        is None
    )
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    c.close()


def test_tenant_migration_is_idempotent_and_keeps_explicit_tenant_assignment():
    c = legacy_connection()
    init_schema(c)
    historical = default_shop(c)
    c.execute(
        "INSERT INTO shops(id,name,email,created,verified) VALUES ('shop-b','B','b@example.test',0,1)"
    )
    c.execute(
        "INSERT INTO products(id,config,created,shop_id) VALUES ('b','{}',0,'shop-b')"
    )
    init_schema(c)
    assert default_shop(c) == historical
    assert c.execute("SELECT count(*) FROM shops WHERE legacy=1").fetchone()[0] == 1
    assert (
        c.execute("SELECT shop_id FROM products WHERE id='b'").fetchone()[0] == "shop-b"
    )
    assert c.execute("SELECT count(*) FROM account_tokens").fetchone()[0] == 0
    c.close()


def test_missing_legacy_setting_reuses_existing_legacy_shop():
    c = legacy_connection()
    init_schema(c)
    historical = default_shop(c)
    c.execute("DELETE FROM settings WHERE key='legacy_shop_id'")
    assert default_shop(c) == historical
    assert c.execute("SELECT count(*) FROM shops").fetchone()[0] == 1
    c.close()

import json
import sqlite3
import time

import pytest
from fastapi import HTTPException

from extore import db as database
from extore.security import staff_authorization


def test_legacy_staff_links_migrate_without_escalating_permissions(
    tmp_path, monkeypatch
):
    original_data = database.DATA
    database_path = tmp_path / "extore.sqlite3"
    now = time.time()
    with sqlite3.connect(database_path) as c:
        c.executescript(
            """
            CREATE TABLE products (
                id TEXT PRIMARY KEY,
                config TEXT NOT NULL,
                created REAL NOT NULL
            );
            CREATE TABLE staff (
                id TEXT PRIMARY KEY,
                digest TEXT UNIQUE NOT NULL,
                product_id TEXT NOT NULL REFERENCES products(id),
                name TEXT NOT NULL,
                expires REAL NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE sessions (
                digest TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                staff_id TEXT REFERENCES staff(id),
                expires REAL NOT NULL,
                created REAL NOT NULL
            );
            PRAGMA user_version=1;
            """
        )
        c.execute(
            "INSERT INTO products(id,config,created) VALUES (?,?,?)",
            ("product", "{}", now),
        )
        c.execute(
            "INSERT INTO staff VALUES (?,?,?,?,?,?)",
            ("legacy", "legacy-digest", "product", "原有链接", now + 86400, 0),
        )
        c.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?)",
            ("session-digest", "staff", "legacy", now + 3600, now),
        )

    with monkeypatch.context() as patch:
        patch.setattr(database, "DATA", tmp_path)
        database.init()
        database.init()
        with database.db() as c:
            assert c.execute("PRAGMA user_version").fetchone()[0] == 18
            assert "result_json" in {
                row["name"] for row in c.execute("PRAGMA table_info(jobs)")
            }
            assert {
                "card_meta",
                "card_batches",
                "receipt_batches",
                "receipt_batch_cards",
            } <= {
                row["name"]
                for row in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            row = dict(c.execute("SELECT * FROM staff WHERE id='legacy'").fetchone())
            assert row == {
                "id": "legacy",
                "digest": "legacy-digest",
                "product_id": "product",
                "name": "原有链接",
                "expires": now + 86400,
                "revoked": 0,
                "permissions": '["queue.view","queue.process"]',
                "parent_id": None,
                "created": 0,
                "max_uses": 1,
                "uses": 1,
                "max_cli_uses": 1,
                "cli_uses": 0,
                "archived": 0,
            }
            assert staff_authorization(c, "legacy")["permissions"] == [
                "queue.view",
                "queue.process",
            ]
            migrated = dict(c.execute("SELECT * FROM sessions").fetchone())
            assert migrated["id"]
            assert (
                migrated["revoked"],
                migrated["last_seen"],
                migrated["ip"],
                migrated["ua"],
            ) == (0, now, "", "")
            assert {
                key: migrated[key]
                for key in ("digest", "role", "staff_id", "expires", "created")
            } == {
                "digest": "session-digest",
                "role": "staff",
                "staff_id": "legacy",
                "expires": now + 3600,
                "created": now,
            }
            assert not c.execute("PRAGMA foreign_key_check").fetchall()
    assert database.DATA == original_data


@pytest.mark.parametrize(
    "permission", ("queue.process", "queue.retry", "fulfillment.configure")
)
def test_corrupt_permissions_require_dependencies(permission):
    with database.db() as c:
        c.execute(
            "INSERT INTO products(id,config,created) VALUES (?,?,?)",
            ("product", "{}", time.time()),
        )
        c.execute(
            "INSERT INTO staff(id,digest,product_id,name,expires,permissions) VALUES (?,?,?,?,?,?)",
            (
                "broken-link",
                "broken-digest",
                "product",
                "错误配置",
                time.time() + 3600,
                json.dumps([permission]),
            ),
        )
        with pytest.raises(HTTPException) as error:
            staff_authorization(c, "broken-link")
        assert error.value.status_code == 401
        dependency = (
            "product.edit" if permission == "fulfillment.configure" else "queue.view"
        )
        c.execute(
            "UPDATE staff SET permissions=? WHERE id=?",
            (json.dumps([dependency, permission]), "broken-link"),
        )
        assert set(staff_authorization(c, "broken-link")["permissions"]) == {
            dependency,
            permission,
        }

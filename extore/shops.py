"""Shop ownership is a server-side boundary, never a customer parameter."""

import time
import uuid

from .db import set_setting, setting
from .security import fail, session


def init_schema(c):
    from .config import UPLOAD_SHOP_BYTES

    c.execute(
        "CREATE TABLE IF NOT EXISTS shops (id TEXT PRIMARY KEY,name TEXT NOT NULL,email TEXT UNIQUE,password_hash TEXT,enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),created REAL NOT NULL,verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1)),legacy INTEGER NOT NULL DEFAULT 0 CHECK(legacy IN (0,1)),totp_secret TEXT,pending_totp_secret TEXT,pending_totp_expires REAL,last_totp_counter INTEGER NOT NULL DEFAULT -1,totp_backup_digests TEXT NOT NULL DEFAULT '[]')"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS account_tokens (digest TEXT PRIMARY KEY,kind TEXT NOT NULL CHECK(kind IN ('invite','register','reset')),shop_id TEXT REFERENCES shops(id) ON DELETE CASCADE,email TEXT NOT NULL,password_hash TEXT,name TEXT,created REAL NOT NULL,expires REAL NOT NULL,used REAL)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS account_tokens_expiry ON account_tokens(expires)"
    )
    columns = {r["name"] for r in c.execute("PRAGMA table_info(shops)")}
    if "storage_limit_bytes" not in columns:
        c.execute(
            f"ALTER TABLE shops ADD COLUMN storage_limit_bytes INTEGER NOT NULL DEFAULT {int(UPLOAD_SHOP_BYTES)} CHECK(storage_limit_bytes>0)"
        )
    if "factory_slogan" not in columns:
        c.execute(
            "ALTER TABLE shops ADD COLUMN factory_slogan TEXT NOT NULL DEFAULT ''"
        )
    c.execute(
        "CREATE TABLE IF NOT EXISTS shop_integration_keys (shop_id TEXT PRIMARY KEY REFERENCES shops(id) ON DELETE CASCADE,key_digest TEXT UNIQUE NOT NULL,created REAL NOT NULL)"
    )
    for table in (
        "products",
        "credentials",
        "sessions",
        "owner_cli_devices",
        "owner_cli_requests",
    ):
        columns = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        if "shop_id" not in columns:
            c.execute(
                f"ALTER TABLE {table} ADD COLUMN shop_id TEXT REFERENCES shops(id)"
            )
    columns = {r["name"] for r in c.execute("PRAGMA table_info(sessions)")}
    if "auth_at" not in columns:
        c.execute("ALTER TABLE sessions ADD COLUMN auth_at REAL NOT NULL DEFAULT 0")
    if "auth_method" not in columns:
        c.execute(
            "ALTER TABLE sessions ADD COLUMN auth_method TEXT NOT NULL DEFAULT 'legacy'"
        )
    sid = default_shop(c)
    c.execute("UPDATE products SET shop_id=? WHERE shop_id IS NULL", (sid,))
    c.execute("CREATE INDEX IF NOT EXISTS products_shop ON products(shop_id)")
    c.execute("CREATE INDEX IF NOT EXISTS credentials_shop ON credentials(shop_id)")
    c.execute("CREATE INDEX IF NOT EXISTS sessions_shop ON sessions(shop_id)")
    job_columns = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
    if "retry_mode" not in job_columns:
        c.execute(
            "ALTER TABLE jobs ADD COLUMN retry_mode TEXT NOT NULL DEFAULT 'revise'"
        )
    if "retry_reason_type" not in job_columns:
        c.execute(
            "ALTER TABLE jobs ADD COLUMN retry_reason_type TEXT NOT NULL DEFAULT 'customer_input'"
        )


def default_shop(c):
    from .config import UPLOAD_SHOP_BYTES

    value = setting(c, "legacy_shop_id")
    if value and c.execute("SELECT 1 FROM shops WHERE id=?", (value,)).fetchone():
        return value
    row = c.execute(
        "SELECT id FROM shops WHERE legacy=1 ORDER BY created LIMIT 1"
    ).fetchone()
    if row:
        value = row["id"]
    else:
        value = str(uuid.uuid4())
        c.execute(
            "INSERT INTO shops(id,name,created,verified,legacy,storage_limit_bytes) VALUES (?,?,?,1,1,?)",
            (value, "默认店铺", time.time(), UPLOAD_SHOP_BYTES),
        )
    set_setting(c, "legacy_shop_id", value)
    return value


def scoped_shop_id(s):
    return s.get("shop_id")


def shop_row(c, shop_id, require_enabled=True):
    row = c.execute("SELECT * FROM shops WHERE id=?", (shop_id,)).fetchone()
    if row is None or (require_enabled and not row["enabled"]):
        fail("店铺不可用", 401)
    return row


def require_superadmin(request):
    s = session(request)
    if s.get("shop_id") is not None:
        fail("此操作需要平台管理权限", 403)
    return s


def require_shop_owner(request):
    s = session(request)
    if s.get("shop_id") is None:
        fail("此操作需要店铺账号", 403)
    return s


def authorize_product(c, s, product_id):
    row = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if row is None:
        fail("商品不存在", 404)
    sid = row["shop_id"] or default_shop(c)
    if s["role"] == "staff":
        if s.get("product_id") != product_id:
            fail("没有此商品的管理权限", 403)
    elif s["role"] == "admin":
        if s.get("shop_id") is not None and s["shop_id"] != sid:
            fail("没有此商品的管理权限", 403)
    else:
        fail("请先登录", 401)
    shop_row(c, sid)
    return row


def require_enabled_product(c, product_id):
    row = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if row is None:
        fail("商品不存在", 404)
    shop = c.execute(
        "SELECT enabled FROM shops WHERE id=?", (row["shop_id"],)
    ).fetchone()
    if shop is None or not shop["enabled"]:
        fail("商品不存在", 404)
    return row


def resolve_create_shop(c, s, requested_shop_id=None):
    if s["role"] != "admin":
        fail("此操作需要店铺管理权限", 403)
    own = s.get("shop_id")
    if own is not None and requested_shop_id not in (None, own):
        fail("不能为其他店铺创建商品", 403)
    sid = own or requested_shop_id or default_shop(c)
    shop_row(c, sid)
    return sid

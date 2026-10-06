import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

from .config import DATA

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS products (id TEXT PRIMARY KEY, config TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cards (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, product_id TEXT NOT NULL REFERENCES products(id), state TEXT NOT NULL DEFAULT 'ready', created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS grants (digest TEXT PRIMARY KEY, card_id TEXT NOT NULL REFERENCES cards(id), expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, card_id TEXT UNIQUE NOT NULL REFERENCES cards(id), product_id TEXT NOT NULL REFERENCES products(id), state TEXT NOT NULL, params TEXT NOT NULL, content TEXT, message TEXT NOT NULL DEFAULT '', progress INTEGER NOT NULL DEFAULT 0, attempt INTEGER NOT NULL DEFAULT 1, retryable INTEGER NOT NULL DEFAULT 0, claimed_by TEXT, lease REAL, created REAL NOT NULL, updated REAL NOT NULL, revealed INTEGER NOT NULL DEFAULT 0, result_json TEXT, progress_plan TEXT, completed_steps TEXT NOT NULL DEFAULT '[]', schema_snapshot TEXT);
CREATE TABLE IF NOT EXISTS staff (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, product_id TEXT NOT NULL REFERENCES products(id), name TEXT NOT NULL, expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, permissions TEXT NOT NULL DEFAULT '["queue.view","queue.process"]', parent_id TEXT REFERENCES staff(id), created REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions (digest TEXT PRIMARY KEY, role TEXT NOT NULL, staff_id TEXT REFERENCES staff(id), expires REAL NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS credentials (id TEXT PRIMARY KEY, public_key BLOB NOT NULL, sign_count INTEGER NOT NULL, name TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS challenges (digest TEXT PRIMARY KEY, challenge BLOB NOT NULL, kind TEXT NOT NULL, session_digest TEXT, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, type TEXT NOT NULL, job_id TEXT, product_id TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY REFERENCES events(id), url TEXT NOT NULL, secret TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS callback_nonces (nonce TEXT PRIMARY KEY, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS api_requests (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response BLOB NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS rate_limits (key TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state, created);
CREATE INDEX IF NOT EXISTS outbox_due ON outbox(state, due);
"""


@contextmanager
def db():
    c = sqlite3.connect(DATA / "extore.sqlite3", timeout=20, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA secure_delete=ON")
    c.execute("PRAGMA busy_timeout=20000")
    try:
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()


def init():
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    with db() as c:
        c.executescript(SCHEMA)
        # executescript commits the connection's previous transaction. Keep all
        # migration checks and changes together for concurrent server starts.
        c.execute("BEGIN IMMEDIATE")
        columns = {r["name"] for r in c.execute("PRAGMA table_info(staff)")}
        if "permissions" not in columns:
            c.execute(
                "ALTER TABLE staff ADD COLUMN permissions TEXT NOT NULL "
                'DEFAULT \'["queue.view","queue.process"]\''
            )
        if "parent_id" not in columns:
            c.execute(
                "ALTER TABLE staff ADD COLUMN parent_id TEXT REFERENCES staff(id)"
            )
        if "created" not in columns:
            c.execute("ALTER TABLE staff ADD COLUMN created REAL NOT NULL DEFAULT 0")
        c.execute("CREATE INDEX IF NOT EXISTS staff_parent ON staff(parent_id)")
        job_columns = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
        if "result_json" not in job_columns:
            c.execute("ALTER TABLE jobs ADD COLUMN result_json TEXT")
        if "progress_plan" not in job_columns:
            c.execute("ALTER TABLE jobs ADD COLUMN progress_plan TEXT")
        if "completed_steps" not in job_columns:
            c.execute(
                "ALTER TABLE jobs ADD COLUMN completed_steps TEXT NOT NULL DEFAULT '[]'"
            )
        if "schema_snapshot" not in job_columns:
            c.execute("ALTER TABLE jobs ADD COLUMN schema_snapshot TEXT")
        from .card_tracking import init_schema

        init_schema(c)
        from .files import init_schema as init_files_schema

        init_files_schema(c)
        from .link_access import init_schema as init_link_access

        init_link_access(c)
        if c.execute("PRAGMA user_version").fetchone()[0] < 7:
            c.execute("PRAGMA user_version=7")
    # WAL is set outside a transaction.
    with sqlite3.connect(DATA / "extore.sqlite3") as c:
        c.execute("PRAGMA journal_mode=WAL")
    (DATA / "extore.sqlite3").chmod(0o600)
    import os

    from cryptography.fernet import Fernet

    try:
        fd = os.open(DATA / "issuance.key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as file:
            file.write(Fernet.generate_key())


def setting(c, key, default=None):
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(c, key, value):
    c.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))


def audit(c, actor, action, target):
    c.execute(
        "INSERT INTO audit(actor,action,target,created) VALUES (?,?,?,?)",
        (actor, action, target, time.time()),
    )


def event(c, kind, product_id, job=None):
    eid = str(uuid.uuid4())
    payload = {
        "id": eid,
        "type": kind,
        "version": 1,
        "created_at": time.time(),
        "product_id": product_id,
    }
    if job:
        from .service import progress_view
        from .variants import card_variant

        payload["data"] = {
            k: job[k] for k in ("id", "state", "attempt", "progress", "message")
        }
        payload["data"]["variant"] = card_variant(c, job)
        payload["data"]["steps"], payload["data"]["completed_steps"] = progress_view(
            c, job
        )
        if kind == "redemption.requested":
            payload["data"]["params"] = json.loads(job["params"])
    else:
        payload["data"] = {}
    c.execute(
        "INSERT INTO events VALUES (?,?,?,?,?,?)",
        (
            eid,
            kind,
            job["id"] if job else None,
            product_id,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            time.time(),
        ),
    )
    row = c.execute("SELECT config FROM products WHERE id=?", (product_id,)).fetchone()
    p = json.loads(row["config"])
    if p.get("webhook_url"):
        c.execute(
            "INSERT INTO outbox(id,url,secret,due) VALUES (?,?,?,?)",
            (eid, p["webhook_url"], p["webhook_secret"], time.time()),
        )
    return eid

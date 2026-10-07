"""Self-declared processor identities, approved metadata and claim attribution."""

import time
import unicodedata

from .security import fail


def normalize_identity(client_name, agent_type=None, *, legacy=False):
    def clean(value, maximum, *, historical=False):
        if not isinstance(value, str) or any(
            unicodedata.category(char).startswith("C")
            or (not historical and not char.isprintable())
            for char in value
        ):
            raise ValueError("处理端名称或类型无效")
        value = value.strip()
        if not 1 <= len(value) <= maximum:
            raise ValueError("处理端名称或类型无效")
        return value

    return clean(client_name, 100), None if agent_type is None else clean(
        agent_type, 64
    )


def add_type_column(c, table):
    # Table names are fixed call-site constants, never caller-controlled.
    if "agent_type" not in {
        r["name"] for r in c.execute(f"PRAGMA table_info({table})")
    }:
        c.execute(f"ALTER TABLE {table} ADD COLUMN agent_type TEXT")


def init_schema(c):
    c.execute(
        "CREATE TABLE IF NOT EXISTS job_worker_identities ("
        "job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,"
        "attempt INTEGER NOT NULL,actor TEXT NOT NULL,device_ref TEXT,"
        "client_name TEXT NOT NULL,agent_type TEXT,channel TEXT NOT NULL "
        "CHECK(channel IN ('cli','browser','automatic')),claimed_at REAL NOT NULL,"
        "PRIMARY KEY(job_id,attempt))"
    )


def record_claim(c, row, actor, *, session=None, device_id=None):
    """Capture only a successful live claim; the management actor stays unchanged."""
    if row["state"] != "processing" or row["claimed_by"] != actor:
        fail("处理身份与领取任务不匹配", 409)
    device_id = device_id or (session or {}).get("device_id")
    if device_id:
        device = c.execute(
            "SELECT id,staff_id,client_name,agent_type,revoked FROM cli_devices WHERE id=?",
            (device_id,),
        ).fetchone()
        if device is None or device["revoked"] or device["staff_id"] != actor:
            fail("CLI 领取身份已失效", 401)
        name, agent_type = normalize_identity(
            device["client_name"], device["agent_type"]
        )
        channel, ref = "cli", device["id"]
    elif actor == "worker":
        name, agent_type, channel, ref = "商品处理器", "processor", "automatic", None
    elif (session or {}).get("role") == "admin":
        name, agent_type, channel, ref = "店主", "human", "browser", None
    else:
        staff = c.execute("SELECT name FROM staff WHERE id=?", (actor,)).fetchone()
        name = staff["name"] if staff else "处理人员"
        name = (
            "".join(char for char in name if char.isprintable())[:100].strip()
            or "处理人员"
        )
        agent_type, channel, ref = "human", "browser", None
    c.execute(
        "INSERT INTO job_worker_identities(job_id,attempt,actor,device_ref,client_name,"
        "agent_type,channel,claimed_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(job_id,attempt) "
        "DO UPDATE SET actor=excluded.actor,device_ref=excluded.device_ref,"
        "client_name=excluded.client_name,agent_type=excluded.agent_type,"
        "channel=excluded.channel,claimed_at=excluded.claimed_at",
        (row["id"], row["attempt"], actor, ref, name, agent_type, channel, time.time()),
    )


def processing_worker(c, row):
    identity = c.execute(
        "SELECT client_name,agent_type,channel FROM job_worker_identities "
        "WHERE job_id=? AND attempt=? AND actor=?",
        (row["id"], row["attempt"], row["claimed_by"]),
    ).fetchone()
    if identity is None:
        return None
    return {
        "name": identity["client_name"],
        "agent_type": identity["agent_type"],
        "kind": {"cli": "cli", "browser": "human", "automatic": "automatic"}[
            identity["channel"]
        ],
    }

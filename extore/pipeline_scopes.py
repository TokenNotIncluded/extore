"""Explicit, key-bound product snapshots for merchant-approved CLI processors.

Approval does not create links.  ``materialize`` is called only after the
request's signature and approving merchant have been checked, inside the
caller's IMMEDIATE transaction.  The resulting private links have no browser
admission and each admits only its one CLI key.
"""

import base64
import hashlib
import hmac
import json
import math
import time
import unicodedata
import uuid

from .db import audit
from .models import LINK_PERMISSIONS
from .security import digest, fail, staff_authorization, token
from .shops import shop_row

PIPELINE_PERMISSIONS = ("queue.view", "queue.process", "queue.retry")
DEFAULT_DAYS = 7
MAX_DAYS = 90
MAX_PRODUCTS = 500
MAX_ACTIVE_AUTHORIZATIONS = 10000
MAX_KEY_AUTHORIZATIONS = 20
MAX_TOTAL_AUTHORIZATIONS = 50000
MAX_KEY_TOTAL_AUTHORIZATIONS = 100


def init_schema(c):
    """Add metadata tables without changing legacy links, devices or sessions."""
    c.execute(
        "CREATE TABLE IF NOT EXISTS pipeline_authorizations ("
        "id TEXT PRIMARY KEY,shop_id TEXT NOT NULL REFERENCES shops(id),"
        "public_key TEXT NOT NULL,fingerprint TEXT NOT NULL,client_name TEXT NOT NULL,"
        "kind TEXT NOT NULL CHECK(kind IN ('product','shop.pipeline')),"
        "permissions TEXT NOT NULL,expires REAL NOT NULL,"
        "revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),"
        "revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),"
        "created REAL NOT NULL,last_seen REAL NOT NULL,approved_actor TEXT NOT NULL,"
        "issuer_role TEXT NOT NULL CHECK(issuer_role IN ('root','shop')),"
        "issuer_shop_id TEXT REFERENCES shops(id),"
        "CHECK((issuer_role='root' AND issuer_shop_id IS NULL) OR "
        "(issuer_role='shop' AND issuer_shop_id=shop_id)))"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS pipeline_bindings ("
        "authorization_id TEXT NOT NULL REFERENCES pipeline_authorizations(id),"
        "product_id TEXT NOT NULL REFERENCES products(id),"
        "staff_id TEXT NOT NULL UNIQUE REFERENCES staff(id),"
        "device_id TEXT NOT NULL UNIQUE REFERENCES cli_devices(id),"
        "created REAL NOT NULL,PRIMARY KEY(authorization_id,product_id))"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS pipeline_authorizations_shop "
        "ON pipeline_authorizations(shop_id,revoked,expires)"
    )

    from .agent_identity import add_type_column

    add_type_column(c, "pipeline_authorizations")


def _permissions(value, kind):
    allowed = (
        (*PIPELINE_PERMISSIONS, "queue.monitor")
        if kind == "shop.pipeline"
        else LINK_PERMISSIONS
    )
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(p, str) or p not in allowed for p in value)
        or len(value) != len(set(value))
        or ({"queue.process", "queue.retry"} & set(value) and "queue.view" not in value)
        or ("fulfillment.configure" in value and "product.edit" not in value)
    ):
        fail("商品流水线授权权限无效", 400)
    return [p for p in LINK_PERMISSIONS if p in value]


def _stored_permissions(row):
    try:
        value = json.loads(row["permissions"])
    except (ValueError, TypeError):
        fail("商品流水线授权已失效", 401)
    return _permissions(value, row["kind"])


def _current(c, authorization_id):
    row = c.execute(
        "SELECT * FROM pipeline_authorizations WHERE id=?", (authorization_id,)
    ).fetchone()
    if row is None or row["revoked"] or row["kind"] not in ("product", "shop.pipeline"):
        fail("商品流水线授权已过期或撤销", 401)
    try:
        expires = float(row["expires"])
    except (TypeError, ValueError, OverflowError):
        fail("商品流水线授权已过期或撤销", 401)
    if not math.isfinite(expires) or expires <= time.time():
        fail("商品流水线授权已过期或撤销", 401)
    shop_row(c, row["shop_id"])
    _stored_permissions(row)
    return row


def _product(c, product_id, shop_id, kind):
    row = c.execute(
        "SELECT shop_id,config FROM products WHERE id=?", (product_id,)
    ).fetchone()
    if row is None or row["shop_id"] != shop_id:
        fail("商品不属于本次授权的店铺", 403)
    if kind == "shop.pipeline":
        try:
            config = json.loads(row["config"])
        except (TypeError, ValueError):
            fail("商品流水线配置无效", 403)
        if not isinstance(config, dict) or config.get("mode", "manual") != "manual":
            fail("店铺流水线授权只能处理队列商品", 403)
    return row


def _view(c, row):
    return {
        key: row[key]
        for key in (
            "id",
            "shop_id",
            "kind",
            "expires",
            "revision",
            "created",
            "last_seen",
            "client_name",
            "agent_type",
            "fingerprint",
            "approved_actor",
            "issuer_role",
            "issuer_shop_id",
        )
    } | {
        "permissions": _stored_permissions(row),
        "product_ids": [
            binding["product_id"]
            for binding in c.execute(
                "SELECT product_id FROM pipeline_bindings "
                "WHERE authorization_id=? ORDER BY product_id",
                (row["id"],),
            )
        ],
    }


def authorization(c, authorization_id):
    """Return a safe current scope, failing expired, revoked or disabled shops."""
    return _view(c, _current(c, authorization_id))


def authorization_raw(c, authorization_id):
    """Return persisted metadata for owner audit/revocation, including history."""
    row = c.execute(
        "SELECT * FROM pipeline_authorizations WHERE id=?", (authorization_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def find_recovery(
    c, shop_id, kind, public_key, product_ids, permissions, issuer_role=None
):
    """Identify an exact existing scope before its real expiry is reviewed.

    This helper never grants or mutates anything.  The approval route must
    include the returned ID, revision and expiration in its reviewed draft.
    """
    permissions = _permissions(permissions, kind)
    wanted_products = sorted(product_ids)
    for candidate in c.execute(
        "SELECT * FROM pipeline_authorizations WHERE shop_id=? AND kind=? "
        "AND public_key=? AND revoked=0 AND expires>? ORDER BY created,id",
        (shop_id, kind, public_key, time.time()),
    ).fetchall():
        view = _view(c, candidate)
        if (
            view["permissions"] == permissions
            and view["product_ids"] == wanted_products
            and (issuer_role is None or candidate["issuer_role"] == issuer_role)
        ):
            _current(c, candidate["id"])
            _bindings(c, candidate["id"])
            return view
    return None


def validate_staff_scope(c, staff):
    """Validate every mapped ancestor, including delegated browser grants."""
    binding = c.execute(
        "SELECT * FROM pipeline_bindings WHERE staff_id=?", (staff["id"],)
    ).fetchone()
    if binding is None:
        return None
    row = _current(c, binding["authorization_id"])
    device = c.execute(
        "SELECT * FROM cli_devices WHERE id=?", (binding["device_id"],)
    ).fetchone()
    try:
        permissions = staff["permissions"]
        if isinstance(permissions, str):
            permissions = json.loads(permissions)
        expires = float(staff["expires"])
    except (TypeError, ValueError, OverflowError):
        fail("商品流水线授权已失效", 401)
    if (
        binding["product_id"] != staff["product_id"]
        or staff["parent_id"] is not None
        or not isinstance(permissions, list)
        or any(not isinstance(p, str) for p in permissions)
        or not set(permissions).issubset(_stored_permissions(row))
        or not math.isfinite(expires)
        or expires > row["expires"]
        or staff["revoked"]
        or device is None
        or device["revoked"]
        or device["staff_id"] != staff["id"]
        or device["public_key"] != row["public_key"]
        or device["fingerprint"] != row["fingerprint"]
    ):
        fail("商品流水线授权已失效", 401)
    _product(c, binding["product_id"], row["shop_id"], row["kind"])
    return _view(c, row)


def check_device_scope(c, device_row, staff):
    """Refresh a derived grant on every signed handshake and business request.

    Unmapped devices are legacy grants.  A partial or inconsistent mapping is
    rejected, so one product's metadata cannot authorize another device.
    """
    binding = c.execute(
        "SELECT * FROM pipeline_bindings WHERE device_id=? OR staff_id=?",
        (device_row["id"], device_row["staff_id"]),
    ).fetchall()
    if not binding:
        return None
    if len(binding) != 1:
        fail("商品流水线设备与授权不匹配", 401)
    binding = binding[0]
    row = _current(c, binding["authorization_id"])
    if (
        device_row["revoked"]
        or binding["device_id"] != device_row["id"]
        or binding["staff_id"] != device_row["staff_id"]
        or staff["id"] != binding["staff_id"]
        or staff["product_id"] != binding["product_id"]
        or staff["shop_id"] != row["shop_id"]
        or device_row["public_key"] != row["public_key"]
        or device_row["fingerprint"] != row["fingerprint"]
        or staff["expires"] > row["expires"]
        or not set(staff["permissions"]).issubset(_stored_permissions(row))
    ):
        fail("商品流水线设备与授权不匹配", 401)
    _product(c, binding["product_id"], row["shop_id"], row["kind"])
    return _view(c, row)


def _identity(public_key, fingerprint, client_name):
    if not isinstance(public_key, str) or len(public_key) != 43:
        fail("流水线设备密钥无效", 400)
    try:
        raw = base64.urlsafe_b64decode(public_key + "=")
    except (ValueError, TypeError):
        fail("流水线设备密钥无效", 400)
    canonical = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    if (
        len(raw) != 32
        or canonical != public_key
        or not isinstance(fingerprint, str)
        or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), fingerprint)
    ):
        fail("流水线设备密钥无效", 400)
    if (
        not isinstance(client_name, str)
        or not client_name.strip()
        or len(client_name) > 100
        or any(unicodedata.category(x).startswith("C") for x in client_name)
    ):
        fail("流水线设备名称无效", 400)
    return client_name.strip()


def _draft(c, draft, existing):
    if not isinstance(draft, dict) or set(draft) - {
        "shop_id",
        "kind",
        "product_ids",
        "permissions",
        "expires",
    }:
        fail("商品流水线授权参数无效", 400)
    shop_id, kind = draft.get("shop_id"), draft.get("kind")
    if not isinstance(shop_id, str) or kind not in ("product", "shop.pipeline"):
        fail("商品流水线授权参数无效", 400)
    shop_row(c, shop_id)
    product_ids = draft.get("product_ids")
    if (
        not isinstance(product_ids, list)
        or not product_ids
        or len(product_ids) > MAX_PRODUCTS
        or any(not isinstance(p, str) or not p or len(p) > 100 for p in product_ids)
        or len(product_ids) != len(set(product_ids))
        or (kind == "product" and len(product_ids) != 1)
    ):
        fail("请选择本次授权的商品", 400)
    permissions = _permissions(draft.get("permissions"), kind)
    now = time.time()
    expires = draft.get("expires")
    if expires is None:
        expires = existing["expires"] if existing else now + DEFAULT_DAYS * 86400
    if (
        isinstance(expires, bool)
        or not isinstance(expires, (int, float))
        or not math.isfinite(expires)
        or expires <= now
        or expires > now + MAX_DAYS * 86400
    ):
        fail("商品流水线授权期限无效", 400)
    if existing:
        if shop_id != existing["shop_id"] or kind != existing["kind"]:
            fail("不能更改已有流水线授权的店铺或类型", 403)
        if expires != existing["expires"]:
            fail("追加权限不能更改已有流水线授权期限", 403)
        old_permissions = set(_stored_permissions(existing))
        old_products = {
            r["product_id"]
            for r in c.execute(
                "SELECT product_id FROM pipeline_bindings WHERE authorization_id=?",
                (existing["id"],),
            )
        }
        if not old_permissions.issubset(permissions) or not old_products.issubset(
            product_ids
        ):
            fail("追加授权不能缩减或替换原来的商品及权限", 403)
    from .product_lifecycle import require_active

    retained = old_products if existing else set()
    for product_id in product_ids:
        _product(c, product_id, shop_id, kind)
        if product_id not in retained:
            require_active(c, product_id)
    return shop_id, kind, sorted(product_ids), permissions, expires


def _bindings(c, authorization_id):
    result = []
    for binding in c.execute(
        "SELECT * FROM pipeline_bindings WHERE authorization_id=? ORDER BY product_id",
        (authorization_id,),
    ).fetchall():
        staff = staff_authorization(c, binding["staff_id"])
        device = c.execute(
            "SELECT * FROM cli_devices WHERE id=?", (binding["device_id"],)
        ).fetchone()
        if device is None or device["revoked"]:
            fail("流水线设备已撤销，请申请新的授权", 401)
        check_device_scope(c, device, staff)
        result.append(
            {
                "product_id": binding["product_id"],
                "staff_id": binding["staff_id"],
                "device_id": binding["device_id"],
                "shop_id": staff["shop_id"],
                "permissions": staff["permissions"],
                "expires": staff["expires"],
                "client_name": device["client_name"],
                "agent_type": device["agent_type"],
                "fingerprint": device["fingerprint"],
                "authorization_id": authorization_id,
                "authorization_revision": authorization(c, authorization_id)[
                    "revision"
                ],
                "already_authorized": True,
                "max_cli_uses": staff["max_cli_uses"],
                "cli_uses": staff["cli_uses"],
                "remaining_cli_uses": max(0, staff["max_cli_uses"] - staff["cli_uses"]),
            }
        )
    return result


def materialize(
    c,
    draft,
    public_key,
    client_name,
    fingerprint,
    actor,
    existing_authorization_id=None,
    expected_revision=None,
    issuer_role="shop",
    issuer_shop_id=None,
    agent_type=None,
):
    """Atomically bind a signed claim to its explicitly approved snapshot.

    Callers must verify the signed claim and the live approving owner before
    calling.  Replaying a request is handled by its request table, not by
    creating another authorization here.
    """
    from .link_access import consume_link

    if not c.in_transaction:
        raise RuntimeError("Pipeline claims require an active write transaction")
    client_name = _identity(public_key, fingerprint, client_name)
    from .agent_identity import normalize_identity

    try:
        client_name, agent_type = normalize_identity(
            client_name, agent_type, legacy=agent_type is None
        )
    except ValueError:
        fail("处理端名称或类型无效", 400)
    if issuer_role not in ("root", "shop"):
        fail("流水线授权批准者无效", 403)
    if issuer_role == "shop":
        if issuer_shop_id is None:
            issuer_shop_id = draft.get("shop_id") if isinstance(draft, dict) else None
        if not isinstance(draft, dict) or issuer_shop_id != draft.get("shop_id"):
            fail("流水线授权批准者与店铺不匹配", 403)
    elif issuer_shop_id is not None:
        fail("流水线授权批准者无效", 403)
    existing = (
        _current(c, existing_authorization_id) if existing_authorization_id else None
    )
    if existing:
        if (
            existing["public_key"] != public_key
            or existing["fingerprint"] != fingerprint
            or isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or existing["revision"] != expected_revision
        ):
            fail("流水线授权已变化，请重新申请追加权限", 409)
        # Validate every old device before mutating any permission.  Revoked
        # bindings are permanent tombstones and are never re-created here.
        _bindings(c, existing["id"])
    elif expected_revision is not None:
        fail("追加授权版本无效", 400)
    if existing and agent_type is None:
        agent_type = existing["agent_type"]
    shop_id, kind, product_ids, permissions, expires = _draft(c, draft, existing)
    now = time.time()
    if existing is None:
        # Recovery must have been reviewed explicitly, including the actual
        # old expiration.  An intervening claim cannot silently substitute a
        # different scope or a longer lifetime for the approved draft.
        if find_recovery(c, shop_id, kind, public_key, product_ids, permissions):
            fail("已有相同流水线授权，请重新申请并确认恢复范围", 409)
        active = c.execute(
            "SELECT count(*) FROM pipeline_authorizations WHERE revoked=0 AND expires>?",
            (now,),
        ).fetchone()[0]
        active_key = c.execute(
            "SELECT count(*) FROM pipeline_authorizations WHERE public_key=? "
            "AND revoked=0 AND expires>?",
            (public_key, now),
        ).fetchone()[0]
        total = c.execute("SELECT count(*) FROM pipeline_authorizations").fetchone()[0]
        total_key = c.execute(
            "SELECT count(*) FROM pipeline_authorizations WHERE public_key=?",
            (public_key,),
        ).fetchone()[0]
        if (
            total >= MAX_TOTAL_AUTHORIZATIONS
            or total_key >= MAX_KEY_TOTAL_AUTHORIZATIONS
        ):
            fail("流水线授权历史已达到保留上限，请联系服务器管理员维护", 409)
        if active >= MAX_ACTIVE_AUTHORIZATIONS or active_key >= MAX_KEY_AUTHORIZATIONS:
            fail("有效流水线授权过多，请撤销不再使用的授权后重试", 409)
    authorization_id = existing["id"] if existing else str(uuid.uuid4())
    encoded_permissions = json.dumps(permissions, separators=(",", ":"))
    old_products = {
        r["product_id"]
        for r in c.execute(
            "SELECT product_id FROM pipeline_bindings WHERE authorization_id=?",
            (authorization_id,),
        )
    }
    changed = existing is None or (
        permissions != _stored_permissions(existing)
        or set(product_ids) != old_products
        or issuer_role != existing["issuer_role"]
        or issuer_shop_id != existing["issuer_shop_id"]
        or client_name != existing["client_name"]
        or agent_type != existing["agent_type"]
    )
    if existing:
        if changed:
            c.execute(
                "UPDATE pipeline_authorizations SET permissions=?,revision=revision+1,"
                "last_seen=?,approved_actor=?,issuer_role=?,issuer_shop_id=?,client_name=?,agent_type=? "
                "WHERE id=? AND revision=? AND revoked=0",
                (
                    encoded_permissions,
                    now,
                    actor,
                    issuer_role,
                    issuer_shop_id,
                    client_name,
                    agent_type,
                    authorization_id,
                    expected_revision,
                ),
            )
            c.execute(
                "UPDATE staff SET permissions=? WHERE id IN (SELECT staff_id "
                "FROM pipeline_bindings WHERE authorization_id=?)",
                (encoded_permissions, authorization_id),
            )
            c.execute(
                "UPDATE cli_devices SET client_name=?,agent_type=? WHERE id IN ("
                "SELECT device_id FROM pipeline_bindings WHERE authorization_id=?)",
                (client_name, agent_type, authorization_id),
            )
            if issuer_role != existing["issuer_role"]:
                audit(
                    c,
                    actor,
                    f"cli.scope.issuer.{existing['issuer_role']}_to_{issuer_role}",
                    authorization_id,
                )
    else:
        c.execute(
            "INSERT INTO pipeline_authorizations(id,shop_id,public_key,fingerprint,"
            "client_name,kind,permissions,expires,created,last_seen,approved_actor,"
            "issuer_role,issuer_shop_id,agent_type) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                authorization_id,
                shop_id,
                public_key,
                fingerprint,
                client_name,
                kind,
                encoded_permissions,
                expires,
                now,
                now,
                actor,
                issuer_role,
                issuer_shop_id,
                agent_type,
            ),
        )
    current_products = old_products
    for product_id in product_ids:
        if product_id in current_products:
            continue
        staff_id, device_id = str(uuid.uuid4()), str(uuid.uuid4())
        c.execute(
            "INSERT INTO staff(id,digest,product_id,name,expires,permissions,created,"
            "max_uses,uses,max_cli_uses,cli_uses) VALUES (?,?,?,?,?,?,?,0,0,1,0)",
            (
                staff_id,
                digest(token()),
                product_id,
                f"CLI · {client_name}",
                expires,
                encoded_permissions,
                now,
            ),
        )
        staff = staff_authorization(c, staff_id)
        consume_link(c, staff, channel="cli")
        c.execute(
            "INSERT INTO cli_devices(id,staff_id,public_key,fingerprint,client_name,"
            "created,last_seen,agent_type) VALUES (?,?,?,?,?,?,?,?)",
            (
                device_id,
                staff_id,
                public_key,
                fingerprint,
                client_name,
                now,
                now,
                agent_type,
            ),
        )
        c.execute(
            "INSERT INTO pipeline_bindings VALUES (?,?,?,?,?)",
            (authorization_id, product_id, staff_id, device_id, now),
        )
        audit(c, actor, "cli.device.create", device_id)
    audit(
        c,
        actor,
        ("cli.scope.upgrade" if changed else "cli.scope.recover")
        if existing
        else "cli.scope.create",
        authorization_id,
    )
    bindings = _bindings(c, authorization_id)
    for binding in bindings:
        binding["already_authorized"] = binding["product_id"] in old_products
    return {
        "authorization": authorization(c, authorization_id),
        "bindings": bindings,
    }


def revoke_authorization(c, authorization_id, actor="owner"):
    """Revoke one snapshot and return its unfinished work to the queue."""
    from .link_access import revoke_staff_sessions
    from .security import link_descendant_ids

    row = c.execute(
        "SELECT * FROM pipeline_authorizations WHERE id=?", (authorization_id,)
    ).fetchone()
    if row is None or row["revoked"]:
        return {"authorizations": 0, "bindings": 0, "jobs": 0}
    c.execute(
        "UPDATE pipeline_authorizations SET revoked=1 WHERE id=?", (authorization_id,)
    )
    bindings = c.execute(
        "SELECT * FROM pipeline_bindings WHERE authorization_id=?", (authorization_id,)
    ).fetchall()
    released = 0
    descendants = [
        (binding["product_id"], staff_id)
        for binding in bindings
        for staff_id in link_descendant_ids(c, binding["staff_id"])
    ]
    for product_id, staff_id in descendants:
        # Record the number before session revocation's existing release helper
        # can return the same lease to the queue.
        count = c.execute(
            "SELECT count(*) FROM jobs WHERE product_id=? AND claimed_by=? "
            "AND state='processing'",
            (product_id, staff_id),
        ).fetchone()[0]
        c.execute("UPDATE staff SET revoked=1 WHERE id=?", (staff_id,))
        revoke_staff_sessions(c, staff_id, actor)
        jobs = c.execute(
            "SELECT id FROM jobs WHERE product_id=? AND claimed_by=? AND state='processing'",
            (product_id, staff_id),
        ).fetchall()
        for job in jobs:
            from .flow_adapter import release_actor_tasks

            release_actor_tasks(c, staff_id)
            c.execute(
                "UPDATE jobs SET state='queued',claimed_by=NULL,lease=NULL,updated=? "
                "WHERE id=? AND state='processing' AND claimed_by=?",
                (time.time(), job["id"], staff_id),
            )
            audit(c, actor, "job.release", job["id"])
        released += count
    audit(c, actor, "cli.scope.revoke", authorization_id)
    return {"authorizations": 1, "bindings": len(bindings), "jobs": released}


def revoke_authorizations(c, shop_id=None, actor="owner", issuer_role=None):
    """Revoke a shop's scopes; ``None`` is the platform-wide reset sentinel."""
    sql = "SELECT id FROM pipeline_authorizations WHERE revoked=0"
    args = []
    if shop_id is not None:
        sql += " AND shop_id=?"
        args.append(shop_id)
    if issuer_role is not None:
        if issuer_role not in ("root", "shop"):
            raise ValueError("Unknown pipeline issuer role")
        sql += " AND issuer_role=?"
        args.append(issuer_role)
    result = {"authorizations": 0, "bindings": 0, "jobs": 0}
    for row in c.execute(sql, args).fetchall():
        changed = revoke_authorization(c, row["id"], actor)
        for key in result:
            result[key] += changed[key]
    return result

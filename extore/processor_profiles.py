"""Tenant-owned encrypted processor settings and immutable issuance bindings."""

import json
import time
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .db import audit, db
from .link_access import session_actor
from .processors import specification
from .secret_store import open_secret, store_secret
from .security import fail, session
from .shops import authorize_product, resolve_create_shop, shop_row

router = APIRouter(prefix="/api/admin/processor-profiles", tags=["processor profiles"])


class ProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    processor_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=120)
    configuration: dict[str, str] = Field(default_factory=dict, max_length=30)
    shop_id: str | None = Field(default=None, min_length=1, max_length=100)


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    configuration: dict[str, str] | None = Field(default=None, max_length=30)


class ProfileBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str = Field(min_length=1, max_length=100)


def init_schema(c):
    for statement in (
        "CREATE TABLE IF NOT EXISTS processor_profiles (id TEXT PRIMARY KEY,shop_id TEXT NOT NULL REFERENCES shops(id),processor_id TEXT NOT NULL,name TEXT NOT NULL,revision INTEGER NOT NULL,disabled INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN(0,1)),created REAL NOT NULL,updated REAL NOT NULL)",
        "CREATE TABLE IF NOT EXISTS processor_profile_revisions (profile_id TEXT NOT NULL REFERENCES processor_profiles(id),revision INTEGER NOT NULL,schema_version INTEGER NOT NULL,ciphertext TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(profile_id,revision))",
        "CREATE TABLE IF NOT EXISTS processor_product_bindings (product_id TEXT PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,profile_id TEXT NOT NULL,revision INTEGER NOT NULL,FOREIGN KEY(profile_id,revision) REFERENCES processor_profile_revisions(profile_id,revision))",
        "CREATE TABLE IF NOT EXISTS processor_card_bindings (card_id TEXT PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,product_id TEXT NOT NULL REFERENCES products(id),shop_id TEXT NOT NULL REFERENCES shops(id),processor_id TEXT NOT NULL,profile_id TEXT NOT NULL,revision INTEGER NOT NULL,FOREIGN KEY(profile_id,revision) REFERENCES processor_profile_revisions(profile_id,revision))",
        "CREATE INDEX IF NOT EXISTS processor_profiles_shop ON processor_profiles(shop_id)",
    ):
        c.execute(statement)
    # Old per-product settings become private tenant profiles in the same
    # migration transaction. Secrets no longer live in product JSON.
    for row in c.execute("SELECT id,config FROM products").fetchall():
        values = json.loads(row["config"])
        if values.get("mode") == "script" and values.get("processor_config"):
            persist_product_configuration(c, row["id"], values)
            c.execute(
                "UPDATE products SET config=? WHERE id=?",
                (json.dumps(values), row["id"]),
            )
    # Historical issued cards are bound once, before any later account edits.
    c.execute(
        "INSERT OR IGNORE INTO processor_card_bindings "
        "SELECT cards.id,cards.product_id,products.shop_id,profiles.processor_id,"
        "binding.profile_id,binding.revision FROM cards "
        "JOIN products ON products.id=cards.product_id "
        "JOIN processor_product_bindings binding ON binding.product_id=products.id "
        "JOIN processor_profiles profiles ON profiles.id=binding.profile_id"
    )


def _configuration(processor_id, values, *, allow_incomplete=False):
    from extore_processors import validate_configuration

    try:
        return validate_configuration(
            processor_id, values, allow_incomplete=allow_incomplete
        )
    except (ValueError, KeyError):
        fail("配置不符合商品处理器定义", 422)


def _metadata(row):
    return {
        key: row[key]
        for key in (
            "id",
            "shop_id",
            "processor_id",
            "name",
            "revision",
            "disabled",
            "created",
            "updated",
        )
    }


def _profile(c, profile_id, owner=None):
    row = c.execute(
        "SELECT * FROM processor_profiles WHERE id=?", (profile_id,)
    ).fetchone()
    if row is None:
        fail("商品处理器配置不存在", 404)
    if owner is not None:
        if owner["role"] != "admin":
            fail("此操作需要店铺管理权限", 403)
        if owner.get("shop_id") is not None and owner["shop_id"] != row["shop_id"]:
            fail("商品处理器配置不存在", 404)
    shop_row(c, row["shop_id"])
    return row


def _seal(profile_id, shop_id, processor_id, revision, values):
    version = specification(processor_id)["schema_version"]
    ciphertext = store_secret(
        values,
        tenant_id=shop_id,
        resource_type=f"processor-config:{processor_id}:v{version}",
        resource_id=f"{profile_id}:{revision}",
    )
    return version, ciphertext


def _create(c, shop_id, processor_id, name, values):
    if not name.strip():
        fail("请输入配置名称", 422)
    pid = str(uuid.uuid4())
    now = time.time()
    version, ciphertext = _seal(pid, shop_id, processor_id, 1, values)
    c.execute(
        "INSERT INTO processor_profiles(id,shop_id,processor_id,name,revision,created,updated) VALUES (?,?,?,?,1,?,?)",
        (pid, shop_id, processor_id, name.strip(), now, now),
    )
    c.execute(
        "INSERT INTO processor_profile_revisions VALUES (?,?,?,?,?)",
        (pid, 1, version, ciphertext, now),
    )
    return _profile(c, pid)


def _revision(c, row, values):
    revision = row["revision"] + 1
    version, ciphertext = _seal(
        row["id"], row["shop_id"], row["processor_id"], revision, values
    )
    c.execute(
        "INSERT INTO processor_profile_revisions VALUES (?,?,?,?,?)",
        (row["id"], revision, version, ciphertext, time.time()),
    )
    c.execute(
        "UPDATE processor_profiles SET revision=?,updated=? WHERE id=?",
        (revision, time.time(), row["id"]),
    )
    return revision


def persist_product_configuration(c, pid, values, actor_session=None):
    product_row = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if product_row is None:
        fail("商品不存在", 404)
    if actor_session is not None:
        authorize_product(c, actor_session, pid)
        if actor_session["role"] != "admin":
            if values.get("processor_config"):
                fail("管理链接不能配置店铺处理器账户", 403)
            return
    if values.get("mode") != "script":
        c.execute("DELETE FROM processor_product_bindings WHERE product_id=?", (pid,))
        values["processor_config"] = {}
        return
    current = c.execute(
        "SELECT profiles.* FROM processor_product_bindings binding "
        "JOIN processor_profiles profiles ON profiles.id=binding.profile_id WHERE product_id=?",
        (pid,),
    ).fetchone()
    supplied = values.get("processor_config", {})
    if (
        current is not None
        and not supplied
        and current["processor_id"] == values["processor_id"]
    ):
        values["processor_config"] = {}
        return
    # Explicit inline configuration is a write-only compatibility interface.
    # Create a separate profile, so editing one product never mutates another
    # product's shared account. Existing cards retain their previous revision.
    config = _configuration(values["processor_id"], supplied, allow_incomplete=True)
    profile = _create(
        c, product_row["shop_id"], values["processor_id"], values["name"], config
    )
    c.execute(
        "INSERT INTO processor_product_bindings VALUES (?,?,?) "
        "ON CONFLICT(product_id) DO UPDATE SET profile_id=excluded.profile_id,revision=excluded.revision",
        (pid, profile["id"], profile["revision"]),
    )
    values["processor_config"] = {}


def product_configuration_status(c, product_id):
    row = c.execute(
        "SELECT profiles.*,binding.revision AS bound_revision FROM processor_product_bindings binding "
        "JOIN processor_profiles profiles ON profiles.id=binding.profile_id WHERE product_id=?",
        (product_id,),
    ).fetchone()
    return {
        "product_id": product_id,
        "profile": (
            {**_metadata(row), "bound_revision": row["bound_revision"]} if row else None
        ),
    }


def _open_revision(c, row, revision, *, allow_incomplete=False):
    stored = c.execute(
        "SELECT * FROM processor_profile_revisions WHERE profile_id=? AND revision=?",
        (row["id"], revision),
    ).fetchone()
    if stored is None or row["disabled"]:
        fail("商品处理器配置已撤销或不可用", 409)
    if specification(row["processor_id"])["schema_version"] != stored["schema_version"]:
        fail("商品处理器配置需要重新确认", 409)
    config = open_secret(
        stored["ciphertext"],
        tenant_id=row["shop_id"],
        resource_type=f"processor-config:{row['processor_id']}:v{stored['schema_version']}",
        resource_id=f"{row['id']}:{revision}",
    )
    return _configuration(
        row["processor_id"], config, allow_incomplete=allow_incomplete
    )


def issue_configuration(c, product_id):
    product_row = c.execute(
        "SELECT * FROM products WHERE id=?", (product_id,)
    ).fetchone()
    if product_row is None:
        fail("商品不存在", 404)
    shop_row(c, product_row["shop_id"])
    values = json.loads(product_row["config"])
    binding = c.execute(
        "SELECT * FROM processor_product_bindings WHERE product_id=?", (product_id,)
    ).fetchone()
    if binding is None:
        fail("请先完成商品处理器配置", 409)
    profile = _profile(c, binding["profile_id"])
    if (
        profile["shop_id"] != product_row["shop_id"]
        or profile["processor_id"] != values["processor_id"]
    ):
        fail("商品处理器配置不属于此店铺和商品", 409)
    try:
        _open_revision(c, profile, binding["revision"])
    except HTTPException as exc:
        if exc.status_code != 422:
            raise
        fail("请先完成商品处理器配置", 400)
    return binding


def freeze_card_binding(c, card_id, product_id, binding):
    row = c.execute(
        "SELECT shop_id,config FROM products WHERE id=?", (product_id,)
    ).fetchone()
    c.execute(
        "INSERT INTO processor_card_bindings VALUES (?,?,?,?,?,?)",
        (
            card_id,
            product_id,
            row["shop_id"],
            json.loads(row["config"])["processor_id"],
            binding["profile_id"],
            binding["revision"],
        ),
    )


def runtime_configuration(c, job_row, processor_id):
    product_row = c.execute(
        "SELECT * FROM products WHERE id=?", (job_row["product_id"],)
    ).fetchone()
    if product_row is None:
        fail("商品不存在", 404)
    shop_row(c, product_row["shop_id"])
    binding = c.execute(
        "SELECT * FROM processor_card_bindings WHERE card_id=?", (job_row["card_id"],)
    ).fetchone()
    if binding is None:
        fail("此卡密没有已确认的商品处理器配置", 409)
    profile = _profile(c, binding["profile_id"])
    if (
        binding["product_id"] != job_row["product_id"]
        or binding["shop_id"] != product_row["shop_id"]
        or profile["shop_id"] != product_row["shop_id"]
        or binding["processor_id"] != processor_id
        or profile["processor_id"] != processor_id
    ):
        fail("商品处理器配置的店铺或处理器不匹配", 409)
    return _open_revision(c, profile, binding["revision"]), {
        "shop_id": product_row["shop_id"],
        "profile_id": profile["id"],
        "revision": binding["revision"],
    }


@router.get("")
def list_profiles(request: Request, shop_id: str | None = None):
    owner = session(request)
    with db() as c:
        sid = resolve_create_shop(c, owner, shop_id)
        return [
            _metadata(row)
            for row in c.execute(
                "SELECT * FROM processor_profiles WHERE shop_id=? ORDER BY created,id",
                (sid,),
            )
        ]


@router.post("")
def create_profile(body: ProfileCreate, request: Request):
    owner = session(request)
    config = _configuration(body.processor_id, body.configuration)
    with db() as c:
        sid = resolve_create_shop(c, owner, body.shop_id)
        row = _create(c, sid, body.processor_id, body.name, config)
        audit(c, session_actor(owner), "processor_profile.create", row["id"])
        return _metadata(row)


@router.get("/bindings/{product_id}")
def get_binding(product_id: str, request: Request):
    owner = session(request)
    with db() as c:
        authorize_product(c, owner, product_id)
        return product_configuration_status(c, product_id)


@router.put("/bindings/{product_id}")
def bind_profile(product_id: str, body: ProfileBinding, request: Request):
    owner = session(request)
    with db() as c:
        product_row = authorize_product(c, owner, product_id)
        profile = _profile(c, body.profile_id, owner)
        values = json.loads(product_row["config"])
        if (
            values.get("mode") != "script"
            or profile["disabled"]
            or profile["shop_id"] != product_row["shop_id"]
            or profile["processor_id"] != values.get("processor_id")
        ):
            fail("配置必须属于此店铺及商品处理器", 409)
        _open_revision(c, profile, profile["revision"])
        c.execute(
            "INSERT INTO processor_product_bindings VALUES (?,?,?) ON CONFLICT(product_id) DO UPDATE SET profile_id=excluded.profile_id,revision=excluded.revision",
            (product_id, profile["id"], profile["revision"]),
        )
        audit(c, session_actor(owner), "processor_profile.bind", product_id)
        return product_configuration_status(c, product_id)


@router.delete("/bindings/{product_id}")
def unbind_profile(product_id: str, request: Request):
    owner = session(request)
    with db() as c:
        authorize_product(c, owner, product_id)
        c.execute(
            "DELETE FROM processor_product_bindings WHERE product_id=?", (product_id,)
        )
        audit(c, session_actor(owner), "processor_profile.unbind", product_id)
    return {"ok": True}


@router.get("/{profile_id}")
def get_profile(profile_id: str, request: Request):
    owner = session(request)
    with db() as c:
        return _metadata(_profile(c, profile_id, owner))


@router.put("/{profile_id}")
def update_profile(profile_id: str, body: ProfileUpdate, request: Request):
    owner = session(request)
    with db() as c:
        row = _profile(c, profile_id, owner)
        if row["disabled"]:
            fail("已撤销的配置不能恢复", 409)
        if body.configuration is not None:
            old = _open_revision(c, row, row["revision"], allow_incomplete=True)
            _revision(
                c,
                row,
                _configuration(row["processor_id"], {**old, **body.configuration}),
            )
        if body.name is not None:
            if not body.name.strip():
                fail("请输入配置名称", 422)
            c.execute(
                "UPDATE processor_profiles SET name=?,updated=? WHERE id=?",
                (body.name.strip(), time.time(), profile_id),
            )
        audit(c, session_actor(owner), "processor_profile.update", profile_id)
        return _metadata(_profile(c, profile_id, owner))


@router.delete("/{profile_id}")
def revoke_profile(profile_id: str, request: Request):
    owner = session(request)
    with db() as c:
        _profile(c, profile_id, owner)
        c.execute(
            "UPDATE processor_profiles SET disabled=1,updated=? WHERE id=?",
            (time.time(), profile_id),
        )
        audit(c, session_actor(owner), "processor_profile.revoke", profile_id)
    return {"ok": True}

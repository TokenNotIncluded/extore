"""Signed redemption destinations; forwarding is a client-local operation."""

import base64
import hashlib
import ipaddress
import re
import socket
import time
import uuid
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .config import ORIGIN
from .db import audit, db, set_setting, setting
from .link_access import session_actor
from .secret_store import open_secret, store_secret
from .security import authorize_management, card_digest, fail, session
from .shops import resolve_create_shop, shop_row

router = APIRouter(tags=["proxy routing"])
_HEX = re.compile(r"[0-9a-f]{32}\Z")
_SECRET = re.compile(r"[A-Z2-7]{32}\Z")
_B64 = re.compile(r"[A-Za-z0-9_-]+\Z")
_PATH = re.compile(r"/[A-Za-z0-9/_-]*\Z")


class IdentityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    shop_id: str | None = Field(default=None, max_length=100)


class RouteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    identity_id: str | None = Field(default=None, max_length=100)
    route_id: str | None = Field(default=None, max_length=32)
    issuer_id: str | None = Field(default=None, max_length=32)
    public_key: str | None = Field(default=None, max_length=43)
    origin: str = Field(max_length=300)
    path: str = Field(default="/", max_length=150)
    default_issuer: StrictBool = False
    shop_id: str | None = Field(default=None, max_length=100)


class RouteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: StrictBool | None = None
    default_issuer: StrictBool | None = None


class CleanupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shop_id: str | None = Field(default=None, max_length=100)


def init_schema(c):
    for statement in (
        "CREATE TABLE IF NOT EXISTS proxy_identities (id TEXT PRIMARY KEY,shop_id TEXT NOT NULL REFERENCES shops(id),name TEXT NOT NULL,public_key TEXT NOT NULL,private_key TEXT NOT NULL,created REAL NOT NULL)",
        "CREATE TABLE IF NOT EXISTS proxy_routes (route_id TEXT NOT NULL,shop_id TEXT NOT NULL REFERENCES shops(id),name TEXT NOT NULL,identity_id TEXT REFERENCES proxy_identities(id),issuer_id TEXT NOT NULL,public_key TEXT NOT NULL,origin TEXT NOT NULL,path TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN(0,1)),default_issuer INTEGER NOT NULL DEFAULT 0 CHECK(default_issuer IN(0,1)),created REAL NOT NULL,updated REAL NOT NULL,PRIMARY KEY(shop_id,route_id))",
        "CREATE UNIQUE INDEX IF NOT EXISTS proxy_default_issuer ON proxy_routes(shop_id) WHERE default_issuer=1",
        "CREATE INDEX IF NOT EXISTS proxy_routes_shop ON proxy_routes(shop_id)",
    ):
        c.execute(statement)
    columns = {r["name"] for r in c.execute("PRAGMA table_info(proxy_identities)")}
    if "current" not in columns:
        c.execute(
            "ALTER TABLE proxy_identities ADD COLUMN current INTEGER NOT NULL DEFAULT 0 CHECK(current IN(0,1))"
        )
    columns = {r["name"] for r in c.execute("PRAGMA table_info(proxy_routes)")}
    if "archived" not in columns:
        c.execute(
            "ALTER TABLE proxy_routes ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN(0,1))"
        )
    c.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS proxy_current_identity ON proxy_identities(shop_id) WHERE current=1"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS proxy_issued_cards (card_id TEXT PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,shop_id TEXT NOT NULL REFERENCES shops(id),route_id TEXT NOT NULL,issued REAL NOT NULL,FOREIGN KEY(shop_id,route_id) REFERENCES proxy_routes(shop_id,route_id) ON DELETE CASCADE)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS proxy_issued_route ON proxy_issued_cards(shop_id,route_id)"
    )
    if setting(c, "proxy_tracking_started") is None:
        set_setting(c, "proxy_tracking_started", str(time.time()))
    for row in c.execute("SELECT id FROM shops").fetchall():
        ensure_shop_issuer(c, row["id"])
    c.execute(
        "UPDATE proxy_routes SET archived=1 WHERE identity_id IS NOT NULL AND default_issuer=0"
    )


def _has_schema(c):
    return (
        c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='proxy_routes'"
        ).fetchone()
        is not None
    )


def _encode(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value, size):
    if not isinstance(value, str) or not _B64.fullmatch(value):
        raise ValueError("Invalid routed code")
    try:
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except ValueError:
        raise ValueError("Invalid routed code") from None
    if len(raw) != size or _encode(raw) != value:
        raise ValueError("Invalid routed code")
    return raw


def canonical_origin(value, *, resolve=False):
    """A fixed public HTTPS origin, never a URL supplied inside a code."""
    if (
        not isinstance(value, str)
        or len(value) > 300
        or re.search(r"[\s\x00-\x1f\x7f\\%]", value)
    ):
        raise ValueError("Invalid routed destination")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port if parsed.port is not None else 443
        if port == 0:
            raise ValueError
        if host == "localhost" or host.endswith((".localhost", ".local")):
            raise ValueError
        # A stable DNS name is shared by the browser and issuer trust record.
        # Reject literal/numeric IP aliases rather than relying on URL parsers
        # that normalize nonstandard IPv4 representations differently.
        if (
            not re.fullmatch(r"[a-z0-9.-]+", host)
            or "." not in host
            or re.fullmatch(r"[0-9.]+", host)
            or re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", host.split(".")[-1])
            or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split(".")
            )
        ):
            raise ValueError
        if resolve:
            addresses = {
                row[4][0]
                for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            }
            if not addresses or any(
                not ipaddress.ip_address(address).is_global for address in addresses
            ):
                raise ValueError
        authority = f"[{host}]" if ":" in host else host
        if port != 443:
            authority += f":{port}"
        return "https://" + authority
    except (ValueError, UnicodeError, OSError):
        raise ValueError("Invalid routed destination") from None


def canonical_path(value):
    if (
        not isinstance(value, str)
        or len(value) > 150
        or not _PATH.fullmatch(value)
        or "//" in value
    ):
        raise ValueError("Invalid routed destination")
    return value


def parse_routed_code(code):
    if not isinstance(code, str) or len(code.strip()) > 200:
        raise ValueError("Invalid routed code")
    parts = code.strip().split(".")
    if (
        len(parts) != 5
        or parts[0] != "EXR1"
        or not _HEX.fullmatch(parts[1])
        or not _SECRET.fullmatch(parts[2])
        or not _HEX.fullmatch(parts[3])
    ):
        raise ValueError("Invalid routed code")
    _decode(parts[4], 64)
    return {
        "route_id": parts[1],
        "secret": parts[2],
        "issuer_id": parts[3],
        "signature": parts[4],
    }


def is_routed_code(code):
    if not isinstance(code, str):
        return False
    value = code.strip()
    prefix = re.match(r"(?i)^EXR([0-9]+)", value)
    if prefix is None:
        return False
    # A damaged first separator still contains the original credential. Never
    # send it to the legacy path. Preserve short genuine base32 legacy codes.
    return bool(
        len(value) > 64
        or re.search(r"[0189]", prefix[1])
        or value[prefix.end() :] in ("",)
        or value[prefix.end() :].startswith(".")
    )


def signed_message(route, secret):
    if (
        not _HEX.fullmatch(route["route_id"])
        or not _HEX.fullmatch(route["issuer_id"])
        or not _SECRET.fullmatch(secret)
        or canonical_origin(route["origin"]) != route["origin"]
    ):
        raise ValueError("Invalid routed code")
    path = canonical_path(route["path"])
    return (
        "Extore routed code v1\n"
        + route["route_id"]
        + "\n"
        + route["issuer_id"]
        + "\n"
        + route["origin"]
        + "\n"
        + path
        + "\n"
        + hashlib.sha256(secret.encode("ascii")).hexdigest()
    ).encode("ascii")


def verify_routed_code(code, route):
    """Pure local verification against an explicitly pinned route record."""
    try:
        parsed = parse_routed_code(code)
        if (
            parsed["route_id"] != route["route_id"]
            or parsed["issuer_id"] != route["issuer_id"]
        ):
            raise ValueError
        Ed25519PublicKey.from_public_bytes(_decode(route["public_key"], 32)).verify(
            _decode(parsed["signature"], 64), signed_message(route, parsed["secret"])
        )
        return parsed["secret"]
    except (KeyError, TypeError, ValueError, InvalidSignature):
        raise ValueError("Invalid routed code") from None


def route_public(row):
    return {
        key: row[key]
        for key in ("route_id", "issuer_id", "name", "origin", "path", "public_key")
    }


def _route_owner(c, row, owner):
    authorize_management(c, owner)
    if owner["role"] != "admin" or (
        owner.get("shop_id") is not None and owner["shop_id"] != row["shop_id"]
    ):
        fail("没有此店铺的路由管理权限", 403)
    shop_row(c, row["shop_id"])
    return row


def _route(c, route_id, owner, requested_shop_id=None):
    sid = resolve_create_shop(c, owner, requested_shop_id)
    row = c.execute(
        "SELECT * FROM proxy_routes WHERE route_id=? AND shop_id=?", (route_id, sid)
    ).fetchone()
    if row is None:
        fail("兑换路由不存在", 404)
    return _route_owner(c, row, owner)


def _owner_route(row):
    return {
        **route_public(row),
        **{
            key: row[key]
            for key in (
                "shop_id",
                "identity_id",
                "enabled",
                "default_issuer",
                "created",
                "updated",
                "archived",
            )
        },
        "kind": "issuer" if row["identity_id"] else "proxy",
    }


def _identity_public(row):
    return {
        **{key: row[key] for key in ("id", "shop_id", "name", "public_key", "created")},
        "current": bool(row["current"]),
    }


def _new_identity(c, sid, name):
    identity_id = uuid.uuid4().hex
    key = Ed25519PrivateKey.generate()
    public = _encode(
        key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    )
    private = _encode(
        key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
    )
    ciphertext = store_secret(
        private,
        tenant_id=sid,
        resource_type="proxy-issuer-ed25519:v1",
        resource_id=identity_id,
    )
    c.execute("UPDATE proxy_identities SET current=0 WHERE shop_id=?", (sid,))
    c.execute(
        "INSERT INTO proxy_identities(id,shop_id,name,public_key,private_key,created,current) VALUES (?,?,?,?,?,?,1)",
        (identity_id, sid, name, public, ciphertext, time.time()),
    )
    return c.execute(
        "SELECT * FROM proxy_identities WHERE id=?", (identity_id,)
    ).fetchone()


def _local_default(c, identity):
    try:
        origin = canonical_origin(ORIGIN)
    except ValueError:
        # Local HTTP development has an identity, but cannot publish an HTTPS
        # routing pin. Production issuance requires the configured HTTPS origin.
        return None
    existing = c.execute(
        "SELECT * FROM proxy_routes WHERE shop_id=? AND identity_id=? AND origin=? AND path='/' AND enabled=1 ORDER BY default_issuer DESC,created DESC LIMIT 1",
        (identity["shop_id"], identity["id"], origin),
    ).fetchone()
    if existing and existing["default_issuer"] and not existing["archived"]:
        return existing
    now = time.time()
    c.execute(
        "UPDATE proxy_routes SET default_issuer=0,archived=1 WHERE shop_id=? AND identity_id IS NOT NULL",
        (identity["shop_id"],),
    )
    if existing:
        c.execute(
            "UPDATE proxy_routes SET default_issuer=1,archived=0,updated=? WHERE shop_id=? AND route_id=?",
            (now, identity["shop_id"], existing["route_id"]),
        )
        route_id = existing["route_id"]
    else:
        route_id = uuid.uuid4().hex
        c.execute(
            "INSERT INTO proxy_routes(route_id,shop_id,name,identity_id,issuer_id,public_key,origin,path,enabled,default_issuer,created,updated,archived) VALUES (?,?,?,?,?,?,?,'/',1,1,?,?,0)",
            (
                route_id,
                identity["shop_id"],
                identity["name"],
                identity["id"],
                identity["id"],
                identity["public_key"],
                origin,
                now,
                now,
            ),
        )
    return c.execute(
        "SELECT * FROM proxy_routes WHERE shop_id=? AND route_id=?",
        (identity["shop_id"], route_id),
    ).fetchone()


def ensure_shop_issuer(c, sid):
    """Every shop has one current issuer. Existing signed pins stay immutable."""
    identity = c.execute(
        "SELECT * FROM proxy_identities WHERE shop_id=? AND current=1", (sid,)
    ).fetchone()
    if identity is None:
        identity = c.execute(
            "SELECT i.* FROM proxy_identities i JOIN proxy_routes r ON r.identity_id=i.id AND r.shop_id=i.shop_id WHERE i.shop_id=? AND r.default_issuer=1 AND r.enabled=1 ORDER BY r.created DESC LIMIT 1",
            (sid,),
        ).fetchone()
        if identity is None:
            identity = _new_identity(c, sid, "main")
        else:
            c.execute(
                "UPDATE proxy_identities SET current=1 WHERE id=?", (identity["id"],)
            )
            identity = c.execute(
                "SELECT * FROM proxy_identities WHERE id=?", (identity["id"],)
            ).fetchone()
    _local_default(c, identity)
    return identity


def _issued_count(c, row):
    """Only codes that can still redeem or reveal retain their signing pins."""
    if not row["identity_id"]:
        return 0
    cutoff = float(setting(c, "proxy_tracking_started", "0"))
    return c.execute(
        "SELECT count(*) FROM cards JOIN products ON products.id=cards.product_id "
        "LEFT JOIN card_meta meta ON meta.card_id=cards.id LEFT JOIN jobs j ON j.card_id=cards.id "
        "LEFT JOIN proxy_issued_cards issued ON issued.card_id=cards.id "
        "WHERE products.shop_id=? AND cards.state NOT IN ('revoked','rejected') "
        "AND (j.id IS NULL OR j.state NOT IN ('destroyed','rejected')) "
        "AND (j.id IS NULL OR j.state!='succeeded' OR (COALESCE(json_extract(products.config,'$.view_policy'),'repeat')!='once' "
        "AND COALESCE(json_extract(products.config,'$.delivery'),'content')!='service')) "
        "AND (meta.expires IS NULL OR meta.expires>? OR (j.id IS NOT NULL AND j.state NOT IN ('failed','needs_input'))) "
        "AND ((issued.shop_id=? AND issued.route_id=?) OR (issued.card_id IS NULL AND cards.created>=? AND cards.created<=?))",
        (
            row["shop_id"],
            time.time(),
            row["shop_id"],
            row["route_id"],
            row["created"],
            cutoff,
        ),
    ).fetchone()[0]


def _route_preview(c, row):
    count = _issued_count(c, row)
    current = bool(row["default_issuer"])
    return {
        "shop_id": row["shop_id"],
        "route_id": row["route_id"],
        "current": current,
        "enabled": bool(row["enabled"]),
        "default_issuer": current,
        "eligible": not current and not row["enabled"] and count == 0,
        "issued_card_count": count,
    }


def _identity(c, identity_id, owner, requested_shop_id=None):
    sid = resolve_create_shop(c, owner, requested_shop_id)
    authorize_management(c, owner)
    row = c.execute(
        "SELECT * FROM proxy_identities WHERE id=? AND shop_id=?", (identity_id, sid)
    ).fetchone()
    if row is None:
        fail("发行标识不存在", 404)
    return row


def _identity_preview(c, row):
    routes = c.execute(
        "SELECT * FROM proxy_routes WHERE shop_id=? AND identity_id=?",
        (row["shop_id"], row["id"]),
    ).fetchall()
    issued = sum(_issued_count(c, route) for route in routes)
    return {
        "shop_id": row["shop_id"],
        "id": row["id"],
        "current": bool(row["current"]),
        "eligible": not row["current"]
        and issued == 0
        and all(not route["default_issuer"] for route in routes),
        "route_count": len(routes),
        "issued_card_count": issued,
    }


def _cleanup_preview(c, sid):
    identities = [
        row["id"]
        for row in c.execute(
            "SELECT * FROM proxy_identities WHERE shop_id=? AND current=0", (sid,)
        ).fetchall()
        if _identity_preview(c, row)["eligible"]
    ]
    routes = [
        row["route_id"]
        for row in c.execute(
            "SELECT * FROM proxy_routes WHERE shop_id=?", (sid,)
        ).fetchall()
        if row["identity_id"] in identities or _route_preview(c, row)["eligible"]
    ]
    return {
        "shop_id": sid,
        "eligible_identity_ids": identities,
        "eligible_route_ids": routes,
        "eligible_identity_count": len(identities),
        "eligible_route_count": len(routes),
    }


def default_issuer_route(c, shop_id, routed=None):
    ensure_shop_issuer(c, shop_id)
    if routed is False and ORIGIN.startswith("https:"):
        fail("新卡必须使用本店发行标识", 409)
    row = (
        c.execute(
            "SELECT * FROM proxy_routes WHERE shop_id=? AND default_issuer=1 AND identity_id IS NOT NULL",
            (shop_id,),
        ).fetchone()
        if _has_schema(c)
        else None
    )
    if row is None:
        if routed is True or ORIGIN.startswith("https:"):
            fail("请先配置本店的签名发行路由", 409)
        return None
    if not row["enabled"]:
        fail("本店签名发行路由已停用，请明确选择新的发行方式", 409)
    try:
        current_origin = canonical_origin(ORIGIN)
    except ValueError:
        fail("本站必须配置 HTTPS 地址才能发行签名卡密", 409)
    if row["origin"] != current_origin or row["path"] != "/":
        fail("签名发行路由与本站地址不匹配", 409)
    return row


def wrap_issued_codes(c, codes, route):
    if route is None:
        return codes
    identity = c.execute(
        "SELECT * FROM proxy_identities WHERE id=? AND shop_id=?",
        (route["identity_id"], route["shop_id"]),
    ).fetchone()
    if identity is None or identity["public_key"] != route["public_key"]:
        fail("签名发行身份不可用", 409)
    encoded = open_secret(
        identity["private_key"],
        tenant_id=identity["shop_id"],
        resource_type="proxy-issuer-ed25519:v1",
        resource_id=identity["id"],
    )
    key = Ed25519PrivateKey.from_private_bytes(_decode(encoded, 32))
    if (
        _encode(
            key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        )
        != identity["public_key"]
    ):
        fail("签名发行身份不可用", 409)
    result = []
    for code in codes:
        secret = code.replace("-", "")
        signature = _encode(key.sign(signed_message(route, secret)))
        result.append(
            f"EXR1.{route['route_id']}.{secret}.{route['issuer_id']}.{signature}"
        )
        card = c.execute(
            "SELECT cards.id FROM cards JOIN products ON products.id=cards.product_id WHERE cards.digest=? AND products.shop_id=?",
            (card_digest(secret), route["shop_id"]),
        ).fetchone()
        if card is None:
            fail("签名发行卡密不存在于此店铺", 409)
        existing = c.execute(
            "SELECT route_id,shop_id FROM proxy_issued_cards WHERE card_id=?",
            (card["id"],),
        ).fetchone()
        if existing and (
            existing["route_id"] != route["route_id"]
            or existing["shop_id"] != route["shop_id"]
        ):
            fail("已经发行的卡密不能改写签名路由", 409)
        c.execute(
            "INSERT INTO proxy_issued_cards(card_id,shop_id,route_id,issued) VALUES (?,?,?,?) ON CONFLICT(card_id) DO NOTHING",
            (card["id"], route["shop_id"], route["route_id"], time.time()),
        )
    return result


def unwrap_local_code(c, code):
    if not is_routed_code(code):
        return code
    try:
        parsed = parse_routed_code(code)
        row = (
            c.execute(
                "SELECT * FROM proxy_routes WHERE route_id=? AND enabled=1 AND identity_id IS NOT NULL",
                (parsed["route_id"],),
            ).fetchone()
            if _has_schema(c)
            else None
        )
        if (
            row is None
            or row["origin"] != canonical_origin(ORIGIN)
            or row["path"] not in ("/", "/proxy")
        ):
            raise ValueError
        shop_row(c, row["shop_id"])
        identity = c.execute(
            "SELECT id FROM proxy_identities WHERE id=? AND shop_id=? AND public_key=?",
            (row["identity_id"], row["shop_id"], row["public_key"]),
        ).fetchone()
        if not identity:
            raise ValueError
        secret = verify_routed_code(code, row)
        # A valid signing key grants no authority over another shop's card.
        # The issuer may only unwrap an existing card from this route's shop.
        card = c.execute(
            "SELECT products.shop_id FROM cards JOIN products ON products.id=cards.product_id WHERE cards.digest=?",
            (card_digest(secret),),
        ).fetchone()
        if not card or card["shop_id"] != row["shop_id"]:
            raise ValueError
        return secret
    except ValueError:
        fail("签名卡密无效或不属于本站", 400)


@router.get("/api/proxy/routes")
def public_routes():
    with db() as c:
        if not _has_schema(c):
            return []
        unique = {}
        for row in c.execute(
            "SELECT routes.* FROM proxy_routes routes JOIN shops ON shops.id=routes.shop_id WHERE routes.enabled=1 AND shops.enabled=1 ORDER BY routes.created,routes.route_id"
        ):
            unique.setdefault(row["route_id"], route_public(row))
        return list(unique.values())


@router.get("/api/admin/proxy/identities")
def list_identities(
    request: Request, shop_id: str | None = None, history: bool = False
):
    owner = session(request)
    with db() as c:
        sid = resolve_create_shop(c, owner, shop_id)
        authorize_management(c, owner)
        return [
            _identity_public(row)
            for row in c.execute(
                "SELECT * FROM proxy_identities WHERE shop_id=? AND (? OR current=1) ORDER BY current DESC,created DESC,id",
                (sid, int(history)),
            )
        ]


@router.post("/api/admin/proxy/identities")
def create_identity(body: IdentityCreate, request: Request):
    owner = session(request)
    if not body.name.strip():
        fail("请输入发行身份名称", 422)
    with db() as c:
        sid = resolve_create_shop(c, owner, body.shop_id)
        authorize_management(c, owner)
        identity = _new_identity(c, sid, "main")
        _local_default(c, identity)
        audit(c, session_actor(owner), "proxy_identity.replace", identity["id"])
        return _identity_public(identity)


@router.get("/api/admin/proxy/routes")
def list_routes(request: Request, shop_id: str | None = None, history: bool = False):
    owner = session(request)
    with db() as c:
        sid = resolve_create_shop(c, owner, shop_id)
        authorize_management(c, owner)
        return [
            _owner_route(row)
            for row in c.execute(
                "SELECT * FROM proxy_routes WHERE shop_id=? AND (? OR (archived=0 AND enabled=1)) ORDER BY default_issuer DESC,created DESC,route_id",
                (sid, int(history)),
            )
        ]


@router.post("/api/admin/proxy/routes")
def create_route(body: RouteCreate, request: Request):
    owner = session(request)
    try:
        origin = canonical_origin(body.origin, resolve=True)
        path = canonical_path(body.path)
    except ValueError:
        fail("路由必须使用固定 HTTPS 公网地址和路径", 422)
    if not body.name.strip():
        fail("请输入路由名称", 422)
    with db() as c:
        sid = resolve_create_shop(c, owner, body.shop_id)
        authorize_management(c, owner)
        if body.identity_id:
            identity = c.execute(
                "SELECT * FROM proxy_identities WHERE id=? AND shop_id=?",
                (body.identity_id, sid),
            ).fetchone()
            if not identity:
                fail("发行身份不属于此店铺", 404)
            try:
                current_origin = canonical_origin(ORIGIN)
            except ValueError:
                fail("本站必须配置 HTTPS 地址才能创建发行路由", 422)
            if (
                origin != current_origin
                or path != "/"
                or any((body.issuer_id, body.public_key))
            ):
                fail("发行路由必须指向本站，并使用本站发行身份", 422)
            if body.default_issuer and not identity["current"]:
                fail("请使用本店当前发行标识", 409)
            if identity["current"] and body.route_id is None:
                row = _local_default(c, identity)
                c.execute(
                    "UPDATE proxy_routes SET name=?,updated=? WHERE shop_id=? AND route_id=?",
                    (body.name.strip(), time.time(), sid, row["route_id"]),
                )
                audit(c, session_actor(owner), "proxy_route.update", row["route_id"])
                return _owner_route(_route(c, row["route_id"], owner, sid))
            route_id = body.route_id or uuid.uuid4().hex
            issuer_id, public = identity["id"], identity["public_key"]
        else:
            route_id, issuer_id, public = body.route_id, body.issuer_id, body.public_key
            if body.default_issuer:
                fail("导入路由不能用作本地发行身份", 422)
        try:
            if (
                not isinstance(route_id, str)
                or not _HEX.fullmatch(route_id)
                or not isinstance(issuer_id, str)
                or not _HEX.fullmatch(issuer_id)
            ):
                raise ValueError
            _decode(public, 32)
        except ValueError:
            fail("请输入完整的已确认发行站配置", 422)
        existing = c.execute(
            "SELECT * FROM proxy_routes WHERE route_id=?", (route_id,)
        ).fetchall()
        pins = (issuer_id, public, origin, path)
        if any(
            tuple(row[key] for key in ("issuer_id", "public_key", "origin", "path"))
            != pins
            for row in existing
        ):
            fail("此路由 ID 已固定发行身份、目标与公钥，不能替换", 409)
        if any(row["shop_id"] == sid for row in existing):
            fail("本店已绑定此路由，请编辑已有绑定", 409)
        if body.default_issuer:
            c.execute(
                "UPDATE proxy_routes SET default_issuer=0 WHERE shop_id=?", (sid,)
            )
        now = time.time()
        c.execute(
            "INSERT INTO proxy_routes(route_id,shop_id,name,identity_id,issuer_id,public_key,origin,path,enabled,default_issuer,created,updated,archived) VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?)",
            (
                route_id,
                sid,
                body.name.strip(),
                body.identity_id,
                issuer_id,
                public,
                origin,
                path,
                int(body.default_issuer),
                now,
                now,
                int(bool(body.identity_id and not body.default_issuer)),
            ),
        )
        audit(c, session_actor(owner), "proxy_route.create", route_id)
        return _owner_route(_route(c, route_id, owner, sid))


@router.put("/api/admin/proxy/routes/{route_id}")
def update_route(
    route_id: str, body: RouteUpdate, request: Request, shop_id: str | None = None
):
    owner = session(request)
    with db() as c:
        row = _route(c, route_id, owner, shop_id)
        if body.name is not None and not body.name.strip():
            fail("请输入路由名称", 422)
        if body.default_issuer is True and not row["identity_id"]:
            fail("导入路由不能用作本地发行身份", 422)
        if row["default_issuer"] and (
            body.enabled is False or body.default_issuer is False
        ):
            fail("必须保留当前发行路由；请替换发行标识后再清理旧路由", 409)
        if body.default_issuer is True:
            identity = c.execute(
                "SELECT current FROM proxy_identities WHERE id=? AND shop_id=?",
                (row["identity_id"], row["shop_id"]),
            ).fetchone()
            if (
                not identity
                or not identity["current"]
                or body.enabled is False
                or not row["enabled"]
                and body.enabled is not True
            ):
                fail("默认发行路由必须属于当前标识且已启用", 409)
        if body.default_issuer is True:
            c.execute(
                "UPDATE proxy_routes SET default_issuer=0 WHERE shop_id=?",
                (row["shop_id"],),
            )
        c.execute(
            "UPDATE proxy_routes SET name=?,enabled=?,default_issuer=?,updated=?,archived=? WHERE route_id=? AND shop_id=?",
            (
                body.name.strip() if body.name is not None else row["name"],
                int(body.enabled) if body.enabled is not None else row["enabled"],
                int(body.default_issuer)
                if body.default_issuer is not None
                else row["default_issuer"],
                time.time(),
                0 if body.default_issuer is True else row["archived"],
                route_id,
                row["shop_id"],
            ),
        )
        audit(c, session_actor(owner), "proxy_route.update", route_id)
        return _owner_route(_route(c, route_id, owner, row["shop_id"]))


@router.get("/api/admin/proxy/identities/{identity_id}/cleanup-preview")
def identity_cleanup_preview(
    identity_id: str, request: Request, shop_id: str | None = None
):
    owner = session(request)
    with db() as c:
        return _identity_preview(c, _identity(c, identity_id, owner, shop_id))


@router.delete("/api/admin/proxy/identities/{identity_id}")
def delete_identity(identity_id: str, request: Request, shop_id: str | None = None):
    owner = session(request)
    with db() as c:
        row = _identity(c, identity_id, owner, shop_id)
        if not _identity_preview(c, row)["eligible"]:
            fail("当前发行标识或仍有已发行卡密的标识不能删除", 409)
        c.execute(
            "DELETE FROM proxy_routes WHERE shop_id=? AND identity_id=?",
            (row["shop_id"], identity_id),
        )
        c.execute(
            "DELETE FROM proxy_identities WHERE id=? AND shop_id=?",
            (identity_id, row["shop_id"]),
        )
        audit(c, session_actor(owner), "proxy_identity.delete", identity_id)
        return {
            "ok": True,
            "shop_id": row["shop_id"],
            "id": identity_id,
            "deleted": True,
        }


@router.get("/api/admin/proxy/routes/{route_id}/cleanup-preview")
def route_cleanup_preview(route_id: str, request: Request, shop_id: str | None = None):
    owner = session(request)
    with db() as c:
        return _route_preview(c, _route(c, route_id, owner, shop_id))


@router.delete("/api/admin/proxy/routes/{route_id}")
def delete_route(route_id: str, request: Request, shop_id: str | None = None):
    owner = session(request)
    with db() as c:
        row = _route(c, route_id, owner, shop_id)
        if not _route_preview(c, row)["eligible"]:
            fail("请先停用路由；当前发行路由或仍有已发行卡密的路由不能删除", 409)
        c.execute(
            "DELETE FROM proxy_routes WHERE shop_id=? AND route_id=?",
            (row["shop_id"], route_id),
        )
        audit(c, session_actor(owner), "proxy_route.delete", route_id)
        return {
            "ok": True,
            "shop_id": row["shop_id"],
            "route_id": route_id,
            "deleted": True,
        }


@router.get("/api/admin/proxy/cleanup")
def cleanup_preview(request: Request, shop_id: str | None = None):
    owner = session(request)
    with db() as c:
        sid = resolve_create_shop(c, owner, shop_id)
        authorize_management(c, owner)
        return _cleanup_preview(c, sid)


@router.post("/api/admin/proxy/cleanup")
def cleanup_history(body: CleanupInput, request: Request):
    owner = session(request)
    with db() as c:
        sid = resolve_create_shop(c, owner, body.shop_id)
        authorize_management(c, owner)
        preview = _cleanup_preview(c, sid)
        # Recompute eligibility inside the same write transaction as deletion.
        for route_id in preview["eligible_route_ids"]:
            c.execute(
                "DELETE FROM proxy_routes WHERE shop_id=? AND route_id=?",
                (sid, route_id),
            )
            audit(c, session_actor(owner), "proxy_route.delete", route_id)
        for identity_id in preview["eligible_identity_ids"]:
            c.execute(
                "DELETE FROM proxy_identities WHERE id=? AND shop_id=?",
                (identity_id, sid),
            )
            audit(c, session_actor(owner), "proxy_identity.delete", identity_id)
        return {
            "ok": True,
            "shop_id": sid,
            "deleted_identity_ids": preview["eligible_identity_ids"],
            "deleted_route_ids": preview["eligible_route_ids"],
            "deleted_identity_count": preview["eligible_identity_count"],
            "deleted_route_count": preview["eligible_route_count"],
        }

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
from .db import audit, db
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


def init_schema(c):
    for statement in (
        "CREATE TABLE IF NOT EXISTS proxy_identities (id TEXT PRIMARY KEY,shop_id TEXT NOT NULL REFERENCES shops(id),name TEXT NOT NULL,public_key TEXT NOT NULL,private_key TEXT NOT NULL,created REAL NOT NULL)",
        "CREATE TABLE IF NOT EXISTS proxy_routes (route_id TEXT NOT NULL,shop_id TEXT NOT NULL REFERENCES shops(id),name TEXT NOT NULL,identity_id TEXT REFERENCES proxy_identities(id),issuer_id TEXT NOT NULL,public_key TEXT NOT NULL,origin TEXT NOT NULL,path TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN(0,1)),default_issuer INTEGER NOT NULL DEFAULT 0 CHECK(default_issuer IN(0,1)),created REAL NOT NULL,updated REAL NOT NULL,PRIMARY KEY(shop_id,route_id))",
        "CREATE UNIQUE INDEX IF NOT EXISTS proxy_default_issuer ON proxy_routes(shop_id) WHERE default_issuer=1",
        "CREATE INDEX IF NOT EXISTS proxy_routes_shop ON proxy_routes(shop_id)",
    ):
        c.execute(statement)


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
            )
        },
        "kind": "issuer" if row["identity_id"] else "proxy",
    }


def default_issuer_route(c, shop_id, routed=None):
    if routed is False:
        return None
    row = (
        c.execute(
            "SELECT * FROM proxy_routes WHERE shop_id=? AND default_issuer=1 AND identity_id IS NOT NULL",
            (shop_id,),
        ).fetchone()
        if _has_schema(c)
        else None
    )
    if row is None:
        if routed is True:
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
            or row["path"] != "/"
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
def list_identities(request: Request, shop_id: str | None = None):
    owner = session(request)
    with db() as c:
        sid = resolve_create_shop(c, owner, shop_id)
        authorize_management(c, owner)
        return [
            {
                key: row[key]
                for key in ("id", "shop_id", "name", "public_key", "created")
            }
            for row in c.execute(
                "SELECT * FROM proxy_identities WHERE shop_id=? ORDER BY created,id",
                (sid,),
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
        c.execute(
            "INSERT INTO proxy_identities VALUES (?,?,?,?,?,?)",
            (identity_id, sid, body.name.strip(), public, ciphertext, time.time()),
        )
        audit(c, session_actor(owner), "proxy_identity.create", identity_id)
        return {
            "id": identity_id,
            "shop_id": sid,
            "name": body.name.strip(),
            "public_key": public,
        }


@router.get("/api/admin/proxy/routes")
def list_routes(request: Request, shop_id: str | None = None):
    owner = session(request)
    with db() as c:
        sid = resolve_create_shop(c, owner, shop_id)
        authorize_management(c, owner)
        return [
            _owner_route(row)
            for row in c.execute(
                "SELECT * FROM proxy_routes WHERE shop_id=? ORDER BY created,route_id",
                (sid,),
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
            "INSERT INTO proxy_routes VALUES (?,?,?,?,?,?,?,?,1,?,?,?)",
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
        if body.default_issuer is True:
            c.execute(
                "UPDATE proxy_routes SET default_issuer=0 WHERE shop_id=?",
                (row["shop_id"],),
            )
        c.execute(
            "UPDATE proxy_routes SET name=?,enabled=?,default_issuer=?,updated=? WHERE route_id=? AND shop_id=?",
            (
                body.name.strip() if body.name is not None else row["name"],
                int(body.enabled) if body.enabled is not None else row["enabled"],
                int(body.default_issuer)
                if body.default_issuer is not None
                else row["default_issuer"],
                time.time(),
                route_id,
                row["shop_id"],
            ),
        )
        audit(c, session_actor(owner), "proxy_route.update", route_id)
        return _owner_route(_route(c, route_id, owner, row["shop_id"]))

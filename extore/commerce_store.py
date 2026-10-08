"""Transactional commerce grants, opaque tokens and encrypted issuance retries."""

import json
import time
from urllib.parse import urlsplit

from .commerce_models import MAX_GRANT_DAYS, SCOPES
from .config import ORIGIN
from .db import audit
from .security import digest, token
from .shops import shop_row

REQUEST_TTL = 600
CODE_TTL = 120
ACCESS_TTL = 900
RECOVERY_TTL = 86400
MAX_ACTIVE_CLIENTS = 50
MAX_ACTIVE_GRANTS = 100
CLEANUP_BATCH = 200


class CommerceError(Exception):
    """Only fixed, nonsecret descriptions may reach an OAuth error response."""

    def __init__(self, error, description, status=400):
        self.error = error
        self.description = description
        self.status = status
        super().__init__(error)


def fail(error, description, status=400):
    raise CommerceError(error, description, status)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def init_schema(c):
    """Additive tables only; existing business rows and DDL are unchanged."""
    statements = (
        "CREATE TABLE IF NOT EXISTS commerce_clients ("
        "id TEXT PRIMARY KEY,shop_id TEXT NOT NULL REFERENCES shops(id),"
        "metadata TEXT NOT NULL,created REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0 "
        "CHECK(revoked IN (0,1)))",
        "CREATE TABLE IF NOT EXISTS commerce_requests ("
        "id TEXT PRIMARY KEY,client_id TEXT NOT NULL REFERENCES commerce_clients(id),"
        "redirect_uri TEXT NOT NULL,state TEXT NOT NULL,code_challenge TEXT NOT NULL,"
        "scopes TEXT NOT NULL,product_ids TEXT NOT NULL,created REAL NOT NULL,"
        "expires REAL NOT NULL,status TEXT NOT NULL DEFAULT 'pending' "
        "CHECK(status IN ('pending','approved','denied')))",
        "CREATE TABLE IF NOT EXISTS commerce_grants ("
        "id TEXT PRIMARY KEY,client_id TEXT NOT NULL REFERENCES commerce_clients(id),"
        "shop_id TEXT NOT NULL REFERENCES shops(id),request_id TEXT UNIQUE "
        "REFERENCES commerce_requests(id) ON DELETE SET NULL,scopes TEXT NOT NULL,"
        "issuer_role TEXT NOT NULL CHECK(issuer_role IN ('root','shop')),"
        "product_ids TEXT NOT NULL,card_limits TEXT NOT NULL,created REAL NOT NULL,"
        "expires REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),"
        "last_used REAL,code_digest TEXT UNIQUE NOT NULL,code_expires REAL NOT NULL,"
        "code_used REAL,redirect_uri TEXT NOT NULL,code_challenge TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS commerce_tokens ("
        "digest TEXT PRIMARY KEY,grant_id TEXT NOT NULL REFERENCES commerce_grants(id) "
        "ON DELETE CASCADE,kind TEXT NOT NULL CHECK(kind IN ('access','refresh')),"
        "created REAL NOT NULL,expires REAL NOT NULL,used REAL)",
        "CREATE TABLE IF NOT EXISTS commerce_issuances ("
        "id TEXT PRIMARY KEY,grant_id TEXT NOT NULL REFERENCES commerce_grants(id) "
        "ON DELETE CASCADE,key_digest TEXT NOT NULL,fingerprint TEXT NOT NULL,"
        "product_id TEXT NOT NULL REFERENCES products(id),variant_id TEXT NOT NULL,"
        "batch_id TEXT NOT NULL,created REAL NOT NULL,recovery_expires REAL NOT NULL,"
        "response_ciphertext TEXT,UNIQUE(grant_id,key_digest))",
        "CREATE INDEX IF NOT EXISTS commerce_clients_shop ON commerce_clients(shop_id,revoked)",
        "CREATE INDEX IF NOT EXISTS commerce_requests_expiry ON commerce_requests(expires)",
        "CREATE INDEX IF NOT EXISTS commerce_requests_client ON commerce_requests(client_id,status)",
        "CREATE INDEX IF NOT EXISTS commerce_grants_shop ON commerce_grants(shop_id,revoked,expires)",
        "CREATE INDEX IF NOT EXISTS commerce_tokens_grant ON commerce_tokens(grant_id,kind)",
        "CREATE INDEX IF NOT EXISTS commerce_tokens_expiry ON commerce_tokens(expires)",
        "CREATE INDEX IF NOT EXISTS commerce_issuances_recovery ON commerce_issuances(recovery_expires)",
    )
    for statement in statements:
        c.execute(statement)


def cleanup(c, now=None):
    """Clear expired plaintext-bearing ciphertext, retaining issuance tombstones."""
    now = time.time() if now is None else now
    c.execute(
        "UPDATE commerce_issuances SET response_ciphertext=NULL WHERE id IN ("
        "SELECT id FROM commerce_issuances WHERE recovery_expires<=? "
        "AND response_ciphertext IS NOT NULL LIMIT ?)",
        (now, CLEANUP_BATCH),
    )
    c.execute(
        "DELETE FROM commerce_tokens WHERE digest IN (SELECT digest FROM commerce_tokens "
        "WHERE kind='access' AND expires<=? LIMIT ?)",
        (now, CLEANUP_BATCH),
    )
    c.execute(
        "DELETE FROM commerce_requests WHERE id IN (SELECT id FROM commerce_requests "
        "WHERE expires<? LIMIT ?)",
        (now - 300, CLEANUP_BATCH),
    )
    # Refresh digests and issuance tombstones remain until their entire grant has
    # expired plus a retention window. Old refresh reuse cannot go undetected.
    c.execute(
        "DELETE FROM commerce_grants WHERE id IN (SELECT id FROM commerce_grants "
        "WHERE expires<? LIMIT ?)",
        (now - 7 * 86400, CLEANUP_BATCH),
    )


def actor_id(actor):
    return "shop:" + actor["shop_id"] if actor.get("shop_id") else "owner"


def client_row(c, client_id, active=True):
    row = c.execute(
        "SELECT * FROM commerce_clients WHERE id=?", (client_id,)
    ).fetchone()
    if row is None or active and row["revoked"]:
        fail("invalid_client", "商城应用无效或已撤销", 401)
    shop_row(c, row["shop_id"])
    return row


def client_view(row):
    return {
        **json.loads(row["metadata"]),
        "id": row["id"],
        "client_id": row["id"],
        "shop_id": row["shop_id"],
        "created_at": row["created"],
        "client_id_issued_at": int(row["created"]),
        "enabled": not bool(row["revoked"]),
    }


def grant_row(c, grant_id, *, active=True):
    row = c.execute("SELECT * FROM commerce_grants WHERE id=?", (grant_id,)).fetchone()
    if row is None or active and (row["revoked"] or row["expires"] <= time.time()):
        fail("invalid_token", "商城授权无效、已到期或已撤销", 401)
    client_row(c, row["client_id"], active=active)
    shop_row(c, row["shop_id"])
    return row


def grant_view(c, row):
    client = client_row(c, row["client_id"], active=False)
    product_ids = json.loads(row["product_ids"])
    products = []
    for pid in product_ids:
        product = c.execute("SELECT config FROM products WHERE id=?", (pid,)).fetchone()
        products.append(
            {
                "id": pid,
                "name": json.loads(product["config"])["name"] if product else pid,
            }
        )
    limits = json.loads(row["card_limits"])
    return {
        "id": row["id"],
        "client_id": row["client_id"],
        "client_name": json.loads(client["metadata"])["client_name"],
        "shop_id": row["shop_id"],
        "scopes": json.loads(row["scopes"]),
        "product_ids": product_ids,
        "products": products,
        "card_limits": [
            {**limit, "remaining": limit["max_count"] - limit["issued_count"]}
            for limit in limits
        ],
        "expires": row["expires"],
        "created_at": row["created"],
        "revoked": bool(row["revoked"]),
        "last_used": row["last_used"],
    }


def revoke_grant(c, grant_id, actor, action="commerce.grant.revoke"):
    changed = c.execute(
        "UPDATE commerce_grants SET revoked=1 WHERE id=? AND revoked=0", (grant_id,)
    ).rowcount
    # Erase recoverable card bodies, never the cards or issuance tombstones.
    c.execute(
        "UPDATE commerce_issuances SET response_ciphertext=NULL WHERE grant_id=?",
        (grant_id,),
    )
    if changed:
        audit(c, actor, action, grant_id)


def revoke_shop_grants(c, shop_id=None, actor="owner"):
    """Account recovery invalidates approvals without touching issued cards."""
    sql = "SELECT id FROM commerce_grants WHERE revoked=0"
    args = []
    if shop_id is not None:
        sql += " AND shop_id=?"
        args.append(shop_id)
    else:
        sql += " AND issuer_role='root'"
    for row in c.execute(sql, args).fetchall():
        revoke_grant(c, row["id"], actor, "commerce.grant.auth_reset")
    # Requests do not carry authorization yet. Shop recovery invalidates its
    # pending drafts; root recovery preserves independently managed shops.
    if shop_id is None:
        return
    sql = "UPDATE commerce_requests SET status='denied' WHERE status='pending'"
    if shop_id is not None:
        sql += " AND client_id IN (SELECT id FROM commerce_clients WHERE shop_id=?)"
    c.execute(sql, args)


def issue_tokens(c, grant):
    access, refresh, now = token(), token(), time.time()
    access_expires = min(now + ACCESS_TTL, grant["expires"])
    if access_expires - now < 1:
        fail("invalid_grant", "商城授权即将到期，请重新申请授权")
    c.executemany(
        "INSERT INTO commerce_tokens(digest,grant_id,kind,created,expires) VALUES (?,?,?,?,?)",
        [
            (digest(access), grant["id"], "access", now, access_expires),
            (digest(refresh), grant["id"], "refresh", now, grant["expires"]),
        ],
    )
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": int(access_expires - now),
        "refresh_token": refresh,
        "scope": " ".join(json.loads(grant["scopes"])),
        "grant_id": grant["id"],
        "grant_expires": grant["expires"],
    }


def request_context(c, row):
    """Bind consent to a current client and immutable selection of visible data."""
    from .commerce_listing import commerce_listing
    from .product_lifecycle import matches

    client = client_row(c, row["client_id"])
    shop = shop_row(c, client["shop_id"])
    requested = json.loads(row["product_ids"])
    products = []
    revisions = {}
    for product in c.execute(
        "SELECT id FROM products WHERE shop_id=? ORDER BY created,id", (shop["id"],)
    ):
        pid = product["id"]
        if not matches(c, pid, "active") or requested and pid not in requested:
            continue
        listing = commerce_listing(c, pid)
        products.append(
            {
                "id": pid,
                "name": listing["product"]["name"],
                "variants": [
                    {key: variant[key] for key in ("id", "name", "enabled")}
                    for variant in listing["variants"]
                ],
            }
        )
        revisions[pid] = listing["revision"]
    if len(products) > 100:
        fail(
            "invalid_request", "商品超过单次授权上限，请由商城指定最多 100 件商品", 409
        )
    if requested and set(requested) - {item["id"] for item in products}:
        fail("invalid_request", "申请的商品不可用或不属于当前店铺", 409)
    result = {
        "request": {
            "id": row["id"],
            "client_id": row["client_id"],
            "client_name": json.loads(client["metadata"])["client_name"],
            "redirect_uri": row["redirect_uri"],
            "redirect_host": urlsplit(row["redirect_uri"]).netloc,
            "scopes": json.loads(row["scopes"]),
            "requested_product_ids": requested,
            "expires": row["expires"],
            "max_grant_expires": row["created"] + MAX_GRANT_DAYS * 86400,
        },
        "shop": {"id": shop["id"], "name": shop["name"]},
        "products": products,
    }
    review = {**result, "client_metadata": client["metadata"], "revisions": revisions}
    result["review_digest"] = digest(canonical(review))
    return result


def metadata():
    return {
        "issuer": ORIGIN,
        "authorization_endpoint": ORIGIN + "/oauth/authorize",
        "token_endpoint": ORIGIN + "/api/integrations/commerce/token",
        "revocation_endpoint": ORIGIN + "/api/integrations/commerce/revoke",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": list(SCOPES),
        "authorization_response_iss_parameter_supported": True,
        "extore_commerce": {
            "schema": "extore.commerce-import.v1",
            "schema_uri": ORIGIN + "/api/integrations/commerce/schema",
            "products_endpoint": ORIGIN + "/api/integrations/commerce/products",
            "cards_endpoint": ORIGIN + "/api/integrations/commerce/cards",
            "listing_schema": "extore.product-listing.v1",
            "maximum_products": 100,
            "maximum_cards_per_request": 100,
            "issuance_recovery_seconds": RECOVERY_TTL,
        },
    }

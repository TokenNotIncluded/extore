"""Synthetic signed routing: no real issuer, card, DNS or downstream call."""

import json
import socket
import time
import uuid
from contextlib import ExitStack

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from starlette.responses import Response
from test_product_links import create_link, login_link

from extore import proxy_routes as routing
from extore.app import app
from extore.db import db, set_setting
from extore.security import card_digest, create_session, digest

ISSUER = "https://issuer.example.com"


@pytest.fixture(autouse=True)
def proxy_store(clean, monkeypatch):
    monkeypatch.setattr(routing, "ORIGIN", ISSUER)
    monkeypatch.setattr(
        routing.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))
        ],
    )
    with db() as c:
        routing.init_schema(c)
    yield
    with db() as c:
        c.execute("DELETE FROM proxy_issued_cards")
        c.execute("DELETE FROM proxy_routes")
        c.execute("DELETE FROM proxy_identities")


def product(client, **changes):
    shop_id = changes.pop("shop_id", None)
    response = client.post(
        "/api/admin/products",
        params={"shop_id": shop_id} if shop_id else None,
        json={"name": "路由测试", **changes},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def identity(client, **changes):
    response = client.post(
        "/api/admin/proxy/identities", json={"name": "测试发行站", **changes}
    )
    assert response.status_code == 200, response.text
    return response.json()


def issuer_route(client, **changes):
    created = identity(client)
    response = client.post(
        "/api/admin/proxy/routes",
        json={
            "name": "领取站",
            "identity_id": created["id"],
            "origin": ISSUER,
            "default_issuer": True,
            **changes,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def issue(client, pid, **changes):
    response = client.post(
        "/api/admin/cards", json={"product_id": pid, "count": 1, **changes}
    )
    assert response.status_code == 200, response.text
    return response.json()


def imported(client, **changes):
    key = Ed25519PrivateKey.generate()
    body = {
        "name": "下游领取站",
        "route_id": uuid.uuid4().hex,
        "issuer_id": uuid.uuid4().hex,
        "public_key": routing._encode(
            key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        ),
        "origin": "https://downstream.example.com",
        "path": "/",
        **changes,
    }
    response = client.post("/api/admin/proxy/routes", json=body)
    assert response.status_code == 200, response.text
    return response.json(), key


def signed(route, key, secret="A" * 32):
    signature = routing._encode(key.sign(routing.signed_message(route, secret)))
    return f"EXR1.{route['route_id']}.{secret}.{route['issuer_id']}.{signature}"


def snapshot():
    with db() as c:
        return {
            table: [
                tuple(row) for row in c.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in (
                "cards",
                "grants",
                "receipt_batches",
                "receipt_batch_cards",
                "jobs",
                "card_meta",
                "audit",
            )
        }


def test_identity_encrypted_and_public_pins_have_no_private_metadata(owner):
    route = issuer_route(owner)
    with db() as c:
        stored = c.execute(
            "SELECT * FROM proxy_identities WHERE id=?", (route["issuer_id"],)
        ).fetchone()
        private = routing.open_secret(
            stored["private_key"],
            tenant_id=stored["shop_id"],
            resource_type="proxy-issuer-ed25519:v1",
            resource_id=stored["id"],
        )
        assert private not in stored["private_key"]
        recovered = Ed25519PrivateKey.from_private_bytes(routing._decode(private, 32))
        assert (
            routing._encode(
                recovered.public_key().public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw
                )
            )
            == stored["public_key"]
        )
        assert private not in str([dict(r) for r in c.execute("SELECT * FROM audit")])
    public = owner.get("/api/proxy/routes").json()
    assert routing.route_public(route) in public
    assert len({row["route_id"] for row in public}) == len(public)
    assert set(public[0]) == {
        "route_id",
        "issuer_id",
        "name",
        "origin",
        "path",
        "public_key",
    }
    assert "identity_id" not in json.dumps(public) and "private_key" not in json.dumps(
        public
    )
    rows = owner.get("/api/admin/proxy/identities").json()
    assert rows[0]["id"] == route["issuer_id"] and "private_key" not in rows[0]


def test_signed_issue_stores_original_hash_and_exchanges_without_using_card(owner):
    route = issuer_route(owner)
    pid = product(owner)
    result = issue(owner, pid, count=2, label="签名库存")
    assert result["batch_id"]
    codes = result["codes"]
    assert len(codes) == 2 and codes[0] != codes[1]
    for code in codes:
        secret = routing.verify_routed_code(code, route)
        assert len(code) == 190
        with db() as c:
            card = c.execute(
                "SELECT * FROM cards WHERE digest=?", (card_digest(secret),)
            ).fetchone()
            assert card and card["state"] == "ready"
            assert code not in str(dict(card)) and secret not in str(dict(card))
            assert (
                c.execute(
                    "SELECT digest FROM cards WHERE digest=?", (card_digest(code),)
                ).fetchone()
                is None
            )
    response = owner.post("/api/exchange", json={"code": "\n".join(codes)})
    assert response.status_code == 200, response.text
    assert response.json()["batch"] and len(response.json()["items"]) == 2
    with db() as c:
        assert all(row[0] == "ready" for row in c.execute("SELECT state FROM cards"))
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert all(row[0] for row in c.execute("SELECT first_verified FROM card_meta"))
        assert all(
            code not in str([dict(r) for r in c.execute("SELECT * FROM audit")])
            for code in codes
        )
    stats = owner.get("/api/admin/card-stats", params={"product_id": pid}).json()[
        "summary"
    ]
    assert stats["total"] == 2 and stats["remaining"] == 2


@pytest.mark.parametrize("part", [0, 1, 2, 3, 4])
def test_any_wrapper_tampering_rejected_before_grant_or_card_changes(owner, part):
    issuer_route(owner)
    pid = product(owner)
    code = issue(owner, pid)["codes"][0]
    parts = code.split(".")
    parts[part] = (
        "EXR2"
        if part == 0
        else ("B" if part == 2 else "0" if part in (1, 3) else "A") + parts[part][1:]
    )
    if ".".join(parts) == code:
        parts[part] = parts[part][:-1] + (
            "C" if part == 2 else "1" if part in (1, 3) else "B"
        )
    before = snapshot()
    response = owner.post("/api/exchange", json={"code": code + "\n" + ".".join(parts)})
    assert response.status_code == 400, response.text
    assert snapshot() == before
    assert code not in response.text and parts[2] not in response.text


def test_pinned_target_path_key_and_issuer_are_signed(owner):
    route = issuer_route(owner)
    code = issue(owner, product(owner))["codes"][0]
    for changes in (
        {"origin": "https://elsewhere.example.com"},
        {"path": "/other"},
        {"public_key": routing._encode(b"x" * 32)},
        {"issuer_id": uuid.uuid4().hex},
        {"route_id": uuid.uuid4().hex},
    ):
        with pytest.raises(ValueError, match="Invalid routed code"):
            routing.verify_routed_code(code, {**route, **changes})
    for changes in (
        {"origin": "https://elsewhere.example.com"},
        {"public_key": "x" * 43},
        {"route_id": uuid.uuid4().hex},
        {"issuer_id": uuid.uuid4().hex},
    ):
        assert (
            owner.put(
                "/api/admin/proxy/routes/" + route["route_id"], json=changes
            ).status_code
            == 422
        )
    assert (
        owner.post(
            "/api/admin/proxy/routes",
            json={**routing.route_public(route), "name": "替换"},
        ).status_code
        == 409
    )


def test_imported_route_cannot_issue_or_unwrap_even_valid_signature(owner):
    route, key = imported(owner)
    code = signed(route, key)
    before = snapshot()
    assert owner.post("/api/exchange", json={"code": code}).status_code == 400
    assert snapshot() == before
    assert (
        owner.put(
            "/api/admin/proxy/routes/" + route["route_id"],
            json={"default_issuer": True},
        ).status_code
        == 422
    )
    issued = issue(owner, product(owner), routed=True)["codes"][0]
    assert routing.parse_routed_code(issued)["route_id"] != route["route_id"]
    assert owner.post("/api/exchange", json={"code": issued}).status_code == 200


def test_mandatory_https_issuer_disallows_downgrade_and_current_disable(
    owner, monkeypatch
):
    monkeypatch.setattr(routing, "ORIGIN", "http://localhost:8000")
    pid = product(owner)
    legacy = "ABCD2345-EFGH2345-IJKL2345-MNOP2345"
    with db() as c:
        c.execute(
            "INSERT INTO cards(id,digest,product_id,created) VALUES (?,?,?,?)",
            (str(uuid.uuid4()), card_digest(legacy), pid, time.time()),
        )
    assert not legacy.startswith("EXR")
    assert (
        owner.post(
            "/api/admin/cards", json={"product_id": pid, "routed": True}
        ).status_code
        == 409
    )
    monkeypatch.setattr(routing, "ORIGIN", ISSUER)
    route = issuer_route(owner)
    assert issue(owner, pid)["codes"][0].startswith("EXR1.")
    assert issue(owner, pid, routed=True)["codes"][0].startswith("EXR1.")
    before = snapshot()
    assert (
        owner.post(
            "/api/admin/cards", json={"product_id": pid, "routed": False}
        ).status_code
        == 409
    )
    assert snapshot() == before
    for changes in ({"enabled": False}, {"default_issuer": False}):
        assert (
            owner.put(
                "/api/admin/proxy/routes/" + route["route_id"], json=changes
            ).status_code
            == 409
        )
        assert snapshot() == before
    assert routing.route_public(route) in owner.get("/api/proxy/routes").json()
    assert (
        owner.post(
            "/api/exchange", json={"code": legacy.lower().replace("-", " ")}
        ).status_code
        == 200
    )


def test_signature_exchange_cannot_cross_shop_even_with_real_local_signing_key(owner):
    route = issuer_route(owner)
    with db() as c:
        sid = str(uuid.uuid4())
        c.execute(
            "INSERT INTO shops(id,name,created,verified) VALUES (?,?,?,1)",
            (sid, "另一店", time.time()),
        )
    foreign_pid = product(owner, shop_id=sid)
    foreign_code = issue(owner, foreign_pid)["codes"][0]
    raw = routing.parse_routed_code(foreign_code)["secret"]
    with db() as c:
        identity_row = c.execute(
            "SELECT * FROM proxy_identities WHERE id=?", (route["issuer_id"],)
        ).fetchone()
        key = Ed25519PrivateKey.from_private_bytes(
            routing._decode(
                routing.open_secret(
                    identity_row["private_key"],
                    tenant_id=identity_row["shop_id"],
                    resource_type="proxy-issuer-ed25519:v1",
                    resource_id=identity_row["id"],
                ),
                32,
            )
        )
    code = signed(route, key, raw)
    before = snapshot()
    assert owner.post("/api/exchange", json={"code": code}).status_code == 400
    assert snapshot() == before
    assert owner.post("/api/exchange", json={"code": foreign_code}).status_code == 200


def test_shop_owner_scoping_and_staff_denial(owner):
    with ExitStack() as stack:
        shops = []
        for name in ("A", "B"):
            with db() as c:
                sid = str(uuid.uuid4())
                c.execute(
                    "INSERT INTO shops(id,name,email,created,verified) VALUES (?,?,?,?,1)",
                    (sid, name, sid + "@example.com", time.time()),
                )
                value = create_session(c, Response(), "admin", shop_id=sid)
            merchant = stack.enter_context(
                TestClient(
                    app,
                    base_url="http://localhost:8000",
                    headers={"Origin": "http://localhost:8000"},
                )
            )
            merchant.cookies.set("extore_session", value)
            shops.append((sid, merchant, issuer_route(merchant)))
        sid_a, merchant_a, route_a = shops[0]
        sid_b, merchant_b, route_b = shops[1]
        assert merchant_a.get("/api/admin/proxy/routes").json() == [route_a]
        assert (
            merchant_a.get("/api/admin/proxy/identities").json()[0]["shop_id"] == sid_a
        )
        assert (
            merchant_a.get(
                "/api/admin/proxy/routes", params={"shop_id": sid_b}
            ).status_code
            == 403
        )
        assert (
            merchant_a.post(
                "/api/admin/proxy/identities", json={"name": "越权", "shop_id": sid_b}
            ).status_code
            == 403
        )
        assert (
            merchant_a.post(
                "/api/admin/proxy/routes",
                json={
                    "name": "越权",
                    "identity_id": route_b["identity_id"],
                    "origin": ISSUER,
                },
            ).status_code
            == 404
        )
        assert (
            merchant_a.put(
                "/api/admin/proxy/routes/" + route_b["route_id"],
                params={"shop_id": sid_b},
                json={"enabled": False},
            ).status_code
            == 403
        )
        assert owner.get(
            "/api/admin/proxy/routes", params={"shop_id": sid_b}
        ).json() == [route_b]
        pid = product(merchant_a)
        link = create_link(merchant_a, pid, permissions=["queue.view", "cards.manage"])
        login_link(merchant_a, link)
        for method, url, body in (
            ("GET", "/api/admin/proxy/routes", None),
            ("GET", "/api/admin/proxy/identities", None),
            ("POST", "/api/admin/proxy/identities", {"name": "员工"}),
            (
                "PUT",
                "/api/admin/proxy/routes/" + route_a["route_id"],
                {"enabled": False},
            ),
        ):
            assert merchant_a.request(method, url, json=body).status_code == 401
        issued = merchant_a.post(
            "/api/manage/cards", json={"product_id": pid, "count": 1}
        )
        assert issued.status_code == 200, issued.text
        assert routing.verify_routed_code(issued.json()["codes"][0], route_a)
        with db() as c:
            c.execute("UPDATE shops SET enabled=0 WHERE id=?", (sid_b,))
        assert all(
            row["route_id"] != route_b["route_id"]
            for row in owner.get("/api/proxy/routes").json()
        )
        assert merchant_b.get("/api/admin/proxy/routes").status_code == 401


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "https://localhost",
        "https://sub.local",
        "https://127.0.0.1",
        "https://8.8.8.8",
        "https://[::1]",
        "https://a@public.example.com",
        "https://public.example.com?q=1",
        "https://public.example.com#key",
        "https://public.example.com/path",
        "https://public.example.com%2f",
        "https://-bad.example.com",
    ],
)
def test_unsafe_origin_rejected(owner, origin):
    response = owner.post(
        "/api/admin/proxy/routes", json={"name": "无效路由", "origin": origin}
    )
    assert response.status_code == 422, response.text


def test_dns_private_or_mixed_answer_rejected(owner, monkeypatch):
    for addresses in (("127.0.0.1",), ("8.8.8.8", "169.254.169.254")):
        monkeypatch.setattr(
            routing.socket,
            "getaddrinfo",
            lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
                for address in addresses
            ],
        )
        response = owner.post(
            "/api/admin/proxy/routes",
            json={"name": "混合解析", "origin": "https://public.example.com"},
        )
        assert response.status_code == 422
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM proxy_routes WHERE identity_id IS NULL"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "path", ["//elsewhere", "/a/../b", "/%2e", "/#code", "/?query", "relative", "/a\\b"]
)
def test_unsafe_path_rejected(owner, path):
    response = owner.post(
        "/api/admin/proxy/routes",
        json={"name": "无效路径", "origin": ISSUER, "path": path},
    )
    assert response.status_code == 422


def test_platform_idempotency_legacy_fingerprint_and_signed_response_encryption(owner):
    pid = product(owner)
    with db() as c:
        set_setting(c, "integration_key", digest("synthetic-platform-key"))
    body = {"product_id": pid, "count": 1}
    headers = {
        "Authorization": "Bearer synthetic-platform-key",
        "Idempotency-Key": "synthetic-order-001",
    }
    first = owner.post("/api/integrations/cards", json=body, headers=headers)
    assert first.status_code == 200, first.text
    with db() as c:
        old = c.execute("SELECT * FROM api_requests").fetchone()
        assert old["fingerprint"] == digest(json.dumps(body, separators=(",", ":")))
    route = issuer_route(owner)
    assert (
        owner.post("/api/integrations/cards", json=body, headers=headers).json()
        == first.json()
    )
    next_headers = {**headers, "Idempotency-Key": "synthetic-order-002"}
    wrapped = owner.post("/api/integrations/cards", json=body, headers=next_headers)
    assert wrapped.status_code == 200, wrapped.text
    code = wrapped.json()["codes"][0]
    assert routing.verify_routed_code(code, route)
    assert (
        owner.post("/api/integrations/cards", json=body, headers=next_headers).json()
        == wrapped.json()
    )
    assert (
        owner.post(
            "/api/integrations/cards",
            json={**body, "routed": False},
            headers=next_headers,
        ).status_code
        == 409
    )
    with db() as c:
        assert c.execute("SELECT count(*) FROM cards").fetchone()[0] == 2
        assert all(
            code.encode() not in bytes(row[0])
            for row in c.execute("SELECT response FROM api_requests")
        )


def test_unconfigured_http_issuer_fails_cleanly_and_origin_change_fails_closed(
    owner, monkeypatch
):
    route = issuer_route(owner)
    pid = product(owner)
    code = issue(owner, pid)["codes"][0]
    before = snapshot()
    monkeypatch.setattr(routing, "ORIGIN", "http://localhost:8000")
    assert owner.post("/api/exchange", json={"code": code}).status_code == 400
    assert owner.post("/api/admin/cards", json={"product_id": pid}).status_code == 409
    assert snapshot() == before
    monkeypatch.setattr(routing, "ORIGIN", "https://new.example.com")
    assert owner.post("/api/exchange", json={"code": code}).status_code == 400
    issued = issue(owner, pid)["codes"][0]
    assert routing.parse_routed_code(issued)["route_id"] != route["route_id"]
    assert owner.post("/api/exchange", json={"code": issued}).status_code == 200
    with db() as c:
        stored = dict(
            c.execute(
                "SELECT * FROM proxy_routes WHERE route_id=?", (route["route_id"],)
            ).fetchone()
        )
    assert routing.route_public(stored) == routing.route_public(route)
    assert routing.route_public(route)["origin"] == ISSUER


def test_same_pin_bindings_are_independent_per_shop_and_public_deduplicates(owner):
    first, _ = imported(owner)
    with db() as c:
        sid = str(uuid.uuid4())
        c.execute(
            "INSERT INTO shops(id,name,created,verified) VALUES (?,?,?,1)",
            (sid, "另一店的绑定", time.time()),
        )
    public = routing.route_public(first)
    second = owner.post(
        "/api/admin/proxy/routes", json={**public, "name": "独立绑定", "shop_id": sid}
    )
    assert second.status_code == 200, second.text
    assert second.json()["shop_id"] == sid
    assert (
        sum(
            row["route_id"] == first["route_id"]
            for row in owner.get("/api/proxy/routes").json()
        )
        == 1
    )
    assert (
        owner.put(
            "/api/admin/proxy/routes/" + first["route_id"], json={"enabled": False}
        ).status_code
        == 200
    )
    assert (
        sum(
            row["route_id"] == first["route_id"]
            for row in owner.get("/api/proxy/routes").json()
        )
        == 1
    )
    rows = owner.get("/api/admin/proxy/routes", params={"shop_id": sid}).json()
    assert len(rows) == 1 and rows[0]["enabled"]
    assert (
        owner.put(
            "/api/admin/proxy/routes/" + first["route_id"],
            params={"shop_id": sid},
            json={"enabled": False},
        ).status_code
        == 200
    )
    assert all(
        row["route_id"] != first["route_id"]
        for row in owner.get("/api/proxy/routes").json()
    )
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM proxy_routes WHERE route_id=?",
                (first["route_id"],),
            ).fetchone()[0]
            == 2
        )
    # Re-enabling one shop never enables the other binding.
    assert (
        owner.put(
            "/api/admin/proxy/routes/" + first["route_id"],
            params={"shop_id": sid},
            json={"enabled": True},
        ).status_code
        == 200
    )
    historical = owner.get("/api/admin/proxy/routes", params={"history": "true"}).json()
    assert (
        next(row for row in historical if row["route_id"] == first["route_id"])[
            "enabled"
        ]
        == 0
    )
    assert (
        sum(
            row["route_id"] == first["route_id"]
            for row in owner.get("/api/proxy/routes").json()
        )
        == 1
    )


@pytest.mark.parametrize(
    "change",
    [
        {"origin": "https://changed.example.com"},
        {"path": "/changed"},
        {"issuer_id": "f" * 32},
        {"public_key": routing._encode(b"f" * 32)},
    ],
)
def test_global_route_pin_cannot_be_redefined_by_other_shop(owner, change):
    route, _ = imported(owner)
    with db() as c:
        sid = str(uuid.uuid4())
        c.execute(
            "INSERT INTO shops(id,name,created,verified) VALUES (?,?,?,1)",
            (sid, "恶意绑定", time.time()),
        )
    before = snapshot()
    response = owner.post(
        "/api/admin/proxy/routes",
        json={**routing.route_public(route), **change, "shop_id": sid},
    )
    assert response.status_code == 409, response.text
    assert snapshot() == before
    assert owner.get("/api/admin/proxy/routes", params={"shop_id": sid}).json() == []
    assert routing.route_public(route) in owner.get("/api/proxy/routes").json()


@pytest.mark.parametrize("separator", ["X", ":", "-", "/", "%2E"])
def test_damaged_wrapper_separator_never_accepted_as_legacy(owner, separator):
    issuer_route(owner)
    code = issue(owner, product(owner))["codes"][0]
    damaged = code.replace("EXR1.", "EXR1" + separator)
    assert routing.is_routed_code(damaged)
    before = snapshot()
    assert owner.post("/api/exchange", json={"code": damaged}).status_code == 400
    assert snapshot() == before
    assert not routing.is_routed_code("EXR2ABCD-AAAAAAAA-AAAAAAAA-AAAAAAAA")

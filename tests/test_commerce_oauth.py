"""The commerce profile authorizes only explicit listings and bounded new stock."""

import base64
import hashlib
import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response

from extore.app import app
from extore.commerce_store import metadata
from extore.config import ORIGIN
from extore.db import db
from extore.security import create_session

ADMIN = "/api/admin/commerce"
API = "/api/integrations/commerce"
REDIRECT = "https://store.example.com/extore/callback?store=one"


@pytest.fixture
def machine():
    with TestClient(app, base_url=ORIGIN) as opened:
        yield opened


def client_registration(owner):
    response = owner.post(
        ADMIN + "/clients",
        json={
            "client_name": "示例上游商城",
            "redirect_uris": [REDIRECT],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def application(owner, machine, pid, scopes=None, limit=5, products=None):
    client = client_registration(owner)
    verifier = secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    scopes = scopes or ["products.read", "cards.issue"]
    params = {
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": REDIRECT,
        "scope": " ".join(scopes),
        "state": "merchant-random-state-123",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "product_ids": pid,
    }
    response = machine.get("/oauth/authorize", params=params, follow_redirects=False)
    assert response.status_code == 303, response.text
    rid = urlsplit(response.headers["location"]).fragment
    context = owner.get(ADMIN + "/requests/" + rid).json()
    body = {
        "shop_id": context["shop"]["id"],
        "review_digest": context["review_digest"],
        "scopes": scopes,
        "product_ids": products or [pid],
        "card_limits": [
            {"product_id": pid, "variant_id": "default", "max_count": limit}
        ]
        if "cards.issue" in scopes
        else [],
        "grant_expires": time.time() + 86400,
    }
    return client, verifier, rid, context, body, params


def connected(owner, machine, pid, **kwargs):
    client, verifier, rid, context, body, params = application(
        owner, machine, pid, **kwargs
    )
    response = owner.post(ADMIN + "/requests/" + rid + "/approve", json=body)
    assert response.status_code == 200, response.text
    callback = parse_qs(urlsplit(response.json()["redirect_uri"]).query)
    assert callback["state"] == [params["state"]]
    assert callback["iss"] == [ORIGIN]
    assert callback["store"] == ["one"]
    code = callback["code"][0]
    exchange = {
        "grant_type": "authorization_code",
        "client_id": client["id"],
        "code": code,
        "redirect_uri": REDIRECT,
        "code_verifier": verifier,
    }
    response = machine.post(API + "/token", data=exchange)
    assert response.status_code == 200, response.text
    tokens = response.json()
    return (
        client,
        tokens,
        exchange,
        {"Authorization": "Bearer " + tokens["access_token"]},
    )


def count_cards():
    with db() as c:
        return c.execute("SELECT COUNT(*) FROM cards").fetchone()[0]


def test_metadata_has_real_endpoints_and_no_anonymous_registration(client):
    response = client.get("/.well-known/oauth-authorization-server")
    assert response.status_code == 200
    assert response.json() == metadata()
    assert "registration_endpoint" not in response.json()
    assert response.json()["code_challenge_methods_supported"] == ["S256"]
    assert response.json()["authorization_response_iss_parameter_supported"] is True
    assert client.get("/connect/authorize").status_code == 200


def test_connect_read_issue_retry_quota_and_no_secret_storage(
    owner, machine, setup_product
):
    pid, _ = setup_product(webhook_secret="PRIVATE_WEBHOOK_SENTINEL")
    client, tokens, _, headers = connected(owner, machine, pid)
    response = machine.get(API + "/products", headers=headers)
    assert response.status_code == 200, response.text
    catalog = response.json()
    assert catalog["schema"] == "extore.commerce-catalog.v1"
    assert [p["id"] for p in catalog["products"]] == [pid]
    assert "PRIVATE_WEBHOOK_SENTINEL" not in response.text
    assert "inventory" not in catalog["products"][0]
    listing = machine.get(API + "/products/" + pid, headers=headers).json()
    issue = {
        "product_id": pid,
        "variant_id": "default",
        "count": 3,
        "expected_revision": listing["revision"],
    }
    issue_headers = {**headers, "Idempotency-Key": "import-operation-001"}
    before = count_cards()
    first = machine.post(API + "/cards", json=issue, headers=issue_headers)
    assert first.status_code == 200, first.text
    result = first.json()
    assert result["schema"] == "extore.card-batch.v1"
    assert len(result["codes"]) == result["count"] == 3
    assert result["quota"] == {"max_count": 5, "issued_count": 3, "remaining": 2}
    assert count_cards() == before + 3
    assert (
        machine.post(API + "/cards", json=issue, headers=issue_headers).json() == result
    )
    assert count_cards() == before + 3
    changed = machine.post(
        API + "/cards", json={**issue, "count": 2}, headers=issue_headers
    )
    assert changed.status_code == 409
    assert changed.json()["error"] == "idempotency_conflict"
    exhausted = machine.post(
        API + "/cards",
        json=issue,
        headers={**headers, "Idempotency-Key": "import-operation-002"},
    )
    assert exhausted.status_code == 409
    assert exhausted.json()["error"] == "quota_exceeded"
    with db() as c:
        stored = c.execute("SELECT * FROM commerce_issuances").fetchone()
        assert stored["response_ciphertext"].startswith("v1.")
        for table in (
            "commerce_clients",
            "commerce_requests",
            "commerce_grants",
            "commerce_tokens",
            "commerce_issuances",
            "audit",
        ):
            serialized = json.dumps(
                [tuple(row) for row in c.execute("SELECT * FROM " + table)]
            )
            assert tokens["access_token"] not in serialized
            assert tokens["refresh_token"] not in serialized
            assert all(code not in serialized for code in result["codes"])
        assert c.execute("SELECT COUNT(*) FROM api_requests").fetchone()[0] == 0
    grants = owner.get(ADMIN + "/grants").json()["grants"]
    assert grants[0]["client_id"] == client["id"]
    assert grants[0]["card_limits"][0]["remaining"] == 2


def test_read_only_grant_never_issues_cards(owner, machine, setup_product):
    pid, _ = setup_product()
    _, _, _, headers = connected(owner, machine, pid, scopes=["products.read"])
    response = machine.post(
        API + "/cards",
        json={"product_id": pid, "variant_id": "default", "count": 1},
        headers={**headers, "Idempotency-Key": "read-only-key"},
    )
    assert response.status_code == 403
    assert response.json()["error"] == "insufficient_scope"
    assert response.headers["www-authenticate"] == 'Bearer error="insufficient_scope"'


def test_issue_only_grant_never_reads_products(owner, machine, setup_product):
    pid, _ = setup_product()
    _, _, _, headers = connected(owner, machine, pid, scopes=["cards.issue"])
    response = machine.get(API + "/products", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"] == "insufficient_scope"


def test_invalid_redirect_and_missing_pkce_never_redirect(
    owner, machine, setup_product
):
    pid, _ = setup_product()
    _, _, _, _, _, params = application(owner, machine, pid)
    for changed in (
        {"redirect_uri": "https://evil.example.com/cb"},
        {"redirect_uri": REDIRECT + "/other"},
        {"code_challenge_method": "plain"},
        {"code_challenge": "bad"},
        {"scope": "shop.owner"},
    ):
        response = machine.get(
            "/oauth/authorize", params={**params, **changed}, follow_redirects=False
        )
        assert response.status_code in (400, 401)
        assert "location" not in response.headers
    response = machine.get(
        "/oauth/authorize",
        params=list(params.items()) + [("scope", "products.read")],
        follow_redirects=False,
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "url",
    [
        "http://store.example.com/cb",
        "https://user:password@store.example.com/cb",
        "https://store.example.com/cb#fragment",
        "https://127.0.0.1/cb",
        "https://0177.0.0.1/cb",
        "https://127.1/cb",
        "https://0x7f.0.0.1/cb",
        "https://-bad.example.com/cb",
        "https://localhost/cb",
        "https://service.local/cb",
        "https://store.example.com/cb?code=x",
        "https://store.example.com/cb?error_description=x",
        "https://store.example.com/cb?code=",
        "https://store.example.com/cb?state",
        "https://store.example.com/cb?iss=",
        "https://store.example.com/cb?error_description=",
        "https://store.example.com/cb?tag=one&tag=two",
        "https://store.example.com\\@evil.example.com/cb",
        "https://store.example.com\n/cb",
    ],
)
def test_client_registration_rejects_unsafe_redirect(owner, url):
    response = owner.post(
        ADMIN + "/clients", json={"client_name": "Mall", "redirect_uris": [url]}
    )
    assert response.status_code == 422
    with db() as c:
        assert c.execute("SELECT COUNT(*) FROM commerce_clients").fetchone()[0] == 0


def test_denial_returns_state_and_issuer_no_tokens(owner, machine, setup_product):
    pid, _ = setup_product()
    _, _, rid, context, _, _ = application(owner, machine, pid)
    response = owner.post(
        ADMIN + "/requests/" + rid + "/deny",
        json={
            "shop_id": context["shop"]["id"],
            "review_digest": context["review_digest"],
        },
    )
    assert response.status_code == 200
    callback = parse_qs(urlsplit(response.json()["redirect_uri"]).query)
    assert callback["error"] == ["access_denied"]
    assert callback["iss"] == [ORIGIN]
    assert "code" not in callback
    with db() as c:
        assert c.execute("SELECT COUNT(*) FROM commerce_grants").fetchone()[0] == 0


def test_fresh_session_and_csrf_required_for_consent(owner, machine, setup_product):
    pid, _ = setup_product()
    _, _, rid, _, body, _ = application(owner, machine, pid)
    response = owner.post(
        ADMIN + "/requests/" + rid + "/approve",
        json=body,
        headers={"Origin": "https://evil.example.com"},
    )
    assert response.status_code == 403
    with db() as c:
        c.execute("UPDATE sessions SET auth_at=?", (time.time() - 601,))
    response = owner.post(ADMIN + "/requests/" + rid + "/approve", json=body)
    assert response.status_code == 401
    with db() as c:
        assert c.execute("SELECT COUNT(*) FROM commerce_grants").fetchone()[0] == 0


def test_stale_review_and_added_product_not_implicitly_authorized(
    owner, machine, setup_product
):
    pid, _ = setup_product()
    _, _, rid, _, body, _ = application(owner, machine, pid)
    with db() as c:
        p = c.execute("SELECT config FROM products WHERE id=?", (pid,)).fetchone()
        values = json.loads(p["config"])
        values["description"] = "Changed after consent review"
        c.execute("UPDATE products SET config=? WHERE id=?", (json.dumps(values), pid))
    stale = owner.post(ADMIN + "/requests/" + rid + "/approve", json=body)
    assert stale.status_code == 409
    assert stale.json()["error"] == "review_changed"
    fresh = owner.get(ADMIN + "/requests/" + rid).json()
    body["review_digest"] = fresh["review_digest"]
    second, _ = setup_product(name="Not approved")
    response = owner.post(
        ADMIN + "/requests/" + rid + "/approve",
        json={**body, "product_ids": [pid, second]},
    )
    assert response.status_code == 403


def test_pkce_and_client_binding_and_code_single_use(owner, machine, setup_product):
    pid, _ = setup_product()
    _, tokens, exchange, headers = connected(owner, machine, pid)
    bad = machine.post(
        API + "/token", data={**exchange, "code_verifier": secrets.token_urlsafe(32)}
    )
    assert bad.status_code == 400
    assert machine.get(API + "/products", headers=headers).status_code == 200
    bad = machine.post(
        API + "/token", data={**exchange, "redirect_uri": REDIRECT + "x"}
    )
    assert bad.status_code == 400
    replay = machine.post(API + "/token", data=exchange)
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"
    assert machine.get(API + "/products", headers=headers).status_code == 401
    with db() as c:
        assert (
            c.execute(
                "SELECT revoked FROM commerce_grants WHERE id=?", (tokens["grant_id"],)
            ).fetchone()[0]
            == 1
        )


def test_refresh_rotation_and_replay_revoke_family(owner, machine, setup_product):
    pid, _ = setup_product()
    client, tokens, _, headers = connected(owner, machine, pid)
    body = {
        "grant_type": "refresh_token",
        "client_id": client["id"],
        "refresh_token": tokens["refresh_token"],
    }
    response = machine.post(API + "/token", data=body)
    assert response.status_code == 200
    newer = response.json()
    assert newer["access_token"] != tokens["access_token"]
    assert newer["refresh_token"] != tokens["refresh_token"]
    assert newer["grant_id"] == tokens["grant_id"]
    assert newer["scope"] == tokens["scope"]
    replay = machine.post(API + "/token", data=body)
    assert replay.status_code == 400
    for access in (tokens["access_token"], newer["access_token"]):
        assert (
            machine.get(
                API + "/products", headers={"Authorization": "Bearer " + access}
            ).status_code
            == 401
        )
    assert (
        machine.post(
            API + "/token", data={**body, "refresh_token": newer["refresh_token"]}
        ).status_code
        == 400
    )
    assert machine.get(API + "/products", headers=headers).status_code == 401


def test_revocation_and_client_removal_stop_access_not_cards(
    owner, machine, setup_product
):
    pid, _ = setup_product()
    client, tokens, _, headers = connected(owner, machine, pid)
    first = machine.post(
        API + "/cards",
        json={"product_id": pid, "variant_id": "default", "count": 1},
        headers={**headers, "Idempotency-Key": "mint-before-revoke"},
    )
    assert first.status_code == 200
    count = count_cards()
    response = machine.post(
        API + "/revoke",
        data={"client_id": client["id"], "token": tokens["refresh_token"]},
    )
    assert response.status_code == 200 and response.content == b""
    assert (
        machine.post(
            API + "/revoke", data={"client_id": client["id"], "token": "unknown"}
        ).status_code
        == 200
    )
    assert machine.get(API + "/products", headers=headers).status_code == 401
    assert owner.delete(ADMIN + "/clients/" + client["id"]).status_code == 200
    assert owner.get(ADMIN + "/clients").json() == {"clients": []}
    assert count_cards() == count
    with db() as c:
        assert (
            c.execute("SELECT response_ciphertext FROM commerce_issuances").fetchone()[
                0
            ]
            is None
        )


def test_retry_after_recovery_expiry_never_remints(owner, machine, setup_product):
    pid, _ = setup_product()
    _, _, _, headers = connected(owner, machine, pid)
    body = {"product_id": pid, "variant_id": "default", "count": 1}
    headers = {**headers, "Idempotency-Key": "expire-recovery-key"}
    assert machine.post(API + "/cards", json=body, headers=headers).status_code == 200
    count = count_cards()
    with db() as c:
        c.execute(
            "UPDATE commerce_issuances SET recovery_expires=?", (time.time() - 1,)
        )
    for _ in range(2):
        response = machine.post(API + "/cards", json=body, headers=headers)
        assert response.status_code == 410
        assert response.json()["error"] == "issuance_expired"
    assert count_cards() == count
    with db() as c:
        row = c.execute("SELECT response_ciphertext FROM commerce_issuances").fetchone()
        assert row[0] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"count": True},
        {"count": "2"},
        {"count": 101},
        {"attributes": {"revisions": 999}},
        {"routed": False},
        {"expires": "2000000000"},
        {"expires": True},
    ],
)
def test_issue_strict_fields_cannot_override_frozen_entitlements(
    owner, machine, setup_product, changes
):
    pid, _ = setup_product()
    _, _, _, headers = connected(owner, machine, pid)
    before = count_cards()
    response = machine.post(
        API + "/cards",
        json={"product_id": pid, "variant_id": "default", "count": 1, **changes},
        headers={**headers, "Idempotency-Key": "strict-import-key"},
    )
    assert response.status_code == 400
    assert count_cards() == before


def test_tokens_are_never_management_sessions_or_cookie_fallback(
    owner, machine, setup_product
):
    pid, _ = setup_product()
    _, _, _, headers = connected(owner, machine, pid)
    assert machine.get("/api/admin/products", headers=headers).status_code == 401
    assert owner.get(API + "/products", headers=headers).status_code == 401
    assert owner.get(API + "/products").status_code == 401
    machine.cookies.set("extore_session", "invalid-browser-cookie")
    assert machine.get(API + "/products", headers=headers).status_code == 401


def test_bearer_scheme_is_case_insensitive_but_token_case_is_not(
    owner, machine, setup_product
):
    pid, _ = setup_product()
    _, tokens, _, _ = connected(owner, machine, pid)
    for scheme in ("bearer ", "BEARER   "):
        assert (
            machine.get(
                API + "/products",
                headers={"Authorization": scheme + tokens["access_token"]},
            ).status_code
            == 200
        )
    changed = tokens["access_token"].swapcase()
    assert (
        machine.get(
            API + "/products", headers={"Authorization": "Bearer " + changed}
        ).status_code
        == 401
    )


def test_staff_and_other_shop_cannot_approve(owner, machine, setup_product):
    pid, _ = setup_product()
    _, _, rid, _, body, _ = application(owner, machine, pid)
    link = owner.post(
        "/api/admin/staff", json={"product_id": pid, "name": "Worker"}
    ).json()
    with db() as c:
        staff = c.execute("SELECT id FROM staff").fetchone()[0]
        staff_value = create_session(c, Response(), "staff", staff)
        sid = "other-shop-id"
        c.execute(
            "INSERT INTO shops(id,name,created,verified) VALUES (?,?,?,1)",
            (sid, "Other shop", time.time()),
        )
        other_value = create_session(c, Response(), "admin", shop_id=sid)
    assert link
    for value in (staff_value, other_value):
        machine.cookies.set("extore_session", value)
        response = machine.post(
            ADMIN + "/requests/" + rid + "/approve",
            json=body,
            headers={"Origin": ORIGIN},
        )
        assert response.status_code in (401, 403)
    with db() as c:
        assert c.execute("SELECT COUNT(*) FROM commerce_grants").fetchone()[0] == 0


def test_token_form_duplicates_and_refresh_scope_increase_rejected(
    owner, machine, setup_product
):
    pid, _ = setup_product()
    client, tokens, _, headers = connected(
        owner, machine, pid, scopes=["products.read"]
    )
    response = machine.post(
        API + "/token",
        content="grant_type=refresh_token&grant_type=authorization_code",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 400
    response = machine.post(
        API + "/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client["id"],
            "refresh_token": tokens["refresh_token"],
            "scope": "products.read cards.issue",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_scope"
    assert machine.get(API + "/products", headers=headers).status_code == 200


def test_issuer_grant_expiry_and_deleted_product_gate_even_idempotent_replay(
    owner, machine, setup_product
):
    pid, _ = setup_product()
    _, tokens, _, headers = connected(owner, machine, pid)
    body = {"product_id": pid, "variant_id": "default", "count": 1}
    headers = {**headers, "Idempotency-Key": "mint-before-delete"}
    assert machine.post(API + "/cards", json=body, headers=headers).status_code == 200
    before = count_cards()
    assert (
        owner.request(
            "DELETE", "/api/admin/products/" + pid, json={"confirmed": True}
        ).status_code
        == 200
    )
    assert machine.post(API + "/cards", json=body, headers=headers).status_code == 409
    assert (
        machine.get(
            API + "/products", headers={"Authorization": headers["Authorization"]}
        ).json()["products"]
        == []
    )
    assert count_cards() == before
    with db() as c:
        c.execute(
            "UPDATE commerce_grants SET expires=? WHERE id=?",
            (time.time() - 1, tokens["grant_id"]),
        )
    assert (
        machine.get(
            API + "/products", headers={"Authorization": headers["Authorization"]}
        ).status_code
        == 401
    )


def test_parallel_same_key_mints_once_and_parallel_quotas_cannot_overrun(
    owner, machine, setup_product
):
    pid, _ = setup_product()
    _, _, _, headers = connected(owner, machine, pid, limit=3)
    before = count_cards()
    body = {"product_id": pid, "variant_id": "default", "count": 2}
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda _: machine.post(
                    API + "/cards",
                    json=body,
                    headers={**headers, "Idempotency-Key": "concurrent-same-key"},
                ),
                range(2),
            )
        )
    assert [r.status_code for r in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    assert count_cards() == before + 2
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda index: machine.post(
                    API + "/cards",
                    json={**body, "count": 1},
                    headers={**headers, "Idempotency-Key": f"concurrent-quota-{index}"},
                ),
                range(2),
            )
        )
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert count_cards() == before + 3


def test_schema22_additive_migration_preserves_original_tables_and_rows(setup_product):
    from extore.db import init

    setup_product()
    with db() as c:
        for name in (
            "commerce_issuances",
            "commerce_tokens",
            "commerce_grants",
            "commerce_requests",
            "commerce_clients",
        ):
            c.execute("DROP TABLE " + name)
        c.execute("PRAGMA user_version=21")
        before = {
            row["name"]: (
                row["sql"],
                [tuple(item) for item in c.execute("SELECT * FROM " + row["name"])],
            )
            for row in c.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        }
        assert len(before) == 61
    init()
    init()
    with db() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 22
        for name, (ddl, rows) in before.items():
            assert (
                c.execute(
                    "SELECT sql FROM sqlite_master WHERE name=?", (name,)
                ).fetchone()[0]
                == ddl
            )
            assert [tuple(row) for row in c.execute("SELECT * FROM " + name)] == rows
        for name in (
            "commerce_clients",
            "commerce_requests",
            "commerce_grants",
            "commerce_tokens",
            "commerce_issuances",
        ):
            assert c.execute("SELECT COUNT(*) FROM " + name).fetchone()[0] == 0
        assert not c.execute("PRAGMA foreign_key_check").fetchall()


def test_account_recovery_revokes_shop_grants_and_preserves_independent_shop_approval(
    owner, machine, setup_product
):
    from extore.account_auth import revoke_shop_auth

    pid, _ = setup_product()
    _, root_tokens, _, root_headers = connected(owner, machine, pid)
    with db() as c:
        sid = c.execute("SELECT shop_id FROM products WHERE id=?", (pid,)).fetchone()[0]
        cookie = create_session(c, Response(), "admin", shop_id=sid)
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as shop_owner:
        shop_owner.cookies.set("extore_session", cookie)
        _, shop_tokens, _, shop_headers = connected(shop_owner, machine, pid)
        before = count_cards()
        with db() as c:
            revoke_shop_auth(c, None)
            values = {
                row["id"]: row["revoked"]
                for row in c.execute("SELECT id,revoked FROM commerce_grants")
            }
            assert values[root_tokens["grant_id"]] == 1
            assert values[shop_tokens["grant_id"]] == 0
        assert machine.get(API + "/products", headers=root_headers).status_code == 401
        assert machine.get(API + "/products", headers=shop_headers).status_code == 200
        with db() as c:
            revoke_shop_auth(c, sid)
        assert machine.get(API + "/products", headers=shop_headers).status_code == 401
        assert count_cards() == before


def test_root_and_machine_commerce_audit_records_belong_to_the_real_shop(
    owner, machine, setup_product
):
    pid, _ = setup_product()
    client, tokens, _, _ = connected(owner, machine, pid)
    assert (
        machine.post(
            API + "/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client["id"],
                "refresh_token": tokens["refresh_token"],
            },
        ).status_code
        == 200
    )
    assert owner.delete(ADMIN + "/grants/" + tokens["grant_id"]).status_code == 200
    with db() as c:
        sid = c.execute("SELECT shop_id FROM products WHERE id=?", (pid,)).fetchone()[0]
        records = c.execute(
            "SELECT action,shop_id,target FROM audit WHERE action LIKE 'commerce.%'"
        ).fetchall()
        assert {row["action"] for row in records} >= {
            "commerce.client.create",
            "commerce.grant.approve",
            "commerce.token.exchange",
            "commerce.token.refresh",
            "commerce.grant.revoke",
        }
        assert all(row["shop_id"] == sid for row in records)

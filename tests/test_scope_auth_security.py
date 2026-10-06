"""Real signed scope requests, bounded discovery and atomic admission."""

import json
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response
from test_cli_auth import b64, identity, product

from extore import scope_auth
from extore.app import app
from extore.db import db
from extore.security import create_session, digest

ORIGIN = "http://localhost:8000"
CLI = "/api/cli/scopes"
BROWSER = "/api/manage/device"
PERMISSIONS = ["queue.view", "queue.process", "queue.retry"]


@pytest.fixture
def scope_clients():
    opened = []

    def create(*, cookie=None):
        client = TestClient(app, base_url=ORIGIN)
        if cookie:
            client.cookies.set("extore_session", cookie)
            client.headers["Origin"] = ORIGIN
        opened.append(client)
        return client

    yield create
    for client in opened:
        client.close()


def _body(
    key,
    pid,
    *,
    nonce=None,
    name="Scope Security Bot",
    origin=ORIGIN,
    permissions=None,
    authorization=None,
    kind="product",
    shop_id=None,
    product_ids=None,
    reason="",
):
    unsigned = {
        "public_key": key[1],
        "client_name": name,
        "nonce": nonce
        or b64(int(time.time()).to_bytes(8, "big") + secrets.token_bytes(24)),
        "kind": kind,
        "shop_id": shop_id,
        "product_ids": [pid] if product_ids is None else product_ids,
        "permissions": PERMISSIONS if permissions is None else permissions,
        "authorization_id": authorization["id"] if authorization else None,
        "expected_revision": authorization["revision"] if authorization else None,
        "reason": reason,
    }
    canonical = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    proof = f"extore-cli-scope-request-v1\n{origin}\n{canonical}"
    return {**unsigned, "signature": b64(key[0].sign(proof.encode()))}


def _pending(client, pid, key=None, **kwargs):
    key = key or identity()
    body = _body(key, pid, **kwargs)
    response = client.post(CLI + "/request", json=body)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["approval_url"] == ORIGIN + "/cli/device"
    assert value["flow"] == "scope"
    assert "set-cookie" not in response.headers
    return value, key, body


def _proof(value, key, action="status", *, origin=ORIGIN):
    proof = f"extore-cli-scope-{action}-v1\n{origin}\n{value['request_id']}\n"
    if action == "claim":
        proof += value["challenge"] + "\n"
    proof += key[1]
    return {
        "request_id": value["request_id"],
        "public_key": key[1],
        "signature": b64(key[0].sign(proof.encode())),
    }


def _review(owner, value, **selection):
    response = owner.post(
        BROWSER + "/options", json={"user_code": value["user_code"], **selection}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _approval(value, context):
    return {
        "user_code": value["user_code"],
        "product_ids": context["selected"]["product_ids"],
        "permissions": context["selected"]["permissions"],
        "expires": context["selected"]["expires"],
        "review_digest": context["review_digest"],
    }


def _approve(owner, value, context=None):
    context = context or _review(owner, value)
    response = owner.post(BROWSER + "/approve", json=_approval(value, context))
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "status": "approved"}
    return context


def _claim(client, value, key):
    response = client.post(CLI + "/claim", json=_proof(value, key, "claim"))
    assert response.status_code == 200, response.text
    assert "set-cookie" not in response.headers
    return response.json()


def _state(value):
    with db() as c:
        return dict(
            c.execute(
                "SELECT * FROM cli_scope_requests WHERE id=?", (value["request_id"],)
            ).fetchone()
        )


def _business_state():
    with db() as c:
        return {
            name: [
                tuple(row) for row in c.execute(f"SELECT * FROM {name} ORDER BY rowid")
            ]
            for name in (
                "pipeline_authorizations",
                "pipeline_bindings",
                "staff",
                "cli_devices",
            )
        }


@pytest.mark.parametrize(
    "field",
    [
        "public_key",
        "client_name",
        "nonce",
        "kind",
        "shop_id",
        "product_ids",
        "permissions",
        "authorization_id",
        "expected_revision",
        "reason",
        "signature",
    ],
)
def test_scope_signature_binds_every_request_field_without_reflection(
    owner, scope_clients, field
):
    pid = product(owner)
    key = identity()
    body = _body(key, pid)
    body[field] = {
        "public_key": identity()[1],
        "client_name": "Unreviewed Bot",
        "nonce": _body(key, pid)["nonce"],
        "kind": "shop.pipeline",
        "shop_id": str(uuid.uuid4()),
        "product_ids": [str(uuid.uuid4())],
        "permissions": ["queue.view"],
        "authorization_id": str(uuid.uuid4()),
        "expected_revision": 2,
        "reason": "Unreviewed reason",
        "signature": "X" * 86,
    }[field]
    response = scope_clients().post(CLI + "/request", json=body)
    assert response.status_code == 401, response.text
    for name in ("signature", "nonce", "public_key"):
        assert body[name] not in response.text
    assert _business_state() == {
        "pipeline_authorizations": [],
        "pipeline_bindings": [],
        "staff": [],
        "cli_devices": [],
    }
    with db() as c:
        assert c.execute("SELECT count(*) FROM cli_scope_requests").fetchone()[0] == 0


def test_scope_request_signature_binds_origin_and_array_order(owner, scope_clients):
    pid = product(owner)
    client, key = scope_clients(), identity()
    body = _body(key, pid, origin="https://evil.example")
    assert client.post(CLI + "/request", json=body).status_code == 401
    body = _body(key, pid)
    body["permissions"] = list(reversed(body["permissions"]))
    assert client.post(CLI + "/request", json=body).status_code == 401
    valid = _body(key, pid, permissions=list(reversed(PERMISSIONS)), reason="文档处理")
    response = client.post(CLI + "/request", json=valid)
    assert response.status_code == 200, response.text
    context = _review(owner, response.json())
    assert context["selected"]["permissions"] == PERMISSIONS
    assert context["request"]["reason"] == "文档处理"


def test_scope_nonce_replay_is_idempotent_and_never_renews_expiry(owner, scope_clients):
    pid = product(owner)
    client = scope_clients()
    value, key, body = _pending(client, pid)
    replay = client.post(CLI + "/request", json=body)
    assert replay.status_code == 200, replay.text
    assert {k: v for k, v in replay.json().items() if k != "expires_in"} == {
        k: v for k, v in value.items() if k != "expires_in"
    }
    altered = _body(key, pid, nonce=body["nonce"], name="Different signed name")
    assert client.post(CLI + "/request", json=altered).status_code == 401
    with db() as c:
        c.execute(
            "UPDATE cli_scope_requests SET expires=? WHERE id=?",
            (time.time() - 1, value["request_id"]),
        )
    expired = client.post(CLI + "/request", json=body)
    assert expired.status_code == 200, expired.text
    assert expired.json()["request_id"] == value["request_id"]
    assert expired.json()["expires_in"] == 0
    assert expired.json()["expires"] < value["expires"]
    with db() as c:
        assert c.execute("SELECT count(*) FROM cli_scope_requests").fetchone()[0] == 1


def test_scope_clock_bounds_and_retired_nonce_cannot_create_a_new_request(
    owner, scope_clients, monkeypatch
):
    pid = product(owner)
    client, key = scope_clients(), identity()
    fixed_now = int(time.time())
    # Freeze only this module's clock: HTTP delays must not turn the +61
    # rejection case into the allowed +60 case, or affect real session checks.
    monkeypatch.setattr(scope_auth, "time", SimpleNamespace(time=lambda: fixed_now))
    for issued in (fixed_now - 600, fixed_now + 61):
        nonce = b64(issued.to_bytes(8, "big") + secrets.token_bytes(24))
        assert (
            client.post(CLI + "/request", json=_body(key, pid, nonce=nonce)).status_code
            == 401
        )
    future_nonce = b64((fixed_now + 60).to_bytes(8, "big") + secrets.token_bytes(24))
    future = client.post(CLI + "/request", json=_body(key, pid, nonce=future_nonce))
    assert future.status_code == 200, future.text
    assert future.json()["expires"] == fixed_now + 600
    value, _, _ = _pending(
        client,
        pid,
        key,
        nonce=b64(fixed_now.to_bytes(8, "big") + secrets.token_bytes(24)),
    )
    retired = b64((fixed_now - 1000).to_bytes(8, "big") + secrets.token_bytes(24))
    with db() as c:
        c.execute(
            "UPDATE cli_scope_requests SET nonce=?,expires=? WHERE id=?",
            (retired, fixed_now - 1000, value["request_id"]),
        )
        assert scope_auth.cleanup_requests(c) == 1
    replay = client.post(CLI + "/request", json=_body(key, pid, nonce=retired))
    assert replay.status_code == 401


@pytest.mark.parametrize("credential", ["cookie", "bearer"])
@pytest.mark.parametrize("endpoint", ["request", "status", "claim"])
def test_scope_handshake_rejects_mixed_credentials(
    owner, scope_clients, credential, endpoint
):
    client = scope_clients()
    value, key, body = _pending(client, product(owner))
    payload = body if endpoint == "request" else _proof(value, key, endpoint)
    if credential == "cookie":
        client.cookies.set("extore_session", owner.cookies.get("extore_session"))
    else:
        client.headers["Authorization"] = "Bearer synthetic-secret"
    response = client.post(CLI + "/" + endpoint, json=payload)
    assert response.status_code == 400, response.text
    assert "synthetic-secret" not in response.text


def test_scope_proofs_bind_device_action_origin_request_and_challenge(
    owner, scope_clients
):
    client = scope_clients()
    pid = product(owner)
    value, key, _ = _pending(client, pid)
    other, _, _ = _pending(client, pid)
    for body in (
        _proof(value, identity()),
        _proof(value, key, "claim"),
        _proof(value, key, origin="https://evil.example"),
        {**_proof(value, key), "request_id": other["request_id"]},
    ):
        assert client.post(CLI + "/status", json=body).status_code == 401
    _approve(owner, value)
    for body in (
        _proof(value, identity(), "claim"),
        _proof(value, key, "status"),
        _proof(value, key, "claim", origin="https://evil.example"),
        _proof({**value, "challenge": other["challenge"]}, key, "claim"),
        {**_proof(value, key, "claim"), "request_id": other["request_id"]},
    ):
        assert client.post(CLI + "/claim", json=body).status_code == 401
    assert _state(value)["state"] == "approved"
    assert _business_state()["pipeline_authorizations"] == []
    assert len(_claim(client, value, key)["bindings"]) == 1


def test_scope_public_status_only_exposes_polling_state_and_persists_backoff(
    owner, scope_clients
):
    client = scope_clients()
    value, key, _ = _pending(client, product(owner))
    body = _proof(value, key)
    first = client.post(CLI + "/status", json=body)
    assert first.status_code == 200, first.text
    assert first.json() == {
        "status": "pending",
        "expires": value["expires"],
        "interval": 5,
    }
    for interval in (10, 15, 20):
        result = client.post(CLI + "/status", json=body).json()
        assert result == {
            "status": "slow_down",
            "expires": value["expires"],
            "interval": interval,
            "retry_after": interval,
        }
        assert _state(value)["poll_interval"] == interval
    with db() as c:
        c.execute(
            "UPDATE cli_scope_requests SET poll_after=0,poll_interval=60 WHERE id=?",
            (value["request_id"],),
        )
    assert client.post(CLI + "/status", json=body).json()["interval"] == 60
    assert client.post(CLI + "/status", json=body).json()["interval"] == 60


def test_expired_scope_code_and_unknown_code_share_bounded_lookup_bucket(
    owner, scope_clients
):
    value, _, _ = _pending(scope_clients(), product(owner))
    with db() as c:
        c.execute(
            "UPDATE cli_scope_requests SET expires=? WHERE id=?",
            (time.time() - 1, value["request_id"]),
        )
    responses = [
        owner.post(BROWSER + "/options", json={"user_code": code})
        for code in [value["user_code"], *("AAAA-AAAA-AAA" + c for c in "BCDEF")]
    ]
    assert [response.status_code for response in responses] == [401] * 5 + [429]
    assert len({response.text for response in responses[:5]}) == 1
    with db() as c:
        keys = [
            row["key"]
            for row in c.execute(
                "SELECT key FROM rate_limits WHERE key LIKE '%device-invalid-code%'"
            )
        ]
    assert len(keys) == 2
    assert all(value["user_code"] not in name for name in keys)


def test_scope_request_total_cap_is_enforced_without_creating_business_rows(
    owner, scope_clients, monkeypatch
):
    monkeypatch.setattr(scope_auth, "MAX_REQUESTS", 2)
    client, pid = scope_clients(), product(owner)
    _pending(client, pid)
    _pending(client, pid)
    response = client.post(CLI + "/request", json=_body(identity(), pid))
    assert response.status_code == 429, response.text
    with db() as c:
        assert c.execute("SELECT count(*) FROM cli_scope_requests").fetchone()[0] == 2
    assert _business_state()["pipeline_authorizations"] == []


def test_scope_request_key_cap_counts_approved_but_releases_denied_requests(
    owner, scope_clients, monkeypatch
):
    monkeypatch.setattr(scope_auth, "MAX_PENDING_PER_KEY", 2)
    client, pid, key = scope_clients(), product(owner), identity()
    first, _, _ = _pending(client, pid, key)
    _approve(owner, first)
    second, _, _ = _pending(client, pid, key)
    response = client.post(CLI + "/request", json=_body(key, pid))
    assert response.status_code == 429, response.text
    denied = owner.post(BROWSER + "/deny", json={"user_code": second["user_code"]})
    assert denied.json() == {"ok": True, "status": "denied"}
    _pending(client, pid, key)
    assert _business_state()["pipeline_authorizations"] == []


def test_scope_expired_request_cleanup_is_bounded_and_preserves_live_and_grace_rows(
    owner, scope_clients
):
    value, _, body = _pending(scope_clients(), product(owner))
    with db() as c:
        original = dict(
            c.execute(
                "SELECT * FROM cli_scope_requests WHERE id=?", (value["request_id"],)
            ).fetchone()
        )
        names = list(original)
        rows = scope_auth.CLEANUP_BATCH + 7
        for index in range(rows + 1):
            old = {
                **original,
                "id": str(uuid.uuid4()),
                "nonce": b64(secrets.token_bytes(32)),
                "user_code": str(index),
                "expires": time.time() - (1000 if index < rows else 1),
            }
            c.execute(
                f"INSERT INTO cli_scope_requests ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                list(old.values()),
            )
        assert scope_auth.cleanup_requests(c) == scope_auth.CLEANUP_BATCH
        assert c.execute("SELECT count(*) FROM cli_scope_requests").fetchone()[0] == 9
        assert (
            c.execute(
                "SELECT nonce FROM cli_scope_requests WHERE id=?",
                (value["request_id"],),
            ).fetchone()[0]
            == body["nonce"]
        )
    assert _business_state()["pipeline_authorizations"] == []


def test_scope_review_is_bound_to_browser_session_and_requires_fresh_authentication(
    owner, scope_clients
):
    value, _, _ = _pending(scope_clients(), product(owner))
    context = _review(owner, value)
    with db() as c:
        cookie = create_session(c, Response(), "admin", auth_method="passkey")
    rotated = scope_clients(cookie=cookie)
    refreshed = _review(rotated, value)
    assert refreshed["review_digest"] != context["review_digest"]
    stale = rotated.post(BROWSER + "/approve", json=_approval(value, context))
    assert stale.status_code == 409, stale.text
    assert _state(value)["state"] == "pending"
    with db() as c:
        c.execute(
            "UPDATE sessions SET auth_at=? WHERE digest=?",
            (time.time() - 601, digest(cookie)),
        )
    old_auth = rotated.post(BROWSER + "/approve", json=_approval(value, refreshed))
    assert old_auth.status_code == 401, old_auth.text
    assert _state(value)["state"] == "pending"
    assert _business_state()["pipeline_authorizations"] == []


@pytest.mark.parametrize("phase", ["approve", "claim"])
@pytest.mark.parametrize("change", ["product_name", "shop_name", "product_scope"])
def test_scope_review_and_first_claim_reject_metadata_drift(
    owner, scope_clients, phase, change
):
    pid = product(owner)
    client = scope_clients()
    value, key, _ = _pending(client, pid)
    context = _review(owner, value)
    if phase == "claim":
        _approve(owner, value, context)
    with db() as c:
        if change == "product_name":
            c.execute(
                "UPDATE products SET config=json_set(config,'$.name','Changed title') WHERE id=?",
                (pid,),
            )
        elif change == "shop_name":
            c.execute(
                "UPDATE shops SET name='Changed store' WHERE id=(SELECT shop_id FROM products WHERE id=?)",
                (pid,),
            )
        else:
            sid = str(uuid.uuid4())
            c.execute(
                "INSERT INTO shops(id,name,created,verified) VALUES (?,?,?,1)",
                (sid, "Other shop", time.time()),
            )
            c.execute("UPDATE products SET shop_id=? WHERE id=?", (sid, pid))
    if phase == "approve":
        response = owner.post(BROWSER + "/approve", json=_approval(value, context))
    else:
        response = client.post(CLI + "/claim", json=_proof(value, key, "claim"))
    assert 400 <= response.status_code < 500, response.text
    assert _business_state()["pipeline_authorizations"] == []


@pytest.mark.parametrize("change", ["revoked", "expired", "role", "shop", "disabled"])
def test_first_scope_claim_requires_original_browser_session_authority(
    owner, scope_clients, change
):
    client, pid = scope_clients(), product(owner)
    value, key, _ = _pending(client, pid)
    _approve(owner, value)
    with db() as c:
        cookie_digest = digest(owner.cookies.get("extore_session"))
        if change == "revoked":
            c.execute("UPDATE sessions SET revoked=1 WHERE digest=?", (cookie_digest,))
        elif change == "expired":
            c.execute(
                "UPDATE sessions SET expires=? WHERE digest=?",
                (time.time() - 1, cookie_digest),
            )
        elif change == "role":
            c.execute(
                "UPDATE sessions SET role='bootstrap' WHERE digest=?", (cookie_digest,)
            )
        elif change == "shop":
            sid = str(uuid.uuid4())
            c.execute(
                "INSERT INTO shops(id,name,created,verified) VALUES (?,?,?,1)",
                (sid, "Other shop", time.time()),
            )
            c.execute(
                "UPDATE sessions SET shop_id=? WHERE digest=?", (sid, cookie_digest)
            )
        else:
            c.execute(
                "UPDATE shops SET enabled=0 WHERE id=(SELECT shop_id FROM products WHERE id=?)",
                (pid,),
            )
    response = client.post(CLI + "/claim", json=_proof(value, key, "claim"))
    assert response.status_code == 401, response.text
    assert _business_state()["pipeline_authorizations"] == []


@pytest.mark.parametrize("outcome", ["pending", "approved", "denied", "expired"])
def test_upgrade_until_successful_claim_never_mutates_current_scope(
    owner, scope_clients, outcome
):
    client, pid = scope_clients(), product(owner)
    first, key, _ = _pending(client, pid, permissions=["queue.view"])
    _approve(owner, first)
    original = _claim(client, first, key)
    before = _business_state()
    value, _, _ = _pending(
        client,
        pid,
        key,
        authorization=original["authorization"],
        permissions=PERMISSIONS,
    )
    context = _review(owner, value)
    assert context["current"]["id"] == original["authorization"]["id"]
    if outcome in ("approved", "expired"):
        _approve(owner, value, context)
    elif outcome == "denied":
        response = owner.post(BROWSER + "/deny", json={"user_code": value["user_code"]})
        assert response.status_code == 200, response.text
    if outcome == "expired":
        with db() as c:
            c.execute(
                "UPDATE cli_scope_requests SET expires=? WHERE id=?",
                (time.time() - 1, value["request_id"]),
            )
    if outcome != "approved":
        response = client.post(CLI + "/claim", json=_proof(value, key, "claim"))
        assert response.status_code == 401, response.text
    assert _business_state() == before


@pytest.mark.parametrize("phase", ["approve", "claim"])
@pytest.mark.parametrize("change", ["revision", "permission", "device_revoke"])
def test_upgrade_review_rejects_changed_old_authority_without_partial_mutation(
    owner, scope_clients, phase, change
):
    client, pid = scope_clients(), product(owner)
    first, key, _ = _pending(client, pid, permissions=["queue.view"])
    _approve(owner, first)
    original = _claim(client, first, key)
    aid = original["authorization"]["id"]
    value, _, _ = _pending(
        client,
        pid,
        key,
        authorization=original["authorization"],
        permissions=PERMISSIONS,
    )
    context = _review(owner, value)
    if phase == "claim":
        _approve(owner, value, context)
    with db() as c:
        if change == "revision":
            c.execute(
                "UPDATE pipeline_authorizations SET revision=revision+1 WHERE id=?",
                (aid,),
            )
        elif change == "permission":
            c.execute(
                "UPDATE pipeline_authorizations SET permissions=? WHERE id=?",
                ('["queue.view","queue.process"]', aid),
            )
        else:
            c.execute(
                "UPDATE cli_devices SET revoked=1 WHERE id=?",
                (original["bindings"][0]["device_id"],),
            )
    changed = _business_state()
    if phase == "approve":
        response = owner.post(BROWSER + "/approve", json=_approval(value, context))
    else:
        response = client.post(CLI + "/claim", json=_proof(value, key, "claim"))
    assert response.status_code in (401, 409), response.text
    assert _business_state() == changed


def test_parallel_same_request_claim_creates_only_one_scope_and_binding(
    owner, scope_clients
):
    value, key, _ = _pending(scope_clients(), product(owner))
    _approve(owner, value)
    barrier = Barrier(2)

    def run(_):
        with TestClient(app, base_url=ORIGIN) as client:
            barrier.wait(timeout=10)
            return client.post(CLI + "/claim", json=_proof(value, key, "claim"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(run, range(2)))
    assert [response.status_code for response in responses] == [200, 200]
    assert len({response.json()["authorization"]["id"] for response in responses}) == 1
    assert (
        len({response.json()["bindings"][0]["device_id"] for response in responses})
        == 1
    )
    state = _business_state()
    assert all(len(rows) == 1 for rows in state.values())
    with db() as c:
        assert c.execute("SELECT cli_uses,max_cli_uses FROM staff").fetchone()[:] == (
            1,
            1,
        )
        assert (
            c.execute("SELECT state FROM cli_scope_requests").fetchone()[0] == "claimed"
        )
        assert (
            c.execute(
                "SELECT count(*) FROM audit WHERE action='cli.scope.claim'"
            ).fetchone()[0]
            == 1
        )


def test_parallel_distinct_requests_for_same_scope_require_explicit_recovery_review(
    owner, scope_clients
):
    client, pid, key = scope_clients(), product(owner), identity()
    first, _, _ = _pending(client, pid, key)
    second, _, _ = _pending(client, pid, key)
    _approve(owner, first)
    _approve(owner, second)
    barrier = Barrier(2)

    def run(value):
        with TestClient(app, base_url=ORIGIN) as worker:
            barrier.wait(timeout=10)
            return worker.post(CLI + "/claim", json=_proof(value, key, "claim"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(run, [first, second]))
    assert sorted(response.status_code for response in responses) == [200, 409]
    state = _business_state()
    assert all(len(rows) == 1 for rows in state.values())


def test_scope_audit_and_safe_views_never_include_handshake_credentials(
    owner, scope_clients
):
    client, pid = scope_clients(), product(owner)
    value, key, body = _pending(client, pid, reason="Secret reason marker")
    context = _approve(owner, value)
    result = _claim(client, value, key)
    with db() as c:
        audit_rows = [dict(row) for row in c.execute("SELECT * FROM audit")]
        c.execute(
            "UPDATE cli_scope_requests SET expires=? WHERE id=?",
            (time.time() - 1000, value["request_id"]),
        )
        assert scope_auth.cleanup_requests(c) == 1
        durable = dict(
            c.execute("SELECT * FROM audit WHERE action='cli.scope.claim'").fetchone()
        )
    serialized = json.dumps(audit_rows)
    for secret in (
        body["nonce"],
        body["signature"],
        key[1],
        value["challenge"],
        value["user_code"],
        "Secret reason marker",
        owner.cookies.get("extore_session"),
        digest(owner.cookies.get("extore_session")),
    ):
        assert secret not in serialized
    assert {row["action"] for row in audit_rows} >= {
        "cli.scope.request",
        "cli.scope.approve",
        "cli.scope.create",
        "cli.scope.claim",
    }
    assert durable["target"] == result["authorization"]["id"]
    response = owner.get("/api/admin/pipeline-authorizations")
    assert response.status_code == 200, response.text
    public_json = json.dumps([context, result, response.json()])
    for secret in (key[1], body["nonce"], value["challenge"], body["signature"]):
        assert secret not in public_json

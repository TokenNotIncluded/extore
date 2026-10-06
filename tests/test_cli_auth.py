"""CLI proof of key ownership, bounded grants, and browser-channel isolation."""

import base64
import json
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from extore.app import app
from extore.db import db
from extore.security import digest

ORIGIN = "http://localhost:8000"


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def identity():
    private = Ed25519PrivateKey.generate()
    public = b64(
        private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    )
    return private, public


def bind_body(token, key, name="CLI test device", *, proof_origin=ORIGIN):
    private, public = key
    proof = f"extore-cli-bind-v1\n{proof_origin}\n{token}\n{public}"
    return {
        "token": token,
        "public_key": public,
        "client_name": name,
        "signature": b64(private.sign(proof.encode())),
    }


def session_body(device_id, challenge, key, *, proof_origin=ORIGIN):
    proof = (
        f"extore-cli-session-v1\n{proof_origin}\n{device_id}\n"
        f"{challenge['challenge_id']}\n{challenge['challenge']}"
    )
    return {
        "device_id": device_id,
        "challenge_id": challenge["challenge_id"],
        "signature": b64(key[0].sign(proof.encode())),
    }


@pytest.fixture
def clients():
    opened = []

    def create():
        # Public CLI endpoints must work without a browser Origin or cookie.
        client = TestClient(app, base_url=ORIGIN)
        opened.append(client)
        return client

    yield create
    for client in opened:
        client.close()


def product(owner):
    response = owner.post(
        "/api/admin/products", json={"name": "CLI scope test", "parameters": []}
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def link(owner, pid, *, permissions=None, max_uses=1, max_cli_uses=1):
    response = owner.post(
        "/api/admin/staff",
        json={
            "product_id": pid,
            "name": "CLI authorization test",
            "permissions": permissions or ["queue.view"],
            "max_uses": max_uses,
            "max_cli_uses": max_cli_uses,
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    return result, result["url"].split("#", 1)[1]


def authorize(client, token, key=None, **kwargs):
    key = key or identity()
    response = client.post("/api/cli/authorize", json=bind_body(token, key, **kwargs))
    assert response.status_code == 200, response.text
    assert "set-cookie" not in response.headers
    assert client.cookies.get("extore_session") is None
    return response.json(), key


def challenge(client, device_id):
    response = client.post("/api/cli/challenge", json={"device_id": device_id})
    assert response.status_code == 200, response.text
    result = response.json()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", result["challenge"])
    assert result["expires_in"] == 300
    assert time.time() + 290 < result["expires"] <= time.time() + 301
    assert "set-cookie" not in response.headers
    return result


def login(client, device, key):
    nonce = challenge(client, device["device_id"])
    response = client.post(
        "/api/cli/session", json=session_body(device["device_id"], nonce, key)
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["token_type"] == "Bearer"
    assert result["expires_in"] == 8 * 3600
    assert result["device_id"] == device["device_id"]
    assert result["product_id"] == device["product_id"]
    assert result["permissions"] == device["permissions"]
    assert time.time() + 8 * 3600 - 10 < result["expires"] <= time.time() + 8 * 3600 + 1
    assert "set-cookie" not in response.headers
    return result


def bearer(session):
    return {"Authorization": "Bearer " + session["access_token"]}


def staff_browser(clients, token):
    client = clients()
    response = client.post(
        "/api/staff/login", json={"token": token}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 200, response.text
    return client


def quotas(sid):
    with db() as c:
        row = c.execute(
            "SELECT uses,max_uses,cli_uses,max_cli_uses FROM staff WHERE id=?", (sid,)
        ).fetchone()
    return tuple(row)


def denied(response):
    assert 400 <= response.status_code < 500, response.text


def test_browser_and_cli_admission_have_independent_quotas(owner, clients):
    grant, token = link(owner, product(owner))
    client = clients()
    device, key = authorize(client, token)
    assert not device["already_authorized"]
    assert device["max_cli_uses"] == device["cli_uses"] == 1
    assert device["remaining_cli_uses"] == 0
    assert quotas(grant["id"]) == (0, 1, 1, 1)
    browser = staff_browser(clients, token)
    assert quotas(grant["id"]) == (1, 1, 1, 1)
    denied(
        clients().post(
            "/api/staff/login", json={"token": token}, headers={"Origin": ORIGIN}
        )
    )
    response = client.post("/api/cli/authorize", json=bind_body(token, identity()))
    assert response.status_code == 409, response.text
    existing, _ = authorize(client, token, key)
    assert existing["already_authorized"]
    assert existing["device_id"] == device["device_id"]
    assert quotas(grant["id"]) == (1, 1, 1, 1)
    session = login(client, existing, key)
    assert (
        client.get("/api/manage/products", headers=bearer(session)).status_code == 200
    )
    assert browser.get("/api/manage/products").status_code == 200


def test_binding_exact_full_link_and_domain_separates_the_proof(owner, clients):
    grant, token = link(owner, product(owner))
    client, key = clients(), identity()
    signed_raw = bind_body(token, key)
    signed_raw["token"] = grant["url"]
    denied(client.post("/api/cli/authorize", json=signed_raw))
    for bad_token in (
        "https://example.invalid/staff#" + token,
        ORIGIN + "/receipt#" + token,
        ORIGIN + "/staff?token=" + token,
        ORIGIN + "/cli#" + token,
    ):
        denied(client.post("/api/cli/authorize", json=bind_body(bad_token, key)))
    denied(
        client.post(
            "/api/cli/authorize",
            json=bind_body(token, key, proof_origin="https://example.invalid"),
        )
    )
    assert quotas(grant["id"]) == (0, 1, 0, 1)
    device, _ = authorize(client, grant["url"], key)
    again, _ = authorize(client, token, key)
    assert again["device_id"] == device["device_id"]
    assert again["already_authorized"]
    assert quotas(grant["id"]) == (0, 1, 1, 1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("public_key", b64(b"x" * 31)),
        ("public_key", b64(b"x" * 33)),
        ("public_key", "A" * 43 + "="),
        ("public_key", "/" * 43),
        ("signature", b64(b"x" * 63)),
        ("signature", b64(b"x" * 65)),
        ("signature", "A" * 86 + "=="),
        ("signature", "+" * 86),
        ("client_name", ""),
        ("client_name", "x" * 101),
    ],
)
def test_invalid_bind_input_never_consumes_quota(owner, clients, field, value):
    grant, token = link(owner, product(owner))
    body = bind_body(token, identity())
    body[field] = value
    denied(clients().post("/api/cli/authorize", json=body))
    assert quotas(grant["id"]) == (0, 1, 0, 1)
    with db() as c:
        assert c.execute("SELECT count(*) FROM cli_devices").fetchone()[0] == 0


def test_wrong_key_and_modified_token_do_not_bind_a_device(owner, clients):
    grant, token = link(owner, product(owner))
    key, attacker = identity(), identity()
    body = bind_body(token, key)
    body["public_key"] = attacker[1]
    denied(clients().post("/api/cli/authorize", json=body))
    body = bind_body(token, key)
    body["token"] = token[:-1] + ("a" if token[-1] != "a" else "b")
    denied(clients().post("/api/cli/authorize", json=body))
    assert quotas(grant["id"]) == (0, 1, 0, 1)


def test_concurrent_distinct_devices_cannot_overdraw_cli_quota(owner, clients):
    grant, token = link(owner, product(owner))
    candidates = [(clients(), bind_body(token, identity())) for _ in range(6)]
    ready = Barrier(len(candidates))

    def attempt(candidate):
        client, body = candidate
        ready.wait(timeout=10)
        return client.post("/api/cli/authorize", json=body).status_code

    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        statuses = list(pool.map(attempt, candidates))
    assert statuses.count(200) == 1
    assert statuses.count(409) == len(candidates) - 1
    assert quotas(grant["id"]) == (0, 1, 1, 1)
    with db() as c:
        assert c.execute("SELECT count(*) FROM cli_devices").fetchone()[0] == 1


def test_challenge_is_bound_to_device_key_origin_and_consumed_once(owner, clients):
    _, token = link(owner, product(owner), max_cli_uses=2)
    client = clients()
    first, first_key = authorize(client, token)
    second, second_key = authorize(client, token)
    nonce = challenge(client, first["device_id"])
    denied(
        client.post(
            "/api/cli/session",
            json=session_body(first["device_id"], nonce, second_key),
        )
    )
    correct = session_body(first["device_id"], nonce, first_key)
    recovered = client.post("/api/cli/session", json=correct)
    assert recovered.status_code == 200, recovered.text
    denied(client.post("/api/cli/session", json=correct))
    nonce = challenge(client, first["device_id"])
    denied(
        client.post(
            "/api/cli/session",
            json=session_body(second["device_id"], nonce, second_key),
        )
    )
    nonce = challenge(client, first["device_id"])
    denied(
        client.post(
            "/api/cli/session",
            json=session_body(
                first["device_id"],
                nonce,
                first_key,
                proof_origin="https://example.invalid",
            ),
        )
    )
    nonce = challenge(client, first["device_id"])
    body = session_body(first["device_id"], nonce, first_key)
    response = client.post("/api/cli/session", json=body)
    assert response.status_code == 200, response.text
    denied(client.post("/api/cli/session", json=body))
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM cli_challenges WHERE id=?",
                (nonce["challenge_id"],),
            ).fetchone()[0]
            == 0
        )


def test_concurrent_session_exchange_cannot_replay_one_challenge(owner, clients):
    _, token = link(owner, product(owner))
    client = clients()
    device, key = authorize(client, token)
    nonce = challenge(client, device["device_id"])
    body = session_body(device["device_id"], nonce, key)
    requesters = [clients() for _ in range(4)]
    ready = Barrier(len(requesters))

    def attempt(requester):
        ready.wait(timeout=10)
        return requester.post("/api/cli/session", json=body).status_code

    with ThreadPoolExecutor(max_workers=len(requesters)) as pool:
        statuses = list(pool.map(attempt, requesters))
    assert statuses.count(200) == 1
    assert all(400 <= status < 500 for status in statuses if status != 200)
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM sessions WHERE device_id=?",
                (device["device_id"],),
            ).fetchone()[0]
            == 1
        )


def test_expired_challenge_and_bearer_require_fresh_key_proof(owner, clients):
    grant, token = link(owner, product(owner))
    client = clients()
    device, key = authorize(client, token)
    nonce = challenge(client, device["device_id"])
    with db() as c:
        c.execute(
            "UPDATE cli_challenges SET expires=? WHERE id=?",
            (time.time() - 1, nonce["challenge_id"]),
        )
    denied(
        client.post(
            "/api/cli/session", json=session_body(device["device_id"], nonce, key)
        )
    )
    first = login(client, device, key)
    with db() as c:
        c.execute(
            "UPDATE sessions SET expires=? WHERE id=?",
            (time.time() - 1, first["session_id"]),
        )
    assert client.get("/api/cli/status", headers=bearer(first)).status_code == 401
    second = login(client, device, key)
    assert second["session_id"] != first["session_id"]
    assert second["access_token"] != first["access_token"]
    assert client.get("/api/cli/status", headers=bearer(second)).status_code == 200
    assert quotas(grant["id"]) == (0, 1, 1, 1)


def test_bearer_is_hashed_and_never_accepted_as_a_browser_cookie(owner, clients):
    _, token = link(owner, product(owner))
    client = clients()
    device, key = authorize(client, token)
    session = login(client, device, key)
    with db() as c:
        row = dict(
            c.execute(
                "SELECT * FROM sessions WHERE id=?", (session["session_id"],)
            ).fetchone()
        )
        assert row["digest"] == digest(session["access_token"])
        assert row["channel"] == "cli"
        assert row["device_id"] == device["device_id"]
        assert session["access_token"] not in json.dumps(row)
    browser = clients()
    browser.cookies.set("extore_session", session["access_token"])
    assert browser.get("/api/manage/products").status_code == 401
    assert browser.get("/api/cli/status").status_code == 401
    assert owner.get("/api/cli/status").status_code == 401
    cookie = owner.cookies.get("extore_session")
    assert (
        clients()
        .get("/api/cli/status", headers={"Authorization": "Bearer " + cookie})
        .status_code
        == 401
    )
    assert (
        client.get(
            "/api/cli/status", headers={"Authorization": session["access_token"]}
        ).status_code
        == 401
    )


def test_bearer_scopes_management_and_mixed_cookie_credentials_are_rejected(
    owner, clients
):
    pid, other_pid = product(owner), product(owner)
    _, token = link(owner, pid, permissions=["queue.view", "cards.manage"])
    client = clients()
    device, key = authorize(client, token)
    session = login(client, device, key)
    headers = bearer(session)
    owner_cookie = owner.cookies.get("extore_session")
    for path in ("/api/manage/products", "/api/admin/products", "/api/cli/status"):
        response = owner.get(path, headers=headers)
        assert response.status_code in (400, 401), response.text
    assert owner.cookies.get("extore_session") == owner_cookie
    assert owner.get("/api/admin/products").status_code == 200
    scoped = client.get("/api/manage/products", headers=headers)
    assert scoped.status_code == 200, scoped.text
    assert {row["id"] for row in scoped.json()} == {pid}
    denied(client.get("/api/admin/products", headers=headers))
    own = client.post(
        "/api/manage/cards", json={"product_id": pid, "count": 1}, headers=headers
    )
    assert own.status_code == 200, own.text
    assert "set-cookie" not in own.headers
    denied(
        client.post(
            "/api/manage/cards",
            json={"product_id": other_pid, "count": 1},
            headers=headers,
        )
    )
    exchanged = owner.post("/api/exchange", json={"code": own.json()["codes"][0]})
    assert exchanged.status_code == 200, exchanged.text
    job = owner.post(
        "/api/redeem", json={"token": exchanged.json()["token"], "params": {}}
    )
    assert job.status_code == 200, job.text
    forbidden = client.post(
        "/api/manage/batch",
        json={"product_id": pid, "ids": [job.json()["id"]], "action": "claim"},
        headers=headers,
    )
    assert forbidden.status_code == 403, forbidden.text
    browser = staff_browser(clients, token)
    # Cookie-only mutation still needs Origin; it cannot use the CLI exemption.
    assert (
        browser.post(
            "/api/manage/cards", json={"product_id": pid, "count": 1}
        ).status_code
        == 403
    )
    assert (
        browser.post(
            "/api/manage/cards",
            json={"product_id": pid, "count": 1},
            headers={"Origin": ORIGIN},
        ).status_code
        == 200
    )


def test_current_cli_logout_does_not_revoke_device_other_bearers_or_browser(
    owner, clients
):
    grant, token = link(owner, product(owner))
    browser = staff_browser(clients, token)
    browser_cookie = browser.cookies.get("extore_session")
    client = clients()
    device, key = authorize(client, token)
    first, second = login(client, device, key), login(client, device, key)
    response = client.delete("/api/cli/session", headers=bearer(first))
    assert response.status_code == 200, response.text
    assert "set-cookie" not in response.headers
    assert client.get("/api/cli/status", headers=bearer(first)).status_code == 401
    assert client.get("/api/cli/status", headers=bearer(second)).status_code == 200
    assert browser.get("/api/manage/products").status_code == 200
    assert browser.cookies.get("extore_session") == browser_cookie
    renewed = login(client, device, key)
    assert client.get("/api/cli/status", headers=bearer(renewed)).status_code == 200
    assert quotas(grant["id"]) == (1, 1, 1, 1)
    with db() as c:
        assert (
            c.execute(
                "SELECT revoked FROM cli_devices WHERE id=?", (device["device_id"],)
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT revoked FROM sessions WHERE id=?", (first["session_id"],)
            ).fetchone()[0]
            == 1
        )


def test_device_revocation_ends_every_cli_session_without_touching_browser(
    owner, clients
):
    grant, token = link(owner, product(owner))
    browser = staff_browser(clients, token)
    client = clients()
    device, key = authorize(client, token)
    sessions = [login(client, device, key), login(client, device, key)]
    pending = challenge(client, device["device_id"])
    response = owner.delete(f"/api/admin/cli-devices/{device['device_id']}")
    assert response.status_code == 200, response.text
    assert response.json()["revoked_sessions"] == 2
    for session in sessions:
        assert client.get("/api/cli/status", headers=bearer(session)).status_code == 401
    denied(client.post("/api/cli/challenge", json={"device_id": device["device_id"]}))
    denied(
        client.post(
            "/api/cli/session", json=session_body(device["device_id"], pending, key)
        )
    )
    denied(client.post("/api/cli/authorize", json=bind_body(token, key)))
    assert browser.get("/api/manage/products").status_code == 200
    assert quotas(grant["id"]) == (1, 1, 1, 1)
    with db() as c:
        assert (
            c.execute(
                "SELECT revoked FROM cli_devices WHERE id=?", (device["device_id"],)
            ).fetchone()[0]
            == 1
        )


def test_ticket_creation_is_free_but_signed_binding_is_one_time_and_cli_only(
    owner, clients
):
    grant, token = link(owner, product(owner))
    browser = staff_browser(clients, token)
    issued = browser.post("/api/manage/cli-ticket", json={}, headers={"Origin": ORIGIN})
    assert issued.status_code == 200, issued.text
    ticket = issued.json()
    assert ticket["origin"] == ORIGIN
    assert ticket["staff_id"] == grant["id"]
    assert ticket["product_id"] == grant["product_id"]
    assert ticket["expires_in"] == 300
    assert time.time() + 290 < ticket["expires"] <= time.time() + 301
    assert quotas(grant["id"]) == (1, 1, 0, 1)
    client = clients()
    denied(
        client.post(
            "/api/staff/login",
            json={"token": ticket["token"]},
            headers={"Origin": ORIGIN},
        )
    )
    denied(
        client.get(
            "/api/manage/products",
            headers={"Authorization": "Bearer " + ticket["token"]},
        )
    )
    key = identity()
    device, _ = authorize(client, ORIGIN + "/cli#" + ticket["token"], key)
    assert device["product_id"] == grant["product_id"]
    assert quotas(grant["id"]) == (1, 1, 1, 1)
    existing, _ = authorize(client, ticket["token"], key)
    assert existing["already_authorized"]
    assert existing["device_id"] == device["device_id"]
    assert quotas(grant["id"]) == (1, 1, 1, 1)
    denied(
        client.post("/api/cli/authorize", json=bind_body(ticket["token"], identity()))
    )
    with db() as c:
        stored = dict(
            c.execute(
                "SELECT * FROM cli_bind_tickets WHERE digest=?",
                (digest(ticket["token"]),),
            ).fetchone()
        )
        assert stored["consumed"] == 1
        assert ticket["token"] not in json.dumps(stored)
    assert login(client, device, key)["device_id"] == device["device_id"]


def test_expired_ticket_and_bad_signature_leave_cli_quota_available(owner, clients):
    grant, _ = link(owner, product(owner))
    response = owner.post("/api/manage/cli-ticket", json={"staff_id": grant["id"]})
    assert response.status_code == 200, response.text
    ticket, key = response.json(), identity()
    body = bind_body(ticket["token"], key)
    body["signature"] = b64(b"\0" * 64)
    denied(clients().post("/api/cli/authorize", json=body))
    with db() as c:
        assert (
            c.execute(
                "SELECT consumed FROM cli_bind_tickets WHERE digest=?",
                (digest(ticket["token"]),),
            ).fetchone()[0]
            == 0
        )
        c.execute(
            "UPDATE cli_bind_tickets SET expires=? WHERE digest=?",
            (time.time() - 1, digest(ticket["token"])),
        )
    denied(clients().post("/api/cli/authorize", json=bind_body(ticket["token"], key)))
    assert quotas(grant["id"]) == (0, 1, 0, 1)


def test_staff_can_only_issue_ticket_for_its_own_link(owner, clients):
    pid = product(owner)
    parent, token = link(owner, pid, permissions=["queue.view", "links.delegate"])
    browser = staff_browser(clients, token)
    child = browser.post(
        "/api/manage/links",
        json={"name": "Child", "permissions": ["queue.view"]},
        headers={"Origin": ORIGIN},
    ).json()
    other, _ = link(owner, product(owner))
    for forbidden in (child["id"], other["id"]):
        denied(
            browser.post(
                "/api/manage/cli-ticket",
                json={"staff_id": forbidden},
                headers={"Origin": ORIGIN},
            )
        )
    own = browser.post(
        "/api/manage/cli-ticket",
        json={"staff_id": parent["id"]},
        headers={"Origin": ORIGIN},
    )
    assert own.status_code == 200, own.text
    assert own.json()["staff_id"] == parent["id"]


def test_devices_are_visible_and_revocable_only_within_link_hierarchy(owner, clients):
    pid = product(owner)
    parent, parent_token = link(
        owner, pid, permissions=["queue.view", "queue.process", "links.delegate"]
    )
    parent_browser = staff_browser(clients, parent_token)
    child_response = parent_browser.post(
        "/api/manage/links",
        json={"name": "Child", "permissions": ["queue.view", "links.delegate"]},
        headers={"Origin": ORIGIN},
    )
    assert child_response.status_code == 200, child_response.text
    child = child_response.json()
    child_browser = staff_browser(clients, child["url"].split("#", 1)[1])
    grand_response = child_browser.post(
        "/api/manage/links",
        json={"name": "Leaf", "permissions": ["queue.view"]},
        headers={"Origin": ORIGIN},
    )
    assert grand_response.status_code == 200, grand_response.text
    grand = grand_response.json()
    leaf_browser = staff_browser(clients, grand["url"].split("#", 1)[1])
    peer, peer_token = link(owner, pid)
    foreign, foreign_token = link(owner, product(owner))
    links = [
        (parent, parent_token),
        (child, child["url"].split("#", 1)[1]),
        (grand, grand["url"].split("#", 1)[1]),
        (peer, peer_token),
        (foreign, foreign_token),
    ]
    devices = {}
    for grant, token in links:
        device, _ = authorize(clients(), token)
        devices[grant["id"]] = device
    parent_rows = parent_browser.get("/api/manage/cli-devices").json()
    assert {row["link_id"] for row in parent_rows} == {
        parent["id"],
        child["id"],
        grand["id"],
    }
    child_rows = child_browser.get("/api/manage/cli-devices").json()
    assert {row["link_id"] for row in child_rows} == {child["id"], grand["id"]}
    own_rows = leaf_browser.get("/api/manage/cli-devices").json()
    assert {row["link_id"] for row in own_rows} == {grand["id"]}
    assert len(owner.get("/api/admin/cli-devices").json()) == 5
    for sid in (parent["id"], child["id"], peer["id"], foreign["id"]):
        response = leaf_browser.delete(
            f"/api/manage/cli-devices/{devices[sid]['device_id']}",
            headers={"Origin": ORIGIN},
        )
        assert response.status_code == 404, response.text
    denied(leaf_browser.get("/api/admin/cli-devices"))
    response = parent_browser.delete(
        f"/api/manage/cli-devices/{devices[grand['id']]['device_id']}",
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("change", ["revoke", "expire", "permissions"])
def test_bearer_and_new_proofs_revalidate_ancestor_authorization(
    owner, clients, change
):
    parent, parent_token = link(
        owner,
        product(owner),
        permissions=["queue.view", "queue.process", "links.delegate"],
    )
    browser = staff_browser(clients, parent_token)
    response = browser.post(
        "/api/manage/links",
        json={
            "name": "Child",
            "permissions": ["queue.view", "queue.process"],
            "max_cli_uses": 2,
        },
        headers={"Origin": ORIGIN},
    )
    # The delegated quota cannot exceed its parent's quota.
    assert response.status_code == 403, response.text
    response = browser.post(
        "/api/manage/links",
        json={"name": "Child", "permissions": ["queue.view", "queue.process"]},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200, response.text
    child = response.json()
    client = clients()
    device, key = authorize(client, child["url"].split("#", 1)[1])
    session = login(client, device, key)
    pending = challenge(client, device["device_id"])
    codes = owner.post(
        "/api/admin/cards", json={"product_id": parent["product_id"], "count": 1}
    )
    assert codes.status_code == 200, codes.text
    exchanged = owner.post("/api/exchange", json={"code": codes.json()["codes"][0]})
    assert exchanged.status_code == 200, exchanged.text
    task = owner.post(
        "/api/redeem", json={"token": exchanged.json()["token"], "params": {}}
    )
    assert task.status_code == 200, task.text
    with db() as c:
        if change == "revoke":
            c.execute("UPDATE staff SET revoked=1 WHERE id=?", (parent["id"],))
        elif change == "expire":
            c.execute(
                "UPDATE staff SET expires=? WHERE id=?", (time.time() - 1, parent["id"])
            )
        else:
            c.execute(
                "UPDATE staff SET permissions=? WHERE id=?",
                (json.dumps(["queue.view", "links.delegate"]), parent["id"]),
            )
    if change == "permissions":
        status = client.get("/api/cli/status", headers=bearer(session))
        assert status.status_code == 200, status.text
        assert status.json()["permissions"] == ["queue.view"]
        assert (
            client.get("/api/manage/products", headers=bearer(session)).status_code
            == 200
        )
        renewed_challenge = client.post(
            "/api/cli/challenge", json={"device_id": device["device_id"]}
        )
        assert renewed_challenge.status_code == 200, renewed_challenge.text
        renewed = client.post(
            "/api/cli/session", json=session_body(device["device_id"], pending, key)
        )
        assert renewed.status_code == 200, renewed.text
        assert renewed.json()["permissions"] == ["queue.view"]
        existing, _ = authorize(client, child["url"].split("#", 1)[1], key)
        assert existing["permissions"] == ["queue.view"]
        response = client.post(
            "/api/manage/batch",
            json={
                "product_id": parent["product_id"],
                "ids": [task.json()["id"]],
                "action": "claim",
            },
            headers=bearer(session),
        )
        assert response.status_code == 403, response.text
        return
    denied(client.get("/api/cli/status", headers=bearer(session)))
    denied(client.get("/api/manage/products", headers=bearer(session)))
    denied(client.post("/api/cli/challenge", json={"device_id": device["device_id"]}))
    denied(
        client.post(
            "/api/cli/session", json=session_body(device["device_id"], pending, key)
        )
    )
    denied(
        client.post(
            "/api/cli/authorize", json=bind_body(child["url"].split("#", 1)[1], key)
        )
    )


def test_root_auth_reset_preserves_product_devices_and_shop_authorizations(
    owner, clients, monkeypatch
):
    from extore import cli

    grant, token = link(owner, product(owner), max_cli_uses=2)
    client = clients()
    device, key = authorize(client, token)
    session = login(client, device, key)
    pending = challenge(client, device["device_id"])
    ticket_response = owner.post(
        "/api/manage/cli-ticket", json={"staff_id": grant["id"]}
    )
    assert ticket_response.status_code == 200
    ticket = ticket_response.json()["token"]
    monkeypatch.setattr("builtins.input", lambda prompt: "RESET")
    monkeypatch.setattr(
        cli.getpass, "getpass", lambda prompt: "cli-auth-test-recovery-password"
    )
    cli.main(["reset-auth"])
    assert owner.get("/api/admin/products").status_code == 401
    assert client.get("/api/cli/status", headers=bearer(session)).status_code == 200
    assert (
        client.post(
            "/api/cli/session", json=session_body(device["device_id"], pending, key)
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/cli/authorize", json=bind_body(ticket, identity())
        ).status_code
        == 200
    )
    with db() as c:
        assert (
            c.execute(
                "SELECT revoked FROM cli_devices WHERE id=?", (device["device_id"],)
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT count(*) FROM products WHERE id=?", (grant["product_id"],)
            ).fetchone()[0]
            == 1
        )


def test_metadata_and_audit_do_not_expose_credentials_or_signature_material(
    owner, clients
):
    grant, token = link(owner, product(owner))
    client, key = clients(), identity()
    request = bind_body(token, key)
    response = client.post("/api/cli/authorize", json=request)
    assert response.status_code == 200, response.text
    device = response.json()
    nonce = challenge(client, device["device_id"])
    proof = session_body(device["device_id"], nonce, key)
    response = client.post("/api/cli/session", json=proof)
    assert response.status_code == 200, response.text
    session = response.json()
    status = client.get("/api/cli/status", headers=bearer(session))
    assert status.status_code == 200, status.text
    devices = owner.get("/api/admin/cli-devices")
    assert devices.status_code == 200, devices.text
    assert devices.json()[0]["fingerprint"] == device["fingerprint"]
    assert devices.json()[0]["id"] == device["device_id"]
    assert client.delete("/api/cli/session", headers=bearer(session)).status_code == 200
    with db() as c:
        rows = [dict(row) for row in c.execute("SELECT * FROM audit ORDER BY created")]
        assert any(row["target"] == device["device_id"] for row in rows)
        assert any(row["target"] == session["session_id"] for row in rows)
    public = status.text + devices.text + json.dumps(rows)
    for private in (
        token,
        digest(token),
        key[1],
        request["signature"],
        nonce["challenge"],
        proof["signature"],
        session["access_token"],
        digest(session["access_token"]),
    ):
        assert private not in public
    assert all(
        "digest" not in row and "public_key" not in row for row in devices.json()
    )
    assert quotas(grant["id"]) == (0, 1, 1, 1)


@pytest.mark.parametrize("invalid", [0, -1, 1001, True, "2"])
def test_cli_quota_is_a_bounded_strict_integer(owner, invalid):
    response = owner.post(
        "/api/admin/staff",
        json={
            "product_id": product(owner),
            "name": "Invalid CLI quota",
            "max_cli_uses": invalid,
        },
    )
    assert response.status_code == 422, response.text


def test_same_key_on_different_links_cannot_exchange_another_devices_challenge(
    owner, clients
):
    first_grant, first_token = link(owner, product(owner))
    second_grant, second_token = link(owner, product(owner))
    client, key = clients(), identity()
    first, _ = authorize(client, first_token, key)
    second, _ = authorize(client, second_token, key)
    assert first["device_id"] != second["device_id"]
    assert first["product_id"] == first_grant["product_id"]
    assert second["product_id"] == second_grant["product_id"]
    nonce = challenge(client, first["device_id"])
    # The signature is valid for the second device's key, but its challenge is not.
    denied(
        client.post(
            "/api/cli/session", json=session_body(second["device_id"], nonce, key)
        )
    )
    session = login(client, second, key)
    response = client.get("/api/manage/products", headers=bearer(session))
    assert response.status_code == 200, response.text
    assert {row["id"] for row in response.json()} == {second_grant["product_id"]}


def test_handshakes_reject_browser_cookies_auth_headers_and_foreign_origin(
    owner, clients
):
    grant, token = link(owner, product(owner))
    client, key = clients(), identity()
    binding = bind_body(token, key)
    assert owner.post("/api/cli/authorize", json=binding).status_code == 400
    for headers in (
        {"Authorization": ""},
        {"Authorization": "Basic not-a-cli-proof"},
        {"Authorization": "Bearer not-a-cli-proof"},
        {"Origin": "https://example.invalid"},
    ):
        assert client.post(
            "/api/cli/authorize", json=binding, headers=headers
        ).status_code in (400, 403)
    assert quotas(grant["id"]) == (0, 1, 0, 1)
    device, _ = authorize(client, token, key)
    challenge_request = {"device_id": device["device_id"]}
    assert owner.post("/api/cli/challenge", json=challenge_request).status_code == 400
    for headers in (
        {"Authorization": ""},
        {"Authorization": "Bearer not-a-cli-proof"},
        {"Origin": "https://example.invalid"},
    ):
        assert client.post(
            "/api/cli/challenge", json=challenge_request, headers=headers
        ).status_code in (400, 403)
    nonce = challenge(client, device["device_id"])
    exchange_request = session_body(device["device_id"], nonce, key)
    assert owner.post("/api/cli/session", json=exchange_request).status_code == 400
    for headers in (
        {"Authorization": ""},
        {"Authorization": "Bearer not-a-cli-proof"},
        {"Origin": "https://example.invalid"},
    ):
        assert client.post(
            "/api/cli/session", json=exchange_request, headers=headers
        ).status_code in (400, 403)
    response = client.post("/api/cli/session", json=exchange_request)
    assert response.status_code == 200, response.text
    assert quotas(grant["id"]) == (0, 1, 1, 1)


def test_challenge_and_bearer_expiry_are_capped_by_the_link_and_ancestor(
    owner, clients
):
    parent, token = link(
        owner, product(owner), permissions=["queue.view", "links.delegate"]
    )
    browser = staff_browser(clients, token)
    child_response = browser.post(
        "/api/manage/links",
        json={"name": "Short lived", "permissions": ["queue.view"]},
        headers={"Origin": ORIGIN},
    )
    assert child_response.status_code == 200, child_response.text
    child = child_response.json()
    ancestor_expiry = time.time() + 120
    with db() as c:
        c.execute(
            "UPDATE staff SET expires=? WHERE id=?", (ancestor_expiry, parent["id"])
        )
    client, key = clients(), identity()
    device, _ = authorize(client, child["url"].split("#", 1)[1], key)
    assert device["expires"] <= ancestor_expiry
    nonce_response = client.post(
        "/api/cli/challenge", json={"device_id": device["device_id"]}
    )
    assert nonce_response.status_code == 200, nonce_response.text
    nonce = nonce_response.json()
    assert 0 < nonce["expires_in"] <= 120
    assert nonce["expires"] <= ancestor_expiry
    response = client.post(
        "/api/cli/session", json=session_body(device["device_id"], nonce, key)
    )
    assert response.status_code == 200, response.text
    session = response.json()
    assert 0 < session["expires_in"] <= 120
    assert session["expires"] <= ancestor_expiry
    assert client.get("/api/cli/status", headers=bearer(session)).status_code == 200
    with db() as c:
        c.execute(
            "UPDATE staff SET expires=? WHERE id=?", (time.time() - 1, parent["id"])
        )
    denied(client.get("/api/cli/status", headers=bearer(session)))


@pytest.mark.parametrize("legacy_version", [1, 8])
def test_legacy_database_migration_retains_browser_sessions_and_separate_cli_quota(
    tmp_path, monkeypatch, clients, legacy_version
):
    from extore import db as database
    from extore.models import Product

    now = time.time()
    link_token = b64(b"L" * 32)
    browser_cookie, owner_cookie = b64(b"B" * 32), b64(b"O" * 32)
    with sqlite3.connect(tmp_path / "extore.sqlite3") as c:
        c.executescript(
            """
            CREATE TABLE products(id TEXT PRIMARY KEY,config TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE staff(id TEXT PRIMARY KEY,digest TEXT UNIQUE NOT NULL,
                product_id TEXT NOT NULL REFERENCES products(id),name TEXT NOT NULL,
                expires REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE sessions(digest TEXT PRIMARY KEY,role TEXT NOT NULL,
                staff_id TEXT REFERENCES staff(id),expires REAL NOT NULL,created REAL NOT NULL);
            """
        )
        if legacy_version == 8:
            c.executescript(
                """
                ALTER TABLE staff ADD COLUMN permissions TEXT NOT NULL DEFAULT '["queue.view","queue.process"]';
                ALTER TABLE staff ADD COLUMN parent_id TEXT REFERENCES staff(id);
                ALTER TABLE staff ADD COLUMN created REAL NOT NULL DEFAULT 0;
                ALTER TABLE staff ADD COLUMN max_uses INTEGER NOT NULL DEFAULT 1;
                ALTER TABLE staff ADD COLUMN uses INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE sessions ADD COLUMN id TEXT;
                ALTER TABLE sessions ADD COLUMN last_seen REAL NOT NULL DEFAULT 0;
                ALTER TABLE sessions ADD COLUMN ip TEXT NOT NULL DEFAULT '';
                ALTER TABLE sessions ADD COLUMN ua TEXT NOT NULL DEFAULT '';
                ALTER TABLE sessions ADD COLUMN revoked INTEGER NOT NULL DEFAULT 0;
                """
            )
        c.execute(
            "INSERT INTO products(id,config,created) VALUES (?,?,?)",
            (
                "legacy-product",
                Product(name="Legacy browser product", parameters=[]).model_dump_json(),
                now,
            ),
        )
        c.execute(
            "INSERT INTO staff(id,digest,product_id,name,expires) VALUES (?,?,?,?,?)",
            (
                "legacy-link",
                digest(link_token),
                "legacy-product",
                "Legacy handler",
                now + 86400,
            ),
        )
        c.executemany(
            "INSERT INTO sessions(digest,role,staff_id,expires,created) VALUES (?,?,?,?,?)",
            [
                (digest(browser_cookie), "staff", "legacy-link", now + 3600, now),
                (digest(owner_cookie), "admin", None, now + 3600, now),
            ],
        )
        if legacy_version == 8:
            c.execute("UPDATE staff SET uses=1 WHERE id='legacy-link'")
            c.execute(
                "UPDATE sessions SET id=CASE role WHEN 'staff' THEN 'legacy-staff-session' ELSE 'legacy-owner-session' END,last_seen=?,ip='127.0.0.1',ua='Legacy browser'",
                (now,),
            )
        c.execute(f"PRAGMA user_version={legacy_version}")
    with monkeypatch.context() as patch:
        patch.setattr(database, "DATA", tmp_path)
        database.init()
        database.init()
        with database.db() as c:
            assert c.execute("PRAGMA user_version").fetchone()[0] == 13
            assert not c.execute("PRAGMA foreign_key_check").fetchall()
            rows = [dict(row) for row in c.execute("SELECT * FROM sessions")]
            assert len(rows) == 2
            assert all(
                row["channel"] == "browser" and row["device_id"] is None for row in rows
            )
            assert {row["digest"] for row in rows} == {
                digest(browser_cookie),
                digest(owner_cookie),
            }
            assert all(
                row["expires"] == now + 3600
                and row["created"] == now
                and row["revoked"] == 0
                for row in rows
            )
            if legacy_version == 8:
                assert {row["id"] for row in rows} == {
                    "legacy-staff-session",
                    "legacy-owner-session",
                }
                assert all(
                    row["ip"] == "127.0.0.1"
                    and row["ua"] == "Legacy browser"
                    and row["last_seen"] == now
                    for row in rows
                )
            else:
                assert all(row["id"] for row in rows)
        assert quotas("legacy-link") == (1, 1, 0, 1)
        browser, legacy_owner, cli_client = clients(), clients(), clients()
        browser.cookies.set("extore_session", browser_cookie)
        legacy_owner.cookies.set("extore_session", owner_cookie)
        assert browser.get("/api/manage/products").status_code == 200
        assert legacy_owner.get("/api/admin/products").status_code == 200
        assert browser.get("/api/cli/status").status_code == 401
        device, key = authorize(cli_client, link_token)
        session = login(cli_client, device, key)
        assert quotas("legacy-link") == (1, 1, 1, 1)
        assert (
            cli_client.get("/api/cli/status", headers=bearer(session)).status_code
            == 200
        )
        assert browser.get("/api/manage/products").status_code == 200


def test_browser_logout_cannot_revoke_a_cli_bearer_placed_in_its_cookie(owner, clients):
    grant, token = link(owner, product(owner))
    browser = staff_browser(clients, token)
    browser_cookie = browser.cookies.get("extore_session")
    client = clients()
    device, key = authorize(client, token)
    session = login(client, device, key)
    confused = clients()
    confused.cookies.set(
        "extore_session", session["access_token"], domain="localhost.local", path="/"
    )
    response = confused.post("/api/auth/logout", json={}, headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    assert client.get("/api/cli/status", headers=bearer(session)).status_code == 200
    response = browser.post("/api/auth/logout", json={}, headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    assert client.get("/api/cli/status", headers=bearer(session)).status_code == 200
    with db() as c:
        assert (
            c.execute(
                "SELECT revoked FROM sessions WHERE digest=?", (digest(browser_cookie),)
            ).fetchone()[0]
            == 1
        )
        assert (
            c.execute(
                "SELECT revoked FROM sessions WHERE id=?", (session["session_id"],)
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT revoked FROM cli_devices WHERE id=?", (device["device_id"],)
            ).fetchone()[0]
            == 0
        )
    assert quotas(grant["id"]) == (1, 1, 1, 1)


def test_browser_login_replacement_cannot_revoke_a_cli_bearer_cookie(owner, clients):
    first, token = link(owner, product(owner))
    replacement, replacement_token = link(owner, product(owner))
    client = clients()
    device, key = authorize(client, token)
    session = login(client, device, key)
    confused = clients()
    confused.cookies.set(
        "extore_session", session["access_token"], domain="localhost.local", path="/"
    )
    response = confused.post(
        "/api/staff/login",
        json={"token": replacement_token},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200, response.text
    assert confused.cookies.get("extore_session") != session["access_token"]
    assert confused.get("/api/manage/products").status_code == 200
    assert client.get("/api/cli/status", headers=bearer(session)).status_code == 200
    with db() as c:
        assert (
            c.execute(
                "SELECT revoked FROM sessions WHERE id=?", (session["session_id"],)
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT count(*) FROM audit WHERE action='session.replace' AND target=?",
                (session["session_id"],),
            ).fetchone()[0]
            == 0
        )
    assert quotas(first["id"]) == (0, 1, 1, 1)
    assert quotas(replacement["id"]) == (1, 1, 0, 1)


def test_cli_bearer_cannot_mint_another_device_grant_but_browser_can(owner, clients):
    grant, token = link(owner, product(owner), max_cli_uses=2)
    client = clients()
    first_device, first_key = authorize(client, token)
    first_session = login(client, first_device, first_key)
    response = client.post(
        "/api/manage/cli-ticket", json={}, headers=bearer(first_session)
    )
    assert response.status_code == 403, response.text
    assert client.cookies.get("extore_session") is None
    assert quotas(grant["id"]) == (0, 1, 1, 2)
    with db() as c:
        assert c.execute("SELECT count(*) FROM cli_bind_tickets").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM cli_devices").fetchone()[0] == 1
    browser = staff_browser(clients, token)
    response = browser.post(
        "/api/manage/cli-ticket", json={}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 200, response.text
    second_device, _ = authorize(client, response.json()["token"])
    assert second_device["device_id"] != first_device["device_id"]
    assert quotas(grant["id"]) == (1, 1, 2, 2)
    assert (
        client.get("/api/cli/status", headers=bearer(first_session)).status_code == 200
    )


@pytest.mark.parametrize(
    "authorization", ["", "Bearer invalid-cli-token", "Basic invalid-credential"]
)
def test_invalid_authorization_never_falls_back_to_owner_cookie_or_logs_it_out(
    owner, authorization
):
    original_cookie = owner.cookies.get("extore_session")
    headers = {"Authorization": authorization}
    for path in ("/api/admin/products", "/api/auth/status"):
        response = owner.get(path, headers=headers)
        assert response.status_code in (400, 401), response.text
        assert "set-cookie" not in response.headers
        assert owner.cookies.get("extore_session") == original_cookie
    response = owner.post("/api/auth/logout", json={}, headers=headers)
    assert response.status_code in (400, 401), response.text
    assert "set-cookie" not in response.headers
    assert owner.cookies.get("extore_session") == original_cookie
    assert owner.get("/api/admin/products").status_code == 200
    assert owner.get("/api/auth/status").json()["role"] == "admin"
    with db() as c:
        assert (
            c.execute(
                "SELECT revoked FROM sessions WHERE digest=?",
                (digest(original_cookie),),
            ).fetchone()[0]
            == 0
        )

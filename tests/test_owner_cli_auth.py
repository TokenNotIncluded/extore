"""Owner CLI grants require Passkey consent and device-key proof for every write."""

import base64
import hashlib
import json
import secrets
import sqlite3
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from starlette.responses import Response
from test_passkeys import Authenticator, register

from extore.app import app
from extore.config import DATA, ORIGIN, RP_ID
from extore.db import db
from extore.security import create_session, digest

ROOT = "/api/cli/owner"
APPROVAL = "/api/auth/cli-owner"


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def unb64(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def keypair():
    private = Ed25519PrivateKey.generate()
    return private, b64(
        private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    )


def signed(key, proof):
    return b64(key[0].sign(proof.encode()))


def request_body(key, name="Owner CLI test", nonce=None):
    nonce = nonce or b64(secrets.token_bytes(32))
    return {
        "public_key": key[1],
        "client_name": name,
        "nonce": nonce,
        "signature": signed(
            key, f"extore-cli-owner-request-v1\n{ORIGIN}\n{key[1]}\n{name}\n{nonce}"
        ),
    }


def status_body(request, key):
    return {
        "request_id": request["request_id"],
        "public_key": key[1],
        "signature": signed(
            key,
            f"extore-cli-owner-status-v1\n{ORIGIN}\n{request['request_id']}\n{key[1]}",
        ),
    }


def claim_body(request, key):
    return {
        "request_id": request["request_id"],
        "public_key": key[1],
        "signature": signed(
            key,
            f"extore-cli-owner-claim-v1\n{ORIGIN}\n{request['request_id']}\n{request['challenge']}\n{key[1]}",
        ),
    }


@pytest.fixture
def clients():
    opened = []

    def create():
        client = TestClient(app, base_url=ORIGIN)
        opened.append(client)
        return client

    yield create
    for client in opened:
        client.close()


@pytest.fixture
def passkey(owner, monkeypatch):
    from extore import owner_cli_auth

    authenticator = Authenticator()
    register(owner, authenticator, "Owner CLI approval Passkey")
    original = owner_cli_auth.verify_authentication_response
    calls = []

    def verify(**kwargs):
        calls.append(kwargs)
        assert kwargs["expected_rp_id"] == RP_ID
        assert kwargs["expected_origin"] == ORIGIN
        assert kwargs["require_user_verification"] is True
        with sqlite3.connect(f"file:{DATA / 'extore.sqlite3'}?mode=ro", uri=True) as c:
            c.row_factory = sqlite3.Row
            stored = c.execute(
                "SELECT * FROM credentials WHERE id=?", (kwargs["credential"]["id"],)
            ).fetchone()
        assert stored is not None
        assert kwargs["credential_public_key"] == stored["public_key"]
        assert kwargs["credential_current_sign_count"] == stored["sign_count"]
        return original(**kwargs)

    monkeypatch.setattr(owner_cli_auth, "verify_authentication_response", verify)
    return authenticator, calls


def pending(client, key=None, name="Owner CLI test"):
    key = key or keypair()
    body = request_body(key, name)
    response = client.post(ROOT + "/request", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["request_id"] and result["device_code"]
    assert len(unb64(result["challenge"])) == 32
    assert time.time() + 590 < result["expires"] <= time.time() + 601
    assert result["fingerprint"] == hashlib.sha256(unb64(key[1])).hexdigest()
    assert "set-cookie" not in response.headers
    return result, key


def options(owner, request):
    response = owner.post(
        APPROVAL + "/options",
        json={
            "request_id": request["request_id"],
            "device_code": request["device_code"],
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["request_id"] == request["request_id"]
    assert result["fingerprint"] == request["fingerprint"]
    assert result["role"] == "admin"
    assert result["scope"] == "shop.owner"
    assert result["options"]["userVerification"] == "required"
    assert result["options"]["rpId"] == RP_ID
    assert "pubKeyCredParams" not in result["options"]
    return result


def approve(owner, request, passkey):
    authenticator, calls = passkey
    before = len(calls)
    context = options(owner, request)
    response = owner.post(
        APPROVAL + "/verify",
        json={
            "request_id": request["request_id"],
            "credential": authenticator.assertion(context["options"]),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "status": "approved"}
    assert len(calls) == before + 1
    assert calls[-1]["expected_challenge"] == unb64(context["options"]["challenge"])
    return context


def claim(client, request, key):
    response = client.post(ROOT + "/claim", json=claim_body(request, key))
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["device_id"]
    assert "set-cookie" not in response.headers
    return result


def session_body(
    device_id,
    challenge,
    key,
    *,
    proof_origin=ORIGIN,
    prefix="extore-cli-owner-session-v1",
):
    proof = f"{prefix}\n{proof_origin}\n{device_id}\n{challenge['challenge_id']}\n{challenge['challenge']}"
    return {
        "device_id": device_id,
        "challenge_id": challenge["challenge_id"],
        "signature": signed(key, proof),
    }


def owner_session(client, device, key):
    response = client.post(ROOT + "/challenge", json={"device_id": device["device_id"]})
    assert response.status_code == 200, response.text
    challenge = response.json()
    response = client.post(
        ROOT + "/session", json=session_body(device["device_id"], challenge, key)
    )
    assert response.status_code == 200, response.text
    session = response.json()
    assert session["token_type"] == "Bearer"
    assert session["device_id"] == device["device_id"]
    assert session["expires_in"] == 8 * 3600
    assert session["role"] == "admin"
    assert session["scope"] == "shop.owner"
    assert "set-cookie" not in response.headers
    return session


def grant(owner, client, passkey, key=None):
    request, key = pending(client, key)
    approve(owner, request, passkey)
    device = claim(client, request, key)
    return request, device, owner_session(client, device, key), key


def bearer(session):
    return {"Authorization": "Bearer " + session["access_token"]}


def payload(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def action(client, session, method, path, body):
    response = client.post(
        ROOT + "/action-challenge",
        json={
            "method": method,
            "path": path,
            "body_sha256": hashlib.sha256(body).hexdigest(),
        },
        headers=bearer(session),
    )
    assert response.status_code == 200, response.text
    challenge = response.json()
    assert challenge["expires_in"] == 300
    return challenge


def action_headers(session, key, nonce, http_method, target_path, raw_body, **changes):
    fields = {
        "device_id": session["device_id"],
        "session_id": session["session_id"],
        "challenge_id": nonce["challenge_id"],
        "challenge": nonce["challenge"],
        "method": http_method,
        "path": target_path,
        "body_sha256": hashlib.sha256(raw_body).hexdigest(),
    }
    fields.update(changes)
    proof = "extore-cli-owner-action-v1\n" + "\n".join(
        [
            ORIGIN,
            fields["device_id"],
            fields["session_id"],
            fields["challenge_id"],
            fields["challenge"],
            fields["method"],
            fields["path"],
            fields["body_sha256"],
        ]
    )
    return {
        **bearer(session),
        "Content-Type": "application/json",
        "X-Extore-CLI-Challenge": nonce["challenge_id"],
        "X-Extore-CLI-Signature": signed(key, proof),
    }


def write(client, session, key, method, path, value=None):
    body = b"" if value is None else payload(value)
    challenge = action(client, session, method, path, body)
    headers = action_headers(session, key, challenge, method, path, body)
    return client.request(method, path, content=body, headers=headers)


def denied(response):
    assert 400 <= response.status_code < 500, response.text


def device_count():
    with db() as c:
        return c.execute("SELECT count(*) FROM owner_cli_devices").fetchone()[0]


def test_request_status_and_claim_require_the_exact_requesting_private_key(
    owner, clients, passkey
):
    client = clients()
    request, key = pending(client)
    response = client.post(ROOT + "/status", json=status_body(request, key))
    assert response.status_code == 200, response.text
    denied(client.post(ROOT + "/status", json=status_body(request, keypair())))
    denied(client.post(ROOT + "/claim", json=claim_body(request, key)))
    assert device_count() == 0
    approve(owner, request, passkey)
    denied(client.post(ROOT + "/claim", json=claim_body(request, keypair())))
    assert device_count() == 0
    device = claim(client, request, key)
    assert device_count() == 1
    session = owner_session(client, device, key)
    assert client.get(ROOT + "/status", headers=bearer(session)).status_code == 200


def test_owner_grant_is_thirty_days_and_bearer_is_eight_hours(owner, clients, passkey):
    client = clients()
    _, device, session, _ = grant(owner, client, passkey)
    with db() as c:
        stored = dict(
            c.execute(
                "SELECT * FROM owner_cli_devices WHERE id=?", (device["device_id"],)
            ).fetchone()
        )
        row = dict(
            c.execute(
                "SELECT * FROM sessions WHERE id=?", (session["session_id"],)
            ).fetchone()
        )
    assert 30 * 86400 - 10 <= stored["expires"] - stored["created"] <= 30 * 86400 + 1
    assert row["role"] == "admin" and row["channel"] == "cli"
    assert row["staff_id"] is None and row["device_id"] is None
    assert row["owner_device_id"] == device["device_id"]
    assert row["digest"] == digest(session["access_token"])
    assert session["access_token"] not in json.dumps(row)
    assert 8 * 3600 - 1 <= row["expires"] - row["created"] <= 8 * 3600 + 1


def test_recent_owner_cookie_alone_does_not_approve_a_cli_device(
    owner, clients, passkey
):
    client = clients()
    request, key = pending(client)
    denied(
        owner.post(
            APPROVAL + "/verify",
            json={"request_id": request["request_id"], "credential": {}},
        )
    )
    denied(client.post(ROOT + "/claim", json=claim_body(request, key)))
    assert device_count() == 0


@pytest.mark.parametrize(
    "field", ["public_key", "device_code", "scope", "grant_expires"]
)
def test_approval_rejects_mutated_request_snapshot(owner, clients, passkey, field):
    client = clients()
    request, key = pending(client)
    context = options(owner, request)
    changed = {
        "public_key": keypair()[1],
        "device_code": "changed-device-code",
        "scope": "changed.scope",
        "grant_expires": time.time() + 90 * 86400,
    }[field]
    with db() as c:
        c.execute(
            f"UPDATE owner_cli_requests SET {field}=? WHERE id=?",
            (changed, request["request_id"]),
        )
    credential = passkey[0].assertion(context["options"])
    denied(
        owner.post(
            APPROVAL + "/verify",
            json={"request_id": request["request_id"], "credential": credential},
        )
    )
    denied(client.post(ROOT + "/claim", json=claim_body(request, key)))
    assert device_count() == 0


def test_approval_assertion_cannot_be_moved_to_another_request(owner, clients, passkey):
    client = clients()
    first, _ = pending(client)
    second, _ = pending(client)
    context = options(owner, first)
    response = owner.post(
        APPROVAL + "/verify",
        json={
            "request_id": second["request_id"],
            "credential": passkey[0].assertion(context["options"]),
        },
    )
    denied(response)
    assert device_count() == 0


def test_approval_assertion_cannot_move_between_browser_approval_cookies(
    owner, clients, passkey
):
    client = clients()
    request, _ = pending(client)
    context = options(owner, request)
    other = clients()
    with db() as c:
        value = create_session(c, Response(), "admin")
    other.cookies.set("extore_session", value, domain="localhost.local", path="/")
    second_context = other.post(
        APPROVAL + "/options",
        json={
            "request_id": request["request_id"],
            "device_code": request["device_code"],
        },
        headers={"Origin": ORIGIN},
    )
    assert second_context.status_code == 200, second_context.text
    assert (
        second_context.json()["options"]["challenge"] != context["options"]["challenge"]
    )
    response = other.post(
        APPROVAL + "/verify",
        json={
            "request_id": request["request_id"],
            "credential": passkey[0].assertion(context["options"]),
        },
        headers={"Origin": ORIGIN},
    )
    denied(response)
    assert device_count() == 0


def test_ordinary_passkey_login_assertion_cannot_approve_a_device(
    owner, clients, passkey
):
    request, _ = pending(clients())
    options(owner, request)
    login_options = owner.post("/api/auth/login/options", json={}).json()
    credential = passkey[0].assertion(login_options)
    denied(
        owner.post(
            APPROVAL + "/verify",
            json={"request_id": request["request_id"], "credential": credential},
        )
    )
    assert device_count() == 0


@pytest.mark.parametrize(
    "failure", ["unverified", "unknown", "wrong_signer", "wrong_origin"]
)
def test_real_webauthn_assertion_requires_registered_key_origin_and_user_verification(
    owner, clients, passkey, monkeypatch, failure
):
    request, _ = pending(clients())
    context = options(owner, request)
    authenticator = passkey[0]
    if failure == "unknown":
        authenticator = Authenticator()
    if failure == "wrong_origin":
        original = authenticator.client_data

        def client_data(options, operation):
            value = json.loads(original(options, operation))
            value["origin"] = "https://example.invalid"
            return payload(value)

        monkeypatch.setattr(authenticator, "client_data", client_data)
    credential = authenticator.assertion(
        context["options"],
        verified=failure != "unverified",
        signer=Authenticator() if failure == "wrong_signer" else None,
    )
    denied(
        owner.post(
            APPROVAL + "/verify",
            json={"request_id": request["request_id"], "credential": credential},
        )
    )
    assert device_count() == 0


def test_expired_request_and_approval_cannot_be_claimed(owner, clients, passkey):
    client = clients()
    request, key = pending(client)
    with db() as c:
        c.execute(
            "UPDATE owner_cli_requests SET expires=? WHERE id=?",
            (time.time() - 1, request["request_id"]),
        )
    denied(
        owner.post(
            APPROVAL + "/options",
            json={
                "request_id": request["request_id"],
                "device_code": request["device_code"],
            },
        )
    )
    denied(client.post(ROOT + "/claim", json=claim_body(request, key)))
    assert device_count() == 0


def test_expired_passkey_approval_challenge_does_not_approve_request(
    owner, clients, passkey
):
    request, _ = pending(clients())
    context = options(owner, request)
    with db() as c:
        c.execute(
            "UPDATE owner_cli_approval_challenges SET expires=?", (time.time() - 1,)
        )
    denied(
        owner.post(
            APPROVAL + "/verify",
            json={
                "request_id": request["request_id"],
                "credential": passkey[0].assertion(context["options"]),
            },
        )
    )
    assert device_count() == 0


def test_owner_reads_are_scoped_to_owner_and_writes_need_fresh_device_proof(
    owner, clients, passkey
):
    client = clients()
    _, _, session, key = grant(owner, client, passkey)
    assert client.get("/api/admin/products", headers=bearer(session)).status_code == 200
    assert (
        client.get("/api/manage/products", headers=bearer(session)).status_code == 200
    )
    body = {"name": "Created by the owner CLI", "parameters": []}
    denied(client.post("/api/admin/products", json=body, headers=bearer(session)))
    assert client.get("/api/admin/products", headers=bearer(session)).json() == []
    response = write(client, session, key, "POST", "/api/admin/products", body)
    assert response.status_code == 200, response.text
    assert len(client.get("/api/admin/products", headers=bearer(session)).json()) == 1


@pytest.mark.parametrize(
    "component",
    [
        "device_id",
        "session_id",
        "challenge_id",
        "challenge",
        "method",
        "path",
        "body_sha256",
    ],
)
def test_action_signature_binds_every_authorization_and_request_component(
    owner, clients, passkey, component
):
    client = clients()
    _, _, session, key = grant(owner, client, passkey)
    path = "/api/admin/products"
    body = payload({"name": "Must not be created", "parameters": []})
    nonce = action(client, session, "POST", path, body)
    changed = {
        "device_id": "wrong-device",
        "session_id": "wrong-session",
        "challenge_id": "wrong-challenge",
        "challenge": b64(secrets.token_bytes(32)),
        "method": "PUT",
        "path": path + "?wrong=1",
        "body_sha256": hashlib.sha256(b"changed").hexdigest(),
    }
    headers = action_headers(
        session, key, nonce, "POST", path, body, **{component: changed[component]}
    )
    denied(client.post(path, content=body, headers=headers))
    assert client.get(path, headers=bearer(session)).json() == []


def test_exact_body_bytes_and_raw_query_are_bound_to_action(owner, clients, passkey):
    client = clients()
    _, _, session, key = grant(owner, client, passkey)
    path = "/api/admin/products?trace=A%2FB&x=1"
    body = payload({"name": "Exact wire request", "parameters": []})
    nonce = action(client, session, "POST", path, body)
    headers = action_headers(session, key, nonce, "POST", path, body)
    denied(client.post(path, content=body + b" ", headers=headers))
    nonce = action(client, session, "POST", path, body)
    headers = action_headers(session, key, nonce, "POST", path, body)
    denied(client.post(path.replace("x=1", "x=2"), content=body, headers=headers))
    response = write(
        client,
        session,
        key,
        "POST",
        path,
        {"name": "Exact wire request", "parameters": []},
    )
    assert response.status_code == 200, response.text


def test_action_challenge_is_consumed_once_even_when_business_validation_fails(
    owner, clients, passkey
):
    client = clients()
    _, _, session, key = grant(owner, client, passkey)
    path, body = "/api/admin/products", payload({"name": "", "parameters": []})
    nonce = action(client, session, "POST", path, body)
    headers = action_headers(session, key, nonce, "POST", path, body)
    first = client.post(path, content=body, headers=headers)
    assert first.status_code in (400, 422), first.text
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM owner_cli_action_challenges WHERE id=?",
                (nonce["challenge_id"],),
            ).fetchone()[0]
            == 0
        )
    denied(client.post(path, content=body, headers=headers))
    assert client.get(path, headers=bearer(session)).json() == []


def test_successful_action_cannot_be_replayed_or_moved_to_another_session(
    owner, clients, passkey
):
    client = clients()
    _, device, session, key = grant(owner, client, passkey)
    second = owner_session(client, device, key)
    path, body = "/api/admin/products", payload({"name": "Only once", "parameters": []})
    nonce = action(client, session, "POST", path, body)
    headers = action_headers(session, key, nonce, "POST", path, body)
    denied(client.post(path, content=body, headers={**headers, **bearer(second)}))
    nonce = action(client, session, "POST", path, body)
    headers = action_headers(session, key, nonce, "POST", path, body)
    first = client.post(path, content=body, headers=headers)
    assert first.status_code == 200, first.text
    denied(client.post(path, content=body, headers=headers))
    assert len(client.get(path, headers=bearer(session)).json()) == 1


def test_stolen_owner_bearer_cannot_write_with_an_attacker_key(owner, clients, passkey):
    client = clients()
    _, _, session, _ = grant(owner, client, passkey)
    path, body = (
        "/api/admin/products",
        payload({"name": "Attacker product", "parameters": []}),
    )
    nonce = action(client, session, "POST", path, body)
    headers = action_headers(session, keypair(), nonce, "POST", path, body)
    denied(client.post(path, content=body, headers=headers))
    denied(client.post(APPROVAL + "/options", json={}, headers=bearer(session)))
    denied(client.post("/api/auth/register/options", json={}, headers=bearer(session)))
    assert device_count() == 1
    assert client.get(path, headers=bearer(session)).json() == []


def test_owner_bearer_cannot_be_used_as_cookie_or_mixed_with_browser_auth(
    owner, clients, passkey
):
    client = clients()
    _, _, session, _ = grant(owner, client, passkey)
    confused = clients()
    confused.cookies.set(
        "extore_session", session["access_token"], domain="localhost.local", path="/"
    )
    denied(confused.get("/api/admin/products"))
    denied(confused.get(ROOT + "/status"))
    response = confused.post("/api/auth/logout", json={}, headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    assert client.get(ROOT + "/status", headers=bearer(session)).status_code == 200
    original_cookie = owner.cookies.get("extore_session")
    denied(owner.get("/api/admin/products", headers=bearer(session)))
    denied(owner.post("/api/auth/logout", json={}, headers=bearer(session)))
    assert owner.cookies.get("extore_session") == original_cookie
    assert owner.get("/api/admin/products").status_code == 200


def test_owner_cli_logout_requires_signature_and_only_revokes_current_bearer(
    owner, clients, passkey
):
    client = clients()
    _, device, session, key = grant(owner, client, passkey)
    second = owner_session(client, device, key)
    denied(client.delete(ROOT + "/session", headers=bearer(session)))
    assert client.get(ROOT + "/status", headers=bearer(session)).status_code == 200
    response = write(client, session, key, "DELETE", ROOT + "/session")
    assert response.status_code == 200, response.text
    denied(client.get(ROOT + "/status", headers=bearer(session)))
    assert client.get(ROOT + "/status", headers=bearer(second)).status_code == 200
    renewed = owner_session(client, device, key)
    assert client.get(ROOT + "/status", headers=bearer(renewed)).status_code == 200


@pytest.mark.parametrize(
    "field", ["public_key", "device_code", "scope", "grant_expires"]
)
def test_claim_rechecks_manifest_approved_by_passkey(owner, clients, passkey, field):
    client = clients()
    request, key = pending(client)
    approve(owner, request, passkey)
    replacement = keypair()
    changed = {
        "public_key": replacement[1],
        "device_code": "CHANGED-CODE",
        "scope": "changed.scope",
        "grant_expires": time.time() + 90 * 86400,
    }[field]
    with db() as c:
        c.execute(
            f"UPDATE owner_cli_requests SET {field}=? WHERE id=?",
            (changed, request["request_id"]),
        )
    attempted_key = replacement if field == "public_key" else key
    denied(client.post(ROOT + "/claim", json=claim_body(request, attempted_key)))
    assert device_count() == 0


def test_cli_can_register_a_real_verified_passkey_with_session_bound_challenge(
    owner, clients, passkey, monkeypatch
):
    from extore import auth

    client = clients()
    _, _, session, key = grant(owner, client, passkey)
    authenticator = Authenticator()
    calls = []
    original = auth.verify_registration_response

    def verify(**kwargs):
        calls.append(kwargs)
        assert kwargs["expected_rp_id"] == RP_ID
        assert kwargs["expected_origin"] == ORIGIN
        assert kwargs["require_user_verification"] is True
        return original(**kwargs)

    monkeypatch.setattr(auth, "verify_registration_response", verify)
    response = write(client, session, key, "POST", "/api/auth/register/options", {})
    assert response.status_code == 200, response.text
    options = response.json()
    assert options["authenticatorSelection"]["userVerification"] == "required"
    assert options["challenge_id"]
    assert "set-cookie" not in response.headers
    body = {
        "credential": authenticator.registration(options),
        "name": "CLI registered key",
        "challenge_id": options["challenge_id"],
    }
    response = write(client, session, key, "POST", "/api/auth/register/verify", body)
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    assert calls[0]["expected_challenge"] == unb64(options["challenge"])
    with db() as c:
        row = c.execute(
            "SELECT * FROM credentials WHERE id=?", (authenticator.id,)
        ).fetchone()
        assert row is not None and row["name"] == "CLI registered key"
    response = write(
        client, session, key, "DELETE", "/api/auth/passkeys/" + authenticator.id
    )
    assert response.status_code == 200, response.text
    denied(write(client, session, key, "DELETE", "/api/auth/passkeys/" + passkey[0].id))


@pytest.mark.parametrize(
    "failure", ["unverified", "wrong_origin", "wrong_rp", "bad_attestation_signature"]
)
def test_cli_passkey_registration_still_checks_real_webauthn(
    owner, clients, passkey, monkeypatch, failure
):
    import cbor2

    client = clients()
    _, _, session, key = grant(owner, client, passkey)
    authenticator = Authenticator()
    if failure == "wrong_origin":
        original = authenticator.client_data

        def wrong_origin(options, operation):
            value = json.loads(original(options, operation))
            value["origin"] = "https://example.invalid"
            return payload(value)

        monkeypatch.setattr(authenticator, "client_data", wrong_origin)
    if failure == "wrong_rp":
        original = authenticator.auth_data

        def wrong_rp(flags, counter):
            return (
                hashlib.sha256(b"example.invalid").digest()
                + original(flags, counter)[32:]
            )

        monkeypatch.setattr(authenticator, "auth_data", wrong_rp)
    response = write(client, session, key, "POST", "/api/auth/register/options", {})
    assert response.status_code == 200, response.text
    options = response.json()
    credential = authenticator.registration(options, verified=failure != "unverified")
    if failure == "bad_attestation_signature":
        attestation = cbor2.loads(unb64(credential["response"]["attestationObject"]))
        attestation["fmt"] = "packed"
        attestation["attStmt"] = {"alg": -7, "sig": b"\0" * 72}
        credential["response"]["attestationObject"] = b64(cbor2.dumps(attestation))
    denied(
        write(
            client,
            session,
            key,
            "POST",
            "/api/auth/register/verify",
            {"credential": credential, "challenge_id": options["challenge_id"]},
        )
    )
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM credentials WHERE id=?", (authenticator.id,)
            ).fetchone()[0]
            == 0
        )


def test_cli_registration_challenge_cannot_be_moved_to_a_different_bearer_session(
    owner, clients, passkey
):
    client = clients()
    _, device, session, key = grant(owner, client, passkey)
    other = owner_session(client, device, key)
    response = write(client, session, key, "POST", "/api/auth/register/options", {})
    assert response.status_code == 200, response.text
    options = response.json()
    authenticator = Authenticator()
    body = {
        "credential": authenticator.registration(options),
        "challenge_id": options["challenge_id"],
    }
    denied(write(client, other, key, "POST", "/api/auth/register/verify", body))
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM credentials WHERE id=?", (authenticator.id,)
            ).fetchone()[0]
            == 0
        )


def test_staff_cli_is_not_promoted_when_same_key_receives_a_separate_owner_grant(
    owner, clients, passkey
):
    from test_cli_auth import authorize as authorize_staff
    from test_cli_auth import link as staff_link
    from test_cli_auth import login as login_staff
    from test_cli_auth import product as staff_product

    _, token = staff_link(owner, staff_product(owner))
    client = clients()
    staff_device, key = authorize_staff(client, token)
    staff_session = login_staff(client, staff_device, key)
    denied(client.get("/api/admin/products", headers=bearer(staff_session)))
    denied(
        client.post(ROOT + "/challenge", json={"device_id": staff_device["device_id"]})
    )
    denied(
        client.post(
            ROOT + "/action-challenge",
            json={
                "method": "POST",
                "path": "/api/admin/products",
                "body_sha256": hashlib.sha256(b"{}").hexdigest(),
            },
            headers=bearer(staff_session),
        )
    )
    _, owner_device, owner_token, _ = grant(owner, client, passkey, key)
    assert owner_device["device_id"] != staff_device["device_id"]
    assert (
        client.get("/api/admin/products", headers=bearer(owner_token)).status_code
        == 200
    )
    denied(client.get("/api/admin/products", headers=bearer(staff_session)))
    denied(client.get(ROOT + "/status", headers=bearer(staff_session)))
    with db() as c:
        old = c.execute(
            "SELECT * FROM sessions WHERE id=?", (staff_session["session_id"],)
        ).fetchone()
        assert old["role"] == "staff" and old["owner_device_id"] is None
        assert old["device_id"] == staff_device["device_id"]


def test_owner_device_revocation_rejects_old_bearers_pending_login_and_reclaim(
    owner, clients, passkey
):
    client = clients()
    request, device, session, key = grant(owner, client, passkey)
    second = owner_session(client, device, key)
    login_response = client.post(
        ROOT + "/challenge", json={"device_id": device["device_id"]}
    )
    assert login_response.status_code == 200, login_response.text
    pending_login = login_response.json()
    body = payload({"name": "Never created", "parameters": []})
    nonce = action(client, session, "POST", "/api/admin/products", body)
    headers = action_headers(session, key, nonce, "POST", "/api/admin/products", body)
    response = owner.delete("/api/admin/cli-owner-devices/" + device["device_id"])
    assert response.status_code == 200, response.text
    for bearer_session in (session, second):
        denied(client.get(ROOT + "/status", headers=bearer(bearer_session)))
    denied(
        client.post(
            ROOT + "/session",
            json=session_body(device["device_id"], pending_login, key),
        )
    )
    denied(client.post("/api/admin/products", content=body, headers=headers))
    denied(client.post(ROOT + "/claim", json=claim_body(request, key)))
    assert owner.get("/api/admin/products").status_code == 200
    with db() as c:
        assert (
            c.execute(
                "SELECT revoked FROM owner_cli_devices WHERE id=?",
                (device["device_id"],),
            ).fetchone()[0]
            == 1
        )


def test_auth_reset_invalidates_owner_devices_requests_challenges_and_sessions(
    owner, clients, passkey, monkeypatch
):
    from extore import cli

    client = clients()
    request, device, session, key = grant(owner, client, passkey)
    fresh_request, fresh_key = pending(client)
    approve(owner, fresh_request, passkey)
    pending_response = client.post(
        ROOT + "/challenge", json={"device_id": device["device_id"]}
    )
    assert pending_response.status_code == 200, pending_response.text
    body = payload({"name": "Never created", "parameters": []})
    nonce = action(client, session, "POST", "/api/admin/products", body)
    headers = action_headers(session, key, nonce, "POST", "/api/admin/products", body)
    monkeypatch.setattr("builtins.input", lambda prompt: "RESET")
    monkeypatch.setattr(
        cli.getpass, "getpass", lambda prompt: "owner-cli-test-reset-password"
    )
    cli.main(["reset-auth"])
    denied(client.get(ROOT + "/status", headers=bearer(session)))
    denied(
        client.post(
            ROOT + "/session",
            json=session_body(device["device_id"], pending_response.json(), key),
        )
    )
    denied(client.post("/api/admin/products", content=body, headers=headers))
    denied(client.post(ROOT + "/claim", json=claim_body(request, key)))
    denied(client.post(ROOT + "/claim", json=claim_body(fresh_request, fresh_key)))
    with db() as c:
        assert c.execute("SELECT count(*) FROM credentials").fetchone()[0] == 0
        assert (
            c.execute(
                "SELECT revoked FROM owner_cli_devices WHERE id=?",
                (device["device_id"],),
            ).fetchone()[0]
            == 1
        )
        for table in (
            "owner_cli_challenges",
            "owner_cli_action_challenges",
            "owner_cli_approval_challenges",
        ):
            assert c.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0


def test_owner_metadata_and_audit_do_not_expose_keys_codes_nonces_or_bearers(
    owner, clients, passkey
):
    client = clients()
    request, key = pending(client)
    request_signature = status_body(request, key)["signature"]
    context = options(owner, request)
    approval_cookie = owner.cookies.get("extore_owner_approval")
    credential = passkey[0].assertion(context["options"])
    response = owner.post(
        APPROVAL + "/verify",
        json={"request_id": request["request_id"], "credential": credential},
    )
    assert response.status_code == 200, response.text
    device = claim(client, request, key)
    session = owner_session(client, device, key)
    status = client.get(ROOT + "/status", headers=bearer(session))
    devices = owner.get("/api/admin/cli-owner-devices")
    assert status.status_code == devices.status_code == 200
    assert status.json()["scope"] == "shop.owner"
    assert devices.json()[0]["id"] == device["device_id"]
    with db() as c:
        audit_rows = [dict(row) for row in c.execute("SELECT * FROM audit")]
        request_event = next(
            row for row in audit_rows if row["action"] == "cli.owner.request"
        )
        assert (
            request_event["actor"] == "pending"
            and request_event["target"] == request["request_id"]
        )
        assert any(row["target"] == device["device_id"] for row in audit_rows)
        assert any(row["target"] == session["session_id"] for row in audit_rows)
        request_nonce = c.execute(
            "SELECT nonce FROM owner_cli_requests WHERE id=?", (request["request_id"],)
        ).fetchone()[0]
    public = status.text + devices.text + json.dumps(audit_rows)
    for secret in (
        key[1],
        request["device_code"],
        request["challenge"],
        request_nonce,
        request_signature,
        approval_cookie,
        digest(approval_cookie),
        credential["response"]["signature"],
        session["access_token"],
        digest(session["access_token"]),
    ):
        assert secret not in public

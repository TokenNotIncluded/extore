"""Exercise WebAuthn verification with real P-256 keys and signed assertions."""

import hashlib
import json
import secrets

import cbor2
from argon2 import PasswordHasher
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.responses import Response
from webauthn.helpers import bytes_to_base64url

from extore.config import DATA, ORIGIN, RP_ID
from extore.db import db, set_setting, setting
from extore.security import create_session, digest


class Authenticator:
    """A minimal authenticator that produces actual WebAuthn wire responses."""

    def __init__(self):
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)
        self.id = bytes_to_base64url(self.credential_id)
        self.sign_count = 0

    def client_data(self, options, operation):
        return json.dumps(
            {
                "type": "webauthn." + operation,
                "challenge": options["challenge"],
                "origin": ORIGIN,
                "crossOrigin": False,
            },
            separators=(",", ":"),
        ).encode()

    def auth_data(self, flags, sign_count):
        return (
            hashlib.sha256(RP_ID.encode()).digest()
            + bytes([flags])
            + sign_count.to_bytes(4, "big")
        )

    def credential(self, response):
        return {
            "id": self.id,
            "rawId": self.id,
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "response": {k: bytes_to_base64url(v) for k, v in response.items()},
        }

    def registration(self, options, verified=True):
        public_key = self.private_key.public_key().public_numbers()
        cose_key = cbor2.dumps(
            {
                1: 2,  # EC2
                3: -7,  # ES256
                -1: 1,  # P-256
                -2: public_key.x.to_bytes(32, "big"),
                -3: public_key.y.to_bytes(32, "big"),
            }
        )
        auth_data = (
            self.auth_data(0x45 if verified else 0x41, 0)
            + bytes(16)  # AAGUID
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + cose_key
        )
        return self.credential(
            {
                "clientDataJSON": self.client_data(options, "create"),
                "attestationObject": cbor2.dumps(
                    {"fmt": "none", "authData": auth_data, "attStmt": {}}
                ),
            }
        )

    def assertion(self, options, *, signer=None, verified=True, sign_count=None):
        if sign_count is None:
            self.sign_count += 1
            sign_count = self.sign_count
        auth_data = self.auth_data(0x05 if verified else 0x01, sign_count)
        client_data = self.client_data(options, "get")
        key = signer.private_key if signer else self.private_key
        signature = key.sign(
            auth_data + hashlib.sha256(client_data).digest(),
            ec.ECDSA(hashes.SHA256()),
        )
        return self.credential(
            {
                "clientDataJSON": client_data,
                "authenticatorData": auth_data,
                "signature": signature,
                "userHandle": b"extore-owner",
            }
        )


def register(client, authenticator, name="测试 Passkey"):
    response = client.post("/api/auth/register/options", json={})
    assert response.status_code == 200, response.text
    options = response.json()
    assert options["authenticatorSelection"]["residentKey"] == "required"
    assert options["authenticatorSelection"]["userVerification"] == "required"
    response = client.post(
        "/api/auth/register/verify",
        json={"credential": authenticator.registration(options), "name": name},
    )
    assert response.status_code == 200, response.text
    return options


def login_options(client):
    response = client.post("/api/auth/login/options", json={})
    assert response.status_code == 200, response.text
    assert response.json()["userVerification"] == "required"
    return response.json()


def login(client, authenticator):
    response = client.post(
        "/api/auth/login/verify",
        json={"credential": authenticator.assertion(login_options(client))},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"role": "admin"}
    assert client.get("/api/admin/products").status_code == 200


def test_first_passkey_disables_password_and_invalidates_bootstrap_sessions(client):
    password = "bootstrap-testing-password"
    password_path = DATA / "bootstrap-password.txt"
    password_path.write_text(password)
    with db() as c:
        set_setting(c, "bootstrap_password", PasswordHasher().hash(password))
        other_bootstrap = create_session(c, Response(), "bootstrap")
    assert (
        client.post("/api/auth/password", json={"password": password}).status_code
        == 200
    )
    assert client.get("/api/admin/products").status_code == 401
    register(client, Authenticator())

    status = client.get("/api/auth/status").json()
    assert status == {"configured": True, "password_enabled": False, "role": "admin"}
    assert client.get("/api/admin/products").status_code == 200
    assert (
        client.post("/api/auth/password", json={"password": password}).status_code
        == 403
    )
    assert not password_path.exists()
    with db() as c:
        assert setting(c, "bootstrap_password") == ""
        assert (
            c.execute(
                "SELECT count(*) FROM sessions WHERE role='bootstrap' AND revoked=0"
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT 1 FROM sessions WHERE digest=? AND revoked=0",
                (digest(other_bootstrap),),
            ).fetchone()
            is None
        )


def test_multiple_passkeys_can_independently_login_and_duplicates_are_rejected(owner):
    first, second = Authenticator(), Authenticator()
    register(owner, first, "电脑")
    options = register(owner, second, "手机")
    assert {key["id"] for key in options["excludeCredentials"]} == {first.id}
    keys = owner.get("/api/auth/passkeys").json()
    assert {key["id"]: key["name"] for key in keys} == {
        first.id: "电脑",
        second.id: "手机",
    }

    for authenticator in (first, second, first):
        assert owner.post("/api/auth/logout", json={}).status_code == 200
        login(owner, authenticator)
    with db() as c:
        counts = {
            row["id"]: row["sign_count"]
            for row in c.execute("SELECT id, sign_count FROM credentials")
        }
    assert counts == {first.id: 2, second.id: 1}

    options = owner.post("/api/auth/register/options", json={}).json()
    assert {key["id"] for key in options["excludeCredentials"]} == {first.id, second.id}
    response = owner.post(
        "/api/auth/register/verify", json={"credential": first.registration(options)}
    )
    assert response.status_code == 409
    assert len(owner.get("/api/auth/passkeys").json()) == 2


def test_removing_one_passkey_preserves_other_and_cannot_remove_last(owner):
    first, second = Authenticator(), Authenticator()
    register(owner, first)
    register(owner, second)
    assert owner.delete("/api/auth/passkeys/" + first.id).status_code == 200
    assert owner.post("/api/auth/logout", json={}).status_code == 200
    response = owner.post(
        "/api/auth/login/verify",
        json={"credential": first.assertion(login_options(owner))},
    )
    assert response.status_code == 401
    assert owner.get("/api/auth/status").json()["role"] is None
    login(owner, second)
    assert owner.delete("/api/auth/passkeys/" + second.id).status_code == 409
    assert [key["id"] for key in owner.get("/api/auth/passkeys").json()] == [second.id]
    assert owner.post("/api/auth/logout", json={}).status_code == 200
    login(owner, second)


def test_unknown_key_and_wrong_signing_key_cannot_authenticate(owner):
    registered, unknown = Authenticator(), Authenticator()
    register(owner, registered)
    assert owner.post("/api/auth/logout", json={}).status_code == 200

    for authenticator, signer in ((unknown, None), (registered, unknown)):
        response = owner.post(
            "/api/auth/login/verify",
            json={
                "credential": authenticator.assertion(
                    login_options(owner), signer=signer
                )
            },
        )
        assert response.status_code == 401
        assert owner.get("/api/auth/status").json()["role"] is None
        assert owner.get("/api/admin/products").status_code == 401
    login(owner, registered)


def test_user_verification_challenge_and_counter_are_required(owner):
    authenticator = Authenticator()
    options = owner.post("/api/auth/register/options", json={}).json()
    assert (
        owner.post(
            "/api/auth/register/verify",
            json={"credential": authenticator.registration(options, verified=False)},
        ).status_code
        == 400
    )
    assert owner.get("/api/auth/passkeys").json() == []
    register(owner, authenticator)
    assert owner.post("/api/auth/logout", json={}).status_code == 200

    credential = authenticator.assertion(login_options(owner))
    assert (
        owner.post(
            "/api/auth/login/verify", json={"credential": credential}
        ).status_code
        == 200
    )
    assert owner.post("/api/auth/logout", json={}).status_code == 200
    assert (
        owner.post(
            "/api/auth/login/verify", json={"credential": credential}
        ).status_code
        == 400
    )  # A successfully consumed challenge cannot be replayed.
    login_options(owner)
    assert (
        owner.post(
            "/api/auth/login/verify", json={"credential": credential}
        ).status_code
        == 401
    )  # A previous response cannot satisfy a fresh challenge.

    for kwargs in ({"verified": False}, {"sign_count": 1}):
        credential = authenticator.assertion(login_options(owner), **kwargs)
        assert (
            owner.post(
                "/api/auth/login/verify", json={"credential": credential}
            ).status_code
            == 401
        )
        assert owner.get("/api/auth/status").json()["role"] is None
    login(owner, authenticator)


def test_staff_cannot_register_or_manage_owner_passkeys(owner, setup_product):
    authenticator = Authenticator()
    register(owner, authenticator)
    product_id, _ = setup_product()
    staff = owner.post(
        "/api/admin/staff", json={"product_id": product_id, "name": "员工"}
    ).json()
    assert (
        owner.post(
            "/api/staff/login", json={"token": staff["url"].split("#")[1]}
        ).status_code
        == 200
    )
    assert owner.get("/api/auth/status").json()["role"] == "staff"
    assert owner.post("/api/auth/register/options", json={}).status_code == 401
    assert (
        owner.post(
            "/api/auth/register/verify", json={"credential": {"id": authenticator.id}}
        ).status_code
        == 401
    )
    assert owner.get("/api/auth/passkeys").status_code == 401
    assert owner.delete("/api/auth/passkeys/" + authenticator.id).status_code == 401
    with db() as c:
        assert c.execute("SELECT count(*) FROM credentials").fetchone()[0] == 1

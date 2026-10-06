import hashlib
import json

import httpx
import pytest
from test_owner_cli import ORIGIN, OwnerMerchant, arguments, authorize

from extore import owner_client, shop_commands
from extore.manage_client import ManageError

SHOP = "12345678-1234-1234-1234-123456789012"
ROUTE = "a" * 32
IDENTITY = "b" * 32
PUBLIC = {
    "route_id": ROUTE,
    "issuer_id": IDENTITY,
    "name": "Issuer",
    "origin": "https://issuer.example",
    "path": "/",
    "public_key": "A" * 43,
}
OWNER = {"shop_id": SHOP, "origin": "https://merchant.example"}


class Client:
    def __init__(self, rows=None):
        self.rows = rows or [
            {
                **PUBLIC,
                "shop_id": SHOP,
                "identity_id": None,
                "enabled": True,
                "private_key": "NEVER-ECHO",
            }
        ]
        self.calls = []

    def request(self, owner, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if method == "GET":
            return self.rows
        return {
            **PUBLIC,
            "shop_id": kwargs.get("json", {}).get("shop_id", SHOP),
            "private_key": "NEVER-ECHO",
        }


def args(tmp_path, *argv):
    return arguments(tmp_path / "owner.json", "proxy", *argv)


def test_proxy_owner_commands_are_registered_and_keep_public_keys(tmp_path):
    client = Client()
    result = shop_commands.dispatch(client, OWNER, args(tmp_path, "routes", "list"))
    assert result["result"][0]["public_key"] == PUBLIC["public_key"]
    assert "private_key" not in result["result"][0]
    assert client.calls[0][2]["params"] == {"shop_id": SHOP}
    assert "proxy" in shop_commands.COMMANDS


def test_proxy_export_is_exact_six_fields_and_import_ignores_no_hidden_fields(tmp_path):
    client = Client()
    result = shop_commands.dispatch(
        client, OWNER, args(tmp_path, "routes", "export", ROUTE)
    )
    assert result == PUBLIC
    path = tmp_path / "route.json"
    path.write_text(json.dumps(result))
    imported = shop_commands.dispatch(
        client, OWNER, args(tmp_path, "routes", "import", "--json-file", str(path))
    )
    assert imported["result"]["public_key"] == PUBLIC["public_key"]
    assert client.calls[-1][2]["json"] == {**PUBLIC, "shop_id": SHOP}
    path.write_text(json.dumps({**PUBLIC, "private_key": "DO-NOT-IMPORT"}))
    with pytest.raises(ManageError, match="six public"):
        shop_commands.dispatch(
            client, OWNER, args(tmp_path, "routes", "import", "--json-file", str(path))
        )


def test_proxy_shop_scope_is_explicit_for_root_and_fixed_for_merchant(tmp_path):
    client = Client()
    with pytest.raises(ManageError, match="explicitly"):
        shop_commands.dispatch(
            client, {**OWNER, "shop_id": None}, args(tmp_path, "routes", "list")
        )
    with pytest.raises(ManageError, match="different shop"):
        shop_commands.dispatch(
            client, OWNER, args(tmp_path, "routes", "list", "--shop", "another-shop")
        )
    assert client.calls == []
    shop_commands.dispatch(
        client,
        {**OWNER, "shop_id": None},
        args(tmp_path, "routes", "list", "--shop", SHOP),
    )
    assert client.calls[-1][2]["params"] == {"shop_id": SHOP}


def test_route_mutation_reads_scope_and_sends_strict_bool_with_shop_query(tmp_path):
    client = Client()
    shop_commands.dispatch(client, OWNER, args(tmp_path, "routes", "disable", ROUTE))
    method, path, kwargs = client.calls[-1]
    assert method == "PUT" and path.endswith("/" + ROUTE)
    assert kwargs == {"json": {"enabled": False}, "params": {"shop_id": SHOP}}
    with pytest.raises(ManageError, match="selected shop"):
        shop_commands.dispatch(
            client, OWNER, args(tmp_path, "routes", "enable", "c" * 32)
        )
    with pytest.raises(ManageError, match="cannot issue"):
        shop_commands.dispatch(
            client, OWNER, args(tmp_path, "routes", "default", ROUTE)
        )


def test_local_issuer_route_uses_own_origin_and_explicit_default_only(tmp_path):
    client = Client()
    shop_commands.dispatch(
        client,
        OWNER,
        args(tmp_path, "routes", "create", "--name", "Local", "--identity", IDENTITY),
    )
    assert client.calls[-1][2]["json"] == {
        "name": "Local",
        "identity_id": IDENTITY,
        "origin": OWNER["origin"],
        "path": "/",
        "default_issuer": False,
        "shop_id": SHOP,
    }


def test_foreign_response_and_unsafe_target_never_create_an_import(tmp_path):
    client = Client(rows=[{**PUBLIC, "shop_id": "another-shop"}])
    with pytest.raises(ManageError, match="different shop"):
        shop_commands.dispatch(client, OWNER, args(tmp_path, "routes", "list"))
    for origin in (
        "http://issuer.example",
        "https://user:pass@issuer.example",
        "https://issuer.example/path",
        "https://issuer.example?code=secret",
        "https://issuer.example#secret",
    ):
        with pytest.raises(ManageError):
            shop_commands._public_proxy_route({**PUBLIC, "origin": origin}, input=True)


def test_actual_owner_transport_signs_route_mutation_and_shop_query(tmp_path):
    peer = OwnerMerchant()
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, peer)
    observed = []

    def server(request):
        if not request.url.path.startswith("/api/admin/proxy/"):
            return peer(request)
        assert "cookie" not in request.headers
        assert request.headers["authorization"] == "Bearer short-lived-owner-secret"
        assert request.url.params["shop_id"] == SHOP
        observed.append((request.method, request.url.raw_path.decode()))
        if request.method == "PUT":
            raw = request.read()
            body = json.loads(raw)
            assert body == {"enabled": False}
            challenge_id = request.headers["X-Extore-CLI-Challenge"]
            action = peer.actions.pop(challenge_id)
            body_hash = hashlib.sha256(raw).hexdigest()
            assert action == {
                "method": "PUT",
                "path": request.url.raw_path.decode(),
                "body_sha256": body_hash,
            }
            peer.verify(
                {
                    "public_key": peer.public,
                    "signature": request.headers["X-Extore-CLI-Signature"],
                },
                "extore-cli-owner-action-v1",
                ORIGIN,
                "owner-device",
                "safe-session-id",
                challenge_id,
                "one-use-action-challenge",
                "PUT",
                request.url.raw_path.decode(),
                body_hash,
            )
            return httpx.Response(
                200,
                json={
                    **PUBLIC,
                    "shop_id": SHOP,
                    "enabled": False,
                    "private_key": "never-export",
                },
            )
        return httpx.Response(200, json=[{**PUBLIC, "shop_id": SHOP, "enabled": True}])

    result = owner_client.execute(
        arguments(profile, "proxy", "routes", "disable", ROUTE, "--shop", SHOP),
        transport=httpx.MockTransport(server),
    )
    assert result["ok"] and result["result"]["enabled"] is False
    assert "never-export" not in json.dumps(result)
    assert observed == [
        ("GET", "/api/admin/proxy/routes?shop_id=" + SHOP),
        ("PUT", "/api/admin/proxy/routes/" + ROUTE + "?shop_id=" + SHOP),
    ]

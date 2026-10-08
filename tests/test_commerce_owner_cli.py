"""Storefront connection management uses existing shop-pinned signed owner devices."""

import json
import stat

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response
from test_commerce_oauth import connected
from test_owner_cli import ORIGIN as FAKE_ORIGIN
from test_owner_cli import OwnerMerchant, arguments, authorize, execute
from test_shop_cli import Workspace, seed_shop

from extore import owner_client as remote
from extore.app import app
from extore.config import ORIGIN
from extore.db import db
from extore.manage_client import ManageError
from extore.security import create_session

REGISTRATION = {
    "client_name": "示例商城",
    "redirect_uris": ["https://store.example.com/extore/callback"],
    "token_endpoint_auth_method": "none",
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
}
PREFIX = "/api/admin/commerce/"


class CommerceMerchant(OwnerMerchant):
    def __init__(self, shop=None):
        super().__init__()
        self.shop = shop
        self.connections = []
        self.response_override = None
        self.fail_mutation = False

    def __call__(self, request):
        # Reuse the real Ed25519 protocol peer, including one-use action proofs.
        response = super().__call__(request)
        if request.url.path.startswith(remote.OWNER_PREFIX):
            value = response.json()
            if "shop_id" in value:
                value.update(shop_id=self.shop, superadmin=self.shop is None)
                return httpx.Response(200, json=value)
            return response
        if not request.url.path.startswith(PREFIX):
            return response
        self.connections.append(request)
        shop = request.url.params["shop_id"]
        client = {
            **REGISTRATION,
            "id": "store-client",
            "client_id": "store-client",
            "shop_id": shop,
            "created_at": 1234,
            "client_id_issued_at": 1234,
            "enabled": True,
        }
        grant = {
            "id": "store-grant",
            "client_id": "store-client",
            "client_name": REGISTRATION["client_name"],
            "shop_id": shop,
            "scopes": ["products.read", "cards.issue"],
            "product_ids": ["product-a"],
            "products": [{"id": "product-a", "name": "商品"}],
            "card_limits": [
                {
                    "product_id": "product-a",
                    "variant_id": "default",
                    "max_count": 10,
                    "issued_count": 3,
                    "remaining": 7,
                }
            ],
            "expires": 9999999999,
            "created_at": 1234,
            "last_used": None,
            "revoked": False,
        }
        if self.fail_mutation and request.method != "GET":
            return httpx.Response(401, json={"detail": "revoked owner device"})
        if request.method == "DELETE":
            value = {"ok": True}
        elif request.method == "POST":
            assert json.loads(request.read()) == REGISTRATION
            value = client
        elif request.url.path.endswith("/clients"):
            value = {"clients": [client]}
        else:
            value = {"grants": [grant]}
        if self.response_override:
            value = self.response_override(value)
        return httpx.Response(200, json=value)


def private_json(tmp_path, value=REGISTRATION, name="registration.json"):
    path = tmp_path / name
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def signed_owner(tmp_path, shop=None):
    peer = CommerceMerchant(shop)
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, peer)
    peer.calls.clear()
    return profile, peer


def test_platform_registration_signed_shop_query_without_secret_or_approval(tmp_path):
    profile, peer = signed_owner(tmp_path)
    result = execute(
        profile,
        peer,
        "commerce",
        "clients",
        "create",
        "--origin",
        FAKE_ORIGIN,
        "--shop",
        "shop-a",
        "--json-file",
        str(private_json(tmp_path)),
    )
    assert result == {
        "ok": True,
        "client": {
            **REGISTRATION,
            "id": "store-client",
            "client_id": "store-client",
            "shop_id": "shop-a",
            "created_at": 1234,
            "client_id_issued_at": 1234,
            "enabled": True,
        },
    }
    assert dict(peer.connections[0].url.params) == {"shop_id": "shop-a"}
    assert peer.action_index == 1
    assert "x-extore-cli-signature" in peer.connections[0].headers
    assert not any("approve" in path for _, path, _, _ in peer.calls)
    assert "client_secret" not in json.dumps(result)


def test_merchant_defaults_to_pinned_shop_and_grants_show_remaining_quota(tmp_path):
    profile, peer = signed_owner(tmp_path, "shop-a")
    clients = execute(profile, peer, "commerce", "clients", "list")
    assert clients["clients"][0]["shop_id"] == "shop-a"
    grants = execute(profile, peer, "commerce", "grants", "list", "--view", "all")
    assert grants["grants"][0]["card_limits"][0]["remaining"] == 7
    assert dict(peer.connections[-1].url.params) == {"shop_id": "shop-a", "view": "all"}
    execute(profile, peer, "commerce", "grants", "list")
    assert peer.connections[-1].url.params["view"] == "active"
    execute(profile, peer, "commerce", "clients", "list", "--view", "all")
    assert dict(peer.connections[-1].url.params) == {"shop_id": "shop-a", "view": "all"}


@pytest.mark.parametrize(
    "group,operation,id",
    [
        ("clients", "delete", "store-client"),
        ("grants", "revoke", "store-grant"),
    ],
)
def test_revocation_is_signed_and_requires_explicit_confirmation(
    tmp_path, group, operation, id
):
    profile, peer = signed_owner(tmp_path, "shop-a")
    with pytest.raises(SystemExit):
        arguments(profile, "commerce", group, operation, id)
    assert not peer.connections
    result = execute(profile, peer, "commerce", group, operation, id, "--yes")
    assert result == {"ok": True}
    request = peer.connections[-1]
    assert request.method == "DELETE"
    assert request.url.path == PREFIX + group + "/" + id
    assert dict(request.url.params) == {"shop_id": "shop-a"}
    assert request.read() == b""
    assert "x-extore-cli-signature" in request.headers


@pytest.mark.parametrize(
    "id", ["../store-client", "store-client?shop_id=other", "id%2fother", "bad\nname"]
)
def test_revocation_rejects_invalid_path_identifiers(tmp_path, id):
    profile, peer = signed_owner(tmp_path, "shop-a")
    with pytest.raises(ManageError) as error:
        execute(profile, peer, "commerce", "clients", "delete", id, "--yes")
    assert error.value.code == "invalid_input"
    assert not peer.connections


@pytest.mark.parametrize("shop,selected", [(None, None), ("shop-a", "shop-b")])
def test_missing_or_cross_shop_selection_makes_no_requests(tmp_path, shop, selected):
    profile, peer = signed_owner(tmp_path, shop)
    argv = ["commerce", "clients", "list"]
    if selected:
        argv += ["--shop", selected]
    with pytest.raises(ManageError) as error:
        execute(profile, peer, *argv)
    assert error.value.code == "no_scope"
    assert not peer.calls


def test_client_registration_input_is_private_and_cannot_override_shop(tmp_path):
    profile, peer = signed_owner(tmp_path, "shop-a")
    path = private_json(tmp_path)
    path.chmod(0o644)
    with pytest.raises(ManageError) as error:
        execute(
            profile, peer, "commerce", "clients", "create", "--json-file", str(path)
        )
    assert error.value.code == "unsafe_input"
    assert not peer.calls
    path = private_json(tmp_path, {**REGISTRATION, "shop_id": "shop-b"})
    with pytest.raises(ManageError) as error:
        execute(
            profile, peer, "commerce", "clients", "create", "--json-file", str(path)
        )
    assert error.value.code == "invalid_input"
    assert not peer.calls


def test_registration_accepts_private_stdin(tmp_path, monkeypatch):
    import io
    import sys

    profile, peer = signed_owner(tmp_path, "shop-a")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(REGISTRATION)))
    result = execute(profile, peer, "commerce", "clients", "create", "--json-stdin")
    assert result["client"]["id"] == "store-client"


def test_metadata_drops_unexpected_secrets_including_nested_grant_fields(tmp_path):
    profile, peer = signed_owner(tmp_path, "shop-a")

    def inject(value):
        for rows in value.values():
            if not isinstance(rows, list):
                continue
            for row in rows:
                row.update(
                    client_secret="PRIVATE_SENTINEL", refresh_token="PRIVATE_SENTINEL"
                )
                for name in ("products", "card_limits"):
                    for nested in row.get(name, []):
                        nested["client_secret"] = "PRIVATE_SENTINEL"
        return value

    peer.response_override = inject
    for group in ("clients", "grants"):
        result = execute(profile, peer, "commerce", group, "list")
        assert "PRIVATE_SENTINEL" not in json.dumps(result)
        assert "client_secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "field,wrapped",
    [
        ("client_name", {"client_secret": "PRIVATE_SENTINEL"}),
        ("scopes", [{"refresh_token": "PRIVATE_SENTINEL"}]),
        ("expires", {"access_token": "PRIVATE_SENTINEL"}),
        (
            "products",
            [{"id": "product-a", "name": {"client_secret": "PRIVATE_SENTINEL"}}],
        ),
        ("card_limits", [{"product_id": {"access_token": "PRIVATE_SENTINEL"}}]),
    ],
)
def test_allowed_metadata_fields_cannot_wrap_credentials_in_wrong_types(
    tmp_path, field, wrapped
):
    profile, peer = signed_owner(tmp_path, "shop-a")

    def change(value):
        value["grants"][0][field] = wrapped
        return value

    peer.response_override = change
    with pytest.raises(ManageError) as error:
        execute(profile, peer, "commerce", "grants", "list")
    assert error.value.code == "invalid_response"
    assert "PRIVATE_SENTINEL" not in str(error.value)


def test_malformed_client_name_cannot_wrap_credentials(tmp_path):
    profile, peer = signed_owner(tmp_path, "shop-a")

    def change(value):
        value["clients"][0]["client_name"] = {"refresh_token": "PRIVATE_SENTINEL"}
        return value

    peer.response_override = change
    with pytest.raises(ManageError) as error:
        execute(profile, peer, "commerce", "clients", "list")
    assert error.value.code == "invalid_response"
    assert "PRIVATE_SENTINEL" not in str(error.value)


@pytest.mark.parametrize("kind", ["clients", "grants"])
def test_response_cannot_return_another_shops_metadata(tmp_path, kind):
    profile, peer = signed_owner(tmp_path, "shop-a")

    def change(value):
        value[kind][0].update(shop_id="shop-b")
        return value

    peer.response_override = change
    with pytest.raises(ManageError) as error:
        execute(profile, peer, "commerce", kind, "list")
    assert error.value.code == "invalid_response"


@pytest.mark.parametrize("value", [{}, {"clients": {}}, {"clients": ["secret"]}])
def test_malformed_server_shape_is_rejected_without_echo(tmp_path, value):
    profile, peer = signed_owner(tmp_path, "shop-a")
    peer.response_override = lambda _: value
    with pytest.raises(ManageError) as error:
        execute(profile, peer, "commerce", "clients", "list")
    assert error.value.code == "invalid_response"
    assert "secret" not in str(error.value)


def test_existing_output_blocks_registration_before_remote_mutation(tmp_path):
    profile, peer = signed_owner(tmp_path, "shop-a")
    output = tmp_path / "saved.json"
    output.write_text("preserve")
    with pytest.raises(ManageError) as error:
        execute(
            profile,
            peer,
            "commerce",
            "clients",
            "create",
            "--json-file",
            str(private_json(tmp_path)),
            "--output",
            str(output),
        )
    assert error.value.code == "output_exists"
    assert not peer.connections
    assert output.read_text() == "preserve"


def test_private_export_has_no_secret_echo_and_failed_mutation_is_not_retried(tmp_path):
    profile, peer = signed_owner(tmp_path, "shop-a")
    output = tmp_path / "saved.json"
    result = execute(
        profile, peer, "commerce", "clients", "list", "--output", str(output)
    )
    assert result["output"] == str(output)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert "client" not in result
    assert json.loads(output.read_text())["clients"][0]["shop_id"] == "shop-a"
    peer.fail_mutation = True
    failed = tmp_path / "failed.json"
    with pytest.raises(ManageError):
        execute(
            profile,
            peer,
            "commerce",
            "clients",
            "delete",
            "store-client",
            "--yes",
            "--output",
            str(failed),
        )
    assert not failed.exists()
    assert len([r for r in peer.connections if r.method == "DELETE"]) == 1


@pytest.mark.parametrize(
    "path",
    [
        "/api/admin/commerce/requests/req-a",
        "/api/admin/commerce/requests/req-a/approve",
        "/api/admin/commerce/requests/req-a/deny",
    ],
)
def test_generic_owner_api_does_not_expose_browser_consent(tmp_path, path):
    profile, peer = signed_owner(tmp_path, "shop-a")
    method = "GET" if path.endswith("req-a") else "POST"
    with pytest.raises(ManageError) as error:
        execute(profile, peer, "api", method, path)
    assert error.value.code == "invalid_path"
    assert not peer.connections


def test_real_backend_registration_listing_and_revoke_preserve_browser_only_consent(
    tmp_path,
):
    with TestClient(app, base_url=ORIGIN) as api:
        workspace = Workspace(api, tmp_path)
        sid, password, _ = seed_shop("commerce-cli@example.test")
        device = workspace.login("commerce-cli@example.test", password)
        client = workspace.command(
            "commerce",
            "clients",
            "create",
            "--grant",
            device["device_id"],
            "--json-file",
            workspace.file(REGISTRATION),
        )["client"]
        assert client["shop_id"] == sid
        assert workspace.command("commerce", "clients", "list")["clients"] == [client]
        pid = workspace.command(
            "product",
            "create",
            "--json-file",
            workspace.file({"name": "商城测试商品", "parameters": []}),
        )["product"]["id"]
        with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as browser:
            with db() as c:
                cookie = create_session(c, Response(), "admin", shop_id=sid)
            browser.cookies.set("extore_session", cookie)
            _, tokens, _, headers = connected(browser, api, pid)
        grants = workspace.command("commerce", "grants", "list")["grants"]
        assert grants[0]["id"] == tokens["grant_id"]
        assert grants[0]["card_limits"][0]["remaining"] == 5
        assert workspace.command(
            "commerce", "grants", "revoke", tokens["grant_id"], "--yes"
        ) == {"ok": True}
        assert not workspace.command("commerce", "grants", "list")["grants"]
        assert workspace.command("commerce", "grants", "list", "--view", "all")[
            "grants"
        ][0]["revoked"]
        assert (
            api.get("/api/integrations/commerce/products", headers=headers).status_code
            == 401
        )
        assert workspace.command(
            "commerce", "clients", "delete", client["id"], "--yes"
        ) == {"ok": True}
        with db() as c:
            assert (
                c.execute(
                    "SELECT revoked FROM commerce_clients WHERE id=?", (client["id"],)
                ).fetchone()[0]
                == 1
            )
        assert not any(
            path.endswith(("/approve", "/deny")) for _, path in workspace.paths
        )

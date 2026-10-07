"""Issuer replacement and history cleanup stay tenant-bound and device-signed."""

import hashlib
import json
import re

import httpx
import pytest
from test_owner_cli import ORIGIN, OwnerMerchant, arguments, authorize

from extore import owner_client, shop_commands
from extore.manage_client import ManageError

SHOP = "12345678-1234-1234-1234-123456789012"
IDENTITY = "b" * 32
ROUTE = "a" * 32
OWNER = {"shop_id": SHOP, "origin": ORIGIN}
PRIVATE = "never-return-the-issuer-private-key"


def args(tmp_path, *argv):
    return arguments(tmp_path / "owner.json", "proxy", *argv)


class Client:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, owner, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return self.response


def preview(identity=False, **changes):
    return {
        "id" if identity else "route_id": IDENTITY if identity else ROUTE,
        "shop_id": SHOP,
        "eligible": True,
        "current": False,
        "route_count": 1,
        "issued_card_count": 0,
        "private_key": PRIVATE,
        **changes,
    }


@pytest.mark.parametrize("operation", ["replace", "create"])
def test_replace_has_same_public_only_contract_as_compatibility_create(
    tmp_path, operation
):
    client = Client(
        {"id": IDENTITY, "shop_id": SHOP, "current": True, "private_key": PRIVATE}
    )
    value = shop_commands.dispatch(
        client, OWNER, args(tmp_path, "identities", operation, "--name", "main")
    )
    assert value["result"]["current"] is True
    assert PRIVATE not in json.dumps(value)
    assert client.calls == [
        (
            "POST",
            "/api/admin/proxy/identities",
            {"json": {"name": "main", "shop_id": SHOP}},
        )
    ]


def test_identity_list_defaults_current_and_history_is_explicit(tmp_path):
    client = Client(
        [{"id": IDENTITY, "shop_id": SHOP, "current": False, "private_key": PRIVATE}]
    )
    shop_commands.dispatch(client, OWNER, args(tmp_path, "identities", "list"))
    result = shop_commands.dispatch(
        client, OWNER, args(tmp_path, "identities", "list", "--history")
    )
    assert client.calls == [
        ("GET", "/api/admin/proxy/identities", {"params": {"shop_id": SHOP}}),
        (
            "GET",
            "/api/admin/proxy/identities",
            {"params": {"shop_id": SHOP, "history": "true"}},
        ),
    ]
    assert result["result"][0]["current"] is False
    assert PRIVATE not in json.dumps(result)


def test_route_list_defaults_active_and_explicit_history_keeps_archive_metadata(
    tmp_path,
):
    client = Client(
        [{"route_id": ROUTE, "shop_id": SHOP, "archived": True, "private_key": PRIVATE}]
    )
    shop_commands.dispatch(client, OWNER, args(tmp_path, "routes", "list"))
    result = shop_commands.dispatch(
        client, OWNER, args(tmp_path, "routes", "list", "--history")
    )
    assert result["result"][0]["archived"] is True
    assert PRIVATE not in json.dumps(result)
    assert client.calls == [
        ("GET", "/api/admin/proxy/routes", {"params": {"shop_id": SHOP}}),
        (
            "GET",
            "/api/admin/proxy/routes",
            {"params": {"shop_id": SHOP, "history": "true"}},
        ),
    ]


@pytest.mark.parametrize("operation", ["enable", "disable", "default", "export"])
def test_route_lookup_includes_history_without_adding_it_to_write_scope(
    tmp_path, operation
):
    row = {
        "route_id": ROUTE,
        "issuer_id": IDENTITY,
        "shop_id": SHOP,
        "identity_id": IDENTITY,
        "name": "main",
        "origin": ORIGIN,
        "path": "/",
        "public_key": "A" * 43,
        "enabled": False,
        "archived": True,
    }

    class HistoryClient(Client):
        def request(self, owner, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            return [row] if method == "GET" else row

    client = HistoryClient(None)
    shop_commands.dispatch(client, OWNER, args(tmp_path, "routes", operation, ROUTE))
    assert client.calls[0] == (
        "GET",
        "/api/admin/proxy/routes",
        {"params": {"shop_id": SHOP, "history": "true"}},
    )
    if operation != "export":
        assert client.calls[-1][2]["params"] == {"shop_id": SHOP}


@pytest.mark.parametrize("group,target", [("identities", IDENTITY), ("routes", ROUTE)])
def test_cleanup_preview_is_read_only_and_delete_is_explicit(tmp_path, group, target):
    client = Client(preview(group == "identities"))
    result = shop_commands.dispatch(
        client, OWNER, args(tmp_path, group, "cleanup-preview", target)
    )
    assert result["result"]["eligible"] is True
    assert PRIVATE not in json.dumps(result)
    assert client.calls == [
        (
            "GET",
            f"/api/admin/proxy/{group}/{target}/cleanup-preview",
            {"params": {"shop_id": SHOP}},
        )
    ]
    client.response = {
        "ok": True,
        "shop_id": SHOP,
        "id" if group == "identities" else "route_id": target,
        "deleted": True,
        "private_key": PRIVATE,
    }
    result = shop_commands.dispatch(
        client, OWNER, args(tmp_path, group, "delete", target, "--yes")
    )
    assert result["result"]["deleted"] is True
    assert PRIVATE not in json.dumps(result)
    assert client.calls[-1] == (
        "DELETE",
        f"/api/admin/proxy/{group}/{target}",
        {"params": {"shop_id": SHOP}},
    )


def test_cleanup_aggregate_returns_only_deleted_identifiers_and_counts(tmp_path):
    client = Client(
        {
            "ok": True,
            "shop_id": SHOP,
            "deleted_identity_ids": [IDENTITY],
            "deleted_route_ids": [ROUTE],
            "deleted_identity_count": 1,
            "deleted_route_count": 1,
            "private_key": PRIVATE,
        }
    )
    result = shop_commands.dispatch(client, OWNER, args(tmp_path, "cleanup", "--yes"))
    assert result["result"]["deleted_identity_count"] == 1
    assert PRIVATE not in json.dumps(result)
    assert client.calls == [
        ("POST", "/api/admin/proxy/cleanup", {"json": {"shop_id": SHOP}})
    ]


def test_aggregate_cleanup_preview_is_read_only_and_returns_scoped_counts(tmp_path):
    client = Client(
        {
            "shop_id": SHOP,
            "eligible_identity_ids": [IDENTITY],
            "eligible_route_ids": [ROUTE],
            "eligible_identity_count": 1,
            "eligible_route_count": 1,
            "private_key": PRIVATE,
        }
    )
    result = shop_commands.dispatch(client, OWNER, args(tmp_path, "cleanup-preview"))
    assert result["result"]["eligible_route_count"] == 1
    assert PRIVATE not in json.dumps(result)
    assert client.calls == [
        ("GET", "/api/admin/proxy/cleanup", {"params": {"shop_id": SHOP}})
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"eligible_identity_count": 2},
        {"eligible_route_count": True},
        {"eligible_identity_ids": [IDENTITY, IDENTITY], "eligible_identity_count": 2},
        {"eligible_route_ids": [{"private_key": PRIVATE}]},
        {"shop_id": "another-shop"},
    ],
)
def test_aggregate_preview_rejects_inconsistent_counts_and_foreign_scope(
    tmp_path, changes
):
    client = Client(
        {
            "shop_id": SHOP,
            "eligible_identity_ids": [IDENTITY],
            "eligible_route_ids": [ROUTE],
            "eligible_identity_count": 1,
            "eligible_route_count": 1,
            **changes,
        }
    )
    with pytest.raises(ManageError) as error:
        shop_commands.dispatch(client, OWNER, args(tmp_path, "cleanup-preview"))
    assert error.value.code == "invalid_response"


def test_clear_mandatory_issuer_is_rejected_before_any_owner_http_request(tmp_path):
    peer = OwnerMerchant()
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, peer)
    before = len(peer.calls)
    with pytest.raises(ManageError) as error:
        owner_client.execute(
            arguments(
                profile, "proxy", "routes", "default", ROUTE, "--clear", "--shop", SHOP
            ),
            transport=httpx.MockTransport(peer),
        )
    assert error.value.code == "invalid_input" and len(peer.calls) == before


@pytest.mark.parametrize(
    "argv",
    [
        ("identities", "delete", IDENTITY),
        ("routes", "delete", ROUTE),
        ("cleanup",),
    ],
)
def test_unconfirmed_cleanup_fails_in_parser_without_a_remote_call(tmp_path, argv):
    with pytest.raises(SystemExit):
        args(tmp_path, *argv)


@pytest.mark.parametrize(
    "target", ["../routes", "a" * 32 + "?shop_id=foreign", "%2F", "not-an-id"]
)
def test_cleanup_rejects_non_identity_paths_before_any_remote_call(tmp_path, target):
    client = Client({})
    with pytest.raises(ManageError) as error:
        shop_commands.dispatch(
            client, OWNER, args(tmp_path, "routes", "delete", target, "--yes")
        )
    assert error.value.code == "invalid_input" and not client.calls


@pytest.mark.parametrize(
    "argv",
    [
        ("identities", "replace", "--name", "main"),
        ("identities", "cleanup-preview", IDENTITY),
        ("routes", "delete", ROUTE, "--yes"),
        ("cleanup", "--yes"),
        ("cleanup-preview",),
    ],
)
def test_new_commands_require_explicit_root_shop_and_reject_foreign_merchant(
    tmp_path, argv
):
    client = Client({})
    with pytest.raises(ManageError) as error:
        shop_commands.dispatch(
            client, {**OWNER, "shop_id": None}, args(tmp_path, *argv)
        )
    assert error.value.code == "no_scope"
    with pytest.raises(ManageError) as error:
        shop_commands.dispatch(
            client, OWNER, args(tmp_path, *argv, "--shop", "foreign-shop")
        )
    assert error.value.code == "no_scope" and not client.calls


@pytest.mark.parametrize(
    "changes",
    [
        {"shop_id": "foreign-shop"},
        {"id": "c" * 32},
        {"eligible": 1},
        {"issued_card_count": -1},
        {"route_count": True},
        {"current": "false"},
    ],
)
def test_cleanup_preview_rejects_wrong_scope_target_or_corrupt_status(
    tmp_path, changes
):
    with pytest.raises(ManageError) as error:
        shop_commands.dispatch(
            Client(preview(True, **changes)),
            OWNER,
            args(tmp_path, "identities", "cleanup-preview", IDENTITY),
        )
    assert error.value.code == "invalid_response"


def test_actual_owner_transport_signs_delete_and_aggregate_cleanup(tmp_path):
    peer = OwnerMerchant()
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, peer)
    observed = []

    def server(request):
        if not request.url.path.startswith("/api/admin/proxy/"):
            return peer(request)
        assert "cookie" not in request.headers
        assert request.headers["authorization"] == "Bearer short-lived-owner-secret"
        raw = request.read()
        body = json.loads(raw) if raw else {}
        if request.method == "DELETE":
            assert dict(request.url.params) == {"shop_id": SHOP} and not raw
            result = {"ok": True, "shop_id": SHOP, "route_id": ROUTE, "deleted": True}
        else:
            assert request.method == "POST" and body == {"shop_id": SHOP}
            result = {
                "ok": True,
                "shop_id": SHOP,
                "deleted_identity_ids": [],
                "deleted_route_ids": [],
                "deleted_identity_count": 0,
                "deleted_route_count": 0,
            }
        challenge_id = request.headers["X-Extore-CLI-Challenge"]
        body_hash = hashlib.sha256(raw).hexdigest()
        assert peer.actions.pop(challenge_id) == {
            "method": request.method,
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
            request.method,
            request.url.raw_path.decode(),
            body_hash,
        )
        observed.append((request.method, request.url.raw_path.decode()))
        return httpx.Response(200, json={**result, "private_key": PRIVATE})

    for argv in (("routes", "delete", ROUTE, "--yes"), ("cleanup", "--yes")):
        result = owner_client.execute(
            arguments(profile, "proxy", *argv, "--shop", SHOP),
            transport=httpx.MockTransport(server),
        )
        assert result["ok"] and PRIVATE not in json.dumps(result)
    assert observed == [
        ("DELETE", "/api/admin/proxy/routes/" + ROUTE + "?shop_id=" + SHOP),
        ("POST", "/api/admin/proxy/cleanup"),
    ]


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/admin/proxy/identities"),
        ("POST", "/api/admin/proxy/identities"),
        ("GET", f"/api/admin/proxy/identities/{IDENTITY}/cleanup-preview"),
        ("DELETE", f"/api/admin/proxy/identities/{IDENTITY}"),
        ("GET", f"/api/admin/proxy/routes/{ROUTE}/cleanup-preview"),
        ("DELETE", f"/api/admin/proxy/routes/{ROUTE}"),
        ("POST", "/api/admin/proxy/cleanup"),
    ],
)
def test_named_proxy_endpoints_have_exact_owner_api_routes(method, path):
    assert any(
        m == method and re.fullmatch(pattern, path)
        for m, pattern in owner_client._API_ROUTES
    )
    assert not any(
        m == method and re.fullmatch(pattern, path + "/private-key")
        for m, pattern in owner_client._API_ROUTES
    )

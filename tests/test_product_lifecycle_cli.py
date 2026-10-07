"""CLI lifecycle confirmation, scope and actual owner signature protocol."""

import json

import httpx
import pytest
from test_manage_cli import MockMerchant, login
from test_manage_cli import run as manage_run
from test_owner_cli import OwnerMerchant, authorize
from test_owner_cli import execute as owner_run

from extore.manage_client import ManageError
from extore.manage_commands import PERMISSIONS


class LifecycleOwner(OwnerMerchant):
    def __init__(self):
        super().__init__()
        self.deleted = False
        self.queries = []
        self.lifecycle_response = None

    def __call__(self, request):
        # The parent verifies the real Ed25519 action challenge, raw body hash,
        # method/path/query and short-lived owner session before this response.
        response = super().__call__(request)
        path = request.url.path
        self.queries.append((request.method, path, dict(request.url.params)))
        if path == "/api/admin/products":
            view = request.url.params.get("view", "active")
            rows = [
                {
                    **self.product,
                    **(
                        {"deleted": True, "deleted_at": 1700000000}
                        if self.deleted
                        else {}
                    ),
                }
            ]
            return httpx.Response(
                200,
                json=rows
                if view in ("all", "history") or (view == "deleted") == self.deleted
                else [],
            )
        if request.method == "DELETE" and path == "/api/admin/products/product-a":
            assert json.loads(request.content) == {"confirmed": True}
            self.deleted = True
        elif (
            request.method == "POST" and path == "/api/admin/products/product-a/restore"
        ):
            assert request.content == b""
            self.deleted = False
        else:
            return response
        return httpx.Response(
            200,
            json=self.lifecycle_response
            or {
                "ok": True,
                "product_id": "product-a",
                "deleted": self.deleted,
                "deleted_at": 1700000000 if self.deleted else None,
                "private_key": "must-not-appear",
                "content": "must-not-appear",
            },
        )


class LifecycleManager(MockMerchant):
    def __init__(self):
        super().__init__()
        self.links["delete"] = ("product-a", ["product.delete"])
        self.links["legacyfull"] = ("product-a", list(PERMISSIONS[:8]))
        self.deleted = False
        self.lifecycle_calls = []
        self.product_queries = []

    def __call__(self, request):
        path = request.url.path
        if path in (
            "/api/manage/product",
            "/api/manage/product/restore",
        ) and request.method in ("DELETE", "POST"):
            self.lifecycle_calls.append(
                (request.method, path, dict(request.url.params), request.content)
            )
            token = request.headers["authorization"].removeprefix("Bearer ")
            grant = self.sessions[token]
            assert grant["permissions"] == ["product.delete"]
            assert (
                request.url.params["product_id"] == grant["product_id"] == "product-a"
            )
            if request.method == "DELETE":
                assert json.loads(request.content) == {"confirmed": True}
                self.deleted = True
            else:
                assert request.content == b""
                self.deleted = False
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "product_id": "product-a",
                    "deleted": self.deleted,
                    "deleted_at": 1700000000 if self.deleted else None,
                },
            )
        response = super().__call__(request)
        if path == "/api/manage/products":
            self.product_queries.append(dict(request.url.params))
            rows = response.json()
            if self.deleted:
                rows[0].update(deleted=True, deleted_at=1700000000)
            view = request.url.params.get("view", "active")
            return httpx.Response(
                200,
                json=rows
                if view in ("all", "history") or (view == "deleted") == self.deleted
                else [],
            )
        return response


def test_named_owner_delete_restore_signed_actions_and_recycle_views(tmp_path):
    peer = LifecycleOwner()
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, peer)
    deleted = owner_run(
        profile, peer, "product", "delete", "--product", "product-a", "--yes"
    )
    assert deleted == {
        "ok": True,
        "product_id": "product-a",
        "deleted": True,
        "deleted_at": 1700000000,
    }
    assert not owner_run(profile, peer, "products")["products"]
    assert (
        owner_run(profile, peer, "product", "list", "--view", "deleted")["products"][0][
            "deleted"
        ]
        is True
    )
    queued = owner_run(profile, peer, "queues")
    assert queued["queues"][0]["product_id"] == "product-a"
    assert peer.queries[-2][2]["view"] == "history"
    restored = owner_run(profile, peer, "product", "restore", "--product", "product-a")
    assert restored["deleted"] is False and restored["deleted_at"] is None
    assert owner_run(profile, peer, "products")["products"]
    mutations = [
        call
        for call in peer.calls
        if call[0] in ("DELETE", "POST") and call[1].startswith("/api/admin/products/")
    ]
    assert len(mutations) == 2 and all(
        "x-extore-cli-signature" in call[3] for call in mutations
    )
    assert "must-not-appear" not in json.dumps([deleted, restored])


@pytest.mark.parametrize("generic", [False, True])
def test_unconfirmed_owner_delete_does_not_renew_expired_session_or_contact_server(
    tmp_path, generic
):
    peer = LifecycleOwner()
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, peer)
    saved = json.loads(profile.read_text())
    saved["owners"][0]["expires"] = 0
    profile.write_text(json.dumps(saved))
    before = len(peer.calls)
    argv = (
        ("api", "DELETE", "/api/admin/products/product-a", "--product", "product-a")
        if generic
        else ("product", "delete", "--product", "product-a")
    )
    with pytest.raises(ManageError) as error:
        owner_run(profile, peer, *argv)
    assert error.value.code == "confirmation_required" and len(peer.calls) == before


@pytest.mark.parametrize(
    "prefix", ["/api/admin/products/product-a", "/api/manage/product"]
)
def test_owner_generic_lifecycle_uses_safe_adapter_and_actual_signed_action(
    tmp_path, prefix
):
    peer = LifecycleOwner()
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, peer)
    result = owner_run(
        profile, peer, "api", "DELETE", prefix, "--product", "product-a", "--yes"
    )
    assert result["result"]["deleted"] is True
    result = owner_run(
        profile, peer, "api", "POST", prefix + "/restore", "--product", "product-a"
    )
    assert result["result"]["deleted"] is False
    assert peer.calls[-1][1] == "/api/admin/products/product-a/restore"


def test_product_delete_permission_is_explicit_and_delete_only_grant_needs_no_edit_read(
    tmp_path, monkeypatch
):
    peer = LifecycleManager()
    profile = tmp_path / "private" / "manager.json"
    login(peer, profile, monkeypatch, "legacyfull")
    with pytest.raises(ManageError) as error:
        manage_run(
            peer, profile, "product", "delete", "--product", "product-a", "--yes"
        )
    assert error.value.code == "no_scope" and not peer.lifecycle_calls
    login(peer, profile, monkeypatch, "delete")
    result = manage_run(
        peer, profile, "product", "delete", "--product", "product-a", "--yes"
    )
    assert result["deleted"] is True
    assert not manage_run(peer, profile, "products")["products"]
    assert (
        manage_run(peer, profile, "products", "--view", "deleted")["products"][0][
            "deleted"
        ]
        is True
    )
    restored = manage_run(peer, profile, "product", "restore", "--product", "product-a")
    assert restored["deleted"] is False
    assert [call[1] for call in peer.lifecycle_calls] == [
        "/api/manage/product",
        "/api/manage/product/restore",
    ]
    assert not any(
        path == "/api/manage/product" and method == "GET"
        for method, path, *_ in peer.calls
    )


@pytest.mark.parametrize("generic", [False, True])
def test_manager_confirmation_gate_is_local_and_foreign_scope_is_rejected(
    tmp_path, monkeypatch, generic
):
    peer = LifecycleManager()
    profile = tmp_path / "private" / "manager.json"
    login(peer, profile, monkeypatch, "delete")
    before = len(peer.calls)
    argv = (
        ("api", "DELETE", "/api/manage/product", "--product", "product-a")
        if generic
        else ("product", "delete", "--product", "product-a")
    )
    with pytest.raises(ManageError) as error:
        manage_run(peer, profile, *argv)
    assert error.value.code == "confirmation_required" and len(peer.calls) == before
    with pytest.raises(ManageError):
        manage_run(
            peer,
            profile,
            "api",
            "DELETE",
            "/api/manage/product",
            "--product",
            "product-a",
            "--query",
            "product_id=product-b",
            "--yes",
        )
    assert not peer.lifecycle_calls


@pytest.mark.parametrize(
    "bad",
    [
        {
            "ok": True,
            "product_id": "foreign",
            "deleted": True,
            "deleted_at": 1700000000,
        },
        {
            "ok": True,
            "product_id": "product-a",
            "deleted": "true",
            "deleted_at": 1700000000,
        },
        {"ok": True, "product_id": "product-a", "deleted": True, "deleted_at": None},
        {"ok": True, "product_id": "product-a", "deleted": True, "deleted_at": 10**400},
    ],
)
def test_malformed_lifecycle_response_is_generic_and_never_prints_private_fields(
    tmp_path, bad
):
    peer = LifecycleOwner()
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, peer)
    peer.lifecycle_response = {**bad, "private_key": "synthetic-private-secret"}
    with pytest.raises(ManageError) as error:
        owner_run(profile, peer, "product", "delete", "--product", "product-a", "--yes")
    assert (
        error.value.code == "invalid_response"
        and "synthetic-private-secret" not in str(error.value)
    )


def test_generic_confirm_body_is_strict_and_never_substitutes_for_yes(
    tmp_path, monkeypatch
):
    peer = LifecycleOwner()
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, peer)
    source = tmp_path / "confirmation.json"
    source.write_text('{"confirmed":true}')
    with pytest.raises(ManageError) as error:
        owner_run(
            profile,
            peer,
            "api",
            "DELETE",
            "/api/admin/products/product-a",
            "--product",
            "product-a",
            "--json-file",
            str(source),
        )
    assert error.value.code == "confirmation_required"
    source.write_text('{"confirmed":true,"extra":"private"}')
    with pytest.raises(ManageError) as error:
        owner_run(
            profile,
            peer,
            "api",
            "DELETE",
            "/api/admin/products/product-a",
            "--product",
            "product-a",
            "--yes",
            "--json-file",
            str(source),
        )
    assert error.value.code == "invalid_input" and "private" not in str(error.value)
    assert not peer.deleted

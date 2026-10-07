"""Permanent recycle-bin removal uses explicit scope and signed snapshots."""

import json

import httpx
import pytest
from test_manage_cli import login
from test_manage_cli import run as manage_run
from test_owner_cli import authorize
from test_owner_cli import execute as owner_run
from test_product_lifecycle_cli import LifecycleManager, LifecycleOwner

from extore.manage_client import ManageError
from extore.manage_commands import PERMISSIONS


class PurgeOwner(LifecycleOwner):
    def __init__(self, count=1):
        super().__init__()
        self.rows = [
            {
                "id": "product-a" if index == 0 else f"product-{index}",
                "name": "retired",
                "shop_id": "shop-a",
                "deleted": True,
                "deleted_at": 1700000000,
            }
            for index in range(count)
        ]
        self.purge_calls = []
        self.bad_response = None

    def __call__(self, request):
        response = super().__call__(request)
        if response.status_code != 200:
            return response
        path = request.url.path
        if path == "/api/admin/products":
            view = request.url.params.get("view", "active")
            rows = (
                self.rows
                if view == "history"
                else [row for row in self.rows if not row.get("purged")]
            )
            if view == "active":
                rows = []
            return httpx.Response(200, json=rows)
        if request.method == "POST" and (
            path == "/api/admin/products/empty-trash" or path.endswith("/purge")
        ):
            # Super verifies the real owner action signature including raw query
            # and body hash before reaching this business fixture response.
            self.purge_calls.append(
                (path, dict(request.url.params), json.loads(request.content))
            )
            if path.endswith("/purge"):
                assert json.loads(request.content) == {"confirmed": True}
                ids = [path.split("/")[-2]]
                result = {
                    "ok": True,
                    "product_id": ids[0],
                    "deleted": True,
                    "purged": True,
                    "purged_at": 1700000001,
                }
            else:
                body = json.loads(request.content)
                assert (
                    set(body) == {"confirmed", "product_ids"}
                    and body["confirmed"] is True
                )
                ids = body["product_ids"]
                result = {
                    "ok": True,
                    "purged_product_ids": ids,
                    "purged_count": len(ids),
                    "preserved_fulfillment": True,
                }
            for row in self.rows:
                if row["id"] in ids:
                    row.update(purged=True, purged_at=1700000001)
            return httpx.Response(
                200,
                json=self.bad_response or {**result, "private_key": "must-not-appear"},
            )
        return response


class PurgeManager(LifecycleManager):
    def __init__(self):
        super().__init__()
        self.links["purge"] = ("product-a", ["product.purge"])
        self.links["oldnine"] = ("product-a", list(PERMISSIONS[:-1]))
        self.retired = True
        self.purged = False
        self.purge_calls = []

    def __call__(self, request):
        if request.method == "POST" and request.url.path in (
            "/api/manage/product/purge",
            "/api/manage/products/empty-trash",
        ):
            token = request.headers["authorization"].removeprefix("Bearer ")
            assert self.sessions[token]["permissions"] == ["product.purge"]
            assert request.url.params["product_id"] == "product-a"
            body = json.loads(request.content)
            self.purge_calls.append((request.url.path, dict(request.url.params), body))
            self.purged = True
            if request.url.path.endswith("/purge"):
                assert body == {"confirmed": True}
                result = {
                    "ok": True,
                    "product_id": "product-a",
                    "deleted": True,
                    "purged": True,
                    "purged_at": 1700000001,
                }
            else:
                assert body == {"confirmed": True, "product_ids": ["product-a"]}
                result = {
                    "ok": True,
                    "purged_product_ids": ["product-a"],
                    "purged_count": 1,
                    "preserved_fulfillment": True,
                }
            return httpx.Response(200, json=result)
        response = super().__call__(request)
        if request.url.path == "/api/manage/products":
            view = request.url.params.get("view", "active")
            row = {
                "id": "product-a",
                "name": "retired",
                "deleted": True,
                "deleted_at": 1700000000,
            }
            if self.purged:
                row.update(purged=True, purged_at=1700000001)
            return httpx.Response(
                200,
                json=[row]
                if self.retired
                and (
                    view == "history" or not self.purged and view in ("deleted", "all")
                )
                else [],
            )
        return response


def test_owner_empty_uses_single_shop_snapshot_signed_exact_ids_and_history(tmp_path):
    peer = PurgeOwner(3)
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, peer)
    before = len(peer.calls)
    with pytest.raises(ManageError) as error:
        owner_run(profile, peer, "trash", "empty", "--yes")
    assert error.value.code == "no_scope" and len(peer.calls) == before
    result = owner_run(profile, peer, "trash", "empty", "--shop", "shop-a", "--yes")
    assert result["purged_count"] == 3 and result["preserved_fulfillment"] is True
    assert peer.purge_calls == [
        (
            "/api/admin/products/empty-trash",
            {"shop_id": "shop-a"},
            {"confirmed": True, "product_ids": ["product-a", "product-1", "product-2"]},
        )
    ]
    assert not owner_run(profile, peer, "products", "--view", "all")["products"]
    assert (
        owner_run(profile, peer, "products", "--view", "history")["products"][0][
            "purged"
        ]
        is True
    )
    queues = owner_run(profile, peer, "queues")
    assert len(queues["queues"]) == 3
    assert any(
        query.get("view") == "history"
        for _, path, query in peer.queries
        if path == "/api/admin/products"
    )
    assert "must-not-appear" not in json.dumps(result)


@pytest.mark.parametrize("count", [0, 501])
def test_empty_noop_and_large_snapshot_never_trigger_bulk_post(tmp_path, count):
    peer = PurgeOwner(count)
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, peer)
    if count:
        with pytest.raises(ManageError) as error:
            owner_run(profile, peer, "trash", "empty", "--shop", "shop-a", "--yes")
        assert error.value.code == "invalid_input"
    else:
        assert (
            owner_run(profile, peer, "trash", "empty", "--shop", "shop-a", "--yes")[
                "purged_count"
            ]
            == 0
        )
    assert not peer.purge_calls


@pytest.mark.parametrize(
    "command",
    [
        ("product", "purge", "--product", "product-a"),
        ("trash", "empty", "--shop", "shop-a"),
        (
            "api",
            "POST",
            "/api/admin/products/product-a/purge",
            "--product",
            "product-a",
        ),
        ("api", "POST", "/api/admin/products/empty-trash"),
    ],
)
def test_unconfirmed_purge_is_local_even_with_expired_owner_session(tmp_path, command):
    peer = PurgeOwner()
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, peer)
    saved = json.loads(profile.read_text())
    saved["owners"][0]["expires"] = 0
    profile.write_text(json.dumps(saved))
    before = len(peer.calls)
    with pytest.raises(ManageError) as error:
        owner_run(profile, peer, *command)
    assert error.value.code == "confirmation_required" and len(peer.calls) == before


@pytest.mark.parametrize(
    "path", ["/api/admin/products/product-a/purge", "/api/manage/product/purge"]
)
def test_owner_single_generic_purge_maps_exact_signed_resource(tmp_path, path):
    peer = PurgeOwner()
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, peer)
    result = owner_run(
        profile, peer, "api", "POST", path, "--product", "product-a", "--yes"
    )["result"]
    assert result["purged"] is True
    assert peer.purge_calls[0][0] == "/api/admin/products/product-a/purge"


@pytest.mark.parametrize("bulk", [False, True])
def test_old_nine_permissions_do_not_expand_and_single_purge_grant_is_sufficient(
    tmp_path, monkeypatch, bulk
):
    peer = PurgeManager()
    profile = tmp_path / "private" / "manager.json"
    login(peer, profile, monkeypatch, "oldnine")
    with pytest.raises(ManageError) as error:
        manage_run(peer, profile, "product", "purge", "--product", "product-a", "--yes")
    assert error.value.code == "no_scope" and not peer.purge_calls
    login(peer, profile, monkeypatch, "purge")
    argv = ("trash", "empty") if bulk else ("product", "purge")
    result = manage_run(peer, profile, *argv, "--product", "product-a", "--yes")
    assert result.get("purged") is True or result.get("purged_count") == 1
    assert peer.purge_calls[0][1] == {"product_id": "product-a"}
    assert (
        manage_run(peer, profile, "products", "--view", "history")["products"][0][
            "purged"
        ]
        is True
    )
    assert not manage_run(peer, profile, "products", "--view", "all")["products"]


def test_bulk_generic_foreign_ids_or_bad_confirmation_are_never_forwarded(
    tmp_path, monkeypatch
):
    peer = PurgeManager()
    profile = tmp_path / "private" / "manager.json"
    login(peer, profile, monkeypatch, "purge")
    source = tmp_path / "purge.json"
    for body in (
        {"confirmed": True, "product_ids": ["product-b"]},
        {"confirmed": True, "product_ids": ["product-a", "product-a"]},
        {"confirmed": 1, "product_ids": ["product-a"]},
    ):
        source.write_text(json.dumps(body))
        with pytest.raises(ManageError):
            manage_run(
                peer,
                profile,
                "api",
                "POST",
                "/api/manage/products/empty-trash",
                "--product",
                "product-a",
                "--yes",
                "--json-file",
                str(source),
            )
    assert not peer.purge_calls
    source.write_text('{"confirmed":true,"product_ids":["product-a"]}')
    assert (
        manage_run(
            peer,
            profile,
            "api",
            "POST",
            "/api/manage/products/empty-trash",
            "--product",
            "product-a",
            "--yes",
            "--json-file",
            str(source),
        )["result"]["purged_count"]
        == 1
    )


def test_out_of_shop_snapshot_or_unexpected_purge_response_fails_closed(tmp_path):
    peer = PurgeOwner()
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, peer)
    peer.rows[0]["shop_id"] = "other-shop"
    with pytest.raises(ManageError) as error:
        owner_run(profile, peer, "trash", "empty", "--shop", "shop-a", "--yes")
    assert error.value.code == "invalid_response" and not peer.purge_calls
    peer.rows[0]["shop_id"] = "shop-a"
    peer.bad_response = {
        "ok": True,
        "purged_product_ids": ["foreign"],
        "purged_count": 1,
        "preserved_fulfillment": True,
        "private_key": "never-echo",
    }
    with pytest.raises(ManageError) as error:
        owner_run(profile, peer, "trash", "empty", "--shop", "shop-a", "--yes")
    assert error.value.code == "invalid_response" and "never-echo" not in str(
        error.value
    )

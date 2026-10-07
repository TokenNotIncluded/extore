"""Both CLI interfaces use explicit folder revisions for destructive cleanup."""

import json

import httpx
import pytest
import test_manage_commands
from test_manage_commands import run as manage
from test_owner_cli import OwnerMerchant, authorize, execute

from extore.manage_client import ManageError

manager_fixture = test_manage_commands.manager


def test_real_manager_folder_preview_delete_restore_and_purge(manager_fixture):
    manager = manager_fixture
    issuance = manage(
        manager, "cards", "issue", "--count", "2", "--label", "CLI folder"
    )
    bid = issuance["batch_id"]
    result = manage(manager, "cards", "batches")
    assert result["total"] == 1 and result["items"][0]["total"] == 2
    preview = manage(manager, "cards", "batch-delete", "--batch", bid)
    assert preview["requires_confirmation"] is True and preview["revocable_count"] == 2
    assert not manage(manager, "cards", "batches", "--view", "deleted")["items"]
    result = manage(
        manager,
        "cards",
        "batch-delete",
        "--batch",
        bid,
        "--revision",
        preview["revision"],
        "--yes",
    )
    assert result["revoked"] == 2
    assert not manage(manager, "cards", "batches")["items"]
    assert manage(manager, "cards", "batch-restore", "--batch", bid, "--yes")["ok"]
    preview = manage(manager, "cards", "batch-delete", "--batch", bid)
    manage(
        manager,
        "cards",
        "batch-delete",
        "--batch",
        bid,
        "--revision",
        preview["revision"],
        "--yes",
    )
    plan = manage(manager, "cards", "batch-purge", "--batch", bid)
    assert plan["delete_count"] == 2 and plan["retain_count"] == 0
    assert (
        manage(
            manager,
            "cards",
            "batch-purge",
            "--batch",
            bid,
            "--revision",
            plan["revision"],
            "--yes",
        )["deleted_cards"]
        == 2
    )
    assert not manage(manager, "cards", "batches", "--view", "deleted")["items"]


@pytest.mark.parametrize(
    "operation,arguments",
    [
        ("batch-delete", ["--yes"]),
        ("batch-purge", ["--yes", "--revision", "wrong"]),
        ("batch-restore", []),
    ],
)
def test_unconfirmed_or_unreviewed_manager_actions_make_no_request(
    manager_fixture, operation, arguments
):
    manager = manager_fixture
    calls = len(manager["calls"])
    with pytest.raises(ManageError):
        manage(manager, "cards", operation, "--batch", "batch-a", *arguments)
    assert len(manager["calls"]) == calls


class BatchOwner(OwnerMerchant):
    def __call__(self, request):
        # Super verifies actual one-use action challenges, raw query/body
        # hashes and Ed25519 signatures before any synthetic batch response.
        response = super().__call__(request)
        if "/card-batches" not in request.url.path or response.status_code != 200:
            return response
        assert request.url.params["product_id"] == "product-a"
        if request.url.path.endswith("/delete-preview"):
            return httpx.Response(
                200, json={"revision": "a" * 64, "revocable_count": 2}
            )
        if request.method == "DELETE":
            assert json.loads(request.read()) == {
                "revision": "a" * 64,
                "confirmed": True,
            }
            return httpx.Response(200, json={"ok": True, "revoked": 2})
        return httpx.Response(
            200, json={"items": [{"id": "batch-a", "total": 2}], "total": 1}
        )


def test_owner_batch_write_signs_exact_revision_query_and_is_not_replayed(tmp_path):
    profile = tmp_path / "private" / "owner.json"
    merchant = BatchOwner()
    authorize(profile, merchant)
    listing = execute(profile, merchant, "cards", "batches", "--product", "product-a")
    assert listing["total"] == 1
    plan = execute(
        profile,
        merchant,
        "cards",
        "batch-delete",
        "--product",
        "product-a",
        "--batch",
        "batch-a",
    )
    assert plan["revision"] == "a" * 64
    assert (
        execute(
            profile,
            merchant,
            "cards",
            "batch-delete",
            "--product",
            "product-a",
            "--batch",
            "batch-a",
            "--revision",
            plan["revision"],
            "--yes",
        )["revoked"]
        == 2
    )
    merchant.reject_write = True
    prior = len(merchant.calls)
    with pytest.raises(ManageError):
        execute(
            profile,
            merchant,
            "cards",
            "batch-delete",
            "--product",
            "product-a",
            "--batch",
            "batch-a",
            "--revision",
            plan["revision"],
            "--yes",
        )
    writes = [
        call
        for call in merchant.calls[prior:]
        if call[0] == "DELETE" and "/card-batches/" in call[1]
    ]
    assert len(writes) == 1

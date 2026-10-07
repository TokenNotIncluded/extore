"""A board reads independently approved progress scopes without task content."""

import copy
import json

import httpx
import pytest
from test_manage_cli import MockMerchant, arguments, login, run
from test_owner_cli import OwnerMerchant, authorize
from test_owner_cli import execute as admin

from extore.manage_client import ManageError
from extore.manage_commands import BOARD_STATES, progress_board


def board(pids=("product-a",), *, shop="shop-a", view="active", limit=100, offset=0):
    state = "processing" if view == "active" else "succeeded"
    counts = {key: len(pids) if key == state else 0 for key in BOARD_STATES}
    products = []
    for index, pid in enumerate(pids):
        products.append(
            {
                "id": pid,
                "name": "Product",
                "mode": "manual",
                "counts": {key: int(key == state) for key in BOARD_STATES},
                "jobs": [
                    {
                        "id": "job-" + pid,
                        "state": state,
                        "progress": 33,
                        "attempt": 1,
                        "created": 1700000000,
                        "updated": 1700000001,
                        "queue_position": None,
                        "worker_id": "a" * 64,
                        "step_count": 3,
                        "completed_step_count": 1,
                        "steps": [
                            {"position": 1, "state": "done"},
                            {"position": 2, "state": "current"},
                            {"position": 3, "state": "pending"},
                        ],
                        "flow_phase": None,
                    }
                ]
                if offset <= index < offset + limit
                else [],
            }
        )
    return {
        "schema": "extore.progress-board.v1",
        "generated_at": 1700000002,
        "shop": {"id": shop, "name": "Shop"},
        "totals": counts,
        "products": products,
        "workers": [
            {
                "id": "a" * 64,
                "name": "Worker",
                "kind": "unknown",
                "active_jobs": len(pids) if view == "active" else 0,
                "completed_jobs": len(pids) if view == "processed" else 0,
                "last_update": 1700000001,
            }
        ],
        "pagination": {
            "limit": limit,
            "offset": offset,
            "total": len(pids),
            "has_more": offset + limit < len(pids),
        },
        "scope": {"product_ids": list(pids)},
    }


class BoardMerchant(MockMerchant):
    def __init__(self):
        super().__init__()
        self.links.update(
            {
                "monitor-a": ("product-a", ["queue.monitor"]),
                "monitor-b": ("product-b", ["queue.monitor"]),
                "viewer": ("product-c", ["queue.view"]),
                "editor": ("product-d", ["product.edit"]),
            }
        )
        self.reads = []
        self.invalid = None

    def __call__(self, request):
        if request.url.path == "/api/manage/progress-board":
            self.calls.append(
                (
                    request.method,
                    request.url.path,
                    request.headers.get("Authorization"),
                    request.read(),
                )
            )
            token = request.headers["authorization"].removeprefix("Bearer ")
            device = self.sessions[token]
            params = dict(request.url.params)
            assert params["product_id"] == device["product_id"]
            assert set(device["permissions"]) & {"queue.view", "queue.monitor"}
            self.reads.append((device["device_id"], params))
            data = board(
                (device["product_id"],),
                view=params["view"],
                limit=int(params["limit"]),
                offset=int(params["offset"]),
            )
            if self.invalid and self.invalid[0] == device["product_id"]:
                data["products"][0]["jobs"][0]["params"] = {"private": self.invalid[1]}
            return httpx.Response(200, json=data)
        return super().__call__(request)


class BoardOwner(OwnerMerchant):
    def __init__(self, shop=None):
        super().__init__()
        self.shop = shop
        self.reads = []
        self.corrupt = False

    def __call__(self, request):
        response = super().__call__(request)
        data = response.json()
        if "shop_id" in data:
            data.update(shop_id=self.shop, superadmin=self.shop is None)
            response = httpx.Response(response.status_code, json=data)
        if request.url.path == "/api/manage/progress-board":
            params = dict(request.url.params)
            self.reads.append(params)
            data = board(
                (params["product_id"],)
                if "product_id" in params
                else ("product-a", "product-b"),
                shop=params["shop_id"],
                view=params["view"],
                limit=int(params["limit"]),
                offset=int(params["offset"]),
            )
            if self.corrupt:
                data["shop"]["id"] = "different-shop"
            return httpx.Response(200, json=data)
        return response


def test_monitor_aggregates_independent_grants_and_never_reads_task_bodies(
    tmp_path, monkeypatch
):
    peer = BoardMerchant()
    profile = tmp_path / "private" / "cli.json"
    for name in ("monitor-a", "monitor-b", "viewer", "editor"):
        login(peer, profile, monkeypatch, name)
    before = len(peer.calls)
    result = run(peer, profile, "board", "--limit", "1", "--offset", "0")
    assert result["ok"] and len(result["boards"]) == 3
    assert {item["product_id"] for item in result["boards"]} == {
        "product-a",
        "product-b",
        "product-c",
    }
    assert len({device for device, _ in peer.reads}) == 3
    assert all(
        path in ("/api/cli/status", "/api/manage/progress-board")
        for _, path, _, _ in peer.calls[before:]
    )
    assert not peer.queue_queries
    serialized = json.dumps(result)
    assert "sensitive task input" not in serialized and "params" not in serialized
    selected = run(
        peer, profile, "board", "--product", "product-b", "--view", "processed"
    )
    assert (
        len(selected["boards"]) == 1
        and selected["boards"][0]["board"]["totals"]["succeeded"] == 1
    )


def test_fresh_monitor_permission_cannot_read_jobs_files_or_process(
    tmp_path, monkeypatch
):
    peer = BoardMerchant()
    profile = tmp_path / "private" / "cli.json"
    login(peer, profile, monkeypatch, "monitor-a")
    for command in (
        ("jobs", "--product", "product-a"),
        ("files", "job-a", "--product", "product-a"),
        ("claim", "job-a", "--product", "product-a"),
    ):
        with pytest.raises(ManageError) as error:
            run(peer, profile, *command)
        assert error.value.code == "no_scope"
    for device in peer.devices.values():
        device["permissions"] = ["product.edit"]
    with pytest.raises(ManageError) as error:
        run(peer, profile, "board")
    assert error.value.code == "no_scope" and not peer.reads


def test_bad_board_is_rejected_without_echoing_unknown_task_text(tmp_path, monkeypatch):
    peer = BoardMerchant()
    profile = tmp_path / "private" / "cli.json"
    for name in ("monitor-a", "monitor-b"):
        login(peer, profile, monkeypatch, name)
    peer.invalid = ("product-a", "NEVER-DISCLOSE")
    result = run(peer, profile, "board")
    assert not result["ok"] and len(result["boards"]) == 1
    assert result["errors"][0]["code"] == "invalid_response"
    assert "NEVER-DISCLOSE" not in json.dumps(result)


@pytest.mark.parametrize("shop", (None, "shop-a"))
def test_owner_board_and_generic_api_require_one_fresh_shop_scope(tmp_path, shop):
    peer = BoardOwner(shop)
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, peer)
    saved = json.loads(profile.read_text())
    saved["owners"][0]["expires"] = 0
    profile.write_text(json.dumps(saved))
    before = len(peer.calls)
    if shop is None:
        with pytest.raises(ManageError) as error:
            admin(profile, peer, "board")
        assert error.value.code == "no_scope" and len(peer.calls) == before
        with pytest.raises(ManageError):
            admin(profile, peer, "api", "GET", "/api/manage/progress-board")
        assert len(peer.calls) == before
    else:
        with pytest.raises(ManageError):
            admin(profile, peer, "board", "--shop", "other-shop")
        assert len(peer.calls) == before
    filters = ("--shop", "shop-a") if shop is None else ()
    result = admin(profile, peer, "board", *filters, "--limit", "1")
    assert result["board"]["pagination"]["has_more"] is True
    assert peer.reads[-1]["shop_id"] == "shop-a"
    query = ("--query", "shop_id=shop-a") if shop is None else ()
    assert (
        admin(profile, peer, "api", "GET", "/api/manage/progress-board", *query)[
            "result"
        ]["shop"]["id"]
        == "shop-a"
    )
    peer.corrupt = True
    with pytest.raises(ManageError) as error:
        admin(profile, peer, "board", *filters)
    assert error.value.code == "invalid_response"


def test_generic_monitor_api_returns_only_the_selected_board(tmp_path, monkeypatch):
    peer = BoardMerchant()
    profile = tmp_path / "private" / "cli.json"
    login(peer, profile, monkeypatch, "monitor-a")
    result = run(
        peer,
        profile,
        "api",
        "GET",
        "/api/manage/progress-board",
        "--product",
        "product-a",
    )
    assert result["result"]["scope"]["product_ids"] == ["product-a"]
    before = len(peer.reads)
    with pytest.raises(ManageError):
        run(
            peer,
            profile,
            "api",
            "GET",
            "/api/manage/progress-board",
            "--product",
            "product-a",
            "--query",
            "message=private",
        )
    assert len(peer.reads) == before


@pytest.mark.parametrize(
    "path",
    (
        (),
        ("shop",),
        ("workers", 0),
        ("products", 0),
        ("products", 0, "jobs", 0),
        ("products", 0, "jobs", 0, "steps", 0),
    ),
)
def test_unknown_fields_at_every_board_level_fail_closed(path):
    data = board()
    target = data
    for key in path:
        target = target[key]
    target["message"] = "SECRET-CUSTOMER-TEXT"
    with pytest.raises(ManageError) as error:
        progress_board(data, product="product-a")
    assert error.value.code == "invalid_response" and "SECRET" not in str(error.value)


@pytest.mark.parametrize(
    "change",
    (
        lambda data: data["pagination"].update(total=True),
        lambda data: data["products"][0]["jobs"][0].update(progress=float("nan")),
        lambda data: data["products"][0]["jobs"][0].update(worker_id={}),
        lambda data: data["products"][0]["jobs"][0].update(
            flow_phase="private-node-name"
        ),
        lambda data: data["scope"].update(product_ids=["product-b"]),
        lambda data: data["workers"][0].update(id="raw-device-id"),
    ),
)
def test_malformed_or_cross_scope_board_rejects_without_raw_data(change):
    data = copy.deepcopy(board())
    change(data)
    with pytest.raises(ManageError):
        progress_board(data, product="product-a")


def test_board_parser_limits_and_optional_monitor_pipeline_permission():
    parsed = arguments(
        "login",
        "--device-code",
        "--origin",
        "https://merchant.example",
        "--shop",
        "shop-a",
        "--pipelines-all",
        "--permissions",
        "queue.monitor",
        "--no-wait",
    )
    assert parsed.permissions == ["queue.monitor"]
    for flags in (
        ("--limit", "0"),
        ("--limit", "201"),
        ("--offset", "1000001"),
        ("--view", "all"),
    ):
        with pytest.raises(SystemExit):
            arguments("board", *flags)

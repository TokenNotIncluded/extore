"""AI work briefs remain scoped and separate from customer input and credentials."""

import copy
import io
import json
import sys
import time
from dataclasses import FrozenInstanceError

import httpx
import pytest
from test_automation_manage_cli import AutomationMerchant
from test_manage_cli import login, run
from test_owner_cli import OwnerMerchant, authorize
from test_owner_cli import execute as admin
from test_private_worker_sdk import ORIGIN as WORKER_ORIGIN
from test_private_worker_sdk import SCOPE, SECRET, headers
from test_progress_board_cli import board

from extore.manage_client import ManageError
from extore.manage_commands import progress_board
from extore.sdk import Result, Task, WorkInstructions
from extore.sdk.client import verify_flow_event
from extore.sdk.script import run as run_script


def instructions(
    product="product-a",
    shop="shop-a",
    *,
    factory="先确认范围",
    workshop="交付可编辑文档",
):
    return {
        "schema": "extore.work-instructions.v1",
        "shop_id": shop,
        "product_id": product,
        "factory_slogan": factory,
        "workshop_slogan": workshop,
        "revision": "a" * 64,
    }


class InstructionsMerchant(AutomationMerchant):
    def __init__(self):
        super().__init__()
        self.links["monitor"] = ("product-d", ["queue.monitor"])
        self.current_factory = "先确认范围"
        self.current_workshop = "交付可编辑文档"
        self.corrupt = None
        self.instruction_reads = []

    def __call__(self, request):
        if request.url.path == "/api/manage/instructions":
            token = request.headers["authorization"].removeprefix("Bearer ")
            device = self.sessions[token]
            assert not request.headers.get("cookie")
            assert set(device["permissions"]) & {"queue.view", "queue.monitor"}
            assert dict(request.url.params) == {"product_id": device["product_id"]}
            self.instruction_reads.append(device["device_id"])
            value = instructions(
                device["product_id"],
                factory=self.current_factory,
                workshop=self.current_workshop,
            )
            if self.corrupt:
                self.corrupt(value)
            return httpx.Response(200, json=value)
        response = super().__call__(request)
        if request.url.path == "/api/manage/next":
            value = response.json()
            for item in value["items"]:
                item["instructions"] = instructions(
                    item["product_id"],
                    factory=self.current_factory,
                    workshop=self.current_workshop,
                )
                item["execution"]["params"]["instructions"] = "customer-spoof"
                if self.corrupt:
                    self.corrupt(item["instructions"])
            return httpx.Response(200, json=value)
        return response


def test_next_returns_current_brief_without_additional_requests(tmp_path, monkeypatch):
    peer = InstructionsMerchant()
    profile = tmp_path / "private" / "cli.json"
    login(peer, profile, monkeypatch)
    result = run(peer, profile, "next", "--product", "product-a", "--wait", "0")
    item = result["items"][0]
    assert item["instructions"] == instructions()
    assert item["execution"]["params"]["instructions"] == "customer-spoof"
    assert not peer.instruction_reads
    assert not peer.queue_queries
    assert not any(call[1] == "/api/manage/batch" for call in peer.calls)
    peer.current_factory = "先校验附件，确认后再制作"
    peer.current_workshop = "不得编造参考来源"
    result = run(peer, profile, "next", "--product", "product-a", "--wait", "0")
    assert result["items"][0]["instructions"]["factory_slogan"] == peer.current_factory
    assert (
        result["items"][0]["instructions"]["workshop_slogan"] == peer.current_workshop
    )
    assert "先确认范围" not in profile.read_text()
    assert not peer.instruction_reads


def test_next_invalid_brief_keeps_claim_recovery_receipt(tmp_path, monkeypatch):
    peer = InstructionsMerchant()
    profile = tmp_path / "private" / "cli.json"
    login(peer, profile, monkeypatch)
    peer.corrupt = lambda value: value.update(product_id="unapproved-product")
    with pytest.raises(ManageError) as error:
        run(peer, profile, "next", "--product", "product-a", "--wait", "0")
    assert error.value.code == "invalid_response"
    saved = json.loads(profile.read_text())["automation_next_requests"]
    assert len(saved) == 1
    peer.corrupt = None
    recovered = run(peer, profile, "next", "--product", "product-a", "--wait", "0")
    assert recovered["replayed"]
    assert recovered["request_id"] == saved[0]["request_id"]
    assert not json.loads(profile.read_text())["automation_next_requests"]


def test_explicit_read_rechecks_each_scope_and_does_not_read_tasks(
    tmp_path, monkeypatch
):
    peer = InstructionsMerchant()
    profile = tmp_path / "private" / "cli.json"
    for link in ("first", "second", "monitor"):
        login(peer, profile, monkeypatch, link)
    selected = run(peer, profile, "instructions", "--product", "product-b")
    assert selected["instructions"] == instructions("product-b")
    assert selected["grant_id"] == peer.instruction_reads[-1]
    assert not peer.queue_queries and not peer.next_requests
    run(peer, profile, "instructions", "--product", "product-d")
    assert len(peer.instruction_reads) == 2
    for device in peer.devices.values():
        if device["product_id"] == "product-b":
            device["permissions"] = ["product.edit"]
    with pytest.raises(ManageError) as error:
        run(peer, profile, "instructions", "--product", "product-b")
    assert error.value.code == "no_scope"
    assert len(peer.instruction_reads) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"product_id": "unapproved-product"},
        {"factory_slogan": None},
        {"workshop_slogan": "x" * 4001},
        {"factory_slogan": "private-malformed-slogan\x1b[31m"},
        {"schema": "forged-schema"},
        {"revision": "not-a-digest"},
        {"unknown": "private-malformed-slogan"},
    ],
)
def test_malformed_read_does_not_echo_work_brief(tmp_path, monkeypatch, change):
    peer = InstructionsMerchant()
    profile = tmp_path / "private" / "cli.json"
    login(peer, profile, monkeypatch)
    peer.corrupt = lambda value: value.update(change)
    with pytest.raises(ManageError) as error:
        run(peer, profile, "instructions", "--product", "product-a")
    assert error.value.code == "invalid_response"
    assert "private-malformed-slogan" not in str(error.value)


def test_sdk_work_brief_is_frozen_and_customer_cannot_override_it():
    value = instructions()
    task = Task(
        "job-a",
        "product-a",
        1,
        {"factory_slogan": "customer-spoof", "instructions": "customer-spoof"},
        shop_context={"shop_id": "shop-a"},
        instructions=value,
    )
    value["factory_slogan"] = "changed-after-construction"
    assert task.instructions.factory_slogan == "先确认范围"
    assert task.params["factory_slogan"] == "customer-spoof"
    with pytest.raises(FrozenInstanceError):
        task.instructions.factory_slogan = "changed"
    with pytest.raises(FrozenInstanceError):
        task.instructions = None
    assert "先确认范围" not in repr(task)
    assert Task("job-a", "product-a", 1, {}).instructions is None
    with pytest.raises(ValueError, match="scope"):
        Task("job-a", "product-b", 1, {}, instructions=instructions())
    with pytest.raises(ValueError, match="scope"):
        Task(
            "job-a",
            "product-a",
            1,
            {},
            shop_context={"shop_id": "shop-b"},
            instructions=instructions(),
        )


def test_sdk_json_stdin_preserves_separate_instruction_source(monkeypatch, capsys):
    payload = {
        "id": "job-a",
        "product_id": "product-a",
        "attempt": 1,
        "params": {"instructions": instructions(factory="customer-spoof")},
        "instructions": instructions(),
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    seen = []

    def process(task):
        seen.append(task.instructions.factory_slogan)
        return Result.success(output={"content": "real-result"})

    run_script(process)
    assert seen == ["先确认范围"]
    assert json.loads(capsys.readouterr().out)["state"] == "succeeded"


def test_sdk_accepts_markdown_unicode_but_rejects_control_text_and_wrong_scope():
    text = "## 工作约定\n\t中文与 emoji 🌱，空行允许。\r\n"
    value = WorkInstructions.from_dict(instructions(factory=text))
    assert value.factory_slogan == text
    assert WorkInstructions.from_dict(instructions(factory="中" * 4000))
    for invalid in ("\x00", "\x7f", "\ud800", "中" * 4001):
        with pytest.raises(ValueError):
            WorkInstructions.from_dict(instructions(factory=invalid))
    with pytest.raises(ValueError, match="scope"):
        WorkInstructions.from_dict(instructions(), shop_id="another-shop")


def test_signed_worker_brief_is_bound_to_execution_and_cannot_be_modified():
    dispatch = {
        "version": 2,
        "type": "flow.process.requested",
        "scope": SCOPE.__dict__,
        "deadline": time.time() + 60,
        "instructions": instructions(),
    }
    body = json.dumps(dispatch).encode()
    signature = headers(body)
    result = verify_flow_event(
        SECRET, body, signature, audience=WORKER_ORIGIN, path="/process"
    )
    assert result["instructions"]["factory_slogan"] == "先确认范围"
    dispatch["instructions"]["shop_id"] = "another-shop"
    changed = json.dumps(dispatch).encode()
    with pytest.raises(ValueError):
        verify_flow_event(
            SECRET, changed, signature, audience=WORKER_ORIGIN, path="/process"
        )
    with pytest.raises(ValueError):
        verify_flow_event(
            SECRET, changed, headers(changed), audience=WORKER_ORIGIN, path="/process"
        )


def test_board_exposes_only_intended_brief_fields_and_keeps_old_servers_compatible():
    original = board()
    assert progress_board(copy.deepcopy(original))
    value = copy.deepcopy(original)
    value["shop"]["factory_slogan"] = "真实汇报进度"
    value["products"][0]["workshop_slogan"] = "保持文档可编辑"
    result = progress_board(value)
    assert result["shop"]["factory_slogan"] == "真实汇报进度"
    assert "factory_slogan" not in original["shop"]
    for section, update in (
        (value["shop"], {"unknown": "secret-not-in-error"}),
        (value["products"][0], {"workshop_slogan": "secret-not-in-error\x00"}),
    ):
        section.update(update)
        with pytest.raises(ManageError) as error:
            progress_board(value)
        assert "secret-not-in-error" not in str(error.value)
        for key in update:
            section.pop(key)


class FactoryOwner(OwnerMerchant):
    def __init__(self, *, shop=None):
        super().__init__()
        self.shop = shop
        self.slogans = {}
        self.factory_reads = []
        self.corrupt = False

    def __call__(self, request):
        # The common peer verifies real owner signatures, including the query,
        # before this fixture returns a factory-specific response.
        response = super().__call__(request)
        data = response.json()
        if isinstance(data, dict) and "shop_id" in data:
            data.update(shop_id=self.shop, superadmin=self.shop is None)
            response = httpx.Response(response.status_code, json=data)
        if request.url.path == "/api/admin/factory":
            selected = request.url.params["shop_id"]
            assert self.shop is None or selected == self.shop
            self.factory_reads.append((request.method, selected))
            if request.method == "PUT":
                value = json.loads(request.read())
                assert set(value) == {"factory_slogan"}
                self.slogans[selected] = value["factory_slogan"]
            return httpx.Response(
                200,
                json={
                    "shop_id": "other-shop" if self.corrupt else selected,
                    "shop_name": "Example shop",
                    "factory_slogan": self.slogans.get(selected, ""),
                },
            )
        return response


def test_merchant_factory_update_is_device_signed_and_tenant_fixed(tmp_path):
    peer = FactoryOwner(shop="shop-a")
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, peer)
    patch = tmp_path / "factory.json"
    patch.write_text(json.dumps({"factory_slogan": "先读车间提示词"}))
    updated = admin(profile, peer, "factory", "update", "--json-file", str(patch))
    assert updated["factory"]["shop_id"] == "shop-a"
    assert peer.slogans == {"shop-a": "先读车间提示词"}
    assert (
        admin(profile, peer, "factory", "get")["factory"]["factory_slogan"]
        == "先读车间提示词"
    )
    assert any(path.endswith("/action-challenge") for _, path, _, _ in peer.calls)
    before = len(peer.calls)
    with pytest.raises(ManageError) as error:
        admin(profile, peer, "factory", "get", "--shop", "shop-b")
    assert error.value.code == "no_scope" and len(peer.calls) == before


def test_platform_factory_requires_one_explicit_shop_and_checks_response(tmp_path):
    peer = FactoryOwner()
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, peer)
    before = len(peer.calls)
    with pytest.raises(ManageError) as error:
        admin(profile, peer, "factory", "get")
    assert error.value.code == "no_scope" and len(peer.calls) == before
    assert (
        admin(profile, peer, "factory", "get", "--shop", "shop-a")["factory"]["shop_id"]
        == "shop-a"
    )
    peer.corrupt = True
    with pytest.raises(ManageError) as error:
        admin(profile, peer, "factory", "get", "--shop", "shop-a")
    assert error.value.code == "invalid_response"


def test_invalid_factory_patch_does_not_send_write_or_echo_text(tmp_path):
    peer = FactoryOwner(shop="shop-a")
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, peer)
    patch = tmp_path / "factory.json"
    patch.write_text(json.dumps({"factory_slogan": "private-bad-text\x00"}))
    before = len(peer.calls)
    with pytest.raises(ManageError) as error:
        admin(profile, peer, "factory", "update", "--json-file", str(patch))
    assert error.value.code == "invalid_input"
    assert "private-bad-text" not in str(error.value)
    assert len(peer.calls) == before and not peer.slogans

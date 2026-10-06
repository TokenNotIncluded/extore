"""Automation CLI signs atomic claims and keeps their recovery receipts private."""

import copy
import io
import json
import re
import stat
import sys
import time
from email import policy
from email.parser import BytesParser

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi.testclient import TestClient
from test_manage_cli import ORIGIN, DeviceClock, MockMerchant, arguments, login, run

from extore import manage_client as remote
from extore.app import app
from extore.db import db


class AutomationMerchant(MockMerchant):
    def __init__(self):
        super().__init__()
        self.next_requests = []
        self.receipts = {}
        self.drop_responses = 0
        self.unauthorized_once = False
        self.interrupt_once = False
        self.tamper_response = None
        self.item_index = 0
        self.idle = False
        self.next_idles = []
        self.job_outputs = None
        self.flow_guard = None
        self.delivery_uploads = []
        self.tamper_upload = None

    def __call__(self, request):
        if (
            request.url.path in ("/api/manage/jobs", "/api/manage/files/upload")
            and self.job_outputs is not None
        ):
            raw = request.read()
            self.calls.append(
                (
                    request.method,
                    request.url.path,
                    request.headers.get("Authorization"),
                    raw,
                )
            )
            token = request.headers.get("Authorization", "").removeprefix("Bearer ")
            device = self.sessions[token]
            if request.url.path == "/api/manage/jobs":
                job = {
                    "id": request.url.params["job_id"],
                    "product_id": device["product_id"],
                    "state": "processing",
                    "attempt": 1,
                    "params": {},
                    "parameters": [],
                    "outputs": copy.deepcopy(self.job_outputs),
                }
                if self.flow_guard is not None:
                    epoch, action = self.flow_guard
                    job.update(
                        flow_epoch=epoch,
                        action_id=action,
                        task_flow={"current": {"id": "node-current", "revision": 3}},
                    )
                return httpx.Response(200, json=[job])
            message = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: "
                + request.headers["Content-Type"].encode()
                + b"\r\nMIME-Version: 1.0\r\n\r\n"
                + raw
            )
            fields = {}
            content = None
            filename = None
            for part in message.iter_parts():
                name = part.get_param("name", header="Content-Disposition")
                if name == "file":
                    content = part.get_payload(decode=True)
                    filename = part.get_filename()
                else:
                    fields[name] = part.get_payload(decode=True).decode()
            self.delivery_uploads.append(
                {"fields": fields, "content": content, "filename": filename}
            )
            file_id = f"00000000-0000-4000-8000-{len(self.delivery_uploads):012d}"
            uploaded = {
                "id": file_id,
                "job_id": fields["job_id"],
                "field_key": fields["field_key"],
                "size": len(content),
            }
            if self.tamper_upload:
                self.tamper_upload(uploaded)
            return httpx.Response(200, json=uploaded)
        if request.url.path != "/api/manage/next":
            return super().__call__(request)
        raw = request.read()
        self.calls.append(
            (
                request.method,
                request.url.path,
                request.headers.get("Authorization"),
                raw,
            )
        )
        payload = json.loads(raw)
        self.next_requests.append(
            {
                "payload": payload,
                "raw": raw,
                "authorization": request.headers.get("Authorization"),
            }
        )
        assert request.method == "POST"
        assert request.headers.get("Cookie") is None
        assert set(payload) == {
            "request_id",
            "issued_at",
            "wait_seconds",
            "limit",
            "grants",
        }
        assert re.fullmatch(r"[A-Za-z0-9_-]{16,100}", payload["request_id"])
        assert type(payload["issued_at"]) is int
        assert (
            type(payload["wait_seconds"]) is int and 0 <= payload["wait_seconds"] <= 25
        )
        assert type(payload["limit"]) is int and 1 <= payload["limit"] <= 10
        pairs = [
            {"device_id": grant["device_id"], "product_id": grant["product_id"]}
            for grant in payload["grants"]
        ]
        assert 1 <= len(pairs) <= 500
        assert len({(pair["device_id"], pair["product_id"]) for pair in pairs}) == len(
            pairs
        )
        assert len({pair["device_id"] for pair in pairs}) == len(pairs)
        assert len({pair["product_id"] for pair in pairs}) == len(pairs)
        canonical = json.dumps(
            {
                "request_id": payload["request_id"],
                "issued_at": payload["issued_at"],
                "wait_seconds": payload["wait_seconds"],
                "limit": payload["limit"],
                "grants": sorted(
                    pairs, key=lambda pair: (pair["device_id"], pair["product_id"])
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        origin = str(request.url.copy_with(path="", query=None)).rstrip("/")
        for grant in payload["grants"]:
            assert set(grant) == {"device_id", "product_id", "signature"}
            device = self.devices[grant["device_id"]]
            assert grant["product_id"] == device["product_id"]
            proof = "\n".join(
                (
                    "extore-automation-next-v1",
                    origin,
                    "POST",
                    "/api/manage/next",
                    grant["device_id"],
                    canonical,
                )
            )
            Ed25519PublicKey.from_public_bytes(
                remote._unb64(device["public_key"])
            ).verify(remote._unb64(grant["signature"]), proof.encode())
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        primary = self.sessions.get(token)
        assert primary is not None
        assert {
            "device_id": primary["device_id"],
            "product_id": primary["product_id"],
        } in pairs
        if self.unauthorized_once:
            self.unauthorized_once = False
            return httpx.Response(401)
        request_id = payload["request_id"]
        replayed = request_id in self.receipts
        if replayed:
            assert self.receipts[request_id]["raw"] == raw
            response = copy.deepcopy(self.receipts[request_id]["response"])
        else:
            pair = pairs[self.item_index]
            idle = self.next_idles.pop(0) if self.next_idles else self.idle
            job_id = "job-" + pair["product_id"]
            params = {"request": "current customer demand"}
            parameters = [{"key": "request", "type": "text"}]
            outputs = [{"key": "result", "type": "text"}]
            response = {
                "request_id": request_id,
                "idle": idle,
                "replayed": False,
                "items": []
                if idle
                else [
                    {
                        **pair,
                        "job": {
                            "id": job_id,
                            "product_id": pair["product_id"],
                            "state": "processing",
                            "attempt": 1,
                        },
                        "execution": {
                            "params": params,
                            "parameters": parameters,
                            "outputs": outputs,
                            "flow_epoch": 1,
                            "action_id": "action-a",
                            "deadline": time.time() + 3600,
                        },
                    }
                ],
            }
            self.receipts[request_id] = {
                "raw": raw,
                "response": copy.deepcopy(response),
            }
        response["replayed"] = replayed
        if self.interrupt_once:
            self.interrupt_once = False
            raise KeyboardInterrupt
        if self.drop_responses:
            self.drop_responses -= 1
            raise httpx.ReadError("synthetic lost claim response", request=request)
        if self.tamper_response:
            self.tamper_response(response)
        return httpx.Response(200, json=response)


@pytest.fixture
def merchant():
    return AutomationMerchant()


@pytest.fixture
def profile(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    return directory / "cli.json"


def stored_grants(profile):
    return json.loads(profile.read_text())["grants"]


def assert_atomic_next_only(merchant):
    assert not any(
        call[1] in ("/api/manage/jobs", "/api/manage/batch") for call in merchant.calls
    )


def test_next_claims_one_product_with_one_signed_post_and_current_execution(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    saved = stored_grants(profile)[0]
    result = run(merchant, profile, "next", "--product", "product-a")
    assert result["ok"] is True and result["origin"] == ORIGIN
    assert result["idle"] is False and result["replayed"] is False
    assert len(merchant.next_requests) == 1
    request = merchant.next_requests[0]
    assert request["authorization"] == "Bearer " + saved["access_token"]
    assert request["payload"]["wait_seconds"] == 25
    assert request["payload"]["limit"] == 1
    item = result["items"][0]
    assert item["grant_id"] == saved["id"]
    assert item["product_id"] == "product-a" and item["device_id"] == saved["device_id"]
    assert (
        item["execution"]
        == merchant.receipts[result["request_id"]]["response"]["items"][0]["execution"]
    )
    output = json.dumps(result)
    assert saved["private_key"] not in output and saved["access_token"] not in output
    assert "signature" not in output and "history" not in output
    assert_atomic_next_only(merchant)


def test_next_all_signs_each_grant_and_waits_once_for_multiple_products(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    login(merchant, profile, monkeypatch, "second")
    run(merchant, profile, "next", "--all", "--wait", "0", "--limit", "10")
    assert len(merchant.next_requests) == 1
    payload = merchant.next_requests[0]["payload"]
    assert payload["wait_seconds"] == 0 and payload["limit"] == 10
    assert {
        (grant["device_id"], grant["product_id"]) for grant in payload["grants"]
    } == {(grant["device_id"], grant["product_id"]) for grant in stored_grants(profile)}
    assert_atomic_next_only(merchant)


def test_next_grant_selects_one_product_without_product_flag(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    login(merchant, profile, monkeypatch, "second")
    saved = stored_grants(profile)[1]
    result = run(merchant, profile, "next", "--grant", saved["id"], "--wait", "0")
    assert [(item["product_id"], item["grant_id"]) for item in result["items"]] == [
        ("product-b", saved["id"])
    ]
    assert [
        (grant["device_id"], grant["product_id"])
        for grant in merchant.next_requests[0]["payload"]["grants"]
    ] == [(saved["device_id"], "product-b")]


def test_next_product_selects_one_complete_grant_deterministically(
    merchant, profile, monkeypatch
):
    merchant.links["another"] = ("product-a", ["queue.view", "queue.process"])
    login(merchant, profile, monkeypatch)
    login(merchant, profile, monkeypatch, "another")
    result = run(merchant, profile, "next", "--product", "product-a", "--wait", "0")
    grants = stored_grants(profile)
    assert len(merchant.next_requests[0]["payload"]["grants"]) == 1
    item = result["items"][0]
    assert item["grant_id"] == grants[0]["id"]
    assert item["device_id"] == grants[0]["device_id"]


def test_next_never_unions_view_and_process_grants_for_one_product(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch, "view")
    login(merchant, profile, monkeypatch, "process")
    with pytest.raises(remote.ManageError):
        run(merchant, profile, "next", "--product", "product-c", "--wait", "0")
    assert not merchant.next_requests


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--wait", "-1"),
        ("--wait", "26"),
        ("--wait", "1.5"),
        ("--limit", "0"),
        ("--limit", "11"),
        ("--limit", "1.5"),
    ],
)
def test_next_rejects_unbounded_arguments_before_network(
    flag, value, merchant, profile
):
    with pytest.raises((SystemExit, remote.ManageError)):
        run(merchant, profile, "next", "--all", flag, value)
    assert merchant.calls == []


@pytest.mark.parametrize(
    "selector", [[], ["--origin", ORIGIN], ["--all", "--product", "product-a"]]
)
def test_next_requires_an_explicit_unambiguous_selector(
    selector, merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    with pytest.raises((SystemExit, remote.ManageError)):
        run(merchant, profile, "next", *selector, "--wait", "0")
    assert not merchant.next_requests


def test_next_all_cannot_also_select_an_individual_grant(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    grant_id = stored_grants(profile)[0]["id"]
    with pytest.raises((SystemExit, remote.ManageError)):
        run(merchant, profile, "next", "--all", "--grant", grant_id, "--wait", "0")
    assert not merchant.next_requests


def test_next_product_and_grant_must_match(merchant, profile, monkeypatch):
    login(merchant, profile, monkeypatch)
    grant_id = stored_grants(profile)[0]["id"]
    with pytest.raises(remote.ManageError):
        run(
            merchant,
            profile,
            "next",
            "--product",
            "product-b",
            "--grant",
            grant_id,
            "--wait",
            "0",
        )
    assert not merchant.next_requests


def test_next_all_needs_origin_when_saved_grants_span_servers(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    login(merchant, profile, monkeypatch, "second")
    with remote.private_profile(profile) as data:
        data["grants"][1]["origin"] = "https://another-merchant.example"
    with pytest.raises(remote.ManageError):
        run(merchant, profile, "next", "--all", "--wait", "0")
    assert not merchant.next_requests
    run(merchant, profile, "next", "--all", "--origin", ORIGIN, "--wait", "0")
    assert len(merchant.next_requests) == 1
    assert {
        grant["product_id"] for grant in merchant.next_requests[0]["payload"]["grants"]
    } == {"product-a"}


def test_next_idle_does_not_fetch_or_claim_historical_jobs(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    merchant.idle = True
    result = run(merchant, profile, "next", "--product", "product-a", "--wait", "0")
    assert result["ok"] is True and result["idle"] is True and result["items"] == []
    assert len(merchant.next_requests) == 1
    assert_atomic_next_only(merchant)


@pytest.mark.parametrize(
    "field,value", [("product_id", "foreign-product"), ("device_id", "foreign-device")]
)
def test_next_rejects_returned_items_outside_signed_grants(
    field, value, merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    merchant.tamper_response = lambda response: response["items"][0].update(
        {field: value}
    )
    with pytest.raises(remote.ManageError) as error:
        run(merchant, profile, "next", "--product", "product-a", "--wait", "0")
    assert error.value.code == "invalid_response"


def test_next_rejects_job_scope_mismatching_its_item(merchant, profile, monkeypatch):
    login(merchant, profile, monkeypatch)
    merchant.tamper_response = lambda response: response["items"][0]["job"].update(
        product_id="foreign-product"
    )
    with pytest.raises(remote.ManageError) as error:
        run(merchant, profile, "next", "--product", "product-a", "--wait", "0")
    assert error.value.code == "invalid_response"


def test_next_omits_server_history_and_private_provider_metadata(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    secret = "provider-secret-that-must-not-reach-cli-output"

    def extra_metadata(response):
        response["history"] = [{"params": {"old_demand": secret}}]
        item = response["items"][0]
        item["history"] = [{"private_prompt": secret}]
        item["provider"] = {"api_key": secret}
        item["job"]["history"] = [{"params": {"old_demand": secret}}]
        item["job"]["provider"] = {"api_key": secret}
        item["execution"]["history"] = [{"private_prompt": secret}]
        item["execution"]["provider_api_key"] = secret

    merchant.tamper_response = extra_metadata
    result = run(merchant, profile, "next", "--product", "product-a", "--wait", "0")
    serialized = json.dumps(result)
    assert secret not in serialized and "history" not in serialized
    assert "provider" not in serialized
    assert result["items"][0]["execution"]["params"] == {
        "request": "current customer demand"
    }


def test_next_401_refresh_reuses_the_exact_signed_claim_body(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    merchant.unauthorized_once = True
    result = run(merchant, profile, "next", "--product", "product-a", "--wait", "0")
    assert result["ok"] is True
    assert len(merchant.next_requests) == 2
    first, second = merchant.next_requests
    assert first["raw"] == second["raw"]
    assert first["authorization"] != second["authorization"]
    assert len(merchant.receipts) == 1
    assert_atomic_next_only(merchant)


def test_lost_next_response_survives_process_restart_and_finishes_the_original_receipt(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    merchant.drop_responses = 100
    with pytest.raises(remote.ManageError):
        run(merchant, profile, "next", "--all", "--wait", "0")
    first = merchant.next_requests[0]
    assert all(request["raw"] == first["raw"] for request in merchant.next_requests)
    assert first["payload"]["request_id"] in profile.read_text()
    # A new grant between invocations cannot broaden an already committed claim.
    login(merchant, profile, monkeypatch, "second")
    with remote.private_profile(profile) as data:
        data["grants"].reverse()
    merchant.drop_responses = 0
    result = run(merchant, profile, "next", "--all", "--wait", "0")
    assert result["ok"] is True and result["replayed"] is True
    assert result["request_id"] == first["payload"]["request_id"]
    assert merchant.next_requests[-1]["raw"] == first["raw"]
    assert merchant.next_requests[-1]["authorization"] == first["authorization"]
    assert len(merchant.receipts) == 1
    following = run(merchant, profile, "next", "--all", "--wait", "0")
    assert following["request_id"] != result["request_id"]
    assert len(merchant.next_requests[-1]["payload"]["grants"]) == 2
    assert_atomic_next_only(merchant)


def test_interrupted_next_keeps_the_original_receipt_for_the_next_invocation(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    merchant.interrupt_once = True
    with pytest.raises(KeyboardInterrupt):
        run(merchant, profile, "next", "--product", "product-a", "--wait", "0")
    original = merchant.next_requests[0]
    assert original["payload"]["request_id"] in profile.read_text()
    result = run(merchant, profile, "next", "--product", "product-a", "--wait", "0")
    assert result["replayed"] is True
    assert merchant.next_requests[-1]["raw"] == original["raw"]
    assert len(merchant.receipts) == 1


def test_main_ctrl_c_exits_privately_and_repeated_command_recovers_same_receipt(
    merchant, profile, monkeypatch, capsys
):
    login(merchant, profile, monkeypatch)
    saved = stored_grants(profile)[0]
    execute = remote.execute
    monkeypatch.setattr(
        remote,
        "execute",
        lambda args: execute(args, transport=httpx.MockTransport(merchant)),
    )
    args = arguments("next", "--product", "product-a", "--wait", "0", profile=profile)
    merchant.interrupt_once = True
    with pytest.raises(SystemExit) as error:
        remote.main(args)
    assert error.value.code == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    interrupted = json.loads(captured.err)
    assert interrupted["ok"] is False and interrupted["code"] == "interrupted"
    assert "不加 --new-request" in interrupted["error"]
    assert (
        captured.err
        == json.dumps(interrupted, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    assert (
        saved["private_key"] not in captured.err
        and saved["access_token"] not in captured.err
    )
    assert "signature" not in captured.err
    assert stat.S_IMODE(profile.stat().st_mode) == 0o600
    original = merchant.next_requests[0]
    assert original["payload"]["request_id"] in profile.read_text()
    remote.main(args)
    recovered_output = capsys.readouterr()
    assert recovered_output.err == ""
    recovered = json.loads(recovered_output.out)
    assert recovered["replayed"] is True
    assert merchant.next_requests[-1]["raw"] == original["raw"]
    assert len(merchant.receipts) == 1


def test_pending_next_rejects_changed_wait_before_issuing_a_new_claim(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    merchant.drop_responses = 100
    with pytest.raises(remote.ManageError):
        run(merchant, profile, "next", "--product", "product-a", "--wait", "0")
    original = merchant.next_requests[-1]
    before = len(merchant.next_requests)
    with pytest.raises(remote.ManageError) as error:
        run(merchant, profile, "next", "--product", "product-a", "--wait", "1")
    assert error.value.code == "pending_request"
    assert len(merchant.next_requests) == before
    merchant.drop_responses = 0
    result = run(merchant, profile, "next", "--product", "product-a", "--wait", "0")
    assert result["replayed"] is True
    assert merchant.next_requests[-1]["raw"] == original["raw"]


def test_expired_pending_claim_stops_until_explicit_new_request_and_keeps_other_selector(
    merchant, profile, monkeypatch
):
    clock = DeviceClock()
    monkeypatch.setattr(remote, "time", clock)
    login(merchant, profile, monkeypatch)
    login(merchant, profile, monkeypatch, "second")
    original = {}
    merchant.drop_responses = 100
    for product in ("product-a", "product-b"):
        with pytest.raises(remote.ManageError):
            run(merchant, profile, "next", "--product", product, "--wait", "0")
        original[product] = merchant.next_requests[-1]
    clock.elapsed += 601
    before = len(merchant.next_requests)
    with pytest.raises(remote.ManageError) as error:
        run(merchant, profile, "next", "--product", "product-a", "--wait", "0")
    assert error.value.code == "request_expired"
    assert len(merchant.next_requests) == before
    saved = profile.read_text()
    assert all(
        request["payload"]["request_id"] in saved for request in original.values()
    )
    merchant.drop_responses = 0
    result = run(
        merchant,
        profile,
        "next",
        "--product",
        "product-a",
        "--wait",
        "0",
        "--new-request",
    )
    assert result["request_id"] != original["product-a"]["payload"]["request_id"]
    saved = profile.read_text()
    assert original["product-a"]["payload"]["request_id"] not in saved
    assert original["product-b"]["payload"]["request_id"] in saved


def test_explicit_new_request_can_replace_active_receipt_with_changed_wait(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    merchant.drop_responses = 100
    with pytest.raises(remote.ManageError):
        run(merchant, profile, "next", "--product", "product-a", "--wait", "0")
    original = merchant.next_requests[-1]
    merchant.drop_responses = 0
    result = run(
        merchant,
        profile,
        "next",
        "--product",
        "product-a",
        "--wait",
        "1",
        "--new-request",
    )
    assert result["request_id"] != original["payload"]["request_id"]
    assert merchant.next_requests[-1]["payload"]["wait_seconds"] == 1
    assert original["payload"]["request_id"] not in profile.read_text()


def test_recovering_one_selector_preserves_another_pending_claim(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    login(merchant, profile, monkeypatch, "second")
    original = {}
    merchant.drop_responses = 100
    for product in ("product-a", "product-b"):
        with pytest.raises(remote.ManageError):
            run(merchant, profile, "next", "--product", product, "--wait", "0")
        original[product] = merchant.next_requests[-1]
    merchant.drop_responses = 0
    first = run(merchant, profile, "next", "--product", "product-a", "--wait", "0")
    assert first["replayed"] is True
    assert merchant.next_requests[-1]["raw"] == original["product-a"]["raw"]
    assert original["product-b"]["payload"]["request_id"] in profile.read_text()
    second = run(merchant, profile, "next", "--product", "product-b", "--wait", "0")
    assert second["replayed"] is True
    assert merchant.next_requests[-1]["raw"] == original["product-b"]["raw"]
    assert len(merchant.receipts) == 2


def test_next_rejects_one_device_id_claiming_two_products_locally(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    login(merchant, profile, monkeypatch, "second")
    first, second = stored_grants(profile)
    with remote.private_profile(profile) as data:
        data["grants"][1]["device_id"] = first["device_id"]
    merchant.sessions[second["access_token"]]["device_id"] = first["device_id"]
    with pytest.raises(remote.ManageError) as error:
        run(merchant, profile, "next", "--all", "--wait", "0")
    assert error.value.code == "invalid_input"
    assert not merchant.next_requests


def test_logout_erases_selected_claim_receipt_and_keeps_another_grants_recovery(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    login(merchant, profile, monkeypatch, "second")
    first_grant, second_grant = stored_grants(profile)
    original = {}
    merchant.drop_responses = 100
    for grant in (first_grant, second_grant):
        with pytest.raises(remote.ManageError):
            run(merchant, profile, "next", "--grant", grant["id"], "--wait", "0")
        original[grant["id"]] = merchant.next_requests[-1]
    result = run(merchant, profile, "logout", "--grant", first_grant["id"])
    assert result["ok"] is True
    saved = profile.read_text()
    assert original[first_grant["id"]]["payload"]["request_id"] not in saved
    assert original[second_grant["id"]]["payload"]["request_id"] in saved
    assert [grant["id"] for grant in stored_grants(profile)] == [second_grant["id"]]
    merchant.drop_responses = 0
    recovered = run(
        merchant, profile, "next", "--grant", second_grant["id"], "--wait", "0"
    )
    assert recovered["replayed"] is True
    assert merchant.next_requests[-1]["raw"] == original[second_grant["id"]]["raw"]


@pytest.mark.parametrize(
    "command,flags,action",
    [
        ("claim", [], "claim"),
        ("progress", ["--progress", "40"], "progress"),
        ("complete", [], "succeed"),
        ("succeed", [], "succeed"),
        ("fail", ["--retryable"], "fail"),
        ("retry", [], "retry"),
        ("request-retry", ["--reason", "请补充输入"], "request_retry"),
        ("request-changes", ["--reason", "请补充输入"], "request_changes"),
        ("reject", ["--reason", "无法提供此服务"], "reject"),
    ],
)
def test_manage_actions_forward_the_exact_attempt_and_flow_execution_guards(
    command, flags, action, merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    result = run(
        merchant,
        profile,
        command,
        "job-product-a",
        "--product",
        "product-a",
        "--attempt",
        "4",
        "--flow-epoch",
        str(2**53 - 1),
        "--action-id",
        "action_BC-1.2",
        *flags,
    )
    assert result["ok"] is True
    writes = [call for call in merchant.calls if call[1] == "/api/manage/batch"]
    assert len(writes) == 1
    payload = json.loads(writes[0][3])
    assert payload["ids"] == ["job-product-a"] and payload["action"] == action
    assert payload["attempt"] == 4 and type(payload["attempt"]) is int
    assert payload["flow_epoch"] == 2**53 - 1
    assert payload["action_id"] == "action_BC-1.2"
    assert "expected_revision" not in payload


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "true"])
def test_manage_action_rejects_nonpositive_or_noninteger_attempt_before_network(
    value, merchant, profile
):
    with pytest.raises(SystemExit) as error:
        run(
            merchant,
            profile,
            "progress",
            "job",
            "--product",
            "product-a",
            "--attempt",
            value,
        )
    assert error.value.code == 2
    assert not merchant.calls


@pytest.mark.parametrize("value", ["0", "-1", str(2**53), "1.5", "true"])
def test_manage_action_rejects_invalid_flow_epoch_before_network(
    value, merchant, profile
):
    with pytest.raises(SystemExit) as error:
        run(
            merchant,
            profile,
            "progress",
            "job",
            "--product",
            "product-a",
            "--flow-epoch",
            value,
        )
    assert error.value.code == 2
    assert not merchant.calls


@pytest.mark.parametrize("value", ["", "has space", "é", "\t", "\n", "\x7f"])
def test_manage_action_rejects_invalid_action_token_before_network(
    value, merchant, profile
):
    with pytest.raises(SystemExit) as error:
        run(
            merchant,
            profile,
            "progress",
            "job",
            "--product",
            "product-a",
            "--action-id",
            value,
        )
    assert error.value.code == 2
    assert not merchant.calls


def file_options(field, *paths):
    return [value for path in paths for value in ("--file", f"{field}={path}")]


def batch_payload(merchant):
    writes = [call for call in merchant.calls if call[1] == "/api/manage/batch"]
    assert len(writes) == 1
    return json.loads(writes[0][3])


@pytest.mark.parametrize("command", ["complete", "succeed"])
def test_complete_repeated_images_keep_upload_order_and_canonical_string_output(
    command, merchant, profile, monkeypatch, tmp_path
):
    login(merchant, profile, monkeypatch)
    merchant.job_outputs = [{"key": "photos", "type": "images", "max_items": 3}]
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"first-image")
    second.write_bytes(b"second-image")
    result = run(
        merchant,
        profile,
        command,
        "job",
        "--product",
        "product-a",
        *file_options("photos", second, first),
    )
    assert result["ok"] is True
    assert [upload["content"] for upload in merchant.delivery_uploads] == [
        b"second-image",
        b"first-image",
    ]
    assert [upload["fields"]["field_key"] for upload in merchant.delivery_uploads] == [
        "photos",
        "photos",
    ]
    assert all(
        upload["fields"]["job_id"] == "job" for upload in merchant.delivery_uploads
    )
    payload = batch_payload(merchant)
    assert payload["action"] == "succeed"
    assert payload["output"] == {
        "photos": '["00000000-0000-4000-8000-000000000001","00000000-0000-4000-8000-000000000002"]'
    }


@pytest.mark.parametrize("kind", ["file", "image"])
def test_complete_scalar_file_schema_uses_one_lowercase_uuid_string(
    kind, merchant, profile, monkeypatch, tmp_path
):
    login(merchant, profile, monkeypatch)
    merchant.job_outputs = [{"key": "artifact", "type": kind}]
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"artifact")
    run(
        merchant,
        profile,
        "complete",
        "job",
        "--product",
        "product-a",
        *file_options("artifact", source),
    )
    assert batch_payload(merchant)["output"] == {
        "artifact": "00000000-0000-4000-8000-000000000001"
    }
    assert len(merchant.delivery_uploads) == 1


@pytest.mark.parametrize("kind", ["file", "image"])
def test_complete_repeated_scalar_files_are_refused_before_any_upload(
    kind, merchant, profile, monkeypatch, tmp_path
):
    login(merchant, profile, monkeypatch)
    merchant.job_outputs = [{"key": "artifact", "type": kind}]
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"artifact")
    with pytest.raises(remote.ManageError):
        run(
            merchant,
            profile,
            "complete",
            "job",
            "--product",
            "product-a",
            *file_options("artifact", source, source),
        )
    assert not merchant.delivery_uploads
    assert not any(call[1] == "/api/manage/batch" for call in merchant.calls)


@pytest.mark.parametrize(
    "schema,count",
    [
        ({"key": "photos", "type": "images", "max_items": 2}, 3),
        ({"key": "photos", "type": "images"}, 11),
    ],
)
def test_complete_images_obeys_schema_count_and_default_ten_before_uploading(
    schema, count, merchant, profile, monkeypatch, tmp_path
):
    login(merchant, profile, monkeypatch)
    merchant.job_outputs = [schema]
    source = tmp_path / "picture.png"
    source.write_bytes(b"image")
    with pytest.raises(remote.ManageError):
        run(
            merchant,
            profile,
            "complete",
            "job",
            "--product",
            "product-a",
            *file_options("photos", *([source] * count)),
        )
    assert not merchant.delivery_uploads
    assert not any(call[1] == "/api/manage/batch" for call in merchant.calls)


@pytest.mark.parametrize("max_items", [0, 21, True, 1.5])
def test_complete_rejects_invalid_images_server_schema_before_upload(
    max_items, merchant, profile, monkeypatch, tmp_path
):
    login(merchant, profile, monkeypatch)
    merchant.job_outputs = [{"key": "photos", "type": "images", "max_items": max_items}]
    source = tmp_path / "picture.png"
    source.write_bytes(b"image")
    with pytest.raises(remote.ManageError):
        run(
            merchant,
            profile,
            "complete",
            "job",
            "--product",
            "product-a",
            *file_options("photos", source),
        )
    assert not merchant.delivery_uploads


@pytest.mark.parametrize("bad_field", ["unknown", "text"])
def test_complete_files_must_match_the_current_file_capable_output_schema(
    bad_field, merchant, profile, monkeypatch, tmp_path
):
    login(merchant, profile, monkeypatch)
    merchant.job_outputs = [
        {"key": "artifact", "type": "file"},
        {"key": "text", "type": "text"},
    ]
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"artifact")
    with pytest.raises(remote.ManageError):
        run(
            merchant,
            profile,
            "complete",
            "job",
            "--product",
            "product-a",
            *file_options(bad_field, source),
        )
    assert not merchant.delivery_uploads
    assert not any(call[1] == "/api/manage/batch" for call in merchant.calls)


@pytest.mark.parametrize("invalid_source", ["missing", "directory", "oversized"])
def test_complete_prevalidates_every_local_file_before_the_first_upload(
    invalid_source, merchant, profile, monkeypatch, tmp_path
):
    login(merchant, profile, monkeypatch)
    merchant.job_outputs = [{"key": "photos", "type": "images"}]
    first = tmp_path / "valid.png"
    first.write_bytes(b"valid")
    second = tmp_path / invalid_source
    if invalid_source == "directory":
        second.mkdir()
    elif invalid_source == "oversized":
        with second.open("wb") as source:
            source.truncate(remote.MAX_UPLOAD_BYTES + 1)
    with pytest.raises(remote.ManageError):
        run(
            merchant,
            profile,
            "complete",
            "job",
            "--product",
            "product-a",
            *file_options("photos", first, second),
        )
    assert not merchant.delivery_uploads
    assert not any(call[1] == "/api/manage/batch" for call in merchant.calls)


def test_complete_refuses_output_file_and_upload_conflicting_on_the_same_field(
    merchant, profile, monkeypatch, tmp_path
):
    login(merchant, profile, monkeypatch)
    merchant.job_outputs = [{"key": "photos", "type": "images"}]
    source = tmp_path / "picture.png"
    source.write_bytes(b"image")
    output = tmp_path / "output.json"
    output.write_text(
        json.dumps({"photos": '["00000000-0000-4000-8000-000000000001"]'})
    )
    with pytest.raises(remote.ManageError):
        run(
            merchant,
            profile,
            "complete",
            "job",
            "--product",
            "product-a",
            "--output-file",
            str(output),
            *file_options("photos", source),
        )
    assert not merchant.delivery_uploads
    assert not any(call[1] == "/api/manage/batch" for call in merchant.calls)


def test_complete_preserves_images_json_string_from_legacy_output_file(
    merchant, profile, monkeypatch, tmp_path
):
    login(merchant, profile, monkeypatch)
    merchant.job_outputs = [{"key": "photos", "type": "images"}]
    output = tmp_path / "output.json"
    images = '["00000000-0000-4000-8000-000000000002","00000000-0000-4000-8000-000000000001"]'
    output.write_text(json.dumps({"photos": images}))
    run(
        merchant,
        profile,
        "complete",
        "job",
        "--product",
        "product-a",
        "--output-file",
        str(output),
    )
    assert batch_payload(merchant)["output"] == {"photos": images}
    assert not merchant.delivery_uploads


def test_complete_flow_file_upload_sends_current_epoch_and_batch_action_guard(
    merchant, profile, monkeypatch, tmp_path
):
    login(merchant, profile, monkeypatch)
    merchant.job_outputs = [{"key": "artifact", "type": "file"}]
    merchant.flow_guard = (7, "action-current")
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"artifact")
    run(
        merchant,
        profile,
        "complete",
        "job",
        "--product",
        "product-a",
        "--flow-epoch",
        "7",
        "--action-id",
        "action-current",
        *file_options("artifact", source),
    )
    assert merchant.delivery_uploads[0]["fields"]["flow_epoch"] == "7"
    assert merchant.delivery_uploads[0]["fields"]["action_id"] == "action-current"
    assert "expected_revision" not in merchant.delivery_uploads[0]["fields"]
    payload = batch_payload(merchant)
    assert payload["flow_epoch"] == 7 and payload["action_id"] == "action-current"
    assert "expected_revision" not in payload


@pytest.mark.parametrize(
    "guards",
    [
        [],
        ["--flow-epoch", "7"],
        ["--action-id", "action-current"],
        ["--flow-epoch", "6", "--action-id", "action-current"],
        ["--flow-epoch", "7", "--action-id", "old-action"],
        ["--flow-epoch", "7", "--action-id", "action-current", "--attempt", "2"],
    ],
)
def test_complete_flow_files_require_explicit_guards_matching_the_fresh_current_step(
    guards, merchant, profile, monkeypatch, tmp_path
):
    login(merchant, profile, monkeypatch)
    merchant.job_outputs = [{"key": "artifact", "type": "file"}]
    merchant.flow_guard = (7, "action-current")
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"artifact")
    with pytest.raises(remote.ManageError):
        run(
            merchant,
            profile,
            "complete",
            "job",
            "--product",
            "product-a",
            *guards,
            *file_options("artifact", source),
        )
    assert not merchant.delivery_uploads
    assert not any(call[1] == "/api/manage/batch" for call in merchant.calls)


@pytest.fixture
def real_automation(owner, profile, monkeypatch):
    from extore import automation

    # This is the conftest-owned temporary database. Its explicit schema setup
    # exercises the real endpoint before root's migration integration lands;
    # it does not establish that ordinary server startup installs this table.
    with db() as c:
        automation.init_schema(c)
        c.execute("DELETE FROM automation_requests")
    origin = "http://localhost:8000"

    class API:
        def __init__(self, api):
            self.api = api
            self.calls = []
            self.drop_next_responses = 0
            self.secret = "synthetic-provider-signing-secret-for-cli-integration"

        def transport(self, request):
            raw = request.read()
            self.calls.append((request.method, request.url.path, raw))
            assert request.headers.get("Cookie") is None
            response = self.api.request(
                request.method,
                str(request.url),
                headers=dict(request.headers),
                content=raw,
            )
            assert "set-cookie" not in response.headers
            if (
                request.url.path == "/api/manage/next"
                and response.status_code == 200
                and self.drop_next_responses
            ):
                self.drop_next_responses -= 1
                raise httpx.ReadError(
                    "synthetic lost real API response", request=request
                )
            return httpx.Response(
                response.status_code,
                headers=dict(response.headers),
                content=response.content,
            )

        def command(self, *argv):
            return remote.execute(
                arguments(*argv, profile=profile),
                transport=httpx.MockTransport(self.transport),
            )

        def product(self, name):
            response = owner.post(
                "/api/admin/products",
                json={
                    "name": name,
                    "mode": "manual",
                    "parameters": [
                        {"key": "request", "label": {"zh-CN": "需求"}, "type": "text"}
                    ],
                    "outputs": [
                        {"key": "result", "label": {"zh-CN": "结果"}, "type": "text"}
                    ],
                    "webhook_url": "https://synthetic-provider.example/hook",
                    "webhook_secret": self.secret,
                },
            )
            assert response.status_code == 200, response.text
            return response.json()

        def login(self, product):
            response = owner.post(
                "/api/admin/staff",
                json={
                    "product_id": product["id"],
                    "name": "Synthetic automation CLI",
                    "permissions": ["queue.view", "queue.process"],
                },
            )
            assert response.status_code == 200, response.text
            monkeypatch.setattr(sys, "stdin", io.StringIO(response.json()["url"]))
            return self.command("login", "--link-stdin")

        def queue(self, product, demand):
            response = owner.post(
                "/api/admin/cards", json={"product_id": product["id"], "count": 1}
            )
            assert response.status_code == 200, response.text
            response = owner.post(
                "/api/exchange", json={"code": response.json()["codes"][0]}
            )
            assert response.status_code == 200, response.text
            response = owner.post(
                "/api/redeem",
                json={"token": response.json()["token"], "params": {"request": demand}},
            )
            assert response.status_code == 200, response.text
            return response.json()

    with TestClient(app, base_url=origin) as api:
        yield API(api)
    with db() as c:
        c.execute("DELETE FROM automation_requests")


def test_real_api_next_delivers_current_schema_without_unapproved_inputs_or_provider_secret(
    real_automation, profile
):
    api = real_automation
    approved = api.product("Approved product")
    unapproved = api.product("Unapproved product")
    current = api.queue(approved, "current authorized demand")
    hidden = api.queue(unapproved, "unapproved customer demand")
    api.login(approved)
    result = api.command("next", "--product", approved["id"], "--wait", "0")
    item = result["items"][0]
    assert item["job"]["id"] == current["id"]
    assert item["grant_id"] == stored_grants(profile)[0]["id"]
    assert item["execution"]["params"] == {"request": "current authorized demand"}
    assert item["execution"]["parameters"] == approved["parameters"]
    assert item["execution"]["outputs"] == approved["outputs"]
    serialized = json.dumps(result)
    assert "history" not in serialized
    assert (
        api.secret not in serialized and "synthetic-provider.example" not in serialized
    )
    assert "unapproved customer demand" not in serialized
    with db() as c:
        hidden_row = c.execute(
            "SELECT * FROM jobs WHERE id=?", (hidden["id"],)
        ).fetchone()
        assert hidden_row["state"] == "queued" and hidden_row["claimed_by"] is None


def test_real_api_lost_multi_product_claim_replays_one_receipt_and_one_claim_audit(
    real_automation,
):
    api = real_automation
    first_product = api.product("Document product")
    second_product = api.product("Slides product")
    first = api.queue(first_product, "document demand")
    second = api.queue(second_product, "slides demand")
    api.login(first_product)
    api.login(second_product)
    api.drop_next_responses = 100
    with pytest.raises(remote.ManageError):
        api.command("next", "--all", "--wait", "0")
    request = next(call[2] for call in api.calls if call[1] == "/api/manage/next")
    assert len(json.loads(request)["grants"]) == 2
    with db() as c:
        claimed = c.execute("SELECT * FROM jobs WHERE state='processing'").fetchone()
        assert claimed is not None
        claimed_id = claimed["id"]
        assert (
            c.execute("SELECT count(*) FROM jobs WHERE state='processing'").fetchone()[
                0
            ]
            == 1
        )
        assert (
            c.execute(
                "SELECT count(*) FROM audit WHERE action='queue.next.claim'"
            ).fetchone()[0]
            == 1
        )
    api.drop_next_responses = 0
    recovered = api.command("next", "--all", "--wait", "0")
    assert recovered["replayed"] is True
    assert recovered["items"][0]["job"]["id"] == claimed_id
    assert [call[2] for call in api.calls if call[1] == "/api/manage/next"] == [
        request,
        request,
    ]
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM audit WHERE action='queue.next.claim'"
            ).fetchone()[0]
            == 1
        )
        receipt = c.execute("SELECT * FROM automation_requests").fetchone()
        assert (
            "document demand" not in receipt["items"]
            and "slides demand" not in receipt["items"]
        )
    following = api.command("next", "--all", "--wait", "0")
    assert following["request_id"] != recovered["request_id"]
    assert (
        following["items"][0]["job"]["id"]
        == ({first["id"], second["id"]} - {claimed_id}).pop()
    )
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM audit WHERE action='queue.next.claim' AND target=?",
                (claimed_id,),
            ).fetchone()[0]
            == 1
        )
    assert not any(
        call[1] in ("/api/manage/jobs", "/api/manage/batch") for call in api.calls
    )


def test_watch_waits_again_after_idle_and_main_prints_only_the_claimed_item(
    merchant, profile, monkeypatch, capsys
):
    login(merchant, profile, monkeypatch)
    merchant.next_idles = [True, False]
    execute = remote.execute
    monkeypatch.setattr(
        remote,
        "execute",
        lambda args: execute(args, transport=httpx.MockTransport(merchant)),
    )
    remote.main(arguments("next", "--all", "--watch", profile=profile))
    captured = capsys.readouterr()
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    result = json.loads(captured.out)
    assert result["idle"] is False and len(result["items"]) == 1
    assert len(merchant.next_requests) == 2
    first, second = merchant.next_requests
    assert first["payload"]["request_id"] != second["payload"]["request_id"]
    assert first["payload"]["wait_seconds"] == second["payload"]["wait_seconds"] == 25


@pytest.mark.parametrize("interruption", ["connection", "keyboard"])
def test_watch_stops_on_second_wait_interruption_and_replays_its_saved_body(
    interruption, merchant, profile, monkeypatch, capsys
):
    login(merchant, profile, monkeypatch)
    merchant.next_idles = [True, False]

    def transport(request):
        response = merchant(request)
        if request.url.path == "/api/manage/next" and len(merchant.next_requests) == 2:
            if interruption == "keyboard":
                raise KeyboardInterrupt
            raise httpx.ReadError(
                "synthetic lost second watch response", request=request
            )
        return response

    execute = remote.execute
    monkeypatch.setattr(
        remote,
        "execute",
        lambda args: execute(args, transport=httpx.MockTransport(transport)),
    )
    args = arguments("next", "--all", "--watch", profile=profile)
    with pytest.raises(SystemExit) as error:
        remote.main(args)
    assert error.value.code == (130 if interruption == "keyboard" else 1)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["code"] == (
        "interrupted" if interruption == "keyboard" else "connection_error"
    )
    assert len(merchant.next_requests) == 2
    original = merchant.next_requests[-1]
    assert original["payload"]["request_id"] in profile.read_text()
    remote.main(args)
    recovered_output = capsys.readouterr()
    assert recovered_output.err == "" and len(recovered_output.out.splitlines()) == 1
    recovered = json.loads(recovered_output.out)
    assert recovered["replayed"] is True
    assert recovered["request_id"] == original["payload"]["request_id"]
    assert merchant.next_requests[-1]["raw"] == original["raw"]
    assert len(merchant.next_requests) == 3


def test_watch_refuses_zero_wait_without_entering_a_busy_loop(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    with pytest.raises(remote.ManageError) as error:
        run(merchant, profile, "next", "--all", "--watch", "--wait", "0")
    assert error.value.code == "invalid_input"
    assert not merchant.next_requests


@pytest.mark.parametrize("requested_epoch", [None, 7])
@pytest.mark.parametrize("requested_action", [None, "action-current"])
def test_standalone_upload_uses_the_fresh_flow_epoch_and_action_in_multipart(
    requested_epoch, requested_action, merchant, profile, monkeypatch, tmp_path
):
    login(merchant, profile, monkeypatch)
    merchant.job_outputs = [{"key": "artifact", "type": "file"}]
    merchant.flow_guard = (7, "action-current")
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"artifact")
    flags = [] if requested_epoch is None else ["--flow-epoch", str(requested_epoch)]
    if requested_action is not None:
        flags += ["--action-id", requested_action]
    result = run(
        merchant,
        profile,
        "upload",
        "job",
        "--product",
        "product-a",
        "--field",
        "artifact",
        "--file",
        str(source),
        *flags,
    )
    assert result["ok"] is True
    assert merchant.delivery_uploads[0]["fields"]["flow_epoch"] == "7"
    assert merchant.delivery_uploads[0]["fields"]["action_id"] == "action-current"
    assert not any(call[1] == "/api/manage/batch" for call in merchant.calls)


def test_standalone_upload_rejects_a_stale_explicit_flow_epoch_before_posting(
    merchant, profile, monkeypatch, tmp_path
):
    login(merchant, profile, monkeypatch)
    merchant.job_outputs = [{"key": "artifact", "type": "file"}]
    merchant.flow_guard = (7, "action-current")
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"artifact")
    with pytest.raises(remote.ManageError) as error:
        run(
            merchant,
            profile,
            "upload",
            "job",
            "--product",
            "product-a",
            "--field",
            "artifact",
            "--file",
            str(source),
            "--flow-epoch",
            "6",
        )
    assert error.value.code == "stale_task"
    assert not merchant.delivery_uploads


def test_standalone_upload_rejects_a_stale_explicit_action_before_posting(
    merchant, profile, monkeypatch, tmp_path
):
    login(merchant, profile, monkeypatch)
    merchant.job_outputs = [{"key": "artifact", "type": "file"}]
    merchant.flow_guard = (7, "action-current")
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"artifact")
    with pytest.raises(remote.ManageError) as error:
        run(
            merchant,
            profile,
            "upload",
            "job",
            "--product",
            "product-a",
            "--field",
            "artifact",
            "--file",
            str(source),
            "--action-id",
            "old-action",
        )
    assert error.value.code == "stale_task"
    assert not merchant.delivery_uploads


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "invalid-file-id"),
        ("id", "00000000-0000-4000-8000-00000000000A"),
        ("job_id", "foreign-job"),
        ("field_key", "foreign-field"),
    ],
)
def test_complete_rejects_wrong_attachment_scope_or_uuid_before_sending_delivery(
    field, value, merchant, profile, monkeypatch, tmp_path
):
    login(merchant, profile, monkeypatch)
    merchant.job_outputs = [{"key": "artifact", "type": "file"}]
    merchant.tamper_upload = lambda uploaded: uploaded.update({field: value})
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"artifact")
    with pytest.raises(remote.ManageError) as error:
        run(
            merchant,
            profile,
            "complete",
            "job",
            "--product",
            "product-a",
            *file_options("artifact", source),
        )
    assert error.value.code == "invalid_response"
    assert not any(call[1] == "/api/manage/batch" for call in merchant.calls)


def test_complete_rejects_duplicate_image_ids_returned_by_the_upload_server(
    merchant, profile, monkeypatch, tmp_path
):
    login(merchant, profile, monkeypatch)
    merchant.job_outputs = [{"key": "photos", "type": "images"}]
    merchant.tamper_upload = lambda uploaded: uploaded.update(
        id="00000000-0000-4000-8000-000000000001"
    )
    source = tmp_path / "picture.png"
    source.write_bytes(b"image")
    with pytest.raises(remote.ManageError) as error:
        run(
            merchant,
            profile,
            "complete",
            "job",
            "--product",
            "product-a",
            *file_options("photos", source, source),
        )
    assert error.value.code == "invalid_response"
    assert len(merchant.delivery_uploads) == 2
    assert not any(call[1] == "/api/manage/batch" for call in merchant.calls)

"""Revision CLI recovery, historical files, and immutable worker context."""

import argparse
import copy
import io
import json
import stat
import time
import uuid

import httpx
import pytest
from test_automation_manage_cli import AutomationMerchant
from test_manage_cli import login, run
from test_owner_cli import OwnerMerchant, authorize
from test_owner_cli import execute as admin

from extore import customer_cli
from extore.manage_client import ManageError, compact_job
from extore.sdk import Result, Task
from extore.sdk.client import signature, verify_event
from extore.sdk.script import run as run_script

ORIGIN = "https://merchant.example"


def metadata(revision=0):
    return {
        "card_attributes": {"edit_passes": 2, "layout": "compact"},
        "entitlements": {
            "attribute_key": "edit_passes",
            "label": {"zh-CN": "修改次数"},
            "total": 2,
            "used": revision,
            "remaining": 2 - revision,
            "can_request": True,
            "reason": None,
        },
        "revision": {
            "current": revision,
            "message": "调整表格" if revision else "",
            "is_revision": revision > 0,
        },
        "deliveries": [
            {
                "revision": 0,
                "attempt": 1,
                "created": 123,
                "revealed": True,
                "has_files": True,
            }
        ],
        "last_delivery": {"revision": 0, "attempt": 1, "created": 123},
    }


class RevisionServer:
    def __init__(self):
        self.round = 0
        self.requests = []
        self.downloads = []
        self.fail_once = False
        self.rejection = None

    def job(self):
        return {
            "id": "job-a",
            "state": "succeeded" if not self.round else "queued",
            "attempt": self.round + 1,
            "params": {"private": "hidden-input"},
            "output": {"content": "hidden-content"},
            **metadata(self.round),
        }

    def receipt(self):
        return {
            "product": {
                "id": "product-a",
                "name": "商品",
                "parameters": [],
                "outputs": [],
            },
            "job": self.job(),
            **metadata(self.round),
        }

    def __call__(self, request):
        assert (
            "authorization" not in request.headers and "cookie" not in request.headers
        )
        body = json.loads(request.read())
        assert body["token"] == "private-receipt-token"
        if request.url.path == "/api/receipt":
            return httpx.Response(200, json=self.receipt())
        if request.url.path == "/api/receipt/revisions":
            self.requests.append(body)
            if self.rejection:
                return httpx.Response(self.rejection, json={"detail": "private-error"})
            self.round = 1
            if self.fail_once:
                self.fail_once = False
                raise httpx.ReadError("lost accepted response", request=request)
            return httpx.Response(200, json=self.job())
        if request.url.path == "/api/receipt/reveal":
            revision = body.get("revision", self.round)
            return httpx.Response(
                200,
                json={
                    "revision": revision,
                    "output": {"document": "file-" + str(revision)},
                    "files": [
                        {
                            "id": "file-" + str(revision),
                            "field_key": "document",
                            "filename": "paper.docx",
                            "size": 4,
                        }
                    ],
                },
            )
        if request.url.path == "/api/files/download":
            self.downloads.append(body)
            return httpx.Response(200, content=b"docx")
        raise AssertionError(request.url.path)


def client(server, persisted=None):
    entry = {
        "id": "rcpt_a",
        "origin": ORIGIN,
        "token": "private-receipt-token",
        "status": customer_cli._summary_receipt(server.receipt()),
    }
    data = {"version": 1, "receipts": [entry]}
    return customer_cli.CustomerClient(
        data,
        transport=httpx.MockTransport(server),
        persist=lambda: (
            persisted.append(copy.deepcopy(data)) if persisted is not None else None
        ),
    ), entry


def test_lost_revision_response_reuses_exact_persisted_request_before_refresh():
    server = RevisionServer()
    server.fail_once = True
    persisted = []
    customer, entry = client(server, persisted)
    with customer:
        with pytest.raises(ManageError, match="connect"):
            customer.revise(entry, None, "请修改第二张表\n")
        saved = persisted[0]["receipts"][0]["revision_requests"]["single"]
        assert saved["expected_revision"] == 0
        assert uuid.UUID(saved["request_id"]).version == 4
        assert "请修改" not in json.dumps(saved, ensure_ascii=False)
        with pytest.raises(ManageError) as conflicting:
            customer.revise(entry, None, "另一份建议")
        assert conflicting.value.code == "revision_pending"
        result = customer.revise(entry, None, "请修改第二张表\n")
    assert len(server.requests) == 2 and server.requests[0] == server.requests[1]
    assert result["job"]["revision"]["current"] == 1
    assert result["entitlements"]["remaining"] == 1
    assert entry["revision_requests"] == {}
    assert "private-receipt-token" not in json.dumps(result)


def test_explicit_request_can_replay_when_current_round_is_already_queued():
    server = RevisionServer()
    customer, entry = client(server)
    identity = str(uuid.uuid4())
    with customer:
        for _ in range(2):
            customer.revise(
                entry, None, "同一份建议", request_id=identity, expected_revision=0
            )
    assert len(server.requests) == 2 and server.requests[0] == server.requests[1]


def test_revision_conflict_is_safe_and_keeps_the_same_request_identity():
    server = RevisionServer()
    server.rejection = 409
    customer, entry = client(server)
    with customer, pytest.raises(ManageError) as failure:
        customer.revise(entry, None, "private-message")
    assert failure.value.status == 409
    assert "private-message" not in str(failure.value)
    original = entry["revision_requests"]["single"]["request_id"]
    with customer_cli.CustomerClient(
        customer.data, transport=httpx.MockTransport(server)
    ) as retry:
        with pytest.raises(ManageError):
            retry.revise(entry, None, "private-message")
    assert entry["revision_requests"]["single"]["request_id"] == original
    assert server.requests[0] == server.requests[1]


def test_revision_new_request_must_be_explicit_after_a_conflict():
    server = RevisionServer()
    server.rejection = 409
    customer, entry = client(server)
    with customer:
        with pytest.raises(ManageError):
            customer.revise(entry, None, "first-message")
        old = entry["revision_requests"]["single"]["request_id"]
        server.rejection = None
        result = customer.revise(entry, None, "updated-message", new_request=True)
    assert result["request_id"] != old
    assert server.requests[-1]["message"] == "updated-message"


def test_receipt_summary_preserves_card_metadata_without_inputs_or_content():
    server = RevisionServer()
    summary = customer_cli._summary_receipt(server.receipt())
    assert summary["card_attributes"]["edit_passes"] == 2
    assert summary["job"]["deliveries"][0]["revision"] == 0
    assert "hidden-input" not in json.dumps(
        summary
    ) and "hidden-content" not in json.dumps(summary)
    batch = {
        "batch": True,
        "product": server.receipt()["product"],
        "items": [
            {
                "card_id": "card-a",
                "product": server.receipt()["product"],
                "job": server.job(),
                **metadata(),
            }
        ],
    }
    assert (
        customer_cli._summary_receipt(batch)["items"][0]["entitlements"][
            "attribute_key"
        ]
        == "edit_passes"
    )


def test_reveal_cache_keeps_old_version_files_after_revealing_a_new_version(tmp_path):
    server = RevisionServer()
    customer, entry = client(server)
    with customer:
        first = customer.reveal(entry, None, tmp_path / "first.json", revision=0)
        second = customer.reveal(entry, None, tmp_path / "second.json", revision=1)
        customer.download(entry, None, "file-0", tmp_path / "first.docx")
        customer.download(entry, None, "file-0", tmp_path / "explicit.docx", revision=0)
    assert first["revision"] == 0 and second["revision"] == 1
    assert len(entry["delivery_files"]["single"]) == 2
    assert (
        server.downloads[0]["file_id"] == "file-0"
        and "revision" not in server.downloads[0]
    )
    assert server.downloads[1]["revision"] == 0
    assert (tmp_path / "first.docx").read_bytes() == b"docx"
    assert stat.S_IMODE((tmp_path / "first.json").stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "argv",
    [
        ["revise", "rcpt_a", "--message-stdin", "--request-id", str(uuid.uuid1())],
        ["revise", "rcpt_a", "--message-stdin", "--expected-revision", "-1"],
        ["reveal", "rcpt_a", "--revision", "1.5", "--output", "out.json"],
    ],
)
def test_parser_rejects_invalid_revision_request_fields(argv):
    parser = argparse.ArgumentParser()
    customer_cli.add_parser(parser.add_subparsers())
    with pytest.raises(SystemExit):
        parser.parse_args(["customer", *argv])


@pytest.mark.parametrize("message", ["", " " * 100, "x" * 10001])
def test_bounded_message_input_rejects_empty_or_oversized_text(message, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(message))
    with pytest.raises(ManageError):
        customer_cli._read_revision_message()


def test_admin_card_attribute_override_keeps_credentials_in_private_output(tmp_path):
    merchant = OwnerMerchant()
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, merchant)
    attributes = tmp_path / "attributes.json"
    attributes.write_text(json.dumps({"edit_passes": 2, "color": "blue"}))
    result = admin(
        profile,
        merchant,
        "cards",
        "issue",
        "--product",
        "product-a",
        "--attributes-file",
        str(attributes),
    )
    requests = [
        json.loads(raw)
        for method, path, raw, _ in merchant.calls
        if method == "POST" and path in ("/api/admin/cards", "/api/manage/cards")
    ]
    assert requests[-1]["attributes"] == {"edit_passes": 2, "color": "blue"}
    assert "VERY-PRIVATE-CARD-CODE" not in json.dumps(result)


def test_admin_default_card_attributes_do_not_add_fields_to_legacy_body(tmp_path):
    merchant = OwnerMerchant()
    profile = tmp_path / "private" / "owner.json"
    authorize(profile, merchant)
    admin(profile, merchant, "cards", "issue", "--product", "product-a")
    request = [
        json.loads(raw)
        for method, path, raw, _ in merchant.calls
        if method == "POST" and path in ("/api/admin/cards", "/api/manage/cards")
    ][-1]
    assert "attributes" not in request


def test_manage_next_and_compact_job_preserve_revision_execution_context(
    tmp_path, monkeypatch
):
    merchant = AutomationMerchant()
    profile = tmp_path / "private" / "manage.json"
    login(merchant, profile, monkeypatch)

    def include_context(response):
        for item in response["items"]:
            item["job"].update(metadata(1))
            item["execution"].update(metadata(1))

    merchant.tamper_response = include_context
    result = run(merchant, profile, "next", "--product", "product-a")
    item = result["items"][0]
    assert item["job"]["revision"]["message"] == "调整表格"
    assert item["execution"]["card_attributes"]["edit_passes"] == 2
    assert compact_job(item["job"])["entitlements"]["remaining"] == 1
    assert "deliveries" not in item["job"] and "deliveries" not in item["execution"]
    detailed = run(merchant, profile, "next", "--product", "product-a", "--detail")[
        "items"
    ][0]
    assert detailed["job"]["deliveries"][0]["revision"] == 0
    assert "deliveries" not in compact_job(detailed["job"])


class RevisionUploadMerchant(AutomationMerchant):
    def __init__(self):
        super().__init__()
        self.job_outputs = [{"key": "document", "type": "file", "required": True}]

    def __call__(self, request):
        response = super().__call__(request)
        if request.url.path == "/api/manage/jobs":
            jobs = response.json()
            for job in jobs:
                job.update(
                    attempt=3,
                    revision={"current": 1, "message": "revise", "is_revision": True},
                )
            return httpx.Response(200, json=jobs)
        return response


@pytest.mark.parametrize("attempt", [None, "2"])
def test_revision_upload_rejects_missing_or_old_attempt_before_file_mutation(
    tmp_path, monkeypatch, attempt
):
    merchant = RevisionUploadMerchant()
    profile = tmp_path / "private" / "manage.json"
    login(merchant, profile, monkeypatch)
    document = tmp_path / "paper.docx"
    document.write_bytes(b"attachment-content")
    command = [
        "upload",
        "job-a",
        "--product",
        "product-a",
        "--field",
        "document",
        "--file",
        str(document),
    ]
    if attempt is not None:
        command += ["--attempt", attempt]
    with pytest.raises(ManageError) as failure:
        run(merchant, profile, *command)
    assert failure.value.code == ("invalid_input" if attempt is None else "stale_task")
    assert merchant.delivery_uploads == []
    assert not any(
        path == "/api/manage/files/upload" for _, path, _, _ in merchant.calls
    )


@pytest.mark.parametrize("command", ["upload", "complete"])
def test_revision_upload_preserves_the_claimed_attempt_in_multipart_and_batch(
    tmp_path, monkeypatch, command
):
    merchant = RevisionUploadMerchant()
    profile = tmp_path / "private" / "manage.json"
    login(merchant, profile, monkeypatch)
    document = tmp_path / "paper.docx"
    document.write_bytes(b"attachment-content")
    args = [command, "job-a", "--product", "product-a", "--attempt", "3"]
    if command == "upload":
        args += ["--field", "document", "--file", str(document)]
    else:
        args += ["--file", "document=" + str(document)]
    assert run(merchant, profile, *args)["ok"]
    assert merchant.delivery_uploads[-1]["fields"]["attempt"] == "3"
    if command == "complete":
        body = json.loads(
            [raw for _, path, _, raw in merchant.calls if path == "/api/manage/batch"][
                -1
            ]
        )
        assert body["attempt"] == 3


def test_admin_upload_parser_accepts_explicit_attempt(tmp_path):
    from test_owner_cli import arguments

    args = arguments(
        tmp_path / "owner.json",
        "upload",
        "job-a",
        "--product",
        "product-a",
        "--field",
        "document",
        "--file",
        str(tmp_path / "paper.docx"),
        "--attempt",
        "3",
    )
    assert args.attempt == 3


def test_sdk_delivery_context_is_frozen_and_separate_from_customer_parameters():
    context = metadata(1)
    task = Task(
        "task",
        "product",
        3,
        {"edit_passes": "999", "revision": "customer-spoof"},
        **context,
    )
    context["card_attributes"]["edit_passes"] = 99
    context["revision"]["message"] = "changed-after-construction"
    assert task.card_attributes["edit_passes"] == 2
    assert task.revision["message"] == "调整表格"
    with pytest.raises(TypeError):
        task.entitlements["label"]["zh-CN"] = "changed"
    assert task.idempotency_key == "task"
    assert task.delivery_idempotency_key == "task:revision:1"
    retry = Task("task", "product", 4, {}, **metadata(1))
    assert retry.delivery_idempotency_key == task.delivery_idempotency_key
    assert (
        Task("task", "product", 5, {}, **metadata(2)).delivery_idempotency_key
        != task.delivery_idempotency_key
    )


def test_sdk_script_envelope_accepts_revision_context(monkeypatch, capsys):
    envelope = {
        "id": "task",
        "product_id": "product",
        "attempt": 2,
        "params": {},
        **metadata(1),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(envelope)))

    def handler(task):
        assert task.revision["current"] == 1
        assert task.card_attributes["edit_passes"] == 2
        return Result.success(output={"content": "updated delivery"})

    run_script(handler)
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "succeeded"


def test_signed_webhook_event_retains_revision_context():
    event = {"id": "event-a", "type": "revision.requested", "data": metadata(1)}
    body = json.dumps(event).encode()
    timestamp, nonce, secret = (
        str(int(time.time())),
        "nonce-value",
        "private-webhook-key",
    )
    parsed = verify_event(
        secret,
        body,
        {
            "X-Extore-Timestamp": timestamp,
            "X-Extore-Nonce": nonce,
            "X-Extore-Signature": signature(secret, timestamp, nonce, body),
        },
    )
    assert parsed == event


@pytest.mark.parametrize(
    "revision",
    [{"current": True}, {"current": -1}, {"current": 1, "message": "x" * 10001}],
)
def test_sdk_rejects_invalid_delivery_round_context(revision):
    with pytest.raises(ValueError):
        Task("task", "product", 1, {}, revision=revision)

"""Customer CLI protocol tests use synthetic responses, receipts and files."""

import argparse
import base64
import io
import json
import stat

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from extore import customer_cli as remote
from extore.manage_client import ManageError

ORIGIN = "https://a.example"
TOKEN = "synthetic-private-receipt"


def arguments(*args, profile=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command")
    remote.add_parser(commands)
    prefix = ["customer"]
    if profile:
        prefix += ["--profile", str(profile)]
    return parser.parse_args([*prefix, *args])


def step(phase="input", **overrides):
    return {
        "enabled": True,
        "version": 1,
        "flow_epoch": 4,
        "revision": 12,
        "phase": phase,
        "deadline": 1234,
        "server_time": 1200,
        "actions": [
            "start"
            if phase == "await_start"
            else "continue"
            if phase == "display"
            else "answer"
        ],
        "current": {
            "id": "question",
            "kind": "input",
            "label": {"zh-CN": "问题"},
            "prompt": {"zh-CN": "开始"},
            "question": {"zh-CN": "请选择"},
            "fields": [{"key": "answer", "type": "boolean", "required": True}],
        },
        "shown": [
            {"key": "previous", "type": "text", "value": "private-previous-answer"}
        ],
        "private_runtime": "must-not-be-returned",
        **overrides,
    }


def receipt(flow):
    return {
        "product": {
            "id": "product-1",
            "name": "Synthetic flow",
            "parameters": [],
            "outputs": [],
        },
        "job": {"id": "job-1", "state": "waiting", "task_flow": flow},
    }


@pytest.fixture
def saved(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "customer.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "receipts": [
                    {"id": "receipt-1", "origin": ORIGIN, "token": TOKEN, "created": 0}
                ],
            }
        )
    )
    path.chmod(0o600)
    return path


def test_flow_answer_normalizes_false_and_keeps_private_values_out_of_profile(
    saved, monkeypatch
):
    calls = []

    def relay(request):
        calls.append(request)
        if request.url.path == "/api/receipt":
            return httpx.Response(200, json=receipt(step()))
        assert request.url.path == "/api/task-flow/answer"
        body = json.loads(request.content)
        assert body == {
            "token": TOKEN,
            "flow_epoch": 4,
            "expected_revision": 12,
            "values": {"answer": "false"},
        }
        return httpx.Response(
            200,
            json={
                "id": "job-1",
                "state": "queued",
                "task_flow": step(
                    "queued", actions=[], params={"secret": "must-not-echo"}
                ),
            },
        )

    monkeypatch.setattr("sys.stdin", io.StringIO('{"answer":false}'))
    result = remote.execute(
        arguments("flow", "answer", "receipt-1", "--values-stdin", profile=saved),
        transport=httpx.MockTransport(relay),
    )
    assert result["job"]["task_flow"]["phase"] == "queued"
    for secret in (
        TOKEN,
        "private-previous-answer",
        "must-not-echo",
        "must-not-be-returned",
    ):
        assert secret not in json.dumps(result)
    for secret in ("private-previous-answer", "must-not-echo", "must-not-be-returned"):
        assert secret not in saved.read_text()
    assert len(calls) == 2


@pytest.mark.parametrize(
    "option,value", [("--flow-epoch", "3"), ("--expected-revision", "11")]
)
def test_stale_step_is_rejected_before_mutation(saved, option, value):
    calls = []

    def relay(request):
        calls.append(request.url.path)
        assert request.url.path == "/api/receipt"
        return httpx.Response(200, json=receipt(step("await_start")))

    with pytest.raises(ManageError, match="step changed") as error:
        remote.execute(
            arguments("flow", "start", "receipt-1", option, value, profile=saved),
            transport=httpx.MockTransport(relay),
        )
    assert error.value.code == "stale_flow"
    assert calls == ["/api/receipt"]


def test_unknown_fields_do_not_start_uploads_or_mutate_tasks(saved, monkeypatch):
    calls = []

    def relay(request):
        calls.append(request.url.path)
        assert request.url.path == "/api/receipt"
        return httpx.Response(200, json=receipt(step()))

    monkeypatch.setattr("sys.stdin", io.StringIO('{"unknown":"private-input"}'))
    with pytest.raises(ManageError, match="Unknown step field") as error:
        remote.execute(
            arguments("flow", "answer", "receipt-1", "--values-stdin", profile=saved),
            transport=httpx.MockTransport(relay),
        )
    assert "private-input" not in str(error.value)
    assert calls == ["/api/receipt"]


def test_flow_view_explicitly_shows_only_current_public_projection(saved):
    result = remote.execute(
        arguments("flow", "view", "receipt-1", profile=saved),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=receipt(step()))
        ),
    )
    assert result["task_flow"]["shown"][0]["value"] == "private-previous-answer"
    assert "private_runtime" not in json.dumps(result)
    assert "private-previous-answer" not in saved.read_text()
    assert TOKEN not in json.dumps(result)


def test_flow_view_can_save_answers_to_a_new_private_file(saved, tmp_path):
    target = tmp_path / "step.json"
    result = remote.execute(
        arguments("flow", "view", "receipt-1", "--output", str(target), profile=saved),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=receipt(step()))
        ),
    )
    assert "private-previous-answer" not in json.dumps(result)
    assert (
        json.loads(target.read_text())["shown"][0]["value"] == "private-previous-answer"
    )
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(ManageError, match="already exists"):
        remote.execute(
            arguments(
                "flow", "view", "receipt-1", "--output", str(target), profile=saved
            ),
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=receipt(step()))
            ),
        )


def test_repeated_image_uploads_are_scoped_and_use_one_limit_request(saved, tmp_path):
    files = [tmp_path / "one.png", tmp_path / "two.png"]
    for file in files:
        file.write_bytes(b"synthetic image")
    calls, uploaded = [], []
    flow = step(
        current={
            "id": "pictures",
            "kind": "input",
            "fields": [
                {"key": "images", "type": "images", "required": True, "max_items": 2}
            ],
        }
    )

    def relay(request):
        calls.append(request.url.path)
        if request.url.path == "/api/receipt":
            return httpx.Response(200, json=receipt(flow))
        if request.url.path == "/api/upload-limits":
            return httpx.Response(200, json={"max_file_bytes": 1024})
        if request.url.path == "/api/files/upload":
            raw = request.read()
            assert b'name="flow_epoch"\r\n\r\n4' in raw
            assert b'name="expected_revision"\r\n\r\n12' in raw
            assert b'name="node_id"\r\n\r\npictures' in raw
            fid = f"00000000-0000-0000-0000-{len(uploaded) + 1:012d}"
            uploaded.append(fid)
            return httpx.Response(
                200,
                json={
                    "id": fid,
                    "field_key": "images",
                    "filename": "source.png",
                    "size": 15,
                    "kind": "input",
                },
            )
        assert request.url.path == "/api/task-flow/answer"
        body = json.loads(request.content)
        assert json.loads(body["values"]["images"]) == uploaded
        return httpx.Response(
            200,
            json={
                "id": "job-1",
                "state": "queued",
                "task_flow": step("queued", actions=[]),
            },
        )

    remote.execute(
        arguments(
            "flow",
            "answer",
            "receipt-1",
            "--file",
            f"images={files[0]}",
            "--file",
            f"images={files[1]}",
            profile=saved,
        ),
        transport=httpx.MockTransport(relay),
    )
    assert calls.count("/api/upload-limits") == 1
    assert len(uploaded) == 2
    assert (
        len(
            json.loads(saved.read_text())["receipts"][0]["inputs"]["single"]["images"][
                "files"
            ]
        )
        == 2
    )


def test_effective_server_limit_preflights_all_files_before_first_upload(
    saved, tmp_path
):
    first, second = tmp_path / "one.png", tmp_path / "two.png"
    first.write_bytes(b"1")
    second.write_bytes(b"12345")
    calls = []
    flow = step(
        current={
            "id": "pictures",
            "kind": "input",
            "fields": [
                {"key": "images", "type": "images", "required": True, "max_items": 2}
            ],
        }
    )

    def relay(request):
        calls.append(request.url.path)
        if request.url.path == "/api/receipt":
            return httpx.Response(200, json=receipt(flow))
        assert request.url.path == "/api/upload-limits"
        return httpx.Response(200, json={"max_file_bytes": 4})

    with pytest.raises(ManageError, match="4 bytes"):
        remote.execute(
            arguments(
                "flow",
                "answer",
                "receipt-1",
                "--file",
                f"images={first}",
                "--file",
                f"images={second}",
                profile=saved,
            ),
            transport=httpx.MockTransport(relay),
        )
    assert calls == ["/api/receipt", "/api/upload-limits"]


def test_new_simple_redemption_accepts_repeated_images_without_extra_receipt_fetches(
    saved, tmp_path
):
    files = [tmp_path / "one.png", tmp_path / "two.png"]
    for file in files:
        file.write_bytes(b"image")
    calls, ids = [], []
    value = {
        "product": {
            "id": "product-1",
            "name": "Simple images",
            "parameters": [{"key": "pictures", "type": "images", "max_items": 2}],
            "outputs": [],
        },
        "job": None,
    }

    def relay(request):
        calls.append(request.url.path)
        if request.url.path == "/api/receipt":
            return httpx.Response(200, json=value)
        if request.url.path == "/api/upload-limits":
            return httpx.Response(200, json={"max_file_bytes": 1024})
        if request.url.path == "/api/files/upload":
            ids.append(f"00000000-0000-0000-0000-{len(ids) + 1:012d}")
            return httpx.Response(200, json={"id": ids[-1]})
        assert json.loads(json.loads(request.content)["params"]["pictures"]) == ids
        return httpx.Response(200, json={"id": "job-1", "state": "queued"})

    with remote.CustomerClient(
        {"version": 1, "receipts": []}, transport=httpx.MockTransport(relay)
    ) as client:
        client.redeem(
            {"id": "receipt-1", "origin": ORIGIN, "token": TOKEN},
            {},
            attachments=[f"pictures={file}" for file in files],
        )
    assert calls.count("/api/receipt") == 1
    assert calls.count("/api/upload-limits") == 1


def routed_fixture():
    from extore import proxy_routes as proxy

    key = Ed25519PrivateKey.generate()
    route = {
        "route_id": "a" * 32,
        "issuer_id": "b" * 32,
        "origin": "https://b.example",
        "path": "/",
        "public_key": base64.urlsafe_b64encode(key.public_key().public_bytes_raw())
        .decode()
        .rstrip("="),
    }
    secret = "A" * 32
    signature = (
        base64.urlsafe_b64encode(key.sign(proxy.signed_message(route, secret)))
        .decode()
        .rstrip("=")
    )
    return route, f"EXR1.{route['route_id']}.{secret}.{route['issuer_id']}.{signature}"


def test_proxy_exchange_keeps_complete_code_off_a_and_saves_b_receipt(
    tmp_path, monkeypatch
):
    route, code = routed_fixture()
    calls = []
    profile = tmp_path / "private" / "customer.json"

    def relay(request):
        calls.append(request)
        if request.url.host == "a.example":
            assert request.method == "GET" and request.url.path == "/api/proxy/routes"
            assert not request.content
            return httpx.Response(200, json=[route])
        assert str(request.url) == "https://b.example/api/exchange"
        assert json.loads(request.content)["code"] == code
        return httpx.Response(200, json={"token": TOKEN, **receipt(None)})

    monkeypatch.setattr("sys.stdin", io.StringIO(code))
    result = remote.execute(
        arguments("exchange", "--origin", ORIGIN, "--codes-stdin", profile=profile),
        transport=httpx.MockTransport(relay),
    )
    assert result["origin"] == "https://b.example"
    assert len(calls) == 2
    assert code not in json.dumps(result) and code not in profile.read_text()
    assert all(
        code.encode() not in request.content
        for request in calls
        if request.url.host == "a.example"
    )


def test_proxy_route_tampering_fails_before_posting_any_secret(tmp_path, monkeypatch):
    route, code = routed_fixture()
    route["origin"] = "https://attacker.example"
    calls = []

    def relay(request):
        calls.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json=[route])

    monkeypatch.setattr("sys.stdin", io.StringIO(code))
    with pytest.raises(ManageError) as error:
        remote.execute(
            arguments(
                "exchange",
                "--origin",
                ORIGIN,
                "--codes-stdin",
                profile=tmp_path / "private" / "customer.json",
            ),
            transport=httpx.MockTransport(relay),
        )
    assert error.value.code == "invalid_route"
    assert code not in str(error.value)
    assert len(calls) == 1


def test_unknown_proxy_protocol_never_falls_back_to_posting_a(tmp_path, monkeypatch):
    routed_fixture()
    monkeypatch.setattr("sys.stdin", io.StringIO("EXR2.unknown"))
    with pytest.raises(ManageError) as error:
        remote.execute(
            arguments(
                "exchange",
                "--origin",
                ORIGIN,
                "--codes-stdin",
                profile=tmp_path / "private" / "customer.json",
            ),
            transport=httpx.MockTransport(
                lambda _: pytest.fail("Unknown protocol must not cause requests")
            ),
        )
    assert error.value.code == "invalid_route"


def test_restart_cas_reopens_an_eligible_failed_flow_without_starting_its_timer(saved):
    calls = []
    initial = receipt(step("ended", actions=[]))
    initial["job"].update(state="failed", can_retry=True)

    def relay(request):
        calls.append(request.url.path)
        if request.url.path == "/api/receipt":
            return httpx.Response(200, json=initial)
        assert request.url.path == "/api/task-flow/restart"
        assert json.loads(request.content) == {
            "token": TOKEN,
            "flow_epoch": 4,
            "expected_revision": 12,
        }
        return httpx.Response(
            200,
            json={
                "id": "job-1",
                "state": "waiting",
                "attempt": 2,
                "task_flow": step(
                    "await_start", flow_epoch=5, revision=13, deadline=None
                ),
            },
        )

    result = remote.execute(
        arguments("flow", "restart", "receipt-1", profile=saved),
        transport=httpx.MockTransport(relay),
    )
    assert result["job"]["attempt"] == 2
    assert result["job"]["task_flow"]["deadline"] is None
    assert result["job"]["task_flow"]["actions"] == ["start"]
    assert calls == ["/api/receipt", "/api/task-flow/restart"]


@pytest.mark.parametrize(
    "state", ["queued", "processing", "succeeded", "destroyed", "rejected"]
)
def test_restart_does_not_overwrite_live_or_terminal_deliveries(saved, state):
    initial = receipt(step("ended", actions=[]))
    initial["job"].update(state=state, can_retry=True)
    calls = []

    def relay(request):
        calls.append(request.url.path)
        assert request.url.path == "/api/receipt"
        return httpx.Response(200, json=initial)

    with pytest.raises(ManageError, match="does not accept"):
        remote.execute(
            arguments("flow", "restart", "receipt-1", profile=saved),
            transport=httpx.MockTransport(relay),
        )
    assert calls == ["/api/receipt"]


def test_mixed_routes_save_successful_receipts_and_report_failures_without_credentials(
    tmp_path, monkeypatch
):
    route, code = routed_fixture()
    legacy = "SYNTHETIC-LEGACY-CODE"
    profile = tmp_path / "private" / "customer.json"
    calls = []

    def relay(request):
        calls.append(request)
        if request.url.path == "/api/proxy/routes":
            return httpx.Response(200, json=[route])
        if request.url.host == "a.example":
            assert json.loads(request.content)["code"] == legacy
            assert code.encode() not in request.content
            return httpx.Response(403, text=legacy)
        assert request.url.host == "b.example"
        assert json.loads(request.content)["code"] == code
        return httpx.Response(200, json={"token": TOKEN, **receipt(None)})

    monkeypatch.setattr("sys.stdin", io.StringIO(legacy + "\n" + code))
    result = remote.execute(
        arguments("exchange", "--origin", ORIGIN, "--codes-stdin", profile=profile),
        transport=httpx.MockTransport(relay),
    )
    assert result["ok"] is False
    assert result["receipts"][0]["origin"] == "https://b.example"
    assert result["failed"] == [{"origin": ORIGIN, "code": "http_error", "status": 403}]
    saved = json.loads(profile.read_text())["receipts"]
    assert len(saved) == 1 and saved[0]["origin"] == "https://b.example"
    for private in (legacy, code, TOKEN):
        assert private not in json.dumps(result)
    assert legacy not in profile.read_text() and code not in profile.read_text()
    assert [str(request.url) for request in calls] == [
        ORIGIN + "/api/proxy/routes",
        ORIGIN + "/api/exchange",
        "https://b.example/api/exchange",
    ]


def test_cancel_requires_confirmation_and_does_not_send_input_values(saved):
    with pytest.raises(SystemExit):
        arguments("flow", "cancel", "receipt-1", profile=saved)
    calls = []

    def relay(request):
        calls.append(request.url.path)
        if request.url.path == "/api/receipt":
            return httpx.Response(200, json=receipt(step("processing", actions=[])))
        assert request.url.path == "/api/task-flow/cancel"
        assert json.loads(request.content) == {
            "token": TOKEN,
            "flow_epoch": 4,
            "expected_revision": 12,
        }
        return httpx.Response(
            200,
            json={
                "id": "job-1",
                "state": "failed",
                "can_retry": False,
                "task_flow": step("ended", actions=[], flow_epoch=5),
            },
        )

    result = remote.execute(
        arguments("flow", "cancel", "receipt-1", "--confirm", profile=saved),
        transport=httpx.MockTransport(relay),
    )
    assert result["job"]["can_retry"] is False
    assert calls == ["/api/receipt", "/api/task-flow/cancel"]

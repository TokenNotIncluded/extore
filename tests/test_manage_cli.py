import argparse
import io
import json
import os
import stat
import subprocess
import sys
import time

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from extore import cli
from extore import manage_client as remote

ORIGIN = "https://merchant.example"


def arguments(*argv, profile=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command")
    remote.add_parser(commands)
    args = ["manage"]
    if profile:
        args += ["--profile", str(profile)]
    return parser.parse_args([*args, *argv])


class MockMerchant:
    def __init__(self):
        self.devices = {}
        self.sessions = {}
        self.links = {
            "first": ("product-a", ["queue.view", "queue.process", "queue.retry"]),
            "second": ("product-b", ["queue.view", "queue.process"]),
            "view": ("product-c", ["queue.view"]),
            "process": ("product-c", ["queue.process"]),
        }
        self.calls = []
        self.fail_after_binding = False
        self.rejected_devices = set()
        self.upload_contents = []
        self.upload_unauthorized_once = False
        self.bad_download = False
        self.queue_queries = []

    def __call__(self, request):
        body = request.read()
        self.calls.append(
            (
                request.method,
                request.url.path,
                request.headers.get("Authorization"),
                body,
            )
        )
        path = request.url.path
        if path == "/api/upload-limits":
            return httpx.Response(
                200,
                json={
                    "max_file_bytes": remote.MAX_UPLOAD_BYTES,
                    "max_card_bytes": 100 * 1024 * 1024,
                    "max_card_files": 100,
                },
            )
        if path == "/api/cli/authorize":
            data = json.loads(body)
            fragment = data["token"].split("#", 1)[1]
            product, permissions = self.links[fragment]
            proof = "\n".join(
                (
                    "extore-cli-bind-v1",
                    str(request.url.copy_with(path="", query=None)),
                    data["token"],
                    data["public_key"],
                )
            )
            Ed25519PublicKey.from_public_bytes(
                remote._unb64(data["public_key"])
            ).verify(remote._unb64(data["signature"]), proof.encode())
            existing = next(
                (
                    device
                    for device in self.devices.values()
                    if device["public_key"] == data["public_key"]
                ),
                None,
            )
            device_id = (
                existing["device_id"]
                if existing
                else "device-" + str(len(self.devices) + 1)
            )
            self.devices[device_id] = {
                "device_id": device_id,
                "public_key": data["public_key"],
                "product_id": product,
                "permissions": permissions,
                "client_name": data["client_name"],
            }
            if self.fail_after_binding:
                self.fail_after_binding = False
                raise httpx.ReadError("synthetic lost response", request=request)
            return httpx.Response(
                200,
                json={
                    "device_id": device_id,
                    "product_id": product,
                    "already_authorized": bool(existing),
                },
            )
        if path == "/api/cli/challenge":
            data = json.loads(body)
            if data["device_id"] in self.rejected_devices:
                return httpx.Response(403)
            return httpx.Response(
                200,
                json={
                    "challenge_id": "challenge-id",
                    "challenge": "challenge-bytes",
                    "expires": time.time() + 300,
                },
            )
        if path == "/api/cli/session" and request.method == "POST":
            data = json.loads(body)
            device = self.devices[data["device_id"]]
            proof = "\n".join(
                (
                    "extore-cli-session-v1",
                    str(request.url.copy_with(path="", query=None)),
                    data["device_id"],
                    data["challenge_id"],
                    "challenge-bytes",
                )
            )
            Ed25519PublicKey.from_public_bytes(
                remote._unb64(device["public_key"])
            ).verify(remote._unb64(data["signature"]), proof.encode())
            token = "synthetic-bearer-" + str(len(self.sessions) + 1)
            self.sessions[token] = device
            return httpx.Response(
                200,
                json={
                    "access_token": token,
                    "expires": time.time() + 28800,
                    "device_id": device["device_id"],
                    "product_id": device["product_id"],
                    "permissions": device["permissions"],
                    "session_id": "session",
                },
            )
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        device = self.sessions.get(token)
        if not device:
            return httpx.Response(401)
        if device["device_id"] in self.rejected_devices:
            return httpx.Response(403)
        if path == "/api/cli/status":
            return httpx.Response(
                200,
                json={
                    **device,
                    "link_id": "link-" + device["device_id"],
                    "link_name": "Synthetic link",
                    "link_expires": time.time() + 86400,
                    "expires": time.time() + 28800,
                },
            )
        if path == "/api/cli/session" and request.method == "DELETE":
            self.sessions.pop(token)
            return httpx.Response(200, json={"ok": True})
        if path == "/api/manage/products":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": device["product_id"],
                        "name": "Synthetic product",
                        "parameters": [],
                        "outputs": [],
                    }
                ],
            )
        if path == "/api/manage/jobs":
            self.queue_queries.append(dict(request.url.params))
            assert request.url.params["product_id"] == device["product_id"]
            job_id = request.url.params.get("job_id", "job-" + device["product_id"])
            return httpx.Response(
                200,
                json=[
                    {
                        "id": job_id,
                        "product_id": device["product_id"],
                        "state": "queued",
                        "message": "Synthetic task",
                        "params": {"request": "sensitive task input"},
                        "parameters": [{"key": "request", "type": "text"}],
                        "outputs": [],
                        "files": [],
                    }
                ],
            )
        if path == "/api/manage/batch":
            data = json.loads(body)
            assert data["product_id"] == device["product_id"]
            return httpx.Response(200, json={"ok": True})
        if path == "/api/manage/files":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "file",
                        "job_id": "job",
                        "size": 7,
                        "filename": "unsafe-from-server",
                        "kind": "input",
                    }
                ],
            )
        if path == "/api/manage/files/file/download":
            return httpx.Response(
                200, content=b"bad" if self.bad_download else b"payload"
            )
        if path == "/api/manage/files/upload":
            self.upload_contents.append(body)
            if self.upload_unauthorized_once:
                self.upload_unauthorized_once = False
                return httpx.Response(401)
            return httpx.Response(200, json={"id": "file", "job_id": "job", "size": 7})
        raise AssertionError(path)


@pytest.fixture
def merchant():
    return MockMerchant()


@pytest.fixture
def profile(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    return root / "cli.json"


def login(merchant, profile, monkeypatch, link="first"):
    monkeypatch.setattr(sys, "stdin", io.StringIO(ORIGIN + "/staff#" + link + "\n"))
    return remote.execute(
        arguments("login", "--link-stdin", profile=profile),
        transport=httpx.MockTransport(merchant),
    )


def run(merchant, profile, *argv):
    return remote.execute(
        arguments(*argv, profile=profile), transport=httpx.MockTransport(merchant)
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["manage", "--help"],
        ["manage", "login", "--help"],
        ["manage", "queues", "--help"],
        ["manage", "reject", "job", "--reason", "reason"],
    ],
)
def test_remote_argument_only_commands_do_not_initialize_server_data(argv, tmp_path):
    data = tmp_path / "unused-server-data"
    result = subprocess.run(
        [sys.executable, "-m", "extore.cli", *argv],
        env={
            **os.environ,
            "EXTORE_DATA": str(data),
            "EXTORE_CLI_CONFIG": str(tmp_path / "unused-private" / "cli.json"),
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == (2 if argv[-1] == "reason" else 0)
    assert not data.exists()
    assert not (tmp_path / "unused-private").exists()


@pytest.mark.parametrize(
    "value",
    [
        "http://merchant.example/staff#first",
        "https://user:secret@merchant.example/staff#first",
        "https://merchant.example/staff?next=bad#first",
        "https://merchant.example/staff",
        "https://merchant.example/evil#first",
        "https://merchant.example/staff#has space",
        "not-a-link",
    ],
)
def test_invalid_or_insecure_links_are_rejected_before_network(
    value, merchant, profile, monkeypatch
):
    monkeypatch.setattr(sys, "stdin", io.StringIO(value))
    with pytest.raises(remote.ManageError):
        run(merchant, profile, "login", "--link-stdin")
    assert merchant.calls == []


def test_login_uses_private_key_proofs_and_never_returns_credentials(
    merchant, profile, monkeypatch
):
    result = login(merchant, profile, monkeypatch)
    stored = json.loads(profile.read_text())
    grant = stored["grants"][0]
    assert stat.S_IMODE(profile.stat().st_mode) == 0o600
    assert stat.S_IMODE(profile.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((profile.parent / "cli.json.lock").stat().st_mode) == 0o600
    public = json.dumps(result)
    assert grant["private_key"] not in public and grant["access_token"] not in public
    assert "#first" not in profile.read_text()
    assert result["grant"]["product_id"] == "product-a"
    assert len(merchant.devices) == 1
    login(merchant, profile, monkeypatch)
    assert (
        len([call for call in merchant.calls if call[1] == "/api/cli/authorize"]) == 1
    )


def test_interrupted_authorize_persists_key_before_request_and_reuses_it(
    merchant, profile, monkeypatch
):
    original = merchant.__call__

    def transport(request):
        if request.url.path == "/api/cli/authorize":
            saved = json.loads(profile.read_text())["grants"][0]
            assert "private_key" in saved and "device_id" not in saved
        return original(request)

    merchant.fail_after_binding = True
    monkeypatch.setattr(sys, "stdin", io.StringIO(ORIGIN + "/cli#first"))
    with pytest.raises(remote.ManageError, match="connect"):
        remote.execute(
            arguments("login", "--link-stdin", profile=profile),
            transport=httpx.MockTransport(transport),
        )
    pending = json.loads(profile.read_text())["grants"][0]
    assert "private_key" in pending and "device_id" not in pending
    result = login(merchant, profile, monkeypatch)
    assert result["already_authorized"]
    assert len(merchant.devices) == 1
    assert (
        json.loads(profile.read_text())["grants"][0]["private_key"]
        == pending["private_key"]
    )


def test_multiple_product_queues_are_compact_and_grant_isolation_is_preserved(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    login(merchant, profile, monkeypatch, "second")
    result = run(merchant, profile, "queues", "--all")
    assert {queue["product_id"] for queue in result["queues"]} == {
        "product-a",
        "product-b",
    }
    assert "sensitive task input" not in json.dumps(result)
    queries = [call for call in merchant.calls if call[1] == "/api/manage/jobs"]
    assert len({call[2] for call in queries}) == 2
    detailed = run(merchant, profile, "job", "job-product-a", "--product", "product-a")
    assert detailed["job"]["params"]["request"] == "sensitive task input"
    with pytest.raises(remote.ManageError, match="single grant"):
        run(merchant, profile, "claim", "job", "--product", "unknown-product")


def test_revoked_grant_does_not_hide_other_product_queues(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    login(merchant, profile, monkeypatch, "second")
    merchant.rejected_devices.add("device-1")
    result = run(merchant, profile, "queues", "--all")
    assert result["ok"] is False and len(result["errors"]) == 1
    assert [item["product_id"] for item in result["queues"]] == ["product-b"]
    selected = run(merchant, profile, "jobs", "--product", "product-b")
    assert selected["ok"] is True


def test_scopes_from_multiple_links_are_never_combined(merchant, profile, monkeypatch):
    login(merchant, profile, monkeypatch, "view")
    login(merchant, profile, monkeypatch, "process")
    with (
        remote.private_profile(profile) as data,
        remote.ManageClient(data, transport=httpx.MockTransport(merchant)) as client,
    ):
        with pytest.raises(remote.ManageError, match="single grant"):
            client.grant(
                product="product-c", permissions=("queue.view", "queue.process")
            )


def test_expired_access_token_renews_device_key_without_rebinding(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    data = json.loads(profile.read_text())
    data["grants"][0]["expires"] = 0
    with remote.private_profile(profile) as saved:
        saved.update(data)
    run(merchant, profile, "queues", "--all")
    assert (
        len([call for call in merchant.calls if call[1] == "/api/cli/authorize"]) == 1
    )
    assert (
        len([call for call in merchant.calls if call[1] == "/api/cli/challenge"]) == 2
    )


def test_revoked_session_refreshes_without_using_link_again(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    merchant.sessions.clear()
    assert run(merchant, profile, "queues", "--all")["ok"]
    assert (
        len([call for call in merchant.calls if call[1] == "/api/cli/authorize"]) == 1
    )


@pytest.mark.parametrize(
    "command,flags,action",
    [
        ("claim", [], "claim"),
        ("progress", ["--progress", "40", "--completed-step", "step"], "progress"),
        ("request-changes", ["--reason", "请补充文献范围"], "request_changes"),
        ("reject", ["--reason", "超出服务范围"], "reject"),
        ("retry", [], "retry"),
        ("fail", ["--retryable", "--message", "temporary failure"], "fail"),
    ],
)
def test_cli_writes_use_explicit_product_and_real_batch_contract(
    command, flags, action, merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    result = run(merchant, profile, command, "job", "--product", "product-a", *flags)
    assert result["ok"]
    body = json.loads(
        [call for call in merchant.calls if call[1] == "/api/manage/batch"][-1][3]
    )
    assert (
        body["product_id"] == "product-a"
        and body["ids"] == ["job"]
        and body["action"] == action
    )
    if "--reason" in flags:
        assert body["message"] == flags[1]


@pytest.mark.parametrize("command", ["request-changes", "reject"])
def test_empty_rejection_reason_cannot_reach_queue_backend(
    command, merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    with pytest.raises(remote.ManageError, match="non-empty"):
        run(
            merchant,
            profile,
            command,
            "job",
            "--product",
            "product-a",
            "--reason",
            "   ",
        )
    assert not any(call[1] == "/api/manage/batch" for call in merchant.calls)


def test_complete_uses_structured_output_file(merchant, profile, monkeypatch, tmp_path):
    login(merchant, profile, monkeypatch)
    output = tmp_path / "output.json"
    output.write_text('{"result":"完成"}')
    run(
        merchant,
        profile,
        "succeed",
        "job",
        "--product",
        "product-a",
        "--output-file",
        str(output),
    )
    body = json.loads(
        [call for call in merchant.calls if call[1] == "/api/manage/batch"][-1][3]
    )
    assert body["action"] == "succeed" and body["output"] == {"result": "完成"}


@pytest.mark.parametrize("content", ["[]", '{"result": 3}', "malformed"])
def test_invalid_output_schema_file_does_not_submit(
    content, merchant, profile, monkeypatch, tmp_path
):
    login(merchant, profile, monkeypatch)
    output = tmp_path / "output.json"
    output.write_text(content)
    with pytest.raises(remote.ManageError, match="object"):
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
    assert not any(call[1] == "/api/manage/batch" for call in merchant.calls)


def test_upload_replays_complete_file_after_session_refresh(
    merchant, profile, monkeypatch, tmp_path
):
    login(merchant, profile, monkeypatch)
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    merchant.upload_unauthorized_once = True
    assert run(
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
    )["ok"]
    assert len(merchant.upload_contents) == 2
    assert all(b"payload" in body for body in merchant.upload_contents)


def test_oversized_upload_is_refused_locally(merchant, profile, monkeypatch, tmp_path):
    login(merchant, profile, monkeypatch)
    source = tmp_path / "source.bin"
    source.touch()
    with source.open("wb") as output:
        output.truncate(remote.MAX_UPLOAD_BYTES + 1)
    with pytest.raises(remote.ManageError, match="larger than"):
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
        )
    assert not merchant.upload_contents


def test_download_checks_job_scope_and_private_new_destination(
    merchant, profile, monkeypatch, tmp_path
):
    login(merchant, profile, monkeypatch)
    target = tmp_path / "download.bin"
    result = run(
        merchant,
        profile,
        "download",
        "job",
        "--product",
        "product-a",
        "--file-id",
        "file",
        "--output",
        str(target),
    )
    assert result["size"] == 7 and target.read_bytes() == b"payload"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(remote.ManageError, match="exists"):
        run(
            merchant,
            profile,
            "download",
            "job",
            "--product",
            "product-a",
            "--file-id",
            "file",
            "--output",
            str(target),
        )
    with pytest.raises(remote.ManageError, match="selected task"):
        run(
            merchant,
            profile,
            "download",
            "job",
            "--product",
            "product-a",
            "--file-id",
            "foreign-file",
            "--output",
            str(tmp_path / "other"),
        )


def test_incomplete_download_is_removed(merchant, profile, monkeypatch, tmp_path):
    login(merchant, profile, monkeypatch)
    merchant.bad_download = True
    target = tmp_path / "partial.bin"
    with pytest.raises(remote.ManageError, match="file size"):
        run(
            merchant,
            profile,
            "download",
            "job",
            "--product",
            "product-a",
            "--file-id",
            "file",
            "--output",
            str(target),
        )
    assert not target.exists()


def test_logout_selected_product_keeps_other_product_key(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    login(merchant, profile, monkeypatch, "second")
    run(merchant, profile, "logout", "--product", "product-a")
    saved = json.loads(profile.read_text())["grants"]
    assert [grant["product_id"] for grant in saved] == ["product-b"]
    assert run(merchant, profile, "queues", "--all")["ok"]


@pytest.mark.parametrize("unsafe", ["directory", "file", "symlink"])
def test_unsafe_profile_storage_is_rejected(
    unsafe, merchant, profile, monkeypatch, tmp_path
):
    if unsafe == "directory":
        profile.parent.chmod(0o755)
    elif unsafe == "file":
        profile.write_text('{"version":1,"grants":[]}')
        profile.chmod(0o644)
    else:
        target = tmp_path / "existing"
        target.write_text("do not overwrite")
        profile.symlink_to(target)
    with pytest.raises(remote.ManageError, match="credential"):
        login(merchant, profile, monkeypatch)
    assert merchant.calls == []
    if unsafe == "symlink":
        assert target.read_text() == "do not overwrite"


def test_redirect_cannot_forward_binding_secret_to_other_origin(profile, monkeypatch):
    calls = []

    def redirect(request):
        calls.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://evil.example/collect"})

    monkeypatch.setattr(sys, "stdin", io.StringIO(ORIGIN + "/staff#first"))
    with pytest.raises(remote.ManageError) as rejected:
        remote.execute(
            arguments("login", "--link-stdin", profile=profile),
            transport=httpx.MockTransport(redirect),
        )
    assert rejected.value.status == 307
    assert calls == [ORIGIN + "/api/cli/authorize"]


def test_cli_errors_are_compact_and_never_print_private_credentials(
    monkeypatch, capsys, profile
):
    monkeypatch.setattr(
        cli, "init", lambda: pytest.fail("remote CLI cannot initialize DATA")
    )
    with pytest.raises(SystemExit) as exited:
        cli.main(["manage", "--profile", str(profile), "queues", "--all"])
    assert exited.value.code == 1
    captured = capsys.readouterr()
    assert not captured.out and json.loads(captured.err)["code"] == "no_auth"
    assert "Traceback" not in captured.err


def test_pending_binding_is_reported_without_blocking_existing_grants(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    merchant.fail_after_binding = True
    monkeypatch.setattr(sys, "stdin", io.StringIO(ORIGIN + "/staff#second"))
    with pytest.raises(remote.ManageError):
        run(merchant, profile, "login", "--link-stdin")
    result = run(merchant, profile, "queues", "--all")
    assert [queue["product_id"] for queue in result["queues"]] == ["product-a"]
    assert [error["code"] for error in result["errors"]] == ["pending_binding"]
    assert run(merchant, profile, "jobs", "--product", "product-a")["ok"]
    assert run(merchant, profile, "logout", "--all")["ok"]
    assert json.loads(profile.read_text())["grants"] == []


def test_queue_summaries_request_server_compact_view_but_detail_does_not(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    run(merchant, profile, "queues", "--all")
    run(merchant, profile, "jobs", "--product", "product-a")
    run(merchant, profile, "job", "job", "--product", "product-a")
    assert merchant.queue_queries[-3]["compact"] == "true"
    assert merchant.queue_queries[-2]["compact"] == "true"
    assert "compact" not in merchant.queue_queries[-1]
    assert merchant.queue_queries[-1]["job_id"] == "job"


def test_step_plan_file_is_passed_to_queue_contract(
    merchant, profile, monkeypatch, tmp_path
):
    login(merchant, profile, monkeypatch)
    plan = tmp_path / "steps.json"
    steps = [{"id": "research", "label": {"zh-CN": "检索资料", "en": "Research"}}]
    plan.write_text(json.dumps(steps))
    run(
        merchant,
        profile,
        "claim",
        "job",
        "--product",
        "product-a",
        "--steps-file",
        str(plan),
    )
    body = json.loads(
        [call for call in merchant.calls if call[1] == "/api/manage/batch"][-1][3]
    )
    assert body["progress_steps"] == steps


def test_multiple_server_origin_scope_requires_explicit_selection(
    merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    monkeypatch.setattr(
        sys, "stdin", io.StringIO("https://other-merchant.example/staff#first")
    )
    run(merchant, profile, "login", "--link-stdin")
    with pytest.raises(remote.ManageError, match="multiple servers"):
        run(merchant, profile, "claim", "job", "--product", "product-a")
    assert run(
        merchant, profile, "claim", "job", "--product", "product-a", "--origin", ORIGIN
    )["ok"]


def test_interactive_login_is_private_without_stdin_option(
    merchant, profile, monkeypatch
):
    prompts = []
    monkeypatch.setattr(
        remote.getpass,
        "getpass",
        lambda prompt: prompts.append(prompt) or ORIGIN + "/staff#first",
    )
    result = run(merchant, profile, "login")
    assert result["ok"] and len(prompts) == 1


def test_profile_never_contains_raw_one_use_link(merchant, profile, monkeypatch):
    login(merchant, profile, monkeypatch)
    for path in profile.parent.iterdir():
        assert "/staff#first" not in path.read_text()


def test_manage_version_exits_before_local_data_init(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "init", lambda: pytest.fail("remote version cannot initialize DATA")
    )
    with pytest.raises(SystemExit) as exited:
        cli.main(["manage", "--version"])
    assert exited.value.code == 0
    assert capsys.readouterr().out.startswith("extore ")


@pytest.mark.parametrize(
    "endpoint",
    [
        "/api/cli/status",
        "/api/cli/challenge",
        "/api/cli/session",
        "/api/manage/products",
        "/api/manage/jobs",
    ],
)
def test_malformed_server_json_becomes_bounded_protocol_error(
    endpoint, merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    with remote.private_profile(profile) as data:
        if endpoint in ("/api/cli/challenge", "/api/cli/session"):
            data["grants"][0]["expires"] = 0

    def bad_response(request):
        if request.url.path == endpoint:
            return httpx.Response(
                200, json=[] if endpoint.startswith("/api/cli/") else {}
            )
        return merchant(request)

    command = "products" if endpoint.endswith("products") else "queues"
    result = remote.execute(
        arguments(
            command, *(["--all"] if command == "queues" else []), profile=profile
        ),
        transport=httpx.MockTransport(bad_response),
    )
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "invalid_response"


def test_cli_honors_lower_effective_server_upload_limit(
    merchant, profile, monkeypatch, tmp_path
):
    login(merchant, profile, monkeypatch)
    source = tmp_path / "payload.bin"
    source.write_bytes(b"payload")

    def limited_server(request):
        if request.url.path == "/api/upload-limits":
            return httpx.Response(
                200,
                json={"max_file_bytes": 3, "max_card_bytes": 3, "max_card_files": 1},
            )
        return merchant(request)

    with pytest.raises(remote.ManageError, match="3 bytes"):
        remote.execute(
            arguments(
                "upload",
                "job",
                "--product",
                "product-a",
                "--field",
                "artifact",
                "--file",
                str(source),
                profile=profile,
            ),
            transport=httpx.MockTransport(limited_server),
        )
    assert not merchant.upload_contents


def test_real_cli_two_product_workflow_returns_rejects_and_delivers(
    owner, profile, monkeypatch, tmp_path
):
    from fastapi.testclient import TestClient

    from extore.app import app
    from extore.db import db

    origin = "http://localhost:8000"
    products = []
    links = []
    receipts = []
    for name in ("Pipeline A", "Pipeline B"):
        response = owner.post(
            "/api/admin/products",
            json={
                "name": name,
                "mode": "manual",
                "delivery": "service",
                "allow_retry": False,
                "max_attempts": 1,
                "parameters": [
                    {"key": "request", "label": {"zh-CN": "需求", "en": "Request"}}
                ],
            },
        )
        assert response.status_code == 200, response.text
        pid = response.json()["id"]
        products.append(pid)
        response = owner.post(
            "/api/admin/staff",
            json={
                "product_id": pid,
                "name": name,
                "permissions": ["queue.view", "queue.process", "queue.retry"],
            },
        )
        assert response.status_code == 200, response.text
        links.append(response.json()["url"])
        response = owner.post("/api/admin/cards", json={"product_id": pid, "count": 1})
        code = response.json()["codes"][0]
        receipt = owner.post("/api/exchange", json={"code": code}).json()["token"]
        job_response = owner.post(
            "/api/redeem",
            json={"token": receipt, "params": {"request": "initial requirements"}},
        )
        assert job_response.status_code == 200, job_response.text
        receipts.append((receipt, job_response.json()["id"]))

    with TestClient(app, base_url=origin) as api:

        def real_api(request):
            response = api.request(
                request.method,
                str(request.url),
                headers=dict(request.headers),
                content=request.read(),
            )
            assert "set-cookie" not in response.headers
            return httpx.Response(
                response.status_code,
                headers=dict(response.headers),
                content=response.content,
            )

        transport = httpx.MockTransport(real_api)

        def command(*argv):
            return remote.execute(
                arguments(*argv, profile=profile), transport=transport
            )

        for link in links:
            monkeypatch.setattr(sys, "stdin", io.StringIO(link))
            assert command("login", "--link-stdin")["ok"]
        queued = command("queues", "--all")
        assert {queue["product_id"] for queue in queued["queues"]} == set(products)
        assert "initial requirements" not in json.dumps(queued)
        first_receipt, first_job = receipts[0]
        second_receipt, second_job = receipts[1]
        assert (
            command("job", first_job, "--product", products[0])["job"]["params"][
                "request"
            ]
            == "initial requirements"
        )
        assert command("claim", first_job, "--product", products[0])["ok"]
        assert command(
            "request-changes",
            first_job,
            "--product",
            products[0],
            "--reason",
            "请明确研究范围",
        )["ok"]
        returned = owner.post("/api/receipt", json={"token": first_receipt}).json()[
            "job"
        ]
        assert returned["state"] == "needs_input" and returned["can_retry"]
        resubmitted = owner.post(
            "/api/redeem",
            json={
                "token": first_receipt,
                "params": {"request": "updated scoped requirements"},
            },
        )
        assert resubmitted.status_code == 200, resubmitted.text
        assert (
            resubmitted.json()["id"] == first_job and resubmitted.json()["attempt"] == 2
        )
        assert command("claim", first_job, "--product", products[0])["ok"]
        assert command(
            "progress",
            first_job,
            "--product",
            products[0],
            "--progress",
            "30",
            "--message",
            "已检索资料",
        )["ok"]
        assert command(
            "complete", first_job, "--product", products[0], "--message", "服务完成"
        )["ok"]
        assert command("claim", second_job, "--product", products[1])["ok"]
        assert command(
            "reject", second_job, "--product", products[1], "--reason", "超出服务范围"
        )["ok"]
        rejected = owner.post("/api/receipt", json={"token": second_receipt}).json()[
            "job"
        ]
        assert rejected["state"] == "rejected" and not rejected["can_retry"]
        assert all(not queue["jobs"] for queue in command("queues", "--all")["queues"])
        history = command("queues", "--all", "--view", "processed")
        assert {
            job["state"] for queue in history["queues"] for job in queue["jobs"]
        } == {"succeeded", "rejected"}
        with db() as c:
            uses = c.execute(
                "SELECT max_uses,uses,max_cli_uses,cli_uses FROM staff ORDER BY created"
            ).fetchall()
            assert all(row["uses"] == 0 and row["cli_uses"] == 1 for row in uses)
        # Browser gets its independent first binding after CLI has used its one.
        with TestClient(app, base_url=origin, headers={"Origin": origin}) as browser:
            assert (
                browser.post(
                    "/api/staff/login", json={"token": links[0].split("#", 1)[1]}
                ).status_code
                == 200
            )
            assert browser.get("/api/auth/status").json()["role"] == "staff"
            assert command("logout", "--product", products[0])["ok"]
            assert browser.get("/api/auth/status").json()["role"] == "staff"


def test_real_cli_downloads_customer_brief_and_uploads_structured_delivery(
    owner, profile, monkeypatch, tmp_path
):
    from fastapi.testclient import TestClient

    from extore.app import app

    origin = "http://localhost:8000"
    product = owner.post(
        "/api/admin/products",
        json={
            "name": "File pipeline",
            "mode": "manual",
            "parameters": [
                {
                    "key": "brief",
                    "type": "file",
                    "label": {"zh-CN": "需求附件", "en": "Brief"},
                }
            ],
            "outputs": [
                {
                    "key": "artifact",
                    "type": "file",
                    "label": {"zh-CN": "交付文件", "en": "Artifact"},
                }
            ],
        },
    ).json()["id"]
    link_response = owner.post(
        "/api/admin/staff",
        json={
            "product_id": product,
            "name": "File pipeline agent",
            "permissions": ["queue.view", "queue.process"],
        },
    )
    assert link_response.status_code == 200, link_response.text
    code = owner.post(
        "/api/admin/cards", json={"product_id": product, "count": 1}
    ).json()["codes"][0]
    receipt = owner.post("/api/exchange", json={"code": code}).json()["token"]
    uploaded = owner.post(
        "/api/files/upload",
        data={"token": receipt, "field_key": "brief"},
        files={"file": ("brief.txt", b"synthetic customer brief", "text/plain")},
    )
    assert uploaded.status_code == 200, uploaded.text
    input_file = uploaded.json()["id"]
    submitted = owner.post(
        "/api/redeem", json={"token": receipt, "params": {"brief": input_file}}
    )
    assert submitted.status_code == 200, submitted.text
    job = submitted.json()["id"]
    with TestClient(app, base_url=origin) as api:

        def real_api(request):
            response = api.request(
                request.method,
                str(request.url),
                headers=dict(request.headers),
                content=request.read(),
            )
            return httpx.Response(
                response.status_code,
                headers=dict(response.headers),
                content=response.content,
            )

        transport = httpx.MockTransport(real_api)

        def command(*argv):
            return remote.execute(
                arguments(*argv, profile=profile), transport=transport
            )

        monkeypatch.setattr(sys, "stdin", io.StringIO(link_response.json()["url"]))
        assert command("login", "--link-stdin")["ok"]
        summary = command("queues", "--all")["queues"][0]["jobs"][0]
        assert summary["attachments"]["input"] == {"count": 1, "bytes": 24}
        assert "params" not in summary and "files" not in summary
        files = command("files", job, "--product", product)["files"]
        assert [item["id"] for item in files] == [input_file]
        downloaded = tmp_path / "downloaded-brief.txt"
        assert command(
            "download",
            job,
            "--product",
            product,
            "--file-id",
            input_file,
            "--output",
            str(downloaded),
        )["ok"]
        assert downloaded.read_bytes() == b"synthetic customer brief"
        assert command("claim", job, "--product", product)["ok"]
        output = tmp_path / "artifact.txt"
        output.write_bytes(b"synthetic completed artifact")
        artifact = command(
            "upload",
            job,
            "--product",
            product,
            "--field",
            "artifact",
            "--file",
            str(output),
        )["file"]
        delivery = tmp_path / "delivery.json"
        delivery.write_text(json.dumps({"artifact": artifact["id"]}))
        assert command(
            "complete", job, "--product", product, "--output-file", str(delivery)
        )["ok"]
        revealed = owner.post("/api/receipt/reveal", json={"token": receipt})
        assert revealed.status_code == 200, revealed.text
        assert revealed.json()["output"]["artifact"] == artifact["id"]
        downloaded_output = owner.post(
            "/api/files/download", json={"token": receipt, "file_id": artifact["id"]}
        )
        assert (
            downloaded_output.status_code == 200
            and downloaded_output.content == output.read_bytes()
        )


def test_real_cli_only_ticket_and_device_revocation_leave_browser_login_intact(
    owner, profile, monkeypatch, tmp_path
):
    from fastapi.testclient import TestClient

    from extore.app import app

    origin = "http://localhost:8000"
    product = owner.post(
        "/api/admin/products",
        json={
            "name": "Ticket pipeline",
            "mode": "manual",
            "delivery": "service",
            "parameters": [],
        },
    ).json()["id"]
    response = owner.post(
        "/api/admin/staff",
        json={
            "product_id": product,
            "name": "One browser and CLI",
            "permissions": ["queue.view"],
        },
    )
    assert response.status_code == 200, response.text
    link = response.json()["url"]
    with (
        TestClient(app, base_url=origin, headers={"Origin": origin}) as browser,
        TestClient(app, base_url=origin) as api,
    ):
        assert (
            browser.post(
                "/api/staff/login", json={"token": link.split("#", 1)[1]}
            ).status_code
            == 200
        )
        ticket = browser.post("/api/manage/cli-ticket", json={})
        assert ticket.status_code == 200, ticket.text
        invitation = origin + "/cli#" + ticket.json()["token"]

        def real_api(request):
            response = api.request(
                request.method,
                str(request.url),
                headers=dict(request.headers),
                content=request.read(),
            )
            return httpx.Response(
                response.status_code,
                headers=dict(response.headers),
                content=response.content,
            )

        transport = httpx.MockTransport(real_api)
        monkeypatch.setattr(sys, "stdin", io.StringIO(invitation))
        login = remote.execute(
            arguments("login", "--link-stdin", profile=profile), transport=transport
        )
        assert login["ok"]
        device = login["grant"]["id"]
        second_dir = tmp_path / "second-private-device"
        second_dir.mkdir(mode=0o700)
        monkeypatch.setattr(sys, "stdin", io.StringIO(invitation))
        with pytest.raises(remote.ManageError) as denied:
            remote.execute(
                arguments("login", "--link-stdin", profile=second_dir / "cli.json"),
                transport=transport,
            )
        assert denied.value.status in (401, 403, 409)
        assert browser.get("/api/auth/status").json()["role"] == "staff"
        revoked = owner.delete("/api/admin/cli-devices/" + device)
        assert revoked.status_code == 200, revoked.text
        result = remote.execute(
            arguments("queues", "--all", profile=profile), transport=transport
        )
        assert not result["ok"] and not result["queues"] and len(result["errors"]) == 1
        assert browser.get("/api/auth/status").json()["role"] == "staff"

"""CLI retry requests preserve scope, require reasons, and await the customer."""

import io
import json
import sys

import httpx
import pytest
from fastapi.testclient import TestClient
from test_manage_cli import MockMerchant, arguments, login, run

from extore import manage_client as remote
from extore.app import app
from extore.db import db


@pytest.fixture
def merchant():
    return MockMerchant()


@pytest.fixture
def profile(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    return root / "cli.json"


def batch_calls(merchant):
    return [call for call in merchant.calls if call[1] == "/api/manage/batch"]


@pytest.mark.parametrize("reason_type", ["customer_input", "external", "processor"])
@pytest.mark.parametrize("retry_mode", ["revise", "reuse"])
def test_retry_request_sends_selected_mode_and_reason_category(
    reason_type, retry_mode, merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    result = run(
        merchant,
        profile,
        "request-retry",
        "job",
        "--product",
        "product-a",
        "--reason",
        "上游服务暂时不可用，请稍后重试",
        "--reason-type",
        reason_type,
        "--retry-mode",
        retry_mode,
    )
    assert result == {"ok": True}
    assert len(batch_calls(merchant)) == 1
    assert json.loads(batch_calls(merchant)[0][3]) == {
        "product_id": "product-a",
        "ids": ["job"],
        "action": "request_retry",
        "message": "上游服务暂时不可用，请稍后重试",
        "reason_type": reason_type,
        "retry_mode": retry_mode,
    }
    # Asking the customer to retry must not also claim, fail, or resubmit a task.
    assert not any(call[1] == "/api/redeem" for call in merchant.calls)


@pytest.mark.parametrize(
    "command,action",
    [("request-retry", "request_retry"), ("request-changes", "request_changes")],
)
def test_retry_defaults_keep_existing_customer_revision_semantics(
    command, action, merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    run(
        merchant,
        profile,
        command,
        "job",
        "--product",
        "product-a",
        "--reason",
        "请补充需求",
    )
    body = json.loads(batch_calls(merchant)[0][3])
    assert body["action"] == action
    assert body["retry_mode"] == "revise"
    assert body["reason_type"] == "customer_input"


@pytest.mark.parametrize(
    "link,product", [("view", "product-c"), ("first", "foreign-product")]
)
def test_retry_without_single_product_process_grant_never_mutates_queue(
    link, product, merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch, link)
    with pytest.raises(remote.ManageError, match="single grant"):
        run(
            merchant,
            profile,
            "request-retry",
            "job",
            "--product",
            product,
            "--reason",
            "服务临时不可用",
            "--reason-type",
            "external",
            "--retry-mode",
            "reuse",
        )
    assert not batch_calls(merchant)


@pytest.mark.parametrize("reason", ["", " \n\t", "x" * 1001])
def test_invalid_retry_reason_never_reaches_mutation(
    reason, merchant, profile, monkeypatch
):
    login(merchant, profile, monkeypatch)
    with pytest.raises(remote.ManageError, match="non-empty"):
        run(
            merchant,
            profile,
            "request-retry",
            "job",
            "--product",
            "product-a",
            "--reason",
            reason,
        )
    assert not batch_calls(merchant)


@pytest.mark.parametrize(
    "flag,value", [("--reason-type", "unknown"), ("--retry-mode", "automatic")]
)
def test_unsupported_retry_options_are_rejected_before_network(
    flag, value, merchant, profile
):
    with pytest.raises(SystemExit) as exited:
        run(
            merchant,
            profile,
            "request-retry",
            "job",
            "--product",
            "product-a",
            "--reason",
            "重试原因",
            flag,
            value,
        )
    assert exited.value.code == 2
    assert not merchant.calls


@pytest.mark.parametrize("status", [200, 409, 422])
def test_retry_cli_output_does_not_disclose_credentials_or_server_inputs(
    status, merchant, profile, monkeypatch, capsys
):
    login(merchant, profile, monkeypatch)
    saved = json.loads(profile.read_text())["grants"][0]
    secret = "private customer input returned by a broken server"

    def response(request):
        if request.url.path == "/api/manage/batch":
            return httpx.Response(
                status,
                json={"ok": True} if status == 200 else {"detail": secret},
            )
        return merchant(request)

    execute = remote.execute
    monkeypatch.setattr(
        remote,
        "execute",
        lambda args: execute(args, transport=httpx.MockTransport(response)),
    )
    args = arguments(
        "request-retry",
        "job",
        "--product",
        "product-a",
        "--reason",
        "临时不可用",
        "--reason-type",
        "external",
        "--retry-mode",
        "reuse",
        profile=profile,
    )
    if status == 200:
        remote.main(args)
    else:
        with pytest.raises(SystemExit) as exited:
            remote.main(args)
        assert exited.value.code == 1
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert saved["access_token"] not in output
    assert saved["private_key"] not in output
    assert secret not in output
    assert "sensitive task input" not in output
    if status == 200:
        assert json.loads(captured.out) == {"ok": True}
    else:
        assert not captured.out
        assert json.loads(captured.err)["status"] == status


@pytest.fixture
def real_queue(owner, profile, monkeypatch, setup_product):
    pid, code = setup_product(
        mode="manual", delivery="service", allow_retry=False, max_attempts=1
    )
    response = owner.post(
        "/api/admin/staff",
        json={
            "product_id": pid,
            "name": "Retry CLI test",
            "permissions": ["queue.view", "queue.process"],
        },
    )
    assert response.status_code == 200, response.text
    invitation = response.json()["url"]
    receipt = owner.post("/api/exchange", json={"code": code}).json()["token"]
    response = owner.post(
        "/api/redeem", json={"token": receipt, "params": {"email": "a@example.com"}}
    )
    assert response.status_code == 200, response.text
    jid = response.json()["id"]
    with TestClient(app, base_url="http://localhost:8000") as api:

        def transport(request):
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

        def command(*argv):
            return remote.execute(
                arguments(*argv, profile=profile),
                transport=httpx.MockTransport(transport),
            )

        monkeypatch.setattr(sys, "stdin", io.StringIO(invitation))
        assert command("login", "--link-stdin")["ok"]
        yield pid, jid, receipt, command


def test_real_external_retry_waits_for_customer_and_preserves_frozen_inputs(
    real_queue, owner
):
    pid, jid, receipt, command = real_queue
    assert command("claim", jid, "--product", pid)["ok"]
    assert command(
        "request-retry",
        jid,
        "--product",
        pid,
        "--reason",
        "上游检索服务暂时不可用",
        "--reason-type",
        "external",
        "--retry-mode",
        "reuse",
    )["ok"]
    returned = owner.post("/api/receipt", json={"token": receipt}).json()["job"]
    assert returned["state"] == "needs_input"
    assert returned["attempt"] == 1
    assert returned["params"] == {"email": "a@example.com"}
    assert returned["retry_mode"] == "reuse"
    assert returned["retry_reason_type"] == "external"
    assert returned["can_retry"] is True
    with db() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        assert row["claimed_by"] is None and row["lease"] is None
        assert (
            c.execute(
                "SELECT state FROM cards WHERE id=?", (row["card_id"],)
            ).fetchone()[0]
            == "ready"
        )
    changed = owner.post(
        "/api/redeem", json={"token": receipt, "params": {"email": "b@example.com"}}
    )
    assert changed.status_code == 409
    response = owner.post("/api/retry", json={"token": receipt})
    assert response.status_code == 200, response.text
    assert response.json()["id"] == jid and response.json()["attempt"] == 2
    detailed = command("job", jid, "--product", pid)["job"]
    assert detailed["state"] == "queued"
    assert detailed["params"] == {"email": "a@example.com"}


def test_real_request_changes_still_requires_customer_revision(real_queue, owner):
    pid, jid, receipt, command = real_queue
    assert command("claim", jid, "--product", pid)["ok"]
    assert command(
        "request-changes", jid, "--product", pid, "--reason", "请填写正确的邮箱"
    )["ok"]
    returned = owner.post("/api/receipt", json={"token": receipt}).json()["job"]
    assert returned["retry_mode"] == "revise"
    assert returned["retry_reason_type"] == "customer_input"
    assert owner.post("/api/retry", json={"token": receipt}).status_code == 409
    revised = owner.post(
        "/api/redeem", json={"token": receipt, "params": {"email": "b@example.com"}}
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["id"] == jid and revised.json()["attempt"] == 2
    assert command("job", jid, "--product", pid)["job"]["params"] == {
        "email": "b@example.com"
    }


@pytest.mark.parametrize("state", ["queued", "failed", "succeeded", "rejected"])
def test_retry_request_cannot_reopen_unclaimed_or_closed_tasks(real_queue, state):
    pid, jid, _, command = real_queue
    if state != "queued":
        assert command("claim", jid, "--product", pid)["ok"]
        final_command = {
            "failed": "fail",
            "succeeded": "complete",
            "rejected": "reject",
        }[state]
        flags = ["--reason", "超出能力范围"] if state == "rejected" else []
        assert command(final_command, jid, "--product", pid, *flags)["ok"]
    with db() as c:
        before = dict(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
        card_before = dict(
            c.execute("SELECT * FROM cards WHERE id=?", (before["card_id"],)).fetchone()
        )
        events_before = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        audits_before = c.execute(
            "SELECT COUNT(*) FROM audit WHERE action LIKE 'job.%'"
        ).fetchone()[0]
    with pytest.raises(remote.ManageError) as rejected:
        command(
            "request-retry",
            jid,
            "--product",
            pid,
            "--reason",
            "服务临时不可用",
            "--reason-type",
            "external",
            "--retry-mode",
            "reuse",
        )
    assert rejected.value.status == 409
    with db() as c:
        assert (
            dict(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
            == before
        )
        assert (
            dict(
                c.execute(
                    "SELECT * FROM cards WHERE id=?", (before["card_id"],)
                ).fetchone()
            )
            == card_before
        )
        assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before
        assert (
            c.execute(
                "SELECT COUNT(*) FROM audit WHERE action LIKE 'job.%'"
            ).fetchone()[0]
            == audits_before
        )

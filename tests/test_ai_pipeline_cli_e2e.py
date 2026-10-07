"""Customer → approved CLI AI → delivery, against the real HTTP application.

Only the network transport is in-process. CLI parsers, Ed25519 handshakes,
device review, queue claims, attachment bytes and finalization are real. All
cards/accounts/files belong to pytest's isolated temporary database.
"""

import io
import json
import stat
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from test_customer_cli import arguments as customer_args
from test_customer_cli import issue, make_product
from test_manage_cli import arguments as manage_args
from test_owner_cli import arguments as owner_args
from test_passkeys import Authenticator, register
from test_scope_auth import approve, review

from extore import customer_cli, manage_client, owner_client
from extore.app import app
from extore.config import ORIGIN
from extore.db import db
from extore.manage_client import ManageError

BOT_NAME = "Synthetic Documents Bot"
BOT_TYPE = "dots"


@pytest.fixture
def cli_http(owner, tmp_path, monkeypatch):
    records, fault = [], {"drop_next": False}
    profiles = {
        name: tmp_path / name / "private.json"
        for name in ("admin", "manage", "customer")
    }
    with TestClient(app, base_url=ORIGIN) as public:

        def relay(request):
            assert "cookie" not in request.headers
            raw = request.read()
            response = public.request(
                request.method,
                str(request.url),
                headers=dict(request.headers),
                content=raw,
            )
            records.append(
                {
                    "request": request,
                    "body": raw,
                    "path": request.url.path,
                    "status": response.status_code,
                }
            )
            if fault["drop_next"] and request.url.path == "/api/manage/next":
                fault["drop_next"] = False
                raise httpx.ReadError(
                    "synthetic response lost after committed claim", request=request
                )
            return httpx.Response(
                response.status_code, headers=response.headers, content=response.content
            )

        transport = httpx.MockTransport(relay)

        def run(kind, *args, stdin=None):
            if stdin is not None:
                monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
            module, parser = {
                "admin": (owner_client, owner_args),
                "manage": (manage_client, manage_args),
                "customer": (customer_cli, customer_args),
            }[kind]
            parsed = (
                parser(profiles[kind], *args)
                if kind == "admin"
                else parser(*args, profile=profiles[kind])
            )
            return module.execute(parsed, transport=transport)

        authenticator = Authenticator()
        register(owner, authenticator, "Synthetic E2E owner consent")
        assert run("admin", "login", "--origin", ORIGIN)["status"] == "pending"
        pending = json.loads(profiles["admin"].read_text())["owners"][0]
        options = owner.post(
            "/api/auth/cli-owner/options",
            json={key: pending[key] for key in ("request_id", "device_code")},
        )
        assert options.status_code == 200, options.text
        accepted = owner.post(
            "/api/auth/cli-owner/verify",
            json={
                "request_id": pending["request_id"],
                "credential": authenticator.assertion(options.json()["options"]),
            },
        )
        assert accepted.status_code == 200, accepted.text
        assert run("admin", "login-status")["status"] == "authorized"
        yield SimpleNamespace(
            admin=lambda *args: run("admin", *args),
            manage=lambda *args: run("manage", *args),
            customer=lambda *args, **kwargs: run("customer", *args, **kwargs),
            replay=lambda record: relay(record["request"]),
            records=records,
            fault=fault,
            profiles=profiles,
        )


def field(key, kind="text", **extra):
    return {"key": key, "type": kind, "label": {"en": key}, **extra}


def shop_id(pid):
    with db() as c:
        return c.execute("SELECT shop_id FROM products WHERE id=?", (pid,)).fetchone()[
            0
        ]


def login_args(pid):
    return (
        "login",
        "--device-code",
        "--origin",
        ORIGIN,
        "--shop",
        shop_id(pid),
        "--pipelines-all",
        "--client-name",
        BOT_NAME,
        "--agent-type",
        BOT_TYPE,
    )


def consent(owner, cli, products, *, after_request=None):
    args = login_args(products[0]["id"])
    pending = cli.manage(*args, "--no-wait")["authorization"]
    if after_request:
        after_request()
    context = review(owner, pending)
    assert set(context["request"]["requested_product_ids"]) == {
        p["id"] for p in products
    }
    assert context["selected"]["permissions"] == [
        "queue.view",
        "queue.process",
        "queue.retry",
    ]
    request = next(
        record
        for record in reversed(cli.records)
        if record["path"] == "/api/cli/scopes/request"
    )
    signed_body = json.loads(request["body"])
    assert (
        signed_body["client_name"] == BOT_NAME and signed_body["agent_type"] == BOT_TYPE
    )
    approve(owner, pending, context)
    result = cli.manage(*args)
    assert result["ok"] and set(result["authorization"]["product_ids"]) == {
        p["id"] for p in products
    }
    assert stat.S_IMODE(cli.profiles["manage"].stat().st_mode) == 0o600
    return result["authorization"]


def submit(owner, cli, product, values, *, attachment=None):
    code = issue(owner, product)[0]
    receipt = cli.customer(
        "exchange", "--origin", ORIGIN, "--codes-stdin", "--atomic", stdin=code
    )["receipt_id"]
    args = ("redeem", receipt, "--params-stdin") + (
        ("--file", f"brief={attachment}") if attachment else ()
    )
    task = cli.customer(*args, stdin=json.dumps(values))["job"]
    return receipt, task


def guards(item):
    values = ["--attempt", str(item["job"]["attempt"])]
    if item["execution"].get("flow_epoch") is not None:
        values.extend(
            (
                "--flow-epoch",
                str(item["execution"]["flow_epoch"]),
                "--action-id",
                item["execution"]["action_id"],
            )
        )
    return values


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


def test_ai_device_snapshot_recovers_atomic_claim_and_delivers_two_file_products(
    owner, cli_http, tmp_path
):
    cli = cli_http
    products = [
        make_product(
            owner,
            name=name,
            mode="manual",
            parameters=[field("request"), field("brief", "file")],
            outputs=[field("result", "file"), field("notes", "textarea")],
            progress_steps=[
                {"id": "inspect", "label": {"en": "Inspect"}},
                {"id": "deliver", "label": {"en": "Deliver"}},
            ],
        )
        for name in ("Document queue", "Presentation queue")
    ]
    receipts, originals = {}, {}
    for index, product in enumerate(products):
        source = tmp_path / f"brief-{index}.txt"
        source.write_bytes(f"Synthetic source {index}".encode())
        receipt, task = submit(
            owner,
            cli,
            product,
            {"request": f"Synthetic demand {index}"},
            attachment=source,
        )
        receipts[task["id"]], originals[task["id"]] = receipt, source.read_bytes()
    future = []

    def create_future():
        future.append(make_product(owner, name="Created after the approved snapshot"))
        submit(owner, cli, future[0], {"request": "future-unapproved-input"})

    consent(owner, cli, products, after_request=create_future)
    before = len(cli.records)
    cli.fault["drop_next"] = True
    with pytest.raises(ManageError):
        cli.manage("next", "--all", "--wait", "0", "--limit", "2")
    with db() as c:
        assert (
            c.execute("SELECT count(*) FROM jobs WHERE state='processing'").fetchone()[
                0
            ]
            == 2
        )
        assert (
            c.execute(
                "SELECT count(*) FROM audit WHERE action='queue.next.claim'"
            ).fetchone()[0]
            == 2
        )
    recovered = cli.manage("next", "--all", "--wait", "0", "--limit", "2")
    assert recovered["replayed"] and {
        item["product_id"] for item in recovered["items"]
    } == {p["id"] for p in products}
    claim_records = [r for r in cli.records[before:] if r["path"] == "/api/manage/next"]
    assert (
        len(claim_records) == 2 and claim_records[0]["body"] == claim_records[1]["body"]
    )
    assert not any(
        r["path"] in ("/api/manage/jobs", "/api/manage/batch")
        for r in cli.records[before:]
    )
    board = cli.admin("board", "--shop", shop_id(products[0]["id"]))["board"]
    assert board["totals"]["processing"] == 2 and board["totals"]["queued"] == 1
    assert {worker["name"] for worker in board["workers"]} == {BOT_NAME}
    assert {worker["agent_type"] for worker in board["workers"]} == {BOT_TYPE}
    assert "Synthetic demand" not in json.dumps(
        board
    ) and "future-unapproved-input" not in json.dumps(recovered)
    for index, item in enumerate(recovered["items"]):
        pid, jid = item["product_id"], item["job"]["id"]
        assert set(item["execution"]["params"]) == {"request", "brief"}
        assert {row["key"] for row in item["execution"]["outputs"]} == {
            "result",
            "notes",
        }
        downloaded = tmp_path / f"ai-input-{index}.txt"
        assert cli.manage(
            "download",
            jid,
            "--product",
            pid,
            "--file-id",
            item["execution"]["params"]["brief"],
            "--output",
            str(downloaded),
        )["ok"]
        assert downloaded.read_bytes() == originals[jid]
        assert cli.manage(
            "progress",
            jid,
            "--product",
            pid,
            *guards(item),
            "--progress",
            "50",
            "--completed-step",
            "inspect",
            "--message",
            "Source checked",
        )["ok"]
        progress = cli.customer("receipt", receipts[jid])["job"]
        assert progress["progress"] == 50 and progress["completed_steps"] == ["inspect"]
        result = tmp_path / f"finished-{index}.txt"
        result.write_bytes(downloaded.read_bytes() + b"\nAI output")
        output = write_json(
            tmp_path / f"output-{index}.json",
            {"notes": f"Checked synthetic artifact {index}"},
        )
        assert cli.manage(
            "complete",
            jid,
            "--product",
            pid,
            *guards(item),
            "--output-file",
            output,
            "--file",
            f"result={result}",
        )["ok"]
        assert cli.customer("receipt", receipts[jid])["job"]["state"] == "succeeded"
        revealed = tmp_path / f"customer-delivery-{index}.json"
        assert cli.customer("reveal", receipts[jid], "--output", str(revealed))["ok"]
        delivery = json.loads(revealed.read_text())
        assert delivery["output"]["notes"] == f"Checked synthetic artifact {index}"
        destination = tmp_path / f"customer-output-{index}.txt"
        assert cli.customer(
            "download",
            receipts[jid],
            "--file-id",
            delivery["output"]["result"],
            "--output",
            str(destination),
        )["ok"]
        assert (
            destination.read_bytes() == result.read_bytes()
            and stat.S_IMODE(destination.stat().st_mode) == 0o600
        )
    assert cli.manage("next", "--all", "--wait", "0")["idle"]
    with pytest.raises(ManageError):
        cli.manage("job", "unknown", "--product", future[0]["id"])
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM audit WHERE action='queue.next.claim'"
            ).fetchone()[0]
            == 2
        )
        assert all(
            "Synthetic demand" not in row["items"]
            for row in c.execute("SELECT items FROM automation_requests")
        )
    assert "Synthetic demand" not in cli.profiles["manage"].read_text()


def three_question_graph():
    nodes = []
    for index in range(1, 4):
        question = field(f"question_{index}", ("text", "select", "boolean")[index - 1])
        if index == 2:
            question["options"] = [
                {"value": "yes", "label": {"en": "Yes"}},
                {"value": "no", "label": {"en": "No"}},
            ]
        nodes.extend(
            [
                {
                    "id": f"q{index}",
                    "kind": "input",
                    "prompt": {"en": f"Question {index}"},
                    "fields": [question],
                    "start_policy": "confirm" if index == 1 else "automatic",
                    "timeout_seconds": 120,
                    "timeout_next": "failed",
                    "next": f"p{index}",
                    **(
                        {
                            "show_from": {
                                "previous_answer": {
                                    "node": f"p{index - 1}",
                                    "field": "answer",
                                }
                            }
                        }
                        if index > 1
                        else {}
                    ),
                },
                {
                    "id": f"p{index}",
                    "kind": "process",
                    "inputs": {
                        "question": {"node": f"q{index}", "field": f"question_{index}"}
                    },
                    "outputs": [field("answer", "textarea")],
                    "timeout_seconds": 120,
                    "timeout_next": "failed",
                    "failure_next": "failed",
                    "next": f"q{index + 1}" if index < 3 else "done",
                },
            ]
        )
    nodes.extend(
        [
            {
                "id": "done",
                "kind": "end",
                "state": "succeeded",
                "result": {"content": {"node": "p3", "field": "answer"}},
            },
            {"id": "failed", "kind": "end", "state": "failed", "retryable": True},
        ]
    )
    return {"version": 1, "entry": "q1", "nodes": nodes}


def test_cli_ai_handles_three_current_only_questions_with_typed_inputs(
    owner, cli_http, tmp_path
):
    cli = cli_http
    product = make_product(
        owner,
        name="Aladdin current-only questionnaire",
        mode="manual",
        parameters=[],
        outputs=[field("content", "textarea")],
        task_flow=three_question_graph(),
    )
    code = issue(owner, product)[0]
    exchanged = cli.customer(
        "exchange", "--origin", ORIGIN, "--codes-stdin", "--atomic", stdin=code
    )
    receipt = exchanged["receipt_id"]
    assert exchanged["job"] is None
    consent(owner, cli, [product])
    waiting = cli.customer("flow", "view", receipt)["task_flow"]
    assert waiting["phase"] == "await_start" and waiting["deadline"] is None
    assert "fields" not in waiting["current"]
    assert cli.manage("next", "--all", "--wait", "0")["idle"]
    initial = cli.customer("flow", "start", receipt)["job"]
    values = ["First private question", "yes", False]
    for index, value in enumerate(values, 1):
        view = cli.customer("flow", "view", receipt)["task_flow"]
        assert view["phase"] == "input" and view["deadline"] is not None
        assert [row["key"] for row in view["current"]["fields"]] == [
            f"question_{index}"
        ]
        if index > 1:
            assert [row["value"] for row in view["shown"]] == [f"Answer {index - 1}"]
        answer = cli.customer(
            "flow",
            "answer",
            receipt,
            "--values-stdin",
            stdin=json.dumps({f"question_{index}": value}),
        )
        assert answer["job"]["task_flow"]["phase"] == "queued"
        work = cli.manage("next", "--all", "--wait", "0")["items"]
        assert len(work) == 1
        item = work[0]
        assert item["job"]["id"] == initial["id"] and item["job"]["attempt"] == 1
        assert item["execution"]["params"] == {
            "question": "false" if value is False else value
        }
        assert [row["key"] for row in item["execution"]["parameters"]] == ["question"]
        assert [row["key"] for row in item["execution"]["outputs"]] == ["answer"]
        claim_record = next(
            r for r in reversed(cli.records) if r["path"] == "/api/manage/next"
        )
        assert cli.manage(
            "progress",
            initial["id"],
            "--product",
            product["id"],
            *guards(item),
            "--progress",
            "35",
            "--message",
            f"Thinking about question {index}",
        )["ok"]
        output = write_json(
            tmp_path / f"answer-{index}.json", {"answer": f"Answer {index}"}
        )
        assert cli.manage(
            "complete",
            initial["id"],
            "--product",
            product["id"],
            *guards(item),
            "--output-file",
            output,
        )["ok"]
        stale = cli.replay(claim_record).json()
        assert stale["stale"] and stale["items"] == []
        with pytest.raises(ManageError) as error:
            cli.manage(
                "progress",
                initial["id"],
                "--product",
                product["id"],
                *guards(item),
                "--progress",
                "90",
            )
        assert error.value.status == 409
        if index < 3:
            assert cli.manage("next", "--all", "--wait", "0")["idle"]
    assert cli.customer("receipt", receipt)["job"]["state"] == "succeeded"
    delivery = tmp_path / "aladdin-delivery.json"
    cli.customer("reveal", receipt, "--output", str(delivery))
    assert json.loads(delivery.read_text())["output"] == {"content": "Answer 3"}
    with db() as c:
        assert (
            c.execute(
                "SELECT params FROM jobs WHERE id=?", (initial["id"],)
            ).fetchone()[0]
            == "{}"
        )
        assert (
            c.execute(
                "SELECT count(*) FROM audit WHERE action='queue.next.claim' AND target=?",
                (initial["id"],),
            ).fetchone()[0]
            == 3
        )
    assert (
        "First private question" not in cli.profiles["manage"].read_text()
        and "First private question" not in cli.profiles["customer"].read_text()
    )


def test_retry_attempt_reject_and_revocation_are_safe_in_the_real_cli_loop(
    owner, cli_http, tmp_path
):
    cli = cli_http
    products = [
        make_product(owner, name=name, outputs=[field("content", "textarea")])
        for name in ("Retry queue", "Reject queue")
    ]
    first_receipt, first = submit(owner, cli, products[0], {"request": "Too vague"})
    second_receipt, second = submit(
        owner, cli, products[1], {"request": "Outside supported scope"}
    )
    authorization = consent(owner, cli, products)
    item = cli.manage("next", "--product", products[0]["id"], "--wait", "0")["items"][0]
    reason = "Processor unavailable; clarify requirements before retrying"
    assert cli.manage(
        "request-retry",
        first["id"],
        "--product",
        products[0]["id"],
        *guards(item),
        "--reason",
        reason,
        "--reason-type",
        "processor",
        "--retry-mode",
        "revise",
    )["ok"]
    returned = cli.customer("receipt", first_receipt, "--inputs")
    assert returned["job"]["state"] == "needs_input" and returned["job"]["can_retry"]
    assert returned["job"]["message"] == reason and returned["inputs"] == {
        "request": "Too vague"
    }
    with db() as c:
        assert (
            c.execute(
                "SELECT cards.state FROM cards JOIN jobs ON jobs.card_id=cards.id WHERE jobs.id=?",
                (first["id"],),
            ).fetchone()[0]
            == "ready"
        )
    retried = cli.customer(
        "retry",
        first_receipt,
        "--params-stdin",
        stdin='{"request":"Clear corrected requirements"}',
    )["job"]
    assert retried["id"] == first["id"] and retried["attempt"] == 2
    work = cli.manage("next", "--all", "--wait", "0", "--limit", "2")["items"]
    corrected = next(row for row in work if row["job"]["id"] == first["id"])
    refused = next(row for row in work if row["job"]["id"] == second["id"])
    assert corrected["execution"]["params"] == {
        "request": "Clear corrected requirements"
    }
    with pytest.raises(ManageError) as error:
        cli.manage(
            "progress",
            first["id"],
            "--product",
            products[0]["id"],
            "--attempt",
            "1",
            "--progress",
            "75",
        )
    assert error.value.status == 409
    assert cli.manage(
        "reject",
        second["id"],
        "--product",
        products[1]["id"],
        *guards(refused),
        "--reason",
        "Outside supported scope",
    )["ok"]
    rejected = cli.customer("receipt", second_receipt)["job"]
    assert rejected["state"] == "rejected" and not rejected["can_retry"]
    with pytest.raises(ManageError):
        cli.customer(
            "retry",
            second_receipt,
            "--params-stdin",
            stdin='{"request":"retry rejected"}',
        )
    response = owner.request(
        "DELETE",
        f"/api/admin/pipeline-authorizations/{authorization['id']}",
        json={"expected_revision": authorization["revision"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["released_jobs"] == 1
    with pytest.raises(ManageError):
        cli.manage("next", "--all", "--wait", "0")
    content = tmp_path / "revoked-result.txt"
    content.write_text("Must not be delivered", encoding="utf-8")
    with pytest.raises(ManageError):
        cli.manage(
            "complete",
            first["id"],
            "--product",
            products[0]["id"],
            *guards(corrected),
            "--content-file",
            str(content),
        )
    with db() as c:
        row = c.execute(
            "SELECT state,claimed_by,result_json FROM jobs WHERE id=?", (first["id"],)
        ).fetchone()
        assert (
            row["state"] == "queued"
            and row["claimed_by"] is None
            and row["result_json"] is None
        )


def test_denied_device_code_never_creates_processing_authority(owner, cli_http):
    cli = cli_http
    product = make_product(owner)
    _, task = submit(owner, cli, product, {"request": "Keep queued after denial"})
    args = login_args(product["id"])
    pending = cli.manage(*args, "--no-wait")["authorization"]
    denied = owner.post(
        "/api/manage/device/deny", json={"user_code": pending["user_code"]}
    )
    assert denied.status_code == 200, denied.text
    with pytest.raises(ManageError):
        cli.manage(*args)
    with pytest.raises(ManageError):
        cli.manage("next", "--all", "--wait", "0")
    with db() as c:
        assert (
            c.execute("SELECT state FROM jobs WHERE id=?", (task["id"],)).fetchone()[0]
            == "queued"
        )
        assert (
            c.execute("SELECT count(*) FROM pipeline_authorizations").fetchone()[0] == 0
        )
    assert json.loads(cli.profiles["manage"].read_text())["grants"] == []

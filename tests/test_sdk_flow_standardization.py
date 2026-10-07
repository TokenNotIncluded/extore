"""Public flow authoring and execution helpers keep the server contract intact."""

import io
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest
from test_private_worker_sdk_flow_integration import ActualApiOpener

from extore import private_worker, task_flow
from extore.db import db
from extore.sdk import FlowDefinition, FlowScope, PrivateWorkerClient
from extore.service import job
from extore.task_flow_definition import validate_definition


def field(key, kind="text", **values):
    return {"key": key, "type": kind, "label": {"en": key}, **values}


def product():
    return {
        "mode": "manual",
        "parameters": [field("requirements")],
        "outputs": [field("answer", "textarea")],
    }


def graph():
    return {
        "version": 1,
        "entry": "ask",
        "nodes": [
            {
                "id": "ask",
                "kind": "input",
                "fields": [field("requirements")],
                "next": "work",
            },
            {
                "id": "work",
                "kind": "process",
                "inputs": {"requirements": {"node": "ask", "field": "requirements"}},
                "outputs": [field("answer", "textarea")],
                "next": "done",
                "failure_next": "failed",
                "timeout_next": "failed",
            },
            {
                "id": "done",
                "kind": "end",
                "state": "succeeded",
                "result": {"answer": {"node": "work", "field": "answer"}},
            },
            {"id": "failed", "kind": "end", "state": "failed"},
        ],
    }


def node(definition, identity):
    return next(item for item in definition["nodes"] if item["id"] == identity)


def test_authoring_uses_server_normalization_without_mutating_the_draft():
    draft, configured = graph(), product()
    original_draft, original_product = deepcopy(draft), deepcopy(configured)
    authored = FlowDefinition.from_dict(draft, product=configured)
    canonical = authored.as_dict()
    assert canonical == validate_definition(draft, configured)
    assert json.loads(authored.to_json()) == canonical
    assert node(canonical, "ask")["start_policy"] == "confirm"
    assert node(canonical, "ask")["timeout_seconds"] is None
    assert node(canonical, "work")["timeout_seconds"] == 3600
    assert draft == original_draft and configured == original_product


def test_authoring_from_nodes_and_readback_do_not_share_mutable_values():
    draft, configured = graph(), product()
    expected = validate_definition(draft, configured)
    authored = FlowDefinition.from_nodes(
        draft["entry"], draft["nodes"], product=configured
    )
    node(draft, "ask")["fields"][0]["label"]["en"] = "changed source"
    configured["outputs"][0]["label"]["en"] = "changed product"
    first_read = authored.as_dict()
    node(first_read, "work")["inputs"]["requirements"]["node"] = "changed read"
    first_read["nodes"].clear()
    assert authored.as_dict() == expected
    assert json.loads(authored.to_json()) == expected


def test_definition_does_not_retain_or_serialize_product_credentials():
    configured = {
        **product(),
        "webhook_secret": "synthetic-secret-not-in-definition",
        "webhook_url": "https://private-worker.example/run",
    }
    authored = FlowDefinition.from_dict(graph(), product=configured)
    assert "synthetic-secret-not-in-definition" not in authored.to_json()
    assert "private-worker.example" not in authored.to_json()
    assert "synthetic-secret-not-in-definition" not in repr(authored)


def test_schema_returns_independent_structural_contract_copies():
    authored = FlowDefinition.from_dict(graph(), product=product())
    first = FlowDefinition.schema()
    expected = deepcopy(first)
    body = next(
        candidate for candidate in first["oneOf"] if candidate["type"] == "object"
    )
    assert {"version", "entry", "nodes"} <= set(body["properties"])
    assert set(body["required"]) == {"version", "entry", "nodes"}
    body["properties"]["nodes"].clear()
    assert FlowDefinition.schema() == expected
    assert authored.schema() == expected


def test_branch_helpers_preserve_first_match_order_and_use_data_only_conditions():
    alternatives = ["skip", "cancel"]
    cases = [
        (FlowDefinition.equals("ask", "requirements", "redo"), "ask"),
        (FlowDefinition.one_of("ask", "requirements", alternatives), "failed"),
        (FlowDefinition.exists("work", "answer"), "done"),
    ]
    draft = graph()
    route = FlowDefinition.branch(cases, default="done")
    node(draft, "work")["next"] = route
    authored = FlowDefinition.from_dict(draft, product=product())
    normalized = node(authored.as_dict(), "work")["next"]
    assert [case["to"] for case in normalized["cases"]] == [
        "ask",
        "failed",
        "done",
    ]
    assert normalized["cases"][0]["when"] == {
        "source": {"node": "ask", "field": "requirements"},
        "op": "eq",
        "value": "redo",
    }
    assert normalized["cases"][1]["when"]["value"] == ["skip", "cancel"]
    assert normalized["cases"][2]["when"] == {
        "source": {"node": "work", "field": "answer"},
        "op": "exists",
    }
    alternatives.append("changed")
    cases[0][0]["value"] = "changed"
    assert node(authored.as_dict(), "work")["next"] == normalized


@pytest.mark.parametrize(
    "mutation",
    ["executor", "condition_secret", "display_secret", "terminal_secret"],
)
def test_authoring_cannot_expand_execution_authority_or_expose_sensitive_inputs(
    mutation,
):
    draft = graph()
    if mutation == "executor":
        node(draft, "work")["command"] = "private-value-must-not-be-echoed"
    else:
        node(draft, "ask")["fields"][0]["sensitive"] = True
        if mutation == "condition_secret":
            node(draft, "work")["next"] = FlowDefinition.branch(
                [(FlowDefinition.exists("ask", "requirements"), "done")],
                default="failed",
            )
        elif mutation == "display_secret":
            node(draft, "ask")["show_from"] = {
                "previous": FlowDefinition.reference("ask", "requirements")
            }
        else:
            node(draft, "done")["result"] = {
                "answer": FlowDefinition.reference("ask", "requirements")
            }
    with pytest.raises(ValueError) as error:
        FlowDefinition.from_dict(draft, product=product())
    assert "private-value-must-not-be-echoed" not in str(error.value)


@pytest.mark.parametrize("comparison", [True, 1, None, ["yes"], {"x": "yes"}])
def test_branch_comparisons_do_not_coerce_arbitrary_values_to_text(comparison):
    with pytest.raises(ValueError):
        condition = FlowDefinition.equals("ask", "requirements", comparison)
        draft = graph()
        node(draft, "work")["next"] = FlowDefinition.branch(
            [(condition, "done")], default="failed"
        )
        FlowDefinition.from_dict(draft, product=product())


SCOPE = FlowScope("shop-a", "product-a", "job-a", 3, "work", 7, "action-a")


class RecordingOpener:
    def __init__(self):
        self.requests = []

    def open(self, request, *, timeout):
        assert timeout == 30
        self.requests.append(request)
        if request.method == "GET":
            return io.BytesIO(b"fixture-source")
        return io.BytesIO(b'{"ok":true,"file":{"id":"file-a"}}')


def client_execution():
    opener = RecordingOpener()
    client = PrivateWorkerClient(
        "https://extore.example", "fixture-private-worker-secret", opener=opener
    )
    return client.execution(SCOPE), opener


def test_execution_helpers_send_lifecycle_updates_with_exact_fixed_scope():
    execution, opener = client_execution()
    execution.started("started-1", message="Reading the inputs")
    execution.progress("progress-1", 40, message="Working")
    execution.succeed("result-1", {"answer": "  original answer\n"}, message="Ready")
    bodies = [json.loads(request.data) for request in opener.requests]
    assert [body["result_id"] for body in bodies] == [
        "started-1",
        "progress-1",
        "result-1",
    ]
    assert [body["update"]["state"] for body in bodies] == [
        "processing",
        "processing",
        "succeeded",
    ]
    assert bodies[1]["update"]["progress"] == 40
    assert bodies[2]["update"]["output"] == {"answer": "  original answer\n"}
    for request, body in zip(opener.requests, bodies, strict=True):
        assert request.full_url == (
            "https://extore.example/api/callbacks/v2/product-a/job-a/result"
        )
        assert body["update"]["attempt"] == 3
        assert body["update"]["flow_epoch"] == 7
        assert body["update"]["action_id"] == "action-a"
        for key, expected in (
            ("Shop-id", "shop-a"),
            ("Product-id", "product-a"),
            ("Job-id", "job-a"),
            ("Node-id", "work"),
            ("Attempt", "3"),
            ("Flow-epoch", "7"),
            ("Action-id", "action-a"),
        ):
            assert request.get_header(f"X-extore-{key.lower()}") == expected


def test_execution_retry_keeps_business_body_stable_and_renews_transport_nonce():
    execution, opener = client_execution()
    for _ in range(2):
        execution.succeed("result-1", {"answer": "same result"})
    first, second = opener.requests
    assert first.data == second.data
    assert first.get_header("X-extore-nonce") != second.get_header("X-extore-nonce")


@pytest.mark.parametrize("retryable", [False, True])
def test_execution_failure_is_explicit_and_does_not_supply_success_output(retryable):
    execution, opener = client_execution()
    execution.fail(
        "failure-1", "The external service was unavailable", retryable=retryable
    )
    update = json.loads(opener.requests[0].data)["update"]
    assert update["state"] == "failed"
    assert update["retryable"] is retryable
    assert update["message"] == "The external service was unavailable"
    assert "output" not in update


@pytest.mark.parametrize("progress", [-1, 100, True, "50", float("nan"), 1.5])
def test_execution_rejects_invalid_progress_before_network_io(progress):
    execution, opener = client_execution()
    with pytest.raises(ValueError):
        execution.progress("progress-1", progress)
    assert opener.requests == []


@pytest.mark.parametrize(
    "output", [None, "answer", [], {"answer": 1}, {"answer": None}, {1: "answer"}]
)
def test_execution_rejects_non_string_output_mapping_before_network_io(output):
    execution, opener = client_execution()
    with pytest.raises(ValueError):
        execution.succeed("result-1", output)
    assert opener.requests == []


@pytest.mark.parametrize(
    "output",
    [
        {"../field": "answer"},
        {"Field": "answer"},
        {"f" * 41: "answer"},
        {f"field_{index}": "answer" for index in range(31)},
    ],
)
def test_execution_rejects_unbounded_or_invalid_output_names_before_network_io(output):
    execution, opener = client_execution()
    with pytest.raises(ValueError):
        execution.succeed("result-1", output)
    assert opener.requests == []


@pytest.mark.parametrize("message", [None, 1, "x" * 1001])
def test_execution_rejects_invalid_customer_messages_before_network_io(message):
    execution, opener = client_execution()
    with pytest.raises(ValueError):
        execution.progress("progress-1", 1, message=message)
    assert opener.requests == []


@pytest.mark.parametrize("retryable", ["true", 1, None])
def test_execution_retryability_requires_a_real_boolean_before_network_io(retryable):
    execution, opener = client_execution()
    with pytest.raises(ValueError):
        execution.fail("failure-1", "Failed", retryable=retryable)
    assert opener.requests == []


@pytest.mark.parametrize("result_id", ["", "../escape", "result\n", None])
def test_execution_requires_an_explicit_valid_result_id_before_network_io(result_id):
    execution, opener = client_execution()
    with pytest.raises(ValueError):
        execution.started(result_id)
    assert opener.requests == []


def test_execution_rejects_an_unvalidated_scope_and_scope_is_immutable():
    opener = RecordingOpener()
    client = PrivateWorkerClient(
        "https://extore.example", "fixture-private-worker-secret", opener=opener
    )
    with pytest.raises(ValueError):
        client.execution({"job_id": "customer-controlled"})
    with pytest.raises(FrozenInstanceError):
        SCOPE.flow_epoch = 99
    execution = client.execution(SCOPE)
    with pytest.raises(FrozenInstanceError):
        execution.scope = FlowScope(
            "shop-a", "product-a", "job-a", 3, "work", 99, "action-other"
        )
    assert opener.requests == []


def test_execution_file_helpers_use_the_same_stage_scope_and_output_identity(tmp_path):
    execution, opener = client_execution()
    source = tmp_path / "result.txt"
    source.write_bytes(b"fixture-delivery")
    assert execution.upload("delivery_file", source, result_id="upload-1")["ok"]
    assert execution.download("brief_file", "file-a") == b"fixture-source"
    upload, download = opener.requests
    assert upload.full_url.endswith("/files/delivery_file/upload-1/upload")
    assert download.full_url.endswith("/files/brief_file/file-a/download")
    for request in opener.requests:
        assert request.get_header("X-extore-node-id") == "work"
        assert request.get_header("X-extore-flow-epoch") == "7"
        assert request.get_header("X-extore-action-id") == "action-a"


def test_execution_helpers_complete_real_flow_and_keep_result_retry_idempotent(
    owner,
    monkeypatch,
):
    origin, secret = (
        "https://extore.example.test",
        "isolated-helper-worker-secret-not-a-production-key",
    )
    monkeypatch.setattr(private_worker, "ORIGIN", origin)
    configured = {
        **product(),
        "name": "Synthetic SDK helper flow",
        "mode": "webhook",
        "webhook_url": "https://private-worker.example/run",
        "webhook_secret": secret,
    }
    configured["task_flow"] = FlowDefinition.from_dict(
        graph(), product=configured
    ).as_dict()
    created = owner.post("/api/admin/products", json=configured)
    assert created.status_code == 200, created.text
    issued = owner.post(
        "/api/admin/cards", json={"product_id": created.json()["id"], "count": 1}
    )
    assert issued.status_code == 200, issued.text
    exchanged = owner.post("/api/exchange", json={"code": issued.json()["codes"][0]})
    assert exchanged.status_code == 200, exchanged.text
    token = exchanged.json()["token"]
    started = owner.post(
        "/api/task-flow/start",
        json={"token": token, "flow_epoch": 0, "expected_revision": 0},
    )
    assert started.status_code == 200, started.text
    current = started.json()["task_flow"]
    answered = owner.post(
        "/api/task-flow/answer",
        json={
            "token": token,
            "flow_epoch": current["flow_epoch"],
            "expected_revision": current["revision"],
            "values": {"requirements": "Produce a short synthetic answer"},
        },
    )
    assert answered.status_code == 200, answered.text
    with db() as connection:
        context = task_flow.execution(
            connection, job(connection, answered.json()["id"])
        )
        scope = FlowScope.from_context(context)
    transport = ActualApiOpener(owner)
    execution = PrivateWorkerClient(origin, secret, opener=transport).execution(scope)
    execution.started("started-one", message="Checking requirements")
    execution.progress("progress-one", 55, message="Writing the answer")
    pending = owner.post("/api/receipt", json={"token": token}).json()["job"]
    assert pending["state"] == "processing" and pending["progress"] == 55
    assert pending["message"] == "Writing the answer"
    acknowledgement = execution.succeed("result-one", {"answer": "Synthetic answer"})
    assert (
        execution.succeed("result-one", {"answer": "Synthetic answer"})
        == acknowledgement
    )
    completed = owner.post("/api/receipt", json={"token": token}).json()["job"]
    assert completed["state"] == "succeeded"
    revealed = owner.post("/api/receipt/reveal", json={"token": token})
    assert revealed.status_code == 200, revealed.text
    assert revealed.json()["output"] == {"answer": "Synthetic answer"}
    with pytest.raises(ValueError):
        execution.succeed("result-one", {"answer": "Changed result"})
    assert transport.responses[-1].status_code == 409
    with pytest.raises(ValueError):
        execution.progress("late-progress", 99)
    assert transport.responses[-1].status_code == 409
    with db() as connection:
        count = connection.execute(
            "SELECT count(*) FROM private_worker_receipts WHERE result_id='result-one'"
        ).fetchone()[0]
    assert count == 1

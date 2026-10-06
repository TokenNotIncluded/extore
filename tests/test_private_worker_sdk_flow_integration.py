"""SDK-to-actual-flow acceptance; all cards, files and keys are test fixtures."""

import io
import urllib.error
from urllib.parse import urlsplit

import pytest

from extore import private_worker, task_flow
from extore.db import db
from extore.sdk import FlowScope, PrivateWorkerClient
from extore.service import job

SECRET = "isolated-sdk-worker-secret-not-a-production-key"
HTTPS_ORIGIN = "https://extore.example.test"


def field(key, kind="text", **extra):
    return {
        "key": key,
        "label": {"en": key},
        "type": kind,
        "required": True,
        **extra,
    }


def definition():
    nodes = []
    for index in (1, 2):
        nodes.extend(
            [
                {
                    "id": f"q{index}",
                    "kind": "input",
                    "prompt": {"en": "Provide requirements"},
                    "question": {"en": "What should be made?"},
                    "fields": [field("answer"), field("material", "file")],
                    "start_policy": "confirm" if index == 1 else "automatic",
                    "show_from": {}
                    if index == 1
                    else {"previous": {"node": "p1", "field": "answer"}},
                    "timeout_seconds": 60,
                    "timeout_next": "failed",
                    "next": f"p{index}",
                },
                {
                    "id": f"p{index}",
                    "kind": "process",
                    "inputs": {
                        "answer": {"node": f"q{index}", "field": "answer"},
                        "material": {"node": f"q{index}", "field": "material"},
                    },
                    "outputs": [field("answer"), field("artifact", "file")],
                    "timeout_seconds": 120,
                    "timeout_next": "failed",
                    "failure_next": "failed",
                    "next": "q2" if index == 1 else "done",
                },
            ]
        )
    nodes.extend(
        [
            {
                "id": "done",
                "kind": "end",
                "state": "succeeded",
                "result": {
                    "content": {"node": "p2", "field": "answer"},
                    "final_file": {"node": "p2", "field": "artifact"},
                },
            },
            {
                "id": "failed",
                "kind": "end",
                "state": "failed",
                "message": {"en": "Expired"},
                "retryable": True,
            },
        ]
    )
    return {"version": 1, "entry": "q1", "nodes": nodes}


class ActualApiOpener:
    """Only redirect the transport to TestClient; actual routes/auth stay real."""

    def __init__(self, client):
        self.client = client
        self.responses = []

    def open(self, request, *, timeout):
        assert timeout == 30
        assert urlsplit(request.full_url).netloc == "extore.example.test"
        response = self.client.request(
            request.get_method(),
            urlsplit(request.full_url).path,
            content=request.data,
            headers=dict(request.header_items()),
        )
        self.responses.append(response)
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                request.full_url,
                response.status_code,
                "Synthetic API error",
                response.headers,
                io.BytesIO(response.content),
            )
        return io.BytesIO(response.content)


def upload_and_answer(owner, token, current, text):
    flow = current["task_flow"]
    response = owner.post(
        "/api/files/upload",
        data={
            "token": token,
            "field_key": "material",
            "flow_epoch": str(flow["flow_epoch"]),
            "expected_revision": str(flow["revision"]),
            "node_id": flow["current"]["id"],
        },
        files={"file": ("source.txt", text.encode(), "text/plain")},
    )
    assert response.status_code == 200, response.text
    fid = response.json()["id"]
    response = owner.post(
        "/api/task-flow/answer",
        json={
            "token": token,
            "flow_epoch": flow["flow_epoch"],
            "expected_revision": flow["revision"],
            "values": {"answer": text, "material": fid},
        },
    )
    assert response.status_code == 200, response.text
    with db() as c:
        context = task_flow.execution(c, job(c, current["id"]))
        assert context is not None and context["mode"] == "webhook"
    return response.json(), fid, FlowScope.from_context(context)


def test_sdk_actual_two_round_flow_files_and_result_recovery(
    owner, monkeypatch, tmp_path
):
    monkeypatch.setattr(private_worker, "ORIGIN", HTTPS_ORIGIN)
    response = owner.post(
        "/api/admin/products",
        json={
            "name": "Isolated two-round SDK workflow",
            "mode": "webhook",
            "parameters": [],
            "outputs": [field("content"), field("final_file", "file")],
            "webhook_url": "https://worker.example.test/run",
            "webhook_secret": SECRET,
            "task_flow": definition(),
        },
    )
    assert response.status_code == 200, response.text
    pid = response.json()["id"]
    issued = owner.post("/api/admin/cards", json={"product_id": pid, "count": 1})
    assert issued.status_code == 200, issued.text
    exchanged = owner.post("/api/exchange", json={"code": issued.json()["codes"][0]})
    assert exchanged.status_code == 200, exchanged.text
    token = exchanged.json()["token"]
    started = owner.post(
        "/api/task-flow/start",
        json={"token": token, "flow_epoch": 0, "expected_revision": 0},
    )
    assert started.status_code == 200, started.text
    first, input_id, scope = upload_and_answer(
        owner, token, started.json(), "first input"
    )
    transport = ActualApiOpener(owner)
    sdk = PrivateWorkerClient(HTTPS_ORIGIN, SECRET, opener=transport)
    assert sdk.download(scope, "material", input_id) == b"first input"
    with pytest.raises(ValueError):
        sdk.download(scope, "answer", input_id)
    assert transport.responses[-1].status_code in (400, 403)

    source = tmp_path / "first.txt"
    source.write_bytes(b"first generated file")
    sdk.update(scope, "started-one", state="processing")
    file_ack = sdk.upload(scope, "artifact", source, result_id="file-one")
    assert sdk.upload(scope, "artifact", source, result_id="file-one") == file_ack
    with db() as c:
        assert (
            c.execute("SELECT count(*) FROM job_files WHERE kind='output'").fetchone()[
                0
            ]
            == 1
        )
    output = {"answer": "first result", "artifact": file_ack["file"]["id"]}
    ack = sdk.update(scope, "result-one", state="succeeded", output=output)
    assert sdk.update(scope, "result-one", state="succeeded", output=output) == ack
    with pytest.raises(ValueError):
        sdk.update(
            scope,
            "result-one",
            state="succeeded",
            output={**output, "answer": "changed"},
        )
    assert transport.responses[-1].status_code == 409
    with pytest.raises(ValueError):
        sdk.update(scope, "different-old-result", state="processing", progress=90)
    assert transport.responses[-1].status_code == 409

    receipt = owner.post("/api/receipt", json={"token": token}).json()["job"]
    assert receipt["state"] == "waiting"
    assert receipt["task_flow"]["current"]["id"] == "q2"
    assert scope.flow_epoch < receipt["task_flow"]["flow_epoch"]
    _, next_input, next_scope = upload_and_answer(owner, token, receipt, "second input")
    assert next_scope.flow_epoch > scope.flow_epoch
    assert next_scope.action_id != scope.action_id
    with pytest.raises(ValueError):
        sdk.download(next_scope, "material", input_id)
    assert transport.responses[-1].status_code == 403
    assert sdk.download(next_scope, "material", next_input) == b"second input"
    final_source = tmp_path / "final.txt"
    final_source.write_bytes(b"final generated file")
    sdk.update(next_scope, "started-two", state="processing")
    final = sdk.upload(next_scope, "artifact", final_source, result_id="file-two")
    sdk.update(
        next_scope,
        "result-two",
        state="succeeded",
        output={"answer": "final result", "artifact": final["file"]["id"]},
    )
    completed = owner.post("/api/receipt", json={"token": token}).json()["job"]
    assert completed["state"] == "succeeded"
    reveal = owner.post("/api/receipt/reveal", json={"token": token})
    assert reveal.status_code == 200, reveal.text
    assert reveal.json()["output"] == {
        "content": "final result",
        "final_file": final["file"]["id"],
    }
    downloaded = owner.post(
        "/api/files/download", json={"token": token, "file_id": final["file"]["id"]}
    )
    assert (
        downloaded.status_code == 200 and downloaded.content == b"final generated file"
    )
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM private_worker_receipts WHERE result_id IN ('result-one','result-two')"
            ).fetchone()[0]
            == 2
        )
        assert (
            c.execute(
                "SELECT count(*) FROM task_flow_steps WHERE state='completed' AND kind IN ('input','process')"
            ).fetchone()[0]
            == 4
        )

"""Real flow/storage/signature regression, with synthetic cards and a fake clock."""

import json
import time
from types import SimpleNamespace

import pytest
from test_task_flow_core import answer_current, field, graph, setup

from extore import flow_adapter, private_worker, task_flow
from extore.db import db
from extore.service import job

SECRET = "s" * 40


@pytest.fixture
def running_private_flow(owner, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(task_flow, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setattr(flow_adapter, "time", SimpleNamespace(time=lambda: now[0]))
    definition = graph(1)
    definition["nodes"][0]["fields"][0].update(sensitive=True, sensitive_ttl_seconds=1)
    definition["nodes"][1]["outputs"].append(
        field("document", type="file", required=False)
    )
    pid, card_id, _ = setup(owner, definition, mode="webhook")
    with db() as c:
        entered = answer_current(c, "001234")
        context = task_flow.execution(c, entered["row"])
        flow_adapter.finalize(
            c,
            task_flow.claim(
                c, entered["row"], "synthetic-worker", context["flow_epoch"]
            ),
        )
        assert task_flow.execution(c, job(c, "task"))["params"] == {"answer": "001234"}
    return private_worker.WorkerScope.from_context(context), now, card_id


def post(owner, scope, *, state="succeeded", output=None, result_id="completed"):
    raw = json.dumps(
        {
            "version": 2,
            "result_id": result_id,
            "update": {"attempt": scope.attempt, "state": state, "output": output},
        },
        separators=(",", ":"),
    ).encode()
    path = f"/api/callbacks/v2/{scope.product_id}/{scope.job_id}/result"
    headers = private_worker.signed_headers(
        SECRET,
        scope,
        direction=private_worker.WORKER_TO_EXTORE,
        audience=private_worker.ORIGIN,
        method="POST",
        path=path,
        body=raw,
    )
    return owner.post(path, content=raw, headers=headers)


def expire_input(scope, clock):
    clock[0] += 2
    with db() as c:
        effects = task_flow.expire_due(c)
        assert len(effects) == 1 and effects[0]["input_expired"]
        assert effects[0]["flow"]["phase"] == "processing"
        assert effects[0]["flow"]["flow_epoch"] == scope.flow_epoch
        flow_adapter.finalize(c, effects[0])
        assert task_flow.execution(c, job(c, scope.job_id)) is None
        assert (
            task_flow.stage_values(c, job(c, scope.job_id), 1, "input")["values"] == {}
        )


def test_processing_input_expiry_accepts_progress_then_completion_and_stable_ack(
    owner, running_private_flow
):
    scope, clock, card_id = running_private_flow
    expire_input(scope, clock)
    assert (
        post(owner, scope, state="processing", result_id="progress").status_code == 200
    )
    first = post(owner, scope, output={"answer": "Synthetic result"})
    second = post(owner, scope, output={"answer": "Synthetic result"})
    assert first.status_code == second.status_code == 200, first.text
    assert first.json() == second.json()
    assert post(owner, scope, output={"answer": "changed"}).status_code == 409
    assert (
        post(
            owner, scope, output={"answer": "Synthetic result"}, result_id="other"
        ).status_code
        == 409
    )
    with db() as c:
        row = job(c, scope.job_id)
        assert row["state"] == "succeeded" and row["attempt"] == 1
        assert json.loads(row["result_json"])["answer_1"] == "Synthetic result"
        assert (
            c.execute("SELECT state FROM cards WHERE id=?", (card_id,)).fetchone()[0]
            == "used"
        )


def test_processing_input_expiry_allows_scoped_output_upload_without_reopening_params(
    owner, running_private_flow, monkeypatch
):
    scope, clock, _ = running_private_flow
    expire_input(scope, clock)

    def never_open_inputs(*args, **kwargs):
        pytest.fail("Output permissions must not materialize expired input parameters")

    monkeypatch.setattr(task_flow, "execution", never_open_inputs)
    raw = b"synthetic output after OTP expiry"
    path = f"/api/callbacks/v2/{scope.product_id}/{scope.job_id}/files/document/upload-one/upload"

    def headers():
        return private_worker.signed_headers(
            SECRET,
            scope,
            direction=private_worker.WORKER_TO_EXTORE,
            audience=private_worker.ORIGIN,
            method="POST",
            path=path,
            body=raw,
        )

    uploaded = owner.post(path, content=raw, headers=headers())
    assert uploaded.status_code == 200, uploaded.text
    assert owner.post(path, content=raw, headers=headers()).json() == uploaded.json()
    with db() as c:
        fid = uploaded.json()["file"]["id"]
        stored = c.execute("SELECT * FROM job_files WHERE id=?", (fid,)).fetchone()
        assert stored["content"] == raw and stored["card_id"] is not None
        binding = c.execute(
            "SELECT * FROM task_flow_files WHERE file_id=?", (fid,)
        ).fetchone()
        assert (
            binding["flow_epoch"] == scope.flow_epoch
            and binding["node_id"] == scope.node_id
        )
        assert binding["attempt"] == scope.attempt and binding["kind"] == "output"
        assert c.execute("SELECT count(*) FROM upload_reservations").fetchone()[0] == 0


def test_expired_input_download_stays_denied(owner, running_private_flow, monkeypatch):
    from extore import files

    scope, clock, _ = running_private_flow
    expire_input(scope, clock)

    def never_load_file(*args, **kwargs):
        pytest.fail("Expired input must be rejected before loading any BLOB")

    monkeypatch.setattr(files, "_file", never_load_file)
    path = f"/api/callbacks/v2/{scope.product_id}/{scope.job_id}/files/answer/fake-file/download"
    headers = private_worker.signed_headers(
        SECRET,
        scope,
        direction=private_worker.WORKER_TO_EXTORE,
        audience=private_worker.ORIGIN,
        method="GET",
        path=path,
        body=b"",
    )
    assert owner.get(path, headers=headers).status_code == 409


def test_real_process_deadline_is_committed_without_receipt_or_card_consumption(
    owner, running_private_flow
):
    scope, clock, card_id = running_private_flow
    clock[0] += 61
    response = post(owner, scope, output={"answer": "too late"})
    assert response.status_code == 409, response.text
    with db() as c:
        row = job(c, scope.job_id)
        assert row["state"] == "failed" and not row["retryable"]
        assert (
            c.execute("SELECT state FROM cards WHERE id=?", (card_id,)).fetchone()[0]
            != "used"
        )
        run = c.execute(
            "SELECT * FROM task_flow_runs WHERE job_id=?", (scope.job_id,)
        ).fetchone()
        assert run["phase"] == "ended" and run["flow_epoch"] > scope.flow_epoch
        assert (
            c.execute(
                "SELECT count(*) FROM private_worker_receipts WHERE job_id=?",
                (scope.job_id,),
            ).fetchone()[0]
            == 0
        )

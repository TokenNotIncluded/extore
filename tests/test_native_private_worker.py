"""Real signed HTTP callbacks with real time and frozen native task flows."""
import time

import pytest
from test_task_flow_http_integration import action, answer, definition, field, launch
from test_private_worker_flow_ttl import SECRET, post

from extore import private_worker
from extore.db import db


@pytest.fixture(autouse=True)
def native_only(pytestconfig):
    if not pytestconfig.pluginmanager.hasplugin("native_http_plugin"):
        pytest.skip("This suite verifies the separate native HTTP server")


def queued(owner, *, sensitive=True):
    graph = definition(rounds=1, sensitive=sensitive)
    graph["nodes"][1]["outputs"].append(field("document", "file", required=False))
    _, token = launch(owner, rounds=1, mode="webhook", webhook_url="https://worker.example.test/receive", webhook_secret=SECRET, task_flow=graph)
    started = action(owner, token, "start")
    assert started.status_code == 200, started.text
    task = answer(owner, token, started.json(), question="synthetic-secret-001234")
    with db() as conn:
        j = conn.execute("SELECT * FROM jobs WHERE id=?", (task["id"],)).fetchone()
        run = conn.execute("SELECT * FROM task_flow_runs WHERE job_id=?", (j["id"],)).fetchone()
        step = conn.execute("SELECT action_id FROM task_flow_steps WHERE job_id=? AND flow_epoch=?", (j["id"], run["flow_epoch"])).fetchone()
        sid = conn.execute("SELECT shop_id FROM products WHERE id=?", (j["product_id"],)).fetchone()[0]
        scope = private_worker.WorkerScope.from_context({"shop_id": sid, "product_id": j["product_id"], "job_id": j["id"], "attempt": j["attempt"], "node_id": run["node_id"], "flow_epoch": run["flow_epoch"], "action_id": step["action_id"]})
        cid = j["card_id"]
    return scope, token, cid


def snapshot(scope):
    with db() as conn:
        return {
            "job": dict(conn.execute("SELECT * FROM jobs WHERE id=?", (scope.job_id,)).fetchone()),
            "run": dict(conn.execute("SELECT * FROM task_flow_runs WHERE job_id=?", (scope.job_id,)).fetchone()),
            "receipts": conn.execute("SELECT count(*) FROM private_worker_receipts WHERE job_id=?", (scope.job_id,)).fetchone()[0],
        }


@pytest.mark.parametrize("state", ["processing", "succeeded", "failed"])
def test_real_expired_queued_secret_rejects_signed_update_without_consuming_card(owner, state):
    scope, _, cid = queued(owner)
    time.sleep(1.1)
    response = post(owner, scope, state=state, output={"answer": "too late"} if state == "succeeded" else None)
    assert response.status_code == 409, response.text
    current = snapshot(scope)
    assert current["job"]["state"] == current["run"]["phase"] == "queued"
    assert current["receipts"] == 0
    with db() as conn:
        assert conn.execute("SELECT state FROM cards WHERE id=?", (cid,)).fetchone()[0] != "used"


@pytest.mark.parametrize("running", [False, True])
def test_real_expired_process_commits_terminal_state_before_conflict(owner, running):
    scope, _, cid = queued(owner, sensitive=False)
    if running:
        response = post(owner, scope, state="processing", result_id="started")
        assert response.status_code == 200, response.text
    with db() as conn:
        conn.execute("UPDATE task_flow_runs SET deadline=? WHERE job_id=?", (time.time() - 1, scope.job_id))
        conn.execute("UPDATE task_flow_steps SET deadline=? WHERE job_id=? AND flow_epoch=?", (time.time() - 1, scope.job_id, scope.flow_epoch))
    response = post(owner, scope, output={"answer": "too late"})
    assert response.status_code == 409, response.text
    current = snapshot(scope)
    assert current["job"]["state"] == "failed"
    assert current["run"]["phase"] == "ended" and current["run"]["flow_epoch"] > scope.flow_epoch
    if running:
        assert not current["job"]["retryable"]
    assert current["receipts"] == int(running)
    with db() as conn:
        assert conn.execute("SELECT state FROM cards WHERE id=?", (cid,)).fetchone()[0] != "used"


def test_started_process_completes_after_secret_expiry_and_ack_is_stable(owner):
    scope, token, _ = queued(owner)
    started = post(owner, scope, state="processing", result_id="started")
    assert started.status_code == 200, started.text
    time.sleep(1.1)
    progress = post(owner, scope, state="processing", result_id="progress")
    assert progress.status_code == 200, progress.text
    first = post(owner, scope, output={"answer": "Native delivery"})
    assert first.status_code == 200, first.text
    retry = post(owner, scope, output={"answer": "Native delivery"})
    assert retry.status_code == 200 and retry.json() == first.json()
    assert post(owner, scope, output={"answer": "Changed delivery"}).status_code == 409
    assert post(owner, scope, output={"answer": "Native delivery"}, result_id="other").status_code == 409
    result = owner.post("/api/receipt/reveal", json={"token": token})
    assert result.status_code == 200 and result.json()["output"] == {"answer_1": "Native delivery"}


def test_scoped_output_upload_after_secret_expiry_deduplicates_and_input_stays_closed(owner):
    scope, _, _ = queued(owner)
    started = post(owner, scope, state="processing", result_id="started")
    assert started.status_code == 200, started.text
    time.sleep(1.1)
    raw = b"native output after input expiry"
    path = f"/api/callbacks/v2/{scope.product_id}/{scope.job_id}/files/document/upload-one/upload"
    def headers(method, target, body):
        return private_worker.signed_headers(SECRET, scope, direction=private_worker.WORKER_TO_EXTORE, audience=private_worker.ORIGIN, method=method, path=target, body=body)
    first = owner.post(path, content=raw, headers=headers("POST", path, raw))
    assert first.status_code == 200, first.text
    retry = owner.post(path, content=raw, headers=headers("POST", path, raw))
    assert retry.status_code == 200 and retry.json() == first.json()
    with db() as conn:
        fid = first.json()["file"]["id"]
        saved = conn.execute("SELECT content,job_id FROM job_files WHERE id=?", (fid,)).fetchone()
        assert saved["content"] == raw and saved["job_id"] == scope.job_id
        bound = conn.execute("SELECT flow_epoch,kind FROM task_flow_files WHERE file_id=?", (fid,)).fetchone()
        assert bound["flow_epoch"] == scope.flow_epoch and bound["kind"] == "output"
    path = f"/api/callbacks/v2/{scope.product_id}/{scope.job_id}/files/question/fake-file/download"
    denied = owner.get(path, headers=headers("GET", path, b""))
    assert denied.status_code == 409, denied.text

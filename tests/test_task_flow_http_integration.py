"""Issued-card/task-flow acceptance through the real HTTP and file adapters."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from extore import flow_adapter, task_flow
from extore.db import db
from extore.models import JobUpdate
from extore.service import job


def field(key, kind="text", **extra):
    return {"key": key, "type": kind, "label": {"en": key}, "required": True, **extra}


def definition(rounds=3, *, file_output=False, sensitive=False):
    nodes = []
    for index in range(1, rounds + 1):
        qid, pid = f"q{index}", f"p{index}"
        inputs = (
            [field("question", sensitive=True, sensitive_ttl_seconds=1)]
            if sensitive
            else [field("question")]
        )
        if file_output:
            inputs.append(field("source", "file"))
        nodes.extend(
            [
                {
                    "id": qid,
                    "kind": "input",
                    "prompt": {"en": "Prepare to start"},
                    "question": {"en": f"Private question {index}"},
                    "fields": inputs,
                    "start_policy": "confirm" if index == 1 else "automatic",
                    "show_from": {}
                    if index == 1
                    else {"previous": {"node": f"p{index - 1}", "field": "answer"}},
                    "timeout_seconds": 30,
                    "timeout_next": "failed",
                    "next": pid,
                },
                {
                    "id": pid,
                    "kind": "process",
                    "inputs": {
                        item["key"]: {"node": qid, "field": item["key"]}
                        for item in inputs
                    },
                    "outputs": [field("draft", "file")]
                    if file_output
                    else [field("answer")],
                    "timeout_seconds": 60,
                    "timeout_next": "failed",
                    "failure_next": "failed",
                    "next": "done" if index == rounds else f"q{index + 1}",
                },
            ]
        )
    nodes.extend(
        [
            {
                "id": "done",
                "kind": "end",
                "state": "succeeded",
                "result": {"artifact": {"node": "p1", "field": "draft"}}
                if file_output
                else {
                    f"answer_{i}": {"node": f"p{i}", "field": "answer"}
                    for i in range(1, rounds + 1)
                },
            },
            {
                "id": "failed",
                "kind": "end",
                "state": "failed",
                "message": {"en": "Question timed out"},
                "retryable": True,
            },
        ]
    )
    return {"version": 1, "entry": "q1", "nodes": nodes}


def launch(owner, *, rounds=3, file_output=False, sensitive=False, **options):
    graph = definition(rounds, file_output=file_output, sensitive=sensitive)
    outputs = (
        [field("artifact", "file")]
        if file_output
        else [field(f"answer_{i}") for i in range(1, rounds + 1)]
    )
    response = owner.post(
        "/api/admin/products",
        json={
            "name": "Aladdin synthetic flow",
            "mode": "manual",
            "parameters": [],
            "outputs": outputs,
            "task_flow": graph,
            "allow_retry": True,
            "max_attempts": 3,
            **options,
        },
    )
    assert response.status_code == 200, response.text
    product = response.json()
    assert product.get("task_flow"), product
    issued = owner.post(
        "/api/admin/cards", json={"product_id": product["id"], "count": 1}
    )
    assert issued.status_code == 200, issued.text
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM card_task_flows WHERE product_id=?",
                (product["id"],),
            ).fetchone()[0]
            == 1
        )
    exchanged = owner.post("/api/exchange", json={"code": issued.json()["codes"][0]})
    assert exchanged.status_code == 200, exchanged.text
    payload = exchanged.json()
    assert payload["job"] is None
    assert "Private question" not in json.dumps(payload)
    preview = payload["product"]["task_flow_view"]
    assert preview["phase"] == "await_start" and preview["deadline"] is None
    assert "fields" not in preview["current"]
    return product, payload["token"]


def receipt(owner, token):
    response = owner.post("/api/receipt", json={"token": token})
    assert response.status_code == 200, response.text
    return response.json()["job"]


def action(owner, token, operation, row=None, **extra):
    state = row["task_flow"] if row else {"flow_epoch": 0, "revision": 0}
    return owner.post(
        f"/api/task-flow/{operation}",
        json={
            "token": token,
            "flow_epoch": state["flow_epoch"],
            "expected_revision": state["revision"],
            **extra,
        },
    )


def batch(owner, row, operation, **extra):
    return owner.post(
        "/api/manage/batch",
        json={
            "product_id": row["product_id"],
            "ids": [row["id"]],
            "action": operation,
            "flow_epoch": row["task_flow"]["flow_epoch"],
            "attempt": row["attempt"],
            **extra,
        },
    )


def answer(owner, token, row, **values):
    response = action(
        owner,
        token,
        "answer",
        row,
        values=values or {"question": "Synthetic requirement"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def finish(owner, token, row, output):
    claimed = batch(owner, row, "claim")
    assert claimed.status_code == 200, claimed.text
    response = batch(owner, row, "succeed", output=output)
    assert response.status_code == 200, response.text
    return receipt(owner, token)


def state_snapshot():
    with db() as c:
        return {
            name: [dict(row) for row in c.execute("SELECT * FROM " + name)]
            for name in (
                "cards",
                "jobs",
                "card_task_flows",
                "task_flow_runs",
                "task_flow_steps",
                "events",
                "job_files",
                "task_flow_files",
            )
        }


def test_real_http_aladdin_three_rounds_only_consumes_and_delivers_at_end(
    owner, monkeypatch
):
    now = [time.time() + 1]
    monkeypatch.setattr(task_flow, "time", SimpleNamespace(time=lambda: now[0]))
    product, token = launch(owner)
    response = action(owner, token, "start")
    assert response.status_code == 200, response.text
    row = response.json()
    first_deadline = row["task_flow"]["deadline"]
    assert row["state"] == "waiting"
    assert row["task_flow"]["current"]["question"] == {"en": "Private question 1"}
    now[0] += 4
    again = action(owner, token, "start", row)
    assert (
        again.status_code == 200
        and again.json()["task_flow"]["deadline"] == first_deadline
    )
    for index in range(1, 4):
        assert row["task_flow"]["current"]["id"] == f"q{index}"
        queued = answer(owner, token, row, question=f"Customer round {index}")
        assert queued["state"] == "queued" and queued["attempt"] == 1
        with db() as c:
            raw = job(c, queued["id"])
            assert raw["result_json"] is None
            assert (
                c.execute(
                    "SELECT state FROM cards WHERE id=?", (raw["card_id"],)
                ).fetchone()[0]
                == "reserved"
            )
        assert (
            owner.post("/api/receipt/reveal", json={"token": token}).status_code == 409
        )
        now[0] += 1
        row = finish(owner, token, queued, {"answer": f"Bot answer {index}"})
        if index < 3:
            assert row["state"] == "waiting" and row["task_flow"]["phase"] == "input"
            assert row["task_flow"]["deadline"] == now[0] + 30
            assert row["task_flow"]["shown"][0]["value"] == f"Bot answer {index}"
        else:
            assert row["state"] == "succeeded"
            assert "Bot answer" not in json.dumps(row)
    revealed = owner.post("/api/receipt/reveal", json={"token": token})
    assert revealed.status_code == 200, revealed.text
    assert revealed.json()["output"] == {
        f"answer_{i}": f"Bot answer {i}" for i in range(1, 4)
    }
    with db() as c:
        assert (
            c.execute(
                "SELECT state FROM cards WHERE product_id=?", (product["id"],)
            ).fetchone()[0]
            == "used"
        )
        assert (
            c.execute(
                "SELECT count(*) FROM events WHERE type='fulfillment.succeeded'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("stage", ["input", "queued", "succeeded", "destroyed"])
def test_restart_requires_failed_or_needs_input_and_does_not_change_other_stages(
    owner, stage
):
    _, token = launch(owner, rounds=1)
    response = action(owner, token, "start")
    assert response.status_code == 200, response.text
    row = response.json()
    if stage != "input":
        row = answer(owner, token, row)
    if stage in ("succeeded", "destroyed"):
        row = finish(owner, token, row, {"answer": "Final"})
    if stage == "destroyed":
        assert (
            owner.post("/api/receipt/destroy", json={"token": token}).status_code == 200
        )
        row = receipt(owner, token)
    before = state_snapshot()
    response = action(owner, token, "restart", row)
    assert response.status_code == 409, response.text
    assert state_snapshot() == before


def test_question_timeout_commits_failure_and_bounded_restart_uses_new_epoch(
    owner, monkeypatch
):
    now = [time.time() + 1]
    monkeypatch.setattr(task_flow, "time", SimpleNamespace(time=lambda: now[0]))
    _, token = launch(owner, rounds=1, max_attempts=2)
    row = action(owner, token, "start").json()
    for attempt in (1, 2):
        now[0] = row["task_flow"]["deadline"]
        late = action(owner, token, "answer", row, values={"question": "Late"})
        assert late.status_code == 409, late.text
        ended = receipt(owner, token)
        assert ended["state"] == "failed" and ended["attempt"] == attempt
        assert ended["can_retry"] is (attempt == 1)
        if attempt == 1:
            restart = action(owner, token, "restart", ended)
            assert restart.status_code == 200, restart.text
            waiting = restart.json()
            assert (
                waiting["attempt"] == 2
                and waiting["task_flow"]["phase"] == "await_start"
            )
            assert waiting["task_flow"]["deadline"] is None
            assert waiting["task_flow"]["flow_epoch"] > ended["task_flow"]["flow_epoch"]
            row = action(owner, token, "start", waiting).json()
        else:
            before = state_snapshot()
            assert action(owner, token, "restart", ended).status_code == 409
            assert state_snapshot() == before


def test_processing_timeout_is_not_automatically_restartable(owner, monkeypatch):
    now = [time.time() + 1]
    monkeypatch.setattr(task_flow, "time", SimpleNamespace(time=lambda: now[0]))
    _, token = launch(owner, rounds=1)
    row = answer(owner, token, action(owner, token, "start").json())
    assert batch(owner, row, "claim").status_code == 200
    now[0] = row["task_flow"]["deadline"]
    with db() as c:
        effects = task_flow.expire_due(c)
        assert len(effects) == 1 and effects[0]["terminal"]["needs_review"]
        flow_adapter.finalize(c, effects[0])
    failed = receipt(owner, token)
    assert failed["state"] == "failed" and failed["can_retry"] is False
    assert action(owner, token, "restart", failed).status_code == 409


def test_queue_requests_reentry_then_customer_restarts_without_using_card(owner):
    _, token = launch(owner, rounds=1, allow_retry=False, max_attempts=1)
    row = answer(owner, token, action(owner, token, "start").json())
    assert batch(owner, row, "claim").status_code == 200
    returned = batch(
        owner, row, "request_retry", message="Input does not meet requirements"
    )
    assert returned.status_code == 200, returned.text
    needs_input = receipt(owner, token)
    assert needs_input["state"] == "needs_input" and needs_input["can_retry"] is True
    restart = action(owner, token, "restart", needs_input)
    assert restart.status_code == 200, restart.text
    assert restart.json()["attempt"] == 2
    assert restart.json()["task_flow"]["phase"] == "await_start"
    assert restart.json()["task_flow"]["deadline"] is None
    with db() as c:
        assert c.execute("SELECT state FROM cards").fetchone()[0] == "reserved"
        assert all(
            task_flow._payload(
                c, task_flow.card_snapshot(c, job(c, row["id"])["card_id"]), step
            )
            == {}
            for step in c.execute("SELECT * FROM task_flow_steps WHERE attempt=1")
        )


def prepare_file_flow(owner, *, view_policy="repeat"):
    _, token = launch(owner, rounds=1, file_output=True, view_policy=view_policy)
    row = action(owner, token, "start").json()
    state = row["task_flow"]
    uploaded = owner.post(
        "/api/files/upload",
        data={
            "token": token,
            "field_key": "source",
            "flow_epoch": str(state["flow_epoch"]),
            "expected_revision": str(state["revision"]),
            "node_id": state["current"]["id"],
        },
        files={"file": ("source.txt", b"Synthetic source", "text/plain")},
    )
    assert uploaded.status_code == 200, uploaded.text
    source = uploaded.json()["id"]
    row = answer(owner, token, row, question="Build a document", source=source)
    assert batch(owner, row, "claim").status_code == 200
    output = owner.post(
        "/api/manage/files/upload",
        data={
            "job_id": row["id"],
            "field_key": "draft",
            "flow_epoch": str(row["task_flow"]["flow_epoch"]),
        },
        files={"file": ("draft.txt", b"Synthetic final document", "text/plain")},
    )
    assert output.status_code == 200, output.text
    fid = output.json()["id"]
    succeeded = batch(owner, row, "succeed", output={"draft": fid})
    assert succeeded.status_code == 200, succeeded.text
    return token, receipt(owner, token), source, fid


def download(owner, token, fid):
    return owner.post("/api/files/download", json={"token": token, "file_id": fid})


def test_stage_file_promotes_to_frozen_final_field_and_destroy_erases_everything(owner):
    token, row, source, fid = prepare_file_flow(owner)
    assert download(owner, token, fid).status_code == 409
    revealed = owner.post("/api/receipt/reveal", json={"token": token})
    assert revealed.status_code == 200, revealed.text
    assert revealed.json()["output"] == {"artifact": fid}
    assert download(owner, token, fid).content == b"Synthetic final document"
    assert download(owner, token, source).status_code == 403
    with db() as c:
        assert (
            c.execute("SELECT field_key FROM job_files WHERE id=?", (fid,)).fetchone()[
                0
            ]
            == "artifact"
        )
        scoped = c.execute(
            "SELECT * FROM task_flow_files WHERE file_id=?", (fid,)
        ).fetchone()
        assert scoped["node_id"] == "p1" and scoped["kind"] == "output"
    destroyed = owner.post("/api/receipt/destroy", json={"token": token})
    assert destroyed.status_code == 200, destroyed.text
    assert receipt(owner, token)["state"] == "destroyed"
    assert download(owner, token, fid).status_code in (404, 410)
    with db() as c:
        assert (
            c.execute(
                "SELECT count(*) FROM job_files WHERE job_id=?", (row["id"],)
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT count(*) FROM task_flow_files WHERE job_id=?", (row["id"],)
            ).fetchone()[0]
            == 0
        )
        frozen = task_flow.card_snapshot(c, job(c, row["id"])["card_id"])
        assert all(
            task_flow._payload(c, frozen, step) == {}
            for step in c.execute(
                "SELECT * FROM task_flow_steps WHERE job_id=?", (row["id"],)
            )
        )


def test_once_file_reveal_and_competing_downloads_cannot_recover_stage_data(owner):
    token, row, _, fid = prepare_file_flow(owner, view_policy="once")
    assert owner.post("/api/receipt/reveal", json={"token": token}).status_code == 200
    assert owner.post("/api/receipt/reveal", json={"token": token}).status_code == 410
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda _: download(owner, token, fid), range(3)))
    assert [response.status_code for response in responses].count(200) == 1
    assert [response.status_code for response in responses].count(410) == 2
    with db() as c:
        frozen = task_flow.card_snapshot(c, job(c, row["id"])["card_id"])
        assert all(
            task_flow._payload(c, frozen, step) == {}
            for step in c.execute(
                "SELECT * FROM task_flow_steps WHERE job_id=?", (row["id"],)
            )
        )


def test_finalizer_duplicate_is_idempotent_and_stale_after_destroy_cannot_restore(
    owner,
):
    _, token = launch(owner, rounds=1)
    row = answer(owner, token, action(owner, token, "start").json())
    assert batch(owner, row, "claim").status_code == 200
    with db() as c:
        raw = job(c, row["id"])
        effect = task_flow.process_update(
            c,
            raw,
            JobUpdate(state="succeeded", attempt=1, output={"answer": "Private final"}),
            row["task_flow"]["flow_epoch"],
            actor=raw["claimed_by"],
        )
        flow_adapter.finalize(c, effect)
        first = dict(job(c, row["id"]))
        flow_adapter.finalize(c, effect)
        assert dict(job(c, row["id"])) == first
        assert (
            c.execute(
                "SELECT count(*) FROM events WHERE type='fulfillment.succeeded'"
            ).fetchone()[0]
            == 1
        )
    assert owner.post("/api/receipt/destroy", json={"token": token}).status_code == 200
    with db() as c:
        before = dict(job(c, row["id"]))
        with pytest.raises(HTTPException) as stale:
            flow_adapter.finalize(c, effect)
        assert stale.value.status_code == 409
        assert dict(job(c, row["id"])) == before
        assert before["result_json"] is None and before["content"] is None


def test_issued_flow_and_once_delivery_policy_survive_merchant_schema_edits(owner):
    product, token = launch(owner, rounds=1, view_policy="once")
    row = answer(owner, token, action(owner, token, "start").json())
    edited = owner.put(
        f"/api/admin/products/{product['id']}",
        json={
            **product,
            "task_flow": None,
            "outputs": [field("changed")],
            "view_policy": "repeat",
        },
    )
    assert edited.status_code == 409, edited.text
    edited = owner.put(
        f"/api/admin/products/{product['id']}",
        json={**product, "task_flow": None, "outputs": [field("changed")]},
    )
    assert edited.status_code == 200, edited.text
    done = finish(owner, token, row, {"answer": "Issuance snapshot result"})
    assert done["state"] == "succeeded" and done["view_policy"] == "once"
    first = owner.post("/api/receipt/reveal", json={"token": token})
    assert first.status_code == 200, first.text
    assert first.json()["output"] == {"answer_1": "Issuance snapshot result"}
    assert owner.post("/api/receipt/reveal", json={"token": token}).status_code == 410
    issued = owner.post(
        "/api/admin/cards", json={"product_id": product["id"], "count": 1}
    )
    assert issued.status_code == 200, issued.text
    exchanged = owner.post("/api/exchange", json={"code": issued.json()["codes"][0]})
    assert exchanged.status_code == 200, exchanged.text
    assert "task_flow_view" not in exchanged.json()["product"]


def test_rejected_flow_remains_terminal_and_cannot_restart_with_known_receipt(owner):
    _, token = launch(owner, rounds=1)
    row = answer(owner, token, action(owner, token, "start").json())
    assert batch(owner, row, "claim").status_code == 200
    rejected = batch(
        owner, row, "reject", message="Request is outside this service scope"
    )
    assert rejected.status_code == 200, rejected.text
    ended = receipt(owner, token)
    assert ended["state"] == "rejected" and ended["can_retry"] is False
    before = state_snapshot()
    assert action(owner, token, "restart", ended).status_code in (409, 410)
    assert state_snapshot() == before
    assert owner.post("/api/receipt/reveal", json={"token": token}).status_code == 409


def test_foreign_flow_input_file_fails_before_advancing_epoch(owner):
    product, first_token = launch(owner, rounds=1, file_output=True)
    first = action(owner, first_token, "start").json()
    state = first["task_flow"]
    uploaded = owner.post(
        "/api/files/upload",
        data={
            "token": first_token,
            "field_key": "source",
            "flow_epoch": str(state["flow_epoch"]),
            "expected_revision": str(state["revision"]),
            "node_id": state["current"]["id"],
        },
        files={"file": ("source.txt", b"First card private source", "text/plain")},
    )
    assert uploaded.status_code == 200, uploaded.text
    issued = owner.post(
        "/api/admin/cards", json={"product_id": product["id"], "count": 1}
    )
    second_token = owner.post(
        "/api/exchange", json={"code": issued.json()["codes"][0]}
    ).json()["token"]
    second = action(owner, second_token, "start").json()
    before = state_snapshot()
    rejected = action(
        owner,
        second_token,
        "answer",
        second,
        values={
            "question": "Copy another card's file",
            "source": uploaded.json()["id"],
        },
    )
    assert rejected.status_code == 403, rejected.text
    assert state_snapshot() == before
    assert (
        receipt(owner, second_token)["task_flow"]["flow_epoch"]
        == second["task_flow"]["flow_epoch"]
    )

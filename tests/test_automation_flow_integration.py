"""Atomic-next safety against real frozen task-flow state, without core stubs."""

import time
from types import SimpleNamespace

import pytest
from test_automation_api import device, next_, product, queue, request

from extore import task_flow
from extore.db import db
from extore.service import finalize_task_flow, job


def flow_product(owner, *, sensitive=False):
    question = {
        "key": "requirements",
        "label": {"en": "Requirements"},
        "type": "text",
        "required": True,
        **({"sensitive": True, "sensitive_ttl_seconds": 1} if sensitive else {}),
    }
    output = {"key": "content", "label": {"en": "Result"}, "type": "text"}
    response = owner.post(
        "/api/admin/products",
        json={
            "name": "Isolated next flow",
            "mode": "manual",
            "parameters": [],
            "outputs": [output],
            "task_flow": {
                "version": 1,
                "entry": "input",
                "nodes": [
                    {
                        "id": "input",
                        "kind": "input",
                        "fields": [question],
                        "start_policy": "confirm",
                        "timeout_seconds": 60,
                        "timeout_next": "failed",
                        "next": "process",
                    },
                    {
                        "id": "process",
                        "kind": "process",
                        "inputs": {
                            "requirements": {"node": "input", "field": "requirements"}
                        },
                        "outputs": [output],
                        "timeout_seconds": 120,
                        "timeout_next": "failed",
                        "failure_next": "failed",
                        "next": "done",
                    },
                    {
                        "id": "done",
                        "kind": "end",
                        "state": "succeeded",
                        "result": {"content": {"node": "process", "field": "content"}},
                    },
                    {
                        "id": "failed",
                        "kind": "end",
                        "state": "failed",
                        "retryable": True,
                    },
                ],
            },
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
    current = started.json()["task_flow"]
    answered = owner.post(
        "/api/task-flow/answer",
        json={
            "token": token,
            "flow_epoch": current["flow_epoch"],
            "expected_revision": current["revision"],
            "values": {"requirements": "isolated-customer-input"},
        },
    )
    assert answered.status_code == 200, answered.text
    return pid, token, answered.json()


def test_real_flow_next_current_only_receipt_phase_guards_and_completion(
    owner, monkeypatch
):
    pid, token, original = flow_product(owner)
    bot = device(owner, pid)
    body = request(bot)
    claimed = next_(bot, body)
    assert claimed.status_code == 200, claimed.text
    item = claimed.json()["items"][0]
    assert item["job"]["id"] == original["id"]
    assert item["job"]["state"] == "processing"
    assert item["execution"]["params"] == {"requirements": "isolated-customer-input"}
    assert item["execution"]["parameters"][0]["key"] == "requirements"
    assert item["execution"]["outputs"][0]["key"] == "content"
    assert not {"webhook_url", "webhook_secret", "processor_id", "shop_id"} & set(
        item["execution"]
    )
    assert next_(bot, body).json()["items"] == [item]

    with db() as c:
        actor = job(c, original["id"])["claimed_by"]
        c.execute(
            "UPDATE task_flow_runs SET phase='queued' WHERE job_id=?", (original["id"],)
        )
    with monkeypatch.context() as guard:
        # The corrupt phase must be rejected before any input getter is called.
        def forbidden(*_):
            raise AssertionError("Input getter read during inconsistent receipt replay")

        guard.setattr(task_flow, "execution", forbidden)
        stale = next_(bot, body)
        assert stale.status_code == 200 and stale.json()["stale"]
        assert stale.json()["items"] == []
    with db() as c:
        c.execute(
            "UPDATE task_flow_runs SET phase='processing' WHERE job_id=?",
            (original["id"],),
        )
        c.execute(
            "UPDATE jobs SET state='queued',claimed_by=NULL WHERE id=?",
            (original["id"],),
        )
    assert next_(bot, request(bot)).json()["items"] == []
    with db() as c:
        c.execute(
            "UPDATE jobs SET state='processing',claimed_by=? WHERE id=?",
            (actor, original["id"]),
        )
    finished = bot[0].post(
        "/api/manage/batch",
        json={
            "product_id": pid,
            "ids": [original["id"]],
            "action": "succeed",
            "attempt": item["job"]["attempt"],
            "flow_epoch": item["execution"]["flow_epoch"],
            "action_id": item["execution"]["action_id"],
            "output": {"content": "synthetic final delivery"},
        },
        headers=bot[3],
    )
    assert finished.status_code == 200, finished.text
    receipt = owner.post("/api/receipt", json={"token": token}).json()["job"]
    assert receipt["state"] == "succeeded"
    assert next_(bot, body).json()["stale"]
    assert next_(bot, request(bot)).json()["items"] == []


@pytest.mark.parametrize("sensitive", [False, True])
def test_real_flow_expired_or_secret_expired_head_never_falls_back_to_legacy_claim(
    owner, monkeypatch, sensitive
):
    now = [time.time()]
    monkeypatch.setattr(task_flow, "time", SimpleNamespace(time=lambda: now[0]))
    pid, _, original = flow_product(owner, sensitive=sensitive)
    other = product(owner)
    legacy = queue(other, "safe other queue")
    bots = device(owner, pid), device(owner, other)
    now[0] += 2 if sensitive else 121
    response = next_(bots[0], request(*bots))
    assert response.status_code == 200, response.text
    assert [item["job"]["id"] for item in response.json()["items"]] == [legacy["id"]]
    assert "isolated-customer-input" not in response.text
    with db() as c:
        unclaimed = job(c, original["id"])
        assert unclaimed["claimed_by"] is None and unclaimed["state"] == "queued"
        assert task_flow.view(c, unclaimed)["phase"] == "queued"
        for effect in task_flow.expire_due(c):
            finalize_task_flow(c, effect)
        updated = job(c, original["id"])
        if sensitive:
            assert updated["state"] == "waiting"
            assert task_flow.view(c, updated)["phase"] == "await_start"
        else:
            assert updated["state"] == "failed"

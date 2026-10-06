import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from extore import task_flow as flow
from extore.db import db
from extore.models import JobUpdate
from extore.security import card_digest
from extore.service import issue_cards, job, product


def field(key, **extra):
    return {"key": key, "label": {"en": key}, "type": "text", "required": True, **extra}


def graph(rounds=3):
    nodes = []
    for index in range(1, rounds + 1):
        qid, pid = f"q{index}", f"p{index}"
        nodes.extend(
            [
                {
                    "id": qid,
                    "kind": "input",
                    "prompt": {"en": "Ready?"},
                    "question": {"en": f"Private question {index}"},
                    "fields": [field("answer")],
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
                    "inputs": {"answer": {"node": qid, "field": "answer"}},
                    "outputs": [field("answer")],
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
                "result": {
                    f"answer_{index}": {"node": f"p{index}", "field": "answer"}
                    for index in range(1, rounds + 1)
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


def setup(owner, definition=None, *, mode="manual", freeze=True):
    definition = graph() if definition is None else definition
    outputs = [field(key) for key in definition["nodes"][-2]["result"]]
    response = owner.post(
        "/api/admin/products",
        json={
            "name": "Flow",
            "parameters": [],
            "outputs": outputs,
            "mode": mode,
            **(
                {
                    "webhook_url": "https://worker.example.test/receive",
                    "webhook_secret": "s" * 40,
                }
                if mode == "webhook"
                else {}
            ),
        },
    )
    assert response.status_code == 200, response.text
    pid = response.json()["id"]
    with db() as c:
        flow.init_schema(c)
        p = {**product(c, pid), "task_flow": definition}
        code = issue_cards(c, pid, 1)[0]
        card = c.execute(
            "SELECT * FROM cards WHERE digest=?", (card_digest(code),)
        ).fetchone()
        if freeze:
            flow.freeze_card(c, card["id"], p)
        c.execute(
            "INSERT INTO jobs(id,card_id,product_id,state,params,created,updated,schema_snapshot,progress_plan) "
            "VALUES ('task',?,?,'queued','{}',1,1,?,'[]')",
            (card["id"], pid, json.dumps({"parameters": [], "outputs": outputs})),
        )
        c.execute("UPDATE cards SET state='reserved' WHERE id=?", (card["id"],))
        row = job(c, "task")
        effect = flow.initialize(c, row)
    return pid, card["id"], effect


def answer_current(c, value="input"):
    row = job(c, "task")
    view = flow.view(c, row)
    if view["phase"] == "await_start":
        view = flow.start(c, row, view["flow_epoch"], view["revision"])["flow"]
    return flow.answer(c, row, {"answer": value}, view["flow_epoch"], view["revision"])


def finish_current(c, value="result"):
    row = job(c, "task")
    execution = flow.execution(c, row)
    effect = flow.claim(c, row, "bot", execution["flow_epoch"])
    return flow.process_update(
        c,
        effect["row"],
        JobUpdate(attempt=row["attempt"], state="succeeded", output={"answer": value}),
        execution["flow_epoch"],
        actor="bot",
    )


def snapshot(c):
    return {
        name: [dict(row) for row in c.execute("SELECT * FROM " + name)]
        for name in (
            "cards",
            "jobs",
            "card_task_flows",
            "task_flow_runs",
            "task_flow_steps",
        )
    }


def test_three_rounds_auto_next_stage_and_terminal_only_at_end(owner, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(flow, "time", SimpleNamespace(time=lambda: now[0]))
    _, cid, effect = setup(owner)
    assert effect["row"]["state"] == "waiting"
    assert effect["flow"]["phase"] == "await_start"
    assert effect["flow"]["deadline"] is None
    assert "Private question" not in json.dumps(effect["flow"])
    with db() as c:
        for index in range(1, 4):
            entered = answer_current(c, f"customer {index}")
            assert entered["accepted"]["kind"] == "input"
            execution = flow.execution(c, entered["row"])
            assert execution["params"] == {"answer": f"customer {index}"}
            assert execution["node_id"] == f"p{index}"
            before_epoch = execution["flow_epoch"]
            now[0] += 3
            result = finish_current(c, f"answer {index}")
            assert (
                c.execute("SELECT state FROM cards WHERE id=?", (cid,)).fetchone()[0]
                == "reserved"
            )
            assert job(c, "task")["result_json"] is None
            assert job(c, "task")["attempt"] == 1
            assert result["flow"]["flow_epoch"] == before_epoch + 1
            if index < 3:
                assert "terminal" not in result
                assert result["flow"]["phase"] == "input"
                assert result["flow"]["deadline"] == now[0] + 30
                assert result["flow"]["shown"][0]["value"] == f"answer {index}"
            else:
                assert result["terminal"]["state"] == "succeeded"
                assert result["terminal"]["output"] == {
                    f"answer_{i}": f"answer {i}" for i in range(1, 4)
                }
                assert result["terminal"]["result_sources"]["answer_1"] == {
                    "node": "p1",
                    "field": "answer",
                    "flow_epoch": 2,
                    "kind": "output",
                }
                assert result["flow"]["phase"] == "ended"
                assert "answer 3" not in json.dumps(result["flow"])


def test_start_is_idempotent_refresh_does_not_extend_deadline(owner, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(flow, "time", SimpleNamespace(time=lambda: now[0]))
    setup(owner)
    with db() as c:
        row = job(c, "task")
        initial = flow.view(c, row)
        started = flow.start(c, row, initial["flow_epoch"], initial["revision"])
        now[0] += 10
        again = flow.start(c, row, initial["flow_epoch"], initial["revision"])
        assert again["flow"]["deadline"] == started["flow"]["deadline"] == 1030
        assert again["flow"]["revision"] == started["flow"]["revision"]
        assert flow.view(c, row)["deadline"] == 1030


def test_old_epoch_wrong_actor_and_revision_rejected_without_mutation(owner):
    setup(owner)
    with db() as c:
        answer_current(c)
        row = job(c, "task")
        current = flow.view(c, row)
        before = snapshot(c)
        with pytest.raises(HTTPException):
            flow.start(c, row, current["flow_epoch"] - 1, current["revision"])
        assert snapshot(c) == before
        flow.claim(c, row, "bot", current["flow_epoch"])
        before = snapshot(c)
        with pytest.raises(HTTPException):
            flow.process_update(
                c,
                row,
                JobUpdate(state="succeeded", attempt=1, output={"answer": "stolen"}),
                current["flow_epoch"],
                actor="other",
            )
        assert snapshot(c) == before
        with pytest.raises(HTTPException):
            flow.process_update(
                c,
                row,
                JobUpdate(state="succeeded", attempt=2, output={"answer": "old"}),
                current["flow_epoch"],
                actor="bot",
            )
        assert snapshot(c) == before


def test_duplicate_answer_is_safe_changed_answer_conflicts(owner):
    setup(owner)
    with db() as c:
        row = job(c, "task")
        started = flow.start(c, row, 1, 1)["flow"]
        first = flow.answer(c, row, {"answer": "same"}, 1, started["revision"])
        before = snapshot(c)
        second = flow.answer(c, row, {"answer": "same"}, 1, started["revision"])
        assert second["duplicate"] is True
        assert second["flow"]["flow_epoch"] == first["flow"]["flow_epoch"]
        assert snapshot(c) == before
        with pytest.raises(HTTPException):
            flow.answer(c, row, {"answer": "different"}, 1, started["revision"])
        assert snapshot(c) == before


def test_deadline_race_returns_committable_expired_effect_not_answer(
    owner, monkeypatch
):
    now = [1000.0]
    monkeypatch.setattr(flow, "time", SimpleNamespace(time=lambda: now[0]))
    setup(owner)
    with db() as c:
        row = job(c, "task")
        started = flow.start(c, row, 1, 1)["flow"]
        now[0] = started["deadline"]
        expired = flow.answer(c, row, {"answer": "late"}, 1, started["revision"])
        assert expired["expired"] is True
        assert expired["terminal"]["state"] == "failed"
        assert expired["flow"]["phase"] == "ended"
        assert "accepted" not in expired
        assert flow.expire_due(c, now[0]) == []


def test_processing_timeout_is_uncertain_and_does_not_retry_external_work(
    owner, monkeypatch
):
    now = [1000.0]
    monkeypatch.setattr(flow, "time", SimpleNamespace(time=lambda: now[0]))
    _, _, _ = setup(owner)
    with db() as c:
        answer_current(c)
        row = job(c, "task")
        execution = flow.execution(c, row)
        claimed = flow.claim(c, row, "bot", execution["flow_epoch"])
        now[0] = execution["deadline"]
        assert flow.execution(c, row) is None
        expired = flow.process_update(
            c,
            claimed["row"],
            JobUpdate(state="succeeded", attempt=1, output={"answer": "late"}),
            execution["flow_epoch"],
            actor="bot",
        )
        assert expired["terminal"]["retryable"] is False
        assert expired["terminal"]["needs_review"] is True
        assert expired["flow"]["phase"] == "ended"
        assert job(c, "task")["result_json"] is None


def test_snapshot_is_immutable_and_missing_binding_is_legacy(owner):
    _, cid, _ = setup(owner, freeze=False)
    with db() as c:
        row = job(c, "task")
        assert flow.card_snapshot(c, cid) is None
        assert flow.is_flow(c, row) is False
        assert flow.execution(c, row) is None
        assert flow.view(c, row) is None


def test_encrypted_secrets_visible_only_to_current_execution_and_cleared_after_use(
    owner, monkeypatch
):
    now = [1000.0]
    monkeypatch.setattr(flow, "time", SimpleNamespace(time=lambda: now[0]))
    definition = graph(1)
    definition["nodes"][0]["fields"][0].update(
        sensitive=True, sensitive_ttl_seconds=120
    )
    _, cid, _ = setup(owner, definition)
    marker = "synthetic-otp-not-in-metadata"
    with db() as c:
        entered = answer_current(c, marker)
        public = flow.view(c, entered["row"], staff=True)
        assert marker not in json.dumps(public)
        for table in ("jobs", "card_task_flows", "task_flow_runs", "task_flow_steps"):
            assert marker not in json.dumps(
                [dict(row) for row in c.execute("SELECT * FROM " + table)]
            )
        current = flow.execution(c, entered["row"])
        assert current["params"]["answer"] == marker
        finish_current(c, "public answer")
        source = flow.stage_values(c, job(c, "task"), 1, "input")
        assert "answer" not in source["values"]
        assert flow.card_snapshot(c, cid)["product"]["outputs"]


def test_expired_secret_is_not_given_to_worker(owner, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(flow, "time", SimpleNamespace(time=lambda: now[0]))
    definition = graph(1)
    definition["nodes"][0]["fields"][0].update(sensitive=True, sensitive_ttl_seconds=1)
    setup(owner, definition)
    with db() as c:
        entered = answer_current(c, "synthetic-otp")
        now[0] += 1
        assert flow.execution(c, entered["row"]) is None
        with pytest.raises(HTTPException):
            flow.claim(c, entered["row"], "bot", entered["flow"]["flow_epoch"])


def test_reset_attempt_keeps_epoch_monotonic_and_invalidates_old_process(owner):
    setup(owner)
    with db() as c:
        entered = answer_current(c)
        old_epoch = entered["flow"]["flow_epoch"]
        c.execute(
            "UPDATE jobs SET attempt=2,state='queued',claimed_by=NULL WHERE id='task'"
        )
        reset = flow.reset_attempt(c, job(c, "task"))
        assert reset["flow"]["flow_epoch"] > old_epoch
        assert reset["flow"]["attempt"] == 2
        assert reset["flow"]["phase"] == "await_start"
        with pytest.raises(HTTPException):
            flow.process_update(
                c,
                job(c, "task"),
                JobUpdate(state="succeeded", attempt=1, output={"answer": "late"}),
                old_epoch,
                actor="bot",
            )


def test_release_claim_preserves_epoch_and_absolute_deadline(owner):
    setup(owner)
    with db() as c:
        entered = answer_current(c)
        before = flow.execution(c, entered["row"])
        claim = flow.claim(c, entered["row"], "bot", before["flow_epoch"])
        released = flow.release_claim(c, claim["row"], "bot", before["flow_epoch"])
        after = flow.execution(c, released["row"])
        assert after["flow_epoch"] == before["flow_epoch"]
        assert after["deadline"] == before["deadline"]
        assert released["row"]["state"] == "queued"


def test_cancel_and_reject_outcome_wipe_slots_and_invalidate_epoch(owner):
    setup(owner)
    with db() as c:
        entered = answer_current(c, "private cancellation payload")
        row = entered["row"]
        cancelled = flow.cancel(
            c, row, entered["flow"]["flow_epoch"], entered["flow"]["revision"]
        )
        assert cancelled["flow"]["flow_epoch"] > entered["flow"]["flow_epoch"]
        assert cancelled["terminal"]["retryable"] is True
        assert flow.stage_values(c, row, 1, "input")["values"] == {}


def test_frozen_authority_recovers_completed_step_without_old_data_and_respects_revocation(
    owner,
):
    pid, _, _ = setup(owner, graph(1), mode="webhook")
    with db() as c:
        entered = answer_current(c)
        execution = flow.execution(c, entered["row"])
        flow.process_update(
            c,
            entered["row"],
            JobUpdate(state="succeeded", attempt=1, output={"answer": "result"}),
            execution["flow_epoch"],
        )
        authority = flow.frozen_authority(
            c, job(c, "task"), execution["flow_epoch"], attempt=1
        )
        assert authority["action_id"] == execution["action_id"]
        assert authority["step_state"] == "completed"
        assert "params" not in authority and "output" not in authority
        p = json.loads(
            c.execute("SELECT config FROM products WHERE id=?", (pid,)).fetchone()[0]
        )
        p["webhook_secret"] = "rotated" * 8
        c.execute("UPDATE products SET config=? WHERE id=?", (json.dumps(p), pid))
        with pytest.raises(HTTPException) as rejected:
            flow.frozen_authority(c, job(c, "task"), execution["flow_epoch"], attempt=1)
        assert rejected.value.status_code == 403


def test_graph_backjump_budget_is_bounded(owner):
    definition = graph(1)
    definition["nodes"][1]["next"] = "q1"
    setup(owner, definition)
    with db() as c:
        answer_current(c)
        c.execute("UPDATE task_flow_runs SET transition_count=256 WHERE job_id='task'")
        ended = finish_current(c)
        assert ended["terminal"]["state"] == "failed"
        assert ended["terminal"]["needs_review"] is True
        assert c.execute("SELECT count(*) FROM task_flow_steps").fetchone()[0] == 2


def test_oversized_multibyte_step_data_rejected_before_context_is_written(owner):
    definition = graph(1)
    setup(owner, definition)
    with db() as c:
        entered = answer_current(c)
        row = entered["row"]
        flow.claim(c, row, "bot", entered["flow"]["flow_epoch"])
        before = snapshot(c)
        with pytest.raises(HTTPException) as oversized:
            flow.process_update(
                c,
                row,
                JobUpdate(
                    state="succeeded", attempt=1, output={"answer": "汉" * 70000}
                ),
                entered["flow"]["flow_epoch"],
                actor="bot",
            )
        assert oversized.value.status_code == 413
        assert snapshot(c) == before


def test_process_timeout_does_not_evaluate_unfinished_success_target(
    owner, monkeypatch
):
    now = [1000.0]
    monkeypatch.setattr(flow, "time", SimpleNamespace(time=lambda: now[0]))
    definition = graph(1)
    definition["nodes"][1]["timeout_next"] = "done"
    setup(owner, definition)
    with db() as c:
        entered = answer_current(c)
        before = flow.execution(c, entered["row"])
        now[0] = before["deadline"]
        effects = flow.expire_due(c)
        assert len(effects) == 1
        effect = effects[0]
        assert effect["terminal"]["state"] == "failed"
        assert effect["terminal"]["needs_review"] is True
        assert effect["configured_timeout_target"] == "done"
        assert effect["flow"]["flow_epoch"] > before["flow_epoch"]
        assert effect["flow"]["phase"] == "ended"
        assert (
            c.execute(
                "SELECT count(*) FROM task_flow_steps WHERE node_id='done'"
            ).fetchone()[0]
            == 0
        )
        assert flow.expire_due(c) == []


def test_input_timeout_can_show_next_input_without_unanswered_source(
    owner, monkeypatch
):
    now = [1000.0]
    monkeypatch.setattr(flow, "time", SimpleNamespace(time=lambda: now[0]))
    definition = graph(2)
    definition["nodes"][0]["timeout_next"] = "q2"
    definition["nodes"][2]["show_from"] = {
        "previous": {"node": "q1", "field": "answer"}
    }
    setup(owner, definition)
    with db() as c:
        row = job(c, "task")
        flow.start(c, row, 1, 1)
        now[0] += 30
        effect = flow.expire_due(c)[0]
        assert effect["flow"]["current"]["id"] == "q2"
        assert effect["flow"]["phase"] == "input"
        assert effect["flow"]["shown"] == []
        assert flow.view(c, row)["shown"] == []


def test_exists_branch_without_completed_source_uses_default(owner):
    definition = graph(1)
    definition["nodes"][0]["next"] = {
        "cases": [
            {
                "when": {"source": {"node": "p1", "field": "answer"}, "op": "exists"},
                "to": "done",
            }
        ],
        "default": "p1",
    }
    setup(owner, definition)
    with db() as c:
        assert answer_current(c)["flow"]["current"]["id"] == "p1"


def test_sensitive_ttl_sweep_returns_queued_task_for_reentry_without_attempt_change(
    owner, monkeypatch
):
    now = [1000.0]
    monkeypatch.setattr(flow, "time", SimpleNamespace(time=lambda: now[0]))
    definition = graph(1)
    definition["nodes"][0]["fields"][0].update(sensitive=True, sensitive_ttl_seconds=1)
    setup(owner, definition)
    with db() as c:
        entered = answer_current(c, "001234")
        execution = flow.execution(c, entered["row"])
        assert execution["input_expires_at"] == 1001
        now[0] += 1
        effect = flow.expire_due(c)[0]
        assert effect["input_expired"] is True
        assert effect["flow"]["current"]["id"] == "q1"
        assert effect["flow"]["phase"] == "await_start"
        assert effect["flow"]["deadline"] is None
        assert effect["flow"]["flow_epoch"] > execution["flow_epoch"]
        assert effect["row"]["attempt"] == 1
        assert effect["row"]["state"] == "waiting"
        assert "terminal" not in effect
        assert flow.stage_values(c, effect["row"], 1, "input")["values"] == {}
        assert flow.expire_due(c) == []
        with pytest.raises(HTTPException):
            flow.claim(c, entered["row"], "old bot", execution["flow_epoch"])
        fresh = answer_current(c, "004321")
        assert flow.execution(c, fresh["row"])["params"] == {"answer": "004321"}


def test_sensitive_ttl_cleanup_does_not_repeat_started_process(owner, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(flow, "time", SimpleNamespace(time=lambda: now[0]))
    definition = graph(1)
    definition["nodes"][0]["fields"][0].update(sensitive=True, sensitive_ttl_seconds=1)
    setup(owner, definition)
    with db() as c:
        entered = answer_current(c, "001234")
        execution = flow.execution(c, entered["row"])
        flow.claim(c, entered["row"], "bot", execution["flow_epoch"])
        assert flow.execution(c, entered["row"])["params"]["answer"] == "001234"
        now[0] += 1
        effect = flow.expire_due(c)[0]
        assert effect["flow"]["phase"] == "processing"
        assert effect["flow"]["flow_epoch"] == execution["flow_epoch"]
        assert effect["flow"]["deadline"] == execution["deadline"]
        assert flow.execution(c, effect["row"]) is None
        assert flow.stage_values(c, effect["row"], 1, "input")["values"] == {}
        result = flow.process_update(
            c,
            effect["row"],
            JobUpdate(state="succeeded", attempt=1, output={"answer": "finished"}),
            execution["flow_epoch"],
            actor="bot",
        )
        assert result["terminal"]["output"] == {"answer_1": "finished"}


def test_optional_empty_sensitive_input_does_not_require_a_secret_ttl(owner):
    definition = graph(1)
    definition["nodes"][0]["fields"][0].update(sensitive=True, required=False)
    setup(owner, definition)
    with db() as c:
        entered = answer_current(c, "")
        execution = flow.execution(c, entered["row"])
        assert execution["params"] == {"answer": ""}
        assert execution["input_expires_at"] is None
        assert finish_current(c)["terminal"]["state"] == "succeeded"


def test_run_total_quota_includes_dispatch_and_allows_erasure_over_quota(
    owner, monkeypatch
):
    from extore import flow_worker

    setup(owner, graph(1))
    with db() as c:
        flow_worker.init_schema(c)
        row = job(c, "task")
        p = product(c, row["product_id"])
        c.execute(
            "INSERT INTO task_flow_dispatches(id,job_id,product_id,shop_id,attempt,flow_epoch,node_id,action_id,state,payload_ciphertext,ciphertext_bytes,payload_digest,due,created,updated) "
            "VALUES ('dispatch','task',?,?,1,1,'q1','action','pending',?,5000,'digest',1,1,1)",
            (
                p["id"],
                c.execute(
                    "SELECT shop_id FROM products WHERE id=?", (p["id"],)
                ).fetchone()[0],
                "x" * 5000,
            ),
        )
        monkeypatch.setattr(flow, "MAX_RUN_CIPHERTEXT_BYTES", 5000)
        current = flow.start(c, row, 1, 1)["flow"]
        before = snapshot(c)
        with pytest.raises(HTTPException) as quota:
            flow.answer(
                c,
                row,
                {"answer": "would exceed encrypted capacity"},
                1,
                current["revision"],
            )
        assert quota.value.status_code == 413
        assert snapshot(c) == before
        assert flow.destroy(c, row)["flow"]["phase"] == "ended"


def test_frozen_issuance_quota_is_checked_and_changed_product_does_not_change_question(
    owner, monkeypatch
):
    from extore import storage

    calls = []
    monkeypatch.setattr(
        storage,
        "check_storage_quota",
        lambda c, size, *, product_id: calls.append((size, product_id)),
    )
    pid, cid, _ = setup(owner, graph(1))
    assert calls and all(size > 0 and product_id == pid for size, product_id in calls)
    with db() as c:
        config = json.loads(
            c.execute("SELECT config FROM products WHERE id=?", (pid,)).fetchone()[0]
        )
        config["task_flow"] = graph(3)
        c.execute("UPDATE products SET config=? WHERE id=?", (json.dumps(config), pid))
        assert len(flow.card_snapshot(c, cid)["definition"]["nodes"]) == 4
        assert flow.start(c, job(c, "task"), 1, 1)["flow"]["current"]["question"] == {
            "en": "Private question 1"
        }


def test_reject_then_retry_invalidates_old_actor_and_erases_old_values(owner):
    setup(owner, graph(1))
    with db() as c:
        entered = answer_current(c, "secret business instructions")
        flow.claim(c, entered["row"], "bot", entered["flow"]["flow_epoch"])
        c.execute(
            "UPDATE jobs SET state='needs_input',claimed_by=NULL,lease=NULL WHERE id='task'"
        )
        stopped = flow.stop_for_outcome(c, job(c, "task"))
        assert stopped["flow"]["phase"] == "ended"
        assert stopped["flow"]["flow_epoch"] > entered["flow"]["flow_epoch"]
        assert flow.stage_values(c, stopped["row"], 1, "input")["values"] == {}
        c.execute("UPDATE jobs SET state='queued',attempt=2 WHERE id='task'")
        retry = flow.reset_attempt(c, job(c, "task"))
        assert retry["flow"]["attempt"] == 2
        assert retry["flow"]["phase"] == "await_start"
        with pytest.raises(HTTPException):
            flow.process_update(
                c,
                retry["row"],
                JobUpdate(state="succeeded", attempt=1, output={"answer": "stale"}),
                entered["flow"]["flow_epoch"],
                actor="bot",
            )


def test_secret_equality_receipt_is_keyed_and_resource_bound(owner):
    setup(owner, graph(1))
    with db() as c:
        row = job(c, "task")
        shop_id = c.execute(
            "SELECT shop_id FROM products WHERE id=?", (row["product_id"],)
        ).fetchone()[0]
        value = {"answer": "001234"}
        assert flow.fingerprint(value, shop_id, "task:1") != flow._digest(value)
        assert flow.fingerprint(value, shop_id, "task:1") != flow.fingerprint(
            value, shop_id, "task:2"
        )
        assert flow.fingerprint(value, shop_id, "task:1") == flow.fingerprint(
            value, shop_id, "task:1"
        )

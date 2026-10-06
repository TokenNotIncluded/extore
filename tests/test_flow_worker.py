"""Real SQLite flow stages; sandbox and transport boundaries are explicit fakes."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from extore import flow_worker as worker
from extore import task_flow
from extore.config import DATA
from extore.db import db
from extore.models import JobUpdate
from extore.secret_store import open_secret
from extore.security import card_digest
from extore.service import issue_cards, job, product


def field(key, kind="text", **extra):
    return {"key": key, "label": {"en": key}, "type": kind, **extra}


def definition(rounds=2, *, sensitive=False):
    nodes = []
    for index in range(1, rounds + 1):
        nodes.extend(
            [
                {
                    "id": f"q{index}",
                    "kind": "input",
                    "fields": [field("name", sensitive=sensitive)],
                    "next": f"p{index}",
                    "start_policy": "confirm" if index == 1 else "automatic",
                },
                {
                    "id": f"p{index}",
                    "kind": "process",
                    "inputs": {"name": {"node": f"q{index}", "field": "name"}},
                    "outputs": [field("content", "textarea")],
                    "next": f"q{index + 1}" if index < rounds else "done",
                    "timeout_seconds": 3600,
                    "timeout_next": "failed",
                    "failure_next": "failed",
                },
            ]
        )
    nodes.extend(
        [
            {
                "id": "done",
                "kind": "end",
                "state": "succeeded",
                "result": {"content": {"node": f"p{rounds}", "field": "content"}},
            },
            {"id": "failed", "kind": "end", "state": "failed", "retryable": True},
        ]
    )
    return {"version": 1, "entry": "q1", "nodes": nodes}


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    source = SimpleNamespace(time=lambda: now[0])
    monkeypatch.setattr(worker, "time", source)
    monkeypatch.setattr(task_flow, "time", source)
    return now


@pytest.fixture
def finalizer(monkeypatch):
    seen = []

    def finalize(c, effect):
        seen.append(effect)
        terminal = effect.get("terminal")
        if terminal is not None:
            c.execute(
                "UPDATE jobs SET state=?,result_json=? WHERE id=?",
                (
                    terminal["state"],
                    json.dumps(terminal.get("output", {})),
                    effect["row"]["id"],
                ),
            )
            if terminal["state"] == "succeeded":
                c.execute(
                    "UPDATE cards SET state='used' WHERE id=?",
                    (effect["row"]["card_id"],),
                )
        worker.sync_dispatch(c, effect["row"])
        return effect

    from extore import processor_profiles

    # This fixture replaces the sandbox boundary; native tests use real profiles.
    monkeypatch.setattr(
        processor_profiles, "runtime_execution", lambda *args: ({}, {}, {})
    )
    monkeypatch.setattr(worker, "_finalize", finalize)
    return seen


def setup(owner, *, mode="webhook", rounds=2, sensitive=False, value="customer-input"):
    response = owner.post(
        "/api/admin/products",
        json={
            "name": "Flow worker",
            "parameters": [],
            "outputs": [field("content", "textarea")],
            "mode": "webhook",
            "webhook_url": "https://worker.example.test/receive",
            "webhook_secret": "s" * 40,
        },
    )
    assert response.status_code == 200, response.text
    pid = response.json()["id"]
    with db() as c:
        task_flow.init_schema(c)
        worker.init_schema(c)
        code = issue_cards(c, pid, 1)[0]
        card = c.execute(
            "SELECT * FROM cards WHERE digest=?", (card_digest(code),)
        ).fetchone()
        configured = product(c, pid)
        if mode == "script":
            configured.update(
                mode="script",
                processor_id="personalized_text",
                parameters=[field("name")],
            )
            c.execute(
                "UPDATE products SET config=? WHERE id=?", (json.dumps(configured), pid)
            )
        task_flow.freeze_card(
            c,
            card["id"],
            {**configured, "task_flow": definition(rounds, sensitive=sensitive)},
        )
        c.execute(
            "INSERT INTO jobs(id,card_id,product_id,state,params,created,updated,schema_snapshot,progress_plan) "
            "VALUES ('task',?,?,'queued','{}',1,1,?,'[]')",
            (
                card["id"],
                pid,
                json.dumps({"parameters": [], "outputs": configured["outputs"]}),
            ),
        )
        c.execute("UPDATE cards SET state='reserved' WHERE id=?", (card["id"],))
        effect = task_flow.initialize(c, job(c, "task"))
        view = effect["flow"]
        effect = task_flow.start(c, effect["row"], view["flow_epoch"], view["revision"])
        view = effect["flow"]
        effect = task_flow.answer(
            c, effect["row"], {"name": value}, view["flow_epoch"], view["revision"]
        )
        execution = task_flow.execution(c, effect["row"])
        if mode == "webhook":
            dispatch = worker.sync_dispatch(c, effect["row"])
        else:
            dispatch = None
        shop_id = c.execute(
            "SELECT shop_id FROM products WHERE id=?", (pid,)
        ).fetchone()[0]
    return SimpleNamespace(
        pid=pid,
        card_id=card["id"],
        shop_id=shop_id,
        execution=execution,
        dispatch=dispatch,
    )


def dispatch_row():
    with db() as c:
        row = c.execute(
            "SELECT * FROM task_flow_dispatches ORDER BY created,id LIMIT 1"
        ).fetchone()
        return dict(row) if row is not None else None


def assert_write_boundary_free():
    c = sqlite3.connect(DATA / "extore.sqlite3", timeout=0, isolation_level=None)
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE settings SET value=value WHERE key='nonexistent'")
        c.rollback()
    finally:
        c.close()


def test_sync_is_idempotent_and_private_context_never_enters_normal_tables(
    owner, clock
):
    expected = "PRIVATE-OTP-992831"
    configured = setup(owner, sensitive=True, value=expected)
    before = dispatch_row()
    with db() as c:
        again = worker.sync_dispatch(c, job(c, "task"))
        assert again == configured.dispatch
        stored = c.execute("SELECT * FROM task_flow_dispatches").fetchall()
        assert len(stored) == 1
        context = open_secret(
            before["payload_ciphertext"],
            tenant_id=configured.shop_id,
            resource_type="task-flow-dispatch",
            resource_id=before["id"],
        )
        assert context["params"] == {"name": expected}
        assert context["action_id"] == configured.execution["action_id"]
        assert expected not in json.dumps([dict(row) for row in stored])
        for table in ("events", "outbox", "jobs", "audit"):
            assert expected not in json.dumps(
                [dict(row) for row in c.execute("SELECT * FROM " + table)]
            )
    assert "payload_ciphertext" not in configured.dispatch
    assert "webhook_secret" not in configured.dispatch
    assert dispatch_row()["payload_ciphertext"] == before["payload_ciphertext"]
    assert before["input_expires_at"] == clock[0] + 120


def test_success_sends_outside_transaction_and_erases_envelope_without_resending(
    owner, clock, monkeypatch
):
    setup(owner, sensitive=True, value="private-otp")
    calls = []

    async def deliver(context, *, recheck):
        assert_write_boundary_free()
        recheck()
        assert_write_boundary_free()
        calls.append(context)

    monkeypatch.setattr(worker, "_deliver", deliver)
    assert asyncio.run(worker.outbox_once())
    row = dispatch_row()
    assert row["state"] == "sent" and row["attempts"] == 1
    assert row["payload_ciphertext"] == "" and row["ciphertext_bytes"] == 0
    with db() as c:
        assert worker.sync_dispatch(c, job(c, "task"))["state"] == "sent"
    assert not asyncio.run(worker.outbox_once())
    assert len(calls) == 1


def test_transport_retries_keep_action_and_context_stable_but_never_store_error_contents(
    owner, clock, monkeypatch
):
    setup(owner)
    contexts = []

    async def deliver(context, *, recheck):
        recheck()
        contexts.append(context)
        if len(contexts) == 1:
            raise RuntimeError("https://worker.example.test/?private-token=LEAK")

    monkeypatch.setattr(worker, "_deliver", deliver)
    assert asyncio.run(worker.outbox_once())
    first = dispatch_row()
    assert first["state"] == "pending" and first["error"] == "delivery_failed"
    assert first["due"] == clock[0] + 10
    clock[0] = first["due"]
    assert asyncio.run(worker.outbox_once())
    assert contexts[0] == contexts[1]
    assert dispatch_row()["state"] == "sent"
    assert "LEAK" not in json.dumps(dispatch_row())


def test_eight_delivery_failures_are_bounded_and_dead_payload_is_erased(
    owner, clock, monkeypatch
):
    setup(owner)

    async def deliver(context, *, recheck):
        recheck()
        raise ValueError("secret response body")

    monkeypatch.setattr(worker, "_deliver", deliver)
    for attempt in range(1, 9):
        assert asyncio.run(worker.outbox_once())
        row = dispatch_row()
        assert row["attempts"] == attempt
        if attempt < 8:
            clock[0] = row["due"]
    assert row["state"] == "dead" and row["payload_ciphertext"] == ""
    assert row["ciphertext_bytes"] == 0 and row["error"] == "delivery_failed"
    assert not asyncio.run(worker.outbox_once())


@pytest.mark.parametrize(
    "change",
    [
        "shop",
        "secret",
        "url",
        "card",
        "attempt",
        "epoch",
        "corrupt_ciphertext",
        "digest",
    ],
)
def test_preflight_cancels_invalid_authority_without_network(
    owner, clock, monkeypatch, change
):
    configured = setup(owner)
    with db() as c:
        if change == "shop":
            c.execute("UPDATE shops SET enabled=0 WHERE id=?", (configured.shop_id,))
        elif change in ("secret", "url"):
            p = product(c, configured.pid)
            p["webhook_secret" if change == "secret" else "webhook_url"] = (
                "x" * 40
                if change == "secret"
                else "https://changed.example.test/receive"
            )
            c.execute(
                "UPDATE products SET config=? WHERE id=?",
                (json.dumps(p), configured.pid),
            )
        elif change == "card":
            c.execute(
                "UPDATE cards SET state='revoked' WHERE id=?", (configured.card_id,)
            )
        elif change == "attempt":
            c.execute("UPDATE jobs SET attempt=attempt+1 WHERE id='task'")
        elif change == "epoch":
            c.execute(
                "UPDATE task_flow_runs SET flow_epoch=flow_epoch+1 WHERE job_id='task'"
            )
        elif change == "corrupt_ciphertext":
            c.execute("UPDATE task_flow_dispatches SET payload_ciphertext='v1.corrupt'")
        else:
            c.execute("UPDATE task_flow_dispatches SET payload_digest='wrong'")

    async def forbidden(context, *, recheck):
        pytest.fail("stale context reached network")

    monkeypatch.setattr(worker, "_deliver", forbidden)
    assert asyncio.run(worker.outbox_once())
    assert dispatch_row()["state"] == "cancelled"
    assert dispatch_row()["payload_ciphertext"] == ""


def test_recheck_after_dns_cancels_expired_sensitive_input_without_transport(
    owner, clock, monkeypatch
):
    setup(owner, sensitive=True, value="otp-very-private")
    transmitted = []

    async def deliver(context, *, recheck):
        clock[0] += 121
        recheck()
        transmitted.append(context)

    monkeypatch.setattr(worker, "_deliver", deliver)
    assert asyncio.run(worker.outbox_once())
    assert transmitted == []
    assert dispatch_row()["state"] == "cancelled"
    assert dispatch_row()["ciphertext_bytes"] == 0


def test_expired_secret_is_swept_before_transport_backoff_due(
    owner, clock, monkeypatch, finalizer
):
    setup(owner, sensitive=True)
    with db() as c:
        c.execute("UPDATE task_flow_dispatches SET due=?", (clock[0] + 3600,))
    clock[0] += 121
    assert asyncio.run(worker.task_flow_once())
    assert dispatch_row()["state"] == "cancelled"
    assert dispatch_row()["payload_ciphertext"] == ""


def test_a_completed_stage_outbox_is_cancelled_even_while_next_question_waits(
    owner, clock, finalizer
):
    configured = setup(owner)
    with db() as c:
        effect = task_flow.process_update(
            c,
            job(c, "task"),
            JobUpdate(state="succeeded", output={"content": "first answer"}, attempt=1),
            configured.execution["flow_epoch"],
        )
        assert effect["flow"]["phase"] == "input"
    assert asyncio.run(worker.task_flow_once())
    assert dispatch_row()["state"] == "cancelled"
    with db() as c:
        assert job(c, "task")["state"] == "waiting"
        assert (
            c.execute(
                "SELECT state FROM cards WHERE id=?", (configured.card_id,)
            ).fetchone()[0]
            == "reserved"
        )


def test_sending_lease_recovers_only_the_same_action(owner, clock, monkeypatch):
    configured = setup(owner)
    with db() as c:
        c.execute(
            "UPDATE task_flow_dispatches SET state='sending',due=?", (clock[0] + 60,)
        )
    assert not asyncio.run(worker.outbox_once())
    clock[0] += 60
    contexts = []

    async def deliver(context, *, recheck):
        recheck()
        contexts.append(context)

    monkeypatch.setattr(worker, "_deliver", deliver)
    assert asyncio.run(worker.outbox_once())
    assert contexts[0]["action_id"] == configured.execution["action_id"]
    assert dispatch_row()["state"] == "sent"


def test_dispatch_ciphertext_counts_against_card_and_shop_storage(
    owner, clock, monkeypatch
):
    configured = setup(owner, mode="script")
    with db() as c:
        p = product(c, configured.pid)
        p.update(
            mode="webhook",
            webhook_url="https://worker.example.test/receive",
            webhook_secret="s" * 40,
            outputs=[field("content", "textarea")],
        )
        # Re-freeze a test card in the requested mode, never mutate a production binding.
        c.execute("DELETE FROM card_task_flows WHERE card_id=?", (configured.card_id,))
        c.execute(
            "UPDATE products SET config=? WHERE id=?", (json.dumps(p), configured.pid)
        )
        task_flow.freeze_card(c, configured.card_id, {**p, "task_flow": definition()})
        existing = worker._card_ciphertext_bytes(c, configured.card_id)
    monkeypatch.setattr(worker, "MAX_CARD_CIPHERTEXT_BYTES", existing + 1)
    with pytest.raises(HTTPException) as error, db() as c:
        worker.sync_dispatch(c, job(c, "task"))
    assert error.value.status_code == 413
    assert dispatch_row() is None
    monkeypatch.setattr(worker, "MAX_CARD_CIPHERTEXT_BYTES", 2 * 1024 * 1024)
    seen = []

    def quota(c, additional, *, product_id):
        seen.append((additional, product_id))
        raise HTTPException(507, "full")

    monkeypatch.setattr(worker, "check_storage_quota", quota)
    with pytest.raises(HTTPException), db() as c:
        worker.sync_dispatch(c, job(c, "task"))
    assert seen[0][1] == configured.pid and seen[0][0] > 0
    assert dispatch_row() is None


def test_script_uses_sandbox_entry_with_current_stage_inputs_and_preserves_whole_task(
    owner, clock, monkeypatch, finalizer
):
    configured = setup(owner, mode="script", value="stage-specific-input")
    from extore import worker as old_worker

    calls = []

    async def execute(selected, p, *, task_flow_epoch):
        assert_write_boundary_free()
        assert selected["claimed_by"] == "worker"
        assert (
            selected["flow_epoch"]
            == task_flow_epoch
            == configured.execution["flow_epoch"]
        )
        assert json.loads(selected["params"]) == {"name": "stage-specific-input"}
        assert selected["outputs"][0]["key"] == "content"
        with db() as c:
            assert job(c, "task")["params"] == "{}"
            assert job(c, "task")["state"] == "processing"
        calls.append(selected)
        return JobUpdate(
            state="succeeded", attempt=1, output={"content": "stage result"}
        )

    monkeypatch.setattr(old_worker, "execute_script", execute)
    assert asyncio.run(worker.task_flow_once())
    assert len(calls) == 1 and len(finalizer) == 2
    with db() as c:
        row = job(c, "task")
        assert row["state"] == "waiting" and row["result_json"] is None
        assert task_flow.view(c, row)["current"]["id"] == "q2"
        assert (
            c.execute(
                "SELECT state FROM cards WHERE id=?", (configured.card_id,)
            ).fetchone()[0]
            == "reserved"
        )
    assert not asyncio.run(worker.task_flow_once())


def test_script_terminal_finalizes_only_after_end_node(
    owner, clock, monkeypatch, finalizer
):
    configured = setup(owner, mode="script", rounds=1)
    from extore import worker as old_worker

    async def execute(selected, p, *, task_flow_epoch):
        return JobUpdate(state="succeeded", attempt=1, output={"content": "complete"})

    monkeypatch.setattr(old_worker, "execute_script", execute)
    assert asyncio.run(worker.task_flow_once())
    assert finalizer[-1]["terminal"]["state"] == "succeeded"
    with db() as c:
        assert job(c, "task")["state"] == "succeeded"
        assert json.loads(job(c, "task")["result_json"]) == {"content": "complete"}
        assert (
            c.execute(
                "SELECT state FROM cards WHERE id=?", (configured.card_id,)
            ).fetchone()[0]
            == "used"
        )


def test_script_malformed_result_rolls_back_stage_then_fails_safely(
    owner, clock, monkeypatch, finalizer
):
    setup(owner, mode="script")
    from extore import worker as old_worker

    async def execute(selected, p, *, task_flow_epoch):
        return JobUpdate(state="succeeded", attempt=1, output={"unknown": "wrong"})

    monkeypatch.setattr(old_worker, "execute_script", execute)
    assert asyncio.run(worker.task_flow_once())
    with db() as c:
        assert job(c, "task")["state"] == "failed"
        assert task_flow.view(c, job(c, "task"))["phase"] == "ended"
        row = c.execute("SELECT * FROM task_flow_steps WHERE kind='process'").fetchone()
        assert row["state"] == "failed"
    assert finalizer[-1]["terminal"]["needs_review"] is True


def test_stage_attachment_adapter_failure_rolls_back_acceptance_before_safe_failure(
    owner, clock, monkeypatch, finalizer
):
    setup(owner, mode="script")
    from extore import worker as old_worker

    original = worker._finalize

    def rejecting_adapter(c, effect):
        if "accepted" in effect:
            c.execute("UPDATE settings SET value='bad' WHERE key='nothing'")
            raise ValueError("attachment ownership failed")
        return original(c, effect)

    async def execute(selected, p, *, task_flow_epoch):
        return JobUpdate(
            state="succeeded", attempt=1, output={"content": "must be rolled back"}
        )

    monkeypatch.setattr(worker, "_finalize", rejecting_adapter)
    monkeypatch.setattr(old_worker, "execute_script", execute)
    assert asyncio.run(worker.task_flow_once())
    with db() as c:
        assert job(c, "task")["state"] == "failed"
        assert (
            c.execute(
                "SELECT COUNT(*) FROM task_flow_steps WHERE node_id='q2'"
            ).fetchone()[0]
            == 0
        )
        assert "must be rolled back" not in json.dumps(
            task_flow.view(c, job(c, "task"))
        )


def test_expired_process_finalizes_timeout_before_any_script_claim(
    owner, clock, monkeypatch, finalizer
):
    setup(owner, mode="script")
    from extore import worker as old_worker

    async def forbidden(*args, **kwargs):
        pytest.fail("expired stage reached sandbox")

    monkeypatch.setattr(old_worker, "execute_script", forbidden)
    clock[0] += 3601
    assert asyncio.run(worker.task_flow_once())
    assert len(finalizer) == 1 and finalizer[0]["terminal"]["state"] == "failed"
    with db() as c:
        assert job(c, "task")["state"] == "failed"


def test_stale_script_result_after_stage_cancellation_is_discarded(
    owner, clock, monkeypatch, finalizer
):
    setup(owner, mode="script")
    from extore import worker as old_worker

    async def execute(selected, p, *, task_flow_epoch):
        with db() as c:
            row = job(c, "task")
            view = task_flow.view(c, row)
            original = task_flow.cancel(c, row, view["flow_epoch"], view["revision"])
            worker._finalize(c, original)
        return JobUpdate(
            state="succeeded", attempt=1, output={"content": "late result"}
        )

    monkeypatch.setattr(old_worker, "execute_script", execute)
    assert asyncio.run(worker.task_flow_once())
    with db() as c:
        assert job(c, "task")["state"] == "failed"
        assert "late result" not in (job(c, "task")["result_json"] or "")


def test_claim_timeout_effect_is_finalized_without_misclassifying_it_as_claimed(
    owner, clock, monkeypatch, finalizer
):
    setup(owner, mode="script")
    from extore import worker as old_worker

    original = task_flow.claim

    def timeout_during_claim(c, row, actor, epoch):
        clock[0] += 3601
        return original(c, row, actor, epoch)

    async def forbidden(*args, **kwargs):
        pytest.fail("timeout effect was treated as a successful claim")

    monkeypatch.setattr(task_flow, "claim", timeout_during_claim)
    monkeypatch.setattr(old_worker, "execute_script", forbidden)
    assert asyncio.run(worker.task_flow_once())
    assert finalizer[-1]["terminal"]["state"] == "failed"
    with db() as c:
        assert job(c, "task")["state"] == "failed"


def test_retained_attempt_limit_and_encrypted_scope_metadata_are_failclosed(
    owner, clock, monkeypatch
):
    setup(owner)

    async def forbidden(*args, **kwargs):
        pytest.fail("invalid retained dispatch reached transport")

    monkeypatch.setattr(worker, "_deliver", forbidden)
    with db() as c:
        c.execute("UPDATE task_flow_dispatches SET node_id='different-node'")
    assert asyncio.run(worker.outbox_once())
    assert dispatch_row()["state"] == "cancelled"
    # Even a retained/corrupt pending marker cannot bypass the retry hard cap.
    with db() as c:
        c.execute("UPDATE task_flow_dispatches SET state='pending',attempts=8")
    assert asyncio.run(worker.outbox_once())
    assert dispatch_row()["state"] == "dead"
    assert dispatch_row()["attempts"] == 8


def test_launched_script_can_finish_after_sensitive_input_wipe_without_reusing_it(
    owner, clock, monkeypatch, finalizer
):
    setup(owner, mode="script", rounds=1, sensitive=True, value="short-lived-otp")
    from extore import worker as old_worker

    calls = []

    async def execute(selected, p, *, task_flow_epoch):
        assert json.loads(selected["params"]) == {"name": "short-lived-otp"}
        calls.append(selected["action_id"])
        clock[0] += 121
        with db() as c:
            for effect in task_flow.expire_due(c):
                worker._finalize(c, effect)
            row = job(c, "task")
            assert row["state"] == "processing"
            assert task_flow.execution(c, row) is None
        return JobUpdate(
            state="succeeded", attempt=1, output={"content": "processed once"}
        )

    monkeypatch.setattr(old_worker, "execute_script", execute)
    assert asyncio.run(worker.task_flow_once())
    assert len(calls) == 1
    with db() as c:
        assert job(c, "task")["state"] == "succeeded"
        assert json.loads(job(c, "task")["result_json"]) == {
            "content": "processed once"
        }
    assert not asyncio.run(worker.task_flow_once())

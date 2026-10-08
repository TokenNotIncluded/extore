"""Real SQLite, graph finalization and bounded native processor integration.

All inputs, profiles and test catalogs are synthetic. Native probes use the
production sandbox; they do not replace its launch command or resource policy.
"""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from test_flow_worker import assert_write_boundary_free, definition
from test_official_processing import create, redeem
from test_processor_protocol_independent import package

from extore import flow_worker, processor_runtime, service, task_flow, worker
from extore.db import db
from extore.service import job, product


def flow_task(owner, *, rounds=2, sensitive=False):
    configured = create(
        owner,
        configuration={"template": "Trusted $name"},
        task_flow=definition(rounds, sensitive=sensitive),
    )
    exchanged, initial = redeem(owner, configured, {})
    receipt = SimpleNamespace(
        pid=configured["id"],
        jid=initial["id"],
        token=exchanged["token"],
        configured=configured,
    )
    action(owner, receipt, "start")
    action(owner, receipt, "answer", {"name": "stage-one"})
    return receipt


def action(owner, receipt, name, values=None):
    with db() as c:
        view = task_flow.view(c, job(c, receipt.jid))
    response = owner.post(
        f"/api/task-flow/{name}",
        json={
            "token": receipt.token,
            "flow_epoch": view["flow_epoch"],
            "expected_revision": view["revision"],
            **({"values": values} if values else {}),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def claimed(receipt):
    with db() as c:
        current = job(c, receipt.jid)
        execution = task_flow.execution(c, current)
        service.finalize_task_flow(
            c, task_flow.claim(c, current, "worker", execution["flow_epoch"])
        )
        current = job(c, receipt.jid)
        return dict(current), product(c, receipt.pid), task_flow.execution(c, current)


def test_native_worker_completes_two_stages_only_at_end_node(owner):
    receipt = flow_task(owner)
    assert asyncio.run(flow_worker.task_flow_once())
    with db() as c:
        current = job(c, receipt.jid)
        view = task_flow.view(c, current)
        assert current["state"] == "waiting" and view["phase"] == "input"
        assert view["current"]["id"] == "q2"
        assert current["params"] == "{}" and current["result_json"] is None
        assert json.loads(current["completed_steps"]) == []
        # The native catalog's progress_steps cannot become the whole order's plan.
        assert json.loads(current["progress_plan"]) == []
        assert (
            c.execute(
                "SELECT state FROM cards WHERE id=?", (current["card_id"],)
            ).fetchone()[0]
            == "reserved"
        )
        assert (
            c.execute(
                "SELECT count(*) FROM events WHERE type='fulfillment.succeeded'"
            ).fetchone()[0]
            == 0
        )
    action(owner, receipt, "answer", {"name": "stage-two"})
    assert asyncio.run(flow_worker.task_flow_once())
    with db() as c:
        current = job(c, receipt.jid)
        assert current["state"] == "succeeded" and current["params"] == "{}"
        assert json.loads(current["result_json"]) == {"content": "Trusted stage-two"}
        assert (
            c.execute(
                "SELECT count(*) FROM events WHERE type='fulfillment.succeeded'"
            ).fetchone()[0]
            == 1
        )
        for row in c.execute("SELECT payload FROM events"):
            assert (
                "stage-one" not in row["payload"] and "stage-two" not in row["payload"]
            )


def test_execute_script_uses_stage_input_and_issued_profile_not_caller_metadata(owner):
    receipt = flow_task(owner)
    row, configured, execution = claimed(receipt)
    profile = owner.get(f"/api/admin/processor-profiles/bindings/{receipt.pid}").json()[
        "profile"
    ]
    response = owner.put(
        f"/api/admin/processor-profiles/{profile['id']}",
        json={"configuration": {"template": "New revision $name"}},
    )
    assert response.status_code == 200, response.text
    row.update(
        params='{"name":"forged"}',
        flow_epoch=999,
        action_id="forged",
        shop_context={"shop_id": "forged"},
    )
    configured["processor_config"] = {"template": "Forged $name"}
    result = asyncio.run(
        worker.execute_script(row, configured, task_flow_epoch=execution["flow_epoch"])
    )
    assert result.output == {"content": "Trusted stage-one"}
    assert (
        result.flow_epoch == execution["flow_epoch"]
        and result.action_id == execution["action_id"]
    )
    with db() as c:
        current = job(c, receipt.jid)
        assert current["state"] == "processing" and current["result_json"] is None
        assert current["params"] == "{}" and current["progress"] == 99
        assert json.loads(current["completed_steps"]) == []


@pytest.mark.parametrize(
    "invalid",
    ["epoch", "missing_epoch", "deadline", "shop", "profile", "processor", "claim"],
)
def test_invalid_stage_never_reaches_native_launch(owner, monkeypatch, invalid):
    receipt = flow_task(owner)
    row, configured, execution = claimed(receipt)
    epoch = execution["flow_epoch"]
    with db() as c:
        if invalid == "epoch":
            epoch += 1
        elif invalid == "missing_epoch":
            epoch = None
        elif invalid == "deadline":
            c.execute(
                "UPDATE task_flow_runs SET deadline=1 WHERE job_id=?", (receipt.jid,)
            )
        elif invalid == "shop":
            c.execute(
                "UPDATE shops SET enabled=0 WHERE id=(SELECT shop_id FROM products WHERE id=?)",
                (receipt.pid,),
            )
        elif invalid == "profile":
            c.execute("UPDATE processor_profiles SET disabled=1")
        elif invalid == "processor":
            c.execute(
                "UPDATE products SET config=json_set(config,'$.processor_id','resource_link') WHERE id=?",
                (receipt.pid,),
            )
        else:
            c.execute(
                "UPDATE jobs SET claimed_by='someone-else' WHERE id=?", (receipt.jid,)
            )

    async def forbidden(*args, **kwargs):
        pytest.fail("invalid stage reached the processor")

    monkeypatch.setattr(worker, "_execute_processor", forbidden)
    with pytest.raises((ValueError, HTTPException)):
        asyncio.run(worker.execute_script(row, configured, task_flow_epoch=epoch))


def test_shop_revocation_during_sandbox_build_prevents_launch(owner, monkeypatch):
    receipt = flow_task(owner)
    row, configured, execution = claimed(receipt)

    def build(*args):
        assert_write_boundary_free()
        with db() as c:
            c.execute(
                "UPDATE shops SET enabled=0 WHERE id=(SELECT shop_id FROM products WHERE id=?)",
                (receipt.pid,),
            )
        return []

    async def forbidden(*args, **kwargs):
        pytest.fail("revoked shop reached process launch")

    monkeypatch.setattr(processor_runtime, "sandbox_command", build)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(ValueError, match="授权已失效"):
        asyncio.run(
            worker.execute_script(
                row, configured, task_flow_epoch=execution["flow_epoch"]
            )
        )


def test_sensitive_input_expiring_during_sandbox_build_prevents_launch(
    owner, monkeypatch
):
    now = [time.time()]
    source = SimpleNamespace(time=lambda: now[0])
    monkeypatch.setattr(worker, "time", source)
    monkeypatch.setattr(task_flow, "time", source)
    receipt = flow_task(owner, sensitive=True)
    row, configured, execution = claimed(receipt)

    def build(*args):
        now[0] += 121
        return []

    async def forbidden(*args, **kwargs):
        pytest.fail("expired sensitive input reached process launch")

    monkeypatch.setattr(processor_runtime, "sandbox_command", build)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    with pytest.raises(ValueError, match="输入已失效"):
        asyncio.run(
            worker.execute_script(
                row, configured, task_flow_epoch=execution["flow_epoch"]
            )
        )


def test_revocation_after_native_launch_reaps_child_before_input(owner, monkeypatch):
    receipt = flow_task(owner)
    row, configured, execution = claimed(receipt)
    original = asyncio.create_subprocess_exec
    captured = []

    async def capture(*args, **kwargs):
        proc = await original(*args, **kwargs)
        captured.append(proc)
        with db() as c:
            c.execute("UPDATE processor_profiles SET disabled=1")
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    with pytest.raises(ValueError, match="授权已失效"):
        asyncio.run(
            worker.execute_script(
                row, configured, task_flow_epoch=execution["flow_epoch"]
            )
        )
    assert captured and captured[0].returncode is not None


def test_worker_loop_pumps_both_private_flow_queues(monkeypatch):
    seen = set()
    required = {"legacy-jobs", "legacy-events", "flow-jobs", "flow-events", "mail"}

    def handler(name):
        async def run():
            seen.add(name)
            await asyncio.Event().wait()

        return run

    monkeypatch.setattr(worker, "job_once", handler("legacy-jobs"))
    monkeypatch.setattr(worker, "outbox_once", handler("legacy-events"))
    monkeypatch.setattr(flow_worker, "task_flow_once", handler("flow-jobs"))
    monkeypatch.setattr(flow_worker, "outbox_once", handler("flow-events"))
    from extore import mail, maintenance, storage

    monkeypatch.setattr(mail, "process_outbox_once", handler("mail"))
    monkeypatch.setattr(storage, "maintenance_once", lambda: None)
    monkeypatch.setattr(maintenance, "maintenance_once", lambda: None)
    monkeypatch.setattr(worker, "automation_maintenance_once", lambda: None)

    async def run():
        pending = asyncio.create_task(worker.loop())
        try:

            async def started():
                while seen != required:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(started(), 2)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(run())


def instrument_catalog(monkeypatch, catalog):
    original = worker._execute_processor

    async def execute(row, configured, ignored_package):
        return await original(row, configured, catalog)

    monkeypatch.setattr(worker, "_execute_processor", execute)


async def wait_entered(pending, jid):
    async def check():
        while True:
            with db() as c:
                if job(c, jid)["message"] == "entered":
                    return
            if pending.done():
                await pending
                pytest.fail("processor exited before fixture entered")
            await asyncio.sleep(0.02)

    await asyncio.wait_for(check(), 5)


@pytest.mark.parametrize("revoke", ["shop", "profile", "cancel", "deadline"])
def test_silent_native_stage_is_killed_when_authority_expires(
    owner, tmp_path, monkeypatch, revoke
):
    receipt = flow_task(owner)
    row, configured, execution = claimed(receipt)
    catalog = package(
        tmp_path,
        "import json,sys,time\njson.load(sys.stdin)\n"
        "print(json.dumps({'kind':'progress','progress':0,'message':'entered'}),flush=True)\n"
        "time.sleep(60)\n",
    )
    instrument_catalog(monkeypatch, catalog)
    original = asyncio.create_subprocess_exec
    captured = []

    async def capture(*args, **kwargs):
        assert_write_boundary_free()
        proc = await original(*args, **kwargs)
        captured.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)

    async def run():
        pending = asyncio.create_task(
            worker.execute_script(
                row, configured, task_flow_epoch=execution["flow_epoch"]
            )
        )
        try:
            await wait_entered(pending, receipt.jid)
            with db() as c:
                if revoke == "shop":
                    c.execute(
                        "UPDATE shops SET enabled=0 WHERE id=(SELECT shop_id FROM products WHERE id=?)",
                        (receipt.pid,),
                    )
                elif revoke == "profile":
                    c.execute("UPDATE processor_profiles SET disabled=1")
                elif revoke == "deadline":
                    c.execute(
                        "UPDATE task_flow_steps SET deadline=1 WHERE job_id=? AND flow_epoch=?",
                        (receipt.jid, execution["flow_epoch"]),
                    )
                else:
                    c.execute(
                        "UPDATE jobs SET state='failed',claimed_by=NULL WHERE id=?",
                        (receipt.jid,),
                    )
            with pytest.raises(ValueError):
                await asyncio.wait_for(pending, 3)
            assert captured and captured[0].returncode is not None
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    asyncio.run(run())


def test_started_native_stage_can_finish_after_sensitive_input_is_wiped(
    owner, tmp_path, monkeypatch
):
    now = [time.time()]
    source = SimpleNamespace(time=lambda: now[0])
    monkeypatch.setattr(worker, "time", source)
    monkeypatch.setattr(task_flow, "time", source)
    receipt = flow_task(owner, rounds=1, sensitive=True)
    row, configured, execution = claimed(receipt)
    catalog = package(
        tmp_path,
        "import json,sys,time\njson.load(sys.stdin)\n"
        "print(json.dumps({'kind':'progress','progress':0,'message':'entered'}),flush=True)\n"
        "time.sleep(0.5)\n"
        "print(json.dumps({'kind':'result','state':'succeeded','output':{'content':'finished'}}),flush=True)\n",
    )
    instrument_catalog(monkeypatch, catalog)

    async def run():
        pending = asyncio.create_task(
            worker.execute_script(
                row, configured, task_flow_epoch=execution["flow_epoch"]
            )
        )
        await wait_entered(pending, receipt.jid)
        now[0] += 121
        with db() as c:
            for effect in task_flow.expire_due(c):
                service.finalize_task_flow(c, effect)
            assert task_flow.execution(c, job(c, receipt.jid)) is None
        result = await asyncio.wait_for(pending, 3)
        with db() as c:
            current = job(c, receipt.jid)
            assert flow_worker._script_authority(
                c, current, execution, execution["flow_epoch"]
            )
            service.finalize_task_flow(
                c,
                task_flow.process_update(
                    c, current, result, execution["flow_epoch"], actor="worker"
                ),
            )
            assert job(c, receipt.jid)["state"] == "succeeded"

    asyncio.run(run())


def test_legacy_scan_does_not_claim_or_expire_flow_jobs(owner):
    receipt = flow_task(owner)
    ordinary = create(owner)
    _, ordinary_job = redeem(owner, ordinary, {"name": "ordinary"})
    assert asyncio.run(worker.job_once())
    with db() as c:
        assert job(c, ordinary_job["id"])["state"] == "succeeded"
        assert job(c, receipt.jid)["state"] == "queued"
    claimed(receipt)
    with db() as c:
        c.execute("UPDATE jobs SET lease=1 WHERE id=?", (receipt.jid,))
    assert asyncio.run(worker.job_once()) is False
    with db() as c:
        assert job(c, receipt.jid)["state"] == "processing"


def test_automation_maintenance_deletes_only_two_hundred_expired_receipts():
    now = time.time()
    with db() as c:
        c.executemany(
            "INSERT INTO automation_requests(id,device_id,body_hash,state,created,deadline,expires) VALUES (?,'synthetic','hash','done',1,1,1)",
            [(f"expired-{index}",) for index in range(205)],
        )
        c.execute(
            "INSERT INTO automation_requests(id,device_id,body_hash,state,created,deadline,expires) VALUES ('live','synthetic','hash','pending',?,?,?)",
            (now, now + 25, now + 600),
        )
    assert worker.automation_maintenance_once() == 200
    assert worker.automation_maintenance_once() == 5
    with db() as c:
        assert [
            row["id"] for row in c.execute("SELECT id FROM automation_requests")
        ] == ["live"]

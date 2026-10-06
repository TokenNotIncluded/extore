"""Revoke a synthetic issued profile after the real child exits, before commit."""

import asyncio

from test_task_flow_native_worker import flow_task

from extore import flow_worker, task_flow, worker
from extore.db import db
from extore.service import job


def test_profile_revoked_after_successful_native_return_cannot_finalize_stage(
    owner, monkeypatch
):
    receipt = flow_task(owner)
    profile = owner.get(f"/api/admin/processor-profiles/bindings/{receipt.pid}").json()[
        "profile"
    ]
    original = worker.execute_script
    returned = []

    async def revoke_after_real_return(*args, **kwargs):
        # Keep the actual launcher, catalog, resource policy and child parsing.
        result = await original(*args, **kwargs)
        assert result.state == "succeeded"
        assert result.output == {"content": "Trusted stage-one"}
        returned.append(result)
        with db() as c:
            updated = c.execute(
                "UPDATE processor_profiles SET disabled=1 WHERE id=? AND disabled=0",
                (profile["id"],),
            )
            assert updated.rowcount == 1
        return result

    monkeypatch.setattr(worker, "execute_script", revoke_after_real_return)
    assert asyncio.run(flow_worker.task_flow_once())
    assert len(returned) == 1
    with db() as c:
        current = job(c, receipt.jid)
        view = task_flow.view(c, current)
        assert current["state"] == "processing"
        assert current["result_json"] is None and current["params"] == "{}"
        assert view["phase"] == "processing" and view["current"]["id"] == "p1"
        assert view["flow_epoch"] == returned[0].flow_epoch
        assert (
            c.execute(
                "SELECT state FROM cards WHERE id=?", (current["card_id"],)
            ).fetchone()[0]
            == "reserved"
        )
        assert (
            c.execute(
                "SELECT count(*) FROM task_flow_steps WHERE job_id=? AND node_id='q2'",
                (receipt.jid,),
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT count(*) FROM events WHERE type='fulfillment.succeeded'"
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute(
                "SELECT state FROM task_flow_steps WHERE job_id=? AND flow_epoch=?",
                (receipt.jid, returned[0].flow_epoch),
            ).fetchone()[0]
            == "active"
        )

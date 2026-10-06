"""Independent bounded adversarial probes of the real worker/sandbox protocol.

All catalogs, payloads and jobs are synthetic. These fixtures do not exercise
the merchant API's closed catalog selection or touch production data.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from test_official_processing import create, redeem

from extore import processor_runtime, worker
from extore.db import db
from extore.models import JobUpdate
from extore.service import apply_update, job


def package(tmp_path, source):
    catalog = tmp_path / "protocol_catalog"
    catalog.mkdir()
    (catalog / "__init__.py").write_text("", encoding="utf-8")
    (catalog / "__main__.py").write_text(source, encoding="utf-8")
    return SimpleNamespace(__file__=str(catalog / "__init__.py"))


def task(*, params=None, output_limit=1_000_000):
    return {
        "id": "synthetic-protocol-task",
        "attempt": 1,
        "params": json.dumps(params or {}),
        "variant": {},
        "steps": [],
        "completed_steps": [],
        "shop_context": {},
        "workflow": processor_runtime.validate_workflow(
            {"runtime": {"timeout_seconds": 10, "max_output_bytes": output_limit}}
        ),
    }


def execute(row, catalog):
    return asyncio.run(
        worker._execute_processor(
            row, {"processor_id": "protocol_probe", "processor_config": {}}, catalog
        )
    )


def test_full_encoded_input_is_rejected_before_process_launch(monkeypatch):
    def forbidden(*args):
        raise AssertionError("oversized input reached sandbox construction")

    monkeypatch.setattr(processor_runtime, "sandbox_command", forbidden)
    with pytest.raises(ValueError, match="输入超过限制"):
        execute(task(params={"content": "\x01" * 40_000}), SimpleNamespace())


def test_bidirectional_large_input_and_output_do_not_deadlock(tmp_path):
    catalog = package(
        tmp_path,
        "import json,sys\n"
        "print(json.dumps({'kind':'result','state':'succeeded',"
        "'output':{'content':'x'*50_000}}),flush=True)\n"
        "payload=json.load(sys.stdin)\n"
        "assert len(payload['params']['content'])==150_000\n",
    )
    result = execute(task(params={"content": "x" * 150_000}), catalog)
    assert result.state == "succeeded" and len(result.output["content"]) == 50_000


def test_output_byte_limit_fails_even_for_one_valid_result(tmp_path):
    catalog = package(
        tmp_path,
        "import json,sys\n"
        "json.load(sys.stdin)\n"
        "print(json.dumps({'kind':'result','state':'succeeded',"
        "'output':{'content':'x'*70_000}}),flush=True)\n",
    )
    with pytest.raises(ValueError, match="输出超过限制"):
        execute(task(output_limit=65_536), catalog)


def test_unterminated_overlong_output_fails_without_waiting_for_wall_timeout(tmp_path):
    catalog = package(
        tmp_path,
        "import json,os,sys\njson.load(sys.stdin)\nos.write(1,b'x'*140_000)\n",
    )
    with pytest.raises(ValueError):
        execute(task(), catalog)


def test_result_followed_by_progress_cannot_update_a_job(tmp_path):
    catalog = package(
        tmp_path,
        "import json,sys\n"
        "json.load(sys.stdin)\n"
        "print(json.dumps({'kind':'result','state':'succeeded',"
        "'output':{'content':'done'}}),flush=True)\n"
        "print(json.dumps({'kind':'progress','progress':99}),flush=True)\n",
    )
    with pytest.raises(ValueError, match="结果后不能继续输出"):
        execute(task(), catalog)


def test_progress_flood_is_bounded_before_an_extra_database_update(owner, tmp_path):
    product = create(owner)
    _, queued = redeem(owner, product, {"name": "Synthetic buyer"})
    with db() as c:
        apply_update(
            c, queued["id"], JobUpdate(state="processing", attempt=1, progress=0)
        )
        row = {**dict(job(c, queued["id"])), **task()}
        row["id"] = queued["id"]
        before = c.execute(
            "SELECT count(*) FROM events WHERE type='fulfillment.progress' AND job_id=?",
            (row["id"],),
        ).fetchone()[0]
    catalog = package(
        tmp_path,
        "import json,sys\n"
        "json.load(sys.stdin)\n"
        "for i in range(101):\n"
        " print(json.dumps({'kind':'progress','progress':0,'message':'bounded'}),"
        "flush=True)\n",
    )
    with pytest.raises(ValueError, match="进度更新超过限制"):
        execute(row, catalog)
    with db() as c:
        after = c.execute(
            "SELECT count(*) FROM events WHERE type='fulfillment.progress' AND job_id=?",
            (row["id"],),
        ).fetchone()[0]
        assert after - before == processor_runtime.MAX_PROGRESS_EVENTS == 100
        assert job(c, row["id"])["state"] == "processing"


def test_cancellation_reaps_the_started_sandbox(owner, tmp_path, monkeypatch):
    product = create(owner)
    _, queued = redeem(owner, product, {"name": "Synthetic buyer"})
    with db() as c:
        apply_update(
            c, queued["id"], JobUpdate(state="processing", attempt=1, progress=0)
        )
    row = {**task(), "id": queued["id"]}
    catalog = package(
        tmp_path,
        "import json,sys,time\n"
        "json.load(sys.stdin)\n"
        "print(json.dumps({'kind':'progress','progress':0,'message':'entered'}),"
        "flush=True)\n"
        "time.sleep(60)\n",
    )
    original = asyncio.create_subprocess_exec

    async def probe():
        started = asyncio.Event()
        captured = []

        async def capture(*args, **kwargs):
            proc = await original(*args, **kwargs)
            captured.append(proc)
            started.set()
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
        pending = asyncio.create_task(
            worker._execute_processor(
                row,
                {"processor_id": "protocol_probe", "processor_config": {}},
                catalog,
            )
        )
        await asyncio.wait_for(started.wait(), 20)

        async def entered():
            while True:
                with db() as c:
                    if job(c, row["id"])["message"] == "entered":
                        return
                if pending.done():
                    await pending
                    raise AssertionError("sandbox exited before fixture started")
                await asyncio.sleep(0.02)

        await asyncio.wait_for(entered(), 5)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, 5)
        assert captured[0].returncode is not None

    asyncio.run(probe())

"""Exercise the Rust worker, reviewed SDK and real offline bubblewrap sandbox."""
import os
import subprocess
import time
from pathlib import Path

import pytest


@pytest.mark.parametrize("progress_steps", [[], [{"id": "merchant_delivery", "label": {"en": "Merchant delivery", "zh-CN": "商家交付"}}]])
def test_native_worker_runs_reviewed_processor_and_keeps_single_owner(owner, tmp_path, progress_steps):
    if type(owner).__module__ != "native_http_plugin":
        pytest.skip("Enable -p native_http_plugin to exercise the native service")
    root = Path(__file__).resolve().parents[1]
    response = owner.post(
        "/api/admin/products",
        json={
            "name": "Native sandbox contract",
            "mode": "script",
            "processor_id": "personalized_text",
            "processor_config": {"template": "Hello $name"},
            "progress_steps": progress_steps,
        },
    )
    assert response.status_code == 200, response.text
    product = response.json()
    issued = owner.post(
        "/api/admin/cards", json={"product_id": product["id"], "count": 1}
    )
    assert issued.status_code == 200, issued.text
    checked = owner.post("/api/exchange", json={"code": issued.json()["codes"][0]})
    assert checked.status_code == 200, checked.text
    token = checked.json()["token"]
    submitted = owner.post("/api/redeem", json={"token": token, "params": {"name": "Rust"}})
    assert submitted.status_code == 200, submitted.text
    env = dict(os.environ, EXTORE_STATIC=str(root / "extore/static"))
    binary = str(root / "target/debug/extore")
    with (tmp_path / "worker.log").open("wb") as log:
        worker = subprocess.Popen([binary, "worker"], cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                assert worker.poll() is None, (tmp_path / "worker.log").read_text()
                receipt = owner.post("/api/receipt", json={"token": token})
                assert receipt.status_code == 200, receipt.text
                if receipt.json().get("job", {}).get("state") in {"succeeded", "failed"}:
                    break
                time.sleep(0.1)
            assert receipt.json()["job"]["state"] == "succeeded", receipt.text + "\n" + (tmp_path / "worker.log").read_text()
            job = receipt.json()["job"]
            step_ids = [step["id"] for step in job["steps"]]
            assert step_ids, "The reviewed processor must bootstrap a plan when none was supplied"
            assert job["completed_steps"] == step_ids
            assert job["progress"] == 100
            if progress_steps:
                assert step_ids == [step["id"] for step in progress_steps], "A merchant's frozen plan must be preserved"
            delivered = owner.post("/api/receipt/reveal", json={"token": token})
            assert delivered.status_code == 200, delivered.text
            assert delivered.json()["content"] == "Hello Rust"
            second = subprocess.run([binary, "worker"], cwd=root, env=env, capture_output=True, timeout=5)
            assert second.returncode != 0, "The second worker must not acquire this database"
        finally:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=5)

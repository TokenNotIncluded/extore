"""Native HTTP waits must release SQLite and recheck device authority."""
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_automation_api import device, next_, product, request
from test_automation_flow_integration import flow_product

from extore.db import db


@pytest.fixture(autouse=True)
def native_only(pytestconfig):
    if not pytestconfig.pluginmanager.hasplugin("native_http_plugin"):
        pytest.skip("This suite verifies the separate native HTTP server")
    with db() as conn:
        conn.execute("DELETE FROM automation_requests")


def queue_native(owner, pid, value="native-customer-input"):
    issued = owner.post("/api/admin/cards", json={"product_id": pid})
    assert issued.status_code == 200, issued.text
    receipt = owner.post("/api/exchange", json={"code": issued.json()["codes"][0]})
    assert receipt.status_code == 200, receipt.text
    submitted = owner.post("/api/redeem", json={"token": receipt.json()["token"], "params": {"request": value}})
    assert submitted.status_code == 200, submitted.text
    return submitted.json()


def wait_pending():
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with db() as conn:
            if conn.execute("SELECT count(*) FROM automation_requests WHERE state='pending'").fetchone()[0]:
                return
        time.sleep(0.02)
    pytest.fail("Native request did not publish its pending receipt")


def test_real_wait_releases_database_and_rechecks_revocation(owner):
    pid = product(owner)
    bot = device(owner, pid)
    body = request(bot, wait=3)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(next_, bot, body)
        wait_pending()
        started = time.monotonic()
        with db() as conn:
            conn.execute("UPDATE staff SET revoked=1 WHERE id=(SELECT staff_id FROM cli_devices WHERE id=?)", (bot[1]["device_id"],))
        assert time.monotonic() - started < 1.5, "Long poll retained a SQLite write lock"
        row = queue_native(owner, pid)
        response = future.result(timeout=5)
    assert response.status_code == 401, response.text
    with db() as conn:
        saved = conn.execute("SELECT state,claimed_by FROM jobs WHERE id=?", (row["id"],)).fetchone()
        assert saved["state"] == "queued" and saved["claimed_by"] is None


def test_real_wait_claims_arrival_and_replay_keeps_one_identity(owner):
    pid = product(owner)
    bot = device(owner, pid)
    body = request(bot, wait=3)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(next_, bot, body)
        wait_pending()
        row = queue_native(owner, pid)
        response = future.result(timeout=5)
    assert response.status_code == 200, response.text
    value = response.json()
    assert [item["job"]["id"] for item in value["items"]] == [row["id"]]
    replay = next_(bot, body)
    assert replay.status_code == 200 and replay.json()["replayed"]
    assert replay.json()["items"] == value["items"]
    with db() as conn:
        saved = conn.execute("SELECT items FROM automation_requests").fetchone()[0]
        assert "native-customer-input" not in saved
        assert conn.execute("SELECT count(*) FROM job_worker_identities WHERE job_id=?", (row["id"],)).fetchone()[0] == 1


@pytest.mark.parametrize("secret_expiry", [False, True])
def test_real_expired_flow_never_falls_back_to_classic_claim(owner, secret_expiry):
    pid, _, flow = flow_product(owner, sensitive=secret_expiry)
    other = product(owner)
    safe = queue_native(owner, other, "safe-other-queue")
    bots = device(owner, pid), device(owner, other)
    if secret_expiry:
        time.sleep(1.1)
    else:
        with db() as conn:
            conn.execute("UPDATE task_flow_runs SET deadline=? WHERE job_id=?", (time.time() - 1, flow["id"]))
    response = next_(bots[0], request(*bots))
    assert response.status_code == 200, response.text
    assert [item["job"]["id"] for item in response.json()["items"]] == [safe["id"]]
    assert "isolated-customer-input" not in response.text
    with db() as conn:
        current = conn.execute("SELECT state,claimed_by FROM jobs WHERE id=?", (flow["id"],)).fetchone()
        assert current["state"] == "queued" and current["claimed_by"] is None

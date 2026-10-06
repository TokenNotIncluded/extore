import pytest
from fastapi.testclient import TestClient
from test_redemption import redeem

from extore.app import app
from extore.db import db


@pytest.fixture
def queue_states(owner, setup_product):
    pid, first = setup_product()
    issued = owner.post(
        "/api/admin/cards", json={"product_id": pid, "count": 4}
    ).json()["codes"]
    records = {}
    for state, code in zip(
        ("queued", "processing", "failed", "succeeded", "destroyed"),
        [first, *issued],
        strict=True,
    ):
        _, record = redeem(owner, code)
        with db() as c:
            c.execute("UPDATE jobs SET state=? WHERE id=?", (state, record["id"]))
        records[state] = record["id"]
    return pid, records


def listed(owner, pid, **query):
    response = owner.get("/api/manage/jobs", params={"product_id": pid, **query})
    assert response.status_code == 200, response.text
    return {row["state"]: row["id"] for row in response.json()}


def test_default_queue_shows_only_unfinished_jobs(owner, queue_states):
    pid, records = queue_states
    assert listed(owner, pid) == {
        key: records[key] for key in ("queued", "processing", "failed")
    }
    assert listed(owner, pid, view="active") == listed(owner, pid)
    assert listed(owner, pid, view="processed") == {
        key: records[key] for key in ("succeeded", "destroyed")
    }
    assert listed(owner, pid, view="all") == records


def test_explicit_state_takes_precedence_over_queue_view(owner, queue_states):
    pid, records = queue_states
    assert listed(owner, pid, state="succeeded") == {"succeeded": records["succeeded"]}
    assert listed(owner, pid, view="processed", state="queued") == {
        "queued": records["queued"]
    }


def test_exact_job_id_can_find_completed_history(owner, queue_states, setup_product):
    pid, records = queue_states
    assert listed(owner, pid, job_id=records["succeeded"]) == {
        "succeeded": records["succeeded"]
    }
    assert listed(owner, pid, view="active", job_id=records["destroyed"]) == {
        "destroyed": records["destroyed"]
    }
    assert listed(owner, pid, state="queued", job_id=records["succeeded"]) == {}
    foreign, code = setup_product()
    _, job = redeem(owner, code)
    assert listed(owner, pid, job_id=job["id"], view="all") == {}
    assert listed(owner, foreign, job_id=records["succeeded"]) == {}


def test_compact_queue_omits_customer_body_but_keeps_action_context(
    owner, queue_states
):
    pid, records = queue_states
    response = owner.get(
        "/api/manage/jobs", params={"product_id": pid, "compact": "true"}
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    assert {row["id"] for row in rows} == {
        records[state] for state in ("queued", "processing", "failed")
    }
    for row in rows:
        assert (
            not {"params", "parameters", "outputs", "files", "content", "output"}
            & row.keys()
        )
        assert row["product_id"] == pid
        assert "claimed_by" in row and "attempt" in row
        assert row["attachments"] == {
            "input": {"count": 0, "bytes": 0},
            "output": {"count": 0, "bytes": 0},
        }
    detail = owner.get(
        "/api/manage/jobs", params={"product_id": pid, "job_id": records["queued"]}
    ).json()[0]
    assert detail["params"] and detail["parameters"]


def test_view_validation_and_staff_scope_are_preserved(
    owner, queue_states, setup_product
):
    pid, records = queue_states
    foreign, _ = setup_product()
    assert (
        owner.get(
            "/api/manage/jobs", params={"product_id": pid, "view": "unknown"}
        ).status_code
        == 422
    )
    link = owner.post(
        "/api/admin/staff",
        json={"product_id": pid, "name": "队列查看", "permissions": ["queue.view"]},
    ).json()
    client = TestClient(
        app,
        base_url="http://localhost:8000",
        headers={"Origin": "http://localhost:8000"},
    )
    assert (
        client.post(
            "/api/staff/login", json={"token": link["url"].split("#", 1)[1]}
        ).status_code
        == 200
    )
    assert listed(client, pid, view="processed") == {
        key: records[key] for key in ("succeeded", "destroyed")
    }
    assert (
        client.get(
            "/api/manage/jobs",
            params={
                "product_id": foreign,
                "view": "all",
                "job_id": records["succeeded"],
            },
        ).status_code
        == 403
    )
    client.cookies.clear()
    assert (
        client.get(
            "/api/manage/jobs", params={"product_id": pid, "view": "all"}
        ).status_code
        == 401
    )

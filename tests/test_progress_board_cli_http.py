"""Real signed devices read progress without touching task bodies."""

import json

import pytest
from test_processor_profiles import merchant
from test_product_lifecycle_cli_http import actual_cli as actual_cli
from test_redemption import redeem

from extore.db import db
from extore.manage_client import ManageError


@pytest.fixture(params=("root", "shop"))
def owner(client, request):
    from starlette.responses import Response

    from extore.security import create_session

    if request.param == "shop":
        store, _ = merchant()
        try:
            yield store
        finally:
            store.close()
    else:
        with db() as c:
            cookie = create_session(c, Response(), "admin")
        client.cookies.set("extore_session", cookie)
        yield client


def product_shop(pid):
    with db() as c:
        return c.execute("SELECT shop_id FROM products WHERE id=?", (pid,)).fetchone()[
            0
        ]


def synthetic_task(owner, setup_product, name):
    pid, code = setup_product(
        name=name,
        progress_steps=[
            {"id": "private_step", "label": {"zh-CN": "PRIVATE-STEP-TEXT"}}
        ],
    )
    _, job = redeem(owner, code)
    with db() as c:
        c.execute(
            "UPDATE jobs SET message=?,content=?,params=?,result_json=?,progress=37 WHERE id=?",
            (
                "PRIVATE-MESSAGE",
                "PRIVATE-DELIVERY",
                json.dumps({"email": "PRIVATE-CUSTOMER"}),
                json.dumps({"result": "PRIVATE-RESULT"}),
                job["id"],
            ),
        )
    return pid, job["id"]


def assert_private_board(value):
    text = json.dumps(value)
    for marker in (
        "PRIVATE-STEP-TEXT",
        "private_step",
        "PRIVATE-MESSAGE",
        "PRIVATE-DELIVERY",
        "PRIVATE-CUSTOMER",
        "PRIVATE-RESULT",
    ):
        assert marker not in text


def test_real_owner_board_is_scoped_paginated_and_keeps_task_data(
    owner, setup_product, actual_cli, request
):
    admin, _, _, records = actual_cli
    pids = [synthetic_task(owner, setup_product, f"Pipeline {n}")[0] for n in range(2)]
    sid = product_shop(pids[0])
    foreign, _ = merchant()
    try:
        response = foreign.post(
            "/api/admin/products", json={"name": "FOREIGN-PRIVATE-PRODUCT"}
        )
        assert response.status_code == 200, response.text
        other_id = response.json()["id"]
        root = request.node.callspec.params["owner"] == "root"
        scope = ("--shop", sid) if root else ()
        if root:
            before = len(records)
            with pytest.raises(ManageError):
                admin("board")
            assert len(records) == before
        result = admin("board", *scope, "--limit", "1")
        assert result["ok"] is True
        board = result["board"]
        assert board["shop"]["id"] == sid
        assert board["totals"]["queued"] == 2
        assert board["pagination"] == {
            "limit": 1,
            "offset": 0,
            "total": 2,
            "has_more": True,
        }
        assert_private_board(result)
        assert "FOREIGN-PRIVATE-PRODUCT" not in json.dumps(result)
        assert other_id not in json.dumps(result)
        reads = [r for r in records if r["path"] == "/api/manage/progress-board"]
        assert len(reads) == 1 and reads[0]["method"] == "GET"
        assert not any(r["path"] == "/api/manage/jobs" for r in records)
        second = admin("board", *scope, "--limit", "1", "--offset", "1")["board"]
        first_jobs = {j["id"] for p in board["products"] for j in p["jobs"]}
        second_jobs = {j["id"] for p in second["products"] for j in p["jobs"]}
        assert len(first_jobs) == len(second_jobs) == 1 and first_jobs.isdisjoint(
            second_jobs
        )
        if not root:
            with pytest.raises(ManageError):
                admin("board", "--shop", product_shop(other_id))
        with db() as c:
            assert {r["message"] for r in c.execute("SELECT message FROM jobs")} == {
                "PRIVATE-MESSAGE"
            }
    finally:
        foreign.close()


def test_real_monitor_only_devices_aggregate_products_without_queue_access(
    owner, setup_product, actual_cli
):
    _, manage, bind, records = actual_cli
    pids = [synthetic_task(owner, setup_product, f"Observed {n}")[0] for n in range(2)]
    for pid in pids:
        bind(pid, ["queue.monitor"])
    result = manage("board")
    assert result["ok"] is True
    assert {item["product_id"] for item in result["boards"]} == set(pids)
    assert_private_board(result)
    reads = [r for r in records if r["path"] == "/api/manage/progress-board"]
    assert len(reads) == 2 and {r["query"]["product_id"] for r in reads} == set(pids)
    assert not any(r["path"] == "/api/manage/jobs" for r in records)
    before = len(records)
    with pytest.raises(ManageError):
        manage("jobs", "--product", pids[0])
    assert not any(r["path"] == "/api/manage/jobs" for r in records[before:])
    selected = manage("board", "--product", pids[0])
    assert len(selected["boards"]) == 1
    assert selected["boards"][0]["product_id"] == pids[0]

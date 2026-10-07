"""Progress-only projections cannot read task payloads or gain queue authority."""

import json
import time
import uuid
from pathlib import Path

import pytest
from test_processor_profiles import merchant
from test_product_links import create_link, login_link

from extore import pipeline_scopes, secret_store, service
from extore.db import db
from extore.models import LINK_PERMISSIONS, ProductLinkInput
from extore.progress_board import STATES

BOARD = "/api/manage/progress-board"
PRIVATE = "PRIVATE_BOARD_PAYLOAD_SENTINEL"


def create(owner, name="可见商品"):
    response = owner.post("/api/admin/products", json={"name": name})
    assert response.status_code == 200, response.text
    pid = response.json()["id"]
    with db() as c:
        sid = c.execute("SELECT shop_id FROM products WHERE id=?", (pid,)).fetchone()[0]
    return pid, sid


def seed(
    pid,
    state="queued",
    actor=None,
    *,
    created=100,
    updated=200,
    plan=None,
    completed=None,
):
    card, jid = str(uuid.uuid4()), str(uuid.uuid4())
    with db() as c:
        c.execute(
            "INSERT INTO cards(id,digest,product_id,created) VALUES (?,?,?,?)",
            (card, str(uuid.uuid4()), pid, created),
        )
        c.execute(
            "INSERT INTO jobs(id,card_id,product_id,state,params,content,message,result_json,schema_snapshot,progress_plan,completed_steps,progress,attempt,claimed_by,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                jid,
                card,
                pid,
                state,
                json.dumps({"input": PRIVATE}),
                PRIVATE,
                PRIVATE,
                json.dumps({"output": PRIVATE}),
                json.dumps({"label": PRIVATE}),
                json.dumps(plan) if plan is not None else None,
                json.dumps(completed or []),
                50,
                1,
                actor,
                created,
                updated,
            ),
        )
    return jid


def snapshot():
    with db() as c:
        return {
            table: [tuple(row) for row in c.execute("SELECT * FROM " + table)]
            for table in ("products", "jobs", "task_flow_runs", "task_flow_steps")
        }


def test_private_projection_counts_worker_kinds_and_read_only(owner, monkeypatch):
    pid, sid = create(owner)
    staff = create_link(owner, pid, ["queue.monitor"], name="可见处理人员")
    actor = staff["id"]
    with db() as c:
        c.execute(
            "INSERT INTO cli_devices(id,staff_id,public_key,fingerprint,client_name,created,last_seen) VALUES (?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                actor,
                PRIVATE,
                PRIVATE,
                PRIVATE,
                time.time(),
                time.time(),
            ),
        )
    plan = [
        {"id": "private_step_a", "label": {"en": PRIVATE}},
        {"id": "private_step_b", "label": {"en": PRIVATE}},
    ]
    processing = seed(pid, "processing", actor, plan=plan, completed=["private_step_a"])
    seed(pid, "succeeded", actor)
    seed(pid, "processing", "worker")
    seed(pid, "processing", "owner")
    seed(pid, "processing", "secret-worker@example.invalid")
    legacy = seed(pid)
    before = snapshot()

    def forbidden(*args, **kwargs):
        raise AssertionError("A progress board must not read details/decrypt/freeze")

    for name in ("product", "job_view", "progress_snapshot"):
        monkeypatch.setattr(service, name, forbidden)
    monkeypatch.setattr(secret_store, "open_secret", forbidden)
    response = owner.get(BOARD, params={"shop_id": sid})
    assert response.status_code == 200, response.text
    value = response.json()
    assert PRIVATE not in response.text
    assert "private_step_" not in response.text
    assert "secret-worker@example.invalid" not in response.text
    assert actor not in response.text
    assert snapshot() == before
    assert value["schema"] == "extore.progress-board.v1"
    assert value["totals"] == {
        **dict.fromkeys(STATES, 0),
        "processing": 4,
        "queued": 1,
        "succeeded": 1,
    }
    jobs = {j["id"]: j for j in value["products"][0]["jobs"]}
    assert jobs[processing]["steps"] == [
        {"position": 1, "state": "done"},
        {"position": 2, "state": "current"},
    ]
    assert jobs[processing]["completed_step_count"] == 1
    assert jobs[legacy]["steps"] == []
    workers = {w["name"]: w for w in value["workers"]}
    assert (
        workers["可见处理人员"]["kind"] == "unknown"
    )  # A CLI binding never proves how the task was claimed.
    assert workers["可见处理人员"]["completed_jobs"] == 1
    assert workers["商品处理器"]["kind"] == "automatic"
    assert workers["店主"]["kind"] == "merchant"
    assert workers["处理人员"]["kind"] == "unknown"
    assert all(len(w["id"]) == 64 and w["active_jobs"] == 1 for w in workers.values())
    # A representative fixture contains only this already validated projection.
    Path(
        "/home/lightjunction/.cache/extore-progress-board-20261007/progress-board-contract.json"
    ).write_text(json.dumps(value, ensure_ascii=False))


def test_monitor_only_cannot_get_details_process_or_list_attachments(owner):
    pid, _ = create(owner)
    jid = seed(pid)
    file_id = str(uuid.uuid4())
    with db() as c:
        card = c.execute("SELECT card_id FROM jobs WHERE id=?", (jid,)).fetchone()[0]
        c.execute(
            "INSERT INTO job_files(id,card_id,product_id,job_id,field_key,kind,filename,content_type,size,content,created) VALUES (?,?,?,?,'result','output',?,'text/plain',?,?,200)",
            (file_id, card, pid, jid, PRIVATE, len(PRIVATE), PRIVATE.encode()),
        )
    link = create_link(owner, pid, ["queue.monitor"])
    login_link(owner, link)
    response = owner.get(BOARD)
    assert response.status_code == 200, response.text
    assert response.json()["scope"] == {"product_ids": [pid]}
    assert owner.get("/api/manage/jobs").status_code == 403
    assert owner.get("/api/manage/product").status_code == 403
    assert owner.get("/api/manage/files/" + file_id + "/download").status_code == 403
    assert owner.get("/api/manage/files", params={"job_id": jid}).status_code == 403
    assert (
        owner.post(
            "/api/manage/batch",
            json={"product_id": pid, "ids": [jid], "action": "claim"},
        ).status_code
        == 403
    )
    with db() as c:
        assert (
            c.execute("SELECT state FROM jobs WHERE id=?", (jid,)).fetchone()[0]
            == "queued"
        )
        c.execute("UPDATE staff SET revoked=1 WHERE id=?", (link["id"],))
    assert owner.get(BOARD).status_code == 401


@pytest.mark.parametrize(
    "permissions,allowed",
    [(["queue.view"], True), (["cards.manage"], False), (["product.edit"], False)],
)
def test_existing_stronger_view_permission_is_accepted(owner, permissions, allowed):
    pid, _ = create(owner)
    link = create_link(owner, pid, permissions)
    login_link(owner, link)
    assert owner.get(BOARD).status_code == (200 if allowed else 403)


def test_strict_scope_root_requires_explicit_shop_and_tenant_cannot_widen(owner):
    first, sid_a = merchant()
    second, sid_b = merchant()
    try:
        a, _ = create(first)
        b, _ = create(second)
        seed(a)
        seed(b)
        assert owner.get(BOARD).status_code == 400
        assert (
            owner.get(BOARD, params={"shop_id": sid_a, "product_id": b}).status_code
            == 403
        )
        assert first.get(BOARD).json()["scope"] == {"product_ids": [a]}
        assert first.get(BOARD, params={"shop_id": sid_b}).status_code == 403
        assert first.get(BOARD, params={"product_id": b}).status_code == 403
        link = create_link(first, a, ["queue.monitor"])
        login_link(first, link)
        assert first.get(BOARD, params={"product_id": b}).status_code == 403
        assert first.get(BOARD, params={"shop_id": sid_b}).status_code == 403
    finally:
        first.close()
        second.close()


def test_full_state_counts_global_pagination_and_product_queue_position(owner):
    first, sid = create(owner, "一")
    second, _ = create(owner, "二")
    for state in STATES:
        seed(
            first,
            state,
            "old-completed-worker" if state == "succeeded" else None,
            created=100,
            updated=200,
        )
    last = seed(second, "queued", created=101)
    later = seed(second, "queued", created=102)
    first_page = owner.get(BOARD, params={"shop_id": sid, "limit": 2}).json()
    assert first_page["totals"] == {**dict.fromkeys(STATES, 1), "queued": 3}
    assert first_page["pagination"] == {
        "limit": 2,
        "offset": 0,
        "total": 7,
        "has_more": True,
    }
    assert len(first_page["products"]) == 2
    assert sum(len(p["jobs"]) for p in first_page["products"]) == 2
    assert (
        first_page["workers"] == []
    )  # Historical worker absent from both active work and this page.
    page = owner.get(BOARD, params={"shop_id": sid, "product_id": second}).json()
    rows = {j["id"]: j for j in page["products"][0]["jobs"]}
    assert rows[last]["queue_position"] == 1 and rows[later]["queue_position"] == 2
    assert page["scope"]["product_ids"] == [second]
    processed = owner.get(
        BOARD, params={"shop_id": sid, "view": "processed", "offset": 1, "limit": 1}
    ).json()
    assert processed["totals"] == first_page["totals"]
    assert processed["pagination"] == {
        "limit": 1,
        "offset": 1,
        "total": 3,
        "has_more": True,
    }
    after = owner.get(BOARD, params={"shop_id": sid, "offset": 200}).json()
    assert after["pagination"]["has_more"] is False
    assert all(p["jobs"] == [] for p in after["products"])


def test_flow_projection_uses_generic_activation_steps_without_decryption(owner):
    pid, sid = create(owner)
    jid = seed(pid, "processing", "worker")
    with db() as c:
        c.execute(
            "INSERT INTO task_flow_runs(job_id,attempt,flow_epoch,node_id,phase,created,updated) VALUES (?,1,2,?,'processing',100,200)",
            (jid, PRIVATE),
        )
        for epoch, state in ((1, "completed"), (2, "active"), (3, "cancelled")):
            c.execute(
                "INSERT INTO task_flow_steps(job_id,flow_epoch,attempt,node_id,kind,state,action_id,entered_at,payload_ciphertext,result_digest) VALUES (?,?,1,?,'process',?,?,100,?,?)",
                (jid, epoch, PRIVATE, state, PRIVATE, PRIVATE, PRIVATE),
            )
    before = snapshot()
    value = owner.get(BOARD, params={"shop_id": sid}).json()
    assert PRIVATE not in json.dumps(value)
    row = value["products"][0]["jobs"][0]
    assert row["flow_phase"] == "processing"
    assert row["steps"] == [
        {"position": 1, "state": "done"},
        {"position": 2, "state": "current"},
        {"position": 3, "state": "pending"},
    ]
    assert row["step_count"] == 3 and row["completed_step_count"] == 1
    assert snapshot() == before


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 201},
        {"offset": -1},
        {"offset": 1000001},
        {"view": "all"},
        {"shop_id": "x" * 101},
    ],
)
def test_query_bounds_are_explicit_rejection(owner, params):
    assert owner.get(BOARD, params=params).status_code == 422


def test_monitor_scope_is_optional_and_does_not_expand_old_defaults():
    assert LINK_PERMISSIONS[:10][-2:] == ("product.delete", "product.purge")
    assert LINK_PERMISSIONS[-1] == "queue.monitor"
    assert pipeline_scopes.PIPELINE_PERMISSIONS == (
        "queue.view",
        "queue.process",
        "queue.retry",
    )
    assert pipeline_scopes._permissions(["queue.monitor"], "shop.pipeline") == [
        "queue.monitor"
    ]
    assert ProductLinkInput(
        name="Monitor", permissions=["queue.monitor"]
    ).permissions == ["queue.monitor"]


def test_root_disabled_shop_is_unavailable(owner):
    pid, sid = create(owner)
    seed(pid)
    with db() as c:
        c.execute("UPDATE shops SET enabled=0 WHERE id=?", (sid,))
    assert owner.get(BOARD, params={"shop_id": sid}).status_code == 401


def test_plain_frozen_steps_determine_progress_without_mutation(owner):
    pid, sid = create(owner)
    plan = [
        {"id": "a", "label": {"en": PRIVATE}},
        {"id": "b", "label": {"en": PRIVATE}},
    ]
    jid = seed(pid, "processing", plan=plan, completed=["a"])
    finished = seed(pid, "succeeded", plan=plan)
    with db() as c:
        c.execute("UPDATE jobs SET progress=99 WHERE id=?", (jid,))
    before = snapshot()
    rows = owner.get(BOARD, params={"shop_id": sid}).json()["products"][0]["jobs"]
    assert rows[0]["progress"] == 50
    done = owner.get(BOARD, params={"shop_id": sid, "view": "processed"}).json()[
        "products"
    ][0]["jobs"][0]
    assert done["id"] == finished and done["progress"] == 100
    assert snapshot() == before


@pytest.mark.parametrize("bound", ["products", "workers", "steps"])
def test_safety_caps_reject_instead_of_silently_truncating(owner, monkeypatch, bound):
    from extore import progress_board

    pid, sid = create(owner)
    if bound == "products":
        monkeypatch.setattr(progress_board, "MAX_PRODUCTS", 0)
    elif bound == "workers":
        seed(pid, "processing", "worker")
        monkeypatch.setattr(progress_board, "MAX_WORKERS", 0)
    else:
        seed(pid, plan=[{"id": str(i), "label": {"en": PRIVATE}} for i in range(31)])
    response = owner.get(BOARD, params={"shop_id": sid})
    assert response.status_code == 409
    assert PRIVATE not in response.text


@pytest.mark.parametrize(
    "column,value",
    [
        ("created", float("inf")),
        ("updated", "malformed-private-timestamp"),
        ("progress", 101),
        ("progress", "malformed-private-progress"),
        ("attempt", 0),
    ],
)
def test_bad_metadata_rejects_without_fabricating_fresh_progress(owner, column, value):
    pid, sid = create(owner)
    jid = seed(pid, "processing", "worker")
    with db() as c:
        c.execute("UPDATE jobs SET " + column + "=? WHERE id=?", (value, jid))
    response = owner.get(BOARD, params={"shop_id": sid})
    assert response.status_code == 409
    assert "malformed-private" not in response.text


def test_invalid_historical_worker_timestamp_stays_unknown(owner):
    pid, sid = create(owner)
    seed(pid, "processing", "worker", updated=200)
    past = seed(pid, "succeeded", "worker", updated=300)
    with db() as c:
        c.execute(
            "UPDATE jobs SET updated='malformed-private-time' WHERE id=?", (past,)
        )
    response = owner.get(BOARD, params={"shop_id": sid})
    assert response.status_code == 200, response.text
    assert response.json()["workers"][0]["last_update"] == 200

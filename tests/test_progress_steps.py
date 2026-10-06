import io
import json
import time
import uuid

import pytest
from pydantic import ValidationError
from test_product_links import create_link, login_link

from extore.db import db
from extore.models import BatchUpdate, JobUpdate, Product
from extore.sdk.script import Result, Task, run
from extore.security import sign

STEPS = [
    {"id": "confirm", "label": {"zh-CN": "确认信息", "en": "Confirm details"}},
    {"id": "prepare", "label": {"zh-CN": "准备商品", "en": "Prepare"}},
    {"id": "deliver", "label": {"zh-CN": "完成交付", "en": "Deliver"}},
]


def create(owner, steps=STEPS, **values):
    response = owner.post(
        "/api/admin/products",
        json={
            "name": "分步骤办理",
            "delivery": "service",
            "progress_steps": steps,
            **values,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def launch(owner, product):
    issued = owner.post("/api/admin/cards", json={"product_id": product["id"]})
    assert issued.status_code == 200, issued.text
    exchanged = owner.post("/api/exchange", json={"code": issued.json()["codes"][0]})
    assert exchanged.status_code == 200, exchanged.text
    token = exchanged.json()["token"]
    response = owner.post("/api/redeem", json={"token": token, "params": {}})
    assert response.status_code == 200, response.text
    return token, response.json()


def batch(owner, product, row, action, **values):
    return owner.post(
        "/api/manage/batch",
        json={
            "product_id": product["id"],
            "ids": [row["id"]],
            "action": action,
            **values,
        },
    )


def receipt(owner, token):
    response = owner.post("/api/receipt", json={"token": token})
    assert response.status_code == 200, response.text
    return response.json()["job"]


def edit(owner, product, **values):
    response = owner.put(
        f"/api/admin/products/{product['id']}", json={**product, **values}
    )
    assert response.status_code == 200, response.text
    return response.json()


def state_snapshot():
    with db() as c:
        return {
            table: [dict(row) for row in c.execute(f"SELECT * FROM {table}")]
            for table in ("jobs", "events", "audit")
        }


@pytest.mark.parametrize(
    "steps",
    [
        [STEPS[0], STEPS[0]],
        [{"id": "Bad-ID", "label": {"en": "Step"}}],
        [{"id": "a" * 41, "label": {"en": "Step"}}],
        [{"id": "good", "label": {}}],
        [{"id": "good", "label": {"en": " "}}],
        [{"id": "good", "label": {"en": "x" * 201}}],
        [{"id": "good", "label": {" ": "Step"}}],
        [{"id": "good", "label": {"x" * 41: "Step"}}],
        [{"id": "good", "label": {f"locale{i}": "Step" for i in range(21)}}],
        [{"id": f"step{i}", "label": {"en": "Step"}} for i in range(31)],
    ],
)
def test_invalid_product_plans(steps):
    with pytest.raises(ValidationError):
        Product(name="商品", progress_steps=steps)


@pytest.mark.parametrize(
    "email",
    [
        "plain",
        "x@localhost",
        "x@@example.com",
        "a..b@example.com",
        "x@-bad.com",
        "a b@example.com",
    ],
)
def test_invalid_support_email(email):
    with pytest.raises(ValidationError):
        Product(name="商品", support_email=email)


def test_product_defaults_and_support_email_normalization():
    product = Product(name="商品", support_email="  support+shop@example.com  ")
    assert product.support_email == "support+shop@example.com"
    assert product.progress_steps == []
    assert Product(name="商品", support_email=" ").support_email == ""


@pytest.mark.parametrize("completed", [["confirm", "confirm"], ["bad ID"], ["x" * 41]])
def test_completed_steps_are_unique_valid_ids(completed):
    with pytest.raises(ValidationError):
        JobUpdate(state="processing", attempt=1, completed_steps=completed)


def test_retry_request_does_not_silently_change_step_plan():
    with pytest.raises(ValidationError):
        BatchUpdate(
            product_id="product", ids=["job"], action="retry", progress_steps=STEPS
        )


def test_customer_progress_uses_completed_steps_and_persists_message(owner):
    product = create(owner, support_email="support@example.com")
    token, row = launch(owner, product)
    assert row["queue_position"] == 1 and row["queue_ahead"] == 0
    assert row["completed_steps"] == []
    assert row["steps"] == [{**step, "done": False} for step in STEPS]
    assert row["support_email"] == "support@example.com"
    assert batch(owner, product, row, "claim").status_code == 200
    assert (
        batch(
            owner,
            product,
            row,
            "progress",
            completed_steps=["confirm"],
            progress=99,
            message="已核实资料，正在准备。",
        ).status_code
        == 200
    )
    updated = receipt(owner, token)
    assert updated["progress"] == 33
    assert updated["message"] == "已核实资料，正在准备。"
    assert [step["done"] for step in updated["steps"]] == [True, False, False]
    assert (
        batch(owner, product, row, "progress", message="正在准备。").status_code == 200
    )
    assert receipt(owner, token)["completed_steps"] == ["confirm"]
    assert receipt(owner, token)["progress"] == 33


def test_steps_cannot_be_unknown_or_retracted_atomically(owner):
    product = create(owner)
    token, row = launch(owner, product)
    assert (
        batch(owner, product, row, "claim", completed_steps=["confirm"]).status_code
        == 200
    )
    before = state_snapshot()
    for completed, status in [(["confirm", "unknown"], 400), ([], 409)]:
        response = batch(owner, product, row, "progress", completed_steps=completed)
        assert response.status_code == status, response.text
        assert state_snapshot() == before
    assert receipt(owner, token)["progress"] == 33


def test_full_plan_caps_processing_at_99_and_success_completes_all(owner):
    product = create(owner)
    token, row = launch(owner, product)
    assert batch(owner, product, row, "claim").status_code == 200
    assert (
        batch(
            owner,
            product,
            row,
            "progress",
            completed_steps=[step["id"] for step in STEPS],
        ).status_code
        == 200
    )
    assert receipt(owner, token)["progress"] == 99
    assert (
        batch(owner, product, row, "succeed", message="办理完成。").status_code == 200
    )
    done = receipt(owner, token)
    assert done["progress"] == 100 and done["queue_position"] == 0
    assert all(step["done"] for step in done["steps"])


def test_success_without_explicit_steps_marks_entire_plan_complete(owner):
    product = create(owner)
    token, row = launch(owner, product)
    assert batch(owner, product, row, "claim").status_code == 200
    assert batch(owner, product, row, "succeed").status_code == 200
    assert receipt(owner, token)["completed_steps"] == [step["id"] for step in STEPS]


def test_new_attempt_clears_completion_but_keeps_original_plan(owner):
    product = create(owner)
    token, row = launch(owner, product)
    assert (
        batch(owner, product, row, "claim", completed_steps=["confirm"]).status_code
        == 200
    )
    assert (
        batch(
            owner, product, row, "fail", retryable=True, message="暂时失败。"
        ).status_code
        == 200
    )
    edit(owner, product, progress_steps=[{"id": "replacement", "label": {"en": "New"}}])
    response = owner.post("/api/redeem", json={"token": token, "params": {}})
    assert response.status_code == 200, response.text
    retry = response.json()
    assert retry["attempt"] == 2 and retry["progress"] == 0
    assert retry["completed_steps"] == []
    assert [step["id"] for step in retry["steps"]] == [step["id"] for step in STEPS]


def test_product_plan_changes_only_affect_new_jobs(owner):
    product = create(owner)
    first_token, _ = launch(owner, product)
    new_plan = [{"id": "next", "label": {"en": "Next plan"}}]
    updated = edit(owner, product, progress_steps=new_plan)
    second_token, _ = launch(owner, updated)
    assert receipt(owner, first_token)["steps"] == [
        {**step, "done": False} for step in STEPS
    ]
    assert receipt(owner, second_token)["steps"] == [{**new_plan[0], "done": False}]


def test_product_edit_freezes_unbound_legacy_jobs_before_replacing_plan(owner):
    product = create(owner)
    token, row = launch(owner, product)
    with db() as c:
        c.execute("UPDATE jobs SET progress_plan=NULL WHERE id=?", (row["id"],))
    edit(owner, product, progress_steps=[{"id": "new", "label": {"en": "New"}}])
    assert receipt(owner, token)["steps"] == [{**step, "done": False} for step in STEPS]


def test_first_read_binds_legacy_plan_once(owner):
    product = create(owner)
    token, row = launch(owner, product)
    with db() as c:
        c.execute("UPDATE jobs SET progress_plan=NULL WHERE id=?", (row["id"],))
    receipt(owner, token)
    with db() as c:
        saved = c.execute(
            "SELECT progress_plan FROM jobs WHERE id=?", (row["id"],)
        ).fetchone()[0]
    assert json.loads(saved) == STEPS


def test_empty_job_plan_can_be_explicitly_bootstrapped_once(owner):
    product = create(owner, steps=[])
    token, row = launch(owner, product)
    untouched_token, _ = launch(owner, product)
    assert (
        batch(
            owner,
            product,
            row,
            "claim",
            progress_steps=STEPS,
            completed_steps=["confirm"],
        ).status_code
        == 200
    )
    assert receipt(owner, token)["progress"] == 33
    assert receipt(owner, untouched_token)["steps"] == []
    assert owner.get("/api/admin/products").json()[0]["progress_steps"] == []
    before = state_snapshot()
    assert (
        batch(owner, product, row, "progress", progress_steps=STEPS).status_code == 409
    )
    assert state_snapshot() == before


def test_bootstrap_multiple_jobs_is_atomic_if_one_already_has_plan(owner):
    product = create(owner, steps=[])
    _, bound = launch(owner, product)
    _, empty = launch(owner, product)
    assert (
        batch(owner, product, bound, "claim", progress_steps=STEPS).status_code == 200
    )
    before = state_snapshot()
    response = owner.post(
        "/api/manage/batch",
        json={
            "product_id": product["id"],
            "ids": [empty["id"], bound["id"]],
            "action": "claim",
            "progress_steps": STEPS,
        },
    )
    assert response.status_code == 409, response.text
    assert state_snapshot() == before


@pytest.mark.parametrize("permissions", [["queue.view"], ["queue.view", "queue.retry"]])
def test_read_or_retry_link_cannot_bootstrap_or_complete_steps(owner, permissions):
    product = create(owner, steps=[])
    _, row = launch(owner, product)
    link = create_link(owner, product["id"], permissions=permissions)
    login_link(owner, link)
    assert batch(owner, product, row, "claim", progress_steps=STEPS).status_code == 403


def test_queue_position_includes_processing_and_uses_product_and_id_tiebreak(owner):
    product = create(owner)
    tokens_and_rows = [launch(owner, product) for _ in range(3)]
    other = create(owner)
    _, other_row = launch(owner, other)
    with db() as c:
        c.execute("UPDATE jobs SET created=10 WHERE product_id=?", (product["id"],))
        c.execute("UPDATE jobs SET created=0 WHERE id=?", (other_row["id"],))
    ordered = sorted(tokens_and_rows, key=lambda pair: pair[1]["id"])
    assert batch(owner, product, ordered[0][1], "claim").status_code == 200
    assert [receipt(owner, token)["queue_position"] for token, _ in ordered] == [
        1,
        2,
        3,
    ]
    listed = owner.get("/api/manage/jobs", params={"product_id": product["id"]}).json()
    assert [row["id"] for row in listed] == [row["id"] for _, row in ordered]
    assert batch(owner, product, ordered[0][1], "succeed").status_code == 200
    assert [receipt(owner, token)["queue_position"] for token, _ in ordered] == [
        0,
        1,
        2,
    ]


def test_legacy_percentage_progress_remains_available_without_steps(owner):
    product = create(owner, steps=[])
    token, row = launch(owner, product)
    assert batch(owner, product, row, "claim").status_code == 200
    assert batch(owner, product, row, "progress", progress=42).status_code == 200
    assert receipt(owner, token)["progress"] == 42
    assert batch(owner, product, row, "progress", progress=41).status_code == 409


def test_event_progress_contains_plan_snapshot_and_done_flags(owner):
    product = create(owner)
    _, row = launch(owner, product)
    assert (
        batch(owner, product, row, "claim", completed_steps=["confirm"]).status_code
        == 200
    )
    assert batch(owner, product, row, "succeed").status_code == 200
    with db() as c:
        events = [
            json.loads(r["payload"])
            for r in c.execute(
                "SELECT payload FROM events WHERE job_id=? ORDER BY created",
                (row["id"],),
            )
        ]
    assert events[0]["data"]["steps"] == [{**step, "done": False} for step in STEPS]
    assert events[-1]["data"]["completed_steps"] == [step["id"] for step in STEPS]
    assert all(step["done"] for step in events[-1]["data"]["steps"])


def test_signed_callbacks_share_step_validation_and_auto_completion(owner):
    secret = "step-webhook-signing-secret-at-least-32-chars"
    product = create(
        owner,
        mode="webhook",
        webhook_url="https://example.com/hook",
        webhook_secret=secret,
    )
    token, row = launch(owner, product)

    def callback(**values):
        body = json.dumps({"state": "processing", "attempt": 1, **values}).encode()
        ts, nonce = str(int(time.time())), uuid.uuid4().hex
        return owner.post(
            f"/api/callbacks/{product['id']}/{row['id']}",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Extore-Timestamp": ts,
                "X-Extore-Nonce": nonce,
                "X-Extore-Signature": sign(secret, ts, nonce, body),
            },
        )

    assert callback(completed_steps=["confirm"], progress=99).status_code == 200
    assert receipt(owner, token)["progress"] == 33
    assert callback(completed_steps=[]).status_code == 409
    assert callback(completed_steps=["unknown"]).status_code == 400
    assert callback(state="succeeded").status_code == 200
    assert receipt(owner, token)["progress"] == 100


def test_sdk_steps_only_progress_and_result(capsys, monkeypatch):
    task = Task("job", "product", 1, {}, steps=STEPS)
    task.progress(message="Confirmed", completed_steps=["confirm"])
    assert json.loads(capsys.readouterr().out) == {
        "kind": "progress",
        "message": "Confirmed",
        "completed_steps": ["confirm"],
    }
    task.progress(25, "Legacy percentage")
    assert json.loads(capsys.readouterr().out)["progress"] == 25
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "id": "job",
                    "product_id": "product",
                    "attempt": 1,
                    "params": {},
                    "steps": STEPS,
                    "completed_steps": ["confirm"],
                }
            )
        ),
    )
    run(lambda received: Result.success(message=received.steps[0]["label"]["en"]))
    assert json.loads(capsys.readouterr().out)["message"] == "Confirm details"

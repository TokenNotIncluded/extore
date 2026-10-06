import io
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from extore.sdk.script import Result, ShopContext, Task, run

PLAN = [
    {"id": "validate", "label": {"zh-CN": "核对信息", "en": "Validate"}},
    {"id": "deliver", "label": {"zh-CN": "生成交付", "en": "Deliver"}},
]
ROOT = Path(__file__).resolve().parents[1]


def test_sdk_defines_frozen_ordered_plan_then_reports_completed_steps(capsys):
    task = Task("task", "product", 1, {})
    plan = json.loads(json.dumps(PLAN))
    task.define_steps(plan, "Preparing")
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "kind": "progress",
        "progress_steps": PLAN,
        "progress": 0,
        "completed_steps": [],
        "message": "Preparing",
    }
    plan[0]["label"]["en"] = "changed"
    assert task.steps[0]["label"]["en"] == "Validate"
    with pytest.raises(TypeError):
        task.steps[0]["label"]["en"] = "changed"
    with pytest.raises(FrozenInstanceError):
        task.steps = []
    task.progress(message="Validated", completed_steps=["validate"])
    assert json.loads(capsys.readouterr().out)["completed_steps"] == ["validate"]
    task.progress(99, "Ready", completed_steps=["deliver", "validate"])
    assert json.loads(capsys.readouterr().out)["completed_steps"] == [
        "validate",
        "deliver",
    ]
    assert task.completed_steps == ("validate", "deliver")


def test_saved_sdk_plan_cannot_be_redefined_even_with_same_plan(capsys):
    task = Task("task", "product", 1, {}, steps=PLAN)
    with pytest.raises(ValueError, match="frozen"):
        task.define_steps(PLAN)
    assert not capsys.readouterr().out
    empty = Task("task", "product", 1, {})
    empty.define_steps(PLAN)
    capsys.readouterr()
    with pytest.raises(ValueError, match="frozen"):
        empty.define_steps(PLAN)
    assert not capsys.readouterr().out


@pytest.mark.parametrize(
    "plan",
    [
        [],
        [PLAN[0], PLAN[0]],
        [{"id": "Bad ID", "label": {"en": "Step"}}],
        [{"id": "a" * 41, "label": {"en": "Step"}}],
        [{"id": "valid", "label": {}}],
        [{"id": "valid", "label": {"en": " "}}],
        [{"id": "valid", "label": {"en": "x" * 201}}],
        [{"id": "valid", "label": {" ": "Step"}}],
        [{"id": "valid", "label": {"x" * 41: "Step"}}],
        [{"id": "valid", "label": {str(i): "Step" for i in range(21)}}],
        [{"id": str(i), "label": {"en": "Step"}} for i in range(31)],
        [{"id": "valid", "label": {"en": "Step"}, "private": "secret"}],
    ],
)
def test_invalid_sdk_plan_emits_nothing(plan, capsys):
    task = Task("task", "product", 1, {})
    with pytest.raises(ValueError):
        task.define_steps(plan)
    assert not task.steps and not capsys.readouterr().out


@pytest.mark.parametrize(
    "completed", [["unknown"], ["validate", "validate"], [], ["Bad ID"]]
)
def test_sdk_completed_ids_are_scoped_unique_and_monotonic(completed, capsys):
    task = Task("task", "product", 1, {}, steps=PLAN, completed_steps=["validate"])
    with pytest.raises(ValueError):
        task.progress(completed_steps=completed)
    assert task.completed_steps == ("validate",) and not capsys.readouterr().out


@pytest.mark.parametrize("percent", [-1, 100, 0.5, True, "50"])
def test_sdk_progress_rejects_invalid_percent_without_output(percent, capsys):
    with pytest.raises(ValueError):
        Task("task", "product", 1, {}).progress(percent)
    assert not capsys.readouterr().out


def test_sdk_shop_context_is_frozen_and_separate_from_customer_inputs():
    task = Task(
        "task",
        "product",
        1,
        {"shop_id": "customer-spoof"},
        shop_context={"shop_id": "shop-a", "profile_id": "profile-a", "revision": 2},
    )
    assert task.shop_context == ShopContext("shop-a", "profile-a", 2)
    assert task.params["shop_id"] == "customer-spoof"
    with pytest.raises(FrozenInstanceError):
        task.shop_context.shop_id = "shop-b"
    with pytest.raises(ValueError):
        Task(
            "task",
            "product",
            1,
            {},
            shop_context={"shop_id": "shop-a", "payment_token": "private"},
        )
    with pytest.raises(ValueError):
        ShopContext(revision=True)


def test_sdk_environment_is_private_readonly_and_separate_from_customer_input():
    values = {"LOCALE": "zh-CN", "API_TOKEN": "private-workflow-token"}
    task = Task(
        "task",
        "product",
        1,
        {"API_TOKEN": "customer-spoof", "environment": "customer-value"},
        environment=values,
    )
    values["API_TOKEN"] = "changed-after-construction"
    assert task.environment["API_TOKEN"] == "private-workflow-token"
    assert task.params["API_TOKEN"] == "customer-spoof"
    assert task.params["environment"] == "customer-value"
    assert "private-workflow-token" not in repr(task)
    assert "API_TOKEN': 'private-workflow-token" not in repr(task)
    with pytest.raises(TypeError):
        task.environment["API_TOKEN"] = "replaced"
    with pytest.raises(FrozenInstanceError):
        task.environment = {}
    assert Task("task", "product", 1, {}).environment == {}


@pytest.mark.parametrize(
    "values",
    [
        None,
        [],
        {"lowercase": "secret-not-in-error"},
        {"BAD-NAME": "secret-not-in-error"},
        {"A" * 65: "secret-not-in-error"},
        {"API_TOKEN": None},
        {"API_TOKEN": {"nested": "secret-not-in-error"}},
        {"API_TOKEN": "secret-not-in-error\x00"},
        {"API_TOKEN": "secret-not-in-error\ud800"},
        {"API_TOKEN": "x" * 8193},
        {"API_TOKEN": "文" * 2731},
        {f"VALUE_{i}": "" for i in range(129)},
        {f"VALUE_{i}": "x" * 8192 for i in range(9)},
    ],
)
def test_invalid_sdk_environment_uses_safe_errors(values, capsys):
    with pytest.raises(ValueError) as error:
        Task("task", "product", 1, {}, environment=values)
    assert "secret-not-in-error" not in str(error.value)
    assert not capsys.readouterr().out


def test_sdk_environment_accepts_full_workflow_limits():
    values = {f"VALUE_{i}": "" for i in range(128)}
    values.update({f"VALUE_{i}": "x" * 8192 for i in range(8)})
    task = Task("task", "product", 1, {}, environment=values)
    assert len(task.environment) == 128
    assert (
        sum(len(value.encode("utf-8")) for value in task.environment.values()) == 65536
    )


def test_sdk_environment_is_available_to_handler_without_automatic_result_or_log_copy(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "id": "task",
                    "product_id": "product",
                    "attempt": 1,
                    "params": {"API_TOKEN": "customer-spoof"},
                    "environment": {"API_TOKEN": "private-workflow-token"},
                }
            )
        ),
    )

    def handler(task):
        assert task.environment["API_TOKEN"] == "private-workflow-token"
        assert task.params["API_TOKEN"] == "customer-spoof"
        task.progress(50, "Working")
        return Result.success("safe-delivery")

    run(handler)
    output = capsys.readouterr()
    assert not output.err
    assert "private-workflow-token" not in output.out
    lines = [json.loads(line) for line in output.out.splitlines()]
    assert [line["kind"] for line in lines] == ["progress", "result"]
    assert lines[-1]["state"] == "succeeded"


def test_sdk_combined_input_budget_includes_valid_workflow_environment(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "id": "task",
                    "product_id": "product",
                    "attempt": 1,
                    "params": {"text": "x" * 143000},
                    "environment": {f"VALUE_{i}": "s" * 8192 for i in range(8)},
                }
            )
        ),
    )
    run(lambda task: pytest.fail("oversized combined payload must not execute"))
    output = capsys.readouterr()
    assert not output.err
    assert json.loads(output.out)["state"] == "failed"
    assert "s" * 100 not in output.out


@pytest.mark.parametrize(
    "raw",
    [
        "not-json-private",
        json.dumps({"private": "secret-input"}),
        "[]",
        "{" + '"params":"secret-input"}',
        "[" * 2000 + "]" * 2000,
        "x" * 200001,
    ],
)
def test_sdk_bad_input_is_safe_failure_not_traceback(raw, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))
    run(lambda task: pytest.fail("invalid input must not reach the handler"))
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["state"] == "failed" and result["retryable"] is False
    assert (
        output.err == ""
        and "private" not in output.out
        and "secret-input" not in output.out
    )


def test_sdk_handler_exception_does_not_reveal_credentials(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {"id": "task", "product_id": "product", "attempt": 1, "params": {}}
            )
        ),
    )

    def fail(task):
        raise RuntimeError("secret-api-key")

    run(fail)
    result = capsys.readouterr()
    assert "secret-api-key" not in result.out and not result.err
    assert json.loads(result.out)["state"] == "failed"


def test_sdk_runs_with_stdlib_only_and_streams_define_progress_result():
    source = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
from extore.sdk import Task, Result, run
def handler(task):
    task.define_steps({PLAN!r}, "Preparing")
    task.progress(message="Validated", completed_steps=["validate"])
    return Result.success("synthetic-delivery", completed_steps=["validate", "deliver"])
run(handler)
"""
    process = subprocess.run(
        [sys.executable, "-S", "-c", source],
        input=json.dumps(
            {
                "id": "task",
                "product_id": "product",
                "attempt": 1,
                "params": {},
                "shop_context": {
                    "shop_id": "shop-a",
                    "profile_id": None,
                    "revision": None,
                },
            }
        ).encode(),
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert process.returncode == 0 and not process.stderr
    values = [json.loads(line) for line in process.stdout.splitlines()]
    assert [value["kind"] for value in values] == ["progress", "progress", "result"]
    assert values[0]["progress_steps"] == PLAN
    assert (
        values[-1]["state"] == "succeeded"
        and values[-1]["content"] == "synthetic-delivery"
    )


def test_sdk_unicode_stdin_budget_is_measured_in_bytes(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps({"params": {"text": "文" * 100000}}, ensure_ascii=False)
        ),
    )
    run(lambda task: Result.success("should-not-run"))
    assert json.loads(capsys.readouterr().out)["state"] == "failed"

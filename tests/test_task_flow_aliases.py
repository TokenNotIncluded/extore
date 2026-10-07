"""Aliases retain original values and attachment provenance through real flows."""

from copy import deepcopy

import pytest
from test_task_flow_core import answer_current, finish_current, setup
from test_task_flow_http_integration import (
    action,
    answer,
    batch,
    download,
    launch,
    receipt,
    state_snapshot,
)

from extore import task_flow
from extore.db import db
from extore.service import job
from extore.task_flow_definition import validate_definition


def field(key, kind="text", **values):
    return {"key": key, "type": kind, "label": {"en": key}, **values}


def reference(node, name):
    return {"node": node, "field": name}


def process(identity, inputs, outputs, following):
    return {
        "id": identity,
        "kind": "process",
        "inputs": inputs,
        "outputs": outputs,
        "next": following,
        "failure_next": "failed",
        "timeout_next": "failed",
    }


def graph(*, file_output=False):
    if file_output:
        return {
            "version": 1,
            "entry": "q1",
            "nodes": [
                {
                    "id": "q1",
                    "kind": "input",
                    "fields": [field("question"), field("source", "file")],
                    "next": "p1",
                },
                process(
                    "p1",
                    {
                        "question": reference("q1", "question"),
                        "source": reference("q1", "source"),
                    },
                    [field("draft", "file")],
                    "preview",
                ),
                {
                    "id": "preview",
                    "kind": "display",
                    "show_from": {"renamed_file": reference("p1", "draft")},
                    "next": "done",
                },
                {
                    "id": "export",
                    "kind": "end",
                    "state": "succeeded",
                    "result": {"artifact": reference("preview", "renamed_file")},
                },
                {
                    "id": "done",
                    "kind": "end",
                    "state": "succeeded",
                    "result": {"artifact": reference("export", "artifact")},
                },
                {"id": "failed", "kind": "end", "state": "failed"},
            ],
        }
    return {
        "version": 1,
        "entry": "q1",
        "nodes": [
            {
                "id": "q1",
                "kind": "input",
                "fields": [field("answer")],
                "next": "input_preview",
            },
            {
                "id": "input_preview",
                "kind": "display",
                "show_from": {"renamed_input": reference("q1", "answer")},
                "next": "p1",
            },
            process(
                "p1",
                {"answer": reference("input_preview", "renamed_input")},
                [field("answer")],
                "result_preview",
            ),
            {
                "id": "result_preview",
                "kind": "display",
                "show_from": {"renamed_answer": reference("p1", "answer")},
                "next": "p2",
            },
            {
                "id": "archive",
                "kind": "end",
                "state": "succeeded",
                "result": {"answer_1": reference("result_preview", "renamed_answer")},
            },
            process(
                "p2",
                {"answer": reference("archive", "answer_1")},
                [field("answer")],
                {
                    "cases": [
                        {
                            "when": {
                                "source": reference("archive", "answer_1"),
                                "op": "eq",
                                "value": "yes",
                            },
                            "to": "final_preview",
                        }
                    ],
                    "default": "failed",
                },
            ),
            {
                "id": "final_preview",
                "kind": "display",
                "show_from": {
                    "previous": reference("archive", "answer_1"),
                    "newest": reference("p2", "answer"),
                },
                "next": "done",
            },
            {
                "id": "export",
                "kind": "end",
                "state": "succeeded",
                "result": {"answer_1": reference("final_preview", "newest")},
            },
            {
                "id": "done",
                "kind": "end",
                "state": "succeeded",
                "result": {"answer_1": reference("export", "answer_1")},
            },
            {"id": "failed", "kind": "end", "state": "failed"},
        ],
    }


def continue_current(connection):
    row = job(connection, "task")
    current = task_flow.view(connection, row)
    return task_flow.continue_display(
        connection, row, current["flow_epoch"], current["revision"]
    )


def shown_values(view):
    return {entry["key"]: entry["value"] for entry in view["shown"]}


def test_text_aliases_feed_processing_branches_views_and_final_delivery(owner):
    setup(owner, graph())
    with db() as connection:
        first = answer_current(connection, "customer question")
        assert first["flow"]["current"]["id"] == "input_preview"
        assert shown_values(first["flow"]) == {"renamed_input": "customer question"}
        continue_current(connection)
        execution = task_flow.execution(connection, job(connection, "task"))
        assert execution["params"] == {"answer": "customer question"}
        first_result = finish_current(connection, "yes")
        assert shown_values(first_result["flow"]) == {"renamed_answer": "yes"}
        continue_current(connection)
        execution = task_flow.execution(connection, job(connection, "task"))
        assert execution["node_id"] == "p2"
        assert execution["params"] == {"answer": "yes"}
        second_epoch = execution["flow_epoch"]
        final_preview = finish_current(connection, "final answer")
        assert final_preview["flow"]["current"]["id"] == "final_preview"
        assert shown_values(final_preview["flow"]) == {
            "previous": "yes",
            "newest": "final answer",
        }
        finished = continue_current(connection)
        assert finished["terminal"]["output"] == {"answer_1": "final answer"}
        assert finished["terminal"]["result_sources"] == {
            "answer_1": {
                "node": "p2",
                "field": "answer",
                "flow_epoch": second_epoch,
                "kind": "output",
            }
        }


def test_file_aliases_keep_process_scope_when_promoted_to_final_delivery(owner):
    _, token = launch(
        owner, rounds=1, file_output=True, task_flow=graph(file_output=True)
    )
    started = action(owner, token, "start")
    assert started.status_code == 200, started.text
    row = started.json()
    state = row["task_flow"]
    uploaded = owner.post(
        "/api/files/upload",
        data={
            "token": token,
            "field_key": "source",
            "flow_epoch": str(state["flow_epoch"]),
            "expected_revision": str(state["revision"]),
            "node_id": state["current"]["id"],
        },
        files={"file": ("source.txt", b"Synthetic customer source", "text/plain")},
    )
    assert uploaded.status_code == 200, uploaded.text
    source_id = uploaded.json()["id"]
    row = answer(owner, token, row, question="Build a file", source=source_id)
    claimed = batch(owner, row, "claim")
    assert claimed.status_code == 200, claimed.text
    process_epoch = row["task_flow"]["flow_epoch"]
    invalid = batch(owner, row, "succeed", output={"draft": source_id})
    assert invalid.status_code == 403, invalid.text
    assert receipt(owner, token)["state"] == "processing"
    output = owner.post(
        "/api/manage/files/upload",
        data={
            "job_id": row["id"],
            "field_key": "draft",
            "flow_epoch": str(process_epoch),
        },
        files={"file": ("draft.txt", b"Synthetic generated delivery", "text/plain")},
    )
    assert output.status_code == 200, output.text
    file_id = output.json()["id"]
    completed = batch(owner, row, "succeed", output={"draft": file_id})
    assert completed.status_code == 200, completed.text
    preview = receipt(owner, token)
    assert preview["state"] == "waiting"
    assert preview["task_flow"]["current"]["id"] == "preview"
    assert preview["task_flow"]["shown"] == []
    continued = action(owner, token, "continue", preview)
    assert continued.status_code == 200, continued.text
    assert receipt(owner, token)["state"] == "succeeded"
    reveal = owner.post("/api/receipt/reveal", json={"token": token})
    assert reveal.status_code == 200, reveal.text
    assert reveal.json()["output"] == {"artifact": file_id}
    downloaded = download(owner, token, file_id)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == b"Synthetic generated delivery"
    assert download(owner, token, source_id).status_code == 403
    with db() as connection:
        actual_file = connection.execute(
            "SELECT * FROM job_files WHERE id=?", (file_id,)
        ).fetchone()
        actual_scope = connection.execute(
            "SELECT * FROM task_flow_files WHERE file_id=?", (file_id,)
        ).fetchone()
        assert actual_file["field_key"] == "artifact"
        assert actual_scope["node_id"] == "p1"
        assert actual_scope["flow_epoch"] == process_epoch
        assert actual_scope["kind"] == "output"
        original_file = connection.execute(
            "SELECT * FROM job_files WHERE id=?", (source_id,)
        ).fetchone()
        assert original_file["kind"] == "input"
        assert original_file["field_key"] == "source"


def long_graph():
    nodes = [
        {"id": "q1", "kind": "input", "fields": [field("answer")], "next": "p1"},
        process(
            "p1",
            {"answer": reference("q1", "answer")},
            [field("answer")],
            "display0",
        ),
    ]
    for index in range(60):
        aliases = {}
        for position in range(20):
            if position < 19:
                destination = reference(f"display{index}", f"field{position + 1}")
            elif index < 59:
                destination = reference(f"display{index + 1}", "field0")
            else:
                destination = reference("p1", "answer")
            aliases[f"field{position}"] = destination
        nodes.append(
            {
                "id": f"display{index}",
                "kind": "display",
                "show_from": aliases,
                "next": "done",
            }
        )
    nodes.extend(
        [
            {
                "id": "done",
                "kind": "end",
                "state": "succeeded",
                "result": {"answer_1": reference("display0", "field0")},
            },
            {"id": "failed", "kind": "end", "state": "failed"},
        ]
    )
    return {"version": 1, "entry": "q1", "nodes": nodes}


def test_runtime_handles_bounded_1200_alias_chain_without_python_recursion(owner):
    definition = long_graph()
    assert len(definition["nodes"]) == 64
    setup(owner, definition)
    with db() as connection:
        answer_current(connection)
        epoch = task_flow.execution(connection, job(connection, "task"))["flow_epoch"]
        displayed = finish_current(connection, "deep result")
        assert shown_values(displayed["flow"]) == {
            f"field{index}": "deep result" for index in range(20)
        }
        ended = continue_current(connection)
        assert ended["terminal"]["output"] == {"answer_1": "deep result"}
        assert ended["terminal"]["result_sources"]["answer_1"] == {
            "node": "p1",
            "field": "answer",
            "flow_epoch": epoch,
            "kind": "output",
        }


@pytest.mark.parametrize("corruption", ["missing_node", "missing_field", "cycle"])
def test_corrupt_alias_snapshot_returns_http_conflict_without_partial_mutation(
    owner, monkeypatch, corruption
):
    _, token = launch(
        owner,
        rounds=1,
        outputs=[field("answer_1")],
        task_flow=graph(),
    )
    row = action(owner, token, "start").json()
    row = answer(owner, token, row, answer="Customer input")
    assert row["task_flow"]["current"]["id"] == "input_preview"
    original_snapshot = task_flow.card_snapshot

    def corrupt_snapshot(connection, card_id):
        snapshot = deepcopy(original_snapshot(connection, card_id))
        preview = next(
            node
            for node in snapshot["definition"]["nodes"]
            if node["id"] == "input_preview"
        )
        preview["show_from"]["renamed_input"] = {
            "missing_node": reference("missing", "answer"),
            "missing_field": reference("q1", "missing"),
            "cycle": reference("input_preview", "renamed_input"),
        }[corruption]
        return snapshot

    monkeypatch.setattr(task_flow, "card_snapshot", corrupt_snapshot)
    before = state_snapshot()
    response = owner.post("/api/receipt", json={"token": token})
    assert response.status_code == 409, response.text
    assert isinstance(response.json()["detail"], str)
    assert "Traceback" not in response.text
    assert state_snapshot() == before


@pytest.mark.parametrize("source_kind", ["sensitive", "input_file"])
def test_aliases_cannot_publish_sensitive_input_or_promote_customer_files(source_kind):
    definition = graph(file_output=source_kind == "input_file")
    if source_kind == "input_file":
        preview = next(node for node in definition["nodes"] if node["id"] == "preview")
        preview["show_from"]["renamed_file"] = reference("q1", "source")
        configured = {"mode": "manual", "outputs": [field("artifact", "file")]}
    else:
        definition["nodes"][0]["fields"][0]["sensitive"] = True
        configured = {"mode": "manual", "outputs": [field("answer_1")]}
    with pytest.raises(ValueError):
        validate_definition(definition, configured)

"""Graph publication bounds and string-value contracts, without runtime IO."""

import json
from copy import deepcopy

import pytest

from extore.task_flow_definition import (
    MAX_DEFINITION_BYTES,
    MAX_NODES,
    MAX_TRANSITIONS,
    validate_definition,
    validate_values,
)


def field(key="requirements", kind="text", **values):
    return {"key": key, "type": kind, "label": {"en": key}, **values}


def product(**values):
    return {
        "mode": "manual",
        "parameters": [field()],
        "outputs": [field("content", "textarea")],
        **values,
    }


def reference(node, name):
    return {"node": node, "field": name}


def graph():
    return {
        "version": 1,
        "entry": "ask",
        "nodes": [
            {
                "id": "ask",
                "kind": "input",
                "prompt": {"en": "Start when ready"},
                "question": {"en": "What do you need?"},
                "fields": [field()],
                "next": "work",
            },
            {
                "id": "work",
                "kind": "process",
                "inputs": {"requirements": reference("ask", "requirements")},
                "outputs": [field("content", "textarea")],
                "next": "preview",
                "timeout_next": "failed",
                "failure_next": "failed",
            },
            {
                "id": "preview",
                "kind": "display",
                "content": {"en": "Your result"},
                "result": {"content": reference("work", "content")},
                "next": "done",
            },
            {
                "id": "done",
                "kind": "end",
                "state": "succeeded",
                "result": {"content": reference("preview", "content")},
            },
            {"id": "failed", "kind": "end", "state": "failed", "retryable": True},
        ],
    }


def node(value, node_id):
    return next(n for n in value["nodes"] if n["id"] == node_id)


def test_none_does_not_read_or_change_old_product_contract():
    assert validate_definition(None, object()) is None
    original = {"mode": "script", "parameters": [{"opaque": object()}]}
    assert validate_definition(None, original) is None
    assert set(original) == {"mode", "parameters"}


def test_canonical_graph_keeps_prompt_and_question_separate_and_copies_all_inputs():
    definition, configured = graph(), product()
    before_definition, before_product = deepcopy(definition), deepcopy(configured)
    result = validate_definition(definition, configured)
    assert node(result, "ask")["prompt"] == {"en": "Start when ready"}
    assert node(result, "ask")["question"] == {"en": "What do you need?"}
    assert node(result, "ask")["start_policy"] == "confirm"
    assert node(result, "work")["timeout_seconds"] == 3600
    assert "result" not in node(result, "preview")
    assert node(result, "preview")["show_from"] == {
        "content": reference("work", "content")
    }
    assert definition == before_definition and configured == before_product
    node(result, "ask")["fields"][0]["label"]["en"] = "changed"
    assert definition == before_definition
    assert MAX_NODES == 64 and MAX_TRANSITIONS == 256 and MAX_DEFINITION_BYTES == 100000


@pytest.mark.parametrize(
    "key",
    [
        "executor",
        "processor_id",
        "profile_id",
        "webhook_url",
        "eval",
        "command",
        "environment",
    ],
)
def test_graph_nodes_cannot_select_execution_or_secret_sources(key):
    definition = graph()
    node(definition, "work")[key] = "untrusted-private-value"
    with pytest.raises(ValueError) as error:
        validate_definition(definition, product())
    assert "untrusted-private-value" not in str(error.value)


@pytest.mark.parametrize(
    "change",
    [
        "version",
        "duplicate",
        "bad_id",
        "entry_missing",
        "entry_process",
        "empty",
        "too_many",
        "unknown_top",
    ],
)
def test_graph_shape_and_entry_boundaries(change):
    definition = graph()
    if change == "version":
        definition["version"] = True
    elif change == "duplicate":
        definition["nodes"].append(deepcopy(definition["nodes"][0]))
    elif change == "bad_id":
        node(definition, "ask")["id"] = "../code"
    elif change == "entry_missing":
        definition["entry"] = "missing"
    elif change == "entry_process":
        definition["entry"] = "work"
    elif change == "empty":
        definition["nodes"] = []
    elif change == "too_many":
        definition["nodes"] = [deepcopy(definition["nodes"][0])] * 65
    elif change == "unknown_top":
        definition["script"] = "print(1)"
    with pytest.raises(ValueError):
        validate_definition(definition, product())


def test_definition_size_is_bounded_in_utf8_bytes():
    definition = graph()
    node(definition, "ask")["prompt"] = {f"language{i}": "😀" * 10000 for i in range(6)}
    with pytest.raises(ValueError):
        validate_definition(definition, product())


@pytest.mark.parametrize(
    "change",
    [
        "next_missing",
        "target_missing",
        "timeout_missing",
        "failure_missing",
        "input_cases",
        "display_cases",
        "timeout_bad",
        "timeout_null",
        "timeout_without_route",
        "route_without_timeout",
    ],
)
def test_explicit_routes_and_timeout_contract(change):
    definition = graph()
    if change == "next_missing":
        del node(definition, "work")["next"]
    elif change == "target_missing":
        node(definition, "work")["next"] = "missing"
    elif change == "timeout_missing":
        del node(definition, "work")["timeout_next"]
    elif change == "failure_missing":
        del node(definition, "work")["failure_next"]
    elif change in ("input_cases", "display_cases"):
        node(definition, "ask" if change == "input_cases" else "preview")["next"] = {
            "cases": [],
            "default": "done",
        }
    elif change == "timeout_bad":
        node(definition, "work")["timeout_seconds"] = 86401
    elif change == "timeout_null":
        node(definition, "work")["timeout_seconds"] = None
    elif change == "timeout_without_route":
        node(definition, "ask")["timeout_seconds"] = 1
    elif change == "route_without_timeout":
        node(definition, "ask")["timeout_next"] = "failed"
    with pytest.raises(ValueError):
        validate_definition(definition, product())


def test_reachable_end_required_but_bounded_control_loops_are_allowed():
    definition = graph()
    node(definition, "ask")["next"] = "ask"
    with pytest.raises(ValueError):
        validate_definition(definition, product())
    definition = graph()
    node(definition, "work")["next"] = {
        "cases": [
            {
                "when": {
                    "source": reference("work", "content"),
                    "op": "eq",
                    "value": "retry",
                },
                "to": "ask",
            }
        ],
        "default": "preview",
    }
    assert validate_definition(definition, product())


@pytest.mark.parametrize(
    "change",
    [
        "missing_node",
        "missing_field",
        "missing_showfield",
        "display_ambiguity",
        "data_cycle",
        "missing_final",
        "unknown_final",
        "failed_result",
    ],
)
def test_reference_schema_and_terminal_output_boundaries(change):
    definition = graph()
    if change == "missing_node":
        node(definition, "work")["inputs"]["requirements"] = reference(
            "missing", "requirements"
        )
    elif change == "missing_field":
        node(definition, "work")["inputs"]["requirements"] = reference("ask", "missing")
    elif change == "missing_showfield":
        node(definition, "ask")["show_from"] = {"answer1": reference("work", "missing")}
    elif change == "display_ambiguity":
        node(definition, "preview")["show_from"] = {}
    elif change == "data_cycle":
        node(definition, "preview")["result"]["content"] = reference(
            "preview", "content"
        )
    elif change == "missing_final":
        node(definition, "done")["result"] = {}
    elif change == "unknown_final":
        node(definition, "done")["result"]["unknown"] = reference("work", "content")
    elif change == "failed_result":
        node(definition, "failed")["result"] = {"content": reference("work", "content")}
    with pytest.raises(ValueError):
        validate_definition(definition, product())


def test_failed_and_rejected_terminal_nodes_do_not_require_success_outputs():
    definition = {
        "version": 1,
        "entry": "notice",
        "nodes": [
            {"id": "notice", "kind": "display", "next": "failed"},
            {
                "id": "failed",
                "kind": "end",
                "state": "rejected",
                "result": {},
                "needs_review": True,
            },
        ],
    }
    normalized = validate_definition(definition, product())
    assert node(normalized, "failed")["result"] == {}
    assert node(normalized, "failed")["needs_review"] is True


@pytest.mark.parametrize(
    "change",
    [
        "unknown_input",
        "missing_input",
        "output_type",
        "output_required",
        "output_extra",
    ],
)
def test_script_node_cannot_expand_the_fixed_executor_contract(change):
    definition = graph()
    configured = product(mode="script")
    assert validate_definition(definition, configured)
    if change == "unknown_input":
        node(definition, "work")["inputs"]["unknown"] = reference("ask", "requirements")
    elif change == "missing_input":
        node(definition, "work")["inputs"] = {}
    elif change == "output_type":
        node(definition, "work")["outputs"][0]["type"] = "text"
    elif change == "output_required":
        node(definition, "work")["outputs"][0]["required"] = False
    elif change == "output_extra":
        node(definition, "work")["outputs"].append(field("unknown"))
    with pytest.raises(ValueError):
        validate_definition(definition, configured)


def test_sensitive_input_has_ttl_only_when_opted_in_and_only_processor_can_consume():
    definition = graph()
    node(definition, "ask")["fields"][0]["sensitive"] = True
    result = validate_definition(definition, product())
    assert node(result, "ask")["fields"][0]["sensitive_ttl_seconds"] == 120
    assert node(result, "work")["inputs"]["requirements"] == reference(
        "ask", "requirements"
    )
    node(definition, "ask")["fields"][0]["sensitive"] = False
    result = validate_definition(definition, product())
    assert "sensitive" not in node(result, "ask")["fields"][0]
    assert "sensitive_ttl_seconds" not in node(result, "ask")["fields"][0]


@pytest.mark.parametrize(
    "exposure", ["display", "input", "end", "condition", "output", "type", "ttl"]
)
def test_sensitive_value_cannot_escape_through_graph_metadata(exposure):
    definition = graph()
    node(definition, "ask")["fields"][0]["sensitive"] = True
    if exposure == "display":
        node(definition, "preview")["result"] = {
            "content": reference("ask", "requirements")
        }
    elif exposure == "input":
        node(definition, "ask")["show_from"] = {
            "requirements": reference("ask", "requirements")
        }
    elif exposure == "end":
        configured = product(outputs=[field("content")])
        node(definition, "done")["result"] = {
            "content": reference("ask", "requirements")
        }
        with pytest.raises(ValueError):
            validate_definition(definition, configured)
        return
    elif exposure == "condition":
        node(definition, "work")["next"] = {
            "cases": [
                {
                    "when": {
                        "source": reference("ask", "requirements"),
                        "op": "exists",
                    },
                    "to": "done",
                }
            ],
            "default": "failed",
        }
    elif exposure == "output":
        node(definition, "work")["outputs"][0].update(type="text", sensitive=True)
    elif exposure == "type":
        node(definition, "ask")["fields"][0]["type"] = "textarea"
    elif exposure == "ttl":
        node(definition, "ask")["fields"][0]["sensitive_ttl_seconds"] = 601
    with pytest.raises(ValueError):
        validate_definition(definition, product())


def test_final_attachments_require_processor_output_origin_even_through_projection():
    definition = graph()
    node(definition, "ask")["fields"] = [field("upload", "file")]
    node(definition, "work")["inputs"] = {"source": reference("ask", "upload")}
    node(definition, "work")["outputs"] = [field("delivery", "file")]
    node(definition, "preview")["result"] = {"delivery": reference("work", "delivery")}
    node(definition, "done")["result"] = {"delivery": reference("preview", "delivery")}
    configured = product(outputs=[field("delivery", "file")])
    assert validate_definition(definition, configured)
    node(definition, "preview")["result"] = {"delivery": reference("ask", "upload")}
    with pytest.raises(ValueError):
        validate_definition(definition, configured)


@pytest.mark.parametrize(
    "kind,value",
    [
        ("email", "not-an-email"),
        ("url", "javascript:alert(1)"),
        ("url", "https://name:password@example.com"),
        ("url", "https://example.com:99999"),
        ("number", "NaN"),
        ("number", "Infinity"),
        ("number", "not-a-number"),
        ("boolean", "yes"),
        ("file", "../file"),
        ("image", "https://example.com/image"),
    ],
)
def test_typed_values_reject_noncanonical_or_unsafe_references(kind, value):
    with pytest.raises(ValueError):
        validate_values([field(kind=kind)], {"requirements": value})


def test_false_is_a_present_boolean_and_text_output_preserves_indentation():
    assert validate_values([field(kind="boolean")], {"requirements": " false "}) == {
        "requirements": "false"
    }
    assert validate_values([field(kind="boolean")], {"requirements": "FALSE"}) == {
        "requirements": "false"
    }
    assert validate_values(
        [field(kind="textarea")], {"requirements": "  text\n"}, output=True
    ) == {"requirements": "  text\n"}
    assert validate_values([field(kind="number")], {"requirements": " 1e999999 "}) == {
        "requirements": "1e999999"
    }


def test_select_and_images_are_typed_and_bounded_without_asserting_file_ownership():
    choice = field(
        kind="select", options=[{"value": "basic", "label": {"en": "Basic"}}]
    )
    assert (
        validate_values([choice], {"requirements": " basic "})["requirements"]
        == "basic"
    )
    with pytest.raises(ValueError):
        validate_values([choice], {"requirements": "other"})
    images = field(kind="images", max_items=2)
    value = validate_values(
        [images], {"requirements": '[ "not-owned-file", "uuid-like-reference" ]'}
    )
    assert value == {"requirements": '["not-owned-file","uuid-like-reference"]'}
    for invalid in (
        '["one","one"]',
        '["one","two","three"]',
        '["https://example.com/file"]',
        "[]",
        "{}",
    ):
        with pytest.raises(ValueError):
            validate_values([images], {"requirements": invalid})
    assert validate_values([field(kind="images", required=False)], {}) == {
        "requirements": "[]"
    }


def test_values_reject_unknown_fields_native_types_and_size_overruns():
    for values in (
        {"unknown": "value"},
        {"requirements": False},
        {"requirements": "x" * 10001},
        {"requirements": "\0"},
    ):
        with pytest.raises(ValueError):
            validate_values([field()], values)
    with pytest.raises(ValueError):
        validate_values(
            [field("first"), field("second")],
            {"first": "x" * 60000, "second": "x" * 60000},
            output=True,
        )
    with pytest.raises(ValueError):
        validate_values(
            [field(kind="text", sensitive=True)],
            {"requirements": "secret"},
            output=True,
        )


def test_cases_have_explicit_default_and_canonical_typed_comparison_values():
    definition = graph()
    node(definition, "work")["outputs"].append(field("approved", "boolean"))
    node(definition, "work")["next"] = {
        "cases": [
            {
                "when": {
                    "source": reference("work", "approved"),
                    "op": "eq",
                    "value": " FALSE ",
                },
                "to": "failed",
            },
            {
                "when": {
                    "source": reference("work", "content"),
                    "op": "in",
                    "value": ["one", "two"],
                },
                "to": "preview",
            },
        ],
        "default": "done",
    }
    canonical = validate_definition(definition, product())
    assert node(canonical, "work")["next"]["cases"][0]["when"]["value"] == "false"
    del node(definition, "work")["next"]["default"]
    with pytest.raises(ValueError):
        validate_definition(definition, product())


def test_input_can_branch_after_answer_and_show_unrelated_previous_outputs_without_prefilling():
    definition = graph()
    node(definition, "ask")["show_from"] = {
        "previous_answer": reference("work", "content")
    }
    node(definition, "ask")["next"] = {
        "cases": [
            {
                "when": {
                    "source": reference("ask", "requirements"),
                    "op": "eq",
                    "value": "stop",
                },
                "to": "failed",
            }
        ],
        "default": "work",
    }
    canonical = validate_definition(definition, product())
    assert node(canonical, "ask")["show_from"] == {
        "previous_answer": reference("work", "content")
    }
    assert node(canonical, "ask")["fields"][0]["key"] == "requirements"
    assert "value" not in node(canonical, "ask")["fields"][0]
    assert node(canonical, "ask")["next"]["default"] == "work"


def test_optional_final_fields_can_be_omitted_and_unknown_fields_cannot():
    configured = product(
        outputs=[field("content", "textarea"), field("notes", required=False)]
    )
    assert validate_definition(graph(), configured)
    definition = graph()
    node(definition, "done")["result"]["undeclared"] = reference("ask", "requirements")
    with pytest.raises(ValueError):
        validate_definition(definition, configured)


def test_unknown_schema_options_and_excess_fields_are_rejected():
    for invalid_field in (
        field(options=[]),
        field(max_items=2),
        field(kind="images", max_items=True),
        field(kind="select", options=[]),
        field(sensitive_ttl_seconds=120),
    ):
        definition = graph()
        node(definition, "ask")["fields"] = [invalid_field]
        with pytest.raises(ValueError):
            validate_definition(definition, product())
    definition = graph()
    node(definition, "ask")["fields"] = [field(f"field{i}") for i in range(31)]
    with pytest.raises(ValueError):
        validate_definition(definition, product())


def test_canonical_graph_remains_plain_json():
    definition = validate_definition(graph(), product())
    assert json.loads(json.dumps(definition, ensure_ascii=False)) == definition


def test_large_but_bounded_projection_chain_uses_no_python_recursion():
    definition = graph()
    definition["nodes"] = [n for n in definition["nodes"] if n["id"] != "preview"]
    node(definition, "work")["next"] = "display0"
    node(definition, "done")["result"] = {"content": reference("display0", "field0")}
    for i in range(60):
        aliases = {}
        for j in range(20):
            if j < 19:
                destination = reference(f"display{i}", f"field{j + 1}")
            elif i < 59:
                destination = reference(f"display{i + 1}", "field0")
            else:
                destination = reference("work", "content")
            aliases[f"field{j}"] = destination
        definition["nodes"].append(
            {
                "id": f"display{i}",
                "kind": "display",
                "show_from": aliases,
                "next": f"display{i + 1}" if i < 59 else "done",
            }
        )
    assert len(definition["nodes"]) == 64
    assert validate_definition(definition, product())


def test_encoded_limit_also_applies_after_canonical_defaults_expand_definition():
    definition = {
        "version": 1,
        "entry": "input0",
        "nodes": [
            {
                "id": f"input{i}",
                "kind": "input",
                "fields": [
                    {"key": f"field{j}", "label": {"en": "x" * 10}} for j in range(20)
                ],
                "next": f"input{i + 1}" if i < 62 else "done",
            }
            for i in range(63)
        ]
        + [{"id": "done", "kind": "end", "state": "succeeded", "result": {}}],
    }
    assert (
        len(json.dumps(definition, separators=(",", ":")).encode())
        < MAX_DEFINITION_BYTES
    )
    with pytest.raises(ValueError):
        validate_definition(definition, product(parameters=[], outputs=[]))


def test_frozen_product_and_definition_share_a_utf8_snapshot_budget():
    assert validate_definition(graph(), product(name={"zh": "商品"}))
    with pytest.raises(ValueError, match="快照"):
        validate_definition(graph(), product(name={"zh": "中" * 67000}))
    # Unrelated administrative metadata is not part of the issued snapshot.
    assert validate_definition(graph(), product(admin_notes="中" * 100000))


def test_output_and_input_context_have_a_separate_utf8_byte_budget():
    with pytest.raises(ValueError, match="编码大小"):
        validate_values(
            [field(kind="textarea")], {"requirements": "中" * 70000}, output=True
        )
    fields = [field(f"input{i}") for i in range(10)]
    with pytest.raises(ValueError, match="编码大小"):
        validate_values(fields, {f"input{i}": "中" * 7000 for i in range(10)})
    assert validate_values(
        [field(kind="textarea")], {"requirements": "中" * 60000}, output=True
    )

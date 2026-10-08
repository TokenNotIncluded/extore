"""Check public schema with an independent JSON Schema implementation."""

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from extore.task_flow_definition import validate_definition
from extore.task_flow_schema import SCHEMA_ID, definition_schema


def field(key="question", **values):
    return {"key": key, "label": {"en": key}, **values}


def product():
    return {"mode": "manual", "parameters": [field()], "outputs": [field("answer")]}


def graph():
    return {
        "version": 1,
        "entry": "ask",
        "nodes": [
            {"id": "ask", "kind": "input", "fields": [field()], "next": "work"},
            {
                "id": "work",
                "kind": "process",
                "inputs": {"question": {"node": "ask", "field": "question"}},
                "outputs": [field("answer")],
                "next": "show",
                "timeout_next": "failed",
                "failure_next": "failed",
            },
            {
                "id": "show",
                "kind": "display",
                "result": {"answer": {"node": "work", "field": "answer"}},
                "next": "done",
            },
            {
                "id": "done",
                "kind": "end",
                "state": "succeeded",
                "result": {"answer": {"node": "show", "field": "answer"}},
            },
            {"id": "failed", "kind": "end", "state": "failed", "retryable": True},
        ],
    }


def test_schema_is_valid_fresh_and_product_independent():
    first = definition_schema()
    Draft202012Validator.check_schema(first)
    assert first["$id"] == SCHEMA_ID == "https://extore.lmm.best/api/task-flows/schema"
    assert first["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert first["x-extore-limits"] == {
        "definition_bytes": 100000,
        "snapshot_bytes": 200000,
        "node_count": 64,
        "transition_count": 256,
    }
    first["$defs"]["inputField"]["properties"].clear()
    assert definition_schema()["$defs"]["inputField"]["properties"]["key"]


def test_raw_defaults_and_canonical_definition_both_match_public_schema():
    raw = graph()
    validator = Draft202012Validator(definition_schema())
    validator.validate(raw)
    canonical = validate_definition(raw, product())
    validator.validate(canonical)
    assert canonical["nodes"][0]["timeout_seconds"] is None
    assert canonical["nodes"][1]["timeout_seconds"] == 3600
    assert "result" not in canonical["nodes"][2]
    validator.validate(None)


@pytest.mark.parametrize("kind", ["input", "display"])
def test_optional_timer_parity(kind):
    raw = graph()
    step = next(node for node in raw["nodes"] if node["kind"] == kind)
    validator = Draft202012Validator(definition_schema())
    for values in (
        {},
        {"timeout_seconds": None},
        {"timeout_seconds": 60, "timeout_next": "failed"},
    ):
        node = deepcopy(step)
        node.update(values)
        candidate = deepcopy(raw)
        candidate["nodes"][raw["nodes"].index(step)] = node
        validator.validate(candidate)
        validate_definition(candidate, product())
    for values in (
        {"timeout_next": "failed"},
        {"timeout_seconds": None, "timeout_next": "failed"},
        {"timeout_seconds": 60},
    ):
        node = deepcopy(step)
        node.update(values)
        candidate = deepcopy(raw)
        candidate["nodes"][raw["nodes"].index(step)] = node
        assert not validator.is_valid(candidate)
        with pytest.raises(ValueError):
            validate_definition(candidate, product())


@pytest.mark.parametrize(
    "change",
    [
        "version_bool",
        "uppercase_id",
        "id_newline",
        "unknown_root",
        "unknown_node",
        "missing_fields",
        "missing_timeout_next",
        "missing_failure_next",
        "null_process_timer",
        "boolean_timer",
        "too_long_timer",
        "display_branch",
        "display_both_aliases",
        "end_missing_state",
        "failed_with_result",
        "reference_extra",
        "mapping_bad_key",
        "field_unknown",
        "field_newline",
        "field_missing_label",
        "field_empty_label",
        "field_blank_label",
        "field_long_label",
        "bad_locale",
        "null_description",
        "boolean_string",
        "options_without_select",
        "select_missing_options",
        "select_empty_options",
        "option_extra",
        "option_blank",
        "option_untrimmed",
        "max_items_without_images",
        "max_items_zero",
        "sensitive_textarea",
        "ttl_without_sensitive",
        "ttl_boolean",
        "output_sensitive",
        "empty_cases",
        "exists_with_value",
        "eq_missing_value",
        "in_nonstring",
        "condition_extra",
    ],
)
def test_structural_rejections_match_publication_validator(change):
    raw = graph()
    ask, work, show, done, failed = raw["nodes"]
    f = ask["fields"][0]
    if change == "version_bool":
        raw["version"] = True
    elif change == "uppercase_id":
        ask["id"] = "Ask"
    elif change == "id_newline":
        ask["id"] = "ask\n"
    elif change == "unknown_root":
        raw["credentials"] = "private-value"
    elif change == "unknown_node":
        work["environment"] = {"TOKEN": "private-value"}
    elif change == "missing_fields":
        del ask["fields"]
    elif change == "missing_timeout_next":
        del work["timeout_next"]
    elif change == "missing_failure_next":
        del work["failure_next"]
    elif change == "null_process_timer":
        work["timeout_seconds"] = None
    elif change == "boolean_timer":
        work["timeout_seconds"] = True
    elif change == "too_long_timer":
        work["timeout_seconds"] = 86401
    elif change == "display_branch":
        show["next"] = {"cases": [], "default": "done"}
    elif change == "display_both_aliases":
        show["show_from"] = {}
    elif change == "end_missing_state":
        del done["state"]
    elif change == "failed_with_result":
        failed["result"] = done["result"]
    elif change == "reference_extra":
        work["inputs"]["question"]["fallback"] = "x"
    elif change == "mapping_bad_key":
        work["inputs"]["Question"] = work["inputs"].pop("question")
    elif change == "field_unknown":
        f["placeholder"] = "x"
    elif change == "field_newline":
        f["key"] = "question\n"
    elif change == "field_missing_label":
        del f["label"]
    elif change == "field_empty_label":
        f["label"] = {}
    elif change == "field_blank_label":
        f["label"] = {"en": " \n\t"}
    elif change == "field_long_label":
        f["label"] = {"en": "x" * 201}
    elif change == "bad_locale":
        f["label"] = {"en\n": "Question"}
    elif change == "null_description":
        f["description"] = None
    elif change == "boolean_string":
        f["required"] = "true"
    elif change == "options_without_select":
        f["options"] = [{"value": "yes", "label": {"en": "Yes"}}]
    elif change == "select_missing_options":
        f["type"] = "select"
    elif change == "select_empty_options":
        f.update(type="select", options=[])
    elif change in ("option_extra", "option_blank", "option_untrimmed"):
        option = {"value": "yes", "label": {"en": "Yes"}}
        f.update(type="select", options=[option])
        if change == "option_extra":
            option["disabled"] = True
        if change == "option_blank":
            option["value"] = ""
        if change == "option_untrimmed":
            option["value"] = " yes "
    elif change == "max_items_without_images":
        f["max_items"] = 1
    elif change == "max_items_zero":
        f.update(type="images", max_items=0)
    elif change == "sensitive_textarea":
        f.update(type="textarea", sensitive=True)
    elif change == "ttl_without_sensitive":
        f["sensitive_ttl_seconds"] = 10
    elif change == "ttl_boolean":
        f.update(sensitive=True, sensitive_ttl_seconds=True)
    elif change == "output_sensitive":
        work["outputs"][0]["sensitive"] = True
    else:
        when = {
            "source": {"node": "ask", "field": "question"},
            "op": "eq",
            "value": "yes",
        }
        ask["next"] = {"cases": [{"when": when, "to": "work"}], "default": "failed"}
        if change == "empty_cases":
            ask["next"]["cases"] = []
        elif change == "exists_with_value":
            when["op"] = "exists"
        elif change == "eq_missing_value":
            del when["value"]
        elif change == "in_nonstring":
            when.update(op="in", value=[True])
        elif change == "condition_extra":
            when["script"] = "x"
    assert not Draft202012Validator(definition_schema()).is_valid(raw)
    with pytest.raises(ValueError):
        validate_definition(raw, product())


@pytest.mark.parametrize(
    "operation,value", [("eq", "yes"), ("in", ["yes", "no"]), ("exists", None)]
)
def test_branch_structures_and_sensitive_input_default(operation, value):
    raw = graph()
    source = {"node": "ask", "field": "question"}
    when = {"source": source, "op": operation}
    if operation != "exists":
        when["value"] = value
    raw["nodes"][0]["next"] = {
        "cases": [{"when": when, "to": "work"}],
        "default": "failed",
    }
    validator = Draft202012Validator(definition_schema())
    validator.validate(raw)
    validator.validate(validate_definition(raw, product()))
    raw["nodes"][0]["fields"].append(field("code", sensitive=True))
    validator.validate(raw)
    validator.validate(validate_definition(raw, product()))


def test_schema_is_structural_and_server_remains_graph_authority():
    raw = graph()
    raw["nodes"][0]["next"] = "missing"
    Draft202012Validator(definition_schema()).validate(raw)
    with pytest.raises(ValueError):
        validate_definition(raw, product())


def test_example_validation_ignores_cache_directories(tmp_path, capsys):
    import json

    from examples.workflows.validate_examples import main

    (tmp_path / "__pycache__").mkdir()
    example = tmp_path / "sample"
    example.mkdir()
    (example / "flow.json").write_text(json.dumps(graph()), encoding="utf-8")
    (example / "product.json").write_text(json.dumps(product()), encoding="utf-8")
    main(tmp_path)
    assert capsys.readouterr().out == "sample: valid (5 nodes)\n"
    (example / "product.json").unlink()
    with pytest.raises(FileNotFoundError):
        main(tmp_path)


def test_example_validation_rejects_empty_directory(tmp_path):
    from examples.workflows.validate_examples import main

    with pytest.raises(ValueError, match="No workflow examples found"):
        main(tmp_path)

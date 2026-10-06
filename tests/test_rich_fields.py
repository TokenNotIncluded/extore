import json
import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from extore.field_values import attachment_ids, normalize_rich_value
from extore.models import OutputField, Parameter, Product
from extore.service import validate_output, validate_params


def field(kind, **values):
    return {
        "key": "value",
        "label": {"zh-CN": "字段", "en": "Value"},
        "type": kind,
        **values,
    }


OPTIONS = [
    {"value": "basic", "label": {"zh-CN": "基础版", "en": "Basic"}},
    {"value": "plus", "label": {"zh-CN": "加强版", "en": "Plus"}},
]


def schema(kind, **values):
    definition = Parameter.model_validate(field(kind, **values)).model_dump()
    return {"mode": "manual", "parameters": [definition], "outputs": [definition]}


def test_legacy_field_dump_remains_identical_and_old_processors_still_validate():
    definition = Parameter.model_validate(field("text")).model_dump()
    assert set(definition) == {
        "key",
        "label",
        "description",
        "collapsed",
        "required",
        "type",
    }
    assert OutputField.model_validate(field("text")).model_dump() == definition
    from extore_processors import get_spec

    spec = get_spec("personalized_text")
    product = Product(
        name="旧预设",
        mode="script",
        processor_id=spec["id"],
        parameters=spec["parameters"],
        outputs=spec["outputs"],
    )
    assert product.parameters[0].model_dump() == spec["parameters"][0]
    assert product.outputs[0].model_dump() == spec["outputs"][0]


def test_select_and_boolean_values_remain_strings_and_false_counts_as_present():
    selected = schema("select", options=OPTIONS)
    assert validate_params(selected, {"value": " plus "}) == {"value": "plus"}
    assert validate_output(selected, {"value": "plus"}) == {"value": "plus"}
    boolean = schema("boolean")
    for value in ("true", "false"):
        assert validate_params(boolean, {"value": value}) == {"value": value}
        assert validate_output(boolean, {"value": value}) == {"value": value}
    for definition, value in (
        (selected, "基础版"),
        (selected, "unknown"),
        (boolean, "0"),
        (boolean, "False"),
        (boolean, ""),
    ):
        with pytest.raises(HTTPException):
            validate_params(definition, {"value": value})
        with pytest.raises(HTTPException):
            validate_output(definition, {"value": value})


def test_images_are_canonical_bounded_unique_id_lists_and_optional_empty_is_safe():
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    definition = schema("images", max_items=2)
    value = json.dumps(ids, indent=2)
    expected = json.dumps(ids, separators=(",", ":"))
    assert validate_params(definition, {"value": value}) == {"value": expected}
    assert validate_output(definition, {"value": value}) == {"value": expected}
    assert attachment_ids(definition["parameters"][0], expected) == ids
    optional = schema("images", required=False)
    assert validate_params(optional, {}) == {"value": "[]"}
    assert validate_output(optional, {}) == {"value": "[]"}
    for invalid in (
        "[]",
        "null",
        '"not-array"',
        json.dumps([ids[0], ids[0]]),
        json.dumps(ids + [str(uuid.uuid4())]),
        json.dumps(["https://example.test/image.png"]),
        json.dumps([{}]),
        "[",
    ):
        with pytest.raises(HTTPException):
            validate_params(definition, {"value": invalid})
        with pytest.raises(HTTPException):
            validate_output(definition, {"value": invalid})


@pytest.mark.parametrize(
    "values",
    [
        field("select"),
        field("select", options=OPTIONS + [OPTIONS[0]]),
        field("select", options=[{"value": "bad value", "label": {"en": "Bad"}}]),
        field("select", options=[{"value": "basic", "label": {}}]),
        field("text", options=OPTIONS),
        field("images", max_items=0),
        field("images", max_items=21),
        field("images", max_items=True),
        field("file", max_items=2),
        field("image", sensitive=True),
        field("textarea", sensitive=True),
    ],
)
def test_invalid_rich_schema_is_rejected(values):
    with pytest.raises(ValidationError):
        Parameter.model_validate(values)


def test_sensitive_input_is_explicit_text_only_and_not_an_output():
    sensitive = Parameter.model_validate(field("text", sensitive=True)).model_dump()
    assert sensitive["sensitive"] is True and sensitive["sensitive_ttl_seconds"] == 120
    with pytest.raises(ValidationError):
        Parameter.model_validate(
            field("text", sensitive=True, sensitive_ttl_seconds=601)
        )
    with pytest.raises(ValidationError):
        OutputField.model_validate(field("text", sensitive=True))
    with pytest.raises(ValidationError):
        Product(name="普通任务", parameters=[field("text", sensitive=True)])


def test_service_products_remain_outputless_for_arbitrary_declared_inputs():
    product = Product(name="服务", delivery="service", parameters=[field("boolean")])
    assert product.outputs == []


@pytest.mark.parametrize("kind", ["file", "image", "images"])
@pytest.mark.parametrize("value", [0, False, None, [], {}])
def test_attachment_helpers_do_not_coerce_invalid_empty_values(kind, value):
    definition = {"key": "asset", "type": kind, "required": False}
    with pytest.raises(ValueError, match="文本"):
        attachment_ids(definition, value)
    with pytest.raises(ValueError, match="文本"):
        normalize_rich_value(definition, value)
    assert attachment_ids(definition, "") == []

"""Public structural schema for the version 1 declarative task-flow contract.

The schema is product-independent. Publication still requires
``task_flow_definition.validate_definition`` for graph references, field
compatibility, reachability and encoded-size checks.
"""

from .task_flow_definition import (
    FIELD_TYPES,
    MAX_CASES,
    MAX_DEFINITION_BYTES,
    MAX_FIELDS,
    MAX_IMAGES,
    MAX_NODES,
    MAX_OPTIONS,
    MAX_SNAPSHOT_BYTES,
    MAX_TRANSITIONS,
)

SCHEMA_ID = "https://extore.lmm.best/api/task-flows/schema"
_NODE_PATTERN = "^[a-z][a-z0-9_-]{0,63}(?![\\s\\S])"
_FIELD_PATTERN = "^[a-z][a-z0-9_]{0,39}(?![\\s\\S])"
_NO_NUL = "^[^\\u0000]*(?![\\s\\S])"


def _object(properties, required=(), **values):
    result = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
        **values,
    }
    if required:
        result["required"] = list(required)
    return result


def _ref(name):
    return {"$ref": "#/$defs/" + name}


def _i18n(limit, *, required=False):
    result = {
        "type": "object",
        "maxProperties": 20,
        "propertyNames": {
            "type": "string",
            "minLength": 1,
            "maxLength": 40,
            "pattern": "^(?=.*\\S)[^\\u0000-\\u001f\\u007f]+(?![\\s\\S])",
        },
        "additionalProperties": {
            "type": "string",
            "maxLength": limit,
            "pattern": _NO_NUL,
        },
    }
    if required:
        result.update(
            {
                "minProperties": 1,
                "not": {"additionalProperties": {"pattern": "^\\s*$"}},
            }
        )
    return result


def _field(*, sensitive):
    properties = {
        "key": _ref("fieldKey"),
        "label": _ref("fieldLabel"),
        "description": _ref("i18n"),
        "collapsed": {"type": "boolean", "default": True},
        "required": {"type": "boolean", "default": True},
        "type": {"enum": sorted(FIELD_TYPES), "default": "text"},
        "options": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_OPTIONS,
            "items": _ref("option"),
        },
        "max_items": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_IMAGES,
            "default": 10,
        },
        "sensitive": {"type": "boolean", "default": False},
        "sensitive_ttl_seconds": {
            "type": "integer",
            "minimum": 1,
            "maximum": 600,
            "default": 120,
        },
    }
    if not sensitive:
        properties["sensitive"] = {"const": False}
    return _object(
        properties,
        ("key", "label"),
        allOf=[
            {
                "if": {
                    "required": ["type"],
                    "properties": {"type": {"const": "select"}},
                },
                "then": {"required": ["options"]},
                "else": {"not": {"required": ["options"]}},
            },
            {
                "if": {"required": ["max_items"]},
                "then": {
                    "required": ["type"],
                    "properties": {"type": {"const": "images"}},
                },
            },
            {
                "if": {
                    "required": ["sensitive"],
                    "properties": {"sensitive": {"const": True}},
                },
                "then": {"properties": {"type": {"const": "text"}}},
                "else": {"not": {"required": ["sensitive_ttl_seconds"]}},
            },
        ],
    )


def _fields(name):
    return {"type": "array", "maxItems": MAX_FIELDS, "items": _ref(name)}


def _timeout():
    # Input/display can omit the timer, or contain canonical null. A route
    # without an integer timer is invalid in either raw or canonical graphs.
    return {
        "if": {
            "required": ["timeout_seconds"],
            "properties": {"timeout_seconds": {"type": "integer"}},
        },
        "then": {"required": ["timeout_next"]},
        "else": {"not": {"required": ["timeout_next"]}},
    }


def _node(kind, properties, required=(), **values):
    return _object(
        {
            "id": _ref("nodeId"),
            "kind": {"const": kind},
            "label": _ref("nodeLabel"),
            **properties,
        },
        ("id", "kind", *required),
        **values,
    )


def definition_schema():
    """Return a fresh JSON Schema 2020-12 document; no database or IO needed."""
    timeout_properties = {
        "timeout_seconds": {
            "type": ["integer", "null"],
            "minimum": 1,
            "maximum": 86400,
            "default": None,
        },
        "timeout_next": _ref("nodeId"),
    }
    conditions = [
        _object(
            {"source": _ref("reference"), "op": {"const": "exists"}},
            ("source", "op"),
        ),
        _object(
            {
                "source": _ref("reference"),
                "op": {"const": "eq"},
                "value": {"type": "string"},
            },
            ("source", "op", "value"),
        ),
        _object(
            {
                "source": _ref("reference"),
                "op": {"const": "in"},
                "value": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_OPTIONS,
                    "items": {"type": "string"},
                },
            },
            ("source", "op", "value"),
        ),
    ]
    nodes = [
        _node(
            "input",
            {
                "prompt": _ref("i18n"),
                "question": _ref("i18n"),
                "fields": _fields("inputField"),
                "start_policy": {
                    "enum": ["confirm", "automatic"],
                    "default": "confirm",
                },
                "show_from": _ref("mapping"),
                "next": _ref("next"),
                **timeout_properties,
            },
            ("fields", "next"),
            allOf=[_timeout()],
        ),
        _node(
            "process",
            {
                "inputs": _ref("mapping"),
                "outputs": _fields("outputField"),
                "next": _ref("next"),
                "failure_next": _ref("nodeId"),
                "timeout_next": _ref("nodeId"),
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 86400,
                    "default": 3600,
                },
            },
            ("inputs", "outputs", "next", "failure_next", "timeout_next"),
        ),
        _node(
            "display",
            {
                "content": _ref("i18n"),
                "show_from": _ref("mapping"),
                "result": {
                    **_ref("mapping"),
                    "deprecated": True,
                    "description": "Legacy alias of show_from; cannot be used together.",
                },
                "next": _ref("nodeId"),
                **timeout_properties,
            },
            ("next",),
            allOf=[_timeout(), {"not": {"required": ["result", "show_from"]}}],
        ),
        _node(
            "end",
            {
                "state": {"enum": ["succeeded", "failed", "rejected"]},
                "result": _ref("mapping"),
                "message": _ref("i18n"),
                "retryable": {"type": "boolean", "default": False},
                "needs_review": {"type": "boolean", "default": False},
            },
            ("state",),
            allOf=[
                {
                    "if": {"properties": {"state": {"enum": ["failed", "rejected"]}}},
                    "then": {"properties": {"result": {"maxProperties": 0}}},
                }
            ],
        ),
    ]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "Extore task-flow definition v1",
        "description": (
            "Declarative customer-input, processing, display and terminal nodes. "
            "Null preserves the simple redemption path. Server validation also "
            "checks product field compatibility, graph references, reachability, "
            "UTF-8 encoded size and snapshot size before publication."
        ),
        "oneOf": [
            {"type": "null"},
            _object(
                {
                    "version": {"type": "integer", "const": 1},
                    "entry": _ref("nodeId"),
                    "nodes": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_NODES,
                        "items": {"oneOf": nodes},
                    },
                },
                ("version", "entry", "nodes"),
            ),
        ],
        "x-extore-limits": {
            "definition_bytes": MAX_DEFINITION_BYTES,
            "snapshot_bytes": MAX_SNAPSHOT_BYTES,
            "node_count": MAX_NODES,
            "transition_count": MAX_TRANSITIONS,
        },
        "$defs": {
            "nodeId": {"type": "string", "pattern": _NODE_PATTERN},
            "fieldKey": {"type": "string", "pattern": _FIELD_PATTERN},
            "i18n": _i18n(10000),
            "nodeLabel": _i18n(200),
            "fieldLabel": _i18n(200, required=True),
            "inputField": _field(sensitive=True),
            "outputField": _field(sensitive=False),
            "option": _object(
                {
                    "value": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 100,
                        "pattern": "^(?!\\s)(?!.*\\s$)[^\\u0000-\\u001f\\u007f]+(?![\\s\\S])",
                    },
                    "label": _ref("fieldLabel"),
                },
                ("value", "label"),
            ),
            "reference": _object(
                {"node": _ref("nodeId"), "field": _ref("fieldKey")},
                ("node", "field"),
            ),
            "mapping": {
                "type": "object",
                "maxProperties": MAX_FIELDS,
                "propertyNames": _ref("fieldKey"),
                "additionalProperties": _ref("reference"),
            },
            "next": {
                "oneOf": [
                    _ref("nodeId"),
                    _object(
                        {
                            "cases": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": MAX_CASES,
                                "items": _object(
                                    {
                                        "when": {"oneOf": conditions},
                                        "to": _ref("nodeId"),
                                    },
                                    ("when", "to"),
                                ),
                            },
                            "default": _ref("nodeId"),
                        },
                        ("cases", "default"),
                    ),
                ]
            },
        },
    }

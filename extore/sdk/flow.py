"""Validated flow definitions and callbacks bound to one issued execution.

Definition version 1 and private-worker protocol version 2 are independent.
These helpers construct data, never evaluate expressions or execute a graph.
"""

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field

from ..task_flow_definition import (
    MAX_CASES,
    MAX_FIELDS,
    MAX_OPTIONS,
    validate_definition,
)
from .client import FlowScope, PrivateWorkerClient

_NODE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_FIELD = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")


def _identifier(value, pattern):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError("Invalid flow identifier")
    return value


def _message(value):
    if not isinstance(value, str) or len(value) > 1000:
        raise ValueError("Flow message must be text of at most 1000 characters")
    return value


@dataclass(frozen=True, init=False)
class FlowDefinition:
    """An immutable canonical graph, validated against its final product schema.

    Use ``from_dict`` for an existing JSON definition or ``from_nodes`` for a
    Python-built graph. Validation is the same semantic validator used when a
    product is saved; this does not issue cards or alter a running task.
    """

    _json: str = field(repr=False)

    def __init__(self, definition, *, product):
        canonical = validate_definition(definition, product)
        if canonical is None:
            raise ValueError("A flow definition is required")
        object.__setattr__(
            self,
            "_json",
            json.dumps(
                canonical,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ),
        )

    @classmethod
    def from_dict(cls, definition, *, product):
        return cls(definition, product=product)

    @classmethod
    def from_nodes(cls, entry, nodes, *, product):
        if not isinstance(nodes, (list, tuple)):
            raise ValueError("Flow nodes must be a list or tuple")
        return cls(
            {"version": 1, "entry": entry, "nodes": list(nodes)}, product=product
        )

    def as_dict(self):
        """Return a fresh copy suitable for the product's ``task_flow`` field."""
        return json.loads(self._json)

    def to_json(self):
        """Return canonical compact JSON text, without product credentials."""
        return self._json

    @staticmethod
    def schema():
        """Return the structural JSON Schema; cross-node checks need a product."""
        from ..task_flow_schema import definition_schema

        return deepcopy(definition_schema())

    @staticmethod
    def reference(node, field):
        return {
            "node": _identifier(node, _NODE),
            "field": _identifier(field, _FIELD),
        }

    @staticmethod
    def equals(node, field, value):
        if not isinstance(value, str):
            raise ValueError("Flow comparison values must be strings")
        return {
            "source": FlowDefinition.reference(node, field),
            "op": "eq",
            "value": value,
        }

    @staticmethod
    def one_of(node, field, values):
        if (
            not isinstance(values, (list, tuple))
            or not 1 <= len(values) <= MAX_OPTIONS
            or any(not isinstance(value, str) for value in values)
        ):
            raise ValueError("Flow comparison values must contain 1..50 strings")
        return {
            "source": FlowDefinition.reference(node, field),
            "op": "in",
            "value": list(values),
        }

    @staticmethod
    def exists(node, field):
        return {"source": FlowDefinition.reference(node, field), "op": "exists"}

    @staticmethod
    def branch(cases, *, default):
        """Construct ordered ``(condition, target)`` branches with a fallback.

        Conditions may come from ``equals``, ``one_of`` or ``exists``. The
        finished graph still undergoes schema, reference and type validation.
        """
        if not isinstance(cases, (list, tuple)) or not 1 <= len(cases) <= MAX_CASES:
            raise ValueError("Flow branches must contain 1..32 cases")
        result = {"cases": [], "default": _identifier(default, _NODE)}
        for case in cases:
            if not isinstance(case, (list, tuple)) or len(case) != 2:
                raise ValueError("A branch case must contain a condition and target")
            condition, target = case
            if not isinstance(condition, dict):
                raise ValueError("Invalid flow condition")
            result["cases"].append(
                {"when": deepcopy(condition), "to": _identifier(target, _NODE)}
            )
        return result


@dataclass(frozen=True)
class FlowExecution:
    """Callback conveniences for one exact, already-verified execution scope.

    Result IDs remain explicit: persist each ID and payload before delivery,
    then retry that pair unchanged. This object does not deduplicate business
    work, refresh deadlines, retry transport or grant additional permissions.
    """

    client: PrivateWorkerClient = field(repr=False)
    scope: FlowScope

    def __post_init__(self):
        if not isinstance(self.client, PrivateWorkerClient) or not isinstance(
            self.scope, FlowScope
        ):
            raise ValueError("Use a private-worker client and verified flow scope")

    def started(self, result_id, *, message=""):
        return self.client.update(
            self.scope,
            result_id,
            state="processing",
            progress=0,
            message=_message(message),
        )

    def progress(self, result_id, percent, *, message=""):
        if type(percent) is not int or not 0 <= percent <= 99:
            raise ValueError("Flow progress must be an integer from 0 to 99")
        return self.client.update(
            self.scope,
            result_id,
            state="processing",
            progress=percent,
            message=_message(message),
        )

    def succeed(self, result_id, output, *, message=""):
        if (
            not isinstance(output, Mapping)
            or len(output) > MAX_FIELDS
            or any(
                not isinstance(key, str)
                or not _FIELD.fullmatch(key)
                or not isinstance(value, str)
                for key, value in output.items()
            )
        ):
            raise ValueError("Flow output must map defined field names to strings")
        return self.client.update(
            self.scope,
            result_id,
            state="succeeded",
            output=dict(output),
            message=_message(message),
        )

    def fail(self, result_id, message, *, retryable=False):
        """Fail the step; ``retryable=True`` follows its configured failure edge.

        It does not directly reset the card. A nonretryable failure ends with
        merchant review because external work may already have had an effect.
        """
        if type(retryable) is not bool:
            raise ValueError("Flow retryable must be a boolean")
        return self.client.update(
            self.scope,
            result_id,
            state="failed",
            message=_message(message),
            retryable=retryable,
        )

    def upload(self, field, source, *, result_id):
        return self.client.upload(self.scope, field, source, result_id=result_id)

    def download(self, field, file_id):
        return self.client.download(self.scope, field, file_id)

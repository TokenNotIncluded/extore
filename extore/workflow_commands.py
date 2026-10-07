"""Offline workflow authoring commands; never initialize or contact a server."""

import json
import math
import os
import stat
import sys
from pathlib import Path

from .task_flow_definition import (
    MAX_DEFINITION_BYTES,
    MAX_SNAPSHOT_BYTES,
    MAX_TRANSITIONS,
    validate_definition,
)
from .task_flow_schema import definition_schema


class WorkflowInputError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def add_parser(commands):
    parent = commands.add_parser(
        "workflow", help="inspect and validate task-flow definitions offline"
    )
    children = parent.add_subparsers(dest="workflow_command", required=True)
    schema = children.add_parser("schema", help="print the public JSON Schema")
    validate = children.add_parser(
        "validate", help="validate and normalize a definition against product fields"
    )
    validate.add_argument(
        "--definition", type=Path, required=True, help="UTF-8 JSON file"
    )
    validate.add_argument(
        "--product",
        type=Path,
        required=True,
        help="JSON with mode, parameters and outputs",
    )
    for child in (schema, validate):
        child.add_argument(
            "--output", type=Path, help="create a private JSON file; never overwrite"
        )
    return parent


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("non-finite number")


def _float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite number")
    return result


def _bounded_depth(value):
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 64:
            raise WorkflowInputError("invalid_input")
        children = (
            item.values()
            if isinstance(item, dict)
            else item
            if isinstance(item, list)
            else ()
        )
        pending.extend((child, depth + 1) for child in children)
    return value


def _read_json(path, limit):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > limit:
            raise WorkflowInputError("invalid_input")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            raw = source.read(limit + 1)
        if len(raw) > limit:
            raise WorkflowInputError("invalid_input")
        # Decode explicitly: json.loads(bytes) also accepts UTF-16 and UTF-32.
        return _bounded_depth(
            json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_pairs,
                parse_constant=_constant,
                parse_float=_float,
            )
        )
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise WorkflowInputError("invalid_input") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def definition_summary(definition):
    counts = dict.fromkeys(("input", "process", "display", "end"), 0)
    for node in definition["nodes"] if definition else []:
        counts[node["kind"]] += 1
    return {
        "version": definition["version"] if definition else None,
        "entry": definition["entry"] if definition else None,
        "node_count": sum(counts.values()),
        "nodes": counts,
        "transition_limit": MAX_TRANSITIONS,
    }


def _emit(args, value):
    if args.output is not None:
        # Reuse the CLI's O_EXCL / O_NOFOLLOW, 0600 and fsync export behavior.
        from .manage_client import ManageError
        from .manage_commands import OutputFile

        try:
            with OutputFile(args, "workflow") as output:
                value = output.write(value)
        except ManageError as error:
            code = error.code if error.code == "output_exists" else "unsafe_output"
            raise WorkflowInputError(code) from None
    print(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")))


def run(args):
    try:
        if args.workflow_command == "schema":
            result = definition_schema()
        else:
            definition = _read_json(args.definition, MAX_DEFINITION_BYTES)
            source = _read_json(args.product, MAX_SNAPSHOT_BYTES)
            if not isinstance(source, dict):
                raise WorkflowInputError("invalid_input")
            product = {
                key: source[key]
                for key in ("mode", "parameters", "outputs")
                if key in source
            }
            try:
                canonical = validate_definition(definition, product)
            except (ValueError, TypeError, UnicodeError, RecursionError):
                raise WorkflowInputError("invalid_definition") from None
            result = {
                "ok": True,
                "definition": canonical,
                "summary": definition_summary(canonical),
            }
        _emit(args, result)
        return result
    except WorkflowInputError as error:
        code = error.code
    except (OSError, ValueError):
        code = "unsafe_output"
    messages = {
        "invalid_input": "Provide bounded UTF-8 JSON files without duplicate keys or non-finite numbers.",
        "invalid_definition": "Workflow definition is not valid for the supplied product fields.",
        "unsafe_output": "Cannot create a private workflow output file.",
        "output_exists": "Output already exists; choose a new file.",
    }
    print(
        json.dumps({"ok": False, "error": code, "message": messages[code]}),
        file=sys.stderr,
    )
    raise SystemExit(2)

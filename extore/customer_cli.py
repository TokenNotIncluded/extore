"""Remote customer operations without browser file pickers or merchant credentials."""

import getpass
import hashlib
import json
import math
import os
import re
import secrets
import stat
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from .manage_client import ManageError, _save_profile, origin_from_url

MAX_INPUT_BYTES = 250_000
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
PROFILE_VERSION = 1


def profile_path(value=None):
    configured = value or os.environ.get("EXTORE_CUSTOMER_CONFIG")
    if configured:
        return Path(configured).expanduser().absolute()
    root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return (root / "extore" / "customer.json").absolute()


def _regular_private(path, *, missing=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return
        raise ManageError("No saved customer receipts", code="no_receipt") from None
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise ManageError(
            "Customer credential files must be regular, owned by you, and mode 600",
            code="unsafe_profile",
        )


@contextmanager
def private_profile(path, *, create=False):
    import fcntl

    directory = path.parent
    # Do not follow a symlink in a credentials path, including its ancestors.
    for ancestor in (directory, *directory.parents):
        if ancestor.is_symlink():
            raise ManageError(
                "Credential paths cannot use symlinks", code="unsafe_profile"
            )
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        info = directory.lstat()
    except FileNotFoundError:
        raise ManageError("No saved customer receipts", code="no_receipt") from None
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise ManageError(
            "The customer credential directory must be owned by you and mode 700",
            code="unsafe_profile",
        )
    _regular_private(path, missing=create)
    lock = directory / (path.name + ".lock")
    _regular_private(lock, missing=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _regular_private(path, missing=create)
        if path.exists():
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "r") as source:
                if os.fstat(source.fileno()).st_size > MAX_RESPONSE_BYTES:
                    raise ManageError(
                        "Invalid customer profile", code="invalid_profile"
                    )
                data = json.load(source)
            if (
                not isinstance(data, dict)
                or data.get("version") != PROFILE_VERSION
                or not isinstance(data.get("receipts"), list)
                or any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("id"), str)
                    or not isinstance(item.get("token"), str)
                    or not isinstance(item.get("origin"), str)
                    for item in data["receipts"]
                )
            ):
                raise ManageError("Invalid customer profile", code="invalid_profile")
        else:
            data = {"version": PROFILE_VERSION, "receipts": []}
        before = json.dumps(data, sort_keys=True)
        yield data
        if not path.exists() or before != json.dumps(data, sort_keys=True):
            _save_profile(path, data)
    finally:
        os.close(fd)


def _object(value):
    if not isinstance(value, dict):
        raise ManageError("Invalid server response", code="invalid_response")
    return value


def _objects(value):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ManageError("Invalid server response", code="invalid_response")
    return value


def _product_shape(product):
    _object(product)
    if not isinstance(product.get("id"), str) or not isinstance(
        product.get("name"), str
    ):
        raise ManageError("Invalid product response", code="invalid_response")
    for key in ("parameters", "outputs"):
        for field in _objects(product.get(key, [])):
            if not isinstance(field.get("key"), str):
                raise ManageError(
                    "Invalid product field response", code="invalid_response"
                )
    _objects(product.get("variants", []))
    return product


def _job_shape(value):
    if value is not None:
        _object(value)
        if not isinstance(value.get("id"), str) or not isinstance(
            value.get("state"), str
        ):
            raise ManageError("Invalid task response", code="invalid_response")
        if "params" in value and (
            not isinstance(value["params"], dict)
            or any(
                not isinstance(key, str) or not isinstance(item, str)
                for key, item in value["params"].items()
            )
        ):
            raise ManageError("Invalid task inputs", code="invalid_response")
    return value


BATCH_STATUSES = frozenset({"valid", "used", "needs_retry", "invalid", "duplicate"})


def _batch_counts(items):
    return {
        "total": len(items),
        "accepted": sum(item["accepted"] for item in items),
        **{
            state: sum(item["status"] == state for item in items)
            for state in BATCH_STATUSES
        },
    }


def _receipt_shape(value):
    _object(value)
    if "batch" in value and type(value["batch"]) is not bool:
        raise ManageError("Invalid receipt response", code="invalid_response")
    if "partial" in value and type(value["partial"]) is not bool:
        raise ManageError("Invalid receipt response", code="invalid_response")
    partial = value.get("partial") is True
    if partial and value.get("batch") is not True:
        raise ManageError("Invalid partial receipt", code="invalid_response")
    if not partial:
        _product_shape(value.get("product"))
    if value.get("batch"):
        items = _objects(value.get("items"))
        if not 1 <= len(items) <= 30:
            raise ManageError("Invalid receipt items", code="invalid_response")
        indices, cards = set(), set()
        for item in items:
            if partial:
                index = item.get("index")
                if (
                    not {
                        "index",
                        "suffix",
                        "status",
                        "accepted",
                        "error",
                        "http_status",
                    }.issubset(item)
                    or type(index) is not int
                    or not 0 <= index < 30
                    or index in indices
                    or not isinstance(item.get("status"), str)
                    or item["status"] not in BATCH_STATUSES
                    or type(item.get("accepted")) is not bool
                    or not isinstance(item.get("suffix"), str)
                    or len(item["suffix"]) > 6
                    or not re.fullmatch(r"[A-Za-z0-9]*", item["suffix"])
                    or (
                        item.get("error") is not None
                        and not isinstance(item["error"], str)
                    )
                    or (
                        item.get("http_status") is not None
                        and (
                            type(item["http_status"]) is not int
                            or not 100 <= item["http_status"] <= 599
                        )
                    )
                ):
                    raise ManageError(
                        "Invalid partial receipt item", code="invalid_response"
                    )
                indices.add(index)
                if "duplicate_of" in item and (
                    type(item["duplicate_of"]) is not int
                    or not 0 <= item["duplicate_of"] < index
                ):
                    raise ManageError(
                        "Invalid duplicate receipt item", code="invalid_response"
                    )
                if not item["accepted"]:
                    if any(
                        key in item for key in ("card_id", "product", "variant", "job")
                    ):
                        raise ManageError(
                            "Invalid rejected receipt item", code="invalid_response"
                        )
                    continue
                if item["status"] not in ("valid", "used", "needs_retry"):
                    raise ManageError(
                        "Invalid accepted receipt item", code="invalid_response"
                    )
            if (
                not isinstance(item.get("card_id"), str)
                or not item["card_id"]
                or item["card_id"] in cards
            ):
                raise ManageError("Invalid receipt card", code="invalid_response")
            cards.add(item["card_id"])
            _product_shape(item.get("product"))
            _job_shape(item.get("job"))
            if item.get("variant") is not None:
                _object(item["variant"])
        if partial:
            summary = _object(value.get("summary"))
            if any(
                type(summary.get(key)) is not int or summary[key] != count
                for key, count in _batch_counts(items).items()
            ):
                raise ManageError("Invalid receipt counts", code="invalid_response")
    else:
        _job_shape(value.get("job"))
        if value.get("variant") is not None:
            _object(value["variant"])
    return value


FLOW_PHASES = frozenset(
    {"await_start", "input", "queued", "processing", "display", "ended"}
)
ATTACHMENT_TYPES = frozenset({"file", "image", "images"})


def _field_schema(field, *, detail=False):
    _object(field)
    if not isinstance(field.get("key"), str):
        raise ManageError("Invalid field schema", code="invalid_response")
    keys = ("key", "label", "type", "required", "options", "max_items", "sensitive")
    if detail:
        keys += ("description", "collapsed")
    return {key: field[key] for key in keys if key in field}


def _selected_flow(selected):
    row = selected.get("job") or {}
    product = selected.get("product") or {}
    flow = row.get("task_flow") or product.get("task_flow_view")
    if (
        flow is None
        and isinstance(product.get("task_flow"), dict)
        and product["task_flow"].get("enabled")
    ):
        flow = product["task_flow"]
    if flow is not None:
        _object(flow)
        if (
            flow.get("enabled") is not True
            or flow.get("phase") not in FLOW_PHASES
            or type(flow.get("flow_epoch")) is not int
            or flow["flow_epoch"] < 0
            or type(flow.get("revision")) is not int
            or flow["revision"] < 0
            or not isinstance(flow.get("current"), dict)
            or not isinstance(flow.get("actions", []), list)
            or any(
                action not in ("start", "answer", "continue", "restart")
                for action in flow.get("actions", [])
            )
        ):
            raise ManageError("Invalid task flow response", code="invalid_response")
    return flow


def _summary_flow(flow, *, content=False, detail=False):
    if flow is None:
        return None
    _selected_flow({"job": {"task_flow": flow}})
    result = {
        key: flow[key]
        for key in (
            "enabled",
            "version",
            "flow_epoch",
            "revision",
            "phase",
            "deadline",
            "server_time",
            "actions",
        )
        if key in flow
    }
    current = flow["current"]
    result["current"] = {
        key: current[key]
        for key in (
            "id",
            "kind",
            "label",
            "prompt",
            "question",
            "content",
            "start_policy",
        )
        if key in current
    }
    if "fields" in current:
        result["current"]["fields"] = [
            _field_schema(field, detail=detail) for field in _objects(current["fields"])
        ]
    shown = _objects(flow.get("shown", []))
    result["shown"] = [
        {
            key: item[key]
            for key in (
                ("key", "label", "type", "value")
                if content
                else ("key", "label", "type")
            )
            if key in item
        }
        for item in shown
    ]
    if content:
        for public, source in zip(result["shown"], shown, strict=True):
            if "files" in source:
                public["files"] = [
                    {
                        key: item[key]
                        for key in ("id", "field_key", "filename", "size", "mime")
                        if key in item
                    }
                    for item in _objects(source["files"])
                ]
    return result


def _attachment_paths(attachments, fields):
    attached = {}
    for specification in attachments:
        key, separator, path = specification.partition("=")
        if not separator or not key or not path or key not in fields:
            raise ManageError(
                "Use --file FIELD=PATH for a defined attachment field",
                code="invalid_input",
            )
        field = fields[key]
        if field.get("type") not in ATTACHMENT_TYPES:
            raise ManageError(
                "An attachment must target a file or image parameter",
                code="invalid_input",
            )
        paths = attached.setdefault(key, [])
        if paths and field.get("type") != "images":
            raise ManageError(
                "Use --file FIELD=PATH once per single-file field", code="invalid_input"
            )
        paths.append(Path(path).expanduser())
        if len(paths) > (
            field.get("max_items", 10) if field.get("type") == "images" else 1
        ):
            raise ManageError(
                "Too many files for this attachment field", code="invalid_input"
            )
    # All local paths are checked before the first upload begins.
    for paths in attached.values():
        for source in paths:
            info = source.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                raise ManageError(
                    "Attachment is not a regular file or exceeds 20 MiB",
                    code="invalid_upload",
                )
    return attached


def _flow_values(value, fields):
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ManageError("Step values must be a JSON object", code="invalid_input")
    if set(value) - fields.keys():
        raise ManageError(
            "Unknown step field; use customer flow view first", code="invalid_input"
        )
    result = {}
    for key, supplied in value.items():
        kind = fields[key].get("type", "text")
        if kind == "boolean" and type(supplied) is bool:
            supplied = "true" if supplied else "false"
        elif kind == "number" and type(supplied) in (int, float):
            if type(supplied) is float and not math.isfinite(supplied):
                raise ManageError("Step numbers must be finite", code="invalid_input")
            supplied = str(supplied)
        elif (
            kind == "images"
            and isinstance(supplied, list)
            and all(isinstance(item, str) for item in supplied)
        ):
            supplied = json.dumps(supplied, separators=(",", ":"))
        if not isinstance(supplied, str) or len(supplied) > 10000:
            raise ManageError(
                "Step values must match their defined field types", code="invalid_input"
            )
        if kind == "boolean" and supplied not in ("", "true", "false"):
            raise ManageError(
                "A boolean field accepts true or false", code="invalid_input"
            )
        if (
            kind == "select"
            and supplied
            and supplied
            not in {item["value"] for item in fields[key].get("options", [])}
        ):
            raise ManageError("Choose a defined option value", code="invalid_input")
        if kind == "images" and supplied:
            try:
                ids = json.loads(supplied)
            except ValueError:
                raise ManageError(
                    "An image collection needs a JSON array of file IDs",
                    code="invalid_input",
                ) from None
            if (
                not isinstance(ids, list)
                or any(not isinstance(item, str) for item in ids)
                or len(set(ids)) != len(ids)
                or len(ids) > fields[key].get("max_items", 10)
            ):
                raise ManageError(
                    "Invalid or oversized image collection", code="invalid_input"
                )
            supplied = json.dumps(ids, separators=(",", ":"))
        result[key] = supplied
    return result


def _summary_product(product):
    _product_shape(product)
    result = {
        key: product[key]
        for key in ("id", "name", "logo", "mode", "delivery", "revision_policy")
        if key in product
    }
    if "variants" in product:
        result["variants"] = [
            {
                key: variant[key]
                for key in ("id", "name", "price", "currency", "enabled")
                if key in variant
            }
            for variant in product["variants"]
        ]
    flow = _selected_flow({"product": product})
    if flow is not None:
        result["task_flow"] = _summary_flow(flow)
    return result


def _summary_job(job):
    _job_shape(job)
    if job is None:
        return None
    result = {
        key: job[key]
        for key in (
            "id",
            "product_id",
            "state",
            "message",
            "progress",
            "attempt",
            "queue_position",
            "queue_ahead",
            "can_retry",
            "retry_mode",
            "retry_reason_type",
            "revealed",
            "steps",
            "completed_steps",
            "updated",
            "card_attributes",
            "entitlements",
            "revision",
            "deliveries",
            "last_delivery",
        )
        if key in job
    }
    flow = _selected_flow({"job": job})
    if flow is not None:
        result["task_flow"] = _summary_flow(flow)
    return result


def _summary_variant(value):
    if value is None:
        return None
    _object(value)
    return {
        key: value[key] for key in ("id", "name", "price", "currency") if key in value
    }


def _card_metadata(value):
    """Keep merchant-owned benefits and delivery history, never raw inputs/output."""
    return {
        key: value[key]
        for key in ("card_attributes", "entitlements", "deliveries", "last_delivery")
        if key in value
    }


def _safe_batch_error(error):
    # Error text is not a trusted public field; it may contain a credential or
    # customer input from a downstream handler. Preserve status, not its body.
    return (
        "This card or its inputs could not be processed" if error is not None else None
    )


def _summary_receipt(value, *, allowed_card_ids=None):
    _receipt_shape(value)
    if value.get("batch"):
        partial = value.get("partial") is True
        result = {"batch": True, "items": []}
        if partial:
            result.update(partial=True, summary=_batch_counts(value["items"]))
        else:
            result["product"] = _summary_product(value["product"])
        for item in value["items"]:
            rendered = {"suffix": item.get("suffix", "")}
            if partial:
                rendered.update(
                    {
                        key: item[key]
                        for key in ("index", "status", "accepted", "http_status")
                    }
                )
                rendered["error"] = _safe_batch_error(item.get("error"))
                if "duplicate_of" in item:
                    rendered["duplicate_of"] = item["duplicate_of"]
            if item.get("accepted") is not False:
                rendered.update(
                    card_id=item["card_id"],
                    product=_summary_product(item["product"]),
                    variant=_summary_variant(item.get("variant")),
                    job=_summary_job(item.get("job")),
                )
                rendered.update(_card_metadata(item))
            result["items"].append(rendered)
        if "results" in value:
            rows = _objects(value["results"])
            if not 1 <= len(rows) <= 30:
                raise ManageError("Invalid submission results", code="invalid_response")
            owned = (
                set(allowed_card_ids)
                if allowed_card_ids is not None
                else {
                    item["card_id"]
                    for item in value["items"]
                    if item.get("accepted") is not False
                }
            )
            indices = set()
            clean = []
            for row in rows:
                if (
                    type(row.get("index")) is not int
                    or not 0 <= row["index"] < 30
                    or row["index"] in indices
                    or not isinstance(row.get("status"), str)
                    or row["status"]
                    not in ("submitted", "unchanged", "error", "duplicate")
                    or type(row.get("http_status")) is not int
                    or not 100 <= row["http_status"] <= 599
                    or (
                        "card_id" in row
                        and (
                            not isinstance(row["card_id"], str)
                            or row["card_id"] not in owned
                        )
                    )
                ):
                    raise ManageError(
                        "Invalid submission result", code="invalid_response"
                    )
                indices.add(row["index"])
                rendered = {key: row[key] for key in ("index", "status", "http_status")}
                rendered["error"] = _safe_batch_error(row.get("error"))
                if "card_id" in row:
                    rendered["card_id"] = row["card_id"]
                if row["status"] in ("submitted", "unchanged"):
                    if "card_id" not in row or row.get("job") is None:
                        raise ManageError(
                            "Invalid successful submission", code="invalid_response"
                        )
                    _job_shape(row["job"])
                    rendered["job"] = _summary_job(row["job"])
                elif row.get("job") is not None:
                    raise ManageError(
                        "Invalid failed submission", code="invalid_response"
                    )
                clean.append(rendered)
            counts = {
                "total": len(rows),
                "succeeded": sum(
                    row["status"] in ("submitted", "unchanged") for row in rows
                ),
            }
            counts["failed"] = counts["total"] - counts["succeeded"]
            supplied = _object(value.get("submission_summary"))
            if any(
                type(supplied.get(key)) is not int or supplied[key] != count
                for key, count in counts.items()
            ):
                raise ManageError("Invalid submission counts", code="invalid_response")
            result.update(results=clean, submission_summary=counts)
        return result
    return {
        "batch": False,
        "product": _summary_product(value["product"]),
        "variant": _summary_variant(value.get("variant")),
        "job": _summary_job(value.get("job")),
        **_card_metadata(value),
    }


def _batch_groups(value, *, detail=False):
    _receipt_shape(value)
    groups = {}
    for item in value.get("items", []):
        if item.get("accepted") is False:
            continue
        flow = _selected_flow(item)
        fields = [] if flow else item["product"].get("parameters", [])
        defaults = {
            field["key"]: (item.get("job") or {})
            .get("params", {})
            .get(field["key"], "")
            for field in fields
            if field.get("type") not in ATTACHMENT_TYPES
        }
        signature = json.dumps(
            [
                item["product"]["id"],
                fields,
                defaults,
                (flow or {}).get("definition_hash"),
            ],
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        groups.setdefault(signature, []).append(item)
    result = []
    for grouped in groups.values():
        item = grouped[0]
        flow = _selected_flow(item)
        fields = [] if flow else item["product"].get("parameters", [])
        cards = [row["card_id"] for row in grouped]
        # Do not hash saved private inputs into the exposed ID (dictionary attack).
        public_key = json.dumps(
            [
                item["product"]["id"],
                sorted(cards),
                fields,
                (flow or {}).get("definition_hash"),
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        result.append(
            {
                "group_id": "grp_"
                + hashlib.sha256(public_key.encode()).hexdigest()[:24],
                "card_ids": cards,
                "product": _schema(item["product"], detail),
                "shared_parameters": [
                    _field_schema(field, detail=detail)
                    for field in fields
                    if field.get("type") not in ATTACHMENT_TYPES
                ],
                "separate_files": [
                    _field_schema(field, detail=detail)
                    for field in fields
                    if field.get("type") in ATTACHMENT_TYPES
                ],
                "flow": bool(flow),
                "variants": [
                    {
                        "card_id": row["card_id"],
                        "variant": _summary_variant(row.get("variant")),
                    }
                    for row in grouped
                ],
            }
        )
    return result


def _selected_group(value, group_id, *, detail=False):
    group = next(
        (
            row
            for row in _batch_groups(value, detail=detail)
            if row["group_id"] == group_id
        ),
        None,
    )
    if group is None:
        raise ManageError(
            "Group changed or is not in this receipt; refresh customer schema",
            code="invalid_group",
        )
    return group


def _schema(product, detail=False):
    if detail:
        return product
    return {
        **_summary_product(product),
        "parameters": [_field_schema(field) for field in product.get("parameters", [])],
        "outputs": [_field_schema(field) for field in product.get("outputs", [])],
    }


def _read_json(path=None):
    if path is None:
        source = getattr(sys.stdin, "buffer", sys.stdin)
        raw = source.read(MAX_INPUT_BYTES + 1)
    else:
        with path.open("rb") as source:
            raw = source.read(MAX_INPUT_BYTES + 1)
    if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > MAX_INPUT_BYTES:
        raise ManageError("Command input is too large", code="invalid_input")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise ManageError(
            "Command input must be valid JSON", code="invalid_input"
        ) from None


def _read_revision_message(path=None):
    source = (
        getattr(sys.stdin, "buffer", sys.stdin) if path is None else path.open("rb")
    )
    try:
        raw = source.read(40_001)
    finally:
        if path is not None:
            source.close()
    try:
        message = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    except UnicodeError:
        raise ManageError(
            "Revision message must be UTF-8 text", code="invalid_input"
        ) from None
    if not isinstance(message, str) or not message.strip() or len(message) > 10_000:
        raise ManageError(
            "Revision message must contain 1..10000 characters", code="invalid_input"
        )
    return message


def _revision_number(value):
    import argparse

    if not re.fullmatch(r"[0-9]+", value) or len(value) > 4 or int(value) > 1000:
        raise argparse.ArgumentTypeError("revision must be an integer from 0 to 1000")
    return int(value)


def _revision_request_id(value):
    import argparse

    try:
        parsed = uuid.UUID(value)
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise argparse.ArgumentTypeError(
            "request ID must be a canonical UUID v4"
        ) from None
    return value


def _params(value):
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise ManageError(
            "Parameters must be a JSON object of strings", code="invalid_input"
        )
    return value


def _validate_params(value, product, *, attached=()):
    fields = {field["key"]: field for field in product.get("parameters", [])}
    if set(value) - fields.keys() or set(attached) - fields.keys():
        raise ManageError(
            "Unknown parameter; use customer schema first", code="invalid_input"
        )
    if any(fields[key].get("type") not in ATTACHMENT_TYPES for key in attached):
        raise ManageError(
            "An attachment must target a file or image parameter", code="invalid_input"
        )
    for key, field in fields.items():
        if (
            field.get("required", True)
            and (
                not value.get(key, "").strip()
                or (
                    field.get("type") == "images" and value.get(key, "").strip() == "[]"
                )
            )
            and key not in attached
        ):
            raise ManageError(
                f"Required parameter is missing: {key}", code="invalid_input"
            )


def _exchange_groups(client, origin, text):
    """Resolve signed destinations locally; no routed secret is sent to A."""
    from .proxy_routes import is_routed_code

    whole = re.sub(r"[-\s]", "", text).upper()
    codes = (
        [text]
        if re.fullmatch(r"[A-Z2-7]{32}", whole)
        else [part for part in re.split(r"[\s,，;；]+", text) if part]
    )
    if not 1 <= len(codes) <= 30:
        raise ManageError("Provide 1 to 30 codes", code="invalid_input")
    routed = [code for code in codes if is_routed_code(code)]
    routes = {}
    parsed = {}
    if routed:
        try:
            from .proxy_routes import (
                canonical_origin,
                canonical_path,
                parse_routed_code,
                verify_routed_code,
            )
        except ImportError:
            raise ManageError(
                "Routed code support is unavailable; update Extore CLI",
                code="unsupported_route",
            ) from None
        # Reject unknown protocol versions and malformed signatures before
        # contacting A, even for its public metadata.
        for code in routed:
            try:
                parsed[code] = parse_routed_code(code)
            except ValueError:
                raise ManageError(
                    "Invalid or unsupported routed code", code="invalid_route"
                ) from None
        metadata = _objects(client.json(origin, "GET", "/api/proxy/routes"))
        if len(metadata) > 1000:
            raise ManageError("Too many routed destinations", code="invalid_response")
        for row in metadata:
            route_id = row.get("route_id")
            if not isinstance(route_id, str) or route_id in routes:
                raise ManageError(
                    "Invalid routed destination metadata", code="invalid_response"
                )
            routes[route_id] = {
                key: row[key]
                for key in ("route_id", "issuer_id", "origin", "path", "public_key")
                if key in row
            }
    groups = {}
    for code in codes:
        destination = origin
        if code in parsed:
            route = routes.get(parsed[code]["route_id"])
            if route is None:
                raise ManageError(
                    "Routed destination is unavailable; contact the merchant",
                    code="unknown_route",
                )
            try:
                verify_routed_code(code, route)
                destination = canonical_origin(route["origin"])
                if canonical_path(route["path"]) != "/":
                    raise ValueError("unsupported mount")
            except ValueError:
                raise ManageError(
                    "Routed destination or signature is invalid", code="invalid_route"
                ) from None
        groups.setdefault(destination, []).append(code)
    return groups


def _private_output(target):
    if target.exists() or target.is_symlink():
        raise ManageError(
            "Output already exists; choose another path", code="output_exists"
        )
    return os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)


class CustomerClient:
    def __init__(self, data=None, *, transport=None, persist=None, proxy_settings=None):
        from .http_proxy import make_client

        self.data = data
        self.persist = persist or (lambda: None)
        self.upload_limits = {}
        self.http = make_client(
            proxy_settings,
            transport=transport,
            timeout=httpx.Timeout(120, connect=15),
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.http.close()

    def request(self, origin, method, path, **kwargs):
        origin = origin_from_url(origin)
        request = self.http.build_request(
            method, origin + path, headers={"Origin": origin}, **kwargs
        )
        # Public customer requests never inherit a browser cookie or merchant bearer.
        request.headers.pop("Cookie", None)
        request.headers.pop("Authorization", None)
        try:
            response = self.http.send(request, stream=True)
        except httpx.HTTPError:
            raise ManageError(
                "Cannot connect to Extore", code="connection_error"
            ) from None
        if not 200 <= response.status_code < 300:
            status = response.status_code
            response.close()
            messages = {
                400: "Invalid customer input; check the receipt and product schema",
                403: "This receipt does not permit the operation",
                404: "Code, receipt, product or file is unavailable or expired",
                409: "The task is not ready for this operation; refresh its status",
                410: "This code or delivery was used, rejected, revoked or destroyed",
                413: "Upload exceeds the server's file limit",
                422: "Invalid customer input; check the product schema",
                429: "Too many requests; wait before retrying",
                507: "Server storage is full; contact the merchant",
            }
            raise ManageError(
                messages.get(status, "The server rejected this customer operation"),
                code="http_error",
                status=status,
            )
        return response

    def json(self, origin, method, path, **kwargs):
        response = self.request(origin, method, path, **kwargs)
        try:
            chunks = []
            size = 0
            for chunk in response.iter_bytes(64 * 1024):
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ManageError(
                        "Server response is too large", code="invalid_response"
                    )
                chunks.append(chunk)
            return json.loads(b"".join(chunks))
        except httpx.HTTPError:
            raise ManageError(
                "Server response was interrupted", code="connection_error"
            ) from None
        except ValueError:
            raise ManageError(
                "Invalid server JSON response", code="invalid_response"
            ) from None
        finally:
            response.close()

    def saved(self, receipt_id):
        result = next(
            (item for item in self.data["receipts"] if item["id"] == receipt_id), None
        )
        if result is None:
            raise ManageError("Unknown local receipt ID", code="no_receipt")
        origin_from_url(result["origin"])
        return result

    def remember(self, origin, token, value):
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 100
            or any(ord(char) < 33 or ord(char) > 126 for char in token)
        ):
            raise ManageError("Invalid receipt credential", code="invalid_response")
        entry = next(
            (
                item
                for item in self.data["receipts"]
                if item["origin"] == origin and item["token"] == token
            ),
            None,
        )
        if entry is None:
            entry = {
                "id": "rcpt_" + secrets.token_urlsafe(12),
                "origin": origin,
                "token": token,
                "created": time.time(),
                "files": {},
            }
            self.data["receipts"].append(entry)
        entry["status"] = _summary_receipt(value)
        self.persist()
        return {
            "ok": True,
            "receipt_id": entry["id"],
            "origin": origin,
            **entry["status"],
        }

    def receipt(self, entry):
        value = _object(
            self.json(
                entry["origin"],
                "POST",
                "/api/batch/receipt"
                if entry.get("status", {}).get("partial")
                else "/api/receipt",
                json={"token": entry["token"]},
            )
        )
        entry["status"] = _summary_receipt(value)
        return value

    def select(self, value, card_id=None):
        _receipt_shape(value)
        if value.get("batch"):
            if not card_id:
                raise ManageError(
                    "Select --card for this batch receipt", code="card_required"
                )
            item = next(
                (
                    item
                    for item in value["items"]
                    if item.get("accepted") is not False
                    and item.get("card_id") == card_id
                ),
                None,
            )
            if item is None:
                raise ManageError("Card is not in this receipt", code="invalid_card")
            return item
        if card_id:
            raise ManageError(
                "--card is only used with a batch receipt", code="invalid_card"
            )
        return value

    def upload_limit(self, origin):
        if origin not in self.upload_limits:
            limits = _object(self.json(origin, "GET", "/api/upload-limits"))
            limit = limits.get("max_file_bytes")
            if type(limit) is not int or limit < 1:
                raise ManageError(
                    "Invalid server upload limit", code="invalid_response"
                )
            self.upload_limits[origin] = min(limit, MAX_FILE_BYTES)
        return self.upload_limits[origin]

    def preflight_uploads(self, origin, attached):
        if not attached:
            return
        limit = self.upload_limit(origin)
        for sources in attached.values():
            for source in sources:
                info = source.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                    raise ManageError(
                        f"Upload must be a regular file no larger than {limit} bytes",
                        code="invalid_upload",
                    )

    def upload(self, entry, card_id, field, source, *, value=None):
        value = value or self.receipt(entry)
        selected = self.select(value, card_id)
        flow = _selected_flow(selected)
        definitions = (
            flow["current"].get("fields", [])
            if flow
            else selected["product"].get("parameters", [])
        )
        fields = {item["key"]: item for item in definitions}
        if field not in fields or fields[field].get("type") not in ATTACHMENT_TYPES:
            raise ManageError(
                "Select a file or image parameter from customer schema",
                code="invalid_input",
            )
        row = selected.get("job")
        if (flow and flow.get("phase") != "input") or (
            not flow
            and row
            and (
                row.get("state") not in ("failed", "needs_input")
                or not row.get("can_retry")
            )
        ):
            raise ManageError(
                "This task cannot accept new input files", code="invalid_state"
            )
        limit = self.upload_limit(entry["origin"])
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ManageError(
                f"Upload must be a regular file no larger than {limit} bytes",
                code="invalid_upload",
            )
        data = {"token": entry["token"], "field_key": field}
        if flow:
            data.update(
                flow_epoch=flow["flow_epoch"],
                expected_revision=flow["revision"],
                node_id=flow["current"]["id"],
            )
        if card_id:
            data["card_id"] = card_id
        with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), "rb") as upload:
            result = _object(
                self.json(
                    entry["origin"],
                    "POST",
                    "/api/files/upload",
                    data=data,
                    files={"file": (source.name, upload, "application/octet-stream")},
                )
            )
        descriptor = {
            key: result[key]
            for key in ("id", "field_key", "filename", "size", "kind")
            if key in result
        }
        if not isinstance(descriptor.get("id"), str) or not descriptor["id"]:
            raise ManageError("Invalid uploaded file response", code="invalid_response")
        cached = entry.setdefault("inputs", {}).setdefault(card_id or "single", {})
        if fields[field].get("type") == "images":
            previous = cached.get(field, {})
            epoch = flow["flow_epoch"] if flow else None
            descriptors = (
                previous.get("files", []) if previous.get("flow_epoch") == epoch else []
            )
            cached[field] = {
                "field_key": field,
                "type": "images",
                "flow_epoch": epoch,
                "files": [*descriptors, descriptor],
            }
        else:
            cached[field] = descriptor
            if flow:
                descriptor["flow_epoch"] = flow["flow_epoch"]
        self.persist()
        return {"ok": True, "receipt_id": entry["id"], "field": field, **descriptor}

    def redeem_partial(
        self,
        entry,
        value,
        params,
        *,
        card_id=None,
        group_id=None,
        items=None,
        attachments=(),
        card_attachments=(),
        retry=False,
    ):
        if not value.get("batch"):
            raise ManageError(
                "Groups and per-card files require a batch receipt",
                code="invalid_input",
            )
        if items is not None:
            if (
                card_id
                or group_id
                or attachments
                or not isinstance(items, list)
                or not 1 <= len(items) <= 30
            ):
                raise ManageError(
                    "--items-file requires 1 to 30 batch items without --card, --group or --file",
                    code="invalid_input",
                )
            requested = []
            for item in items:
                if (
                    not isinstance(item, dict)
                    or set(item) != {"card_id", "params"}
                    or not isinstance(item["card_id"], str)
                ):
                    raise ManageError(
                        "Each batch item needs card_id and params", code="invalid_input"
                    )
                requested.append(
                    {
                        "card_id": item["card_id"],
                        "params": dict(_params(item["params"])),
                    }
                )
        elif group_id:
            if card_id or attachments:
                raise ManageError(
                    "--group cannot be combined with --card or --file; use --card-file",
                    code="invalid_input",
                )
            group = _selected_group(value, group_id)
            supplied = _params(params)
            if set(supplied) & {field["key"] for field in group["separate_files"]}:
                raise ManageError(
                    "Shared parameters cannot include file or image fields; use --card-file",
                    code="invalid_input",
                )
            requested = [
                {"card_id": selected, "params": dict(supplied)}
                for selected in group["card_ids"]
            ]
        else:
            self.select(value, card_id)
            requested = [{"card_id": card_id, "params": dict(_params(params))}]
            if card_attachments:
                raise ManageError(
                    "--card-file requires --group or --items-file", code="invalid_input"
                )
        owned_ids = {
            item["card_id"]
            for item in value["items"]
            if item.get("accepted") is not False
        }
        selected_ids = {item["card_id"] for item in requested}
        specifications = {}
        for specification in card_attachments:
            selected, separator, field_path = specification.partition(":")
            if not separator or selected not in selected_ids:
                raise ManageError(
                    "Use --card-file CARD:FIELD=PATH for a selected card",
                    code="invalid_input",
                )
            specifications.setdefault(selected, []).append(field_path)
        if attachments:
            specifications[card_id] = list(attachments)
        attached_by_card = {}
        for selected, paths in specifications.items():
            item = self.select(value, selected)
            if _selected_flow(item):
                raise ManageError(
                    "Prepare task flows with empty inputs; upload at customer flow answer",
                    code="invalid_input",
                )
            fields = {
                field["key"]: field for field in item["product"].get("parameters", [])
            }
            attached_by_card[selected] = _attachment_paths(paths, fields)
        self.preflight_uploads(
            entry["origin"],
            {
                f"{selected}:{field}": paths
                for selected, attached in attached_by_card.items()
                for field, paths in attached.items()
            },
        )
        prepared, original_indices, local_errors = [], [], []
        seen = set()
        for index, item in enumerate(requested):
            selected = None
            supplied = item["params"]
            attached = attached_by_card.get(item["card_id"], {})
            try:
                selected = self.select(value, item["card_id"])
                if item["card_id"] in seen:
                    local_errors.append(
                        {
                            "index": index,
                            "card_id": item["card_id"],
                            "status": "duplicate",
                            "error": "Duplicate card",
                            "http_status": 400,
                        }
                    )
                    continue
                seen.add(item["card_id"])
                if retry:
                    self._submit_state(selected, True)
                flow = _selected_flow(selected)
                if flow:
                    if supplied or attached:
                        raise ManageError(
                            "Prepare task flows with empty inputs; use customer flow start separately",
                            code="invalid_input",
                        )
                elif not selected.get("job") or selected["job"].get("can_retry"):
                    # Only this card's private upload cache can fill its file fields.
                    fields = {
                        field["key"]: field
                        for field in selected["product"].get("parameters", [])
                    }
                    for field, cached in (
                        entry.get("inputs", {}).get(item["card_id"], {}).items()
                    ):
                        if (
                            field not in supplied
                            and field in fields
                            and fields[field].get("type") in ATTACHMENT_TYPES
                        ):
                            ids = (
                                [row["id"] for row in cached.get("files", [])]
                                if cached.get("type") == "images"
                                else [cached.get("id")]
                            )
                            if ids and all(isinstance(file_id, str) for file_id in ids):
                                supplied[field] = (
                                    json.dumps(ids, separators=(",", ":"))
                                    if fields[field].get("type") == "images"
                                    else ids[0]
                                )
                    _validate_params(supplied, selected["product"], attached=attached)
                    for field, paths in attached.items():
                        ids = [
                            self.upload(
                                entry, item["card_id"], field, path, value=value
                            )["id"]
                            for path in paths
                        ]
                        supplied[field] = (
                            json.dumps(ids, separators=(",", ":"))
                            if fields[field].get("type") == "images"
                            else ids[0]
                        )
                prepared.append(item)
                original_indices.append(index)
            except ManageError as error:
                if error.code == "invalid_response":
                    raise
                local_errors.append(
                    {
                        "index": index,
                        "status": "error",
                        "error": "Inputs or attachments could not be submitted",
                        "http_status": error.status
                        or (404 if error.code == "invalid_card" else 400),
                        **({"card_id": item["card_id"]} if selected else {}),
                    }
                )
        response = dict(value)
        combined = []
        # Shared input may grow thirtyfold when expanded per card. Keep every
        # request below the API body limit without making the caller split it.
        chunks, chunk = [], []
        for position, item in enumerate(prepared):
            candidate = chunk + [(position, item)]
            encoded = json.dumps(
                {"token": entry["token"], "items": [row for _, row in candidate]},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > MAX_INPUT_BYTES:
                if not chunk:
                    raise ManageError(
                        "One card's inputs exceed the API request limit",
                        code="invalid_input",
                    )
                chunks.append(chunk)
                chunk = [(position, item)]
                single = json.dumps(
                    {"token": entry["token"], "items": [item]},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                if len(single) > MAX_INPUT_BYTES:
                    raise ManageError(
                        "One card's inputs exceed the API request limit",
                        code="invalid_input",
                    )
            else:
                chunk = candidate
        if chunk:
            chunks.append(chunk)
        for chunk in chunks:
            try:
                current = _object(
                    self.json(
                        entry["origin"],
                        "POST",
                        "/api/batch/redeem",
                        json={
                            "token": entry["token"],
                            "items": [item for _, item in chunk],
                        },
                    )
                )
                # Validate before mapping response indices or persisting status.
                _summary_receipt(current, allowed_card_ids=owned_ids)
                rows = current["results"]
                if len(rows) != len(chunk) or {row["index"] for row in rows} != set(
                    range(len(chunk))
                ):
                    raise ManageError(
                        "Invalid submission result count", code="invalid_response"
                    )
                for row in rows:
                    position, submitted = chunk[row["index"]]
                    if "card_id" in row and row["card_id"] != submitted["card_id"]:
                        raise ManageError(
                            "Invalid submission card", code="invalid_response"
                        )
                    row["index"] = original_indices[position]
                response = current
                combined.extend(rows)
            except ManageError as error:
                if error.code == "invalid_response":
                    raise
                combined.extend(
                    {
                        "index": original_indices[position],
                        "card_id": item["card_id"],
                        "status": "error",
                        "error": "This submission could not be confirmed; refresh before retrying",
                        "http_status": error.status or 503,
                    }
                    for position, item in chunk
                )
        response["results"] = combined
        response["results"] = sorted(
            response["results"] + local_errors, key=lambda row: row["index"]
        )
        successes = sum(
            row["status"] in ("submitted", "unchanged") for row in response["results"]
        )
        response["submission_summary"] = {
            "total": len(requested),
            "succeeded": successes,
            "failed": len(requested) - successes,
        }
        entry["status"] = _summary_receipt(response, allowed_card_ids=owned_ids)
        return {
            "ok": successes == len(requested),
            "receipt_id": entry["id"],
            **entry["status"],
        }

    def redeem(
        self,
        entry,
        params,
        *,
        card_id=None,
        items=None,
        attachments=(),
        retry=False,
        group_id=None,
        card_attachments=(),
    ):
        value = self.receipt(entry)
        if value.get("partial") or group_id or card_attachments:
            return self.redeem_partial(
                entry,
                value,
                params,
                card_id=card_id,
                group_id=group_id,
                items=items,
                attachments=attachments,
                card_attachments=card_attachments,
                retry=retry,
            )
        body = {"token": entry["token"]}
        if items is not None:
            if not value.get("batch") or card_id or attachments:
                raise ManageError(
                    "--items-file requires a batch receipt without --card or --file",
                    code="invalid_input",
                )
            if not isinstance(items, list) or not 1 <= len(items) <= 30:
                raise ManageError(
                    "Batch input must contain 1 to 30 items", code="invalid_input"
                )
            seen = set()
            for item in items:
                if (
                    not isinstance(item, dict)
                    or set(item) != {"card_id", "params"}
                    or not isinstance(item["card_id"], str)
                    or item["card_id"] in seen
                ):
                    raise ManageError(
                        "Each batch item needs a unique card_id and params",
                        code="invalid_input",
                    )
                seen.add(item["card_id"])
                selected = self.select(value, item["card_id"])
                self._submit_state(selected, retry)
                if _selected_flow(selected):
                    raise ManageError(
                        "Start each task flow explicitly with customer flow start",
                        code="invalid_state",
                    )
                _validate_params(_params(item["params"]), selected["product"])
            body["items"] = items
        else:
            selected = self.select(value, card_id)
            self._submit_state(selected, retry)
            params = _params(params)
            if _selected_flow(selected):
                raise ManageError(
                    "This product uses a task flow; use customer flow start or answer",
                    code="invalid_state",
                )
            fields = {
                field["key"]: field
                for field in selected["product"].get("parameters", [])
            }
            attached = _attachment_paths(attachments, fields)
            _validate_params(params, selected["product"], attached=attached)
            self.preflight_uploads(entry["origin"], attached)
            for field, sources in attached.items():
                ids = [
                    self.upload(entry, card_id, field, source, value=value)["id"]
                    for source in sources
                ]
                params[field] = (
                    json.dumps(ids, separators=(",", ":"))
                    if fields[field].get("type") == "images"
                    else ids[0]
                )
            if value.get("batch"):
                body["items"] = [{"card_id": card_id, "params": params}]
            else:
                body["params"] = params
        result = _object(self.json(entry["origin"], "POST", "/api/redeem", json=body))
        if not result.get("batch"):
            result = {**value, "job": result}
        entry["status"] = _summary_receipt(result)
        return {"ok": True, "receipt_id": entry["id"], **entry["status"]}

    def flow(
        self,
        entry,
        action,
        *,
        card_id=None,
        values=None,
        attachments=(),
        flow_epoch=None,
        expected_revision=None,
        target=None,
        detail=False,
    ):
        receipt = self.receipt(entry)
        selected = self.select(receipt, card_id)
        flow = _selected_flow(selected)
        if flow is None:
            raise ManageError(
                "This receipt uses simple redemption, not a task flow",
                code="invalid_state",
            )
        if action == "view":
            result = _summary_flow(flow, content=True, detail=detail)
            if target is not None:
                fd = _private_output(target)
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(result, output, ensure_ascii=False, separators=(",", ":"))
                    output.write("\n")
                    output.flush()
                    os.fsync(output.fileno())
                return {
                    "ok": True,
                    "receipt_id": entry["id"],
                    "output": str(target),
                    "flow_epoch": flow["flow_epoch"],
                    "phase": flow["phase"],
                }
            return {
                "ok": True,
                "receipt_id": entry["id"],
                "card_id": card_id,
                "task_flow": result,
            }
        can_restart = (
            action == "restart"
            and flow.get("phase") == "ended"
            and (selected.get("job") or {}).get("state") in ("failed", "needs_input")
            and (selected.get("job") or {}).get("can_retry") is True
        )
        can_cancel = action == "cancel" and flow.get("phase") != "ended"
        if not can_restart and not can_cancel and action not in flow.get("actions", []):
            raise ManageError(
                "The current step does not accept this action; use customer flow view",
                code="invalid_state",
            )
        if (flow_epoch is not None and flow_epoch != flow["flow_epoch"]) or (
            expected_revision is not None and expected_revision != flow["revision"]
        ):
            raise ManageError(
                "The step changed; refresh its epoch and revision before continuing",
                code="stale_flow",
                status=409,
            )
        body = {
            "token": entry["token"],
            "flow_epoch": flow["flow_epoch"],
            "expected_revision": flow["revision"],
        }
        if card_id:
            body["card_id"] = card_id
        if action == "answer":
            fields = {
                field["key"]: field
                for field in _objects(flow["current"].get("fields", []))
            }
            params = _flow_values({} if values is None else values, fields)
            attached = _attachment_paths(attachments, fields)
            _validate_params(
                params, {"parameters": list(fields.values())}, attached=attached
            )
            self.preflight_uploads(entry["origin"], attached)
            for field, sources in attached.items():
                ids = [
                    self.upload(entry, card_id, field, source, value=receipt)["id"]
                    for source in sources
                ]
                params[field] = (
                    json.dumps(ids, separators=(",", ":"))
                    if fields[field].get("type") == "images"
                    else ids[0]
                )
            body["values"] = params
        response = _object(
            self.json(entry["origin"], "POST", "/api/task-flow/" + action, json=body)
        )
        row = response.get("job") if isinstance(response.get("job"), dict) else response
        _job_shape(row)
        selected["job"] = row
        if action in ("restart", "cancel"):
            entry.setdefault("inputs", {}).pop(card_id or "single", None)
            entry.setdefault("files", {}).pop(card_id or "single", None)
        entry["status"] = _summary_receipt(receipt)
        self.persist()
        result = {"ok": True, "receipt_id": entry["id"], "job": _summary_job(row)}
        if card_id:
            result["card_id"] = card_id
        return result

    def retry_existing(self, entry, card_id=None):
        value = self.receipt(entry)
        selected = self.select(value, card_id)
        row = selected.get("job") or {}
        if (
            row.get("state") != "needs_input"
            or row.get("retry_mode") != "reuse"
            or not row.get("can_retry")
        ):
            raise ManageError(
                "This task requires revised input or cannot be retried",
                code="invalid_state",
            )
        body = {"token": entry["token"]}
        if card_id:
            body["card_id"] = card_id
        self.json(entry["origin"], "POST", "/api/retry", json=body)
        current = self.receipt(entry)
        entry["status"] = _summary_receipt(current)
        return {"ok": True, "receipt_id": entry["id"], **entry["status"]}

    @staticmethod
    def _submit_state(selected, retry):
        row = selected.get("job")
        if row:
            if not row.get("can_retry"):
                raise ManageError(
                    "Task is already submitted or cannot be retried",
                    code="invalid_state",
                )
        elif retry:
            raise ManageError(
                "No task exists to retry; use customer redeem", code="invalid_state"
            )

    def revise(
        self,
        entry,
        card_id,
        message,
        *,
        request_id=None,
        expected_revision=None,
        new_request=False,
    ):
        selected = self.select(self.receipt(entry), card_id)
        # Save the exact request before transmission. A lost response can then be
        # replayed even if the server has already queued the new delivery round.
        key = card_id or "single"
        pending = entry.setdefault("revision_requests", {})
        digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
        saved = pending.get(key)
        if new_request:
            pending.pop(key, None)
            self.persist()
            saved = None
        if saved is not None:
            if (
                not isinstance(saved, dict)
                or saved.get("message_sha256") != digest
                or request_id is not None
                and saved.get("request_id") != request_id
                or expected_revision is not None
                and saved.get("expected_revision") != expected_revision
            ):
                raise ManageError(
                    "A revision request is unresolved; retry the same message and request ID first",
                    code="revision_pending",
                )
            body = {
                "token": entry["token"],
                "request_id": saved["request_id"],
                "expected_revision": saved["expected_revision"],
                "message": message,
            }
        else:
            if expected_revision is None:
                revision = (selected.get("job") or {}).get("revision", {})
                expected_revision = revision.get("current", 0)
            if (
                type(expected_revision) is not int
                or not 0 <= expected_revision <= 2**53 - 1
            ):
                raise ManageError(
                    "Invalid current delivery revision", code="invalid_response"
                )
            body = {
                "token": entry["token"],
                "request_id": request_id or str(uuid.uuid4()),
                "expected_revision": expected_revision,
                "message": message,
            }
            pending[key] = {
                "request_id": body["request_id"],
                "expected_revision": expected_revision,
                "message_sha256": digest,
            }
            self.persist()
        if card_id:
            body["card_id"] = card_id
        try:
            result = _summary_job(
                _object(
                    self.json(
                        entry["origin"], "POST", "/api/receipt/revisions", json=body
                    )
                )
            )
        except ManageError as exc:
            # A definite rejection did not start a new round. Transport failures
            # keep the request identity so a rerun cannot spend another credit.
            if exc.status in (400, 403, 404, 410, 422):
                pending.pop(key, None)
                self.persist()
            raise
        pending.pop(key, None)
        status = entry.setdefault("status", {})
        if card_id:
            item = next(
                (
                    row
                    for row in status.get("items", [])
                    if row.get("card_id") == card_id
                ),
                None,
            )
            if item is not None:
                item.update(job=result, **_card_metadata(result))
        else:
            status.update(job=result, **_card_metadata(result))
        self.persist()
        return {
            "ok": True,
            "receipt_id": entry["id"],
            "request_id": body["request_id"],
            "job": result,
            **({"card_id": card_id} if card_id else {}),
            **_card_metadata(result),
        }

    def reveal(self, entry, card_id, target, revision=None):
        self.select(self.receipt(entry), card_id)
        # Reserve the private output before consuming a single-view delivery.
        fd = _private_output(target)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as destination:
                body = {"token": entry["token"]}
                if card_id:
                    body["card_id"] = card_id
                if revision is not None:
                    body["revision"] = revision
                result = _object(
                    self.json(entry["origin"], "POST", "/api/receipt/reveal", json=body)
                )
                files = result.get("files", [])
                if not isinstance(files, list) or any(
                    not isinstance(item, dict) for item in files
                ):
                    raise ManageError(
                        "Invalid delivery file metadata", code="invalid_response"
                    )
                # Persist file IDs before writing content: later downloads do not
                # need to reveal a single-view delivery a second time.
                key = card_id or "single"
                entry.setdefault("files", {})[key] = files
                if "revision" in result:
                    if type(result["revision"]) is not int or result["revision"] < 0:
                        raise ManageError(
                            "Invalid delivery revision", code="invalid_response"
                        )
                    entry.setdefault("delivery_files", {}).setdefault(key, {})[
                        str(result["revision"])
                    ] = files
                self.persist()
                json.dump(
                    result, destination, ensure_ascii=False, separators=(",", ":")
                )
                destination.write("\n")
                destination.flush()
                os.fsync(destination.fileno())
        except BaseException:
            if target.exists() and target.stat().st_size == 0:
                target.unlink()
            raise
        return {
            "ok": True,
            "receipt_id": entry["id"],
            "output": str(target),
            **({"revision": result["revision"]} if "revision" in result else {}),
            "files": [
                {
                    key: item[key]
                    for key in ("id", "field_key", "filename", "size")
                    if key in item
                }
                for item in files
            ],
        }

    def download(self, entry, card_id, file_id, target, revision=None):
        self.select(self.receipt(entry), card_id)
        key = card_id or "single"
        versions = entry.get("delivery_files", {}).get(key, {})
        files = (
            versions.get(str(revision), [])
            if revision is not None
            else entry.get("files", {}).get(key, [])
            + [descriptor for values in versions.values() for descriptor in values]
        )
        descriptor = next((item for item in files if item.get("id") == file_id), None)
        if descriptor is None or (
            card_id and descriptor.get("card_id") not in (None, card_id)
        ):
            raise ManageError(
                "File not found in this revealed receipt; reveal first",
                code="not_found",
            )
        expected = descriptor.get("size")
        if type(expected) is not int or not 0 <= expected <= MAX_FILE_BYTES:
            raise ManageError(
                "File exceeds the CLI download limit", code="invalid_download"
            )
        fd = _private_output(target)
        try:
            with os.fdopen(fd, "wb") as destination:
                body = {"token": entry["token"], "file_id": file_id}
                if card_id:
                    body["card_id"] = card_id
                if revision is not None:
                    body["revision"] = revision
                response = self.request(
                    entry["origin"], "POST", "/api/files/download", json=body
                )
                try:
                    size = 0
                    for chunk in response.iter_bytes(64 * 1024):
                        size += len(chunk)
                        if size > MAX_FILE_BYTES:
                            raise ManageError(
                                "File exceeds the CLI download limit",
                                code="invalid_download",
                            )
                        destination.write(chunk)
                    if size != expected:
                        raise ManageError(
                            "Downloaded file size did not match its metadata",
                            code="invalid_download",
                        )
                    destination.flush()
                    os.fsync(destination.fileno())
                finally:
                    response.close()
        except httpx.HTTPError:
            target.unlink(missing_ok=True)
            raise ManageError(
                "Download was interrupted", code="connection_error"
            ) from None
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return {
            "ok": True,
            "receipt_id": entry["id"],
            "file_id": file_id,
            "output": str(target),
            "size": size,
        }


def add_parser(commands):
    customer = commands.add_parser(
        "customer", help="redeem codes and manage customer receipts without a browser"
    )
    customer.add_argument(
        "--profile",
        type=Path,
        help="private receipt profile (default: XDG_CONFIG_HOME/extore/customer.json)",
    )
    commands = customer.add_subparsers(dest="customer_command", required=True)
    products = commands.add_parser(
        "products",
        help="list compact public product information",
        description="Variant price values are reference prices for external store configuration. Extore handles redemption and does not collect payments.",
    )
    products.add_argument(
        "--origin", required=True, help="HTTPS server origin; HTTP only for loopback"
    )
    products.add_argument(
        "--detail",
        action="store_true",
        help="include full public descriptions and schemas",
    )
    schema = commands.add_parser(
        "schema",
        help="show required inputs and defined outputs",
        description="Variant price values are reference prices for external store configuration. Extore handles redemption and does not collect payments.",
    )
    selection = schema.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--receipt",
        dest="receipt_id",
        help="saved local receipt ID, including private products",
    )
    selection.add_argument("--product", help="public product ID")
    schema.add_argument("--origin", help="server origin for --product")
    card_selection = schema.add_mutually_exclusive_group()
    card_selection.add_argument("--card", help="card ID in a batch receipt")
    card_selection.add_argument(
        "--group", help="compatible group ID from customer schema"
    )
    schema.add_argument(
        "--detail",
        action="store_true",
        help="include Markdown tutorials and descriptions",
    )
    exchange = commands.add_parser(
        "exchange",
        help="verify up to 30 codes and privately save their receipts",
    )
    exchange.add_argument("--origin", required=True)
    exchange.add_argument(
        "--codes-stdin",
        action="store_true",
        required=True,
        help="read private codes from standard input, never command arguments",
    )
    exchange_mode = exchange.add_mutually_exclusive_group()
    exchange_mode.add_argument(
        "--batch",
        action="store_true",
        help="use per-card partial results, including for one code",
    )
    exchange_mode.add_argument(
        "--atomic",
        action="store_true",
        help="legacy same-product exchange; reject the whole group on an invalid code",
    )
    flow = commands.add_parser(
        "flow", help="start, answer, continue or view the current customer task step"
    )
    flow_commands = flow.add_subparsers(dest="flow_action", required=True)
    for action in ("view", "start", "answer", "continue", "restart", "cancel"):
        command = flow_commands.add_parser(
            action,
            help={
                "view": "show the current step and explicitly visible prior results",
                "start": "explicitly start this step and its server timer",
                "answer": "submit this step's defined inputs",
                "continue": "continue after reviewing the current result",
                "restart": "explicitly restart an eligible failed flow without starting its timer",
                "cancel": "cancel this attempt and clear its data; external actions cannot be undone",
            }[action],
        )
        command.add_argument("receipt_id", help="saved local receipt ID")
        command.add_argument("--card", help="select one card in a batch receipt")
        if action == "view":
            command.add_argument(
                "--detail",
                action="store_true",
                help="include the current field tutorials",
            )
            command.add_argument(
                "--output",
                type=Path,
                help="save the step content privately instead of printing it",
            )
        else:
            command.add_argument(
                "--flow-epoch",
                type=int,
                help="optionally require this exact step activation",
            )
            command.add_argument(
                "--expected-revision",
                type=int,
                help="optionally require this exact step revision",
            )
        if action == "cancel":
            command.add_argument(
                "--confirm",
                action="store_true",
                required=True,
                help="confirm cancellation; processing actions may need merchant verification",
            )
        if action == "answer":
            inputs = command.add_mutually_exclusive_group()
            inputs.add_argument(
                "--values-file",
                type=Path,
                help="bounded JSON object matching the current step fields",
            )
            inputs.add_argument(
                "--values-stdin",
                action="store_true",
                help="read private step values from standard input",
            )
            command.add_argument(
                "--file",
                action="append",
                default=[],
                help="FIELD=PATH; repeat for an images field",
            )
    importer = commands.add_parser(
        "import-receipt", help="privately import an existing browser receipt link"
    )
    importer.add_argument(
        "--link-stdin",
        action="store_true",
        help="read the private receipt link from standard input",
    )
    commands.add_parser(
        "receipts",
        help="list saved receipt summaries without tokens or network requests",
    )
    for name, help_text in (
        ("receipt", "show current receipt progress and queue position"),
        ("files", "list cached input and revealed delivery file IDs"),
        ("redeem", "submit defined inputs, optionally uploading file paths"),
        ("retry", "retry with revised inputs or --reuse the stored inputs"),
        ("revise", "request a new delivery round using the card's remaining benefits"),
        ("upload", "upload an input file without submitting the task"),
        ("reveal", "save delivery content to a new private JSON file"),
        ("download", "download one revealed delivery file"),
        ("destroy", "permanently destroy a completed delivery"),
    ):
        command = commands.add_parser(
            name, aliases=["status"] if name == "receipt" else [], help=help_text
        )
        command.add_argument(
            "receipt_id", help="local ID printed by exchange or import-receipt"
        )
        card_selection = command.add_mutually_exclusive_group()
        card_selection.add_argument(
            "--card", help="required for a particular card in a batch receipt"
        )
        if name in ("redeem", "retry"):
            card_selection.add_argument(
                "--group",
                help="share text inputs within a compatible group from customer schema",
            )
        if name == "receipt":
            command.add_argument(
                "--inputs",
                action="store_true",
                help="explicitly include original inputs returned for correction",
            )
        if name in ("redeem", "retry"):
            input_group = command.add_mutually_exclusive_group(required=True)
            input_group.add_argument(
                "--params-file", type=Path, help="JSON object of input strings"
            )
            input_group.add_argument(
                "--params-stdin",
                action="store_true",
                help="read bounded JSON input from standard input",
            )
            input_group.add_argument(
                "--items-file", type=Path, help="batch JSON list of {card_id,params}"
            )
            if name == "retry":
                input_group.add_argument(
                    "--reuse",
                    action="store_true",
                    help="retry unchanged stored inputs when the processor permits it",
                )
            command.add_argument(
                "--file",
                action="append",
                default=[],
                help="upload FIELD=PATH and put its file ID into inputs",
            )
        if name in ("redeem", "retry"):
            command.add_argument(
                "--card-file",
                action="append",
                default=[],
                help="CARD:FIELD=PATH; separate uploads for --group or --items-file",
            )
        if name == "upload":
            command.add_argument("--field", required=True)
            command.add_argument("--file", type=Path, required=True)
        if name == "revise":
            message = command.add_mutually_exclusive_group(required=True)
            message.add_argument(
                "--message-file", type=Path, help="UTF-8 revision request"
            )
            message.add_argument(
                "--message-stdin",
                action="store_true",
                help="read the revision request privately from stdin",
            )
            command.add_argument(
                "--request-id",
                type=_revision_request_id,
                help="UUID v4; automatically saved and reused after a lost response",
            )
            command.add_argument(
                "--expected-revision",
                type=_revision_number,
                help="require this delivered revision; omitted to read the current one",
            )
            command.add_argument(
                "--new-request",
                action="store_true",
                help="explicitly discard a pending request after checking the current state",
            )
        if name in ("reveal", "download", "files"):
            command.add_argument(
                "--revision",
                type=_revision_number,
                help="delivery round: 0 is the first draft",
            )
        if name in ("reveal", "download"):
            command.add_argument(
                "--output",
                type=Path,
                required=True,
                help="new output file, mode 600; never overwrite existing files",
            )
        if name == "download":
            command.add_argument("--file-id", required=True)
        if name == "destroy":
            command.add_argument(
                "--confirm",
                action="store_true",
                required=True,
                help="confirm permanent destruction",
            )
    from .http_proxy import add_proxy_arguments

    add_proxy_arguments(customer)
    return customer


def dispatch(client, args):
    command = args.customer_command
    if command in ("products", "schema") and not getattr(args, "receipt_id", None):
        if getattr(args, "group", None) or getattr(args, "card", None):
            raise ManageError(
                "--group and --card require --receipt", code="invalid_input"
            )
        if not args.origin:
            raise ManageError(
                "--origin is required for a public product", code="invalid_input"
            )
        origin = origin_from_url(args.origin)
        products = client.json(origin, "GET", "/api/products")
        if not isinstance(products, list) or any(
            not isinstance(item, dict) for item in products
        ):
            raise ManageError("Invalid product list", code="invalid_response")
        if command == "products":
            return {
                "ok": True,
                "products": products
                if args.detail
                else [_summary_product(item) for item in products],
            }
        product = next(
            (item for item in products if item.get("id") == args.product), None
        )
        if product is None:
            raise ManageError(
                "Public product not found; use a receipt for private products",
                code="not_found",
            )
        return {"ok": True, "product": _schema(product, args.detail)}
    if command == "exchange":
        origin = origin_from_url(args.origin)
        code = sys.stdin.read(8001).strip()
        if not code or len(code) > 8000:
            raise ManageError(
                "Provide 1 to 30 codes, at most 8000 characters", code="invalid_input"
            )
        groups = _exchange_groups(client, origin, code)
        partial = getattr(args, "batch", False) or (
            not getattr(args, "atomic", False)
            and sum(len(codes) for codes in groups.values()) > 1
        )
        results = []
        failures = []
        for destination, codes in groups.items():
            try:
                value = _object(
                    client.json(
                        destination,
                        "POST",
                        "/api/batch/exchange" if partial else "/api/exchange",
                        json={"code": "\n".join(codes)},
                    )
                )
                if partial:
                    clean = _summary_receipt(value)
                    if value.get("partial") is not True:
                        raise ManageError(
                            "Invalid partial exchange response", code="invalid_response"
                        )
                    if clean["summary"]["accepted"] == 0:
                        if value.get("token") is not None:
                            raise ManageError(
                                "Invalid empty receipt credential",
                                code="invalid_response",
                            )
                        results.append({"ok": False, "origin": destination, **clean})
                        continue
                results.append(client.remember(destination, value.get("token"), value))
            except ManageError as error:
                if len(groups) == 1:
                    raise
                failures.append(
                    {"origin": destination, "code": error.code, "status": error.status}
                )
        if len(groups) == 1:
            return results[0]
        return {
            "ok": not failures and all(result["ok"] for result in results),
            "receipts": results,
            "failed": failures,
        }
    if command == "import-receipt":
        link = (
            sys.stdin.read(4097).strip()
            if args.link_stdin
            else getpass.getpass("Private Extore receipt link: ").strip()
        )
        if not link or len(link) > 4096:
            raise ManageError("Provide one complete receipt link", code="invalid_input")
        parsed = urlsplit(link)
        origin = origin_from_url(f"{parsed.scheme}://{parsed.netloc}")
        token = unquote(parsed.fragment)
        if (
            parsed.path.rstrip("/") != "/receipt"
            or parsed.query
            or not 1 <= len(token) <= 100
            or any(ord(char) < 33 or ord(char) > 126 for char in token)
        ):
            raise ManageError("Use the complete /receipt#… link", code="invalid_input")
        try:
            value = _object(
                client.json(origin, "POST", "/api/batch/receipt", json={"token": token})
            )
        except ManageError as error:
            if error.status != 404:
                raise
            value = _object(
                client.json(origin, "POST", "/api/receipt", json={"token": token})
            )
        return client.remember(origin, token, value)
    if command == "receipts":
        return {
            "ok": True,
            "receipts": [
                {
                    key: item[key]
                    for key in ("id", "origin", "created", "status")
                    if key in item
                }
                for item in client.data["receipts"]
            ],
        }
    entry = client.saved(args.receipt_id)
    card_id = getattr(args, "card", None)
    if command in ("receipt", "status", "schema", "files"):
        value = client.receipt(entry)
        if command == "schema":
            group_id = getattr(args, "group", None)
            if value.get("batch") and not card_id:
                if group_id:
                    return {
                        "ok": True,
                        "receipt_id": entry["id"],
                        "group": _selected_group(value, group_id, detail=args.detail),
                    }
                return {
                    "ok": True,
                    "receipt_id": entry["id"],
                    "batch": True,
                    "partial": value.get("partial", False),
                    "groups": _batch_groups(value, detail=args.detail),
                }
            if group_id:
                raise ManageError(
                    "--group requires a batch receipt", code="invalid_input"
                )
            selected = client.select(value, card_id)
            return {
                "ok": True,
                "receipt_id": entry["id"],
                "product": _schema(selected["product"], args.detail),
                **_card_metadata(selected),
                "revision": (selected.get("job") or {}).get("revision"),
            }
        if command == "files":
            selected = client.select(value, card_id)
            key = card_id or "single"
            revision = args.revision
            versions = entry.get("delivery_files", {}).get(key, {})
            files = (
                versions.get(str(revision), [])
                if revision is not None
                else entry.get("files", {}).get(key, [])
            )
            return {
                "ok": True,
                "receipt_id": entry["id"],
                "cached": True,
                **_card_metadata(selected),
                "job": _summary_job(selected.get("job")),
                "revealed_revisions": sorted(int(number) for number in versions),
                **({"revision": revision} if revision is not None else {}),
                "inputs": [
                    descriptor
                    for stored in entry.get("inputs", {})
                    .get(card_id or "single", {})
                    .values()
                    for descriptor in (
                        stored.get("files", [])
                        if stored.get("type") == "images"
                        else [stored]
                    )
                ],
                "outputs": [
                    {
                        key: item[key]
                        for key in ("id", "field_key", "filename", "size")
                        if key in item
                    }
                    for item in files
                ],
            }
        if card_id:
            selected = client.select(value, card_id)
            result = {
                "ok": True,
                "receipt_id": entry["id"],
                "card_id": card_id,
                "job": _summary_job(selected.get("job")),
                **_card_metadata(selected),
            }
            if (
                args.inputs
                and selected.get("job")
                and selected["job"].get("state") == "needs_input"
            ):
                result["inputs"] = selected["job"].get("params", {})
            return result
        result = {"ok": True, "receipt_id": entry["id"], **_summary_receipt(value)}
        if args.inputs:
            if value.get("batch"):
                result["inputs"] = [
                    {
                        "card_id": item["card_id"],
                        "params": item["job"].get("params", {}),
                    }
                    for item in value["items"]
                    if item.get("job") and item["job"].get("state") == "needs_input"
                ]
            elif value.get("job") and value["job"].get("state") == "needs_input":
                result["inputs"] = value["job"].get("params", {})
        return result
    if command == "flow":
        action = args.flow_action
        values = (
            _read_json(args.values_file)
            if action == "answer" and (args.values_file or args.values_stdin)
            else {}
        )
        return client.flow(
            entry,
            action,
            card_id=card_id,
            values=values,
            attachments=getattr(args, "file", ()),
            flow_epoch=getattr(args, "flow_epoch", None),
            expected_revision=getattr(args, "expected_revision", None),
            target=getattr(args, "output", None),
            detail=getattr(args, "detail", False),
        )
    if command in ("redeem", "retry"):
        if getattr(args, "reuse", False):
            if args.file or args.card_file or args.group:
                raise ManageError(
                    "--reuse cannot upload new attachments or use --group; select one card",
                    code="invalid_input",
                )
            return client.retry_existing(entry, card_id)
        items = _read_json(args.items_file) if args.items_file else None
        params = _read_json(args.params_file) if items is None else None
        return client.redeem(
            entry,
            params,
            card_id=card_id,
            items=items,
            attachments=args.file,
            retry=command == "retry",
            group_id=args.group,
            card_attachments=args.card_file,
        )
    if command == "upload":
        return client.upload(entry, card_id, args.field, args.file)
    if command == "revise":
        return client.revise(
            entry,
            card_id,
            _read_revision_message(args.message_file),
            request_id=args.request_id,
            expected_revision=args.expected_revision,
            new_request=args.new_request,
        )
    if command == "reveal":
        return client.reveal(entry, card_id, args.output, revision=args.revision)
    if command == "download":
        return client.download(
            entry, card_id, args.file_id, args.output, revision=args.revision
        )
    if command == "destroy":
        client.select(client.receipt(entry), card_id)
        body = {"token": entry["token"]}
        if card_id:
            body["card_id"] = card_id
        result = _object(
            client.json(entry["origin"], "POST", "/api/receipt/destroy", json=body)
        )
        entry.setdefault("files", {}).pop(card_id or "single", None)
        entry.setdefault("delivery_files", {}).pop(card_id or "single", None)
        if result.get("ok") is not True:
            raise ManageError("Invalid destroy response", code="invalid_response")
        return {"ok": True, "receipt_id": entry["id"]}
    raise ManageError("Unknown customer command", code="invalid_input")


def execute(args, *, transport=None):
    from .http_proxy import resolve_proxy

    proxy_settings = resolve_proxy(args)
    if args.customer_command == "products" or (
        args.customer_command == "schema" and args.product
    ):
        with CustomerClient(
            transport=transport, proxy_settings=proxy_settings
        ) as client:
            return dispatch(client, args)
    path = profile_path(args.profile)
    with private_profile(
        path, create=args.customer_command in ("exchange", "import-receipt")
    ) as data:
        with CustomerClient(
            data,
            transport=transport,
            persist=lambda: _save_profile(path, data),
            proxy_settings=proxy_settings,
        ) as client:
            return dispatch(client, args)


def run(args):
    try:
        result = execute(args)
    except ManageError as exc:
        print(
            json.dumps(exc.as_dict(), ensure_ascii=False, separators=(",", ":")),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except (OSError, ValueError, KeyError, TypeError):
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "Cannot read customer credentials, input or output files",
                    "code": "local_error",
                },
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))

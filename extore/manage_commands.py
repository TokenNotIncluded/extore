"""Named commands for every product-scoped management capability."""

import argparse
import json
import math
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit

from .manage_client import ManageError, _limit, _object, _objects, profile_path

COMMANDS = {
    "product",
    "cards",
    "links",
    "events",
    "sessions",
    "devices",
    "audit",
    "processors",
    "source",
    "api",
    "trash",
}
STATUSES = (
    "unused",
    "needs_input",
    "queued",
    "processing",
    "succeeded",
    "failed_retryable",
    "failed_terminal",
    "destroyed",
    "revoked",
    "expired",
    "rejected",
)
PERMISSIONS = (
    "queue.view",
    "queue.process",
    "queue.retry",
    "product.edit",
    "fulfillment.configure",
    "cards.manage",
    "events.manage",
    "links.delegate",
    "product.delete",
    "product.purge",
    "queue.monitor",
)
BOARD_STATES = (
    "queued",
    "processing",
    "waiting",
    "failed",
    "needs_input",
    "succeeded",
    "rejected",
    "destroyed",
)
BOARD_PHASES = ("await_start", "input", "display", "queued", "processing", "ended")
JSON_LIMIT = 256000
TEXT_IMPORT_LIMIT = 2 * 1024 * 1024


def _scope(parser, *, product=True):
    parser.add_argument(
        "--product",
        required=product,
        help="explicit product ID; authorization stays within one saved grant",
    )
    parser.add_argument("--origin", help="select a saved server origin")
    parser.add_argument("--grant", help="select an individual device grant")
    parser.add_argument(
        "--detail",
        action="store_true",
        help="include complete business metadata; credentials remain hidden",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="write the result to a new mode-600 file instead of standard output",
    )


def _json_arguments(parser, *, required=True):
    inputs = parser.add_mutually_exclusive_group(required=required)
    inputs.add_argument("--json-file", type=Path, help="bounded UTF-8 JSON input file")
    inputs.add_argument(
        "--json-stdin",
        action="store_true",
        help="read bounded JSON from standard input",
    )


def _group(subcommands, name, actions):
    parent = subcommands.add_parser(
        name, help=f"{name} management through the current product authorization"
    )
    children = parent.add_subparsers(dest="operation", required=True)
    result = {}
    for action in actions:
        parser = children.add_parser(action)
        _scope(parser)
        result[action] = parser
    return result


def _count(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("count must be 1 to 1000") from None
    if not 1 <= number <= 1000:
        raise argparse.ArgumentTypeError("count must be 1 to 1000")
    return number


def _offset(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "offset must be a non-negative integer"
        ) from None
    if number < 0:
        raise argparse.ArgumentTypeError("offset must be a non-negative integer")
    return number


def board_limit(value):
    number = _offset(value)
    if not 1 <= number <= 200:
        raise argparse.ArgumentTypeError("limit must be 1 to 200")
    return number


def board_offset(value):
    number = _offset(value)
    if number > 1000000:
        raise argparse.ArgumentTypeError("offset must be 0 to 1000000")
    return number


def board_arguments(parser):
    parser.add_argument("--view", choices=("active", "processed"), default="active")
    parser.add_argument("--limit", type=board_limit, default=100)
    parser.add_argument("--offset", type=board_offset, default=0)


def board_parameters(*, product=None, shop=None, view="active", limit=100, offset=0):
    values = {"view": view, "limit": limit, "offset": offset}
    if (
        view not in ("active", "processed")
        or type(limit) is not int
        or not 1 <= limit <= 200
        or type(offset) is not int
        or not 0 <= offset <= 1000000
    ):
        raise ManageError("Invalid progress board filters", code="invalid_input")
    for key, value in (("product_id", product), ("shop_id", shop)):
        if value is not None:
            if not isinstance(value, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,100}", value
            ):
                raise ManageError(
                    "Select valid progress board IDs", code="invalid_input"
                )
            values[key] = value
    return values


def board_api_parameters(args):
    values = {}
    for entry in args.query:
        key, separator, value = entry.partition("=")
        if (
            not separator
            or key not in {"shop_id", "product_id", "view", "limit", "offset"}
            or key in values
        ):
            raise ManageError("Invalid progress board query", code="invalid_input")
        if key in ("limit", "offset"):
            if not re.fullmatch(r"[0-9]{1,7}", value):
                raise ManageError(
                    "Invalid progress board pagination", code="invalid_input"
                )
            value = int(value)
        values[key] = value
    product = getattr(args, "product", None)
    if product and values.get("product_id", product) != product:
        raise ManageError("Query belongs to a different product", code="no_scope")
    return board_parameters(
        product=product or values.get("product_id"),
        shop=values.get("shop_id"),
        view=values.get("view", "active"),
        limit=values.get("limit", 100),
        offset=values.get("offset", 0),
    )


def progress_board(
    value, *, product=None, shop=None, view="active", limit=100, offset=0
):
    """Fail closed before exposing any remotely supplied task fields."""

    def invalid():
        raise ManageError(
            "Invalid or out-of-scope progress board", code="invalid_response"
        )

    def fields(item, keys):
        if not isinstance(item, dict) or set(item) != set(keys):
            invalid()

    def identifier(item):
        if not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", item):
            invalid()

    def name(item):
        if not isinstance(item, str) or len(item) > 120:
            invalid()

    def integer(item, minimum=0, maximum=9007199254740991):
        if type(item) is not int or not minimum <= item <= maximum:
            invalid()

    def timestamp(item):
        if (
            type(item) not in (int, float)
            or not 0 <= item <= 253402300799
            or not math.isfinite(item)
        ):
            invalid()

    def counts(item):
        fields(item, BOARD_STATES)
        for count in item.values():
            integer(count)

    fields(
        value,
        (
            "schema",
            "generated_at",
            "shop",
            "totals",
            "products",
            "workers",
            "pagination",
            "scope",
        ),
    )
    if value["schema"] != "extore.progress-board.v1":
        invalid()
    timestamp(value["generated_at"])
    fields(value["shop"], ("id", "name"))
    identifier(value["shop"]["id"])
    name(value["shop"]["name"])
    if shop is not None and value["shop"]["id"] != shop:
        invalid()
    fields(value["scope"], ("product_ids",))
    ids = value["scope"]["product_ids"]
    if not isinstance(ids, list) or len(ids) > 500:
        invalid()
    for pid in ids:
        identifier(pid)
    if len(set(ids)) != len(ids) or product is not None and ids != [product]:
        invalid()
    counts(value["totals"])
    if (
        not isinstance(value["products"], list)
        or len(value["products"]) > 500
        or not isinstance(value["workers"], list)
        or len(value["workers"]) > 500
    ):
        invalid()
    pids, jobs, worker_ids = set(), set(), set()
    sums = dict.fromkeys(BOARD_STATES, 0)
    for worker in value["workers"]:
        if isinstance(worker, dict) and "agent_type" not in worker:
            worker["agent_type"] = None
        fields(
            worker,
            (
                "id",
                "name",
                "kind",
                "agent_type",
                "active_jobs",
                "completed_jobs",
                "last_update",
            ),
        )
        identifier(worker["id"])
        if not re.fullmatch(r"[0-9a-f]{64}", worker["id"]):
            invalid()
        name(worker["name"])
        if worker["agent_type"] is not None:
            import unicodedata

            if (
                not isinstance(worker["agent_type"], str)
                or not 1 <= len(worker["agent_type"]) <= 64
                or any(
                    unicodedata.category(char).startswith("C") or not char.isprintable()
                    for char in worker["agent_type"]
                )
            ):
                invalid()
        if worker["id"] in worker_ids or worker["kind"] not in (
            "human",
            "cli",
            "automatic",
            "merchant",
            "unknown",
        ):
            invalid()
        worker_ids.add(worker["id"])
        integer(worker["active_jobs"])
        integer(worker["completed_jobs"])
        if worker["last_update"] is not None:
            timestamp(worker["last_update"])
    for row in value["products"]:
        fields(row, ("id", "name", "mode", "counts", "jobs"))
        identifier(row["id"])
        name(row["name"])
        if (
            row["id"] not in ids
            or row["id"] in pids
            or row["mode"] not in ("manual", "script", "webhook", "stock", "unknown")
        ):
            invalid()
        pids.add(row["id"])
        counts(row["counts"])
        for state in BOARD_STATES:
            sums[state] += row["counts"][state]
        if not isinstance(row["jobs"], list) or len(row["jobs"]) > limit:
            invalid()
        for job in row["jobs"]:
            fields(
                job,
                (
                    "id",
                    "state",
                    "progress",
                    "attempt",
                    "created",
                    "updated",
                    "queue_position",
                    "worker_id",
                    "step_count",
                    "completed_step_count",
                    "steps",
                    "flow_phase",
                ),
            )
            identifier(job["id"])
            if job["id"] in jobs or job["state"] not in BOARD_STATES:
                invalid()
            processed = job["state"] in ("succeeded", "rejected", "destroyed")
            if processed != (view == "processed"):
                invalid()
            jobs.add(job["id"])
            integer(job["progress"], 0, 100)
            integer(job["attempt"], 1)
            timestamp(job["created"])
            timestamp(job["updated"])
            if job["state"] == "queued":
                integer(job["queue_position"], 1)
            elif job["queue_position"] is not None:
                invalid()
            if job["worker_id"] is not None:
                identifier(job["worker_id"])
                if job["worker_id"] not in worker_ids:
                    invalid()
            if job["flow_phase"] is not None and job["flow_phase"] not in BOARD_PHASES:
                invalid()
            integer(job["step_count"], 0, 256)
            integer(job["completed_step_count"], 0, job["step_count"])
            if (
                not isinstance(job["steps"], list)
                or len(job["steps"]) != job["step_count"]
            ):
                invalid()
            for position, step in enumerate(job["steps"], 1):
                fields(step, ("position", "state"))
                if (
                    type(step["position"]) is not int
                    or step["position"] != position
                    or step["state"] not in ("pending", "current", "done")
                ):
                    invalid()
            if (
                sum(step["state"] == "done" for step in job["steps"])
                != job["completed_step_count"]
                or sum(step["state"] == "current" for step in job["steps"]) > 1
            ):
                invalid()
    if pids != set(ids) or sums != value["totals"]:
        invalid()
    fields(value["pagination"], ("limit", "offset", "total", "has_more"))
    page = value["pagination"]
    integer(page["limit"], 1, 200)
    integer(page["offset"], 0, 1000000)
    integer(page["total"])
    expected_total = sum(
        value["totals"][state]
        for state in BOARD_STATES
        if (state in ("succeeded", "rejected", "destroyed")) == (view == "processed")
    )
    if (
        page["limit"] != limit
        or page["offset"] != offset
        or page["total"] != expected_total
        or type(page["has_more"]) is not bool
        or page["has_more"] != (offset + limit < page["total"])
        or len(jobs) != min(limit, max(0, page["total"] - offset))
    ):
        invalid()
    return value


def cleanup_limit(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("limit must be 1 to 500") from None
    if not 1 <= number <= 500:
        raise argparse.ArgumentTypeError("limit must be 1 to 500")
    return number


def add_commands(subcommands):
    product = _group(
        subcommands,
        "product",
        ("get", "update", "schema", "prompt", "delete", "restore", "purge"),
    )
    for parser in product.values():
        parser.description = (
            "variants.price is an optional reference price for configuring an "
            "external store, stored as exact decimal text with currency. "
            "Extore handles redemption and fulfillment and does not collect payments."
        )
    product["get"].add_argument(
        "--include-secrets",
        action="store_true",
        help="include fulfillment configuration only in an explicit private --output file",
    )
    product["delete"].add_argument(
        "--yes",
        action="store_true",
        help="confirm moving this product to the recycle bin; existing cards and tasks remain valid",
    )
    product["purge"].add_argument(
        "--yes",
        action="store_true",
        help="permanently remove this retired product from the recycle bin; sold fulfillment remains",
    )
    trash = _group(subcommands, "trash", ("empty",))
    trash["empty"].add_argument(
        "--yes",
        action="store_true",
        help="permanently empty this product's recycle bin entry; sold fulfillment remains",
    )
    _json_arguments(product["update"])
    for action in ("schema", "prompt"):
        product[action].add_argument("--language", default="zh-CN")
    cards = _group(
        subcommands,
        "cards",
        (
            "list",
            "inventory",
            "stats",
            "history",
            "issue",
            "import-text",
            "revoke",
            "batch",
        ),
    )
    cards["list"].add_argument("--limit", type=_limit, default=50)
    for action in ("inventory", "batch"):
        cards[action].add_argument("--status", choices=("", *STATUSES), default="")
        cards[action].add_argument("--variant", default="")
        cards[action].add_argument("--batch", required=action == "batch", default="")
        cards[action].add_argument("--search", default="")
        cards[action].add_argument("--offset", type=_offset, default=0)
        cards[action].add_argument("--limit", type=_limit, default=50)
    for action in ("history", "revoke"):
        cards[action].add_argument(
            "id", help="internal card ID, never a plaintext code"
        )
    cards["issue"].add_argument("--count", type=_count, default=1)
    cards["issue"].add_argument("--variant", default="default")
    cards["issue"].add_argument("--label", default="")
    cards["issue"].add_argument(
        "--expires", type=float, help="future Unix timestamp, omitted for no expiry"
    )
    text_import = cards["import-text"]
    text_source = text_import.add_mutually_exclusive_group(required=True)
    text_source.add_argument(
        "--file", type=Path, help="UTF-8 text file; one delivery per nonempty line"
    )
    text_source.add_argument(
        "--stdin",
        action="store_true",
        help="read private UTF-8 delivery text from standard input",
    )
    text_source.add_argument(
        "--text", help="inline text; use --file or --stdin for private delivery content"
    )
    text_import.add_argument("--variant", default="default")
    text_import.add_argument("--label", default="")
    text_import.add_argument("--expires", type=float)
    links = _group(subcommands, "links", ("list", "create", "revoke", "cleanup"))
    _json_arguments(links["create"])
    links["revoke"].add_argument("id", help="child management-link ID")
    links["list"].add_argument(
        "--view", choices=("active", "history", "all"), default="active"
    )
    links["cleanup"].add_argument(
        "--apply", action="store_true", help="apply cleanup; the default is a preview"
    )
    links["cleanup"].add_argument("--limit", type=cleanup_limit, default=100)
    events = _group(subcommands, "events", ("list", "retry"))
    events["list"].add_argument("--limit", type=_limit, default=50)
    events["retry"].add_argument("id")
    for name in ("sessions", "devices"):
        group = _group(subcommands, name, ("list", "revoke"))
        group["revoke"].add_argument("id")
    for name in ("audit", "processors", "source"):
        parser = subcommands.add_parser(name, help=f"read {name} metadata")
        _scope(parser, product=name != "source")
        if name == "processors":
            parser.add_argument("--language", default="zh-CN")
        if name == "audit":
            parser.add_argument("--limit", type=_limit, default=50)
    api = subcommands.add_parser(
        "api",
        help="same-origin JSON escape hatch for known product-scoped management routes",
    )
    _scope(api)
    api.add_argument("method", choices=("GET", "POST", "PUT", "PATCH", "DELETE"))
    api.add_argument("path", help="relative /api/manage/ route without query strings")
    api.add_argument(
        "--query", action="append", default=[], help="query KEY=VALUE; repeat as needed"
    )
    api.add_argument(
        "--yes",
        action="store_true",
        help="explicitly confirm product deletion; never inferred from JSON",
    )
    _json_arguments(api, required=False)


def read_json(args):
    if getattr(args, "json_stdin", False):
        raw = sys.stdin.read(JSON_LIMIT + 1)
        if len(raw.encode("utf-8")) > JSON_LIMIT:
            raise ManageError("JSON input exceeds 256000 bytes", code="invalid_input")
    elif getattr(args, "json_file", None):
        path = args.json_file
        if not path.is_file() or path.stat().st_size > JSON_LIMIT:
            raise ManageError(
                "JSON input file is missing or exceeds 256000 bytes",
                code="invalid_input",
            )
        with path.open("rb") as source:
            raw = source.read(JSON_LIMIT + 1)
        if len(raw) > JSON_LIMIT:
            raise ManageError("JSON input exceeds 256000 bytes", code="invalid_input")
    else:
        return None
    try:
        value = json.loads(
            raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ManageError(
            "Provide valid UTF-8 JSON without non-finite numbers", code="invalid_input"
        ) from None
    if not isinstance(value, dict):
        raise ManageError("JSON input must be an object", code="invalid_input")
    return value


def _public(value):
    """Do not turn config/export operations into secret echoes in an AI context."""
    if isinstance(value, list):
        return [_public(item) for item in value]
    if not isinstance(value, dict):
        return value
    hidden = {
        "webhook_secret",
        "access_token",
        "private_key",
        "token",
        "digest",
        "public_key",
        "codes",
        "password",
        "new_password",
        "smtp_password",
        "password_hash",
        "backup_code",
        "backup_codes",
        "totp_secret",
        "pending_totp_secret",
        "secret",
        "ciphertext",
        "configuration_ciphertext",
    }
    result = {}
    for key, item in value.items():
        if key == "processor_config":
            try:
                from .processors import editable_configuration

                visible = editable_configuration(value.get("processor_id"), item)
            except (ValueError, KeyError, TypeError):
                visible = {}
            result[key] = (
                {
                    name: visible[name] if name in visible else "[redacted]"
                    for name, config in item.items()
                }
                if isinstance(item, dict)
                else "[redacted]"
            )
        elif (
            key in hidden
            and not (key == "secret" and isinstance(item, bool))
            or (
                key == "url"
                and isinstance(item, str)
                and any(prefix in item for prefix in ("/staff#", "/cli#", "/receipt#"))
            )
        ):
            result[key] = "[redacted]"
        else:
            result[key] = _public(item)
    return result


def _only(value, fields):
    return {key: value[key] for key in fields if key in value}


def _label(value, language):
    if not isinstance(value, dict):
        return value
    return value.get(language) or value.get("en") or next(iter(value.values()), "")


def _processor_summary(item, language="zh-CN"):
    result = _only(
        item, ("id", "schema_version", "delivery", "source", "source_url", "version")
    )
    result["name"] = _label(item.get("name", {}), language)
    description = _label(item.get("description", {}), language)
    result["description"] = description[:160]
    result["parameters_count"] = len(item.get("parameters", []))
    result["outputs_count"] = len(item.get("outputs", []))
    return result


def _schema(product, *, language="zh-CN", detail=False):
    def field(item):
        if detail:
            return item
        result = _only(item, ("key", "type", "required", "collapsed"))
        result["label"] = _label(item.get("label", {}), language)
        return result

    return {
        **_only(product, ("id", "name", "mode", "delivery", "view_policy")),
        "parameters": [field(item) for item in product.get("parameters", [])],
        "outputs": [field(item) for item in product.get("outputs", [])],
        "steps": product.get("progress_steps", [])
        if detail
        else [
            {"id": step["id"], "label": _label(step.get("label", {}), language)}
            for step in product.get("progress_steps", [])
        ],
    }


class OutputFile:
    """Reserve a private destination before the corresponding server mutation."""

    def __init__(self, args, prefix):
        self.path = args.output
        if self.path is None:
            directory = profile_path(args.profile).parent / "exports"
            directory.mkdir(mode=0o700, exist_ok=True)
            info = directory.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise ManageError(
                    "The CLI export directory must be private and owned by you",
                    code="unsafe_output",
                )
            self.path = directory / (prefix + "-" + secrets.token_hex(8) + ".json")
        try:
            self.fd = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            raise ManageError(
                "Output already exists; choose a new path", code="output_exists"
            ) from None
        self.written = False

    def __enter__(self):
        return self

    def write(self, value):
        payload = (
            json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode()
            + b"\n"
        )
        with os.fdopen(self.fd, "wb") as output:
            self.fd = None
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        self.written = True
        return {"ok": True, "output": str(self.path), "bytes": len(payload)}

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
        if not self.written:
            self.path.unlink(missing_ok=True)


def _finish(args, result):
    if args.output and result.get("output") != str(args.output):
        with OutputFile(args, "result") as output:
            return output.write(result)
    return result


def _grant(client, args, origin, permission=""):
    return client.grant(
        product=args.product,
        origin=origin,
        grant_id=args.grant,
        permissions=(permission,) if permission else (),
    )


def _request(client, grant, method, path, *, scoped=True, **kwargs):
    params = dict(kwargs.pop("params", {}))
    if scoped:
        params["product_id"] = grant["product_id"]
    return client.request(grant, method, path, params=params or None, **kwargs)


def _product(client, grant):
    return _object(_request(client, grant, "GET", "/api/manage/product"))


def require_product_delete_confirmation(args):
    """Run before grant/session renewal so an unconfirmed delete stays local."""
    command = getattr(args, "manage_command", "")
    named_operation = getattr(args, "operation", "")
    if command == "product" and named_operation in ("delete", "restore", "purge"):
        if not isinstance(getattr(args, "product", None), str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,100}", args.product
        ):
            raise ManageError("Select one valid --product ID", code="invalid_input")
    named = (
        command == "product"
        and named_operation in ("delete", "purge")
        or command == "trash"
        and named_operation == "empty"
    )
    generic = (
        command == "api"
        and getattr(args, "method", "") == "DELETE"
        and (
            getattr(args, "path", "") == "/api/manage/product"
            or re.fullmatch(
                r"/api/admin/products/[A-Za-z0-9_-]+", getattr(args, "path", "")
            )
        )
    )
    generic = generic or (
        command == "api"
        and getattr(args, "method", "") == "POST"
        and (
            getattr(args, "path", "")
            in (
                "/api/manage/product/purge",
                "/api/manage/products/empty-trash",
                "/api/admin/products/empty-trash",
            )
            or re.fullmatch(
                r"/api/admin/products/[A-Za-z0-9_-]+/purge", getattr(args, "path", "")
            )
        )
    )
    if (named or generic) and getattr(args, "yes", False) is not True:
        raise ManageError(
            "Product deletion requires --yes; existing cards and tasks are retained",
            code="confirmation_required",
        )


def product_lifecycle_body(args, operation, body=None):
    if operation in ("delete", "purge"):
        if getattr(args, "yes", False) is not True:
            raise ManageError(
                "Product deletion requires --yes", code="confirmation_required"
            )
        require_product_delete_confirmation(args)
        if body is not None and (
            not isinstance(body, dict)
            or set(body) != {"confirmed"}
            or body.get("confirmed") is not True
        ):
            raise ManageError(
                "Product deletion JSON must be exactly {confirmed:true}",
                code="invalid_input",
            )
        return {"confirmed": True}
    if body not in (None, {}):
        raise ManageError(
            "Product restoration does not accept configuration or identity fields",
            code="invalid_input",
        )
    return None


def purge_product_ids(body):
    if (
        not isinstance(body, dict)
        or set(body) != {"confirmed", "product_ids"}
        or body.get("confirmed") is not True
    ):
        raise ManageError(
            "Empty-trash JSON needs only confirmed:true and explicit product_ids",
            code="invalid_input",
        )
    ids = body["product_ids"]
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= 500
        or any(
            not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", item)
            for item in ids
        )
        or len(ids) != len(set(ids))
    ):
        raise ManageError(
            "Select 1 to 500 unique product IDs; split larger snapshots explicitly",
            code="invalid_input",
        )
    return list(ids)


def purge_result(value, product_ids):
    value = _object(value)
    ids = value.get("purged_product_ids")
    if (
        value.get("ok") is not True
        or not isinstance(ids, list)
        or any(not isinstance(item, str) for item in ids)
        or len(ids) != len(set(ids))
        or set(ids) != set(product_ids)
        or type(value.get("purged_count")) is not int
        or value["purged_count"] != len(ids)
        or value.get("preserved_fulfillment") is not True
    ):
        raise ManageError("Invalid empty-trash response", code="invalid_response")
    return {
        "ok": True,
        "purged_product_ids": ids,
        "purged_count": len(ids),
        "preserved_fulfillment": True,
    }


def trash_snapshot(items, *, product_id=None, shop_id=None):
    rows = _objects(items)
    if len(rows) > 500:
        raise ManageError(
            "More than 500 deleted products; purge explicit batches instead",
            code="invalid_input",
        )
    ids = []
    for row in rows:
        pid = row.get("id")
        if (
            not isinstance(pid, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", pid)
            or pid in ids
            or row.get("deleted") is not True
            or row.get("purged") is True
            or product_id is not None
            and pid != product_id
            or shop_id is not None
            and row.get("shop_id") != shop_id
        ):
            raise ManageError(
                "Invalid or out-of-scope recycle bin snapshot", code="invalid_response"
            )
        ids.append(pid)
    return ids


def product_lifecycle_result(value, product_id, operation):
    value = _object(value)
    if operation == "purge":
        timestamp = value.get("purged_at")
        if (
            not {"ok", "product_id", "deleted", "purged", "purged_at"}.issubset(value)
            or value.get("ok") is not True
            or value.get("product_id") != product_id
            or value.get("deleted") is not True
            or value.get("purged") is not True
            or type(timestamp) not in (int, float)
            or timestamp <= 0
            or timestamp > 253402300799
            or not math.isfinite(timestamp)
        ):
            raise ManageError(
                "Invalid permanent-retirement response", code="invalid_response"
            )
        return {
            "ok": True,
            "product_id": product_id,
            "deleted": True,
            "purged": True,
            "purged_at": timestamp,
        }
    deleted = operation == "delete"
    timestamp = value.get("deleted_at")
    if (
        not {"ok", "product_id", "deleted", "deleted_at"}.issubset(value)
        or value.get("ok") is not True
        or value.get("product_id") != product_id
        or value.get("deleted") is not deleted
        or (
            deleted
            and (
                type(timestamp) not in (int, float)
                or timestamp <= 0
                or timestamp > 253402300799
                or not math.isfinite(timestamp)
            )
        )
        or (not deleted and timestamp is not None)
    ):
        raise ManageError("Invalid product lifecycle response", code="invalid_response")
    return {
        "ok": True,
        "product_id": product_id,
        "deleted": deleted,
        "deleted_at": timestamp,
    }


def dispatch(client, args, origin):
    command = args.manage_command
    operation = getattr(args, "operation", "")
    require_product_delete_confirmation(args)
    if command == "source":
        if origin:
            selected = origin
        else:
            grants, errors = client.grants(grant_id=args.grant, product=args.product)
            origins = {grant["origin"] for grant in grants}
            if len(origins) != 1:
                raise ManageError(
                    "Select one --origin for repository metadata",
                    code="ambiguous_scope",
                )
            selected = next(iter(origins))
        return _finish(
            args,
            {
                "ok": True,
                "source": _object(client._json(selected, "GET", "/api/source")),
            },
        )
    if command == "api":
        return api_request(client, args, origin)
    permission = {
        "cards": "cards.manage",
        "links": "links.delegate",
        "events": "events.manage",
        "processors": "product.edit",
    }.get(command, "")
    if command == "product" and operation != "schema":
        permission = (
            "product.purge"
            if operation == "purge"
            else "product.delete"
            if operation in ("delete", "restore")
            else "product.edit"
        )
    if command == "trash":
        permission = "product.purge"
    include_secrets = (
        command == "product" and operation == "get" and args.include_secrets
    )
    if include_secrets:
        if not args.output:
            raise ManageError(
                "--include-secrets requires a new private --output file",
                code="invalid_input",
            )
        grant = client.grant(
            product=args.product,
            origin=origin,
            grant_id=args.grant,
            permissions=("product.edit", "fulfillment.configure"),
        )
    else:
        grant = _grant(client, args, origin, permission)
    if command == "trash":
        rows = client.request(
            grant,
            "GET",
            "/api/manage/products",
            params={
                "view": "deleted",
                "compact": "true",
                "product_id": grant["product_id"],
            },
        )
        ids = trash_snapshot(rows, product_id=grant["product_id"])
        result = (
            purge_result(
                {
                    "ok": True,
                    "purged_product_ids": [],
                    "purged_count": 0,
                    "preserved_fulfillment": True,
                },
                [],
            )
            if not ids
            else purge_result(
                _request(
                    client,
                    grant,
                    "POST",
                    "/api/manage/products/empty-trash",
                    json={"confirmed": True, "product_ids": ids},
                ),
                ids,
            )
        )
    elif command == "product":
        result = product_command(client, grant, args)
    elif command == "cards":
        result = cards_command(client, grant, args)
    elif command == "links":
        result = links_command(client, grant, args)
    elif command == "events":
        if operation == "retry":
            result = _request(
                client,
                grant,
                "POST",
                f"/api/manage/events/{quote(args.id, safe='')}/retry",
            )
        else:
            events = _objects(
                _request(
                    client,
                    grant,
                    "GET",
                    "/api/manage/events",
                    params={"limit": args.limit},
                )
            )
            result = {
                "ok": True,
                "events": events
                if args.detail
                else [
                    _only(
                        item,
                        (
                            "id",
                            "type",
                            "job_id",
                            "created",
                            "webhook_state",
                            "attempts",
                        ),
                    )
                    for item in events
                ],
            }
    elif command in ("sessions", "devices"):
        route = "sessions" if command == "sessions" else "cli-devices"
        if operation == "revoke":
            result = _request(
                client,
                grant,
                "DELETE",
                f"/api/manage/{route}/{quote(args.id, safe='')}",
                scoped=False,
            )
            if command == "devices":
                client.data["grants"][:] = [
                    item
                    for item in client.data["grants"]
                    if not (
                        item.get("id") == args.id
                        and item.get("origin") == grant["origin"]
                    )
                ]
            elif result.get("current"):
                grant.pop("access_token", None)
                grant["expires"] = 0
        else:
            items = _objects(
                _request(client, grant, "GET", f"/api/manage/{route}", scoped=False)
            )
            fields = (
                "id",
                "product_id",
                "link_id",
                "link_name",
                "active",
                "current",
                "channel",
                "client_name",
                "created",
                "last_seen",
                "expires",
                "revoked",
            )
            result = {
                "ok": True,
                command: items
                if args.detail
                else [_only(item, fields) for item in items],
            }
    elif command == "audit":
        items = _objects(
            _request(
                client,
                grant,
                "GET",
                "/api/manage/audit",
                scoped=False,
                params={"limit": min(args.limit, 200)},
            )
        )
        result = {
            "ok": True,
            "audit": items
            if args.detail
            else [
                _only(
                    item,
                    ("id", "action", "target", "created", "channel", "client_name"),
                )
                for item in items
            ],
        }
    elif command == "processors":
        items = _objects(
            _request(client, grant, "GET", "/api/manage/processors", scoped=False)
        )
        result = {
            "ok": True,
            "processors": items
            if args.detail
            else [_processor_summary(item, args.language) for item in items],
        }
    else:
        raise ManageError("Unknown management command", code="invalid_input")
    return _finish(args, result if include_secrets else _public(result))


def product_command(client, grant, args):
    operation = args.operation
    if operation in ("delete", "restore", "purge"):
        body = product_lifecycle_body(args, operation)
        result = _request(
            client,
            grant,
            "DELETE" if operation == "delete" else "POST",
            "/api/manage/product"
            + ("/" + operation if operation in ("restore", "purge") else ""),
            **({"json": body} if body is not None else {}),
        )
        return product_lifecycle_result(result, grant["product_id"], operation)
    if operation == "schema":
        products = _objects(
            client.request(
                grant, "GET", "/api/manage/products", params={"view": "history"}
            )
        )
        product = next(
            (item for item in products if item.get("id") == grant["product_id"]), None
        )
        if product is None:
            raise ManageError("Product not found in this grant", code="not_found")
        return {
            "ok": True,
            "schema": _schema(product, language=args.language, detail=args.detail),
        }
    product = _product(client, grant)
    if operation == "update":
        if product.get("purged") is True:
            raise ManageError(
                "Permanently retired products cannot be edited or reconfigured",
                code="invalid_state",
            )
        patch = read_json(args)
        if any(key in patch for key in ("id", "product_id")):
            raise ManageError(
                "Product identity belongs in --product, not the patch",
                code="invalid_input",
            )
        updated = {**product, **patch}
        processor_changed = updated.get("mode") == "script" and (
            product.get("mode") != "script"
            or product.get("processor_id") != updated.get("processor_id")
        )
        if processor_changed:
            catalog = _objects(
                _request(client, grant, "GET", "/api/manage/processors", scoped=False)
            )
            specification = next(
                (
                    item
                    for item in catalog
                    if item.get("id") == updated.get("processor_id")
                ),
                None,
            )
            if specification is None:
                raise ManageError(
                    "Select a product processor from the catalog",
                    code="invalid_input",
                )
            for key in ("parameters", "outputs", "delivery"):
                if key not in patch:
                    updated[key] = specification[key]
            if "processor_config" not in patch:
                updated["processor_config"] = {}
        elif product.get("mode") == "script" and updated.get("mode") != "script":
            if "processor_id" not in patch:
                updated["processor_id"] = ""
            if "processor_config" not in patch:
                updated["processor_config"] = {}
        updated.pop("id", None)
        result = _object(
            _request(client, grant, "PUT", "/api/manage/product", json=updated)
        )
        return {
            "ok": True,
            "product": _public(result)
            if args.detail
            else _only(result, ("id", "name", "mode", "delivery", "public", "active")),
        }
    if operation == "prompt":
        data = {
            **_only(
                product,
                (
                    "id",
                    "name",
                    "description",
                    "logo",
                    "image",
                    "variants",
                    "public",
                    "support_email",
                ),
            ),
            "redemption_url": grant["origin"] + "/",
            "schema": _schema(product, language=args.language, detail=args.detail),
        }
        return {
            "ok": True,
            "prompt": "请根据以下商品资料在目标商城创建商品。variants.price 是参考价，供外部商城配置参考；保留规格与参考价的十进制原文，实际售价由商家在商城确定。Extore 只负责卡密兑换与交付，不收款；付款、销售库存和订单由商城负责，顾客获得卡密后在兑换网站填写需求并领取结果。不要把商品管理权限、顾客隐私或发货凭证放入商城公开描述。\n"
            + json.dumps(data, ensure_ascii=False, indent=2),
        }
    if args.include_secrets:
        return {"ok": True, "product": product}
    safe = _public(product)
    if args.detail:
        return {"ok": True, "product": safe}
    result = _only(
        safe,
        (
            "id",
            "name",
            "mode",
            "delivery",
            "view_policy",
            "public",
            "active",
            "logo",
            "image",
            "support_email",
        ),
    )
    result["variants"] = [
        _only(variant, ("id", "name", "price", "currency", "enabled"))
        for variant in product.get("variants", [])
    ]
    return {"ok": True, "product": result}


def _import_text(args):
    if args.file:
        fd = os.open(args.file, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ManageError(
                    "Import source must be a regular file", code="invalid_input"
                )
            raw = source.read(TEXT_IMPORT_LIMIT + 1)
    elif args.stdin:
        raw = getattr(sys.stdin, "buffer", sys.stdin).read(TEXT_IMPORT_LIMIT + 1)
    else:
        raw = args.text
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > TEXT_IMPORT_LIMIT:
        raise ManageError("Text import exceeds 2 MiB UTF-8", code="invalid_input")
    try:
        value = raw.decode("utf-8")
    except UnicodeError:
        raise ManageError("Text import must use UTF-8", code="invalid_input") from None
    if not value.strip():
        raise ManageError("Text import is empty", code="invalid_input")
    return value


def cards_command(client, grant, args):
    operation = args.operation
    if operation == "list":
        items = _objects(
            _request(
                client, grant, "GET", "/api/manage/cards", params={"limit": args.limit}
            )
        )
        return {"ok": True, "cards": items}
    if operation == "issue":
        with OutputFile(args, "cards") as output:
            result = _object(
                client.request(
                    grant,
                    "POST",
                    "/api/manage/cards",
                    json={
                        "product_id": grant["product_id"],
                        "count": args.count,
                        "variant_id": args.variant,
                        "label": args.label,
                        "expires": args.expires,
                    },
                )
            )
            saved = output.write(result)
            return {
                **saved,
                "count": len(result.get("codes", [])),
                "batch_id": result.get("batch_id"),
            }
    if operation == "import-text":
        text = _import_text(args)
        with OutputFile(args, "text-cards") as output:
            result = _object(
                client.request(
                    grant,
                    "POST",
                    "/api/manage/cards/import-text",
                    json={
                        "product_id": grant["product_id"],
                        "variant_id": args.variant,
                        "text": text,
                        "label": args.label,
                        "expires": args.expires,
                    },
                )
            )
            codes, items = result.get("codes"), result.get("items")
            if (
                not isinstance(codes, list)
                or not isinstance(items, list)
                or len(codes) != len(items)
            ):
                raise ManageError(
                    "Invalid text import response", code="invalid_response"
                )
            saved = output.write(result)
            return {
                **saved,
                "count": len(codes),
                "batch_id": result.get("batch_id"),
                "stats": _only(
                    result.get("stats", {}), ("lines", "blank", "duplicates", "created")
                ),
            }
    if operation == "stats":
        result = _object(_request(client, grant, "GET", "/api/manage/card-stats"))
        if args.detail:
            return {"ok": True, **result}
        products = []
        for product in result.get("products", []):
            compact = _only(
                product,
                (
                    "product_id",
                    "product_name",
                    "total",
                    "remaining",
                    "available",
                    "used",
                    "in_progress",
                    "completed",
                    "rejected",
                    "failed",
                ),
            )
            compact["variants"] = [
                {
                    **_only(
                        variant, ("variant_id", "name", "price", "currency", "enabled")
                    ),
                    "summary": _only(
                        variant.get("summary", {}),
                        (
                            "total",
                            "remaining",
                            "used",
                            "in_progress",
                            "completed",
                            "rejected",
                            "failed",
                        ),
                    ),
                }
                for variant in product.get("variants", [])
            ]
            products.append(compact)
        return {"ok": True, "summary": result.get("summary", {}), "products": products}
    if operation == "history":
        result = _object(
            _request(
                client,
                grant,
                "GET",
                f"/api/manage/cards/{quote(args.id, safe='')}/history",
            )
        )
        return (
            {"ok": True, **result}
            if args.detail
            else {
                "ok": True,
                "card": _only(
                    result.get("card", {}),
                    (
                        "id",
                        "status",
                        "state",
                        "code_suffix",
                        "variant_id",
                        "batch_id",
                        "expires",
                        "job_id",
                    ),
                ),
                "timeline": [
                    _only(item, ("id", "type", "created", "state", "attempt"))
                    for item in result.get("timeline", [])
                ],
            }
        )
    if operation == "revoke":
        return _request(
            client, grant, "POST", f"/api/manage/cards/{quote(args.id, safe='')}/revoke"
        )
    result = _object(
        _request(
            client,
            grant,
            "GET",
            "/api/manage/card-inventory",
            params={
                "status": args.status,
                "variant_id": args.variant,
                "batch_id": args.batch,
                "search": args.search,
                "offset": args.offset,
                "limit": args.limit,
            },
        )
    )
    if not args.detail:
        result["items"] = [
            _only(
                item,
                (
                    "id",
                    "status",
                    "code_suffix",
                    "variant_id",
                    "variant_name",
                    "batch_id",
                    "expires",
                    "job_id",
                    "attempt",
                ),
            )
            for item in result.get("items", [])
        ]
    return {"ok": True, **result}


def links_command(client, grant, args):
    if args.operation == "cleanup":
        return _object(
            _request(
                client,
                grant,
                "POST",
                "/api/manage/links/cleanup",
                json={
                    "dry_run": not args.apply,
                    "limit": args.limit,
                    "product_id": grant["product_id"],
                },
            )
        )
    if args.operation == "create":
        body = read_json(args)
        if body.get("product_id", grant["product_id"]) != grant["product_id"]:
            raise ManageError(
                "Link payload belongs to a different product", code="no_scope"
            )
        body["product_id"] = grant["product_id"]
        with OutputFile(args, "management-link") as output:
            result = _object(
                client.request(grant, "POST", "/api/manage/links", json=body)
            )
            saved = output.write(result)
            return {
                **saved,
                "link": _only(
                    result,
                    (
                        "id",
                        "product_id",
                        "name",
                        "permissions",
                        "expires",
                        "max_uses",
                        "max_cli_uses",
                    ),
                ),
            }
    if args.operation == "revoke":
        return _request(
            client, grant, "POST", f"/api/manage/links/{quote(args.id, safe='')}/revoke"
        )
    items = _objects(
        _request(client, grant, "GET", "/api/manage/links", params={"view": args.view})
    )
    fields = (
        "id",
        "product_id",
        "name",
        "permissions",
        "expires",
        "revoked",
        "max_uses",
        "uses",
        "remaining_uses",
        "max_cli_uses",
        "cli_uses",
        "remaining_cli_uses",
    )
    return {
        "ok": True,
        "links": items if args.detail else [_only(item, fields) for item in items],
    }


# Paths and verbs are deliberately explicit. A generic request cannot turn a
# product grant into owner authority or send credentials to an arbitrary URL.
_API_ROUTES = (
    ("GET", r"products", ""),
    ("GET", r"product", "product.edit"),
    ("PUT", r"product", "product.edit"),
    ("DELETE", r"product", "product.delete"),
    ("POST", r"product/restore", "product.delete"),
    ("POST", r"product/purge", "product.purge"),
    ("POST", r"products/empty-trash", "product.purge"),
    ("GET", r"processors", "product.edit"),
    ("GET", r"jobs", "queue.view"),
    ("POST", r"batch", "queue.process"),
    ("GET", r"cards", "cards.manage"),
    ("POST", r"cards", "cards.manage"),
    ("POST", r"cards/import-text", "cards.manage"),
    ("POST", r"cards/[^/]+/revoke", "cards.manage"),
    ("GET", r"card-stats", "cards.manage"),
    ("GET", r"card-inventory", "cards.manage"),
    ("GET", r"cards/[^/]+/history", "cards.manage"),
    ("GET", r"links", "links.delegate"),
    ("POST", r"links", "links.delegate"),
    ("POST", r"links/[^/]+/revoke", "links.delegate"),
    ("POST", r"links/cleanup", "links.delegate"),
    ("GET", r"events", "events.manage"),
    ("POST", r"events/[^/]+/retry", "events.manage"),
    ("GET", r"sessions", ""),
    ("DELETE", r"sessions/[^/]+", ""),
    ("GET", r"audit", ""),
    ("GET", r"cli-devices", ""),
    ("DELETE", r"cli-devices/[^/]+", ""),
    ("GET", r"files", "queue.view"),
)


def api_request(client, args, origin):
    parsed = urlsplit(args.path)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not args.path.startswith("/api/manage/")
        or any(ord(char) < 33 for char in args.path)
        or any(part in (".", "..") for part in parsed.path.split("/"))
        or "%" in args.path
        or "\\" in args.path
    ):
        raise ManageError(
            "Use one known relative /api/manage/ path without query strings",
            code="invalid_path",
        )
    relative = args.path.removeprefix("/api/manage/")
    if args.method == "GET" and relative == "progress-board":
        params = board_api_parameters(args)
        if read_json(args) is not None:
            raise ManageError(
                "Progress board does not accept a body", code="invalid_input"
            )
        result = client.boards(
            product=args.product,
            shop=params.get("shop_id"),
            origin=origin,
            grant_id=args.grant,
            view=params["view"],
            limit=params["limit"],
            offset=params["offset"],
        )
        if result["errors"] or len(result["boards"]) != 1:
            raise ManageError(
                "Select one available progress board grant", code="no_scope"
            )
        board = result["boards"][0]["board"]
        if params.get("shop_id") and params["shop_id"] != board["shop"]["id"]:
            raise ManageError(
                "Progress board belongs to a different shop", code="no_scope"
            )
        return _finish(args, {"ok": True, "result": board})
    matched = next(
        (
            permission
            for method, pattern, permission in _API_ROUTES
            if method == args.method and re.fullmatch(pattern, relative)
        ),
        None,
    )
    if matched is None:
        raise ManageError(
            "This route has a dedicated command or is unavailable to product CLI credentials",
            code="invalid_path",
        )
    require_product_delete_confirmation(args)
    body = read_json(args)
    lifecycle = (
        "delete"
        if args.method == "DELETE" and relative == "product"
        else "restore"
        if args.method == "POST" and relative == "product/restore"
        else "purge"
        if args.method == "POST" and relative == "product/purge"
        else None
    )
    bulk = args.method == "POST" and relative == "products/empty-trash"
    if bulk:
        ids = purge_product_ids(body)
        if ids != [args.product]:
            raise ManageError(
                "Empty-trash product IDs must contain only the selected grant product",
                code="no_scope",
            )
    if lifecycle:
        body = product_lifecycle_body(args, lifecycle, body)
    if relative == "batch" and body and body.get("action") == "retry":
        matched = "queue.retry"
    grant = _grant(client, args, origin, matched)
    query = {}
    for item in args.query:
        key, separator, value = item.partition("=")
        if not separator or not key or key in query:
            raise ManageError(
                "Queries use unique KEY=VALUE entries", code="invalid_input"
            )
        if key == "product_id" and value != grant["product_id"]:
            raise ManageError("Query belongs to a different product", code="no_scope")
        query[key] = value
    query["product_id"] = grant["product_id"]
    if body and body.get("product_id", grant["product_id"]) != grant["product_id"]:
        raise ManageError("JSON belongs to a different product", code="no_scope")
    if args.method in ("POST", "PUT") and relative in (
        "cards",
        "cards/import-text",
        "links",
        "batch",
    ):
        if body is None:
            raise ManageError(
                "This operation requires a JSON input object", code="invalid_input"
            )
        body["product_id"] = grant["product_id"]
    if relative == "products":
        query.setdefault("compact", "true" if not args.detail else "false")
        query.setdefault("view", "active")
    if relative == "jobs":
        query.setdefault("compact", "true" if not args.detail else "false")
        query.setdefault("view", "active")
    secret_result = args.method == "POST" and relative in (
        "cards",
        "cards/import-text",
        "links",
    )
    if secret_result:
        with OutputFile(args, "api-export") as output:
            return output.write(
                client.request(grant, args.method, args.path, params=query, json=body)
            )
    result = client.request(
        grant,
        args.method,
        args.path,
        params=query,
        **({"json": body} if body is not None else {}),
    )
    if lifecycle:
        result = product_lifecycle_result(result, grant["product_id"], lifecycle)
    if bulk:
        result = purge_result(result, ids)
    if not args.detail and relative == "processors":
        result = [_processor_summary(item) for item in _objects(result)]
    return _finish(args, {"ok": True, "result": _public(result)})

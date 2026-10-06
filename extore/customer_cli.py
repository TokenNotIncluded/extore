"""Remote customer operations without browser file pickers or merchant credentials."""

import getpass
import json
import os
import secrets
import stat
import sys
import time
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


def _receipt_shape(value):
    _object(value)
    _product_shape(value.get("product"))
    if "batch" in value and type(value["batch"]) is not bool:
        raise ManageError("Invalid receipt response", code="invalid_response")
    if value.get("batch"):
        items = _objects(value.get("items"))
        if not 1 <= len(items) <= 30:
            raise ManageError("Invalid receipt items", code="invalid_response")
        for item in items:
            if not isinstance(item.get("card_id"), str) or not item["card_id"]:
                raise ManageError("Invalid receipt card", code="invalid_response")
            _product_shape(item.get("product"))
            _job_shape(item.get("job"))
            if item.get("variant") is not None:
                _object(item["variant"])
    else:
        _job_shape(value.get("job"))
        if value.get("variant") is not None:
            _object(value["variant"])
    return value


def _summary_product(product):
    _product_shape(product)
    result = {
        key: product[key]
        for key in ("id", "name", "logo", "mode", "delivery")
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
    return result


def _summary_job(job):
    _job_shape(job)
    if job is None:
        return None
    return {
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
        )
        if key in job
    }


def _summary_variant(value):
    if value is None:
        return None
    _object(value)
    return {
        key: value[key] for key in ("id", "name", "price", "currency") if key in value
    }


def _summary_receipt(value):
    _receipt_shape(value)
    if value.get("batch"):
        return {
            "batch": True,
            "product": _summary_product(value["product"]),
            "items": [
                {
                    "card_id": item["card_id"],
                    "suffix": item.get("suffix", ""),
                    "variant": _summary_variant(item.get("variant")),
                    "job": _summary_job(item.get("job")),
                }
                for item in value["items"]
            ],
        }
    return {
        "batch": False,
        "product": _summary_product(value["product"]),
        "variant": _summary_variant(value.get("variant")),
        "job": _summary_job(value.get("job")),
    }


def _schema(product, detail=False):
    if detail:
        return product
    return {
        **_summary_product(product),
        "parameters": [
            {
                key: field[key]
                for key in ("key", "label", "type", "required")
                if key in field
            }
            for field in product.get("parameters", [])
        ],
        "outputs": [
            {
                key: field[key]
                for key in ("key", "label", "type", "required")
                if key in field
            }
            for field in product.get("outputs", [])
        ],
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
    if any(fields[key].get("type") != "file" for key in attached):
        raise ManageError(
            "An attachment must target a file parameter", code="invalid_input"
        )
    for key, field in fields.items():
        if (
            field.get("required", True)
            and not value.get(key, "").strip()
            and key not in attached
        ):
            raise ManageError(
                f"Required parameter is missing: {key}", code="invalid_input"
            )


def _private_output(target):
    if target.exists() or target.is_symlink():
        raise ManageError(
            "Output already exists; choose another path", code="output_exists"
        )
    return os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)


class CustomerClient:
    def __init__(self, data=None, *, transport=None, persist=None):
        self.data = data
        self.persist = persist or (lambda: None)
        self.http = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(120, connect=15),
            follow_redirects=False,
            trust_env=False,
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
                entry["origin"], "POST", "/api/receipt", json={"token": entry["token"]}
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
                (item for item in value["items"] if item["card_id"] == card_id), None
            )
            if item is None:
                raise ManageError("Card is not in this receipt", code="invalid_card")
            return item
        if card_id:
            raise ManageError(
                "--card is only used with a batch receipt", code="invalid_card"
            )
        return value

    def upload(self, entry, card_id, field, source, *, value=None):
        value = value or self.receipt(entry)
        selected = self.select(value, card_id)
        fields = {
            item["key"]: item for item in selected["product"].get("parameters", [])
        }
        if field not in fields or fields[field].get("type") != "file":
            raise ManageError(
                "Select a file parameter from customer schema", code="invalid_input"
            )
        row = selected.get("job")
        if row and (
            row.get("state") not in ("failed", "needs_input")
            or not row.get("can_retry")
        ):
            raise ManageError(
                "This task cannot accept new input files", code="invalid_state"
            )
        limits = _object(self.json(entry["origin"], "GET", "/api/upload-limits"))
        limit = limits.get("max_file_bytes")
        if type(limit) is not int or limit < 1:
            raise ManageError("Invalid server upload limit", code="invalid_response")
        limit = min(limit, MAX_FILE_BYTES)
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ManageError(
                f"Upload must be a regular file no larger than {limit} bytes",
                code="invalid_upload",
            )
        data = {"token": entry["token"], "field_key": field}
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
        entry.setdefault("inputs", {}).setdefault(card_id or "single", {})[field] = (
            descriptor
        )
        self.persist()
        return {"ok": True, "receipt_id": entry["id"], "field": field, **descriptor}

    def redeem(
        self, entry, params, *, card_id=None, items=None, attachments=(), retry=False
    ):
        value = self.receipt(entry)
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
                _validate_params(_params(item["params"]), selected["product"])
            body["items"] = items
        else:
            selected = self.select(value, card_id)
            self._submit_state(selected, retry)
            params = _params(params)
            attached = {}
            for specification in attachments:
                field, separator, path = specification.partition("=")
                if not separator or not field or not path or field in attached:
                    raise ManageError(
                        "Use --file FIELD=PATH once per file field",
                        code="invalid_input",
                    )
                attached[field] = Path(path).expanduser()
            _validate_params(params, selected["product"], attached=attached)
            # Validate every local attachment before starting any upload.
            for source in attached.values():
                info = source.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                    raise ManageError(
                        "Attachment is not a regular file or exceeds 20 MiB",
                        code="invalid_upload",
                    )
            for field, source in attached.items():
                params[field] = self.upload(entry, card_id, field, source, value=value)[
                    "id"
                ]
            if value.get("batch"):
                body["items"] = [{"card_id": card_id, "params": params}]
            else:
                body["params"] = params
        result = _object(self.json(entry["origin"], "POST", "/api/redeem", json=body))
        if not result.get("batch"):
            result = {**value, "job": result}
        entry["status"] = _summary_receipt(result)
        return {"ok": True, "receipt_id": entry["id"], **entry["status"]}

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

    def reveal(self, entry, card_id, target):
        self.select(self.receipt(entry), card_id)
        # Reserve the private output before consuming a single-view delivery.
        fd = _private_output(target)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as destination:
                body = {"token": entry["token"]}
                if card_id:
                    body["card_id"] = card_id
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
                entry.setdefault("files", {})[card_id or "single"] = files
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
            "files": [
                {
                    key: item[key]
                    for key in ("id", "field_key", "filename", "size")
                    if key in item
                }
                for item in files
            ],
        }

    def download(self, entry, card_id, file_id, target):
        self.select(self.receipt(entry), card_id)
        files = entry.get("files", {}).get(card_id or "single", [])
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
    schema.add_argument("--card", help="card ID in a batch receipt")
    schema.add_argument(
        "--detail",
        action="store_true",
        help="include Markdown tutorials and descriptions",
    )
    exchange = commands.add_parser(
        "exchange",
        help="verify 1 to 30 same-product codes and privately save the receipt",
    )
    exchange.add_argument("--origin", required=True)
    exchange.add_argument(
        "--codes-stdin",
        action="store_true",
        required=True,
        help="read private codes from standard input, never command arguments",
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
        command.add_argument(
            "--card", help="required for a particular card in a batch receipt"
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
        if name == "upload":
            command.add_argument("--field", required=True)
            command.add_argument("--file", type=Path, required=True)
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
    return customer


def dispatch(client, args):
    command = args.customer_command
    if command in ("products", "schema") and not getattr(args, "receipt_id", None):
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
        value = _object(
            client.json(origin, "POST", "/api/exchange", json={"code": code})
        )
        return client.remember(origin, value.get("token"), value)
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
            selected = client.select(value, card_id)
            return {
                "ok": True,
                "receipt_id": entry["id"],
                "product": _schema(selected["product"], args.detail),
            }
        if command == "files":
            client.select(value, card_id)
            return {
                "ok": True,
                "receipt_id": entry["id"],
                "cached": True,
                "inputs": list(
                    entry.get("inputs", {}).get(card_id or "single", {}).values()
                ),
                "outputs": [
                    {
                        key: item[key]
                        for key in ("id", "field_key", "filename", "size")
                        if key in item
                    }
                    for item in entry.get("files", {}).get(card_id or "single", [])
                ],
            }
        if card_id:
            selected = client.select(value, card_id)
            result = {
                "ok": True,
                "receipt_id": entry["id"],
                "card_id": card_id,
                "job": _summary_job(selected.get("job")),
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
    if command in ("redeem", "retry"):
        if getattr(args, "reuse", False):
            if args.file:
                raise ManageError(
                    "--reuse cannot upload new attachments", code="invalid_input"
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
        )
    if command == "upload":
        return client.upload(entry, card_id, args.field, args.file)
    if command == "reveal":
        return client.reveal(entry, card_id, args.output)
    if command == "download":
        return client.download(entry, card_id, args.file_id, args.output)
    if command == "destroy":
        client.select(client.receipt(entry), card_id)
        body = {"token": entry["token"]}
        if card_id:
            body["card_id"] = card_id
        result = _object(
            client.json(entry["origin"], "POST", "/api/receipt/destroy", json=body)
        )
        entry.setdefault("files", {}).pop(card_id or "single", None)
        if result.get("ok") is not True:
            raise ManageError("Invalid destroy response", code="invalid_response")
        return {"ok": True, "receipt_id": entry["id"]}
    raise ManageError("Unknown customer command", code="invalid_input")


def execute(args, *, transport=None):
    if args.customer_command == "products" or (
        args.customer_command == "schema" and args.product
    ):
        with CustomerClient(transport=transport) as client:
            return dispatch(client, args)
    path = profile_path(args.profile)
    with private_profile(
        path, create=args.customer_command in ("exchange", "import-receipt")
    ) as data:
        with CustomerClient(
            data, transport=transport, persist=lambda: _save_profile(path, data)
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

"""Bounded attachment storage with card, job, field and role authorization."""

import asyncio
import json
import re
import sqlite3
import tempfile
import time
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from .card_tracking import ensure_card_usable
from .config import (
    UPLOAD_FILE_BYTES,
    UPLOAD_JOB_BYTES,
    UPLOAD_JOB_FILES,
    UPLOAD_TIMEOUT_SECONDS,
)
from .db import db
from .field_values import ATTACHMENT_TYPES, IMAGE_TYPES, attachment_ids
from .image_validation import image_content_type
from .security import (
    authorize_management,
    fail,
    rate_limit,
    resolve_customer_card,
    session,
)

router = APIRouter()
MAX_FILE_BYTES = UPLOAD_FILE_BYTES
MAX_CARD_BYTES = UPLOAD_JOB_BYTES
MAX_CARD_FILES = UPLOAD_JOB_FILES
MAX_MULTIPART_BYTES = MAX_FILE_BYTES + 64 * 1024
CHUNK_BYTES = 64 * 1024
_METADATA = (
    "id,card_id,product_id,job_id,field_key,kind,attempt,filename,content_type,size,"
    "bound,released,consumed,created,content IS NOT NULL AS available"
)


class FileDownload(BaseModel):
    token: str = Field(max_length=100)
    file_id: str = Field(min_length=1, max_length=100)
    card_id: str | None = Field(default=None, max_length=80)


def init_schema(c):
    c.execute(
        "CREATE TABLE IF NOT EXISTS job_files ("
        "id TEXT PRIMARY KEY, card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE, "
        "product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE, "
        "job_id TEXT REFERENCES jobs(id) ON DELETE CASCADE, field_key TEXT NOT NULL, "
        "kind TEXT NOT NULL CHECK(kind IN ('input','output')), attempt INTEGER NOT NULL DEFAULT 1, "
        "filename TEXT NOT NULL, content_type TEXT NOT NULL, size INTEGER NOT NULL, "
        "content BLOB, bound INTEGER NOT NULL DEFAULT 0, released INTEGER NOT NULL DEFAULT 0, "
        "consumed INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, "
        "CHECK(size>=0 AND size<=20971520))"
    )
    c.execute("CREATE INDEX IF NOT EXISTS job_files_card ON job_files(card_id)")
    c.execute("CREATE INDEX IF NOT EXISTS job_files_job ON job_files(job_id,kind)")
    from .storage import init_schema as init_storage_schema

    init_storage_schema(c)


def _product(c, card):
    from .service import product

    return product(c, card["product_id"])


def _job_product(c, row):
    from . import service

    resolver = getattr(service, "job_product", None)
    return resolver(c, row) if resolver else service.product(c, row["product_id"])


def _row(c, jid):
    row = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        fail("任务不存在", 404)
    return row


def _field(product, key, kind):
    fields = product["parameters" if kind == "input" else "outputs"]
    for field in fields:
        if field["key"] == key and field["type"] in ATTACHMENT_TYPES:
            return field
    fail("商品未定义这个文件字段")


def _descriptor(row):
    result = {
        key: row[key]
        for key in (
            "id",
            "job_id",
            "field_key",
            "kind",
            "filename",
            "content_type",
            "size",
            "created",
            "consumed",
            "attempt",
        )
    }
    result["available"] = bool(row["available"])
    for key in ("flow_epoch", "node_id"):
        if key in row.keys():
            result[key] = row[key]
    return result


def _with_flow_scope(c, row):
    if c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_flow_files'"
    ).fetchone():
        scope = c.execute(
            "SELECT flow_epoch,node_id FROM task_flow_files WHERE file_id=?",
            (row["id"],),
        ).fetchone()
        if scope:
            return {**dict(row), **dict(scope)}
    return row


def listfiles(c, row, kind=None):
    """Return descriptors only; authorization belongs to the caller."""
    return [
        _descriptor(_with_flow_scope(c, item))
        for item in c.execute(
            "SELECT "
            + _METADATA
            + " FROM job_files WHERE job_id=? AND product_id=? AND card_id=? "
            "AND (? IS NULL OR kind=?) ORDER BY created,id",
            (row["id"], row["product_id"], row["card_id"], kind, kind),
        )
    ]


def _file(c, fid, *, content=False):
    columns = "*,content IS NOT NULL AS available" if content else _METADATA
    row = c.execute(
        "SELECT " + columns + " FROM job_files WHERE id=?", (fid,)
    ).fetchone()
    if not row:
        fail("文件不存在或已删除", 404)
    return _with_flow_scope(c, row)


def _input_scope(c, card, field_key, *, flow_epoch=None, revision=None, node_id=None):
    from .shops import require_enabled_product

    require_enabled_product(c, card["product_id"])
    ensure_card_usable(c, card)
    existing = c.execute("SELECT * FROM jobs WHERE card_id=?", (card["id"],)).fetchone()
    if existing:
        from .flow_adapter import input_file_scope

        flow_scope = input_file_scope(
            c,
            card,
            field_key,
            flow_epoch=flow_epoch,
            revision=revision,
            node_id=node_id,
        )
        if flow_scope:
            return flow_scope
        from .service import job_view

        if not job_view(c, existing)["can_retry"]:
            fail("提交后的任务不能替换输入文件", 409)
        p = _job_product(c, existing)
    else:
        if card["state"] != "ready":
            fail("此卡密不能上传输入文件", 409)
        p = _product(c, card)
    _field(p, field_key, "input")
    return p, existing


def _manage_scope(c, s, row, permission):
    from .shops import authorize_product

    authorize_management(c, s, permission)
    authorize_product(c, s, row["product_id"])


def _output_scope(c, s, jid, field_key, *, flow_epoch=None, action_id=None):
    row = _row(c, jid)
    _manage_scope(c, s, row, "queue.process")
    p = _job_product(c, row)
    actor = (
        s["staff_id"]
        if s["role"] == "staff"
        else s.get("account_id")
        or (f"shop:{s['shop_id']}" if s.get("shop_id") else "owner")
    )
    if p["mode"] != "manual":
        fail("只能为队列商品上传交付文件", 409)
    if row["state"] != "processing" or row["claimed_by"] != actor:
        fail("请先领取任务，且只能处理自己领取的任务", 409)
    from .flow_adapter import output_file_scope

    execution = output_file_scope(c, row, flow_epoch=flow_epoch)
    if execution and action_id is not None and action_id != execution["action_id"]:
        fail("上传所属处理动作已改变", 409)
    _field(p, field_key, "output")
    return row


def _flow_upload_args(fields, *, output=False):
    result = {}
    for key, name in (("flow_epoch", "flow_epoch"), ("expected_revision", "revision")):
        if output and key != "flow_epoch":
            continue
        value = fields.get(key)
        if value is not None:
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,9}", value):
                fail("上传步骤标识无效", 422)
            result[name] = int(value)
    if not output and "node_id" in fields:
        result["node_id"] = fields["node_id"]
    if output and "action_id" in fields:
        value = fields["action_id"]
        if not isinstance(value, str) or not 1 <= len(value) <= 100:
            fail("上传处理动作无效", 422)
        result["action_id"] = value
    return result


def _bind_flow_upload(c, row, key, kind, descriptor):
    from . import task_flow
    from .flow_adapter import bind_stage_file

    if row is not None and task_flow.is_flow(c, row):
        state = task_flow.view(c, row)
        bind_stage_file(
            c, row, state["current"]["id"], state["flow_epoch"], kind, descriptor["id"]
        )
    return descriptor


class _QuotaParser(MultiPartParser):
    spool_max_size = CHUNK_BYTES

    def __init__(
        self, *args, reservation, scope_fields, optional_fields, bind_scope, **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.reservation = reservation
        self.scope_fields = set(scope_fields)
        self.optional_fields = set(optional_fields)
        self.bind_scope = bind_scope
        self.file_bytes = 0
        self.file_started = False

    def on_headers_finished(self):
        super().on_headers_finished()
        if self._current_part.file is not None:
            from .storage import _directory

            # Keep spools on the same checked filesystem as the SQLite database.
            # Replacing Starlette's empty spool here is instance-local and leaves
            # no named temporary payload behind if a worker crashes.
            old = self._current_part.file.file
            spool = tempfile.SpooledTemporaryFile(
                max_size=self.spool_max_size, dir=_directory()
            )
            self._current_part.file.file = spool
            self._files_to_close_on_error.remove(old)
            self._files_to_close_on_error.append(spool)
            old.close()
            if self._current_part.field_name != "file":
                raise MultiPartException("Unexpected file field")
            fields = {}
            for key, value in self.items:
                if (
                    key in fields
                    or key not in self.scope_fields | self.optional_fields
                    or not isinstance(value, str)
                ):
                    raise MultiPartException("Repeated or unexpected upload field")
                fields[key] = value
            if not self.scope_fields <= set(fields):
                raise MultiPartException("extore_upload_field_order")
            # Authenticate the product before accepting even the first payload
            # byte, so its shop's in-flight quota is part of every reservation.
            self.bind_scope(fields)
            self.file_started = True
        elif self.file_started:
            raise MultiPartException("extore_upload_field_order")

    def on_part_data(self, data, start, end):
        if self._current_part.file is not None:
            self.file_bytes += end - start
            if self.file_bytes > MAX_FILE_BYTES:
                raise MultiPartException("extore_upload_too_large")
            self.reservation.grow(end - start)
        super().on_part_data(data, start, end)

    def on_header_field(self, data, start, end):
        if len(self._current_partial_header_name) + end - start > 4096:
            raise MultiPartException("Multipart header is too large")
        super().on_header_field(data, start, end)

    def on_header_value(self, data, start, end):
        if len(self._current_partial_header_value) + end - start > 4096:
            raise MultiPartException("Multipart header is too large")
        super().on_header_value(data, start, end)


def upload_limits():
    return {
        "max_file_bytes": MAX_FILE_BYTES,
        "max_card_bytes": MAX_CARD_BYTES,
        "max_card_files": MAX_CARD_FILES,
    }


@router.get("/api/upload-limits")
def public_upload_limits():
    return upload_limits()


def _byte_limit(value):
    return (
        f"{value // (1024 * 1024)} MiB"
        if value % (1024 * 1024) == 0
        else f"{value} 字节"
    )


def file_limit_message():
    return f"上传文件超过 {_byte_limit(MAX_FILE_BYTES)} 限制"


async def _multipart(request, expected, reservation, bind_scope, optional=()):
    """Count the raw stream before the multipart parser can spool an unbounded file."""
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data;"):
        fail("请使用 multipart/form-data 上传文件")
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) < 0 or int(length) > MAX_MULTIPART_BYTES:
                fail(file_limit_message(), 413)
        except ValueError:
            fail("上传请求格式不正确")
    total = 0

    async def bounded():
        nonlocal total
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_MULTIPART_BYTES:
                # MultiPartParser catches this class and closes its spool files.
                raise MultiPartException("extore_upload_too_large")
            for start in range(0, len(chunk), CHUNK_BYTES):
                yield chunk[start : start + CHUNK_BYTES]

    parser = _QuotaParser(
        request.headers,
        bounded(),
        max_files=1,
        max_fields=len(expected) + len(optional) - 1,
        max_part_size=512,
        reservation=reservation,
        scope_fields=tuple(key for key in expected if key != "file"),
        optional_fields=optional,
        bind_scope=bind_scope,
    )
    try:
        form = await parser.parse()
    except (MultiPartException, MultipartParseError) as exc:
        # Starlette closes spools for MultiPartException. Also close them when
        # the lower-level parser rejects malformed binary multipart framing.
        for spool in getattr(parser, "_files_to_close_on_error", ()):
            spool.close()
        too_large = getattr(exc, "message", "") == "extore_upload_too_large"
        if getattr(exc, "message", "") == "extore_upload_field_order":
            fail("请将上传参数放在文件之前", 400)
        fail(
            file_limit_message() if too_large else "上传请求格式不正确",
            413 if too_large else 400,
        )
    except BaseException:
        # Keep cleanup explicit for quota failures, cancellation and timeouts,
        # independently of the parser version's own error cleanup guarantees.
        for spool in getattr(parser, "_files_to_close_on_error", ()):
            spool.close()
        raise
    allowed = set(expected) | set(optional)
    if not set(expected) <= set(form) <= allowed or any(
        len(form.getlist(key)) != 1 for key in form
    ):
        await _close_form(form)
        fail("上传请求包含重复或未定义的字段")
    text_keys = [key for key in (*expected, *optional) if key != "file" and key in form]
    if not isinstance(form["file"], UploadFile) or any(
        not isinstance(form[key], str) for key in text_keys
    ):
        await _close_form(form)
        fail("上传请求格式不正确")
    return form


async def _close_form(form):
    try:
        await form.close()
    finally:
        # A second cancellation during the asynchronous close must not retain
        # an anonymous disk spool or its file descriptor.
        for _, item in form.multi_items():
            if isinstance(item, UploadFile):
                item.file.close()


def _filename(value):
    value = re.sub(r"[\x00-\x1f\x7f]", "", value or "")
    value = value.replace("\\", "/").split("/")[-1].strip()[:180]
    return value or "attachment"


def _content_type(value):
    return (
        value
        if value
        and len(value) <= 100
        and re.fullmatch(r"[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+", value)
        else "application/octet-stream"
    )


def _store(
    c,
    card_id,
    product_id,
    job_id,
    attempt,
    key,
    kind,
    upload,
    reservation,
    *,
    field=None,
):
    from .storage import check_storage_quota, require_disk_space

    size = upload.size
    if size is None or size < 0 or size > MAX_FILE_BYTES:
        fail(file_limit_message(), 413)
    inventory = c.execute(
        "SELECT COALESCE(SUM(size),0) AS size,COUNT(*) AS count FROM job_files WHERE card_id=? AND content IS NOT NULL",
        (card_id,),
    ).fetchone()
    if inventory["size"] + size > MAX_CARD_BYTES:
        fail(f"此卡密的文件总量超过 {_byte_limit(MAX_CARD_BYTES)} 限制", 413)
    if inventory["count"] >= MAX_CARD_FILES:
        fail(f"此卡密的文件数量超过 {MAX_CARD_FILES} 个限制", 413)
    check_storage_quota(c, size, reservation_id=reservation.id, product_id=product_id)
    content_type = _content_type(upload.content_type)
    if field and field["type"] in IMAGE_TYPES:
        try:
            content_type = image_content_type(upload.file)
        except ValueError as exc:
            fail(str(exc))
    fid = str(uuid.uuid4())
    cursor = c.execute(
        "INSERT INTO job_files(id,card_id,product_id,job_id,field_key,kind,attempt,filename,content_type,size,content,created) VALUES (?,?,?,?,?,?,?,?,?,?,zeroblob(?),?)",
        (
            fid,
            card_id,
            product_id,
            job_id,
            key,
            kind,
            attempt,
            _filename(upload.filename),
            content_type,
            size,
            size,
            time.time(),
        ),
    )
    # Incremental SQLite I/O avoids two whole-file copies in Python memory.
    # The transaction serializes quota accounting with all other upload commits.
    upload.file.seek(0)
    if size:
        with c.blobopen("job_files", "content", cursor.lastrowid) as blob:
            written = 0
            while chunk := upload.file.read(CHUNK_BYTES):
                if written + len(chunk) > size:
                    fail("上传文件大小发生变化，请重试", 409)
                require_disk_space(size - written)
                blob.write(chunk)
                written += len(chunk)
            if written != size:
                fail("上传文件不完整，请重新上传", 400)
    elif upload.file.read(1):
        fail("上传文件大小发生变化，请重试", 409)
    reservation.finish(c)
    return _descriptor(_file(c, fid))


@router.post("/api/files/upload")
async def upload_input(request: Request):
    from .storage import receiving_upload, translate_storage_error

    rate_limit(request, "files-upload", 60, 60)
    try:
        with receiving_upload() as reservation:

            def bind_scope(fields):
                with db() as c:
                    card = resolve_customer_card(
                        c, fields["token"], fields.get("card_id") or None
                    )
                    _input_scope(
                        c, card, fields["field_key"], **_flow_upload_args(fields)
                    )
                    reservation.bind_product(c, card["product_id"])

            async with asyncio.timeout(UPLOAD_TIMEOUT_SECONDS):
                form = await _multipart(
                    request,
                    ("token", "field_key", "file"),
                    reservation,
                    bind_scope,
                    optional=("card_id", "flow_epoch", "expected_revision", "node_id"),
                )
                try:
                    with db() as c:
                        card = resolve_customer_card(
                            c, form["token"], form.get("card_id") or None
                        )
                        p, row = _input_scope(
                            c, card, form["field_key"], **_flow_upload_args(form)
                        )
                        descriptor = _store(
                            c,
                            card["id"],
                            card["product_id"],
                            row["id"] if row and p.get("task_flow") else None,
                            row["attempt"] if row else 1,
                            form["field_key"],
                            "input",
                            form["file"],
                            reservation,
                            field=_field(p, form["field_key"], "input"),
                        )
                        return _bind_flow_upload(
                            c, row, form["field_key"], "input", descriptor
                        )
                finally:
                    await _close_form(form)
    except TimeoutError:
        fail("上传超时，请重新上传", 408)
    except (OSError, sqlite3.Error) as exc:
        translate_storage_error(exc)


@router.post("/api/manage/files/upload")
async def upload_output(request: Request):
    from .storage import receiving_upload, translate_storage_error

    rate_limit(request, "files-upload", 20, 60)
    s = session(request, ("admin", "staff"))
    try:
        with receiving_upload() as reservation:

            def bind_scope(fields):
                current = session(request, ("admin", "staff"))
                with db() as c:
                    row = _output_scope(
                        c,
                        current,
                        fields["job_id"],
                        fields["field_key"],
                        **_flow_upload_args(fields, output=True),
                    )
                    reservation.bind_product(c, row["product_id"])

            async with asyncio.timeout(UPLOAD_TIMEOUT_SECONDS):
                form = await _multipart(
                    request,
                    ("job_id", "field_key", "file"),
                    reservation,
                    bind_scope,
                    optional=("flow_epoch", "action_id"),
                )
                try:
                    s = session(request, ("admin", "staff"))
                    with db() as c:
                        row = _output_scope(
                            c,
                            s,
                            form["job_id"],
                            form["field_key"],
                            **_flow_upload_args(form, output=True),
                        )
                        descriptor = _store(
                            c,
                            row["card_id"],
                            row["product_id"],
                            row["id"],
                            row["attempt"],
                            form["field_key"],
                            "output",
                            form["file"],
                            reservation,
                            field=_field(
                                _job_product(c, row), form["field_key"], "output"
                            ),
                        )
                        return _bind_flow_upload(
                            c, row, form["field_key"], "output", descriptor
                        )
                finally:
                    await _close_form(form)
    except TimeoutError:
        fail("上传超时，请重新上传", 408)
    except (OSError, sqlite3.Error) as exc:
        translate_storage_error(exc)


def _attachment_ids(field, value):
    try:
        return attachment_ids(field, value)
    except ValueError as exc:
        fail(str(exc))


def _validate_image(c, item, field):
    if field["type"] not in IMAGE_TYPES:
        return
    # A file uploaded before a product's schema change may have a forged raster
    # MIME label. Recheck its bytes instead of trusting the stored descriptor.
    rowid = c.execute(
        "SELECT rowid FROM job_files WHERE id=?", (item["id"],)
    ).fetchone()[0]
    try:
        with c.blobopen("job_files", "content", rowid, readonly=True) as blob:
            content_type = image_content_type(blob)
    except ValueError as exc:
        fail(str(exc))
    if item["content_type"] != content_type:
        c.execute(
            "UPDATE job_files SET content_type=? WHERE id=?", (content_type, item["id"])
        )


def validate_input_files(c, card, p, params):
    existing = c.execute(
        "SELECT id,attempt FROM jobs WHERE card_id=?", (card["id"],)
    ).fetchone()
    for field in p["parameters"]:
        for fid in _attachment_ids(field, params.get(field["key"], "")):
            item = _file(c, fid)
            if (
                item["card_id"] != card["id"]
                or item["product_id"] != p["id"]
                or item["field_key"] != field["key"]
                or item["kind"] != "input"
                or item["attempt"] != (existing["attempt"] if existing else 1)
                or not item["available"]
                or (
                    item["job_id"] is not None
                    and (not existing or item["job_id"] != existing["id"])
                )
            ):
                fail("输入文件不属于此卡密、尝试或字段", 403)
            _validate_image(c, item, field)


def validate_output_files(c, row, p, output):
    for field in p["outputs"]:
        for fid in _attachment_ids(field, output.get(field["key"], "")):
            item = _file(c, fid)
            if (
                item["card_id"] != row["card_id"]
                or item["product_id"] != row["product_id"]
                or item["job_id"] != row["id"]
                or item["attempt"] != row["attempt"]
                or item["field_key"] != field["key"]
                or item["kind"] != "output"
                or not item["available"]
            ):
                fail("交付文件不属于此任务、尝试或字段", 403)
            _validate_image(c, item, field)


def bind_inputs(c, row, params):
    selected = {
        fid
        for field in _job_product(c, row)["parameters"]
        for fid in _attachment_ids(field, params.get(field["key"], ""))
    }
    for item in c.execute(
        "SELECT id FROM job_files WHERE card_id=? AND kind='input'", (row["card_id"],)
    ).fetchall():
        if item["id"] in selected:
            c.execute(
                "UPDATE job_files SET job_id=?,attempt=?,bound=1 WHERE id=?",
                (row["id"], row["attempt"], item["id"]),
            )
        else:
            c.execute("DELETE FROM job_files WHERE id=?", (item["id"],))


def bind_outputs(c, row, output):
    selected = {
        fid
        for field in _job_product(c, row)["outputs"]
        for fid in _attachment_ids(field, output.get(field["key"], ""))
    }
    for item in c.execute(
        "SELECT id FROM job_files WHERE job_id=? AND kind='output'", (row["id"],)
    ).fetchall():
        if item["id"] in selected:
            c.execute("UPDATE job_files SET bound=1 WHERE id=?", (item["id"],))
        else:
            c.execute("DELETE FROM job_files WHERE id=?", (item["id"],))


def release_output_files(c, row):
    output = json.loads(row["result_json"]) if row["result_json"] else {}
    p = _job_product(c, row)
    validate_output_files(c, row, p, output)
    ids = [
        fid
        for field in p["outputs"]
        for fid in _attachment_ids(field, output.get(field["key"], ""))
    ]
    result = []
    for fid in ids:
        item = _file(c, fid)
        if not item["bound"]:
            fail("交付文件尚未完成", 409)
        c.execute("UPDATE job_files SET released=1 WHERE id=?", (fid,))
        result.append(_descriptor(item))
    return result


def purge_job_outputs(c, jid):
    c.execute("DELETE FROM job_files WHERE job_id=? AND kind='output'", (jid,))


def purge_job_files(c, jid):
    row = c.execute("SELECT card_id FROM jobs WHERE id=?", (jid,)).fetchone()
    if row:
        c.execute("DELETE FROM job_files WHERE card_id=?", (row["card_id"],))


def _download(item):
    # Never render uploads inline; filenames and MIME values are untrusted data.
    return Response(
        bytes(item["content"]),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": "attachment; filename=\"download\"; filename*=UTF-8''"
            + quote(item["filename"], safe=""),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


@router.get("/api/manage/files")
def managed_files(request: Request, job_id: str):
    s = session(request, ("admin", "staff"))
    with db() as c:
        row = _row(c, job_id)
        _manage_scope(c, s, row, "queue.view")
        return listfiles(c, row)


@router.get("/api/manage/files/{file_id}/download")
def managed_download(file_id: str, request: Request):
    rate_limit(request, "files-download", 60, 60)
    s = session(request, ("admin", "staff"))
    with db() as c:
        # Resolve access using metadata before loading an untrusted attachment.
        # A guessed ID from another shop must not allocate its BLOB in memory.
        item = _file(c, file_id)
        if item["job_id"] is None:
            fail("文件尚未提交到任务", 404)
        row = _row(c, item["job_id"])
        _manage_scope(c, s, row, "queue.view")
        if item["product_id"] != row["product_id"] or item["card_id"] != row["card_id"]:
            fail("文件不属于此任务", 403)
        if row["state"] == "destroyed" or not item["available"]:
            fail("文件已销毁或领取", 410)
        return _download(_file(c, file_id, content=True))


@router.post("/api/files/download")
def customer_download(body: FileDownload, request: Request):
    rate_limit(request, "files-download", 60, 60)
    with db() as c:
        card = resolve_customer_card(c, body.token, body.card_id)
        item = _file(c, body.file_id)
        if (
            item["card_id"] != card["id"]
            or item["kind"] != "output"
            or not item["job_id"]
        ):
            fail("文件不属于此次领取", 403)
        row = _row(c, item["job_id"])
        if row["card_id"] != card["id"] or item["product_id"] != row["product_id"]:
            fail("文件不属于此次领取", 403)
        if row["state"] != "succeeded" or not row["revealed"] or not item["released"]:
            fail("请先领取商品后下载文件", 409)
        if not item["available"] or item["consumed"]:
            fail("文件已领取或销毁", 410)
        p = _job_product(c, row)
        _field(p, item["field_key"], "output")
        if item["attempt"] != row["attempt"]:
            fail("文件对应的尝试已失效", 409)
        response = _download(_file(c, body.file_id, content=True))
        if p["view_policy"] == "once":
            c.execute(
                "UPDATE job_files SET content=NULL,consumed=1 WHERE id=? AND consumed=0",
                (item["id"],),
            )
        return response

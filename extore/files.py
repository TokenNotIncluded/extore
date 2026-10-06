"""Bounded attachment storage with card, job, field and role authorization."""

import json
import re
import time
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from .card_tracking import ensure_card_usable
from .db import db
from .security import (
    authorize_management,
    fail,
    rate_limit,
    resolve_customer_card,
    session,
)

router = APIRouter()
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_CARD_BYTES = 100 * 1024 * 1024
MAX_CARD_FILES = 100
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
        if field["key"] == key and field["type"] == "file":
            return field
    fail("商品未定义这个文件字段")


def _descriptor(row):
    return {
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
        )
    }


def listfiles(c, row, kind=None):
    """Return descriptors only; authorization belongs to the caller."""
    return [
        _descriptor(item)
        for item in c.execute(
            "SELECT "
            + _METADATA
            + " FROM job_files WHERE job_id=? AND (? IS NULL OR kind=?) ORDER BY created,id",
            (row["id"], kind, kind),
        )
    ]


def _file(c, fid, *, content=False):
    columns = "*,content IS NOT NULL AS available" if content else _METADATA
    row = c.execute(
        "SELECT " + columns + " FROM job_files WHERE id=?", (fid,)
    ).fetchone()
    if not row:
        fail("文件不存在或已删除", 404)
    return row


def _input_scope(c, card, field_key):
    ensure_card_usable(c, card)
    existing = c.execute("SELECT * FROM jobs WHERE card_id=?", (card["id"],)).fetchone()
    if existing:
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
    authorize_management(c, s, permission)
    if s["role"] == "staff" and row["product_id"] != s["product_id"]:
        fail("无权访问此商品的文件", 403)


def _output_scope(c, s, jid, field_key):
    row = _row(c, jid)
    _manage_scope(c, s, row, "queue.process")
    p = _job_product(c, row)
    actor = s["staff_id"] if s["role"] == "staff" else "owner"
    if p["mode"] != "manual":
        fail("只能为队列商品上传交付文件", 409)
    if row["state"] != "processing" or row["claimed_by"] != actor:
        fail("请先领取任务，且只能处理自己领取的任务", 409)
    _field(p, field_key, "output")
    return row


async def _multipart(request, expected, optional=()):
    """Count the raw stream before the multipart parser can spool an unbounded file."""
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data;"):
        fail("请使用 multipart/form-data 上传文件")
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) < 0 or int(length) > MAX_MULTIPART_BYTES:
                fail("上传文件超过 20 MiB 限制", 413)
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
            yield chunk

    parser = MultiPartParser(
        request.headers,
        bounded(),
        max_files=1,
        max_fields=len(expected) + len(optional) - 1,
        max_part_size=512,
    )
    try:
        form = await parser.parse()
    except (MultiPartException, MultipartParseError) as exc:
        # Starlette closes spools for MultiPartException. Also close them when
        # the lower-level parser rejects malformed binary multipart framing.
        for spool in getattr(parser, "_files_to_close_on_error", ()):
            spool.close()
        too_large = getattr(exc, "message", "") == "extore_upload_too_large"
        fail(
            "上传文件超过 20 MiB 限制" if too_large else "上传请求格式不正确",
            413 if too_large else 400,
        )
    allowed = set(expected) | set(optional)
    if not set(expected) <= set(form) <= allowed or any(
        len(form.getlist(key)) != 1 for key in form
    ):
        await form.close()
        fail("上传请求包含重复或未定义的字段")
    text_keys = [key for key in (*expected, *optional) if key != "file" and key in form]
    if not isinstance(form["file"], UploadFile) or any(
        not isinstance(form[key], str) for key in text_keys
    ):
        await form.close()
        fail("上传请求格式不正确")
    return form


async def _contents(upload):
    result = bytearray()
    while chunk := await upload.read(CHUNK_BYTES):
        if len(result) + len(chunk) > MAX_FILE_BYTES:
            fail("上传文件超过 20 MiB 限制", 413)
        result.extend(chunk)
    return bytes(result)


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


def _store(c, card_id, product_id, job_id, attempt, key, kind, upload, content):
    # Unselected drafts have no long-lived retention claim.
    c.execute(
        "DELETE FROM job_files WHERE bound=0 AND created<?", (time.time() - 86400,)
    )
    inventory = c.execute(
        "SELECT COALESCE(SUM(size),0) AS size,COUNT(*) AS count FROM job_files WHERE card_id=? AND content IS NOT NULL",
        (card_id,),
    ).fetchone()
    if inventory["size"] + len(content) > MAX_CARD_BYTES:
        fail("此卡密的文件总量超过 100 MiB 限制", 413)
    if inventory["count"] >= MAX_CARD_FILES:
        fail("此卡密的文件数量超过 100 个限制", 413)
    fid = str(uuid.uuid4())
    c.execute(
        "INSERT INTO job_files(id,card_id,product_id,job_id,field_key,kind,attempt,filename,content_type,size,content,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            fid,
            card_id,
            product_id,
            job_id,
            key,
            kind,
            attempt,
            _filename(upload.filename),
            _content_type(upload.content_type),
            len(content),
            content,
            time.time(),
        ),
    )
    return _descriptor(_file(c, fid))


@router.post("/api/files/upload")
async def upload_input(request: Request):
    rate_limit(request, "files-upload", 60, 60)
    form = await _multipart(
        request, ("token", "field_key", "file"), optional=("card_id",)
    )
    try:
        with db() as c:
            card = resolve_customer_card(c, form["token"], form.get("card_id") or None)
            _input_scope(c, card, form["field_key"])
        content = await _contents(form["file"])
        with db() as c:
            card = resolve_customer_card(c, form["token"], form.get("card_id") or None)
            _, row = _input_scope(c, card, form["field_key"])
            return _store(
                c,
                card["id"],
                card["product_id"],
                None,
                row["attempt"] if row else 1,
                form["field_key"],
                "input",
                form["file"],
                content,
            )
    finally:
        await form.close()


@router.post("/api/manage/files/upload")
async def upload_output(request: Request):
    rate_limit(request, "files-upload", 20, 60)
    s = session(request, ("admin", "staff"))
    form = await _multipart(request, ("job_id", "field_key", "file"))
    try:
        with db() as c:
            _output_scope(c, s, form["job_id"], form["field_key"])
        content = await _contents(form["file"])
        s = session(request, ("admin", "staff"))
        with db() as c:
            row = _output_scope(c, s, form["job_id"], form["field_key"])
            return _store(
                c,
                row["card_id"],
                row["product_id"],
                row["id"],
                row["attempt"],
                form["field_key"],
                "output",
                form["file"],
                content,
            )
    finally:
        await form.close()


def validate_input_files(c, card, p, params):
    existing = c.execute(
        "SELECT id FROM jobs WHERE card_id=?", (card["id"],)
    ).fetchone()
    for field in p["parameters"]:
        fid = params.get(field["key"], "")
        if field["type"] != "file" or not fid:
            continue
        item = _file(c, fid)
        if (
            item["card_id"] != card["id"]
            or item["product_id"] != p["id"]
            or item["field_key"] != field["key"]
            or item["kind"] != "input"
            or not item["available"]
            or (
                item["job_id"] is not None
                and (not existing or item["job_id"] != existing["id"])
            )
        ):
            fail("输入文件不属于此卡密或字段", 403)


def validate_output_files(c, row, p, output):
    for field in p["outputs"]:
        fid = output.get(field["key"], "")
        if field["type"] != "file" or not fid:
            continue
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


def bind_inputs(c, row, params):
    selected = {
        params[field["key"]]
        for field in _job_product(c, row)["parameters"]
        if field["type"] == "file" and params.get(field["key"])
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
        output[field["key"]]
        for field in _job_product(c, row)["outputs"]
        if field["type"] == "file" and output.get(field["key"])
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
    ids = {
        output[field["key"]]
        for field in p["outputs"]
        if field["type"] == "file" and output.get(field["key"])
    }
    result = []
    for fid in ids:
        item = _file(c, fid)
        if not item["bound"]:
            fail("交付文件尚未完成", 409)
        c.execute("UPDATE job_files SET released=1 WHERE id=?", (fid,))
        result.append(_descriptor(item))
    return sorted(result, key=lambda item: (item["field_key"], item["id"]))


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
        item = _file(c, file_id, content=True)
        if item["job_id"] is None:
            fail("文件尚未提交到任务", 404)
        row = _row(c, item["job_id"])
        _manage_scope(c, s, row, "queue.view")
        if item["product_id"] != row["product_id"] or item["card_id"] != row["card_id"]:
            fail("文件不属于此任务", 403)
        if row["state"] == "destroyed" or item["content"] is None:
            fail("文件已销毁或领取", 410)
        return _download(item)


@router.post("/api/files/download")
def customer_download(body: FileDownload, request: Request):
    rate_limit(request, "files-download", 60, 60)
    with db() as c:
        card = resolve_customer_card(c, body.token, body.card_id)
        item = _file(c, body.file_id, content=True)
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
        if item["content"] is None or item["consumed"]:
            fail("文件已领取或销毁", 410)
        p = _job_product(c, row)
        _field(p, item["field_key"], "output")
        if item["attempt"] != row["attempt"]:
            fail("文件对应的尝试已失效", 409)
        response = _download(item)
        if p["view_policy"] == "once":
            c.execute(
                "UPDATE job_files SET content=NULL,consumed=1 WHERE id=? AND consumed=0",
                (item["id"],),
            )
        return response

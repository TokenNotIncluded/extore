"""Scoped v2 private-worker transport. Private means self-hosted, not private IP."""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import socket
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Request
from starlette.datastructures import Headers, UploadFile

from .config import ORIGIN
from .db import db
from .models import JobUpdate
from .security import fail, rate_limit

router = APIRouter()
VERSION = 2
SIGNATURE_TTL = 300
NONCE_TTL = 900
MAX_RESULT_BYTES = 256000
EMPTY_BODY_TIMEOUT = 5
WORKER_TO_EXTORE = "worker-to-extore"
EXTORE_TO_WORKER = "extore-to-worker"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_SCOPE_FIELDS = (
    "shop_id",
    "product_id",
    "job_id",
    "attempt",
    "node_id",
    "flow_epoch",
    "action_id",
)
_HEADER_NAMES = {key: "x-extore-" + key.replace("_", "-") for key in _SCOPE_FIELDS}
_SIGNED_HEADERS = set(_HEADER_NAMES.values()) | {
    "x-extore-version",
    "x-extore-audience",
    "x-extore-direction",
    "x-extore-timestamp",
    "x-extore-nonce",
    "x-extore-body-sha256",
    "x-extore-signature",
}
_FIELD = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_UPLOAD_PATH = re.compile(
    r"/api/callbacks/v2/[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}/"
    r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}/files/[a-z][a-z0-9_]{0,39}/"
    r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}/upload\Z"
)


def is_upload_path(path):
    return _UPLOAD_PATH.fullmatch(path) is not None


def init_schema(c):
    c.execute(
        "CREATE TABLE IF NOT EXISTS private_worker_nonces ("
        "digest TEXT PRIMARY KEY,product_id TEXT NOT NULL REFERENCES products(id) "
        "ON DELETE CASCADE,created REAL NOT NULL)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS private_worker_nonce_expiry "
        "ON private_worker_nonces(created)"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS private_worker_receipts ("
        "scope_digest TEXT NOT NULL,result_id TEXT NOT NULL,"
        "job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,"
        "body_digest TEXT NOT NULL,response TEXT NOT NULL,created REAL NOT NULL,"
        "PRIMARY KEY(scope_digest,result_id))"
    )


@dataclass(frozen=True)
class WorkerScope:
    shop_id: str
    product_id: str
    job_id: str
    attempt: int
    node_id: str
    flow_epoch: int
    action_id: str

    def __post_init__(self):
        for key in ("shop_id", "product_id", "job_id", "node_id", "action_id"):
            value = getattr(self, key)
            if not isinstance(value, str) or not _ID.fullmatch(value):
                raise ValueError("Invalid private-worker scope")
        for key in ("attempt", "flow_epoch"):
            value = getattr(self, key)
            if type(value) is not int or not 1 <= value <= 2**53 - 1:
                raise ValueError("Invalid private-worker scope")

    @classmethod
    def from_context(cls, context):
        return cls(**{key: context[key] for key in _SCOPE_FIELDS})

    def as_dict(self):
        return {key: getattr(self, key) for key in _SCOPE_FIELDS}

    def digest(self):
        return hashlib.sha256(_json(self.as_dict())).hexdigest()


def _json(value):
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("ascii")


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Non-finite JSON value")

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        fail("私有处理器 JSON 格式无效", 422)
    if not isinstance(value, dict):
        fail("私有处理器 JSON 必须是对象", 422)
    return value


def signature_v2(
    secret, scope, *, direction, audience, method, path, timestamp, nonce, body_digest
):
    """Canonical JSON array is length-safe; direction derives a separate HMAC key."""
    if direction not in (WORKER_TO_EXTORE, EXTORE_TO_WORKER):
        raise ValueError("Invalid signature direction")
    if not isinstance(secret, str) or not secret:
        raise ValueError("Missing private-worker secret")
    if (
        not isinstance(audience, str)
        or not audience
        or len(audience) > 2000
        or any(ord(ch) < 33 or ord(ch) > 126 for ch in audience)
    ):
        raise ValueError("Invalid signature audience")
    if (
        method not in ("GET", "POST")
        or not isinstance(path, str)
        or not path.startswith("/")
        or len(path) > 4096
        or any(ord(ch) < 33 or ord(ch) > 126 for ch in path)
    ):
        raise ValueError("Invalid signature target")
    if not isinstance(timestamp, str) or not re.fullmatch(r"[0-9]{1,12}", timestamp):
        raise ValueError("Invalid signature timestamp")
    if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", nonce):
        raise ValueError("Invalid signature nonce")
    if not isinstance(body_digest, str) or not _SHA.fullmatch(body_digest):
        raise ValueError("Invalid body digest")
    key = hmac.new(
        secret.encode(),
        b"extore-private-worker-key-v2\0" + direction.encode(),
        hashlib.sha256,
    ).digest()
    fields = [
        "extore-private-worker-signature-v2",
        direction,
        audience,
        method,
        path,
        timestamp,
        nonce,
        body_digest,
        *[getattr(scope, key) for key in _SCOPE_FIELDS],
    ]
    return hmac.new(key, _json(fields), hashlib.sha256).hexdigest()


def signed_headers(
    secret,
    scope,
    *,
    direction,
    audience,
    method,
    path,
    body,
    timestamp=None,
    nonce=None,
):
    timestamp = timestamp or str(int(time.time()))
    nonce = nonce or secrets.token_urlsafe(24)
    body_digest = hashlib.sha256(body).hexdigest()
    return {
        "X-Extore-Version": "2",
        "X-Extore-Direction": direction,
        "X-Extore-Audience": audience,
        "X-Extore-Timestamp": timestamp,
        "X-Extore-Nonce": nonce,
        "X-Extore-Body-SHA256": body_digest,
        "X-Extore-Signature": signature_v2(
            secret,
            scope,
            direction=direction,
            audience=audience,
            method=method,
            path=path,
            timestamp=timestamp,
            nonce=nonce,
            body_digest=body_digest,
        ),
        **{_HEADER_NAMES[key]: str(getattr(scope, key)) for key in _SCOPE_FIELDS},
    }


def _request_scope(request, product_id, job_id):
    seen = set()
    for raw_key, _ in request.scope.get("headers", ()):
        key = raw_key.decode("latin-1").lower()
        if key in _SIGNED_HEADERS:
            if key in seen:
                fail("签名字段不能重复", 401)
            seen.add(key)
    if seen != _SIGNED_HEADERS:
        fail("私有处理器签名字段不完整", 401)
    if request.scope.get("raw_path", b"") != request.url.path.encode(
        "ascii", errors="replace"
    ):
        fail("私有处理器路径必须使用原始规范形式", 401)
    if request.url.query or request.headers.get("authorization") is not None:
        fail("私有处理器不能混用浏览器或管理授权", 401)
    if (
        request.headers.get("x-extore-version") != "2"
        or request.headers.get("x-extore-audience") != ORIGIN
        or request.headers.get("x-extore-direction") != WORKER_TO_EXTORE
    ):
        fail("私有处理器签名范围无效", 401)
    try:
        values = {key: request.headers[_HEADER_NAMES[key]] for key in _SCOPE_FIELDS}
        for key in ("attempt", "flow_epoch"):
            if not re.fullmatch(r"[1-9][0-9]{0,15}", values[key]):
                raise ValueError("Invalid integer scope")
            values[key] = int(values[key])
        scope = WorkerScope(**values)
    except (KeyError, ValueError):
        fail("私有处理器签名范围无效", 401)
    if scope.product_id != product_id or scope.job_id != job_id:
        fail("私有处理器签名目标无效", 401)
    return scope


def _authority(c, scope):
    from . import task_flow
    from .service import job

    row = job(c, scope.job_id)
    authority = task_flow.frozen_authority(
        c, row, scope.flow_epoch, attempt=scope.attempt
    )
    if WorkerScope.from_context(authority) != scope or authority["mode"] != "webhook":
        fail("私有处理器执行范围已失效", 409)
    return row, authority


def _verify(c, request, scope, body_digest, *, consume=True):
    row, authority = _authority(c, scope)
    timestamp = request.headers.get("x-extore-timestamp", "")
    nonce = request.headers.get("x-extore-nonce", "")
    claimed = request.headers.get("x-extore-body-sha256", "")
    try:
        expected = signature_v2(
            authority["webhook_secret"],
            scope,
            direction=WORKER_TO_EXTORE,
            audience=ORIGIN,
            method=request.method,
            path=request.url.path,
            timestamp=timestamp,
            nonce=nonce,
            body_digest=body_digest,
        )
        valid = abs(time.time() - int(timestamp)) <= SIGNATURE_TTL
    except (ValueError, KeyError):
        fail("私有处理器签名无效或过期", 401)
    supplied = request.headers.get("x-extore-signature", "")
    if (
        not valid
        or not _SHA.fullmatch(claimed)
        or not _SHA.fullmatch(supplied)
        or not hmac.compare_digest(claimed, body_digest)
        or not hmac.compare_digest(expected, supplied)
    ):
        fail("私有处理器签名无效或过期", 401)
    if consume:
        c.execute(
            "DELETE FROM private_worker_nonces WHERE created<?",
            (time.time() - NONCE_TTL,),
        )
        nonce_digest = hashlib.sha256(
            _json([scope.product_id, WORKER_TO_EXTORE, nonce])
        ).hexdigest()
        try:
            c.execute(
                "INSERT INTO private_worker_nonces VALUES (?,?,?)",
                (nonce_digest, scope.product_id, time.time()),
            )
        except sqlite3.IntegrityError:
            fail("私有处理器请求已使用", 409)
    return row, authority


def _receipt_scope(scope, kind):
    return hashlib.sha256(_json([scope.as_dict(), kind])).hexdigest()


def _receipt(c, scope, result_id, body_digest, *, kind="result"):
    receipt = c.execute(
        "SELECT body_digest,response FROM private_worker_receipts WHERE scope_digest=? AND result_id=?",
        (_receipt_scope(scope, kind), result_id),
    ).fetchone()
    if receipt:
        if not hmac.compare_digest(receipt["body_digest"], body_digest):
            fail("同一结果编号不能更换内容", 409)
        return json.loads(receipt["response"])


def _save_receipt(c, scope, result_id, body_digest, response, *, kind="result"):
    c.execute(
        "INSERT INTO private_worker_receipts VALUES (?,?,?,?,?,?)",
        (
            _receipt_scope(scope, kind),
            result_id,
            scope.job_id,
            body_digest,
            _json(response).decode(),
            time.time(),
        ),
    )


class _DeadlineReached(Exception):
    pass


@contextmanager
def _transaction():
    expired = False
    with db() as c:
        try:
            yield c
        except _DeadlineReached:
            # Deadline transition and terminal adapter must commit before the
            # caller receives a conflict. Raising inside db() would undo it.
            expired = True
    if expired:
        fail("此处理步骤已超时，请刷新任务状态", 409)


def _active(c, row, scope, *, output=False):
    from . import task_flow

    # execution() intentionally returns None for expired or sensitive inputs.
    # Check frozen metadata first, without materializing those input values.
    _, authority = _authority(c, scope)
    deadline = authority.get("deadline")
    if deadline is not None and time.time() >= deadline:
        # The core processes the deadline and finalizer in the same transaction.
        from .service import finalize_task_flow

        effect = task_flow.process_update(
            c,
            row,
            JobUpdate(attempt=scope.attempt, state="processing"),
            scope.flow_epoch,
        )
        finalize_task_flow(c, effect)
        raise _DeadlineReached
    if output:
        from .flow_adapter import output_file_scope

        context = output_file_scope(
            c, row, flow_epoch=scope.flow_epoch, node_id=scope.node_id
        )
    else:
        context = task_flow.execution(c, row)
    if (
        context is None
        or WorkerScope.from_context(context) != scope
        or context["mode"] != "webhook"
    ):
        fail("此处理步骤或尝试已失效", 409)
    return context


@router.post("/api/callbacks/v2/{product_id}/{job_id}/result")
async def result(product_id: str, job_id: str, request: Request):
    rate_limit(request, "private-worker-result", 120, 60)
    raw = await request.body()
    if len(raw) > MAX_RESULT_BYTES:
        fail("私有处理器结果过大", 413)
    scope = _request_scope(request, product_id, job_id)
    body = strict_json(raw)
    if (
        set(body) != {"version", "result_id", "update"}
        or type(body["version"]) is not int
        or body["version"] != VERSION
        or not isinstance(body["result_id"], str)
        or not _ID.fullmatch(body["result_id"])
    ):
        fail("私有处理器结果字段无效", 422)
    update = body["update"]
    if not isinstance(update, dict) or set(update) - set(JobUpdate.model_fields):
        fail("私有处理器更新字段无效", 422)
    try:
        update = JobUpdate.model_validate(update, strict=True)
    except ValueError:
        fail("私有处理器更新字段无效", 422)
    if update.attempt != scope.attempt:
        fail("处理尝试与签名不一致", 409)
    if (update.flow_epoch is not None and update.flow_epoch != scope.flow_epoch) or (
        update.action_id is not None and update.action_id != scope.action_id
    ):
        fail("处理步骤与签名不一致", 409)
    body_digest = hashlib.sha256(raw).hexdigest()
    with _transaction() as c:
        row, _ = _verify(c, request, scope, body_digest)
        receipt = _receipt(c, scope, body["result_id"], body_digest)
        if receipt is not None:
            return receipt
        from . import task_flow
        from .service import finalize_task_flow

        phase = c.execute(
            "SELECT phase FROM task_flow_runs WHERE job_id=?", (scope.job_id,)
        ).fetchone()
        if phase is not None and phase[0] == "queued":
            # A dispatch is not a claim: a worker may still be queued when its
            # OTP expires, before the cleanup sweep runs. Only an already
            # started process can finish without reopening its erased inputs.
            _active(c, row, scope)
        effect = task_flow.process_update(c, row, update, scope.flow_epoch)
        finalize_task_flow(c, effect)
        if effect.get("expired"):
            raise _DeadlineReached
        response = {
            "ok": True,
            "version": VERSION,
            "result_id": body["result_id"],
            "body_digest": body_digest,
            "scope": scope.as_dict(),
        }
        _save_receipt(c, scope, body["result_id"], body_digest, response)
        return response


def _file_scope(c, row, context, field_key, kind, file_id=None):
    from .flow_adapter import private_worker_file_scope

    if not _FIELD.fullmatch(field_key):
        fail("文件字段无效", 422)
    return private_worker_file_scope(c, row, context, field_key, kind, file_id=file_id)


async def _receive_file(request, *, reservation=None, spool=None):
    from . import files

    hasher = hashlib.sha256()
    size = 0
    async with asyncio.timeout(files.UPLOAD_TIMEOUT_SECONDS):
        async for chunk in request.stream():
            for start in range(0, len(chunk), files.CHUNK_BYTES):
                piece = chunk[start : start + files.CHUNK_BYTES]
                size += len(piece)
                if size > files.MAX_FILE_BYTES:
                    fail(files.file_limit_message(), 413)
                if reservation is not None:
                    reservation.grow(len(piece))
                hasher.update(piece)
                if spool is not None:
                    spool.write(piece)
    return hasher.hexdigest(), size


def _check_received(body_digest, size, claimed_digest, length):
    if not hmac.compare_digest(body_digest, claimed_digest) or (
        length is not None and size != int(length)
    ):
        fail("文件内容与签名或长度不一致", 401)


@router.post(
    "/api/callbacks/v2/{product_id}/{job_id}/files/{field_key}/{result_id}/upload"
)
async def upload(
    product_id: str, job_id: str, field_key: str, result_id: str, request: Request
):
    from . import files
    from .storage import _directory, receiving_upload, translate_storage_error

    rate_limit(request, "private-worker-upload", 20, 60)
    scope = _request_scope(request, product_id, job_id)
    if not _ID.fullmatch(result_id) or not _FIELD.fullmatch(field_key):
        fail("文件字段或结果编号无效", 422)
    claimed_digest = request.headers.get("x-extore-body-sha256", "")
    if not _SHA.fullmatch(claimed_digest):
        fail("文件内容签名无效", 401)
    length = request.headers.get("content-length")
    if length is not None:
        if not re.fullmatch(r"[0-9]{1,12}", length):
            fail("文件长度无效", 400)
        if int(length) > files.MAX_FILE_BYTES:
            fail(files.file_limit_message(), 413)
    with _transaction() as c:
        row, _ = _verify(c, request, scope, claimed_digest, consume=False)
        previous_receipt = _receipt(
            c, scope, result_id, claimed_digest, kind="file:" + field_key
        )
        if previous_receipt is None:
            context = _active(c, row, scope, output=True)
            _file_scope(c, row, context, field_key, "output")
    # A database write transaction never spans awaiting an untrusted stream.
    # Every byte is reserved before it is written to a bounded spool.
    try:
        if previous_receipt is not None:
            # Confirm the actual bytes even on an acknowledged retry, without
            # allocating a second spool or requiring free quota for old data.
            body_digest, size = await _receive_file(request)
            _check_received(body_digest, size, claimed_digest, length)
            with _transaction() as c:
                _verify(c, request, scope, body_digest)
                receipt = _receipt(
                    c, scope, result_id, body_digest, kind="file:" + field_key
                )
                if receipt is None:
                    fail("文件回执已失效", 409)
                return receipt
        with receiving_upload() as reservation:
            with _transaction() as c:
                row, _ = _verify(c, request, scope, claimed_digest, consume=False)
                context = _active(c, row, scope, output=True)
                _file_scope(c, row, context, field_key, "output")
                reservation.bind_product(c, scope.product_id)
            with tempfile.SpooledTemporaryFile(
                max_size=files.CHUNK_BYTES, mode="w+b", dir=_directory()
            ) as spool:
                body_digest, size = await _receive_file(
                    request, reservation=reservation, spool=spool
                )
                _check_received(body_digest, size, claimed_digest, length)
                with _transaction() as c:
                    row, _ = _verify(c, request, scope, body_digest)
                    receipt = _receipt(
                        c, scope, result_id, body_digest, kind="file:" + field_key
                    )
                    if receipt is not None:
                        reservation.finish(c)
                        return receipt
                    context = _active(c, row, scope, output=True)
                    field = _file_scope(c, row, context, field_key, "output")
                    upload_file = UploadFile(
                        spool,
                        size=size,
                        filename=request.headers.get("x-extore-filename", "attachment"),
                        headers=Headers(
                            {
                                "content-type": request.headers.get(
                                    "content-type", "application/octet-stream"
                                )
                            }
                        ),
                    )
                    descriptor = files._store(
                        c,
                        row["card_id"],
                        scope.product_id,
                        scope.job_id,
                        scope.attempt,
                        field_key,
                        "output",
                        upload_file,
                        reservation,
                        field=field,
                    )
                    from .flow_adapter import bind_private_worker_file

                    bind_private_worker_file(
                        c, row, context, field_key, descriptor["id"]
                    )
                    response = {
                        "ok": True,
                        "version": VERSION,
                        "result_id": result_id,
                        "body_digest": body_digest,
                        "scope": scope.as_dict(),
                        "file": descriptor,
                    }
                    _save_receipt(
                        c,
                        scope,
                        result_id,
                        body_digest,
                        response,
                        kind="file:" + field_key,
                    )
                    return response
    except TimeoutError:
        fail("上传超时，请重新上传", 408)
    except (OSError, sqlite3.Error) as exc:
        translate_storage_error(exc)


@router.get(
    "/api/callbacks/v2/{product_id}/{job_id}/files/{field_key}/{file_id}/download"
)
async def download(
    product_id: str, job_id: str, field_key: str, file_id: str, request: Request
):
    from . import files

    rate_limit(request, "private-worker-download", 60, 60)
    scope = _request_scope(request, product_id, job_id)
    if not _ID.fullmatch(file_id) or not _FIELD.fullmatch(field_key):
        fail("文件范围无效", 422)
    if (
        request.headers.get("content-length") not in (None, "0")
        or request.headers.get("transfer-encoding") is not None
    ):
        fail("文件下载 GET 不能包含请求体", 400)
    with _transaction() as c:
        _verify(c, request, scope, hashlib.sha256(b"").hexdigest(), consume=False)
    try:
        async with asyncio.timeout(EMPTY_BODY_TIMEOUT):
            async for chunk in request.stream():
                if chunk:
                    fail("文件下载 GET 不能包含请求体", 400)
    except TimeoutError:
        fail("文件下载请求体读取超时", 408)
    with _transaction() as c:
        row, _ = _verify(c, request, scope, hashlib.sha256(b"").hexdigest())
        context = _active(c, row, scope)
        _file_scope(c, row, context, field_key, "input", file_id=file_id)
        # Scope and source references are checked before allocating a BLOB.
        item = files._file(c, file_id)
        if (
            item["card_id"] != row["card_id"]
            or item["product_id"] != scope.product_id
            or item["job_id"] != scope.job_id
            or item["attempt"] != scope.attempt
            or not item["available"]
        ):
            fail("文件不属于当前处理步骤或已销毁", 403)
        return files._download(files._file(c, file_id, content=True))


async def deliver_v2(context, *, recheck=None):
    """A closed-source worker still needs HTTPS and publicly routable pinned IPs."""
    scope = WorkerScope.from_context(context)
    target = urlsplit(context["webhook_url"])
    if (
        target.scheme != "https"
        or not target.hostname
        or target.username is not None
        or target.password is not None
        or "#" in context["webhook_url"]
        or "?" in context["webhook_url"]
        or target.port is not None
        and not 1 <= target.port <= 65535
    ):
        raise ValueError("私有处理器地址必须是 HTTPS 公网地址且不能含查询参数")
    # Sign the exact normalized bytes that HTTPX sends, including percent
    # encoding and dot-segment removal, rather than urlsplit's decoded path.
    normalized = httpx.URL(target.geturl())
    hostname = normalized.raw_host.decode("ascii")
    async with asyncio.timeout(15):
        answers = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            normalized.port or 443,
            type=socket.SOCK_STREAM,
        )
    addresses = {answer[4][0] for answer in answers}
    if not addresses or any(
        not ipaddress.ip_address(address).is_global for address in addresses
    ):
        raise ValueError("私有处理器地址必须解析到公网 IP")
    if time.time() >= context["deadline"]:
        raise ValueError("私有处理器步骤已过期")
    if recheck is not None:
        # The scheduler supplies a fresh transactional execution check after
        # DNS resolution, including ephemeral-input TTL and secret revocation.
        recheck()
    path = normalized.raw_path.decode("ascii")
    authority = normalized.netloc.decode("ascii")
    audience = "https://" + authority
    payload = {
        "version": VERSION,
        "type": "flow.process.requested",
        "scope": scope.as_dict(),
        "params": context["params"],
        "parameters": context["parameters"],
        "outputs": context["outputs"],
        "deadline": context["deadline"],
        "callback_url": ORIGIN
        + f"/api/callbacks/v2/{scope.product_id}/{scope.job_id}/result",
    }
    if context.get("instructions") is not None:
        payload["instructions"] = context["instructions"]
    raw = _json(payload)
    if len(raw) > MAX_RESULT_BYTES:
        raise ValueError("私有处理器任务输入过大")
    headers = signed_headers(
        context["webhook_secret"],
        scope,
        direction=EXTORE_TO_WORKER,
        audience=audience,
        method="POST",
        path=path,
        body=raw,
    )
    headers.update({"Host": authority, "Content-Type": "application/json"})
    address = sorted(addresses)[0]
    pinned_url = str(normalized.copy_with(host=address))
    async with httpx.AsyncClient(
        timeout=15, follow_redirects=False, trust_env=False
    ) as client:
        async with client.stream(
            "POST",
            pinned_url,
            content=raw,
            headers=headers,
            extensions={"sni_hostname": hostname},
        ) as response:
            if not 200 <= response.status_code < 300:
                raise ValueError(f"HTTP {response.status_code}")

import asyncio
import hashlib
import json
import sys
import time
from types import ModuleType

import pytest
from test_redemption import redeem

from extore import private_worker as pw
from extore.db import db
from extore.models import JobUpdate
from extore.security import fail

SECRET = "synthetic-private-worker-secret-32-chars"


@pytest.fixture
def private_job(owner, setup_product, monkeypatch):
    import extore
    from extore import service

    pid, code = setup_product(
        mode="webhook",
        webhook_url="https://worker.example/tasks",
        webhook_secret=SECRET,
    )
    _, task = redeem(owner, code)
    with db() as c:
        pw.init_schema(c)
        shop_id = c.execute(
            "SELECT shop_id FROM products WHERE id=?", (pid,)
        ).fetchone()[0]
        card_id = c.execute(
            "SELECT card_id FROM jobs WHERE id=?", (task["id"],)
        ).fetchone()[0]
    scope = pw.WorkerScope(
        shop_id, pid, task["id"], 1, "produce", 1, "action-synthetic"
    )
    context = {
        **scope.as_dict(),
        "mode": "webhook",
        "webhook_secret": SECRET,
        "deadline": time.time() + 300,
        "card_id": card_id,
        "params": {},
        "parameters": [],
        "outputs": [],
        "webhook_url": "https://worker.example/tasks",
    }
    counts = {"updates": 0, "finalizers": 0}
    core = ModuleType("extore.task_flow")

    def authority(c, row, epoch, attempt=None):
        assert row["product_id"] == pid
        return context

    def execution(c, row):
        if time.time() >= context["deadline"]:
            return None
        if row["state"] not in ("queued", "processing"):
            fail("Current step changed", 409)
        return context

    def update(c, row, value, epoch):
        if row["state"] not in ("queued", "processing"):
            fail("Current step changed", 409)
        counts["updates"] += 1
        assert isinstance(value, JobUpdate)
        c.execute(
            "UPDATE jobs SET state=?,message=? WHERE id=?",
            (value.state, value.message, row["id"]),
        )
        return {"row": service.job(c, row["id"]), "flow": {}}

    def finalize(c, effect):
        counts["finalizers"] += 1

    core.frozen_authority = authority
    core.execution = execution
    core.process_update = update
    monkeypatch.setitem(sys.modules, "extore.task_flow", core)
    monkeypatch.setattr(extore, "task_flow", core, raising=False)
    monkeypatch.setattr(service, "finalize_task_flow", finalize, raising=False)
    return scope, context, counts


def submit(
    client,
    scope,
    payload=None,
    *,
    raw=None,
    nonce=None,
    headers=None,
    path=None,
    secret=SECRET,
):
    path = path or f"/api/callbacks/v2/{scope.product_id}/{scope.job_id}/result"
    raw = (
        raw if raw is not None else json.dumps(payload, separators=(",", ":")).encode()
    )
    signed = pw.signed_headers(
        secret,
        scope,
        direction=pw.WORKER_TO_EXTORE,
        audience=pw.ORIGIN,
        method="POST",
        path=path,
        body=raw,
        nonce=nonce,
    )
    signed.update(headers or {})
    return client.post(path, content=raw, headers=signed)


def result_payload(result_id="completion-one", **changes):
    return {
        "version": 2,
        "result_id": result_id,
        "update": {
            "attempt": 1,
            "state": "succeeded",
            "output": {"content": "Synthetic"},
            **changes,
        },
    }


def test_result_receipt_survives_step_advance_without_second_update(
    client, private_job
):
    scope, _, counts = private_job
    payload = result_payload()
    first = submit(client, scope, payload)
    retry = submit(client, scope, payload)
    assert first.status_code == retry.status_code == 200
    assert first.json() == retry.json()
    assert counts == {"updates": 1, "finalizers": 1}
    assert submit(client, scope, result_payload(message="changed")).status_code == 409
    assert submit(client, scope, result_payload("new-result")).status_code == 409
    assert counts == {"updates": 1, "finalizers": 1}
    with db() as c:
        assert (
            c.execute("SELECT count(*) FROM private_worker_receipts").fetchone()[0] == 1
        )
        assert (
            c.execute("SELECT count(*) FROM private_worker_nonces").fetchone()[0] == 2
        )


def test_nonce_replay_is_not_result_idempotency(client, private_job):
    scope, _, counts = private_job
    nonce = "synthetic_nonce_used_1234"
    assert submit(client, scope, result_payload(), nonce=nonce).status_code == 200
    assert submit(client, scope, result_payload(), nonce=nonce).status_code == 409
    assert counts["updates"] == 1


def test_deadline_transition_commits_before_conflict(client, private_job, monkeypatch):
    from extore import service, task_flow

    scope, context, counts = private_job
    context["deadline"] = time.time() - 1

    def expired(c, row, value, epoch):
        counts["updates"] += 1
        c.execute(
            "UPDATE jobs SET state='failed',message='Synthetic deadline' WHERE id=?",
            (row["id"],),
        )
        return {"row": service.job(c, row["id"]), "flow": {}, "expired": True}

    monkeypatch.setattr(task_flow, "process_update", expired)
    assert submit(client, scope, result_payload()).status_code == 409
    assert counts == {"updates": 1, "finalizers": 1}
    with db() as c:
        assert (
            c.execute(
                "SELECT state,message FROM jobs WHERE id=?", (scope.job_id,)
            ).fetchone()["message"]
            == "Synthetic deadline"
        )
        assert (
            c.execute("SELECT count(*) FROM private_worker_receipts").fetchone()[0] == 0
        )


def test_terminal_adapter_failure_rolls_back_update_nonce_and_receipt(
    client, private_job, monkeypatch
):
    from extore import service

    scope, _, _ = private_job
    monkeypatch.setattr(
        service,
        "finalize_task_flow",
        lambda c, effect: fail("Synthetic adapter rejection", 422),
    )
    assert submit(client, scope, result_payload()).status_code == 422
    with db() as c:
        assert (
            c.execute("SELECT state FROM jobs WHERE id=?", (scope.job_id,)).fetchone()[
                0
            ]
            == "queued"
        )
        assert (
            c.execute("SELECT count(*) FROM private_worker_receipts").fetchone()[0] == 0
        )
        assert (
            c.execute("SELECT count(*) FROM private_worker_nonces").fetchone()[0] == 0
        )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"version":2,"version":2,"result_id":"r","update":{"attempt":1,"state":"succeeded"}}',
        b'{"version":2,"result_id":"r","update":{"attempt":1,"state":"succeeded","state":"failed"}}',
        b'{"version":2,"result_id":"r","update":{"attempt":1,"state":"processing","progress":NaN}}',
        b'{"version":2,"result_id":"r","update":{"attempt":1,"state":"succeeded","evil":1}}',
        b'{"version":2,"result_id":"r","evil":1,"update":{"attempt":1,"state":"succeeded"}}',
        b'{"version":2,"result_id":"r","update":{"attempt":"1","state":"succeeded"}}',
        b'{"version":true,"result_id":"r","update":{"attempt":1,"state":"succeeded"}}',
    ],
)
def test_strict_json_rejects_duplicate_unknown_and_coerced_fields(
    client, private_job, raw
):
    scope, _, counts = private_job
    assert submit(client, scope, raw=raw).status_code == 422
    assert counts["updates"] == 0
    with db() as c:
        assert (
            c.execute("SELECT count(*) FROM private_worker_receipts").fetchone()[0] == 0
        )


@pytest.mark.parametrize(
    "key,value",
    [
        ("X-Extore-Version", "1"),
        ("X-Extore-Direction", pw.EXTORE_TO_WORKER),
        ("X-Extore-Audience", "https://wrong.example"),
        ("X-Extore-Attempt", "01"),
        ("X-Extore-Flow-Epoch", "0"),
        ("X-Extore-Timestamp", "1"),
        ("X-Extore-Body-SHA256", "0" * 64),
        ("X-Extore-Signature", "F" * 64),
        ("X-Extore-Node-Id", "another-node"),
        ("X-Extore-Action-Id", "another-action"),
        ("Authorization", "Bearer synthetic-unrelated"),
    ],
)
def test_signature_tampering_never_updates(client, private_job, key, value):
    scope, _, counts = private_job
    assert submit(
        client, scope, result_payload(), headers={key: value}
    ).status_code in (401, 409)
    assert counts["updates"] == 0


def test_duplicate_security_headers_and_encoded_alias_rejected(client, private_job):
    scope, _, counts = private_job
    path = f"/api/callbacks/v2/{scope.product_id}/{scope.job_id}/result"
    raw = json.dumps(result_payload()).encode()
    headers = list(
        pw.signed_headers(
            SECRET,
            scope,
            direction=pw.WORKER_TO_EXTORE,
            audience=pw.ORIGIN,
            method="POST",
            path=path,
            body=raw,
        ).items()
    )
    headers.append(("X-Extore-Nonce", "synthetic_second_nonce"))
    assert client.post(path, content=raw, headers=headers).status_code == 401
    encoded = path.replace("/result", "/%72esult")
    assert submit(client, scope, result_payload(), path=encoded).status_code == 401
    assert (
        submit(client, scope, result_payload(), path=path + "?ignored=1").status_code
        == 401
    )
    assert counts["updates"] == 0


def test_domains_and_every_scope_field_change_signature():
    scope = pw.WorkerScope("shop", "product", "job", 1, "node", 3, "action")
    values = dict(
        direction=pw.WORKER_TO_EXTORE,
        audience="https://extore.example",
        method="POST",
        path="/callback",
        timestamp="1790000000",
        nonce="synthetic_nonce_123456",
        body_digest=hashlib.sha256(b"data").hexdigest(),
    )
    signature = pw.signature_v2(SECRET, scope, **values)
    for key, value in [
        ("direction", pw.EXTORE_TO_WORKER),
        ("method", "GET"),
        ("path", "/different"),
        ("audience", "https://other.example"),
    ]:
        assert pw.signature_v2(SECRET, scope, **{**values, key: value}) != signature
    for key in pw._SCOPE_FIELDS:
        other = pw.WorkerScope(
            **{
                **scope.as_dict(),
                key: 2 if key in ("attempt", "flow_epoch") else "other",
            }
        )
        assert pw.signature_v2(SECRET, other, **values) != signature


def test_private_dispatch_does_not_allow_internal_address(monkeypatch):
    context = {
        **pw.WorkerScope("shop", "product", "job", 1, "node", 1, "action").as_dict(),
        "webhook_url": "https://127.0.0.1/tasks",
    }
    with pytest.raises(ValueError, match="公网"):
        asyncio.run(pw.deliver_v2(context))


@pytest.mark.parametrize(
    "worker_url,expected_path",
    [
        ("https://worker.example/tasks", "/tasks"),
        ("https://WORKER.example:443/a/../tasks", "/tasks"),
        (
            "https://worker.example/中文 目录",
            "/%E4%B8%AD%E6%96%87%20%E7%9B%AE%E5%BD%95",
        ),
    ],
)
def test_dispatch_is_pinned_signed_and_never_exports_secret(
    monkeypatch, worker_url, expected_path
):
    context = {
        **pw.WorkerScope("shop", "product", "job", 1, "node", 1, "action").as_dict(),
        "webhook_url": worker_url,
        "webhook_secret": SECRET,
        "params": {"name": "Synthetic"},
        "parameters": [],
        "outputs": [],
        "deadline": time.time() + 30,
    }
    monkeypatch.setattr(
        pw.socket, "getaddrinfo", lambda *a, **kw: [(0, 0, 0, "", ("8.8.8.8", 443))]
    )

    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        status_code = 200

    class Client:
        def __init__(self, **kw):
            assert kw == {"timeout": 15, "follow_redirects": False, "trust_env": False}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        def stream(self, method, url, **kw):
            assert method == "POST" and url == "https://8.8.8.8" + expected_path
            assert kw["headers"]["Host"] == "worker.example"
            assert kw["extensions"] == {"sni_hostname": "worker.example"}
            assert SECRET.encode() not in kw["content"]
            assert kw["headers"]["X-Extore-Direction"] == pw.EXTORE_TO_WORKER
            assert kw["headers"]["X-Extore-Audience"] == "https://worker.example"
            assert kw["headers"]["X-Extore-Signature"] == pw.signature_v2(
                SECRET,
                pw.WorkerScope.from_context(context),
                direction=pw.EXTORE_TO_WORKER,
                audience="https://worker.example",
                method="POST",
                path=expected_path,
                timestamp=kw["headers"]["X-Extore-Timestamp"],
                nonce=kw["headers"]["X-Extore-Nonce"],
                body_digest=hashlib.sha256(kw["content"]).hexdigest(),
            )
            assert json.loads(kw["content"])["scope"]["action_id"] == "action"
            return Stream()

    monkeypatch.setattr(pw.httpx, "AsyncClient", Client)
    asyncio.run(pw.deliver_v2(context))


@pytest.fixture
def private_files(private_job, monkeypatch):
    import extore
    from extore import files

    scope, context, counts = private_job
    field = {"key": "document", "type": "file", "label": {"en": "Document"}}
    context["outputs"] = [field]
    adapter = ModuleType("extore.flow_adapter")

    def file_scope(c, row, execution, field_key, kind, file_id=None):
        if field_key != "document":
            fail("Undeclared field", 403)
        if kind == "input" and execution["params"].get(field_key) != file_id:
            fail("Input source not authorized", 403)
        return field

    def bind(c, row, execution, field_key, file_id):
        counts["files_bound"] = counts.get("files_bound", 0) + 1

    adapter.private_worker_file_scope = file_scope
    adapter.output_file_scope = lambda c, row, **kwargs: (
        context
        if row["state"] in ("queued", "processing")
        else fail("Current step changed", 409)
    )
    adapter.bind_private_worker_file = bind
    monkeypatch.setitem(sys.modules, "extore.flow_adapter", adapter)
    monkeypatch.setattr(extore, "flow_adapter", adapter, raising=False)
    # The separately owned rich-fields module extends _store with field=. This
    # protocol test uses a generic file and the existing real quota/BLOB store.
    store = files._store

    def field_store(*args, field):
        assert field["type"] == "file" and field["key"] == "document"
        return store(*args, field=field)

    monkeypatch.setattr(files, "_store", field_store)
    return private_job


def upload_file(
    client, scope, raw, *, result_id="file-one", field="document", headers=None
):
    path = f"/api/callbacks/v2/{scope.product_id}/{scope.job_id}/files/{field}/{result_id}/upload"
    signed = pw.signed_headers(
        SECRET,
        scope,
        direction=pw.WORKER_TO_EXTORE,
        audience=pw.ORIGIN,
        method="POST",
        path=path,
        body=raw,
    )
    signed.update(headers or {})
    return client.post(path, content=raw, headers=signed)


def test_scoped_upload_streams_beyond_json_limit_and_deduplicates(
    client, private_files
):
    scope, _, counts = private_files
    raw = b"synthetic-file-content\n" * 20000
    first = upload_file(client, scope, raw)
    retry = upload_file(client, scope, raw)
    assert first.status_code == retry.status_code == 200, first.text
    assert first.json() == retry.json()
    assert counts["files_bound"] == 1
    assert upload_file(client, scope, b"changed").status_code == 409
    with db() as c:
        stored = c.execute(
            "SELECT * FROM job_files WHERE id=?", (first.json()["file"]["id"],)
        ).fetchone()
        assert stored["content"] == raw
        assert stored["job_id"] == scope.job_id and stored["attempt"] == scope.attempt
        assert c.execute("SELECT count(*) FROM upload_reservations").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM job_files").fetchone()[0] == 1


def test_upload_rechecks_epoch_after_receiving_and_cleans_reservation(
    client, private_files, monkeypatch
):
    from extore.storage import UploadReservation

    scope, context, _ = private_files
    grow = UploadReservation.grow

    def change_epoch(self, size):
        grow(self, size)
        context["flow_epoch"] += 1

    monkeypatch.setattr(UploadReservation, "grow", change_epoch)
    assert upload_file(client, scope, b"synthetic").status_code == 409
    with db() as c:
        assert c.execute("SELECT count(*) FROM upload_reservations").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM job_files").fetchone()[0] == 0
        assert (
            c.execute("SELECT count(*) FROM private_worker_receipts").fetchone()[0] == 0
        )


def test_old_upload_receipt_ack_needs_no_new_storage_and_still_checks_bytes(
    client, private_files, monkeypatch
):
    from extore import files

    scope, _, _ = private_files
    raw = b"synthetic-upload"
    accepted = upload_file(client, scope, raw)
    assert accepted.status_code == 200
    with db() as c:
        c.execute("UPDATE jobs SET state='succeeded' WHERE id=?", (scope.job_id,))
    monkeypatch.setattr(files, "MAX_CARD_BYTES", 1)
    assert upload_file(client, scope, raw).json() == accepted.json()
    path = f"/api/callbacks/v2/{scope.product_id}/{scope.job_id}/files/document/file-one/upload"
    headers = pw.signed_headers(
        SECRET,
        scope,
        direction=pw.WORKER_TO_EXTORE,
        audience=pw.ORIGIN,
        method="POST",
        path=path,
        body=raw,
    )
    assert (
        client.post(path, content=b"different-bytes", headers=headers).status_code
        == 401
    )
    assert upload_file(client, scope, raw, result_id="new-upload").status_code == 409


def test_dispatch_recheck_after_dns_can_revoke_before_any_http(monkeypatch):
    scope = pw.WorkerScope("shop", "product", "job", 1, "node", 1, "action")
    context = {
        **scope.as_dict(),
        "webhook_url": "https://worker.example/tasks",
        "deadline": time.time() + 20,
    }
    monkeypatch.setattr(
        pw.socket, "getaddrinfo", lambda *a, **kw: [(0, 0, 0, "", ("8.8.8.8", 443))]
    )

    def revoked():
        raise ValueError("Synthetic revoked execution")

    monkeypatch.setattr(
        pw.httpx, "AsyncClient", lambda **kw: pytest.fail("Must not start HTTP")
    )
    with pytest.raises(ValueError, match="revoked"):
        asyncio.run(pw.deliver_v2(context, recheck=revoked))


def test_upload_undeclared_field_wrong_digest_and_quota_fail_closed(
    client, private_files, monkeypatch
):
    from extore import files

    scope, _, _ = private_files
    assert upload_file(client, scope, b"data", field="other").status_code == 403
    assert (
        upload_file(
            client, scope, b"data", headers={"X-Extore-Body-SHA256": "0" * 64}
        ).status_code
        == 401
    )
    monkeypatch.setattr(files, "MAX_CARD_BYTES", 3)
    assert upload_file(client, scope, b"data").status_code == 413
    with db() as c:
        assert c.execute("SELECT count(*) FROM upload_reservations").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM job_files").fetchone()[0] == 0


def test_download_requires_signed_empty_get_and_declared_input_before_blob(
    client, private_files, monkeypatch
):
    import uuid

    from extore import files

    scope, context, _ = private_files
    fid = str(uuid.uuid4())
    with db() as c:
        c.execute(
            "INSERT INTO job_files(id,card_id,product_id,job_id,field_key,kind,attempt,filename,content_type,size,content,created,bound) VALUES (?,?,?,?,?,'input',1,'synthetic.txt','text/plain',4,?,?,1)",
            (
                fid,
                context["card_id"],
                scope.product_id,
                scope.job_id,
                "source",
                b"data",
                time.time(),
            ),
        )
    path = f"/api/callbacks/v2/{scope.product_id}/{scope.job_id}/files/document/{fid}/download"

    def get():
        return pw.signed_headers(
            SECRET,
            scope,
            direction=pw.WORKER_TO_EXTORE,
            audience=pw.ORIGIN,
            method="GET",
            path=path,
            body=b"",
        )

    calls = []
    original = files._file

    def file_lookup(c, file_id, *, content=False):
        calls.append(content)
        return original(c, file_id, content=content)

    monkeypatch.setattr(files, "_file", file_lookup)
    assert client.get(path, headers=get()).status_code == 403
    assert True not in calls
    context["params"] = {"document": fid}
    good = client.get(path, headers=get())
    assert good.status_code == 200 and good.content == b"data"
    assert good.headers["x-content-type-options"] == "nosniff"
    assert (
        client.request("GET", path, content=b"unexpected", headers=get()).status_code
        == 400
    )


def test_signed_download_has_a_server_read_deadline(private_files, monkeypatch):
    from fastapi import HTTPException, Request

    scope, _, _ = private_files
    path = f"/api/callbacks/v2/{scope.product_id}/{scope.job_id}/files/document/file-synthetic/download"
    signed = pw.signed_headers(
        SECRET,
        scope,
        direction=pw.WORKER_TO_EXTORE,
        audience=pw.ORIGIN,
        method="GET",
        path=path,
        body=b"",
    )

    async def no_body_arrives():
        await asyncio.Event().wait()

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "scheme": "http",
            "server": ("localhost", 8000),
            "headers": [
                (key.lower().encode(), value.encode()) for key, value in signed.items()
            ],
        },
        receive=no_body_arrives,
    )
    monkeypatch.setattr(pw, "EMPTY_BODY_TIMEOUT", 0.005)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            pw.download(
                scope.product_id, scope.job_id, "document", "file-synthetic", request
            )
        )
    assert exc.value.status_code == 408
    with db() as c:
        assert (
            c.execute("SELECT count(*) FROM private_worker_nonces").fetchone()[0] == 0
        )


@pytest.mark.parametrize("changes", [{"flow_epoch": 2}, {"action_id": "other-action"}])
def test_result_redundant_scope_must_match_signed_scope(client, private_job, changes):
    scope, _, counts = private_job
    assert submit(client, scope, result_payload(**changes)).status_code == 409
    assert counts == {"updates": 0, "finalizers": 0}
    matching = result_payload(flow_epoch=scope.flow_epoch, action_id=scope.action_id)
    assert submit(client, scope, matching).status_code == 200
    assert counts == {"updates": 1, "finalizers": 1}

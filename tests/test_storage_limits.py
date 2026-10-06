import asyncio
import errno
import inspect
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from extore import files, storage
from extore.config import DATA
from extore.db import db
from extore.security import fail


def product(owner):
    response = owner.post(
        "/api/admin/products",
        json={
            "name": "有界文件任务",
            "parameters": [
                {"key": "source", "label": {"zh-CN": "需求文件"}, "type": "file"}
            ],
            "outputs": [
                {"key": "result", "label": {"zh-CN": "交付文件"}, "type": "file"}
            ],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def token(owner, pid):
    response = owner.post("/api/admin/cards", json={"product_id": pid, "count": 1})
    assert response.status_code == 200, response.text
    response = owner.post("/api/exchange", json={"code": response.json()["codes"][0]})
    assert response.status_code == 200, response.text
    return response.json()["token"]


def upload(owner, receipt, payload):
    return owner.post(
        "/api/files/upload",
        data={"token": receipt, "field_key": "source"},
        files={"file": ("source.bin", payload, "application/octet-stream")},
    )


def counts():
    with db() as c:
        return (
            c.execute("SELECT COUNT(*) FROM job_files").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM upload_reservations").fetchone()[0],
            c.execute(
                "SELECT COALESCE(SUM(size),0) FROM job_files WHERE content IS NOT NULL"
            ).fetchone()[0],
        )


def record_spools(monkeypatch):
    retained = []
    original = files.tempfile.SpooledTemporaryFile

    def record(*args, **kwargs):
        spool = original(*args, **kwargs)
        retained.append(spool)
        return spool

    monkeypatch.setattr(files.tempfile, "SpooledTemporaryFile", record)
    return retained


def test_global_quota_includes_other_cards_drafts_and_output_bytes(owner, monkeypatch):
    monkeypatch.setattr(storage, "UPLOAD_TOTAL_BYTES", 10)
    pid = product(owner)
    receipt = token(owner, pid)
    source = upload(owner, receipt, b"123456")
    assert source.status_code == 200, source.text
    another = token(owner, pid)
    assert upload(owner, another, b"12345").status_code == 507
    assert counts() == (1, 0, 6)
    assert not list((DATA / "upload-tmp").glob("*.lock"))
    assert upload(owner, another, b"1234").status_code == 200
    assert counts() == (2, 0, 10)


def test_task_limit_includes_both_input_and_output(owner, monkeypatch):
    monkeypatch.setattr(files, "MAX_CARD_BYTES", 8)
    receipt = token(owner, product(owner))
    source = upload(owner, receipt, b"123456").json()["id"]
    response = owner.post(
        "/api/redeem", json={"token": receipt, "params": {"source": source}}
    )
    assert response.status_code == 200, response.text
    job = response.json()
    assert (
        owner.post(
            "/api/manage/batch",
            json={
                "product_id": job["product_id"],
                "ids": [job["id"]],
                "action": "claim",
            },
        ).status_code
        == 200
    )
    data = {"job_id": job["id"], "field_key": "result"}
    response = owner.post(
        "/api/manage/files/upload", data=data, files={"file": ("result", b"123")}
    )
    assert response.status_code == 413
    assert counts() == (1, 0, 6)
    response = owner.post(
        "/api/manage/files/upload", data=data, files={"file": ("result", b"12")}
    )
    assert response.status_code == 200, response.text
    assert counts() == (2, 0, 8)


def test_parallel_uploads_cannot_overshoot_global_quota(owner, monkeypatch):
    monkeypatch.setattr(storage, "UPLOAD_TOTAL_BYTES", 10)
    monkeypatch.setattr(storage, "UPLOAD_CONCURRENCY", 8)
    pid = product(owner)
    receipts = [token(owner, pid) for _ in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(
            pool.map(lambda receipt: upload(owner, receipt, b"123456"), receipts)
        )
    assert sum(response.status_code == 200 for response in responses) == 1
    assert all(response.status_code in (200, 507) for response in responses)
    assert counts() == (1, 0, 6)


def test_parallel_stream_reservations_include_uncommitted_uploads(monkeypatch):
    monkeypatch.setattr(storage, "UPLOAD_TOTAL_BYTES", 10)
    monkeypatch.setattr(storage, "UPLOAD_CONCURRENCY", 8)
    started = threading.Barrier(6)
    attempted = threading.Barrier(6)

    def receive():
        with storage.receiving_upload() as reservation:
            started.wait(timeout=10)
            try:
                reservation.grow(4)
                state = 200
            except HTTPException as exc:
                state = exc.status_code
            attempted.wait(timeout=10)
            with db() as c:
                assert storage.storage_usage(c)["uploading_bytes"] <= 10
            return state

    with ThreadPoolExecutor(max_workers=6) as pool:
        states = list(pool.map(lambda _: receive(), range(6)))
    assert states.count(200) == 2
    assert states.count(507) == 4
    assert counts() == (0, 0, 0)


def test_concurrency_limit_rejects_before_receiving_and_recovers(owner, monkeypatch):
    monkeypatch.setattr(storage, "UPLOAD_CONCURRENCY", 1)
    receipt = token(owner, product(owner))
    with storage.receiving_upload():
        response = upload(owner, receipt, b"payload")
        assert response.status_code == 429
        assert counts() == (0, 1, 0)
    assert upload(owner, receipt, b"payload").status_code == 200
    assert counts() == (1, 0, 7)


def test_disk_reserve_rejects_before_spooling(owner, monkeypatch):
    receipt = token(owner, product(owner))
    monkeypatch.setattr(
        storage, "disk_free_bytes", lambda: storage.UPLOAD_DISK_RESERVE_BYTES - 1
    )
    assert upload(owner, receipt, b"payload").status_code == 507
    assert counts() == (0, 0, 0)
    assert not list((DATA / "upload-tmp").iterdir())


def test_disk_failure_mid_stream_closes_spool_and_reservation(owner, monkeypatch):
    receipt = token(owner, product(owner))
    spools = []
    original = files.tempfile.SpooledTemporaryFile

    def record_spool(*args, **kwargs):
        spool = original(*args, **kwargs)
        spools.append(spool)
        return spool

    monkeypatch.setattr(files.tempfile, "SpooledTemporaryFile", record_spool)
    calls = 0

    def free_space():
        nonlocal calls
        calls += 1
        return 10**12 if calls <= 2 else storage.UPLOAD_DISK_RESERVE_BYTES - 1

    monkeypatch.setattr(storage, "disk_free_bytes", free_space)
    response = upload(owner, receipt, b"x" * (3 * files.CHUNK_BYTES))
    assert response.status_code == 507
    assert spools and all(spool.closed for spool in spools)
    assert counts() == (0, 0, 0)
    assert not list((DATA / "upload-tmp").iterdir())


def test_disk_failure_mid_blob_write_rolls_back_partial_content(owner, monkeypatch):
    receipt = token(owner, product(owner))
    original = storage.require_disk_space
    writes = 0

    def fail_second_write(additional, *, pending=0):
        nonlocal writes
        if inspect.currentframe().f_back.f_code.co_name == "_store":
            writes += 1
            if writes == 2:
                fail("injected disk exhaustion", 507)
        original(additional, pending=pending)

    monkeypatch.setattr(storage, "require_disk_space", fail_second_write)
    response = upload(owner, receipt, b"x" * (3 * files.CHUNK_BYTES))
    assert response.status_code == 507
    assert writes == 2
    assert counts() == (0, 0, 0)
    assert not list((DATA / "upload-tmp").iterdir())


def test_os_enospc_becomes_capacity_error_without_leaking_upload(owner, monkeypatch):
    receipt = token(owner, product(owner))

    def no_disk(*args, **kwargs):
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(files.tempfile, "SpooledTemporaryFile", no_disk)
    assert upload(owner, receipt, b"payload").status_code == 507
    assert counts() == (0, 0, 0)
    assert not list((DATA / "upload-tmp").iterdir())


def test_upload_timeout_closes_live_spool_and_releases_slot(owner, monkeypatch):
    receipt = token(owner, product(owner))
    spools = record_spools(monkeypatch)
    monkeypatch.setattr(files, "UPLOAD_TIMEOUT_SECONDS", 0.01)
    boundary = "slow-upload"
    opening = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="token"\r\n\r\n{receipt}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="field_key"\r\n\r\nsource\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a"\r\n\r\npayload'
    ).encode()
    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "http.request", "body": opening, "more_body": True}
        await asyncio.sleep(1)
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/files/upload",
            "headers": [
                (b"content-type", f"multipart/form-data; boundary={boundary}".encode())
            ],
            "client": ("127.0.0.1", 1234),
        },
        receive=receive,
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(files.upload_input(request))
    assert exc.value.status_code == 408
    assert spools and all(spool.closed for spool in spools)
    assert counts() == (0, 0, 0)
    assert not list((DATA / "upload-tmp").iterdir())


def test_global_quota_failure_after_disk_spooling_closes_every_handle(
    owner, monkeypatch
):
    receipt = token(owner, product(owner))
    spools = record_spools(monkeypatch)
    monkeypatch.setattr(storage, "UPLOAD_TOTAL_BYTES", 2 * files.CHUNK_BYTES + 1)
    response = upload(owner, receipt, b"x" * (4 * files.CHUNK_BYTES))
    assert response.status_code == 507
    assert spools and any(spool._rolled for spool in spools)
    assert all(spool.closed for spool in spools)
    assert counts() == (0, 0, 0)
    assert not list((DATA / "upload-tmp").iterdir())


def test_invalid_authorization_after_parsing_cannot_leave_spooled_data(
    owner, monkeypatch
):
    spools = record_spools(monkeypatch)
    response = upload(owner, "invalid-receipt", b"x" * (3 * files.CHUNK_BYTES))
    assert response.status_code == 404
    assert spools and all(spool.closed for spool in spools)
    assert counts() == (0, 0, 0)
    assert not list((DATA / "upload-tmp").iterdir())


def test_cancelled_request_closes_spooled_data_and_releases_quota(monkeypatch):
    spools = record_spools(monkeypatch)
    boundary = "cancel-upload"
    opening = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a"\r\n\r\n'
    ).encode() + b"x" * (2 * files.CHUNK_BYTES)
    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "http.request", "body": opening, "more_body": True}
        raise asyncio.CancelledError

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/files/upload",
            "headers": [
                (b"content-type", f"multipart/form-data; boundary={boundary}".encode())
            ],
            "client": ("127.0.0.1", 1234),
        },
        receive=receive,
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(files.upload_input(request))
    assert spools and any(spool._rolled for spool in spools)
    assert all(spool.closed for spool in spools)
    assert counts() == (0, 0, 0)
    assert not list((DATA / "upload-tmp").iterdir())


def test_maintenance_keeps_live_upload_but_reclaims_abandoned_lease():
    with storage.receiving_upload() as reservation:
        reservation.grow(8)
        with db() as c:
            result = storage.prune_uploads(c, now=time.time() + 30 * 86400)
            assert result["abandoned_uploads"] == 0
            assert storage.storage_usage(c)["uploading_bytes"] == 8
    identifier = str(uuid.uuid4())
    path = DATA / "upload-tmp" / (identifier + ".lock")
    path.write_bytes(b"")
    with db() as c:
        c.execute(
            "INSERT INTO upload_reservations(id,size,created) VALUES (?,?,?)",
            (identifier, 100, time.time() - 3600),
        )
    result = storage.maintenance_once()
    assert result["abandoned_uploads"] == 1
    assert not path.exists()
    assert counts() == (0, 0, 0)


def test_draft_expiry_keeps_bound_inputs_and_active_output_drafts(owner):
    pid = product(owner)
    receipt = token(owner, pid)
    source = upload(owner, receipt, b"bound input").json()["id"]
    job = owner.post(
        "/api/redeem", json={"token": receipt, "params": {"source": source}}
    ).json()
    assert (
        owner.post(
            "/api/manage/batch",
            json={"product_id": pid, "ids": [job["id"]], "action": "claim"},
        ).status_code
        == 200
    )
    output = owner.post(
        "/api/manage/files/upload",
        data={"job_id": job["id"], "field_key": "result"},
        files={"file": ("result", b"active output draft")},
    ).json()["id"]
    abandoned = upload(owner, token(owner, pid), b"abandoned input draft").json()["id"]
    with db() as c:
        c.execute("UPDATE job_files SET created=?", (time.time() - 2 * 86400,))
    assert storage.maintenance_once()["expired_drafts"] == 1
    with db() as c:
        assert {row[0] for row in c.execute("SELECT id FROM job_files")} == {
            source,
            output,
        }
        assert (
            c.execute("SELECT id FROM job_files WHERE id=?", (abandoned,)).fetchone()
            is None
        )
        c.execute("UPDATE jobs SET state='needs_input' WHERE id=?", (job["id"],))
    assert storage.maintenance_once()["expired_drafts"] == 1
    with db() as c:
        assert {row[0] for row in c.execute("SELECT id FROM job_files")} == {source}


def test_public_limits_do_not_disclose_disk_usage_or_global_capacity(client):
    response = client.get("/api/upload-limits")
    assert response.status_code == 200
    assert response.json() == {
        "max_file_bytes": files.MAX_FILE_BYTES,
        "max_card_bytes": files.MAX_CARD_BYTES,
        "max_card_files": files.MAX_CARD_FILES,
    }


@pytest.mark.parametrize(
    "values",
    [
        {"EXTORE_UPLOAD_TOTAL_BYTES": "0"},
        {"EXTORE_UPLOAD_FILE_BYTES": "-1"},
        {"EXTORE_UPLOAD_JOB_BYTES": "not-a-number"},
        {"EXTORE_UPLOAD_FILE_BYTES": str(21 * 1024 * 1024)},
        {"EXTORE_UPLOAD_CONCURRENCY": "65"},
        {"EXTORE_UPLOAD_JOB_BYTES": "1"},
        {"EXTORE_UPLOAD_TOTAL_BYTES": str(20 * 1024 * 1024)},
    ],
)
def test_invalid_storage_configuration_fails_at_startup(values):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("EXTORE_UPLOAD_")
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from extore.config import check_config; check_config()",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={**env, **values},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "RuntimeError" in result.stderr


def test_wal_checkpoint_does_not_interrupt_live_reader_and_retries(owner):
    import sqlite3

    receipt = token(owner, product(owner))
    source = upload(owner, receipt, b"retained data").json()["id"]
    reader = sqlite3.connect(DATA / "extore.sqlite3", isolation_level=None)
    try:
        reader.execute("BEGIN")
        assert (
            reader.execute(
                "SELECT content FROM job_files WHERE id=?", (source,)
            ).fetchone()[0]
            == b"retained data"
        )
        with db() as c:
            c.execute(
                "INSERT INTO settings(key,value) VALUES ('wal-test',?)", ("x" * 100000,)
            )
        started = time.monotonic()
        result = storage.maintenance_once()
        assert time.monotonic() - started < 1
        assert result["wal_checkpoint_busy"]
        assert (
            reader.execute(
                "SELECT content FROM job_files WHERE id=?", (source,)
            ).fetchone()[0]
            == b"retained data"
        )
    finally:
        reader.close()
    assert not storage.maintenance_once()["wal_checkpoint_busy"]
    with db() as c:
        assert (
            c.execute("SELECT content FROM job_files WHERE id=?", (source,)).fetchone()[
                0
            ]
            == b"retained data"
        )


def test_checkpoint_error_does_not_undo_completed_pruning(owner, monkeypatch):
    import sqlite3

    receipt = token(owner, product(owner))
    assert upload(owner, receipt, b"expired draft").status_code == 200
    with db() as c:
        c.execute("UPDATE job_files SET created=?", (time.time() - 2 * 86400,))
    original = sqlite3.connect

    def busy_connection(*args, **kwargs):
        if kwargs.get("timeout") == 0.1:
            raise sqlite3.OperationalError("checkpoint temporarily unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(storage.sqlite3, "connect", busy_connection)
    result = storage.maintenance_once()
    assert result["expired_drafts"] == 1
    assert result["wal_checkpoint_busy"]
    assert counts() == (0, 0, 0)


def test_empty_attachments_cannot_bypass_task_file_count(owner, monkeypatch):
    monkeypatch.setattr(files, "MAX_CARD_FILES", 2)
    receipt = token(owner, product(owner))
    assert upload(owner, receipt, b"").status_code == 200
    assert upload(owner, receipt, b"").status_code == 200
    assert upload(owner, receipt, b"").status_code == 413
    assert counts() == (2, 0, 0)

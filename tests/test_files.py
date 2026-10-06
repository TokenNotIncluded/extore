import json
import uuid
from concurrent.futures import ThreadPoolExecutor

from starlette.responses import Response

from extore.config import DATA
from extore.db import db
from extore.files import MAX_FILE_BYTES
from extore.security import create_session


def make_product(owner, **values):
    response = owner.post(
        "/api/admin/products",
        json={
            "name": "文件处理商品",
            "parameters": [
                {"key": "source", "label": {"zh-CN": "输入文件"}, "type": "file"}
            ],
            "outputs": [
                {"key": "result", "label": {"zh-CN": "结果文件"}, "type": "file"}
            ],
            **values,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def exchange(owner, product_id):
    response = owner.post(
        "/api/admin/cards", json={"product_id": product_id, "count": 1}
    )
    assert response.status_code == 200, response.text
    response = owner.post("/api/exchange", json={"code": response.json()["codes"][0]})
    assert response.status_code == 200, response.text
    return response.json()["token"]


def input_file(
    owner, token, *, key="source", content=b"customer-file", filename="source.txt"
):
    return owner.post(
        "/api/files/upload",
        data={"token": token, "field_key": key},
        files={"file": (filename, content, "text/plain")},
    )


def submit(owner, token, params):
    response = owner.post("/api/redeem", json={"token": token, "params": params})
    assert response.status_code == 200, response.text
    return response.json()


def batch(owner, job, action, **values):
    return owner.post(
        "/api/manage/batch",
        json={
            "product_id": job["product_id"],
            "ids": [job["id"]],
            "action": action,
            **values,
        },
    )


def output_file(
    owner, job, *, key="result", content=b"processed-file", filename="result.txt"
):
    return owner.post(
        "/api/manage/files/upload",
        data={"job_id": job["id"], "field_key": key},
        files={"file": (filename, content, "text/plain")},
    )


def prepare(owner, **product_values):
    product = make_product(owner, **product_values)
    token = exchange(owner, product["id"])
    response = input_file(owner, token)
    assert response.status_code == 200, response.text
    source = response.json()
    job = submit(owner, token, {"source": source["id"]})
    return product, token, job, source


def complete(owner, job):
    response = batch(owner, job, "claim")
    assert response.status_code == 200, response.text
    response = output_file(owner, job)
    assert response.status_code == 200, response.text
    output = response.json()
    response = batch(owner, job, "succeed", output={"result": output["id"]})
    assert response.status_code == 200, response.text
    return output


def download(owner, token, fid):
    return owner.post("/api/files/download", json={"token": token, "file_id": fid})


def set_staff(client, pid, permissions):
    response = client.post(
        "/api/admin/staff",
        json={"product_id": pid, "name": "文件权限链接", "permissions": permissions},
    )
    assert response.status_code == 200, response.text
    sid = response.json()["id"]
    with db() as c:
        cookie = create_session(c, Response(), "staff", sid)
    client.cookies.set("extore_session", cookie)
    return sid


def test_file_submission_queue_delivery_and_repeat_download(owner):
    product, token, job, source = prepare(owner)
    output = complete(owner, job)
    assert download(owner, token, output["id"]).status_code == 409
    assert download(owner, token, source["id"]).status_code == 403
    reveal = owner.post("/api/receipt/reveal", json={"token": token})
    assert reveal.status_code == 200, reveal.text
    assert reveal.json()["output"]["result"] == output["id"]
    for _ in range(2):
        response = download(owner, token, output["id"])
        assert response.status_code == 200 and response.content == b"processed-file"
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
    listed = owner.get("/api/manage/files", params={"job_id": job["id"]})
    assert listed.status_code == 200
    assert {item["id"] for item in listed.json()} == {source["id"], output["id"]}
    assert all("content" not in item for item in listed.json())
    assert "processed-file" not in json.dumps(listed.json())
    assert (
        owner.get("/api/manage/jobs", params={"product_id": product["id"]}).status_code
        == 200
    )


def test_once_files_require_reveal_and_allow_exactly_one_download(owner):
    _, token, job, _ = prepare(owner, view_policy="once")
    output = complete(owner, job)
    assert download(owner, token, output["id"]).status_code == 409
    assert owner.post("/api/receipt/reveal", json={"token": token}).status_code == 200
    assert owner.post("/api/receipt/reveal", json={"token": token}).status_code == 410
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(lambda _: download(owner, token, output["id"]), range(4))
        )
    assert sum(response.status_code == 200 for response in responses) == 1
    assert sum(response.status_code == 410 for response in responses) == 3
    with db() as c:
        row = c.execute(
            "SELECT * FROM job_files WHERE id=?", (output["id"],)
        ).fetchone()
        assert row["content"] is None and row["consumed"] == 1


def test_destroy_removes_input_output_and_unselected_draft_files(owner):
    _, token, job, _ = prepare(owner)
    response = batch(owner, job, "claim")
    assert response.status_code == 200
    response = output_file(owner, job)
    assert response.status_code == 200
    chosen = response.json()["id"]
    response = output_file(owner, job, content=b"unselected-draft")
    assert response.status_code == 200
    assert batch(owner, job, "succeed", output={"result": chosen}).status_code == 200
    assert owner.post("/api/receipt/reveal", json={"token": token}).status_code == 200
    assert owner.post("/api/receipt/destroy", json={"token": token}).status_code == 200
    with db() as c:
        assert c.execute("SELECT count(*) FROM job_files").fetchone()[0] == 0
    assert download(owner, token, chosen).status_code in (404, 410)


def test_file_references_cannot_cross_cards_or_fields(owner):
    product = make_product(
        owner,
        parameters=[
            {
                "key": "source",
                "label": {"zh-CN": "源文件"},
                "type": "file",
                "required": False,
            },
            {
                "key": "other",
                "label": {"zh-CN": "其他文件"},
                "type": "file",
                "required": False,
            },
        ],
    )
    first = exchange(owner, product["id"])
    second = exchange(owner, product["id"])
    source = input_file(owner, first).json()
    for token, params in (
        (second, {"source": source["id"]}),
        (first, {"other": source["id"]}),
    ):
        response = owner.post("/api/redeem", json={"token": token, "params": params})
        assert response.status_code == 403, response.text
    with db() as c:
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_input_upload_rejected_after_submission_and_for_unknown_field(owner):
    _, token, _, _ = prepare(owner)
    assert input_file(owner, token).status_code == 409
    product = make_product(owner)
    another = exchange(owner, product["id"])
    assert input_file(owner, another, key="unknown").status_code == 400
    assert input_file(owner, "not-a-grant").status_code == 404


def test_output_upload_requires_claimed_task_and_exact_field(owner):
    _, _, job, _ = prepare(owner)
    assert output_file(owner, job).status_code == 409
    assert batch(owner, job, "claim").status_code == 200
    assert output_file(owner, job, key="unknown").status_code == 400
    response = output_file(owner, job)
    assert response.status_code == 200
    fid = response.json()["id"]
    assert batch(owner, job, "succeed", output={"result": fid}).status_code == 200
    assert output_file(owner, job).status_code == 409


def test_output_reference_cannot_use_input_file_or_another_job(owner):
    product, _, first, source = prepare(owner)
    token = exchange(owner, product["id"])
    second_source = input_file(owner, token).json()
    second = submit(owner, token, {"source": second_source["id"]})
    assert batch(owner, first, "claim").status_code == 200
    assert batch(owner, second, "claim").status_code == 200
    out = output_file(owner, first).json()
    assert (
        batch(owner, first, "succeed", output={"result": source["id"]}).status_code
        == 403
    )
    assert (
        batch(owner, second, "succeed", output={"result": out["id"]}).status_code == 403
    )


def test_retry_reuses_input_files_and_invalidates_old_outputs(owner):
    _, token, job, source = prepare(owner)
    assert batch(owner, job, "claim").status_code == 200
    old = output_file(owner, job).json()
    assert batch(owner, job, "fail", retryable=True).status_code == 200
    retried = submit(owner, token, {"source": source["id"]})
    assert retried["attempt"] == 2
    with db() as c:
        assert (
            c.execute("SELECT id FROM job_files WHERE id=?", (old["id"],)).fetchone()
            is None
        )
        kept = c.execute(
            "SELECT attempt,bound FROM job_files WHERE id=?", (source["id"],)
        ).fetchone()
        assert kept["attempt"] == 2 and kept["bound"] == 1
    assert batch(owner, retried, "claim").status_code == 200
    assert (
        batch(owner, retried, "succeed", output={"result": old["id"]}).status_code
        == 404
    )


def test_staff_download_is_scoped_to_product_and_queue_view(owner):
    product, _, first, source = prepare(owner)
    other = make_product(owner)
    sid = set_staff(owner, other["id"], ["queue.view"])
    response = owner.get(f"/api/manage/files/{source['id']}/download")
    assert response.status_code == 403
    assert (
        owner.get("/api/manage/files", params={"job_id": first["id"]}).status_code
        == 403
    )
    with db() as c:
        c.execute("UPDATE staff SET product_id=? WHERE id=?", (product["id"], sid))
    assert (
        owner.get(f"/api/manage/files/{source['id']}/download").content
        == b"customer-file"
    )
    assert output_file(owner, first).status_code == 403
    with db() as c:
        c.execute("UPDATE staff SET revoked=1 WHERE id=?", (sid,))
    assert owner.get(f"/api/manage/files/{source['id']}/download").status_code == 401


def test_staff_cannot_upload_for_task_claimed_by_owner(owner):
    product, _, job, _ = prepare(owner)
    assert batch(owner, job, "claim").status_code == 200
    set_staff(owner, product["id"], ["queue.view", "queue.process"])
    assert output_file(owner, job).status_code == 409


def test_only_queue_jobs_accept_output_attachments(owner):
    _, token, job, _ = prepare(
        owner,
        mode="webhook",
        webhook_url="https://example.com/fulfill",
        webhook_secret="attachment-webhook-signing-secret-32-chars",
    )
    with db() as c:
        c.execute(
            "UPDATE jobs SET state='processing',claimed_by='owner' WHERE id=?",
            (job["id"],),
        )
    response = output_file(owner, job)
    assert response.status_code == 409, response.text
    assert owner.post("/api/receipt", json={"token": token}).status_code == 200


def test_upload_limits_are_atomic_and_do_not_retain_partial_blobs(owner, monkeypatch):
    import extore.files as attachments

    product = make_product(owner)
    token = exchange(owner, product["id"])
    monkeypatch.setattr(attachments, "MAX_FILE_BYTES", 4)
    response = input_file(owner, token, content=b"12345")
    assert response.status_code == 413
    with db() as c:
        assert c.execute("SELECT count(*) FROM job_files").fetchone()[0] == 0
    monkeypatch.setattr(attachments, "MAX_FILE_BYTES", MAX_FILE_BYTES)
    monkeypatch.setattr(attachments, "MAX_CARD_BYTES", 10)
    assert input_file(owner, token, content=b"123456").status_code == 200
    assert input_file(owner, token, content=b"12345").status_code == 413
    with db() as c:
        assert c.execute("SELECT SUM(size) FROM job_files").fetchone()[0] == 6


def test_chunked_multipart_stream_cannot_bypass_total_body_limit(owner, monkeypatch):
    import extore.files as attachments

    monkeypatch.setattr(attachments, "MAX_MULTIPART_BYTES", 100)
    boundary = "extore-test-boundary"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\n\r\n'
        + "x" * 101
        + f"\r\n--{boundary}--\r\n"
    ).encode()
    response = owner.post(
        "/api/files/upload",
        content=iter((body[:80], body[80:])),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert response.status_code == 413


def test_multipart_duplicate_and_unknown_fields_are_rejected(owner):
    product = make_product(owner)
    token = exchange(owner, product["id"])
    duplicate = owner.post(
        "/api/files/upload",
        files=[
            ("token", (None, token)),
            ("token", (None, token)),
            ("field_key", (None, "source")),
            ("file", ("a.txt", b"data")),
        ],
    )
    assert duplicate.status_code == 400
    response = owner.post(
        "/api/files/upload",
        data={"token": token, "field_key": "source", "unapproved": "value"},
        files={"file": ("a.txt", b"data")},
    )
    assert response.status_code == 400


def test_upload_bytes_are_never_executed_or_extracted(owner):
    _, token, job, _ = prepare(owner)
    assert batch(owner, job, "claim").status_code == 200
    payload = b"<script>alert('untrusted upload')</script>"
    response = output_file(owner, job, content=payload, filename="../../evil.html")
    assert response.status_code == 200, response.text
    fid = response.json()["id"]
    assert response.json()["filename"] == "evil.html"
    assert not (DATA / "evil.html").exists()
    assert batch(owner, job, "succeed", output={"result": fid}).status_code == 200
    assert owner.post("/api/receipt/reveal", json={"token": token}).status_code == 200
    response = download(owner, token, fid)
    assert response.content == payload
    assert response.headers["content-type"] == "application/octet-stream"
    assert "sandbox" in response.headers["content-security-policy"]


def test_unknown_file_ids_do_not_produce_public_download_links(owner):
    product = make_product(owner)
    token = exchange(owner, product["id"])
    assert download(owner, token, str(uuid.uuid4())).status_code == 404
    assert owner.get("/api/files/download").status_code in (404, 405)
    response = owner.post(
        "/api/files/download", json={"token": "invalid", "file_id": str(uuid.uuid4())}
    )
    assert response.status_code == 404


def test_exact_job_file_preflight_filters_before_limit_and_preserves_scope(owner):
    product = make_product(owner, parameters=[], outputs=[], delivery="service")
    first = submit(owner, exchange(owner, product["id"]), {})
    second = submit(owner, exchange(owner, product["id"]), {})
    response = owner.get(
        "/api/manage/jobs",
        params={"product_id": product["id"], "job_id": second["id"], "limit": 1},
    )
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()] == [second["id"]]
    assert first["id"] != second["id"]
    other = make_product(owner, parameters=[], outputs=[], delivery="service")
    foreign = submit(owner, exchange(owner, other["id"]), {})
    for identifier, state in ((foreign["id"], ""), (second["id"], "processing")):
        response = owner.get(
            "/api/manage/jobs",
            params={
                "product_id": product["id"],
                "job_id": identifier,
                "state": state,
                "limit": 1,
            },
        )
        assert response.status_code == 200 and response.json() == []
    set_staff(owner, product["id"], ["queue.view"])
    response = owner.get(
        "/api/manage/jobs",
        params={"product_id": product["id"], "job_id": second["id"], "limit": 1},
    )
    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [second["id"]]
    assert (
        owner.get(
            "/api/manage/jobs",
            params={"product_id": other["id"], "job_id": foreign["id"]},
        ).status_code
        == 403
    )

import argparse
import io
import json
import stat
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from extore import customer_cli as remote
from extore.app import app
from extore.db import db
from extore.manage_client import ManageError

ORIGIN = "http://localhost:8000"


def arguments(*argv, profile=None):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command")
    remote.add_parser(commands)
    prefix = ["customer"]
    if profile:
        prefix += ["--profile", str(profile)]
    return parser.parse_args([*prefix, *argv])


@pytest.fixture
def customer(tmp_path, monkeypatch):
    profile = tmp_path / "private" / "customer.json"
    calls = []
    with TestClient(app, base_url=ORIGIN) as client:

        def relay(request):
            body = request.read()
            calls.append(
                (request.method, request.url.path, dict(request.headers), body)
            )
            assert "authorization" not in request.headers
            assert "cookie" not in request.headers
            assert request.headers["Origin"] == ORIGIN
            response = client.request(
                request.method,
                str(request.url),
                headers=request.headers,
                content=body,
            )
            return httpx.Response(
                response.status_code, headers=response.headers, content=response.content
            )

        transport = httpx.MockTransport(relay)

        def run(*argv, stdin=None):
            if stdin is not None:
                monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
            return remote.execute(
                arguments(*argv, profile=profile), transport=transport
            )

        yield run, profile, calls


def make_product(owner, **kwargs):
    config = {
        "name": "CLI 任务商品",
        "public": False,
        "parameters": [{"key": "request", "label": {"zh-CN": "需求"}}],
        **kwargs,
    }
    response = owner.post("/api/admin/products", json=config)
    assert response.status_code == 200, response.text
    return response.json()


def issue(owner, product, count=1):
    response = owner.post(
        "/api/admin/cards", json={"product_id": product["id"], "count": count}
    )
    assert response.status_code == 200, response.text
    return response.json()["codes"]


def exchange(run, codes):
    return run("exchange", "--origin", ORIGIN, "--codes-stdin", stdin="\n".join(codes))


def batch(owner, pid, jid, action, **kwargs):
    response = owner.post(
        "/api/manage/batch",
        json={"product_id": pid, "ids": [jid], "action": action, **kwargs},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_public_catalog_is_compact_and_private_schema_requires_receipt(owner, customer):
    run, profile, _ = customer
    public = make_product(owner, public=True, description="long-public-description")
    private = make_product(owner, description="private description")
    listed = run("products", "--origin", ORIGIN)
    assert [item["id"] for item in listed["products"]] == [public["id"]]
    assert "long-public-description" not in json.dumps(listed)
    assert not profile.exists()
    detail = run("schema", "--product", public["id"], "--origin", ORIGIN)
    assert detail["product"]["parameters"][0]["key"] == "request"
    assert "description" not in detail["product"]["parameters"][0]
    with pytest.raises(ManageError, match="Public product not found"):
        run("schema", "--product", private["id"], "--origin", ORIGIN)
    receipt = exchange(run, issue(owner, private))
    detail = run("schema", "--receipt", receipt["receipt_id"], "--detail")
    assert detail["product"]["description"] == "private description"


def test_exchange_privately_saves_token_and_receipts_hide_codes(owner, customer):
    run, profile, calls = customer
    product = make_product(owner)
    codes = issue(owner, product)
    receipt = exchange(run, codes)
    saved = json.loads(profile.read_text())
    token = saved["receipts"][0]["token"]
    assert token and token not in json.dumps(receipt)
    assert codes[0] not in profile.read_text()
    assert saved["receipts"][0]["id"] == receipt["receipt_id"]
    assert stat.S_IMODE(profile.stat().st_mode) == 0o600
    assert stat.S_IMODE(profile.parent.stat().st_mode) == 0o700
    assert not any(
        "private_key" in item or "access_token" in item for item in saved["receipts"]
    )
    count = len(calls)
    listed = run("receipts")
    assert len(calls) == count
    assert token not in json.dumps(listed)
    assert codes[0] not in json.dumps(listed)


def test_local_receipt_id_cannot_be_mistaken_for_a_command_option(
    owner, customer, monkeypatch
):
    run, _, _ = customer
    product = make_product(owner)
    codes = issue(owner, product)
    monkeypatch.setattr(
        remote.secrets, "token_urlsafe", lambda size: "-synthetic-random-id"
    )
    result = exchange(run, codes)
    assert result["receipt_id"].startswith("rcpt_")
    assert run("receipt", result["receipt_id"])["job"] is None


def test_file_path_submission_avoids_picker_and_does_not_complete_task(
    owner, customer, tmp_path
):
    run, _, _ = customer
    product = make_product(
        owner,
        parameters=[
            {"key": "request", "label": {"zh-CN": "需求"}},
            {"key": "source", "label": {"zh-CN": "文件"}, "type": "file"},
        ],
    )
    receipt = exchange(run, issue(owner, product))["receipt_id"]
    source = tmp_path / "input.docx"
    source.write_bytes(b"synthetic-docx")
    uploaded = run("upload", receipt, "--field", "source", "--file", str(source))
    assert uploaded["kind"] == "input"
    assert run("files", receipt)["inputs"][0]["id"] == uploaded["id"]
    assert run("receipt", receipt)["job"] is None
    submitted = run(
        "redeem",
        receipt,
        "--params-stdin",
        "--file",
        f"source={source}",
        stdin=json.dumps({"request": "synthetic-secret-input"}),
    )
    assert submitted["job"]["state"] == "queued"
    assert "synthetic-secret-input" not in json.dumps(submitted)
    with db() as c:
        row = c.execute(
            "SELECT * FROM jobs WHERE id=?", (submitted["job"]["id"],)
        ).fetchone()
        params = json.loads(row["params"])
        assert params["source"]
        assert (
            c.execute(
                "SELECT content FROM job_files WHERE id=?", (params["source"],)
            ).fetchone()[0]
            == b"synthetic-docx"
        )
    with pytest.raises(ManageError, match="cannot accept"):
        run("upload", receipt, "--field", "source", "--file", str(source))


def test_returned_task_can_be_retried_but_rejected_task_cannot(owner, customer):
    run, _, _ = customer
    product = make_product(owner, allow_retry=False, max_attempts=1)
    receipt = exchange(run, issue(owner, product))["receipt_id"]
    first = run("redeem", receipt, "--params-stdin", stdin='{"request":"first"}')["job"]
    batch(owner, product["id"], first["id"], "claim")
    batch(owner, product["id"], first["id"], "request_changes", message="请补充需求")
    status = run("status", receipt)["job"]
    assert status["state"] == "needs_input" and status["can_retry"]
    assert status["message"] == "请补充需求"
    assert "inputs" not in run("receipt", receipt)
    assert run("receipt", receipt, "--inputs")["inputs"] == {"request": "first"}
    retried = run("retry", receipt, "--params-stdin", stdin='{"request":"corrected"}')[
        "job"
    ]
    assert retried["id"] == first["id"] and retried["attempt"] == 2
    batch(owner, product["id"], first["id"], "claim")
    batch(owner, product["id"], first["id"], "reject", message="超出能力范围")
    status = run("receipt", receipt)["job"]
    assert status["state"] == "rejected" and not status["can_retry"]
    assert status["message"] == "超出能力范围"
    with pytest.raises(ManageError, match="cannot be retried"):
        run("retry", receipt, "--params-stdin", stdin='{"request":"again"}')


def test_batch_requires_scoped_card_and_uses_each_frozen_schema(
    owner, customer, tmp_path
):
    run, _, calls = customer
    product = make_product(owner)
    codes = issue(owner, product, 2)
    receipt = exchange(run, codes)
    rid = receipt["receipt_id"]
    cards = [item["card_id"] for item in receipt["items"]]
    with pytest.raises(ManageError, match="Select --card"):
        run("schema", "--receipt", rid)
    with pytest.raises(ManageError, match="not in this receipt"):
        run("schema", "--receipt", rid, "--card", "wrong-card")
    first = run(
        "redeem", rid, "--card", cards[0], "--params-stdin", stdin='{"request":"first"}'
    )
    assert first["items"][0]["job"]["state"] == "queued"
    assert first["items"][1]["job"] is None
    edited = {
        **product,
        "parameters": [{"key": "email", "label": {"zh-CN": "邮箱"}, "type": "email"}],
    }
    assert (
        owner.put(f"/api/admin/products/{product['id']}", json=edited).status_code
        == 200
    )
    assert (
        run("schema", "--receipt", rid, "--card", cards[0])["product"]["parameters"][0][
            "key"
        ]
        == "request"
    )
    assert (
        run("schema", "--receipt", rid, "--card", cards[1])["product"]["parameters"][0][
            "key"
        ]
        == "email"
    )
    items = tmp_path / "items.json"
    items.write_text(
        json.dumps(
            [{"card_id": cards[1], "params": {"email": "synthetic@example.com"}}]
        )
    )
    completed = run("redeem", rid, "--items-file", str(items))
    assert all(item["job"]["state"] == "queued" for item in completed["items"])
    # Local bounds reject cross-card operations before a mutation request.
    count = sum(path == "/api/receipt/destroy" for _, path, _, _ in calls)
    with pytest.raises(ManageError, match="not in this receipt"):
        run("destroy", rid, "--card", "wrong-card", "--confirm")
    assert sum(path == "/api/receipt/destroy" for _, path, _, _ in calls) == count


def test_batch_invalid_items_do_not_partially_submit(owner, customer, tmp_path):
    run, _, _ = customer
    product = make_product(owner)
    receipt = exchange(run, issue(owner, product, 2))
    first, second = (item["card_id"] for item in receipt["items"])
    path = tmp_path / "items.json"
    path.write_text(
        json.dumps(
            [
                {"card_id": first, "params": {"request": "first"}},
                {"card_id": second, "params": {"unknown": "wrong"}},
            ]
        )
    )
    with pytest.raises(ManageError, match="Unknown parameter"):
        run("redeem", receipt["receipt_id"], "--items-file", str(path))
    assert all(
        item["job"] is None for item in run("receipt", receipt["receipt_id"])["items"]
    )


def test_exchange_mixed_products_is_atomic_and_expired_receipt_is_rejected(
    owner, customer
):
    run, profile, _ = customer
    first = make_product(owner)
    second = make_product(owner)
    with pytest.raises(ManageError) as error:
        exchange(run, issue(owner, first) + issue(owner, second))
    assert error.value.status == 400
    with db() as c:
        assert c.execute("SELECT count(*) FROM grants").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM receipt_batches").fetchone()[0] == 0
    result = exchange(run, issue(owner, first))
    with db() as c:
        c.execute("UPDATE grants SET expires=?", (time.time() - 1,))
    with pytest.raises(ManageError) as error:
        run("receipt", result["receipt_id"])
    assert error.value.status == 404
    assert json.loads(profile.read_text())["receipts"][0]["token"] not in str(
        error.value
    )


def test_reveal_once_is_saved_privately_and_existing_output_cannot_consume_delivery(
    owner, customer, tmp_path
):
    run, profile, _ = customer
    product = make_product(owner, view_policy="once")
    receipt = exchange(run, issue(owner, product))["receipt_id"]
    row = run("redeem", receipt, "--params-stdin", stdin='{"request":"synthetic"}')[
        "job"
    ]
    batch(owner, product["id"], row["id"], "claim")
    batch(owner, product["id"], row["id"], "succeed", content="private-delivery")
    output = tmp_path / "delivery.json"
    output.write_text("keep-existing")
    with pytest.raises(ManageError, match="already exists"):
        run("reveal", receipt, "--output", str(output))
    assert run("receipt", receipt)["job"]["revealed"] == 0
    output.unlink()
    result = run("reveal", receipt, "--output", str(output))
    assert result["output"] == str(output)
    assert "private-delivery" not in json.dumps(result)
    assert json.loads(output.read_text())["content"] == "private-delivery"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert "private-delivery" not in profile.read_text()
    second = tmp_path / "again.json"
    with pytest.raises(ManageError) as error:
        run("reveal", receipt, "--output", str(second))
    assert error.value.status == 410
    assert not second.exists()
    assert run("destroy", receipt, "--confirm")["ok"]
    assert run("receipt", receipt)["job"]["state"] == "destroyed"


def test_output_file_downloads_once_and_no_cross_batch_card_leak(
    owner, customer, tmp_path
):
    run, _, _ = customer
    product = make_product(
        owner,
        view_policy="once",
        outputs=[{"key": "result", "label": {"zh-CN": "文件"}, "type": "file"}],
    )
    receipt = exchange(run, issue(owner, product, 2))
    rid = receipt["receipt_id"]
    card = receipt["items"][0]["card_id"]
    other = receipt["items"][1]["card_id"]
    submitted = run(
        "redeem",
        rid,
        "--card",
        card,
        "--params-stdin",
        stdin='{"request":"file output"}',
    )
    row = submitted["items"][0]["job"]
    batch(owner, product["id"], row["id"], "claim")
    file = owner.post(
        "/api/manage/files/upload",
        data={"job_id": row["id"], "field_key": "result"},
        files={
            "file": ("result.docx", b"synthetic-result", "application/octet-stream")
        },
    ).json()
    batch(owner, product["id"], row["id"], "succeed", output={"result": file["id"]})
    revealed = run(
        "reveal", rid, "--card", card, "--output", str(tmp_path / "delivery.json")
    )
    assert revealed["files"][0]["id"] == file["id"]
    with pytest.raises(ManageError, match="File not found"):
        run(
            "download",
            rid,
            "--card",
            other,
            "--file-id",
            file["id"],
            "--output",
            str(tmp_path / "wrong.docx"),
        )
    target = tmp_path / "result.docx"
    result = run(
        "download",
        rid,
        "--card",
        card,
        "--file-id",
        file["id"],
        "--output",
        str(target),
    )
    assert result["size"] == len(b"synthetic-result")
    assert target.read_bytes() == b"synthetic-result"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    again = tmp_path / "again.docx"
    with pytest.raises(ManageError) as error:
        run(
            "download",
            rid,
            "--card",
            card,
            "--file-id",
            file["id"],
            "--output",
            str(again),
        )
    assert error.value.status == 410 and not again.exists()
    run("destroy", rid, "--card", card, "--confirm")
    assert run("receipt", rid)["items"][1]["job"] is None


def test_receipt_link_import_is_private_and_idempotent(owner, customer):
    run, profile, _ = customer
    product = make_product(owner)
    first = exchange(run, issue(owner, product))
    token = json.loads(profile.read_text())["receipts"][0]["token"]
    link = ORIGIN + "/receipt#" + token
    imported = run("import-receipt", "--link-stdin", stdin=link)
    assert imported["receipt_id"] == first["receipt_id"]
    assert token not in json.dumps(imported)
    assert len(run("receipts")["receipts"]) == 1


@pytest.mark.parametrize(
    "origin",
    [
        "http://merchant.example",
        "https://user:pass@example.com",
        "https://merchant.example/path",
        "https://merchant.example?token=x",
    ],
)
def test_customer_origin_rejects_unsafe_addresses(origin):
    with pytest.raises(ManageError):
        remote.execute(
            arguments("products", "--origin", origin),
            transport=httpx.MockTransport(
                lambda request: pytest.fail("unsafe address must not be requested")
            ),
        )


def test_redirect_is_not_followed_and_error_does_not_echo_code(
    tmp_path, monkeypatch, capsys
):
    calls = []

    def redirect(request):
        calls.append(request.url)
        return httpx.Response(
            307,
            headers={"Location": "https://evil.example/collect"},
            text="secret-code",
        )

    profile = tmp_path / "private" / "customer.json"
    monkeypatch.setattr("sys.stdin", io.StringIO("secret-code"))
    with pytest.raises(ManageError) as error:
        remote.execute(
            arguments(
                "exchange",
                "--origin",
                "https://merchant.example",
                "--codes-stdin",
                profile=profile,
            ),
            transport=httpx.MockTransport(redirect),
        )
    assert len(calls) == 1
    assert "secret-code" not in str(error.value)
    monkeypatch.setattr(
        remote, "execute", lambda args: (_ for _ in ()).throw(error.value)
    )
    with pytest.raises(SystemExit):
        remote.run(arguments("receipts"))
    assert "secret-code" not in capsys.readouterr().err


def test_profile_rejects_symlink_and_public_permissions(owner, customer, tmp_path):
    run, profile, _ = customer
    exchange(run, issue(owner, make_product(owner)))
    profile.chmod(0o644)
    with pytest.raises(ManageError, match="mode 600"):
        run("receipts")
    profile.chmod(0o600)
    target = tmp_path / "target.json"
    profile.rename(target)
    profile.symlink_to(target)
    with pytest.raises(ManageError, match="regular"):
        run("receipts")
    profile.unlink()
    target.rename(profile)
    link = tmp_path / "linked-dir"
    link.symlink_to(profile.parent, target_is_directory=True)
    with pytest.raises(ManageError, match="symlinks"):
        remote.execute(arguments("receipts", profile=link / "customer.json"))


def test_input_size_and_file_preflight_avoid_partial_upload(owner, customer, tmp_path):
    run, _, calls = customer
    with pytest.raises(ManageError, match="8000"):
        run("exchange", "--origin", ORIGIN, "--codes-stdin", stdin="x" * 8001)
    product = make_product(
        owner,
        parameters=[{"key": "source", "label": {"zh-CN": "文件"}, "type": "file"}],
    )
    receipt = exchange(run, issue(owner, product))["receipt_id"]
    source = tmp_path / "too-large.bin"
    with source.open("wb") as file:
        file.truncate(remote.MAX_FILE_BYTES + 1)
    count = sum(path == "/api/files/upload" for _, path, _, _ in calls)
    with pytest.raises(ManageError, match="20 MiB"):
        run(
            "redeem",
            receipt,
            "--params-stdin",
            "--file",
            f"source={source}",
            stdin="{}",
        )
    assert sum(path == "/api/files/upload" for _, path, _, _ in calls) == count
    with pytest.raises(ManageError, match="too large"):
        run(
            "redeem",
            receipt,
            "--params-stdin",
            stdin="x" * (remote.MAX_INPUT_BYTES + 1),
        )


def test_destroy_requires_explicit_confirmation():
    with pytest.raises(SystemExit):
        arguments("destroy", "receipt-id")


def test_effective_server_file_limit_is_checked_before_upload(
    owner, customer, tmp_path, monkeypatch
):
    run, _, calls = customer
    monkeypatch.setattr("extore.files.MAX_FILE_BYTES", 4)
    product = make_product(
        owner,
        parameters=[{"key": "source", "label": {"zh-CN": "文件"}, "type": "file"}],
    )
    receipt = exchange(run, issue(owner, product))["receipt_id"]
    source = tmp_path / "over-server-limit.bin"
    source.write_bytes(b"12345")
    with pytest.raises(ManageError, match="4 bytes"):
        run("upload", receipt, "--field", "source", "--file", str(source))
    assert not any(path == "/api/files/upload" for _, path, _, _ in calls)


def test_unicode_json_input_is_bounded_in_bytes(monkeypatch):
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"request": "文" * 100_000}, ensure_ascii=False)),
    )
    with pytest.raises(ManageError, match="too large"):
        remote._read_json()


def test_catalog_and_parser_do_not_initialize_server_data(tmp_path, monkeypatch):
    data = tmp_path / "unused-server-data"
    monkeypatch.setenv("EXTORE_DATA", str(data))
    with pytest.raises(SystemExit) as error:
        arguments("--help")
    assert error.value.code == 0
    assert not data.exists()
    result = remote.execute(
        arguments("products", "--origin", "https://merchant.example"),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[])),
    )
    assert result == {"ok": True, "products": []} and not data.exists()


def test_unexpected_secret_fields_in_upload_and_destroy_are_not_returned(
    owner, customer, tmp_path, monkeypatch
):
    run, profile, _ = customer
    product = make_product(
        owner,
        parameters=[{"key": "source", "label": {"zh-CN": "文件"}, "type": "file"}],
    )
    receipt = exchange(run, issue(owner, product))["receipt_id"]
    original = remote.CustomerClient.json

    def decorated(self, origin, method, path, **kwargs):
        result = original(self, origin, method, path, **kwargs)
        if path in ("/api/files/upload", "/api/receipt/destroy"):
            result.update(
                {
                    "token": "unexpected-secret",
                    "access_token": "unexpected-bearer",
                    "params": {"private": "unexpected-input"},
                }
            )
        return result

    monkeypatch.setattr(remote.CustomerClient, "json", decorated)
    source = tmp_path / "source.txt"
    source.write_bytes(b"synthetic")
    result = run("upload", receipt, "--field", "source", "--file", str(source))
    assert not {"token", "access_token", "params"} & result.keys()
    assert "unexpected-secret" not in json.dumps(result)
    assert "unexpected-secret" not in profile.read_text()
    row = run(
        "redeem", receipt, "--params-stdin", stdin=json.dumps({"source": result["id"]})
    )["job"]
    batch(owner, product["id"], row["id"], "claim")
    batch(owner, product["id"], row["id"], "succeed", content="synthetic-result")
    destroyed = run("destroy", receipt, "--confirm")
    assert destroyed == {"ok": True, "receipt_id": receipt}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(job="unexpected-job-shape"),
        lambda value: value.update(product="unexpected-product-shape"),
        lambda value: value["product"].update(parameters="unexpected-field-shape"),
        lambda value: value.update(batch=True, items="unexpected-items-shape"),
        lambda value: value.update(
            batch=True,
            items=[
                {
                    "card_id": "synthetic-card",
                    "product": value["product"],
                    "job": "unexpected-job-shape",
                }
            ],
        ),
    ],
)
def test_malformed_nested_receipt_is_a_safe_error_without_traceback(
    owner, customer, tmp_path, monkeypatch, capsys, mutation
):
    run, _, _ = customer
    product = make_product(
        owner,
        parameters=[{"key": "source", "label": {"zh-CN": "文件"}, "type": "file"}],
    )
    receipt = exchange(run, issue(owner, product))["receipt_id"]
    original_json = remote.CustomerClient.json

    def malformed(self, origin, method, path, **kwargs):
        result = original_json(self, origin, method, path, **kwargs)
        if path == "/api/receipt":
            mutation(result)
        return result

    monkeypatch.setattr(remote.CustomerClient, "json", malformed)
    source = tmp_path / "source.txt"
    source.write_bytes(b"synthetic")
    with pytest.raises(ManageError) as error:
        run("upload", receipt, "--field", "source", "--file", str(source))
    assert error.value.code == "invalid_response"
    monkeypatch.setattr(
        remote, "execute", lambda args: (_ for _ in ()).throw(error.value)
    )
    with pytest.raises(SystemExit) as exit_error:
        remote.run(arguments("receipt", receipt))
    assert exit_error.value.code == 1
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err)["code"] == "invalid_response"
    assert "Traceback" not in output.err and "unexpected-job-shape" not in output.err


def test_reuse_retry_keeps_private_original_inputs_and_bound_file(
    owner, customer, tmp_path, monkeypatch, capsys
):
    run, profile, calls = customer
    product = make_product(
        owner,
        allow_retry=False,
        max_attempts=1,
        parameters=[
            {"key": "request", "label": {"zh-CN": "需求"}},
            {"key": "source", "label": {"zh-CN": "附件"}, "type": "file"},
        ],
    )
    codes = issue(owner, product)
    rid = exchange(run, codes)["receipt_id"]
    source = tmp_path / "original.txt"
    source.write_bytes(b"private-original-file")
    row = run(
        "redeem",
        rid,
        "--params-stdin",
        "--file",
        f"source={source}",
        stdin='{"request":"private-original-requirements"}',
    )["job"]
    batch(owner, product["id"], row["id"], "claim")
    batch(
        owner,
        product["id"],
        row["id"],
        "request_retry",
        message="上游暂时不可用，请复用原始输入重试",
        retry_mode="reuse",
        reason_type="external",
    )
    token = json.loads(profile.read_text())["receipts"][0]["token"]
    with db() as c:
        original = c.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
        original_params = json.loads(original["params"])
        file_id = original_params["source"]
        original_file = c.execute(
            "SELECT job_id,content FROM job_files WHERE id=?", (file_id,)
        ).fetchone()
        assert original_file["job_id"] == row["id"]
    count = len(calls)
    retried_result = run("retry", rid, "--reuse")
    monkeypatch.setattr(remote, "execute", lambda args: retried_result)
    remote.run(arguments("retry", rid, "--reuse", profile=profile))
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert not output.err
    assert result["job"]["id"] == row["id"]
    assert result["job"]["state"] == "queued"
    assert result["job"]["attempt"] == 2
    for private in (token, codes[0], "private-original-requirements", file_id):
        assert private not in output.out
    mutations = [
        (path, json.loads(body))
        for method, path, _, body in calls[count:]
        if method != "GET" and path != "/api/receipt"
    ]
    assert mutations == [("/api/retry", {"token": token})]
    assert "private-original-requirements" not in profile.read_text()
    with db() as c:
        retried = c.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
        assert json.loads(retried["params"]) == original_params
        assert retried["claimed_by"] is None and retried["lease"] is None
        assert retried["message"] == "" and retried["progress"] == 0
        reused_file = c.execute(
            "SELECT job_id,content FROM job_files WHERE id=?", (file_id,)
        ).fetchone()
        assert tuple(reused_file) == tuple(original_file)


def test_reuse_retry_changes_only_the_selected_batch_card(owner, customer):
    run, profile, calls = customer
    product = make_product(owner, allow_retry=False, max_attempts=1)
    receipt = exchange(run, issue(owner, product, 2))
    rid = receipt["receipt_id"]
    cards = [item["card_id"] for item in receipt["items"]]
    jobs = []
    for index, card in enumerate(cards):
        submitted = run(
            "redeem",
            rid,
            "--card",
            card,
            "--params-stdin",
            stdin=json.dumps({"request": f"private-batch-input-{index}"}),
        )
        row = submitted["items"][index]["job"]
        jobs.append(row["id"])
        batch(owner, product["id"], row["id"], "claim")
        batch(
            owner,
            product["id"],
            row["id"],
            "request_retry",
            message=f"暂时不可用 {index}",
            retry_mode="reuse",
            reason_type="processor",
        )
    token = json.loads(profile.read_text())["receipts"][0]["token"]
    count = len(calls)
    result = run("retry", rid, "--card", cards[1], "--reuse")
    assert result["items"][0]["job"]["state"] == "needs_input"
    assert result["items"][0]["job"]["attempt"] == 1
    assert result["items"][1]["job"]["state"] == "queued"
    assert result["items"][1]["job"]["attempt"] == 2
    assert result["items"][1]["job"]["id"] == jobs[1]
    retries = [
        json.loads(body) for _, path, _, body in calls[count:] if path == "/api/retry"
    ]
    assert retries == [{"token": token, "card_id": cards[1]}]
    assert not any(
        path in ("/api/redeem", "/api/files/upload") for _, path, _, _ in calls[count:]
    )
    for private in (token, "private-batch-input-0", "private-batch-input-1"):
        assert private not in json.dumps(result)
    with db() as c:
        rows = [
            c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
            for jid in jobs
        ]
        assert [json.loads(row["params"])["request"] for row in rows] == [
            "private-batch-input-0",
            "private-batch-input-1",
        ]


def test_reuse_with_new_attachment_is_rejected_before_any_request(
    owner, customer, tmp_path
):
    run, _, calls = customer
    product = make_product(owner)
    rid = exchange(run, issue(owner, product))["receipt_id"]
    count = len(calls)
    with pytest.raises(ManageError, match="cannot upload new attachments") as error:
        run("retry", rid, "--reuse", "--file", f"source={tmp_path / 'missing.txt'}")
    assert error.value.code == "invalid_input"
    assert len(calls) == count
    with db() as c:
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM job_files").fetchone()[0] == 0


@pytest.mark.parametrize(
    "input_flag", ["--params-stdin", "--params-file", "--items-file"]
)
def test_reuse_does_not_accept_replacement_input_flags(input_flag, tmp_path):
    replacement = (
        [] if input_flag == "--params-stdin" else [str(tmp_path / "inputs.json")]
    )
    with pytest.raises(SystemExit) as error:
        arguments("retry", "receipt-id", "--reuse", input_flag, *replacement)
    assert error.value.code == 2


@pytest.mark.parametrize("disable", ["revise", "expired", "already_queued"])
def test_reuse_is_denied_before_retry_when_private_preflight_disallows_it(
    owner, customer, disable
):
    run, profile, calls = customer
    product = make_product(owner)
    rid = exchange(run, issue(owner, product))["receipt_id"]
    row = run("redeem", rid, "--params-stdin", stdin='{"request":"private-input"}')[
        "job"
    ]
    if disable != "already_queued":
        batch(owner, product["id"], row["id"], "claim")
        batch(
            owner,
            product["id"],
            row["id"],
            "request_retry",
            message="需要用户确认",
            retry_mode="revise" if disable == "revise" else "reuse",
        )
    if disable == "expired":
        with db() as c:
            c.execute(
                "UPDATE card_meta SET expires=? WHERE card_id=(SELECT card_id FROM jobs WHERE id=?)",
                (time.time() - 1, row["id"]),
            )
    token = json.loads(profile.read_text())["receipts"][0]["token"]
    count = len(calls)
    with pytest.raises(
        ManageError, match="requires revised input or cannot be retried"
    ) as error:
        run("retry", rid, "--reuse")
    assert error.value.code == "invalid_state" and token not in str(error.value)
    assert [path for _, path, _, _ in calls[count:]] == ["/api/receipt"]
    with db() as c:
        unchanged = c.execute(
            "SELECT state,attempt,params FROM jobs WHERE id=?", (row["id"],)
        ).fetchone()
        assert unchanged["attempt"] == 1
        assert unchanged["state"] == (
            "queued" if disable == "already_queued" else "needs_input"
        )
        assert json.loads(unchanged["params"]) == {"request": "private-input"}


@pytest.mark.parametrize("selection", ["foreign", "missing"])
def test_reuse_batch_requires_a_card_from_the_private_receipt(
    owner, customer, selection
):
    run, _, calls = customer
    product = make_product(owner)
    receipt = exchange(run, issue(owner, product, 2))
    foreign = exchange(run, issue(owner, product))
    with db() as c:
        foreign_card = c.execute(
            "SELECT card_id FROM grants WHERE card_id NOT IN (SELECT card_id FROM receipt_batch_cards)"
        ).fetchone()[0]
    count = len(calls)
    selected = ["--card", foreign_card] if selection == "foreign" else []
    expected = "not in this receipt" if selection == "foreign" else "Select --card"
    with pytest.raises(ManageError, match=expected):
        run("retry", receipt["receipt_id"], "--reuse", *selected)
    assert [path for _, path, _, _ in calls[count:]] == ["/api/receipt"]
    assert foreign["receipt_id"] != receipt["receipt_id"]
    with db() as c:
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0

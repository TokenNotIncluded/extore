"""Customer batch commands against the local HTTP app and synthetic receipts."""

import json

import pytest
from test_customer_cli import (
    ORIGIN,
    arguments,
    issue,
    make_product,
)
from test_customer_cli import (
    customer as customer,
)

from extore import customer_cli as remote
from extore.db import db
from extore.manage_client import ManageError


def exchange(run, codes, *options):
    return run(
        "exchange",
        "--origin",
        ORIGIN,
        "--codes-stdin",
        *options,
        stdin="\n".join(codes),
    )


def accepted(result):
    return [item for item in result["items"] if item["accepted"]]


def only_group(run, rid):
    groups = run("schema", "--receipt", rid)["groups"]
    assert len(groups) == 1
    return groups[0]


def count_calls(calls, path):
    return sum(actual == path for _, actual, _, _ in calls)


def flow_definition(kind):
    entry = {
        "id": "entry",
        "kind": kind,
        "next": "done",
        "timeout_seconds": 30,
        "timeout_next": "failed",
    }
    if kind == "input":
        entry.update(
            prompt={"zh-CN": "点击开始"},
            question={"zh-CN": "只在开始后显示的问题"},
            start_policy="automatic",
            fields=[
                {
                    "key": "answer",
                    "label": {"zh-CN": "答案"},
                    "type": "text",
                    "required": True,
                }
            ],
        )
    else:
        entry["content"] = {"zh-CN": "准备好的展示步骤"}
    return {
        "version": 1,
        "entry": "entry",
        "nodes": [
            entry,
            {"id": "done", "kind": "end", "state": "succeeded", "result": {}},
            {"id": "failed", "kind": "end", "state": "failed", "retryable": True},
        ],
    }


def test_exchange_keeps_bad_and_duplicate_rows_without_losing_valid_authority(
    owner,
    customer,
):
    run, profile, calls = customer
    codes = issue(owner, make_product(owner), 2)
    result = exchange(run, [codes[0], "NO-SUCH-SYNTHETIC-CODE", codes[0], codes[1]])
    assert result["batch"] is True and result["partial"] is True
    assert [item["status"] for item in result["items"]] == [
        "valid",
        "invalid",
        "duplicate",
        "valid",
    ]
    assert result["items"][2]["duplicate_of"] == 0
    assert result["summary"]["accepted"] == 2
    assert result["summary"]["invalid"] == result["summary"]["duplicate"] == 1
    assert all("card_id" not in item for item in result["items"][1:3])
    assert count_calls(calls, "/api/batch/exchange") == 1
    rid = result["receipt_id"]
    refreshed = run("receipt", rid)
    assert [item["index"] for item in refreshed["items"]] == [0, 3]
    assert refreshed["summary"]["accepted"] == 2
    saved = json.loads(profile.read_text())["receipts"]
    assert len(saved) == 1 and saved[0]["token"]
    assert saved[0]["token"] not in json.dumps(result)
    assert all(code not in json.dumps(result) for code in codes)
    assert all(code not in profile.read_text() for code in codes)


def test_all_bad_batch_creates_no_saved_receipt_and_atomic_mode_stays_legacy(
    owner,
    customer,
):
    run, profile, calls = customer
    result = exchange(run, ["NO-SUCH-SYNTHETIC-CODE"], "--batch")
    assert result["partial"] is True and result["summary"]["accepted"] == 0
    assert "receipt_id" not in result
    assert "token" not in result
    assert not profile.exists() or not json.loads(profile.read_text())["receipts"]
    first, second = make_product(owner), make_product(owner)
    with pytest.raises(ManageError) as error:
        exchange(run, issue(owner, first) + issue(owner, second), "--atomic")
    assert error.value.status == 400
    assert count_calls(calls, "/api/exchange") == 1
    assert count_calls(calls, "/api/batch/exchange") == 1
    with db() as c:
        assert c.execute("SELECT count(*) FROM grants").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM receipt_batches").fetchone()[0] == 0


def test_mixed_products_and_compatible_variants_have_scoped_shared_text_groups(
    owner,
    customer,
):
    run, profile, _ = customer
    first = make_product(
        owner,
        variants=[
            {"id": "standard", "name": "标准"},
            {"id": "premium", "name": "高级"},
        ],
    )
    second = make_product(
        owner,
        parameters=[
            {"key": "topic", "label": {"zh-CN": "主题"}, "type": "text"},
        ],
    )
    codes = []
    for variant in ("standard", "premium"):
        response = owner.post(
            "/api/admin/cards",
            json={
                "product_id": first["id"],
                "variant_id": variant,
                "count": 1,
            },
        )
        assert response.status_code == 200, response.text
        codes += response.json()["codes"]
    result = exchange(run, codes + issue(owner, second))
    rid = result["receipt_id"]
    by_product = {}
    for item in accepted(result):
        by_product.setdefault(item["product"]["id"], []).append(item["card_id"])
    groups = run("schema", "--receipt", rid)["groups"]
    assert len(groups) == 2
    first_group = next(
        group
        for group in groups
        if set(group["card_ids"]) == set(by_product[first["id"]])
    )
    second_group = next(
        group for group in groups if group["card_ids"] == by_product[second["id"]]
    )
    assert [field["key"] for field in first_group["shared_parameters"]] == ["request"]
    assert [field["key"] for field in second_group["shared_parameters"]] == ["topic"]
    assert not first_group["separate_files"] and first_group["flow"] is False
    selected = run("schema", "--receipt", rid, "--group", first_group["group_id"])
    assert selected["group"]["card_ids"] == first_group["card_ids"]
    submitted = run(
        "redeem",
        rid,
        "--group",
        first_group["group_id"],
        "--params-stdin",
        stdin='{"request":"private-shared-request"}',
    )
    assert submitted["submission_summary"] == {"total": 2, "succeeded": 2, "failed": 0}
    with db() as c:
        rows = c.execute("SELECT card_id,params FROM jobs").fetchall()
    assert {row["card_id"] for row in rows} == set(by_product[first["id"]])
    assert all(
        json.loads(row["params"]) == {"request": "private-shared-request"}
        for row in rows
    )
    assert "private-shared-request" not in json.dumps(submitted)
    assert "private-shared-request" not in profile.read_text()


def test_group_uploads_different_paths_and_keeps_each_file_with_its_card(
    owner,
    customer,
    tmp_path,
):
    run, profile, calls = customer
    product = make_product(
        owner,
        parameters=[
            {"key": "request", "label": {"zh-CN": "需求"}, "type": "text"},
            {"key": "source", "label": {"zh-CN": "文件"}, "type": "file"},
        ],
    )
    result = exchange(run, issue(owner, product, 2))
    rid = result["receipt_id"]
    cards = [item["card_id"] for item in accepted(result)]
    group = only_group(run, rid)
    assert [field["key"] for field in group["shared_parameters"]] == ["request"]
    assert [field["key"] for field in group["separate_files"]] == ["source"]
    paths = [tmp_path / "a.txt", tmp_path / "b.txt"]
    contents = [b"private-file-A", b"private-file-B"]
    for path, content in zip(paths, contents, strict=True):
        path.write_bytes(content)
    submitted = run(
        "redeem",
        rid,
        "--group",
        group["group_id"],
        "--params-stdin",
        "--card-file",
        f"{cards[0]}:source={paths[0]}",
        "--card-file",
        f"{cards[1]}:source={paths[1]}",
        stdin='{"request":"shared-description"}',
    )
    assert submitted["submission_summary"]["succeeded"] == 2
    uploads = [body for _, path, _, body in calls if path == "/api/files/upload"]
    assert len(uploads) == 2
    for body, card in zip(uploads, cards, strict=True):
        assert f'name="card_id"\r\n\r\n{card}'.encode() in body
    with db() as c:
        rows = c.execute("SELECT card_id,params FROM jobs").fetchall()
        stored = {row["card_id"]: json.loads(row["params"]) for row in rows}
        files = {row["id"]: dict(row) for row in c.execute("SELECT * FROM job_files")}
    assert stored[cards[0]]["source"] != stored[cards[1]]["source"]
    for card, content in zip(cards, contents, strict=True):
        descriptor = files[stored[card]["source"]]
        assert descriptor["card_id"] == card and descriptor["content"] == content
        assert descriptor["bound"] == 1
        assert len(run("files", rid, "--card", card)["inputs"]) == 1
    assert all(content.decode() not in profile.read_text() for content in contents)


@pytest.mark.parametrize("kind", ["file", "image", "images"])
def test_shared_attachment_ids_are_rejected_before_upload_or_submission(
    owner,
    customer,
    kind,
):
    run, _, calls = customer
    product = make_product(
        owner,
        parameters=[
            {"key": "source", "label": {"zh-CN": "附件"}, "type": kind},
        ],
    )
    result = exchange(run, issue(owner, product, 2))
    rid = result["receipt_id"]
    group = only_group(run, rid)
    value = (
        '["00000000-0000-0000-0000-000000000001"]'
        if kind == "images"
        else "00000000-0000-0000-0000-000000000001"
    )
    with pytest.raises(ManageError) as error:
        run(
            "redeem",
            rid,
            "--group",
            group["group_id"],
            "--params-stdin",
            stdin=json.dumps({"source": value}),
        )
    assert error.value.code == "invalid_input"
    assert count_calls(calls, "/api/files/upload") == 0
    assert count_calls(calls, "/api/batch/redeem") == 0
    with db() as c:
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_items_file_preflights_all_card_paths_before_the_first_upload(
    owner,
    customer,
    tmp_path,
    monkeypatch,
):
    run, _, calls = customer
    monkeypatch.setattr("extore.files.MAX_FILE_BYTES", 4)
    product = make_product(
        owner,
        parameters=[
            {"key": "source", "label": {"zh-CN": "文件"}, "type": "file"},
        ],
    )
    receipt = exchange(run, issue(owner, product, 2))
    rid = receipt["receipt_id"]
    cards = [item["card_id"] for item in accepted(receipt)]
    path = tmp_path / "items.json"
    path.write_text(json.dumps([{"card_id": card, "params": {}} for card in cards]))
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_bytes(b"one")
    second.write_bytes(b"large")
    argv = (
        "redeem",
        rid,
        "--items-file",
        str(path),
        "--card-file",
        f"{cards[0]}:source={first}",
        "--card-file",
        f"{cards[1]}:source={second}",
    )
    with pytest.raises(ManageError) as error:
        run(*argv)
    assert error.value.code == "invalid_upload"
    assert count_calls(calls, "/api/files/upload") == 0
    assert count_calls(calls, "/api/batch/redeem") == 0
    second.write_bytes(b"two")
    result = run(*argv)
    assert result["submission_summary"] == {"total": 2, "succeeded": 2, "failed": 0}
    assert count_calls(calls, "/api/files/upload") == 2
    with db() as c:
        files = {
            row["card_id"]: row["content"]
            for row in c.execute("SELECT * FROM job_files")
        }
    assert files == {cards[0]: b"one", cards[1]: b"two"}


def test_items_file_reports_partial_parameter_failure_and_can_retry_one_card(
    owner,
    customer,
    tmp_path,
):
    run, profile, calls = customer
    product = make_product(
        owner,
        parameters=[
            {"key": "email", "label": {"zh-CN": "邮箱"}, "type": "email"},
        ],
    )
    receipt = exchange(run, issue(owner, product, 2))
    rid = receipt["receipt_id"]
    cards = [item["card_id"] for item in accepted(receipt)]
    source = tmp_path / "items.json"
    source.write_text(
        json.dumps(
            [
                {"card_id": cards[0], "params": {"email": "synthetic@example.test"}},
                {"card_id": cards[1], "params": {"email": "bad-email"}},
            ]
        )
    )
    result = run("redeem", rid, "--items-file", str(source))
    assert [item["status"] for item in result["results"]] == ["submitted", "error"]
    assert result["submission_summary"] == {"total": 2, "succeeded": 1, "failed": 1}
    assert result["results"][1]["http_status"] == 400
    with db() as c:
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    fixed = run(
        "redeem",
        rid,
        "--card",
        cards[1],
        "--params-stdin",
        stdin='{"email":"corrected@example.test"}',
    )
    assert fixed["submission_summary"]["succeeded"] == 1
    assert count_calls(calls, "/api/batch/redeem") == 2
    assert "synthetic@example.test" not in json.dumps(result)
    assert "synthetic@example.test" not in profile.read_text()


@pytest.mark.parametrize("kind", ["input", "display"])
def test_group_prepares_flows_without_timers_then_start_affects_only_selected_card(
    owner,
    customer,
    kind,
):
    run, _, calls = customer
    product = make_product(
        owner,
        parameters=[],
        outputs=[],
        delivery="service",
        task_flow=flow_definition(kind),
    )
    result = exchange(run, issue(owner, product, 2))
    rid = result["receipt_id"]
    cards = [item["card_id"] for item in accepted(result)]
    group = only_group(run, rid)
    assert group["flow"] is True and not group["shared_parameters"]
    prepared = run(
        "redeem", rid, "--group", group["group_id"], "--params-stdin", stdin="{}"
    )
    assert prepared["submission_summary"]["succeeded"] == 2
    assert count_calls(calls, "/api/task-flow/start") == 0
    assert all(item["job"]["state"] == "waiting" for item in accepted(prepared))
    with db() as c:
        initial = [dict(row) for row in c.execute("SELECT * FROM task_flow_runs")]
        assert len(initial) == 2
        assert all(
            row["phase"] == "await_start" and row["deadline"] is None for row in initial
        )
        assert c.execute("SELECT count(*) FROM task_flow_dispatches").fetchone()[0] == 0
    started = run("flow", "start", rid, "--card", cards[0])
    assert started["job"]["task_flow"]["phase"] == kind
    deadline = started["job"]["task_flow"]["deadline"]
    assert isinstance(deadline, (int, float))
    refreshed = run("receipt", rid)
    by_card = {item["card_id"]: item for item in accepted(refreshed)}
    assert by_card[cards[0]]["job"]["task_flow"]["deadline"] == deadline
    assert by_card[cards[1]]["job"]["task_flow"]["phase"] == "await_start"
    assert by_card[cards[1]]["job"]["task_flow"]["deadline"] is None
    assert count_calls(calls, "/api/task-flow/start") == 1


@pytest.mark.parametrize("change", ["accepted", "product", "card_id", "summary"])
def test_malformed_partial_receipt_is_rejected_without_traceback_or_uploads(
    owner,
    customer,
    monkeypatch,
    capsys,
    change,
):
    run, _, calls = customer
    result = exchange(run, issue(owner, make_product(owner), 2))
    rid = result["receipt_id"]
    original = remote.CustomerClient.json

    def malformed(self, origin, method, path, **kwargs):
        value = original(self, origin, method, path, **kwargs)
        if path == "/api/batch/receipt":
            if change == "accepted":
                value["items"][0]["accepted"] = "private-unexpected-value"
            elif change == "product":
                value["items"][0].pop("product")
            elif change == "card_id":
                value["items"][1]["card_id"] = value["items"][0]["card_id"]
            else:
                value["summary"]["accepted"] = True
        return value

    monkeypatch.setattr(remote.CustomerClient, "json", malformed)
    with pytest.raises(ManageError) as error:
        run("schema", "--receipt", rid)
    assert error.value.code == "invalid_response"
    assert count_calls(calls, "/api/files/upload") == 0
    assert count_calls(calls, "/api/batch/redeem") == 0
    monkeypatch.setattr(
        remote, "execute", lambda args: (_ for _ in ()).throw(error.value)
    )
    with pytest.raises(SystemExit):
        remote.run(arguments("receipt", rid))
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err)["code"] == "invalid_response"
    assert (
        "Traceback" not in output.err and "private-unexpected-value" not in output.err
    )

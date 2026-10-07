"""Real signed CLI issuance, two revision rounds and historical file delivery."""

import json
import uuid

import pytest
from test_ai_pipeline_cli_e2e import cli_http as cli_http
from test_ai_pipeline_cli_e2e import consent, field, guards, write_json
from test_customer_cli import make_product

from extore.config import ORIGIN
from extore.manage_client import ManageError


def reveal(cli, receipt, destination, *, revision=None):
    args = ["reveal", receipt, "--output", str(destination)]
    if revision is not None:
        args += ["--revision", str(revision)]
    assert cli.customer(*args)["ok"]
    return json.loads(destination.read_text())


def complete(cli, item, destination, text):
    report = destination.with_suffix(".txt")
    report.write_text(text, encoding="utf-8")
    output = write_json(destination, {"notes": text})
    assert cli.manage(
        "complete",
        item["job"]["id"],
        "--product",
        item["product_id"],
        *guards(item),
        "--output-file",
        output,
        "--file",
        f"report={report}",
    )["ok"]
    return report.read_bytes()


def download(cli, receipt, file_id, destination):
    assert cli.customer(
        "download", receipt, "--file-id", file_id, "--output", str(destination)
    )["ok"]
    return destination.read_bytes()


def test_cli_card_attributes_revisions_preserve_old_files_and_retry_budget(
    owner, cli_http, tmp_path
):
    cli = cli_http
    product = make_product(
        owner,
        max_attempts=2,
        parameters=[field("request", "textarea")],
        outputs=[field("report", "file"), field("notes", "textarea")],
        revision_policy={"attribute_key": "edit_passes", "label": {"en": "Edits"}},
        variants=[
            {
                "id": "default",
                "name": "Enhanced",
                "price": "50",
                "attributes": {"edit_passes": 1, "format": "document"},
            }
        ],
    )
    pid = product["id"]
    overrides = write_json(
        tmp_path / "overrides.json", {"edit_passes": 2, "theme": "midnight"}
    )
    issued = tmp_path / "private-codes.json"
    assert cli.admin(
        "cards",
        "issue",
        "--product",
        pid,
        "--count",
        "1",
        "--attributes-file",
        overrides,
        "--output",
        str(issued),
    )["ok"]
    code = json.loads(issued.read_text())["codes"][0]
    verified = cli.customer("exchange", "--origin", ORIGIN, "--codes-stdin", stdin=code)
    receipt = verified["receipt_id"]
    assert verified["card_attributes"] == {
        "edit_passes": 2,
        "format": "document",
        "theme": "midnight",
    }
    assert verified["entitlements"]["remaining"] == 2
    task = cli.customer(
        "redeem", receipt, "--params-stdin", stdin='{"request":"Original brief"}'
    )["job"]
    consent(owner, cli, [product])
    first = cli.manage("next", "--product", pid, "--wait", "0")["items"][0]
    assert first["job"]["id"] == task["id"]
    assert first["execution"]["card_attributes"]["edit_passes"] == 2
    original = complete(cli, first, tmp_path / "round0.json", "Original result")
    original_delivery = reveal(cli, receipt, tmp_path / "delivery0.json")
    original_file = original_delivery["output"]["report"]

    advice = tmp_path / "advice.txt"
    advice.write_text("Use a clearer title", encoding="utf-8")
    request_id = str(uuid.uuid4())
    revise_args = (
        "revise",
        receipt,
        "--message-file",
        str(advice),
        "--request-id",
        request_id,
        "--expected-revision",
        "0",
    )
    assert cli.customer(*revise_args)["ok"]
    assert cli.customer(*revise_args)["ok"]  # A lost response must not charge twice.
    status = cli.customer("receipt", receipt)
    assert status["entitlements"]["used"] == 1
    assert status["entitlements"]["remaining"] == 1
    assert status["job"]["state"] == "queued"
    assert status["job"]["revision"]["current"] == 1
    assert download(cli, receipt, original_file, tmp_path / "old-while-queued.txt") == (
        original
    )
    editing = cli.manage("next", "--product", pid, "--wait", "0")["items"][0]
    assert editing["execution"]["revision"]["message"] == advice.read_text()
    assert editing["job"]["attempt"] > first["job"]["attempt"]
    assert cli.manage(
        "fail",
        editing["job"]["id"],
        "--product",
        pid,
        *guards(editing),
        "--retryable",
        "--message",
        "Synthetic temporary processor outage",
    )["ok"]
    failed = cli.customer("receipt", receipt)
    assert failed["job"]["can_retry"]
    assert failed["entitlements"]["remaining"] == 1
    assert cli.customer(
        "retry", receipt, "--params-stdin", stdin='{"request":"Original brief"}'
    )["ok"]
    retry = cli.manage("next", "--product", pid, "--wait", "0")["items"][0]
    assert retry["execution"]["revision"]["current"] == 1
    assert retry["execution"]["entitlements"]["remaining"] == 1
    changed = complete(cli, retry, tmp_path / "round1.json", "Clear title result")
    changed_delivery = reveal(cli, receipt, tmp_path / "delivery1.json")
    changed_file = changed_delivery["output"]["report"]
    assert download(cli, receipt, changed_file, tmp_path / "changed.txt") == changed
    assert (
        reveal(cli, receipt, tmp_path / "history0.json", revision=0)["output"]
        == (original_delivery["output"])
    )

    advice.write_text("Make the conclusion shorter", encoding="utf-8")
    assert cli.customer(
        "revise",
        receipt,
        "--message-file",
        str(advice),
        "--request-id",
        str(uuid.uuid4()),
        "--expected-revision",
        "1",
    )["ok"]
    assert download(cli, receipt, original_file, tmp_path / "original-round2.txt") == (
        original
    )
    assert download(cli, receipt, changed_file, tmp_path / "changed-round2.txt") == (
        changed
    )
    final = cli.manage("next", "--product", pid, "--wait", "0")["items"][0]
    assert final["execution"]["revision"]["current"] == 2
    complete(cli, final, tmp_path / "round2.json", "Shorter conclusion result")
    done = cli.customer("receipt", receipt)
    assert done["job"]["state"] == "succeeded"
    assert done["entitlements"]["used"] == 2
    assert done["entitlements"]["remaining"] == 0
    assert not done["entitlements"]["can_request"]
    with pytest.raises(ManageError):
        cli.customer("revise", receipt, "--message-file", str(advice))
    assert cli.manage("jobs", "--product", pid)["jobs"] == []
    assert cli.customer("destroy", receipt, "--confirm")["ok"]
    for index, file_id in enumerate((original_file, changed_file)):
        with pytest.raises(ManageError):
            download(cli, receipt, file_id, tmp_path / f"destroyed-{index}.txt")
    with pytest.raises(ManageError):
        reveal(cli, receipt, tmp_path / "destroyed-history.json", revision=0)

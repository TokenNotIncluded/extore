"""Batch CLI recovery, request limits and signed-route safety regressions."""

import io
import json

import httpx
import pytest
from test_customer_cli import ORIGIN, arguments, issue, make_product
from test_customer_cli import customer as customer
from test_customer_flow_cli import routed_fixture

from extore import customer_cli as remote
from extore.db import db
from extore.manage_client import ManageError


def exchange(run, codes):
    return run("exchange", "--origin", ORIGIN, "--codes-stdin", stdin="\n".join(codes))


def test_unknown_item_does_not_submit_foreign_card_or_discard_valid_item(
    owner, customer, tmp_path
):
    run, _, calls = customer
    product = make_product(owner)
    receipt = exchange(run, issue(owner, product, 2))
    card = receipt["items"][0]["card_id"]
    foreign = issue(owner, product)
    with db() as c:
        foreign_id = c.execute(
            "SELECT id FROM cards ORDER BY created DESC LIMIT 1"
        ).fetchone()[0]
    source = tmp_path / "items.json"
    source.write_text(
        json.dumps(
            [
                {"card_id": foreign_id, "params": {"request": "foreign-private-input"}},
                {"card_id": card, "params": {"request": "valid-private-input"}},
            ]
        )
    )
    result = run("redeem", receipt["receipt_id"], "--items-file", str(source))
    assert result["submission_summary"] == {"total": 2, "succeeded": 1, "failed": 1}
    assert [row["status"] for row in result["results"]] == ["error", "submitted"]
    assert "card_id" not in result["results"][0]
    bodies = [
        json.loads(body) for _, path, _, body in calls if path == "/api/batch/redeem"
    ]
    assert len(bodies) == 1 and [item["card_id"] for item in bodies[0]["items"]] == [
        card
    ]
    with db() as c:
        assert c.execute("SELECT card_id FROM jobs").fetchone()[0] == card
    assert foreign[0] not in json.dumps(result)
    assert "foreign-private-input" not in json.dumps(result)


def test_failed_submission_reuses_only_its_own_cached_attachment(
    owner, customer, tmp_path
):
    run, _, calls = customer
    product = make_product(
        owner,
        parameters=[
            {"key": "email", "label": {"zh-CN": "邮箱"}, "type": "email"},
            {"key": "source", "label": {"zh-CN": "附件"}, "type": "file"},
        ],
    )
    receipt = exchange(run, issue(owner, product, 2))
    rid = receipt["receipt_id"]
    card, untouched = [item["card_id"] for item in receipt["items"]]
    source = tmp_path / "input.txt"
    source.write_text("synthetic-private-file")
    failed = run(
        "redeem",
        rid,
        "--card",
        card,
        "--params-stdin",
        "--file",
        f"source={source}",
        stdin='{"email":"bad-email"}',
    )
    assert failed["submission_summary"]["failed"] == 1
    assert sum(path == "/api/files/upload" for _, path, _, _ in calls) == 1
    fixed = run(
        "redeem",
        rid,
        "--card",
        card,
        "--params-stdin",
        stdin='{"email":"fixed@example.test"}',
    )
    assert fixed["submission_summary"]["succeeded"] == 1
    assert sum(path == "/api/files/upload" for _, path, _, _ in calls) == 1
    other = run(
        "redeem",
        rid,
        "--card",
        untouched,
        "--params-stdin",
        stdin='{"email":"other@example.test"}',
    )
    assert other["submission_summary"]["failed"] == 1
    with db() as c:
        row = c.execute("SELECT card_id,params FROM jobs").fetchone()
        assert row["card_id"] == card
        file_id = json.loads(row["params"])["source"]
        assert (
            c.execute(
                "SELECT card_id,bound FROM job_files WHERE id=?", (file_id,)
            ).fetchone()["bound"]
            == 1
        )
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_large_shared_text_is_split_below_body_limit_without_losing_cards(
    owner, customer
):
    run, _, calls = customer
    product = make_product(owner)
    receipt = exchange(run, issue(owner, product, 30))
    group = run("schema", "--receipt", receipt["receipt_id"])["groups"][0]
    result = run(
        "redeem",
        receipt["receipt_id"],
        "--group",
        group["group_id"],
        "--params-stdin",
        stdin=json.dumps({"request": "x" * 10000}),
    )
    assert result["submission_summary"] == {"total": 30, "succeeded": 30, "failed": 0}
    assert [row["index"] for row in result["results"]] == list(range(30))
    requests = [body for _, path, _, body in calls if path == "/api/batch/redeem"]
    assert len(requests) == 2 and all(len(body) <= 250000 for body in requests)
    with db() as c:
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 30


def test_revocation_race_preserves_other_success_and_prior_authorized_failure(
    owner, customer, monkeypatch
):
    run, _, _ = customer
    receipt = exchange(run, issue(owner, make_product(owner), 2))
    card, revoked = [item["card_id"] for item in receipt["items"]]
    group = run("schema", "--receipt", receipt["receipt_id"])["groups"][0]
    original = remote.CustomerClient.json

    def revoke_before_submit(client, origin, method, path, **kwargs):
        if path == "/api/batch/redeem":
            with db() as c:
                c.execute("UPDATE cards SET state='revoked' WHERE id=?", (revoked,))
        return original(client, origin, method, path, **kwargs)

    monkeypatch.setattr(remote.CustomerClient, "json", revoke_before_submit)
    result = run(
        "redeem",
        receipt["receipt_id"],
        "--group",
        group["group_id"],
        "--params-stdin",
        stdin='{"request":"synthetic"}',
    )
    assert result["submission_summary"] == {"total": 2, "succeeded": 1, "failed": 1}
    assert [row["status"] for row in result["results"]] == ["submitted", "error"]
    assert result["results"][0]["card_id"] == card
    assert result["results"][1]["card_id"] == revoked
    assert (
        result["items"][1]["accepted"] is False and "card_id" not in result["items"][1]
    )


def test_incomplete_partial_item_is_cleanly_rejected(owner):
    codes = issue(owner, make_product(owner), 2)
    value = owner.post("/api/batch/exchange", json={"code": "\n".join(codes)}).json()
    value["items"][0].pop("http_status")
    with pytest.raises(ManageError) as error:
        remote._summary_receipt(value)
    assert error.value.code == "invalid_response"


@pytest.mark.parametrize("separator", [":", "/", "-", "X"])
def test_damaged_route_header_never_posts_to_entry_server(
    tmp_path, monkeypatch, separator
):
    _, wrapped = routed_fixture()
    code = wrapped.replace(".", separator, 1)
    calls = []
    monkeypatch.setattr("sys.stdin", io.StringIO(code))

    def relay(request):
        calls.append(request)
        return httpx.Response(500)

    with pytest.raises(ManageError) as error:
        remote.execute(
            arguments(
                "exchange",
                "--origin",
                "https://a.example",
                "--codes-stdin",
                profile=tmp_path / "private" / "customer.json",
            ),
            transport=httpx.MockTransport(relay),
        )
    assert error.value.code == "invalid_route" and not calls
    assert code not in str(error.value)

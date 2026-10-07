"""Merchant API to customer receipt, using the real offline processor sandbox."""

import asyncio
import json

import pytest
from test_processor_profiles import binding, create_product, issue, merchant, submit

from extore.db import db
from extore.worker import job_once


def complete(client, token):
    assert asyncio.run(job_once()) is True
    response = client.post("/api/receipt", json={"token": token})
    assert response.status_code == 200, response.text
    task = response.json()["job"]
    assert task["state"] == "succeeded", task
    assert task["progress"] == 100
    assert len(task["steps"]) >= 2
    assert all(step["done"] for step in task["steps"])
    assert task["completed_steps"] == [step["id"] for step in task["steps"]]
    revealed = client.post("/api/receipt/reveal", json={"token": token})
    assert revealed.status_code == 200, revealed.text
    return revealed.json()["output"]


def exchange(client, code):
    response = client.post("/api/exchange", json={"code": code})
    assert response.status_code == 200, response.text
    return response.json()["token"]


def card_state(product_id):
    with db() as c:
        cards = [
            dict(row)
            for row in c.execute(
                "SELECT id,state FROM cards WHERE product_id=?", (product_id,)
            )
        ]
        jobs = [
            dict(row)
            for row in c.execute(
                "SELECT id,state,attempt,params FROM jobs WHERE product_id=?",
                (product_id,),
            )
        ]
    return cards, jobs


def assert_fields(product, inputs, outputs):
    # The merchant creates only a processor/configuration; the code supplies
    # the customer form and the receipt schema without a second manual setup.
    assert [field["key"] for field in product["parameters"]] == inputs
    assert [field["key"] for field in product["outputs"]] == outputs
    assert product["delivery"] == "content"


def test_list_cleanup_preserves_untrimmed_edges_and_reports_actual_counts(owner):
    product = create_product(
        owner,
        processor_id="text_cleanup",
        configuration={"trim_lines": "no", "deduplicate": "exact"},
    )
    assert_fields(product, ["text"], ["cleaned_text", "report"])
    token, _ = submit(
        owner,
        issue(owner, product["id"]),
        {"text": "  Ada  \r\nAda\r\n  Ada  \r\n \r\n"},
    )
    output = complete(owner, token)
    assert output["cleaned_text"] == "  Ada  \nAda\n"
    assert "Input lines: 4" in output["report"]
    assert "Retained lines: 2" in output["report"]
    assert "Blank lines removed: 1" in output["report"]
    assert "Duplicate lines removed: 1" in output["report"]


def test_csv_experiment_report_delivers_real_group_statistics(owner):
    marker = "PRIVATE-INVALID-CELL-728164"
    product = create_product(
        owner,
        processor_id="csv_summary",
        configuration={"invalid_policy": "skip"},
    )
    assert_fields(
        product,
        ["csv_text", "value_column", "group_column"],
        ["report", "summary"],
    )
    token, _ = submit(
        owner,
        issue(owner, product["id"]),
        {
            "csv_text": f"group,value\nA,1\nA,3\nB,2\nB,4\nA,\nB,{marker}\n",
            "value_column": "value",
            "group_column": "group",
        },
    )
    output = complete(owner, token)
    summary = json.loads(output["summary"])
    assert summary["schema"] == "extore.csv-summary.v1"
    assert summary["data_rows"] == 6
    assert summary["overall"] == {
        "rows": 6,
        "valid_count": 4,
        "missing_count": 1,
        "invalid_count": 1,
        "mean": "2.5",
        "minimum": "1",
        "maximum": "4",
        "population_standard_deviation": "1.11803398875",
        "sample_standard_deviation": "1.29099444874",
    }
    assert {
        group["value"]: group["statistics"]["mean"] for group in summary["groups"]
    } == {
        "A": "2",
        "B": "3",
    }
    assert "| 全部 / Overall | 4 | 1 | 1 | 2.5 |" in output["report"]
    assert "样本标准差 / Sample SD" in output["report"]
    assert "No external retrieval" in output["report"]
    assert marker not in json.dumps(output)


def test_json_formatter_preserves_decimal_tokens_and_delivers_structure_counts(owner):
    product = create_product(
        owner,
        processor_id="json_formatter",
        configuration={"indent": "compact", "sort_keys": "yes"},
    )
    assert_fields(product, ["json_text"], ["formatted_json", "report"])
    token, _ = submit(
        owner,
        issue(owner, product["id"]),
        {
            "json_text": (
                '{"z":9007199254740993,"money":1.2300,"x":1e+2,'
                '"title":"中文","items":[true,null]}'
            )
        },
    )
    output = complete(owner, token)
    assert output["formatted_json"] == (
        '{"items":[true,null],"money":1.2300,"title":"中文",'
        '"x":1e+2,"z":9007199254740993}'
    )
    assert "Type: object" in output["report"]
    assert "Values: 8" in output["report"]
    assert "Object keys: 5" in output["report"]
    assert "Array items: 2" in output["report"]
    assert "Container depth: 2" in output["report"]


def test_document_template_delivers_shop_text_without_reinterpreting_customer_body(
    owner,
):
    product = create_product(
        owner,
        processor_id="document_template",
        configuration={
            "template": "受理单：$title\n称呼：$name\n$body\n固定价格 $$25",
            "output_format": "plain",
        },
    )
    assert_fields(product, ["title", "name", "body"], ["content", "format"])
    token, _ = submit(
        owner,
        issue(owner, product["id"]),
        {"title": "交付说明", "name": "Ada", "body": "\n  $title 保留为原文\n\n"},
    )
    assert complete(owner, token) == {
        "content": "受理单：交付说明\n称呼：Ada\n\n  $title 保留为原文\n\n\n固定价格 $25",
        "format": "plain",
    }


def test_all_blank_cleanup_is_a_successful_empty_result_with_a_report(owner):
    product = create_product(owner, processor_id="text_cleanup", configuration={})
    text_field = product["parameters"][0]
    output_field = product["outputs"][0]
    assert text_field["required"] is False
    assert output_field["required"] is False
    token, _ = submit(owner, issue(owner, product["id"]), {"text": " \r\n\t\r\n"})
    output = complete(owner, token)
    assert output["cleaned_text"] == ""
    assert "Input lines: 2" in output["report"]
    assert "Retained lines: 0" in output["report"]
    assert "Blank lines removed: 2" in output["report"]


def test_invalid_json_never_consumes_card_and_customer_can_correct_same_receipt(owner):
    marker = "PRIVATE-JSON-INPUT-729185"
    product = create_product(owner, processor_id="json_formatter", configuration={})
    token = exchange(owner, issue(owner, product["id"]))
    before = card_state(product["id"])
    assert before[0][0]["state"] == "ready" and before[1] == []
    for text in (
        f'{{"secret":"{marker}","x":1,"x":2}}',
        f'{{"secret":"{marker}","number":NaN}}',
        f'{{"secret":"{marker}",}}',
        f'{{"secret":"{marker}"}} trailing',
        f'{{"secret":"{marker}","unicode":"\\ud800"}}',
    ):
        response = owner.post(
            "/api/redeem", json={"token": token, "params": {"json_text": text}}
        )
        assert response.status_code == 400, response.text
        assert marker not in response.text
        assert card_state(product["id"]) == before
        assert owner.post("/api/receipt", json={"token": token}).json()["job"] is None
    corrected = owner.post(
        "/api/redeem", json={"token": token, "params": {"json_text": '{"fixed":true}'}}
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["attempt"] == 1
    assert json.loads(complete(owner, token)["formatted_json"]) == {"fixed": True}


def test_malformed_csv_is_rejected_before_queue_and_corrected_input_can_be_processed(
    owner,
):
    marker = "PRIVATE-CSV-INPUT-973465"
    product = create_product(owner, processor_id="csv_summary", configuration={})
    token = exchange(owner, issue(owner, product["id"]))
    before = card_state(product["id"])
    response = owner.post(
        "/api/redeem",
        json={
            "token": token,
            "params": {"csv_text": f"value\n1,{marker}", "value_column": "value"},
        },
    )
    assert response.status_code == 400, response.text
    assert marker not in response.text and card_state(product["id"]) == before
    corrected = owner.post(
        "/api/redeem",
        json={
            "token": token,
            "params": {"csv_text": "value\n1\n3", "value_column": "value"},
        },
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["attempt"] == 1
    summary = json.loads(complete(owner, token)["summary"])
    assert summary["overall"]["mean"] == "2"


@pytest.mark.parametrize(
    "processor_id,field",
    [
        ("text_cleanup", "deduplicate"),
        ("csv_summary", "invalid_policy"),
        ("json_formatter", "indent"),
        ("document_template", "output_format"),
    ],
)
def test_invalid_configuration_choice_does_not_mutate_binding_or_echo_value(
    owner, processor_id, field
):
    product = create_product(owner, processor_id=processor_id, configuration={})
    profile = binding(owner, product["id"])
    before = owner.get(f"/api/admin/processor-profiles/{profile['id']}").json()
    marker = "PRIVATE-INVALID-SETTING-562190"
    response = owner.put(
        f"/api/admin/processor-profiles/{profile['id']}",
        json={"configuration": {field: marker}},
    )
    assert response.status_code == 422, response.text
    assert marker not in response.text
    assert owner.get(f"/api/admin/processor-profiles/{profile['id']}").json() == before
    assert binding(owner, product["id"])["bound_revision"] == 1


def test_issued_cleanup_card_keeps_config_until_next_explicit_binding(owner):
    product = create_product(
        owner,
        processor_id="text_cleanup",
        configuration={"deduplicate": "none"},
    )
    old_code = issue(owner, product["id"])
    profile = binding(owner, product["id"])
    updated = owner.put(
        f"/api/admin/processor-profiles/{profile['id']}",
        json={"configuration": {"deduplicate": "casefold"}},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == 2
    assert binding(owner, product["id"])["bound_revision"] == 1
    rebound = owner.put(
        f"/api/admin/processor-profiles/bindings/{product['id']}",
        json={"profile_id": profile["id"]},
    )
    assert rebound.status_code == 200, rebound.text
    new_code = issue(owner, product["id"])
    old_token, _ = submit(owner, old_code, {"text": "Alpha\nalpha\n"})
    old_output = complete(owner, old_token)
    new_token, _ = submit(owner, new_code, {"text": "Alpha\nalpha\n"})
    new_output = complete(owner, new_token)
    assert old_output["cleaned_text"] == "Alpha\nalpha\n"
    assert "Duplicate lines removed: 0" in old_output["report"]
    assert new_output["cleaned_text"] == "Alpha\n"
    assert "Duplicate lines removed: 1" in new_output["report"]


def test_same_cleanup_processor_isolated_by_shop_and_customer_cannot_override_settings(
    owner,
):
    a, shop_a = merchant()
    b, shop_b = merchant()
    product_a = create_product(
        a, processor_id="text_cleanup", configuration={"deduplicate": "none"}
    )
    product_b = create_product(
        b, processor_id="text_cleanup", configuration={"deduplicate": "casefold"}
    )
    profile_a = binding(a, product_a["id"])
    profile_b = binding(b, product_b["id"])
    assert profile_a["shop_id"] == shop_a and profile_b["shop_id"] == shop_b
    assert b.get(f"/api/admin/processor-profiles/{profile_a['id']}").status_code == 404
    token_b = exchange(b, issue(b, product_b["id"]))
    before = card_state(product_b["id"])
    marker = "PRIVATE-CUSTOMER-OVERRIDE-851209"
    attack = b.post(
        "/api/redeem",
        json={
            "token": token_b,
            "params": {"text": "Beta\nbeta\n", "deduplicate": marker},
        },
    )
    assert attack.status_code == 400, attack.text
    assert marker not in attack.text and card_state(product_b["id"]) == before
    token_a, _ = submit(a, issue(a, product_a["id"]), {"text": "Beta\nbeta\n"})
    assert complete(a, token_a)["cleaned_text"] == "Beta\nbeta\n"
    corrected = b.post(
        "/api/redeem", json={"token": token_b, "params": {"text": "Beta\nbeta\n"}}
    )
    assert corrected.status_code == 200, corrected.text
    assert complete(b, token_b)["cleaned_text"] == "Beta\n"

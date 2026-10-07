"""The public schema describes syntax without opening merchant storage."""

from importlib import import_module

import pytest
from jsonschema import Draft202012Validator

from extore.db import db
from extore.task_flow_definition import validate_definition


def test_schema_is_public_machine_readable_and_does_not_read_database(
    client, monkeypatch
):
    app_module = import_module("extore.app")

    def forbidden_database(*args, **kwargs):
        raise AssertionError("Schema export must not access database")

    monkeypatch.setattr(app_module, "db", forbidden_database)
    response = client.get("/api/task-flows/schema")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/schema+json")
    assert response.headers["x-content-type-options"] == "nosniff"
    schema = response.json()
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == "https://extore.lmm.best/api/task-flows/schema"
    Draft202012Validator(schema).validate(None)


def draft():
    field = {"key": "content", "type": "textarea", "label": {"en": "Content"}}
    return {
        "product": {"mode": "manual", "outputs": [field]},
        "definition": {
            "version": 1,
            "entry": "input",
            "nodes": [
                {"id": "input", "kind": "input", "fields": [field], "next": "process"},
                {
                    "id": "process",
                    "kind": "process",
                    "inputs": {"content": {"node": "input", "field": "content"}},
                    "outputs": [field],
                    "next": "done",
                    "failure_next": "failed",
                    "timeout_next": "failed",
                },
                {
                    "id": "done",
                    "kind": "end",
                    "state": "succeeded",
                    "result": {"content": {"node": "process", "field": "content"}},
                },
                {"id": "failed", "kind": "end", "state": "failed", "retryable": True},
            ],
        },
    }


def business_rows():
    with db() as c:
        return {
            table: [tuple(row) for row in c.execute("SELECT * FROM " + table)]
            for table in (
                "products",
                "cards",
                "jobs",
                "card_task_flows",
                "task_flow_runs",
                "task_flow_steps",
                "processor_profiles",
                "events",
                "outbox",
            )
        }


@pytest.mark.parametrize("role", ["admin", "manage"])
def test_draft_validation_uses_issuance_rules_without_business_mutation(owner, role):
    payload = draft()
    before = business_rows()
    response = owner.post(f"/api/{role}/task-flows/validate", json=payload)
    assert response.status_code == 200
    value = response.json()
    assert value["definition"] == validate_definition(
        payload["definition"], payload["product"]
    )
    assert value["summary"]["node_count"] == 4
    assert value["summary"]["transition_limit"] == 256
    assert business_rows() == before


def test_draft_validation_reports_invalid_reference_without_echoing_text(owner):
    payload = draft()
    payload["definition"]["nodes"][1]["inputs"]["content"]["node"] = "missing"
    payload["definition"]["nodes"][0]["label"] = {"en": "PRIVATE_DRAFT_CONTENT"}
    before = business_rows()
    response = owner.post("/api/admin/task-flows/validate", json=payload)
    assert response.status_code == 422
    assert "PRIVATE_DRAFT_CONTENT" not in response.text
    assert business_rows() == before


def test_draft_validation_needs_session_and_configuration_permission(owner):
    product = owner.post(
        "/api/admin/products", json={"name": "Synthetic validation"}
    ).json()
    link = owner.post(
        "/api/admin/staff",
        json={
            "product_id": product["id"],
            "name": "Queue viewer",
            "permissions": ["queue.view"],
        },
    ).json()
    owner.cookies.clear()
    assert owner.post("/api/admin/task-flows/validate", json=draft()).status_code == 401
    assert (
        owner.post(
            "/api/staff/login", json={"token": link["url"].split("#")[1]}
        ).status_code
        == 200
    )
    assert (
        owner.post("/api/manage/task-flows/validate", json=draft()).status_code == 403
    )


def test_draft_validation_rejects_saved_identity_or_credentials(owner):
    payload = draft()
    payload["product"]["webhook_secret"] = "PRIVATE_CREDENTIAL"
    response = owner.post("/api/admin/task-flows/validate", json=payload)
    assert response.status_code == 422
    assert "PRIVATE_CREDENTIAL" not in response.text

import argparse
import io
import json
import stat
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from extore import manage_client as remote
from extore.app import app
from extore.db import db
from extore.models import LINK_PERMISSIONS

ORIGIN = "http://localhost:8000"


def arguments(profile, *argv):
    parser = argparse.ArgumentParser()
    remote.add_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(["manage", "--profile", str(profile), *argv])


@pytest.fixture
def manager(owner, tmp_path, monkeypatch):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    profile = directory / "cli.json"
    secret = "synthetic-signing-secret-for-manager-commands"
    response = owner.post(
        "/api/admin/products",
        json={
            "name": "CLI complete product",
            "mode": "manual",
            "parameters": [],
            "webhook_url": "https://hooks.example/events",
            "webhook_secret": secret,
        },
    )
    assert response.status_code == 200, response.text
    product = response.json()["id"]
    response = owner.post(
        "/api/admin/staff",
        json={
            "product_id": product,
            "name": "CLI product manager",
            "permissions": list(LINK_PERMISSIONS),
        },
    )
    assert response.status_code == 200, response.text
    link = response.json()["url"]
    calls = []
    with TestClient(app, base_url=ORIGIN) as api:

        def real_api(request):
            calls.append(
                (
                    request.method,
                    request.url.path,
                    dict(request.url.params),
                    request.read(),
                )
            )
            response = api.request(
                request.method,
                str(request.url),
                headers=dict(request.headers),
                content=request.content,
            )
            return httpx.Response(
                response.status_code,
                headers=dict(response.headers),
                content=response.content,
            )

        transport = httpx.MockTransport(real_api)

        def run(*argv):
            return remote.execute(arguments(profile, *argv), transport=transport)

        monkeypatch.setattr(sys, "stdin", io.StringIO(link))
        assert run("login", "--link-stdin")["ok"]
        yield {
            "run": run,
            "pid": product,
            "profile": profile,
            "calls": calls,
            "secret": secret,
            "link": link,
            "owner": owner,
            "transport": transport,
        }


def run(manager, *argv):
    return manager["run"](*argv, "--product", manager["pid"])


def patch_input(manager, monkeypatch, value):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(value)))


def test_product_get_and_update_keep_server_secrets_out_of_output(manager, monkeypatch):
    result = run(manager, "product", "get", "--detail")
    assert result["product"]["webhook_secret"] == "[redacted]"
    assert manager["secret"] not in json.dumps(result)
    patch_input(
        manager,
        monkeypatch,
        {
            "name": "Renamed through CLI",
            "description": "Changed without clearing signing configuration",
        },
    )
    result = run(manager, "product", "update", "--json-stdin", "--detail")
    assert result["product"]["name"] == "Renamed through CLI"
    owner = manager["owner"]
    product = next(
        p for p in owner.get("/api/admin/products").json() if p["id"] == manager["pid"]
    )
    assert (
        product["webhook_secret"] == manager["secret"]
        and product["webhook_url"] == "https://hooks.example/events"
    )
    assert manager["secret"] not in json.dumps(result)


def test_schema_and_prompt_include_custom_io_and_tiers_without_auth_secrets(manager):
    schema = run(manager, "product", "schema")
    assert schema["schema"]["id"] == manager["pid"]
    assert schema["schema"]["outputs"][0]["key"] == "content"
    prompt = run(manager, "product", "prompt")
    assert "variants" in prompt["prompt"] and ORIGIN + "/" in prompt["prompt"]
    assert (
        manager["secret"] not in prompt["prompt"]
        and manager["link"] not in prompt["prompt"]
    )


def test_card_issuance_is_private_inventory_is_scoped_and_batch_filter_is_real(
    manager, tmp_path
):
    target = tmp_path / "issued-codes.json"
    result = run(
        manager,
        "cards",
        "issue",
        "--count",
        "2",
        "--label",
        "Test batch",
        "--output",
        str(target),
    )
    exported = json.loads(target.read_text())
    assert len(exported["codes"]) == 2 and result["count"] == 2
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not any(code in json.dumps(result) for code in exported["codes"])
    listing = run(manager, "cards", "list")
    assert len(listing["cards"]) == 2
    inventory = run(manager, "cards", "inventory", "--status", "unused", "--limit", "1")
    assert inventory["total"] == 2 and len(inventory["items"]) == 1
    assert "digest" not in json.dumps(inventory)
    batch = run(manager, "cards", "batch", "--batch", result["batch_id"])
    assert batch["total"] == 2
    stats = run(manager, "cards", "stats")
    assert stats["summary"]["total"] == 2 and stats["summary"]["remaining"] == 2
    assert stats["products"][0]["product_id"] == manager["pid"]
    assert stats["products"][0]["variants"][0]["summary"]["total"] == 2
    card = inventory["items"][0]["id"]
    assert run(manager, "cards", "revoke", card)["ok"]
    history = run(manager, "cards", "history", card)
    assert history["card"]["status"] == "revoked"
    assert any(item["type"] == "card.revoked" for item in history["timeline"])


def test_secret_exports_reserve_destination_before_side_effect_and_never_overwrite(
    manager, tmp_path
):
    target = tmp_path / "existing.json"
    target.write_text("must stay")
    before = len(
        [
            call
            for call in manager["calls"]
            if call[0] == "POST" and call[1] == "/api/manage/cards"
        ]
    )
    with pytest.raises(remote.ManageError, match="already exists"):
        run(manager, "cards", "issue", "--output", str(target))
    assert target.read_text() == "must stay"
    assert (
        len(
            [
                call
                for call in manager["calls"]
                if call[0] == "POST" and call[1] == "/api/manage/cards"
            ]
        )
        == before
    )


def test_delegated_link_create_list_revoke_keeps_url_private(manager, monkeypatch):
    patch_input(
        manager,
        monkeypatch,
        {"name": "Child agent", "permissions": ["queue.view", "queue.process"]},
    )
    result = run(manager, "links", "create", "--json-stdin")
    saved = json.loads(Path(result["output"]).read_text())
    assert saved["url"].startswith(ORIGIN + "/staff#")
    assert saved["url"] not in json.dumps(result)
    assert stat.S_IMODE(Path(result["output"]).stat().st_mode) == 0o600
    links = run(manager, "links", "list")
    assert [link["id"] for link in links["links"]] == [result["link"]["id"]]
    assert run(manager, "links", "revoke", result["link"]["id"])["ok"]
    assert run(manager, "links", "list")["links"] == []
    assert run(manager, "links", "list", "--view", "history")["links"][0]["revoked"]


def test_events_named_retry_reactivates_only_dead_webhook_attempt(manager):
    issued = run(manager, "cards", "issue")
    code = json.loads(Path(issued["output"]).read_text())["codes"][0]
    owner = manager["owner"]
    token = owner.post("/api/exchange", json={"code": code}).json()["token"]
    response = owner.post("/api/redeem", json={"token": token, "params": {}})
    assert response.status_code == 200, response.text
    events = run(manager, "events", "list")
    event = next(
        item for item in events["events"] if item["type"] == "redemption.requested"
    )
    with db() as c:
        c.execute(
            "UPDATE outbox SET state='dead',attempts=9,error='synthetic failure' WHERE id=?",
            (event["id"],),
        )
    assert run(manager, "events", "retry", event["id"])["ok"]
    with db() as c:
        result = c.execute(
            "SELECT state,attempts FROM outbox WHERE id=?", (event["id"],)
        ).fetchone()
        assert result["state"] == "pending" and result["attempts"] == 0


def test_sessions_devices_and_audit_are_named_and_revocable(manager, monkeypatch):
    patch_input(
        manager, monkeypatch, {"name": "Audited child", "permissions": ["queue.view"]}
    )
    child = run(manager, "links", "create", "--json-stdin")
    link = json.loads(Path(child["output"]).read_text())["url"]
    monkeypatch.setattr(sys, "stdin", io.StringIO(link))
    login = manager["run"]("login", "--link-stdin")
    childdevice = login["grant"]["id"]
    sessions = run(manager, "sessions", "list")["sessions"]
    assert len(sessions) == 2 and all(item["channel"] == "cli" for item in sessions)
    childsession = next(
        item["id"] for item in sessions if item["link_name"] == "Audited child"
    )
    assert run(manager, "sessions", "revoke", childsession)["ok"]
    devices = run(manager, "devices", "list")["devices"]
    assert len(devices) == 2
    assert run(manager, "devices", "revoke", childdevice)["ok"]
    retained = json.loads(manager["profile"].read_text())["grants"]
    assert len(retained) == 1 and retained[0]["id"] != childdevice
    audit = run(manager, "audit", "--limit", "10")["audit"]
    assert any(item["action"] == "cli.device.revoke" for item in audit)
    assert "digest" not in json.dumps(audit)


def test_processor_catalog_and_public_source_named_commands(manager, monkeypatch):
    processors = run(manager, "processors")["processors"]
    assert {item["id"] for item in processors} == {
        "resource_link",
        "personalized_text",
    }
    detail = run(manager, "processors", "--detail")["processors"]
    assert all("parameters" in item and "configuration" in item for item in detail)
    monkeypatch.setattr("extore.source._refresh_after", float("inf"))
    monkeypatch.setattr("extore.source._stars", 123)
    source = manager["run"]("source", "--origin", ORIGIN)["source"]
    assert (
        source["stars"] == 123
        and source["url"] == "https://github.com/TokenNotIncluded/extore"
    )


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.example/api/manage/product",
        "//evil.example/api/manage/product",
        "/api/admin/products",
        "/api/manage/../admin/products",
        "/api/manage/%2e%2e/admin/products",
        "/api/manage/product?product_id=other",
        "/api/manage/cli-ticket",
    ],
)
def test_generic_api_cannot_forward_credentials_or_reach_owner_and_browser_only_routes(
    manager, path
):
    before = len(manager["calls"])
    with pytest.raises(remote.ManageError, match="route|relative"):
        run(manager, "api", "GET", path)
    assert len(manager["calls"]) == before


def test_generic_api_scope_and_secrets_match_named_commands(manager, monkeypatch):
    result = run(manager, "api", "GET", "/api/manage/product")
    assert result["result"]["webhook_secret"] == "[redacted]"
    with pytest.raises(remote.ManageError, match="different product"):
        run(
            manager,
            "api",
            "GET",
            "/api/manage/card-stats",
            "--query",
            "product_id=some-other-product",
        )
    patch_input(manager, monkeypatch, {"count": 1})
    result = run(manager, "api", "POST", "/api/manage/cards", "--json-stdin")
    exported = json.loads(Path(result["output"]).read_text())
    assert len(exported["codes"]) == 1 and exported["codes"][0] not in json.dumps(
        result
    )


@pytest.mark.parametrize(
    "payload", ["[]", '{"value":NaN}', "{", '"' + ("x" * 256001) + '"']
)
def test_json_input_is_bounded_and_invalid_payloads_cannot_mutate(
    manager, monkeypatch, payload
):
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    before = len([call for call in manager["calls"] if call[0] == "PUT"])
    with pytest.raises(remote.ManageError):
        run(manager, "product", "update", "--json-stdin")
    assert len([call for call in manager["calls"] if call[0] == "PUT"]) == before


def test_restricted_grant_cannot_edit_product_or_issue_cards(
    owner, tmp_path, monkeypatch
):
    product = owner.post(
        "/api/admin/products", json={"name": "View only", "parameters": []}
    ).json()["id"]
    link = owner.post(
        "/api/admin/staff",
        json={
            "product_id": product,
            "name": "View grant",
            "permissions": ["queue.view"],
        },
    ).json()["url"]
    directory = tmp_path / "private-restricted"
    directory.mkdir(mode=0o700)
    profile = directory / "cli.json"
    with TestClient(app, base_url=ORIGIN) as api:

        def handler(request):
            response = api.request(
                request.method,
                str(request.url),
                headers=dict(request.headers),
                content=request.read(),
            )
            return httpx.Response(
                response.status_code,
                headers=dict(response.headers),
                content=response.content,
            )

        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(sys, "stdin", io.StringIO(link))
        remote.execute(arguments(profile, "login", "--link-stdin"), transport=transport)
        for argv in (("product", "get"), ("cards", "issue"), ("links", "list")):
            with pytest.raises(remote.ManageError, match="single grant"):
                remote.execute(
                    arguments(profile, *argv, "--product", product), transport=transport
                )
        schema = remote.execute(
            arguments(profile, "product", "schema", "--product", product),
            transport=transport,
        )
        assert schema["schema"]["id"] == product


def test_default_product_list_is_small_and_detail_is_explicit(manager):
    compact = manager["run"]("products")["products"][0]
    assert "parameters" not in compact and "outputs" not in compact
    assert compact["parameters_count"] == 0 and compact["outputs_count"] == 1
    assert "description" not in compact["variants"][0]
    detail = manager["run"]("products", "--detail")["products"][0]
    assert detail["outputs"][0]["key"] == "content"
    query = next(
        call[2] for call in manager["calls"] if call[1] == "/api/manage/products"
    )
    assert query["compact"] == "true"


def test_processor_vault_is_owner_only_and_product_cli_preserves_binding(
    manager, monkeypatch
):
    current = next(
        item
        for item in manager["owner"].get("/api/admin/products").json()
        if item["id"] == manager["pid"]
    )
    definition = {
        "name": current["name"],
        "mode": "script",
        "processor_id": "personalized_text",
        "processor_config": {"template": "Hello $name"},
        "webhook_secret": current["webhook_secret"],
    }
    configured = manager["owner"].put(
        "/api/admin/products/" + manager["pid"], json=definition
    )
    assert configured.status_code == 200, configured.text
    binding_path = "/api/admin/processor-profiles/bindings/" + manager["pid"]
    binding = manager["owner"].get(binding_path).json()
    patch_input(
        manager,
        monkeypatch,
        {
            "mode": "script",
            "processor_id": "personalized_text",
            "processor_config": {"template": "Hello $name"},
        },
    )
    with pytest.raises(remote.ManageError) as denied:
        run(manager, "product", "update", "--json-stdin", "--detail")
    assert denied.value.status == 403
    patch_input(manager, monkeypatch, {"name": "Personalized through CLI"})
    updated = run(manager, "product", "update", "--json-stdin", "--detail")
    assert updated["product"]["processor_config"] == {}
    stored = next(
        item
        for item in manager["owner"].get("/api/admin/products").json()
        if item["id"] == manager["pid"]
    )
    assert stored["processor_config"] == {}
    assert manager["owner"].get(binding_path).json() == binding
    assert "Hello $name" not in json.dumps(updated)
    assert (
        "webhook_secret" in updated["product"]
        and updated["product"]["webhook_secret"] == "[redacted]"
    )


def test_compact_queue_summaries_replace_repeated_schema_with_counts():
    summary = remote.compact_job(
        {
            "id": "job",
            "product_id": "product",
            "state": "processing",
            "variant": {
                "id": "tier",
                "name": "One month",
                "description": "long description",
                "attributes": {"payload": "large schema"},
            },
            "steps": [{"id": "step", "label": {"zh-CN": "步骤"}}],
            "completed_steps": ["step"],
            "params": {"request": "large request"},
        }
    )
    assert summary["variant"] == {"id": "tier", "name": "One month"}
    assert summary["steps_total"] == 1 and summary["steps_done"] == 1
    assert not any(key in summary for key in ("params", "steps", "completed_steps"))


def test_explicit_fulfillment_secrets_are_written_only_to_private_file(
    manager, tmp_path
):
    with pytest.raises(remote.ManageError, match="private --output"):
        run(manager, "product", "get", "--include-secrets")
    target = tmp_path / "private-config.json"
    result = run(
        manager, "product", "get", "--include-secrets", "--output", str(target)
    )
    assert manager["secret"] not in json.dumps(result)
    saved = json.loads(target.read_text())
    assert saved["product"]["webhook_secret"] == manager["secret"]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600

"""Owner CLI round trips real encrypted workflow revisions without secret echoes."""

import io
import json
import stat
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_manage_commands import arguments as manager_arguments
from test_shop_cli import Workspace, product, seed_shop

from extore import (
    cli,
    manage_client,
    manage_commands,
    owner_client,
    processor_profiles,
    processors,
    shop_commands,
)
from extore.app import app
from extore.config import ORIGIN
from extore.db import db
from extore.manage_client import ManageError


@pytest.fixture
def workflow_cli(tmp_path):
    with TestClient(app, base_url=ORIGIN) as api:
        workspace = Workspace(api, tmp_path)
        sid, password, _ = seed_shop("workflow@example.test")
        grant = workspace.login("workflow@example.test", password)["device_id"]
        yield workspace, grant, sid


def definition():
    return {
        "name": "Document workflow",
        "processor_id": "personalized_text",
        "configuration": {"template": "OLD $name"},
        "workflow": {
            "variables": {"MODEL": "old-model", "DROP": "remove-me", "NOTE": "note"},
            "secrets": {
                "API_KEY": "synthetic-workflow-secret-781053",
                "OLD_SECRET": "synthetic-obsolete-secret-781053",
            },
            "runtime": {
                "timeout_seconds": 15,
                "memory_mb": 128,
                "cpu_seconds": 10,
                "max_output_bytes": 65536,
            },
        },
    }


def api(workspace, grant, method, path, *, body=None, output=None, shop=None):
    argv = ["api", method, path, "--grant", grant]
    if body is not None:
        argv += ["--json-file", workspace.file(body)]
    if output is not None:
        argv += ["--output", str(output)]
    if shop is not None:
        argv += ["--query", "shop_id=" + shop]
    return workspace.command(*argv)


def issued(workspace, grant, pid):
    result = workspace.command("cards", "issue", "--product", pid, "--grant", grant)
    path = workspace.directory / result["output"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    return json.loads(path.read_text())["codes"][0]


def task(customer, code):
    response = customer.post("/api/exchange", json={"code": code})
    assert response.status_code == 200, response.text
    response = customer.post(
        "/api/redeem",
        json={"token": response.json()["token"], "params": {"name": "Ada"}},
    )
    assert response.status_code == 200, response.text
    with db() as c:
        return dict(
            c.execute(
                "SELECT * FROM jobs WHERE id=?", (response.json()["id"],)
            ).fetchone()
        )


def assert_public_profile(value, *, revision=1):
    assert value["revision"] == revision
    assert value["configuration"] == {
        "template": "OLD $name" if revision == 1 else "NEW $name"
    }
    assert value["configured_fields"] == ["template"]
    assert "secrets" not in value["workflow"]
    assert "ciphertext" not in json.dumps(value)
    assert "synthetic-workflow-secret" not in json.dumps(value)
    assert "synthetic-obsolete-secret" not in json.dumps(value)


def test_generic_owner_api_covers_workflow_crud_bind_and_frozen_issued_cards(
    workflow_cli,
):
    workspace, grant, _ = workflow_cli
    pid = product(
        workspace, grant, "Workflow", mode="script", processor_id="personalized_text"
    )
    prefix = shop_commands.PROFILE_PREFIX
    created = api(workspace, grant, "POST", prefix, body=definition())["result"]
    assert_public_profile(created)
    profile_id = created["id"]
    path = prefix + "/" + profile_id
    bind_path = prefix + "/bindings/" + pid
    assert api(workspace, grant, "GET", path)["result"] == created
    assert created in api(workspace, grant, "GET", prefix)["result"]
    bound = api(workspace, grant, "PUT", bind_path, body={"profile_id": profile_id})[
        "result"
    ]
    assert bound["profile"]["workflow"] == created["workflow"]
    old_code = issued(workspace, grant, pid)
    exported = workspace.directory / "safe-workflow.json"
    result = api(workspace, grant, "GET", path, output=exported)
    assert stat.S_IMODE(exported.stat().st_mode) == 0o600
    assert "synthetic-workflow-secret" not in exported.read_text() + json.dumps(result)
    assert json.loads(exported.read_text())["result"]["configuration"] == {
        "template": "OLD $name"
    }

    patch = {
        "configuration": {"template": "NEW $name"},
        "workflow": {
            "variables": {"MODEL": "new-model", "NOTE": ""},
            "secrets": {"API_KEY": ""},
            "delete_variables": ["DROP"],
            "delete_secrets": ["OLD_SECRET"],
            "runtime": {"timeout_seconds": 30, "cpu_seconds": 20},
        },
    }
    latest = api(workspace, grant, "PUT", path, body=patch)["result"]
    assert_public_profile(latest, revision=2)
    assert latest["workflow"] == {
        "variables": {"MODEL": "new-model", "NOTE": ""},
        "configured_secret_names": ["API_KEY"],
        "runtime": {
            "timeout_seconds": 30,
            "memory_mb": 128,
            "cpu_seconds": 20,
            "max_output_bytes": 65536,
        },
    }
    previous = api(workspace, grant, "GET", bind_path)["result"]
    assert previous["profile"]["bound_revision"] == 1
    assert previous["profile"]["workflow"] == created["workflow"]
    api(workspace, grant, "PUT", bind_path, body={"profile_id": profile_id})
    new_code = issued(workspace, grant, pid)
    api(workspace, grant, "DELETE", bind_path)
    assert api(workspace, grant, "GET", bind_path)["result"]["profile"] is None
    with pytest.raises(ManageError) as blocked:
        issued(workspace, grant, pid)
    assert blocked.value.status == 409

    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as customer:
        old_job, new_job = task(customer, old_code), task(customer, new_code)
    with db() as c:
        old_config, old_context, old_workflow = processor_profiles.runtime_execution(
            c, old_job, "personalized_text"
        )
        new_config, new_context, new_workflow = processor_profiles.runtime_execution(
            c, new_job, "personalized_text"
        )
        assert old_context["revision"] == 1 and new_context["revision"] == 2
        assert (
            old_config["template"] == "OLD $name"
            and new_config["template"] == "NEW $name"
        )
        assert old_workflow["variables"]["MODEL"] == "old-model"
        assert new_workflow["variables"] == {"MODEL": "new-model", "NOTE": ""}
        assert old_workflow["secrets"] == definition()["workflow"]["secrets"]
        assert new_workflow["secrets"] == {
            "API_KEY": definition()["workflow"]["secrets"]["API_KEY"]
        }
        assert old_workflow["runtime"]["timeout_seconds"] == 15
        assert new_workflow["runtime"]["timeout_seconds"] == 30
        raw = json.dumps([dict(row) for row in c.execute("SELECT * FROM audit")])
        assert not any(
            secret in raw for secret in definition()["workflow"]["secrets"].values()
        )
    api(workspace, grant, "DELETE", path)
    disabled = api(workspace, grant, "GET", path)["result"]
    assert (
        disabled["disabled"] and disabled["workflow"]["configured_secret_names"] == []
    )
    assert {
        ("POST", prefix),
        ("PUT", path),
        ("GET", path),
        ("DELETE", path),
        ("GET", prefix),
        ("PUT", bind_path),
        ("GET", bind_path),
        ("DELETE", bind_path),
    } <= set(workspace.paths)


def test_named_profile_commands_read_plain_config_workflow_and_private_inputs(
    workflow_cli,
):
    workspace, grant, _ = workflow_cli
    created = workspace.command(
        "processor-profiles",
        "create",
        "--grant",
        grant,
        "--json-file",
        workspace.file(definition()),
    )["result"]
    assert_public_profile(created)
    fetched = workspace.command(
        "processor-profiles", "get", created["id"], "--grant", grant
    )["result"]
    assert fetched == created
    updated = workspace.command(
        "processor-profiles",
        "update",
        created["id"],
        "--grant",
        grant,
        "--json-file",
        workspace.file({"workflow": {"variables": {"NOTE": "revised"}}}),
    )["result"]
    assert updated["workflow"]["variables"]["NOTE"] == "revised"
    assert updated["workflow"]["configured_secret_names"] == ["API_KEY", "OLD_SECRET"]
    path = workspace.directory / "unsafe.json"
    path.write_text(json.dumps(definition()))
    path.chmod(0o644)
    before = list(workspace.paths)
    for command in (
        ("processor-profiles", "create"),
        ("api", "POST", shop_commands.PROFILE_PREFIX),
    ):
        with pytest.raises(ManageError) as unsafe:
            workspace.command(*command, "--grant", grant, "--json-file", str(path))
        assert unsafe.value.code == "unsafe_input"
    assert not any(
        path.startswith(shop_commands.PROFILE_PREFIX)
        for _, path in workspace.paths[len(before) :]
    )


def test_root_generic_api_reads_only_the_selected_shop_workflow(owner, tmp_path):
    with TestClient(app, base_url=ORIGIN) as http:
        workspace = Workspace(http, tmp_path)
        root = workspace.root(owner)["device_id"]
        sid, _, _ = seed_shop("root-workflow@example.test")
        other_sid, _, _ = seed_shop("root-other@example.test")
        created = api(
            workspace,
            root,
            "POST",
            shop_commands.PROFILE_PREFIX,
            body={**definition(), "shop_id": sid},
        )["result"]
        assert created["shop_id"] == sid
        assert_public_profile(created)
        assert (
            created
            in api(workspace, root, "GET", shop_commands.PROFILE_PREFIX, shop=sid)[
                "result"
            ]
        )
        assert (
            api(workspace, root, "GET", shop_commands.PROFILE_PREFIX, shop=other_sid)[
                "result"
            ]
            == []
        )


def test_actual_cli_stdout_keeps_templates_variables_but_excludes_secret_values(
    workflow_cli, monkeypatch, capsys
):
    workspace, grant, _ = workflow_cli
    created = api(
        workspace, grant, "POST", shop_commands.PROFILE_PREFIX, body=definition()
    )["result"]
    execute = owner_client.execute
    monkeypatch.setattr(
        owner_client,
        "execute",
        lambda args: execute(args, transport=workspace.transport),
    )
    capsys.readouterr()
    cli.main(
        [
            "admin",
            "--profile",
            str(workspace.profile),
            "api",
            "GET",
            shop_commands.PROFILE_PREFIX + "/" + created["id"],
            "--grant",
            grant,
        ]
    )
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert_public_profile(result["result"])
    assert result["result"]["workflow"]["variables"]["MODEL"] == "old-model"
    assert not any(
        secret in captured.out + captured.err
        for secret in definition()["workflow"]["secrets"].values()
    )


def test_product_cli_authority_cannot_read_store_workflow_configuration(
    workflow_cli, monkeypatch
):
    workspace, owner_grant, _ = workflow_cli
    pid = product(
        workspace,
        owner_grant,
        "Workflow",
        mode="script",
        processor_id="personalized_text",
    )
    created = api(
        workspace, owner_grant, "POST", shop_commands.PROFILE_PREFIX, body=definition()
    )["result"]
    api(
        workspace,
        owner_grant,
        "PUT",
        shop_commands.PROFILE_PREFIX + "/bindings/" + pid,
        body={"profile_id": created["id"]},
    )
    invitation = workspace.command(
        "links",
        "create",
        "--product",
        pid,
        "--grant",
        owner_grant,
        "--json-file",
        workspace.file(
            {
                "name": "Product manager",
                "permissions": ["product.edit", "fulfillment.configure"],
            }
        ),
    )
    link = json.loads(Path(invitation["output"]).read_text())["url"]
    profile = workspace.directory / "product-private" / "cli.json"
    monkeypatch.setattr(sys, "stdin", io.StringIO(link))
    manage_client.execute(
        manager_arguments(
            profile,
            "login",
            "--link-stdin",
            "--client-name",
            "Workflow product test agent",
            "--agent-type",
            "Codex test fixture",
        ),
        transport=workspace.transport,
    )
    result = manage_client.execute(
        manager_arguments(profile, "product", "get", "--product", pid, "--detail"),
        transport=workspace.transport,
    )
    assert result["product"]["processor_config"] == {}
    assert "workflow" not in result["product"]
    output = json.dumps(result)
    assert "old-model" not in output and "OLD $name" not in output
    assert not any(
        secret in output for secret in definition()["workflow"]["secrets"].values()
    )
    with pytest.raises(ManageError) as denied:
        manage_client.execute(
            manager_arguments(
                profile, "api", "GET", shop_commands.PROFILE_PREFIX, "--product", pid
            ),
            transport=workspace.transport,
        )
    assert denied.value.code == "invalid_path"


def test_cli_configuration_classification_is_strict_and_duplicates_fail_closed(
    monkeypatch,
):
    fields = [
        {"key": "template", "secret": False},
        {"key": "secret", "secret": True},
        {"key": "missing"},
        {"key": "zero", "secret": 0},
        {"key": "string", "secret": "false"},
        {"key": "duplicate", "secret": False},
        {"key": "duplicate", "secret": False},
        {"key": "object", "secret": False},
    ]
    monkeypatch.setattr(
        processors, "specification", lambda _: {"configuration": fields}
    )
    values = {field["key"]: "sensitive-" + field["key"] for field in fields}
    values.update(
        template="Editable template",
        unknown="sensitive-unknown",
        object={"nested": "sensitive-object"},
    )
    product_value = manage_commands._public(
        {"processor_id": "test", "processor_config": values}
    )
    assert product_value["processor_config"]["template"] == "Editable template"
    assert not any(
        value in json.dumps(product_value)
        for key, value in values.items()
        if key != "template" and isinstance(value, str)
    )
    profile = shop_commands.profile_metadata(
        {
            "processor_id": "test",
            "configuration": values,
            "configured_fields": ["template", "secret"],
            "ciphertext": "sensitive-envelope",
        }
    )
    assert profile["configuration"] == {"template": "Editable template"}
    assert "sensitive-" not in json.dumps(profile)


def test_profile_response_cannot_echo_raw_workflow_secrets_or_runtime_commands():
    names = ["KEY" + str(index) for index in range(65)]
    value = shop_commands.profile_metadata(
        {
            "workflow": {
                "variables": {
                    "MODEL": "plain-model",
                    "KEY64": "secret-collision",
                    "API_KEY": "undeclared-secret-collision",
                    "EXTORE_AUTH": "reserved",
                },
                "runtime": {
                    "timeout_seconds": 30,
                    "memory_mb": True,
                    "command": "secret-command",
                    "image": "secret-image",
                },
                "configured_secret_names": names,
                "secrets": {"API_KEY": "secret-value"},
            },
            "ciphertext": "secret-envelope",
        }
    )
    assert value["workflow"]["variables"] == {"MODEL": "plain-model"}
    assert value["workflow"]["runtime"] == {"timeout_seconds": 30}
    assert not any(
        marker in json.dumps(value)
        for marker in (
            "secret-collision",
            "undeclared-secret-collision",
            "reserved",
            "secret-command",
            "secret-image",
            "secret-value",
            "secret-envelope",
        )
    )

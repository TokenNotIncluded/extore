"""Offline developer commands never emit product credentials or touch a server."""

import json
import os
import stat
import subprocess
import sys

import pytest

from extore import cli
from extore.task_flow_schema import definition_schema


def inputs(tmp_path):
    definition = tmp_path / "definition.json"
    product = tmp_path / "product.json"
    definition.write_text(
        json.dumps(
            {
                "version": 1,
                "entry": "begin",
                "nodes": [
                    {"id": "begin", "kind": "display", "next": "done"},
                    {"id": "done", "kind": "end", "state": "succeeded"},
                ],
            }
        ),
        encoding="utf-8",
    )
    product.write_text(
        json.dumps(
            {
                "mode": "manual",
                "webhook_secret": "never-echo-this-value",
                "processor_config": {"password": "never-echo-this-value"},
            }
        ),
        encoding="utf-8",
    )
    return definition, product


def validate_args(definition, product):
    return [
        "workflow",
        "validate",
        "--definition",
        str(definition),
        "--product",
        str(product),
    ]


def forbid(*args, **kwargs):
    raise AssertionError(
        "Offline workflow commands must not initialize or use IO services"
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["workflow", "--help"],
        ["workflow", "schema", "--help"],
        ["workflow", "validate", "--help"],
    ],
)
def test_workflow_help_without_initialization(arguments, monkeypatch, capsys):
    monkeypatch.setattr(cli, "init", forbid)
    with pytest.raises(SystemExit) as exited:
        cli.main(arguments)
    assert exited.value.code == 0
    assert "usage: extore workflow" in capsys.readouterr().out


def test_schema_and_validate_are_offline_and_omit_product_values(
    tmp_path, monkeypatch, capsys
):
    import socket

    from extore import db

    monkeypatch.setattr(cli, "init", forbid)
    monkeypatch.setattr(db, "db", forbid)
    monkeypatch.setattr(socket, "create_connection", forbid)
    assert cli.main(["workflow", "schema"]) == 0
    assert definition_schema() == json.loads(capsys.readouterr().out)
    definition, product = inputs(tmp_path)
    assert cli.main(validate_args(definition, product)) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert "never-echo-this-value" not in captured.out + captured.err
    assert result["definition"]["nodes"][0]["timeout_seconds"] is None
    assert result["summary"] == {
        "version": 1,
        "entry": "begin",
        "node_count": 2,
        "nodes": {"input": 0, "process": 0, "display": 1, "end": 1},
        "transition_limit": 256,
    }


def test_null_definition_preserves_simple_path(tmp_path, capsys):
    definition, product = inputs(tmp_path)
    definition.write_text("null")
    assert cli.main(validate_args(definition, product)) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["definition"] is None
    assert result["summary"] == {
        "version": None,
        "entry": None,
        "node_count": 0,
        "nodes": {"input": 0, "process": 0, "display": 0, "end": 0},
        "transition_limit": 256,
    }
    assert result["ok"] is True


@pytest.mark.parametrize(
    "raw",
    [
        b'{"x":1,"x":2}',
        b'{"outer":{"x":1,"x":2}}',
        b"NaN",
        b"Infinity",
        b'{"x":1e400}',
        b"\xff",
        b'"\xff"',
        "null".encode("utf-16"),
        b'{"private":"never-echo-this-value"',
        b"[" * 2000 + b"]" * 2000,
        b" " * 100001,
    ],
    ids=[
        "duplicate",
        "nested-duplicate",
        "nan",
        "infinity",
        "overflow",
        "utf8",
        "utf8-string",
        "utf16",
        "syntax",
        "deep",
        "size",
    ],
)
def test_invalid_input_is_bounded_strict_and_private(tmp_path, capsys, raw):
    definition, product = inputs(tmp_path)
    definition.write_bytes(raw)
    with pytest.raises(SystemExit) as exited:
        cli.main(validate_args(definition, product))
    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["ok"] is False and error["error"] == "invalid_input"
    assert "never-echo-this-value" not in captured.err
    assert str(tmp_path) not in captured.err


@pytest.mark.parametrize(
    "bad_product",
    [
        "null",
        '{"outputs":[{"key":"x","label":{"en":"X"},"sensitive":true}],"webhook_secret":"never-echo-this-value"}',
        '{"x":1,"x":2}',
        " " * 200001,
    ],
)
def test_invalid_product_and_semantic_failures_do_not_echo_values(
    tmp_path, capsys, bad_product
):
    definition, product = inputs(tmp_path)
    product.write_text(bad_product)
    with pytest.raises(SystemExit) as exited:
        cli.main(validate_args(definition, product))
    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"] in ("invalid_input", "invalid_definition")
    assert "never-echo-this-value" not in captured.err


def test_reference_validation_uses_authoritative_validator(tmp_path, capsys):
    definition, product = inputs(tmp_path)
    raw = json.loads(definition.read_text())
    raw["nodes"][0]["next"] = "missing"
    definition.write_text(json.dumps(raw))
    with pytest.raises(SystemExit) as exited:
        cli.main(validate_args(definition, product))
    assert exited.value.code == 2
    assert json.loads(capsys.readouterr().err)["error"] == "invalid_definition"


def test_private_output_does_not_overwrite_existing_file(tmp_path, capsys):
    definition, product = inputs(tmp_path)
    output = tmp_path / "normalized.json"
    arguments = [*validate_args(definition, product), "--output", str(output)]
    assert cli.main(arguments) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["output"] == str(output) and status["ok"] is True
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(output.read_text())["ok"] is True
    before = output.read_bytes()
    with pytest.raises(SystemExit) as exited:
        cli.main(arguments)
    assert exited.value.code == 2
    assert json.loads(capsys.readouterr().err)["error"] == "output_exists"
    assert output.read_bytes() == before


def test_reject_input_symlink_and_nonregular_file(tmp_path, capsys):
    definition, product = inputs(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(definition)
    for path in (link, tmp_path, tmp_path / "missing.json"):
        with pytest.raises(SystemExit) as exited:
            cli.main(validate_args(path, product))
        assert exited.value.code == 2
        assert json.loads(capsys.readouterr().err)["error"] == "invalid_input"


@pytest.mark.parametrize(
    "entrypoint",
    [
        ["-m", "extore.cli"],
        ["-c", "import sys; from extore.cli import main; sys.exit(main())"],
    ],
)
def test_module_and_console_commands_do_not_create_data_directory(tmp_path, entrypoint):
    definition, product = inputs(tmp_path)
    data = tmp_path / "unused-data"
    for arguments in (["workflow", "schema"], validate_args(definition, product)):
        completed = subprocess.run(
            [sys.executable, *entrypoint, *arguments],
            env={**os.environ, "EXTORE_DATA": str(data)},
            text=True,
            capture_output=True,
            timeout=15,
        )
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout)
        assert not data.exists()


def test_no_output_secret_from_unknown_definition_property(tmp_path, capsys):
    definition, product = inputs(tmp_path)
    value = json.loads(definition.read_text())
    value["secret"] = "never-echo-this-value"
    definition.write_text(json.dumps(value))
    with pytest.raises(SystemExit):
        cli.main(validate_args(definition, product))
    captured = capsys.readouterr()
    assert "never-echo-this-value" not in captured.out + captured.err


def test_output_error_does_not_expose_path(tmp_path, capsys):
    definition, product = inputs(tmp_path)
    path = tmp_path / "missing-parent" / "out.json"
    with pytest.raises(SystemExit) as exited:
        cli.main([*validate_args(definition, product), "--output", str(path)])
    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert json.loads(captured.err)["error"] == "unsafe_output"
    assert str(path) not in captured.err

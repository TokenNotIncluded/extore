"""Developer CLI prepares contribution data without server or code execution."""

import json
import os
import stat
import subprocess
import sys

import pytest

from extore import cli
from extore.processor_contributions import PROPOSAL_LIMITS, contribution_contract


def forbidden(*args, **kwargs):
    raise AssertionError("Contribution CLI must be offline and must not initialize DB")


@pytest.mark.parametrize(
    "arguments", [["processors", "--help"], ["processors", "contribute", "--help"]]
)
def test_help_without_initialization(arguments, monkeypatch, capsys):
    monkeypatch.setattr(cli, "init", forbidden)
    with pytest.raises(SystemExit) as exited:
        cli.main(arguments)
    assert exited.value.code == 0
    assert "usage: extore processors" in capsys.readouterr().out


def test_global_processors_version_is_metadata_only(monkeypatch, capsys):
    monkeypatch.setattr(cli, "init", forbidden)
    monkeypatch.setattr(cli.metadata, "version", lambda package: "9.8.7")
    with pytest.raises(SystemExit) as exited:
        cli.main(["processors", "--version"])
    assert exited.value.code == 0
    assert capsys.readouterr().out == "extore 9.8.7\n"


@pytest.mark.parametrize("language", ["zh-CN", "en"])
def test_offline_cli_has_contract_and_explicit_data_only(language, monkeypatch, capsys):
    import socket

    from extore import db, processor_runtime

    monkeypatch.setattr(cli, "init", forbidden)
    monkeypatch.setattr(db, "db", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(processor_runtime, "sandbox_command", forbidden)
    monkeypatch.setenv("EXTORE_PROCESSOR_SECRET", "PRIVATE_MERCHANT_CONFIG")
    assert (
        cli.main(
            [
                "processors",
                "contribute",
                "--language",
                language,
                "--name",
                "CSV 描述统计",
                "--summary",
                "$(touch SHOULD_NOT_EXIST)\n```",
                "--inputs",
                "CSV文本",
                "--outputs",
                "JSON 汇总",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert captured.err == ""
    assert result["ok"] is True and result["language"] == language
    assert result["contribution"] == contribution_contract()
    assert result["proposal"] == {
        "name": "CSV 描述统计",
        "summary": "$(touch SHOULD_NOT_EXIST)\n```",
        "inputs": "CSV文本",
        "outputs": "JSON 汇总",
    }
    assert result["prompt"].startswith(
        result["contribution"]["developer_prompt"][language]
    )
    assert "PRIVATE_MERCHANT_CONFIG" not in captured.out + captured.err


def test_default_language_and_empty_proposal(capsys):
    assert cli.main(["processors", "contribute"]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["language"] == "zh-CN"
    assert value["proposal"] == dict.fromkeys(PROPOSAL_LIMITS, "")


@pytest.mark.parametrize(
    "option,value",
    [
        ("--language", "PRIVATE_SENTINEL"),
        ("--name", "PRIVATE_SENTINEL\nname"),
        ("--summary", "PRIVATE_SENTINEL\x1b"),
        ("--outputs", "PRIVATE_SENTINEL\ud800"),
        *(
            ("--" + key, "PRIVATE_SENTINEL" + "a" * maximum)
            for key, maximum in PROPOSAL_LIMITS.items()
        ),
    ],
)
def test_invalid_input_is_bounded_and_not_echoed(tmp_path, capsys, option, value):
    destination = tmp_path / "not-created.json"
    with pytest.raises(SystemExit) as exited:
        cli.main(
            ["processors", "contribute", option, value, "--output", str(destination)]
        )
    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"] == "invalid_input"
    assert "PRIVATE_SENTINEL" not in captured.err
    assert not destination.exists()


def test_private_output_uses_existing_new_file_export_rules(tmp_path, capsys):
    destination = tmp_path / "contribute.json"
    args = [
        "processors",
        "contribute",
        "--name",
        "Simple processor",
        "--output",
        str(destination),
    ]
    assert cli.main(args) == 0
    status = json.loads(capsys.readouterr().out)
    assert status == {
        "ok": True,
        "output": str(destination),
        "bytes": destination.stat().st_size,
    }
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    original = destination.read_bytes()
    value = json.loads(original)
    assert value["contribution"] == contribution_contract()
    assert value["proposal"]["name"] == "Simple processor"
    with pytest.raises(SystemExit) as exited:
        cli.main(args)
    assert exited.value.code == 2
    assert json.loads(capsys.readouterr().err)["error"] == "output_exists"
    assert destination.read_bytes() == original


def test_output_failure_does_not_echo_destination(tmp_path, capsys):
    destination = tmp_path / "private-parent" / "secret.json"
    with pytest.raises(SystemExit) as exited:
        cli.main(["processors", "contribute", "--output", str(destination)])
    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert json.loads(captured.err)["error"] == "unsafe_output"
    assert str(destination) not in captured.err


@pytest.mark.parametrize(
    "entrypoint",
    [
        ["-m", "extore.cli"],
        ["-c", "import sys; from extore.cli import main; sys.exit(main())"],
    ],
)
def test_actual_module_and_console_are_offline_and_exit_zero(tmp_path, entrypoint):
    data = tmp_path / "unused-data"
    completed = subprocess.run(
        [
            sys.executable,
            *entrypoint,
            "processors",
            "contribute",
            "--language",
            "en",
            "--name",
            "Simple processor",
        ],
        env={**os.environ, "EXTORE_DATA": str(data)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert json.loads(completed.stdout)["proposal"]["name"] == "Simple processor"
    assert not data.exists()

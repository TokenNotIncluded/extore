import os
import stat
import subprocess
import sys
from importlib import metadata

import pytest
from argon2 import PasswordHasher

from extore import cli, worker
from extore.db import db, set_setting, setting
from extore.models import Product
from extore.security import digest

COMMANDS = (
    "init",
    "bootstrap",
    "reset-auth",
    "integration-key",
    "demo",
    "serve",
    "worker",
    "manage",
    "customer",
    "admin",
)


def forbid_init():
    raise AssertionError("argument-only commands must not initialize the database")


@pytest.mark.parametrize("command", [None, *COMMANDS])
def test_help_exits_before_initialization(command, monkeypatch, capsys):
    monkeypatch.setattr(cli, "init", forbid_init)
    with pytest.raises(SystemExit) as exited:
        cli.main([command, "--help"] if command else ["--help"])
    assert exited.value.code == 0
    assert "usage: extore" in capsys.readouterr().out


@pytest.mark.parametrize("command", [None, *COMMANDS])
def test_version_uses_installed_metadata_without_initialization(
    command, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "init", forbid_init)
    calls = []

    def installed_version(package):
        calls.append(package)
        return "9.8.7"

    monkeypatch.setattr(cli.metadata, "version", installed_version)
    with pytest.raises(SystemExit) as exited:
        cli.main([command, "--version"] if command else ["--version"])
    assert exited.value.code == 0
    captured = capsys.readouterr()
    assert captured.out == "extore 9.8.7\n" and captured.err == ""
    assert calls == ["extore"]


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["unknown"],
        ["init", "--unknown"],
        ["serve", "unexpected"],
        ["serve", "--port", "0"],
        ["serve", "--port", "65536"],
        ["serve", "--port", "abc"],
        ["serve", "--host", ""],
        ["serve", "--host", "bad host"],
        ["worker", "--port", "8000"],
    ],
)
def test_invalid_arguments_exit_before_initialization(arguments, monkeypatch):
    monkeypatch.setattr(cli, "init", forbid_init)
    with pytest.raises(SystemExit) as exited:
        cli.main(arguments)
    assert exited.value.code == 2


@pytest.mark.parametrize(
    "arguments",
    [
        ["--help"],
        ["--version"],
        ["serve", "--port", "0"],
        ["admin", "--help"],
        ["customer", "--help"],
        ["manage", "--help"],
    ],
)
def test_module_argument_only_invocations_do_not_create_data_directory(
    tmp_path, arguments
):
    data_dir = tmp_path / "unused-data"
    result = subprocess.run(
        [sys.executable, "-m", "extore.cli", *arguments],
        env={**os.environ, "EXTORE_DATA": str(data_dir)},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == (2 if arguments[-1] == "0" else 0)
    assert not data_dir.exists()
    if arguments == ["--version"]:
        assert result.stdout == f"extore {metadata.version('extore')}\n"


def test_missing_package_metadata_is_an_argument_error(monkeypatch):
    monkeypatch.setattr(cli, "init", forbid_init)

    def missing(package):
        raise metadata.PackageNotFoundError(package)

    monkeypatch.setattr(cli.metadata, "version", missing)
    with pytest.raises(SystemExit) as exited:
        cli.main(["--version"])
    assert exited.value.code == 2


@pytest.mark.parametrize(
    "arguments,expected",
    [
        (["serve"], {"host": "127.0.0.1", "port": 8000}),
        (["serve", "--host", "::1", "--port", "65535"], {"host": "::1", "port": 65535}),
    ],
)
def test_serve_delegates_to_uvicorn_without_starting_worker(
    arguments, expected, monkeypatch
):
    import uvicorn

    monkeypatch.setattr(cli, "init", forbid_init)
    monkeypatch.setattr(worker, "main", forbid_init)
    calls = []
    monkeypatch.setattr(
        uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs))
    )
    cli.main(arguments)
    assert calls == [("extore.app:app", expected)]


def test_worker_delegates_to_existing_foreground_entrypoint(monkeypatch):
    import uvicorn

    monkeypatch.setattr(cli, "init", forbid_init)
    monkeypatch.setattr(uvicorn, "run", forbid_init)
    calls = []
    monkeypatch.setattr(worker, "main", lambda: calls.append("worker"))
    cli.main(["worker"])
    assert calls == ["worker"]


@pytest.mark.parametrize("command", ["init", "bootstrap"])
def test_existing_initialization_is_not_overwritten(command, monkeypatch):
    with db() as c:
        set_setting(c, "bootstrap_password", "existing-hash")
    monkeypatch.setattr(cli.getpass, "getpass", forbid_init)
    with pytest.raises(SystemExit, match="Already initialized"):
        cli.main([command])
    with db() as c:
        assert setting(c, "bootstrap_password") == "existing-hash"


def test_init_preserves_interactive_password_flow(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "DATA", tmp_path)
    prompts = []

    def password(prompt):
        prompts.append(prompt)
        return "cli-test-initial-password"

    monkeypatch.setattr(cli.getpass, "getpass", password)
    cli.main(["init"])
    assert len(prompts) == 2
    with db() as c:
        assert PasswordHasher().verify(setting(c, "bootstrap_password"), password(""))
    assert "cli-test-initial-password" not in capsys.readouterr().out


def test_bootstrap_writes_private_password_file_without_printing_secret(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(cli, "DATA", tmp_path)
    cli.main(["bootstrap"])
    password_file = tmp_path / "bootstrap-password.txt"
    password = password_file.read_text().strip()
    assert stat.S_IMODE(password_file.stat().st_mode) == 0o600
    with db() as c:
        assert PasswordHasher().verify(setting(c, "bootstrap_password"), password)
    assert password not in capsys.readouterr().out


def test_reset_auth_cancellation_keeps_credentials_and_password(monkeypatch):
    with db() as c:
        set_setting(c, "bootstrap_password", "existing-hash")
        c.execute("INSERT INTO credentials VALUES ('passkey',X'01',0,'Existing',0)")
    monkeypatch.setattr("builtins.input", lambda prompt: "CANCEL")
    monkeypatch.setattr(cli.getpass, "getpass", forbid_init)
    with pytest.raises(SystemExit, match="Cancelled"):
        cli.main(["reset-auth"])
    with db() as c:
        assert setting(c, "bootstrap_password") == "existing-hash"
        assert c.execute("SELECT count(*) FROM credentials").fetchone()[0] == 1


def test_reset_auth_keeps_product_and_card_data(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "DATA", tmp_path)
    product = Product(name="Existing product")
    with db() as c:
        c.execute(
            "INSERT INTO products VALUES ('product',?,0)", (product.model_dump_json(),)
        )
        c.execute(
            "INSERT INTO cards(id,digest,product_id,created) VALUES ('card','digest','product',0)"
        )
        c.execute("INSERT INTO credentials VALUES ('passkey',X'01',0,'Existing',0)")
    monkeypatch.setattr("builtins.input", lambda prompt: "RESET")
    monkeypatch.setattr(
        cli.getpass, "getpass", lambda prompt: "cli-test-recovery-password"
    )
    cli.main(["reset-auth"])
    with db() as c:
        assert c.execute("SELECT count(*) FROM credentials").fetchone()[0] == 0
        assert c.execute("SELECT id FROM products").fetchone()[0] == "product"
        assert c.execute("SELECT id FROM cards").fetchone()[0] == "card"
        assert PasswordHasher().verify(
            setting(c, "bootstrap_password"), "cli-test-recovery-password"
        )


def test_integration_key_rotation_does_not_change_authentication(monkeypatch, capsys):
    with db() as c:
        set_setting(c, "bootstrap_password", "existing-hash")
        c.execute("INSERT INTO credentials VALUES ('passkey',X'01',0,'Existing',0)")
    monkeypatch.setattr(cli, "token", lambda: "cli-test-platform-key")
    cli.main(["integration-key"])
    with db() as c:
        assert setting(c, "integration_key") == digest("cli-test-platform-key")
        assert setting(c, "bootstrap_password") == "existing-hash"
        assert c.execute("SELECT count(*) FROM credentials").fetchone()[0] == 1
    assert "cli-test-platform-key" in capsys.readouterr().out


def test_demo_command_keeps_approved_processor_behavior(capsys):
    cli.main(["demo"])
    with db() as c:
        product = c.execute("SELECT config FROM products").fetchone()[0]
        assert '"processor_id":"personalized_text"' in product
        assert c.execute("SELECT count(*) FROM cards").fetchone()[0] == 1
    assert "演示卡密" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="empty database"):
        cli.main(["demo"])

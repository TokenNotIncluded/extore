"""Native local administration keeps existing state and explicit command intent."""

import hashlib
import os
import sqlite3
import subprocess
from pathlib import Path


def test_native_admin_help_bootstrap_and_key_rotation(tmp_path):
    root = Path(__file__).resolve().parents[1]
    binary = root / "target/debug/extore"
    if not binary.is_file():
        import pytest

        pytest.skip("Build the native executable before the native CLI checks")
    data = tmp_path / "instance"
    env = dict(os.environ, EXTORE_DATA=str(data), EXTORE_ORIGIN="http://localhost:8000")

    def run(*args):
        return subprocess.run(
            [str(binary), *args], env=env, capture_output=True, text=True, timeout=15
        )

    assert run("reset-auth", "--help").returncode == 0
    assert not data.exists()
    assert run("init", "--unexpected").returncode != 0
    assert not data.exists()
    assert run("bootstrap").returncode == 0
    database = data / "extore.sqlite3"
    before = (data / "bootstrap-password.txt").read_bytes()
    with sqlite3.connect(database) as connection:
        fingerprint = connection.execute(
            "SELECT fingerprint FROM secret_store_state"
        ).fetchone()
    assert run("bootstrap").returncode == 0
    assert (data / "bootstrap-password.txt").read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT fingerprint FROM secret_store_state").fetchone()
            == fingerprint
        )
    first = run("integration-key")
    assert first.returncode == 0, first.stderr
    key = first.stdout.strip()
    assert len(key) == 43
    with sqlite3.connect(database) as connection:
        stored = connection.execute(
            "SELECT value FROM settings WHERE key='integration_key'"
        ).fetchone()[0]
        assert stored == hashlib.sha256(key.encode()).hexdigest()
        assert key not in str(connection.execute("SELECT * FROM audit").fetchall())
    second = run("integration-key")
    assert second.returncode == 0 and second.stdout != first.stdout
    assert (data / "bootstrap-password.txt").stat().st_mode & 0o777 == 0o600
